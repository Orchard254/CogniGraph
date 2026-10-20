"""
Shared constants, embedding helpers, and schema for the project memory system.

SQLite-backed, with FTS5 full-text search, graph edge relationships, code provenance,
hybrid RRF query ranking (BM25 + tokenized + semantic embeddings), recency decay,
auto-dedup guard, optimistic concurrency control, and automated consolidation.
"""
import os
import re
import sys
import logging
from pathlib import Path
from typing import Optional, List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional semantic-embedding backend (fastembed + numpy).
# Gracefully degrades to tokenized-only similarity if not installed.
# ---------------------------------------------------------------------------
try:
    import numpy as np
    from fastembed import TextEmbedding
    _SEMANTIC_AVAILABLE = True
except ImportError:
    _SEMANTIC_AVAILABLE = False
    np = None
    TextEmbedding = None

# Optional sqlite-vec python package (for vec0 virtual table integration)
try:
    import sqlite_vec as _sqlite_vec_pkg
    _SQLITE_VEC_PKG_AVAILABLE = True
except Exception:
    _SQLITE_VEC_PKG_AVAILABLE = False
    _sqlite_vec_pkg = None

# Vec dimension for all-MiniLM-L6-v2 embedding model
_VEC_DIM = 384

_EMBED_MODEL = None          # lazy-loaded singleton
_EMBED_MODEL_NAME = "fastembed:all-MiniLM-L6-v2"   # 80 MB, 384-dim, fast & accurate


def _get_embed_model():
    """Lazy-load the embedding model once and cache it."""
    global _EMBED_MODEL, _SEMANTIC_AVAILABLE
    if not _SEMANTIC_AVAILABLE:
        return None
    if _EMBED_MODEL is None:
        try:
            _EMBED_MODEL = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
        except Exception:
            _SEMANTIC_AVAILABLE = False
            return None
        _align_tokenizer_truncation(_EMBED_MODEL)
    return _EMBED_MODEL


def _align_tokenizer_truncation(model) -> None:
    """Force truncation max_length down to the tokenizer's fixed padding length.

    fastembed's bundled MiniLM tokenizer is configured with padding
    length=128 but truncation max_length=256. The `tokenizers` library only
    pads sequences SHORTER than the padding target, so any document
    tokenizing to 129..256 tokens is left at its natural length. onnx_embed
    then does np.array([e.ids for e in encoded]) over that ragged batch and
    raises ValueError("...inhomogeneous shape after 1 dimensions"), which
    silently aborted batched embedding (bootstrap, backfill) leaving facts
    unembedded. Truncating to the padding length makes every sequence
    exactly 128 tokens, so batches are always rectangular.

    No-op unless the model exposes a tokenizer with a fixed padding length.
    """
    try:
        tokenizer = getattr(getattr(model, "model", None), "tokenizer", None)
        if tokenizer is None:
            return
        padding = getattr(tokenizer, "padding", None) or {}
        truncation = getattr(tokenizer, "truncation", None) or {}
        pad_len = padding.get("length")
        max_len = truncation.get("max_length")
        if isinstance(pad_len, int) and isinstance(max_len, int) and max_len > pad_len:
            tokenizer.enable_truncation(max_length=pad_len)
    except Exception:
        # Never let tokenizer tuning break model loading: a ragged batch is
        # recoverable per-fact, an unavailable model is not.
        pass


def _encode(text: str):
    """Return a normalized numpy float32 embedding, or None if unavailable."""
    model = _get_embed_model()
    if model is None or not text:
        return None
    vec = next(model.embed([text]), None)
    if vec is None:
        return None
    vec = np.asarray(vec, dtype="float32")
    norm = np.linalg.norm(vec)
    if norm > 0.0:
        vec = vec / norm
    return vec.astype("float32")


def _vec_to_blob(vec) -> bytes:
    """Pack float32 numpy array into raw bytes for SQLite BLOB storage."""
    return vec.tobytes()


def _blob_to_vec(blob: bytes):
    """Unpack raw bytes back to numpy float32 array."""
    if not _SEMANTIC_AVAILABLE or not blob:
        return None
    return np.frombuffer(blob, dtype="float32")


def _cosine_sim(a, b) -> float:
    """Cosine similarity between two normalized float32 vectors."""
    if a is None or b is None:
        return 0.0
    dot = float(np.dot(a, b))
    # Both are already L2-normalized from SentenceTransformer, so dot == cosine
    return max(0.0, dot)

VALID_CATEGORIES = {"component", "decision", "constraint", "state", "other"}
VALID_CONFIDENCE = {"confirmed", "assumed", "uncertain"}
VALID_RELATIONS = {"depends_on", "implements", "conflicts_with", "relates_to"}
MAX_LIMIT = 200


class ProjectFieldError(ValueError):
    """A caller asked to project onto a field that does not exist.

    Raised rather than silently ignored on purpose. Field projection exists to
    cut token cost, and the only reason it can be trusted is that a mistake in
    the field list is loud: a caller who misspells `file_path` and receives a
    short, plausible result set with no anchor has been handed silent data
    loss, which is the exact failure mode projection is supposed to reduce.

    Note that `id` is auto-added rather than required. The first draft raised
    on a projection that omitted `id`, which looked more explicit but created
    an error path with a bad escape: a caller scripting `fields="content"`
    hits the error, and the obvious "fix" is to drop `content` too. Auto-adding
    makes the safety property structural instead of something to remember.
    """


# Every key a query_hybrid result may carry. Derived from the live corpus
# rather than hand-maintained, so a new result key cannot make a previously
# valid projection raise. This is a *permissive* set: anything not listed is
# rejected, and the list is checked by test_every_live_result_key_is_known.
KNOWN_RESULT_FIELDS = frozenset({
    "id", "content", "category", "rationale", "confidence", "session_id",
    "file_path", "tags", "importance", "line_start", "line_end", "code_hash",
    "git_commit_sha", "created_at", "updated_at", "access_count",
    "last_accessed_at", "score", "source", "version", "superseded_by",
    "deprecated_at", "deprecated_reason",
    # query_hybrid(..., explain=True) only
    "fused_score", "lanes",
    # episodic / locate_fact only
    "status", "closed_revision", "memory_id", "fact_id", "in_memory",
    "in_episodic", "episodic_status", "memory_deprecated_at",
})


def parse_fields(fields):
    """Normalize a `fields` argument to an ordered list, or None for 'all'.

    Accepts a comma-separated string (handy on the CLI, where a list is
    awkward to type) or an already-split list. None, empty, and
    whitespace-only all mean "no projection", so a caller can pass through an
    unset CLI option without branching.
    """
    if fields is None:
        return None
    if isinstance(fields, str):
        parts = [p.strip() for p in fields.split(",")]
    else:
        parts = [str(p).strip() for p in fields]
    parts = [p for p in parts if p]
    if not parts:
        return None
    seen, out = set(), []
    for p in parts:
        if p not in seen:          # keep caller order, drop repeats
            seen.add(p)
            out.append(p)
    return out


def project_fields(results, fields, extra_keep=()):
    """Narrow each result dict to `fields` (plus `id`, always).

    `id` is auto-added if absent -- see ProjectFieldError for why it is not
    required. `extra_keep` carries keys the caller asked for through a
    *different* flag: query_hybrid passes ("fused_score", "lanes") when
    explain=True, so that an explicit explain request is not silently undone
    by a narrow `fields` whitelist. Same principle as auto-adding `id` -- a
    flag you passed is itself a request for its keys.

    A requested field that a given result does not carry is simply absent from
    that result -- never invented, never null-filled, because a fabricated null
    would be indistinguishable from a real one.
    """
    wanted = parse_fields(fields)
    if wanted is None:
        return results
    unknown = [f for f in wanted if f not in KNOWN_RESULT_FIELDS]
    if unknown:
        raise ProjectFieldError(
            f"unknown result field(s): {', '.join(unknown)}. "
            f"Valid fields: {', '.join(sorted(KNOWN_RESULT_FIELDS))}")
    keep = set(wanted) | {"id"} | set(extra_keep)
    return [{k: v for k, v in r.items() if k in keep} for r in results]


_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                              # AWS access key id
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
    # Match common environment-variable names such as OPENAI_API_KEY and
    # SERVICE_SECRET_TOKEN, not just labels that stand alone in prose.
    re.compile(
        r"(?i)\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|private[_-]?key|"
        r"secret(?:[_-][a-z0-9]+)*|password|passwd|token(?:[_-][a-z0-9]+)*)"
        r"\b\s*[:=]\s*['\"]?[A-Za-z0-9_./+=:-]{12,}"
    ),
    re.compile(r"(?i)://[^/\s:]+:[^/\s@]+@"),                     # user:pass@ embedded in a URL
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),                # GitHub classic tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),              # GitHub fine-grained tokens
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),                  # GitLab personal access tokens
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b"),    # Stripe secret/restricted keys
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                    # Google API keys
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),              # Slack tokens
    re.compile(r"\bnpm_[A-Za-z0-9]{36,}\b"),                     # npm access tokens
    re.compile(r"\bpypi-[A-Za-z0-9_-]{30,}\b"),                  # PyPI tokens
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),                    # OpenAI/Anthropic-style secret keys
]


def looks_like_secret(text: Optional[str]) -> bool:
    """Best-effort secret detector for common key labels and token formats."""
    if not text:
        return False
    return any(p.search(text) for p in _SECRET_PATTERNS)


def tokenize_text(text: Optional[str]) -> set:
    """Extracts code tokens, words, path parts, and handles underscores/hyphens/camelCase."""
    if not text:
        return set()
    s = str(text)
    # Split camelCase e.g. TaskExecutionLoop -> Task Execution Loop
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    # Replace symbols / \ . _ - : with spaces
    cleaned = re.sub(r"[/\\._\-:]+", " ", s.lower())
    words = set(re.findall(r"[a-z0-9]+", cleaned))
    # Also extract full alphanumeric tokens
    direct = set(re.findall(r"\w+", s.lower()))
    return words | direct


SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL CHECK(category IN ('component', 'decision', 'constraint', 'state', 'other')),
    content TEXT NOT NULL,
    rationale TEXT,
    confidence TEXT NOT NULL DEFAULT 'assumed' CHECK(confidence IN ('confirmed', 'assumed', 'uncertain')),
    created_at TEXT NOT NULL,
    session_id TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    importance INTEGER NOT NULL DEFAULT 3 CHECK(importance BETWEEN 1 AND 5),
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TEXT,
    superseded_by INTEGER REFERENCES memory(id),
    deprecated_at TEXT,
    deprecated_reason TEXT,
    file_path TEXT,
    line_start INTEGER,
    line_end INTEGER,
    code_hash TEXT,
    git_commit_sha TEXT,
    tags TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_category ON memory(category);
CREATE INDEX IF NOT EXISTS idx_memory_active ON memory(deprecated_at);
CREATE INDEX IF NOT EXISTS idx_memory_file ON memory(file_path);

CREATE TABLE IF NOT EXISTS memory_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    relation TEXT NOT NULL CHECK(relation IN ('depends_on', 'implements', 'conflicts_with', 'relates_to')),
    created_at TEXT NOT NULL,
    UNIQUE(source_id, target_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON memory_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON memory_edges(target_id);

CREATE TABLE IF NOT EXISTS memory_locks (
    resource TEXT PRIMARY KEY,
    locked_by TEXT NOT NULL,
    locked_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content, rationale, tags, file_path, content='memory', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, content, rationale, tags, file_path) VALUES (new.id, new.content, new.rationale, new.tags, new.file_path);
END;

CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, rationale, tags, file_path) VALUES('delete', old.id, old.content, old.rationale, old.tags, old.file_path);
END;

CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, rationale, tags, file_path) VALUES('delete', old.id, old.content, old.rationale, old.tags, old.file_path);
    INSERT INTO memory_fts(rowid, content, rationale, tags, file_path) VALUES (new.id, new.content, new.rationale, new.tags, new.file_path);
END;

CREATE TABLE IF NOT EXISTS memory_embeddings (
    fact_id  INTEGER PRIMARY KEY REFERENCES memory(id) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    embedding BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS structural_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_type TEXT NOT NULL CHECK(node_type IN ('function','method','class','import','module_ref')),
    name TEXT NOT NULL,
    class_name TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    language TEXT,
    code_hash TEXT,
    base_classes TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(file_path, node_type, name, class_name)
);

CREATE INDEX IF NOT EXISTS idx_structural_nodes_file ON structural_nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_structural_nodes_name ON structural_nodes(name);

CREATE TABLE IF NOT EXISTS fact_node_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    node_id INTEGER NOT NULL REFERENCES structural_nodes(id) ON DELETE CASCADE,
    link_reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(fact_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_fact_node_links_fact ON fact_node_links(fact_id);
CREATE INDEX IF NOT EXISTS idx_fact_node_links_node ON fact_node_links(node_id);

CREATE TABLE IF NOT EXISTS structural_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id INTEGER NOT NULL REFERENCES structural_nodes(id) ON DELETE CASCADE,
    callee_id INTEGER NOT NULL REFERENCES structural_nodes(id) ON DELETE CASCADE,
    edge_type TEXT NOT NULL CHECK(edge_type IN ('calls', 'imports', 'contains')),
    created_at TEXT NOT NULL,
    UNIQUE(caller_id, callee_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_structural_edges_caller ON structural_edges(caller_id);
CREATE INDEX IF NOT EXISTS idx_structural_edges_callee ON structural_edges(callee_id);

CREATE TABLE IF NOT EXISTS call_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id INTEGER NOT NULL REFERENCES structural_nodes(id) ON DELETE CASCADE,
    callee_id INTEGER NOT NULL REFERENCES structural_nodes(id) ON DELETE CASCADE,
    line INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(caller_id, callee_id, line)
);

CREATE INDEX IF NOT EXISTS idx_call_sites_caller ON call_sites(caller_id);
CREATE INDEX IF NOT EXISTS idx_call_sites_callee ON call_sites(callee_id);

CREATE TABLE IF NOT EXISTS fact_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    flag_type TEXT NOT NULL DEFAULT 'misleading',
    note TEXT,
    flagged_by TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fact_flags_fact ON fact_flags(fact_id);
"""


# ---------------------------------------------------------------------------
# Data-directory identity (CogniGraph rebrand, Phase 1.9).
# The live DB lives at <project-root>/.cognigraph/memory.db; legacy checkouts
# still carry <project-root>/.project-memory/memory.db until first-use
# migration renames it. Module filenames, MCP tool names, DB tables, and
# stored row content are all UNCHANGED — only the directory name migrates.
# ---------------------------------------------------------------------------
DATA_DIR_NAME = ".cognigraph"
LEGACY_DATA_DIR_NAME = ".project-memory"

_migration_done = False


def maybe_migrate_legacy_data_dir(project_root) -> Path:
    """Return the active data dir for a project root, migrating legacy -> current.

    - `.cognigraph/` absent + `.project-memory/` present: atomically rename
      (os.replace, same volume), then integrity-check the moved DB. On ANY
      failure move it back, keep using the old path, log to stderr. NEVER
      deletes or overwrites data.
    - Both exist: use `.cognigraph/`, warn about the orphan on stderr.
    - Idempotent: a second run is a no-op.
    - Read-only fast path: no migration needed, no logging, no FS writes.
    """
    root = Path(project_root)
    new_dir = root / DATA_DIR_NAME
    old_dir = root / LEGACY_DATA_DIR_NAME
    if new_dir.is_dir():
        if old_dir.is_dir():
            print(f"[cognigraph] WARNING: both {new_dir} and legacy {old_dir} exist; "
                  f"using {new_dir}, legacy orphan left untouched",
                  file=sys.stderr)
        return new_dir
    if not old_dir.is_dir():
        return new_dir
    try:
        os.replace(str(old_dir), str(new_dir))
    except Exception as e:
        print(f"[cognigraph] migration skipped ({old_dir} -> {new_dir}: {e}); "
              f"continuing with legacy path", file=sys.stderr)
        return old_dir
    db_file = new_dir / "memory.db"
    if not db_file.is_file():
        return new_dir  # nothing to verify (legacy dir held no DB)
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_file), timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if not row or str(row[0]).lower() != "ok":
                raise ValueError(f"integrity_check returned {row!r}")
            conn.execute("SELECT COUNT(*) FROM memory").fetchone()
        finally:
            conn.close()
    except Exception as e:
        try:
            os.replace(str(new_dir), str(old_dir))
        except Exception as e2:
            print(f"[cognigraph] migration verify failed ({e}) AND rollback failed ({e2}); "
                  f"data remains at {new_dir}", file=sys.stderr)
            return new_dir
        print(f"[cognigraph] migration verify failed ({e}); rolled back to {old_dir}",
              file=sys.stderr)
        return old_dir
    return new_dir


def ensure_startup_migration(hints: Optional[List] = None,
                             include_cwd: bool = True,
                             include_env: bool = True) -> None:
    """One-shot legacy data-dir migration for server startup / CLI entry.

    Migrates each candidate project root once per process (NOT per tool call —
    os.replace on an open SQLite file fails on Windows, so this must run
    before any store is opened). When MEMORY_DB_PATH pointed inside a legacy
    dir that just migrated, the env var is rewritten to the new path so later
    store opens land on the migrated DB.
    """
    global _migration_done
    if _migration_done:
        return
    _migration_done = True
    roots: List[Path] = []
    if hints:
        roots.extend(Path(h) for h in hints)
    if include_env:
        override = os.environ.get("MEMORY_DB_PATH")
        if override:
            try:
                p = Path(override)
                if p.parent.name in (DATA_DIR_NAME, LEGACY_DATA_DIR_NAME):
                    roots.append(p.parent.parent)
            except Exception:
                pass
    if include_cwd and not hints:
        roots.append(Path.cwd())
        try:
            roots.append(Path(__file__).resolve().parent)
        except Exception:
            pass
    seen = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except Exception:
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        try:
            active = maybe_migrate_legacy_data_dir(root)
        except Exception:
            continue
        if include_env:
            override = os.environ.get("MEMORY_DB_PATH")
            if override:
                try:
                    op = Path(override)
                    if (op.parent.name == LEGACY_DATA_DIR_NAME
                            and str(op.parent.parent.resolve()) == key
                            and active.name == DATA_DIR_NAME):
                        os.environ["MEMORY_DB_PATH"] = str(active / op.name)
                except Exception:
                    pass


def find_project_root(start_path: Optional[Path] = None) -> Optional[Path]:
    """Finds the root directory containing CogniGraph data (memory.db) by traversing upwards.

    Prefers the current `.cognigraph/` dir, falls back to legacy `.project-memory/`.
    """
    current = Path(start_path or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        if (parent / DATA_DIR_NAME / "memory.db").is_file():
            return parent
    for parent in [current, *current.parents]:
        if (parent / LEGACY_DATA_DIR_NAME / "memory.db").is_file():
            return parent
    return None


def default_db_path() -> str:
    """
    Intelligently finds the canonical memory database path:
    1. MEMORY_DB_PATH environment variable override.
    2. Searches upwards from CWD for an existing .cognigraph/memory.db
       (legacy .project-memory/memory.db as fallback).
    3. Searches upwards from this script's directory likewise.
    4. Fallback: Creates .cognigraph/memory.db in CWD.

    Read-only resolution: never migrates (migration runs once at server
    startup / CLI entry via ensure_startup_migration, never per call).
    """
    override = os.environ.get("MEMORY_DB_PATH")
    if override:
        return override

    # 1. Search upwards from cwd
    root = find_project_root(Path.cwd())
    if root:
        if (root / DATA_DIR_NAME / "memory.db").is_file():
            return str(root / DATA_DIR_NAME / "memory.db")
        return str(root / LEGACY_DATA_DIR_NAME / "memory.db")

    # 2. Search upwards from this script's directory
    try:
        script_root = find_project_root(Path(__file__).resolve().parent)
        if script_root:
            if (script_root / DATA_DIR_NAME / "memory.db").is_file():
                return str(script_root / DATA_DIR_NAME / "memory.db")
            return str(script_root / LEGACY_DATA_DIR_NAME / "memory.db")
    except Exception:
        pass

    # 3. Fallback
    base = Path.cwd() / DATA_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    return str(base / "memory.db")




