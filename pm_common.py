"""
Shared constants, embedding helpers, and schema for the project memory system.

SQLite-backed, with FTS5 full-text search, graph edge relationships, code provenance,
hybrid RRF query ranking (BM25 + tokenized + semantic embeddings), recency decay,
auto-dedup guard, optimistic concurrency control, and automated consolidation.
"""
import sqlite3
import os
import re
import datetime
import hashlib
import json
import struct
import logging
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

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
    return _EMBED_MODEL


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

_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                              # AWS access key id
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+]{12,}"),
    re.compile(r"(?i)://[^/\s:]+:[^/\s@]+@"),                     # user:pass@ embedded in a URL
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                           # GitHub personal access token
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                           # OpenAI/Anthropic-style secret key
]


def looks_like_secret(text: Optional[str]) -> bool:
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


def find_project_root(start_path: Optional[Path] = None) -> Optional[Path]:
    """Finds the root directory containing .project-memory/memory.db by traversing upwards."""
    current = Path(start_path or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        db_candidate = parent / ".project-memory" / "memory.db"
        if db_candidate.is_file():
            return parent
    return None


def default_db_path() -> str:
    """
    Intelligently finds the canonical memory database path:
    1. MEMORY_DB_PATH environment variable override.
    2. Searches upwards from CWD for an existing .project-memory/memory.db.
    3. Searches upwards from this script's directory for .project-memory/memory.db.
    4. Fallback: Creates .project-memory/memory.db in CWD.
    """
    override = os.environ.get("MEMORY_DB_PATH")
    if override:
        return override
    
    # 1. Search upwards from cwd
    root = find_project_root(Path.cwd())
    if root:
        return str(root / ".project-memory" / "memory.db")
    
    # 2. Search upwards from this script's directory
    try:
        script_root = find_project_root(Path(__file__).resolve().parent)
        if script_root:
            return str(script_root / ".project-memory" / "memory.db")
    except Exception:
        pass

    # 3. Fallback
    base = Path.cwd() / ".project-memory"
    base.mkdir(parents=True, exist_ok=True)
    return str(base / "memory.db")




