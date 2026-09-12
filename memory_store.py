"""
Core storage layer for the project memory system.

SQLite-backed fact store: schema + migrations, fact CRUD with auto-dedup,
graph edges, advisory locks, and embeddings persistence. Retrieval, context
generation, lifecycle, and structural sync live in the pm_search / pm_context /
pm_lifecycle / pm_structural mixins composed into MemoryStore below.

Every public name historically exported by this module is re-exported, so
existing imports (server.py, bootstrap.py, memory_cli.py, tests, hooks)
keep working unchanged.
"""
import sqlite3
import re
import datetime
import subprocess
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from pm_common import (
    # backward-compatible re-exports
    SCHEMA, MAX_LIMIT, VALID_CATEGORIES, VALID_CONFIDENCE, VALID_RELATIONS,
    looks_like_secret, tokenize_text, find_project_root, default_db_path,
    DATA_DIR_NAME, LEGACY_DATA_DIR_NAME,
    maybe_migrate_legacy_data_dir, ensure_startup_migration,
    _SECRET_PATTERNS, _SEMANTIC_AVAILABLE, _SQLITE_VEC_PKG_AVAILABLE,
    _VEC_DIM, _EMBED_MODEL_NAME, _get_embed_model, _encode, _vec_to_blob,
    _blob_to_vec, _cosine_sim, _sqlite_vec_pkg, np,
    # used directly by the core methods below
    logger,
)
from pm_search import SearchMixin
from pm_context import ContextMixin
from pm_lifecycle import LifecycleMixin
from pm_structural import StructuralMixin

__all__ = [
    "MemoryStore",
    "RevisionTransaction",
    "get_store",
    "remember",
    "recall",
    "file_briefing",
    "prepare_task",
    "checkpoint_task",
    "default_db_path",
    "find_project_root",
    "DATA_DIR_NAME",
    "LEGACY_DATA_DIR_NAME",
    "maybe_migrate_legacy_data_dir",
    "ensure_startup_migration",
    "looks_like_secret",
    "tokenize_text",
    "SCHEMA",
    "MAX_LIMIT",
    "VALID_CATEGORIES",
    "VALID_CONFIDENCE",
    "VALID_RELATIONS",
    "_SECRET_PATTERNS",
    "_SEMANTIC_AVAILABLE",
    "_SQLITE_VEC_PKG_AVAILABLE",
    "_VEC_DIM",
    "_EMBED_MODEL_NAME",
    "_get_embed_model",
    "_encode",
    "_vec_to_blob",
    "_blob_to_vec",
    "_cosine_sim",
    "_sqlite_vec_pkg",
    "np",
    "logger",
]


# Process-wide counters for best-effort background work that must not crash its
# caller (vec sync, structural sync, re-embedding, fact linking). Surfaced via
# stats() so sync drift is diagnosable without reading logs.
_BACKGROUND_FAILURES = {
    "vec_sync": 0,
    "structural_sync": 0,
    "re_embed": 0,
    "fact_linking": 0,
}


def bump_background_failure(kind: str) -> None:
    """Increment a named background-failure counter (never raises)."""
    if kind in _BACKGROUND_FAILURES:
        _BACKGROUND_FAILURES[kind] += 1


class MemoryStore(SearchMixin, ContextMixin, LifecycleMixin, StructuralMixin):
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or default_db_path()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._ensure_schema_migrations()
        self._ensure_embedding_schema()

        # Attempt to load sqlite-vec extension and create vec0-backed table for fast KNN
        # Graceful degradation: if the package or extension fails to load, keep working as before.
        self._vec_available = False
        self._vec_triggers = False
        if _SQLITE_VEC_PKG_AVAILABLE:
            try:
                # sqlite_vec provides a Python loader that wires the extension to this connection
                self.conn.enable_load_extension(True)
                _sqlite_vec_pkg.load(self.conn)
                self.conn.enable_load_extension(False)
                self._vec_available = True

                # Create virtual table for vector search (rowid == fact_id)
                try:
                    self.conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_memory USING vec0(embedding float[{_VEC_DIM}])")
                    # Try to mirror memory_embeddings -> vec_memory via triggers
                    try:
                        self.conn.execute("CREATE TRIGGER IF NOT EXISTS vec_ei AFTER INSERT ON memory_embeddings BEGIN INSERT OR REPLACE INTO vec_memory(rowid, embedding) VALUES (new.fact_id, new.embedding); END;")
                        self.conn.execute("CREATE TRIGGER IF NOT EXISTS vec_ed AFTER DELETE ON memory_embeddings BEGIN DELETE FROM vec_memory WHERE rowid = old.fact_id; END;")
                        self.conn.execute("CREATE TRIGGER IF NOT EXISTS vec_eu AFTER UPDATE ON memory_embeddings BEGIN INSERT OR REPLACE INTO vec_memory(rowid, embedding) VALUES (new.fact_id, new.embedding); END;")
                        self._vec_triggers = True
                    except Exception:
                        # Triggers may not be supported for this virtual table implementation; fall back to explicit sync
                        self._vec_triggers = False

                    # Backfill: idempotently populate vec_memory from pre-existing memory_embeddings rows
                    try:
                        self.conn.execute("INSERT OR REPLACE INTO vec_memory(rowid, embedding) SELECT fact_id, embedding FROM memory_embeddings WHERE fact_id NOT IN (SELECT rowid FROM vec_memory)")
                        # Clean up any stale rows that don't correspond to memory_embeddings
                        self.conn.execute("DELETE FROM vec_memory WHERE rowid NOT IN (SELECT fact_id FROM memory_embeddings)")
                    except Exception:
                        # be conservative: ignore backfill failures and continue
                        pass
                except Exception:
                    # Could not create vec virtual table despite package being present
                    self._vec_available = False
            except Exception:
                # Extension load failed; leave vec unavailable
                self._vec_available = False

        # The canonical schema includes a vec0 virtual table
        # (phase1_episodic_embeddings.sql), so it must run after the
        # sqlite-vec extension load above.
        self._ensure_canonical_schema()

    def _ensure_canonical_schema(self):
        """Ensure Phase 0/1/2 canonical tables + triggers exist for fresh installs.

        Live DB is built incrementally from phase0_schema.sql etc, but a brand-new
        temp DB via MemoryStore(temp_path) would otherwise lack repositories,
        memory_branches, entities, anchors, observations, episodic_memories etc.
        This method makes fresh installs self-contained while remaining idempotent
        on the live DB (IF NOT EXISTS / try/except already exists).
        """
        try:
            exists = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='repositories'").fetchone()
            if exists is None:
                base = Path(__file__).resolve().parent
                for fname in ["phase0_schema.sql", "phase1_episodic.sql", "phase1_episodic_embeddings.sql", "phase2_branch_admin.sql", "episodic_fts.sql", "legacy_provenance.sql"]:
                    p = base / fname
                    if p.exists():
                        try:
                            self.conn.executescript(p.read_text(encoding="utf-8"))
                        except sqlite3.OperationalError as e:
                            if "already exists" not in str(e):
                                raise
# Ensure episodic_fts exists even if episodic_fts.sql missing triggers
                try:
                    self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS episodic_fts USING fts5(content, tags, content='episodic_memories', content_rowid='memory_id')")
                except Exception as e:
                    logger.warning("episodic_fts ensure failed: %r", e)
                # Ensure index on episodic_memories.created_revision for promotion queries
                try:
                    self.conn.execute("CREATE INDEX IF NOT EXISTS idx_episodic_created_revision ON episodic_memories(created_revision)")
                except Exception as e:
                    logger.warning("idx_episodic_created_revision ensure failed: %r", e)
        except Exception as e:
            logger.error("canonical schema init failed: %r", e)

    def _ensure_embedding_schema(self):
        """Ensure memory_embeddings table exists (safe to call multiple times)."""
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            fact_id  INTEGER PRIMARY KEY REFERENCES memory(id) ON DELETE CASCADE,
            model    TEXT NOT NULL,
            embedding BLOB NOT NULL
        );
        """)

    def _store_embedding(self, fact_id: int, text: str) -> bool:
        """Encode text and persist embedding blob for this fact. Returns True if stored.

        Additionally keep the vec0-backed vec_memory in sync. If triggers were
        successfully created on connect, the triggers will mirror changes
        automatically. If triggers are unavailable, perform an explicit
        INSERT OR REPLACE here as a fallback to ensure vec_memory stays current.
        """
        vec = _encode(text)
        if vec is None:
            return False
        blob = _vec_to_blob(vec)
        self.conn.execute(
            "INSERT OR REPLACE INTO memory_embeddings (fact_id, model, embedding) VALUES (?, ?, ?)",
            (fact_id, _EMBED_MODEL_NAME, blob),
        )
        # If sqlite-vec is available but triggers failed to be created, explicitly sync
        if getattr(self, '_vec_available', False) and not getattr(self, '_vec_triggers', False):
            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO vec_memory(rowid, embedding) VALUES (?, ?)",
                    (fact_id, blob),
                )
            except Exception as e:
                # Best-effort: don't fail the store operation if vec sync fails
                logger.warning("vec_memory sync failed for fact %s: %r", fact_id, e)
                bump_background_failure("vec_sync")
        return True

    def _store_episodic_embedding(self, memory_id: int, text: str) -> bool:
        """Encode text and persist embedding blob for this episodic fact. Returns True if stored.

        Mirrors _store_embedding exactly but targets episodic_embeddings /
        vec_episodic_memory. Additionally keep the vec0-backed vec_episodic_memory
        in sync. If triggers were successfully created on connect, the triggers
        will mirror changes automatically. If triggers are unavailable, perform an
        explicit INSERT OR REPLACE here as a fallback to ensure vec_episodic_memory
        stays current.
        """
        vec = _encode(text)
        if vec is None:
            return False
        blob = _vec_to_blob(vec)
        self.conn.execute(
            "INSERT OR REPLACE INTO episodic_embeddings (memory_id, model, embedding) VALUES (?, ?, ?)",
            (memory_id, _EMBED_MODEL_NAME, blob),
        )
        if getattr(self, '_vec_available', False) and not getattr(self, '_vec_triggers', False):
            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO vec_episodic_memory(rowid, embedding) VALUES (?, ?)",
                    (memory_id, blob),
                )
            except Exception as e:
                logger.warning("vec_episodic_memory sync failed for memory %s: %r", memory_id, e)
                bump_background_failure("vec_sync")
        return True

    def _ensure_schema_migrations(self):
        """Auto-migrate older sqlite databases to include all new columns, tables, and updated FTS index."""
        cols = [c["name"] for c in self.conn.execute("PRAGMA table_info(memory)").fetchall()]
        additions = {
            "version": "INTEGER NOT NULL DEFAULT 1",
            "importance": "INTEGER NOT NULL DEFAULT 3",
            "access_count": "INTEGER NOT NULL DEFAULT 0",
            "last_accessed_at": "TEXT",
            "file_path": "TEXT",
            "line_start": "INTEGER",
            "line_end": "INTEGER",
            "code_hash": "TEXT",
            "git_commit_sha": "TEXT",
            "tags": "TEXT",
        }
        for col_name, col_type in additions.items():
            if col_name not in cols:
                self.conn.execute(f"ALTER TABLE memory ADD COLUMN {col_name} {col_type}")

        # Ensure memory_edges and memory_locks exist
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
            target_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
            relation TEXT NOT NULL CHECK(relation IN ('depends_on', 'implements', 'conflicts_with', 'relates_to')),
            created_at TEXT NOT NULL,
            UNIQUE(source_id, target_id, relation)
        );
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_locks (
            resource TEXT PRIMARY KEY,
            locked_by TEXT NOT NULL,
            locked_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        """)

        # Phase 3: retrieval usage log for usage-pattern learning and proactive suggestions
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_text TEXT NOT NULL,
            intent TEXT,
            agent_type TEXT,
            retrieved_fact_ids TEXT,
            result_count INTEGER NOT NULL DEFAULT 0,
            session_id TEXT,
            logged_at TEXT NOT NULL
        );
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_log_at ON memory_usage_log(logged_at)")

        # Ensure memory_fts contains tags and file_path
        try:
            fts_cols = [c["name"] for c in self.conn.execute("PRAGMA table_info(memory_fts)").fetchall()]
            if "tags" not in fts_cols or "file_path" not in fts_cols:
                self.conn.execute("DROP TABLE IF EXISTS memory_fts")
                self.conn.execute("""
                CREATE VIRTUAL TABLE memory_fts USING fts5(
                    content, rationale, tags, file_path, content='memory', content_rowid='id'
                )
                """)
                self.conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
                # Re-create triggers
                self.conn.execute("DROP TRIGGER IF EXISTS memory_ai")
                self.conn.execute("DROP TRIGGER IF EXISTS memory_ad")
                self.conn.execute("DROP TRIGGER IF EXISTS memory_au")
                self.conn.execute("""
                CREATE TRIGGER memory_ai AFTER INSERT ON memory BEGIN
                    INSERT INTO memory_fts(rowid, content, rationale, tags, file_path) VALUES (new.id, new.content, new.rationale, new.tags, new.file_path);
                END;
                """)
                self.conn.execute("""
                CREATE TRIGGER memory_ad AFTER DELETE ON memory BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, content, rationale, tags, file_path) VALUES('delete', old.id, old.content, old.rationale, old.tags, old.file_path);
                END;
                """)
                self.conn.execute("""
                CREATE TRIGGER memory_au AFTER UPDATE ON memory BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, content, rationale, tags, file_path) VALUES('delete', old.id, old.content, old.rationale, old.tags, old.file_path);
                    INSERT INTO memory_fts(rowid, content, rationale, tags, file_path) VALUES (new.id, new.content, new.rationale, new.tags, new.file_path);
                END;
                """)
        except Exception as e:
            logger.warning("memory_fts rebuild failed: %r", e)

        # Widen structural_nodes.node_type CHECK to include module_ref
        # (rows created by other files' import resolution). Old databases
        # carry the pre-graph CHECK, so rebuild the table preserving rowids.
        # Two independently gated repairs:
        # 1) structural_nodes itself still lacks the widened CHECK.
        # 2) child tables reference 'structural_nodes_old': SQLite >= 3.25
        #    renames rewrite REFERENCES clauses in other tables, so a rebuild
        #    leaves structural_edges/fact_node_links pointing at the dropped
        #    old table; they must be recreated (data copied, rowids kept).
        try:
            def _table_sql(name):
                r = self.conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
                ).fetchone()
                return r["sql"] if r else None

            sn_sql = _table_sql("structural_nodes") or ""
            se_sql = _table_sql("structural_edges") or ""
            fnl_sql = _table_sql("fact_node_links") or ""
            needs_node_rebuild = bool(sn_sql) and "module_ref" not in sn_sql
            needs_child_repair = ("structural_nodes_old" in se_sql) or ("structural_nodes_old" in fnl_sql)
            if needs_node_rebuild or needs_child_repair:
                # The rebuild (rename -> recreate -> copy -> drop) needs FKs
                # suspended; child tables hold FKs into structural_nodes.
                self.conn.execute("PRAGMA foreign_keys=OFF")
                # The rename re-validates every trigger; the vec mirror
                # triggers reference the vec0 virtual table, which cannot be
                # resolved before the extension loads in __init__, so drop
                # them here — the vec block below recreates them with
                # CREATE TRIGGER IF NOT EXISTS once the extension is live.
                self.conn.execute("DROP TRIGGER IF EXISTS vec_ei")
                self.conn.execute("DROP TRIGGER IF EXISTS vec_ed")
                self.conn.execute("DROP TRIGGER IF EXISTS vec_eu")
                if needs_node_rebuild:
                    self.conn.executescript("""
                    ALTER TABLE structural_nodes RENAME TO structural_nodes_old;
                    CREATE TABLE structural_nodes (
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
                    INSERT INTO structural_nodes
                        (id, node_type, name, class_name, file_path, line_start, line_end,
                         language, code_hash, created_at, last_seen_at)
                        SELECT id, node_type, name, class_name, file_path, line_start, line_end,
                         language, code_hash, created_at, last_seen_at FROM structural_nodes_old;
                    DROP TABLE structural_nodes_old;
                    CREATE INDEX IF NOT EXISTS idx_structural_nodes_file ON structural_nodes(file_path);
                    CREATE INDEX IF NOT EXISTS idx_structural_nodes_name ON structural_nodes(name);
                    """)
                if "structural_nodes_old" in se_sql:
                    self.conn.executescript("""
                    DROP TABLE IF EXISTS structural_edges;
                    CREATE TABLE structural_edges (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        caller_id INTEGER NOT NULL REFERENCES structural_nodes(id) ON DELETE CASCADE,
                        callee_id INTEGER NOT NULL REFERENCES structural_nodes(id) ON DELETE CASCADE,
                        edge_type TEXT NOT NULL CHECK(edge_type IN ('calls', 'imports', 'contains')),
                        created_at TEXT NOT NULL,
                        UNIQUE(caller_id, callee_id, edge_type)
                    );
                    CREATE INDEX IF NOT EXISTS idx_structural_edges_caller ON structural_edges(caller_id);
                    CREATE INDEX IF NOT EXISTS idx_structural_edges_callee ON structural_edges(callee_id);
                    """)
                if "structural_nodes_old" in fnl_sql:
                    self.conn.executescript("""
                    ALTER TABLE fact_node_links RENAME TO fact_node_links_old;
                    CREATE TABLE fact_node_links (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        fact_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
                        node_id INTEGER NOT NULL REFERENCES structural_nodes(id) ON DELETE CASCADE,
                        link_reason TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(fact_id, node_id)
                    );
                    INSERT INTO fact_node_links SELECT * FROM fact_node_links_old;
                    DROP TABLE fact_node_links_old;
                    CREATE INDEX IF NOT EXISTS idx_fact_node_links_fact ON fact_node_links(fact_id);
                    CREATE INDEX IF NOT EXISTS idx_fact_node_links_node ON fact_node_links(node_id);
                    """)
                self.conn.execute("PRAGMA foreign_keys=ON")
        except Exception as e:
            logger.warning("structural_nodes rebuild/repair failed: %r", e)
            try:
                self.conn.execute("PRAGMA foreign_keys=ON")
            except Exception as e2:
                logger.warning("foreign_keys re-enable after failed rebuild failed: %r", e2)

        # Composed-class resolution: class rows carry their ordered base-class
        # tails as JSON (structural_extract 'bases'). Simple nullable ADD COLUMN
        # following the memory-table convention above; existing rows stay NULL
        # (treated as "no known bases") until their file is re-synced.
        try:
            sn_cols = [c["name"] for c in self.conn.execute("PRAGMA table_info(structural_nodes)").fetchall()]
            if sn_cols and "base_classes" not in sn_cols:
                self.conn.execute("ALTER TABLE structural_nodes ADD COLUMN base_classes TEXT")
        except Exception as e:
            logger.warning("structural_nodes base_classes migration failed: %r", e)

        # Call-site line numbers: one row per resolved (caller, callee, line)
        # triple (structural_extract 'call_sites'). CREATE TABLE IF NOT EXISTS
        # following the same convention as above; existing databases simply
        # gain an empty table that fills in as files are re-synced, and rows
        # with no stored sites read back as "no known lines".
        try:
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS call_sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caller_id INTEGER NOT NULL REFERENCES structural_nodes(id) ON DELETE CASCADE,
                callee_id INTEGER NOT NULL REFERENCES structural_nodes(id) ON DELETE CASCADE,
                line INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(caller_id, callee_id, line)
            );
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_call_sites_caller ON call_sites(caller_id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_call_sites_callee ON call_sites(callee_id)")
        except Exception as e:
            logger.warning("call_sites migration failed: %r", e)

    def close(self):
        self.conn.close()

    def _derive_default_repo_id(self) -> Optional[int]:
        """Derive the default single-repo ID (first repo in DB). Returns None if no repos."""
        try:
            row = self.conn.execute("SELECT repo_id FROM repositories ORDER BY repo_id LIMIT 1").fetchone()
            return row[0] if row else None
        except sqlite3.OperationalError:
            return None

    # Words too generic to carry retrieval value when auto-derived as tags.
    _TAG_STOPWORDS = {
        "memory", "content", "should", "would", "could", "about", "after",
        "before", "first", "other", "which", "their", "there", "where",
        "implement", "implemented", "change", "changes", "using", "value",
        "verified", "baseline",
    }

    def _auto_tags(self, category: str, content: str, file_path: Optional[str]) -> Optional[str]:
        """Derive tags for a fact written without explicit tags: the category,
        the anchored file's stem and parent directory, and the longest content
        tokens. Keeps tag-based filtering useful even when callers omit tags."""
        tokens = {category}
        if file_path:
            parts = [p for p in file_path.replace("\\", "/").split("/") if p]
            if parts:
                stem = parts[-1].rsplit(".", 1)[0].lower()
                if len(stem) >= 3:
                    tokens.add(stem)
                if len(parts) >= 2 and len(parts[-2]) >= 3:
                    tokens.add(parts[-2].lower())
        words = tokenize_text(content)
        descriptive = sorted(
            (w for w in words if len(w) >= 6 and w not in self._TAG_STOPWORDS),
            key=len, reverse=True,
        )[:3]
        tokens.update(descriptive)
        cleaned = sorted(t for t in tokens if t and re.fullmatch(r"[a-z0-9_\-]+", t))
        return ",".join(cleaned) if cleaned else None

    def _current_git_commit(self) -> Optional[str]:
        """Best-effort HEAD sha of the repository containing the memory DB.

        Gives anchored facts a time axis: the commit the code was at when the
        fact was recorded. Returns None outside a git repository (temp test
        DBs, projects not yet under git) or when git is unavailable.

        stdin is detached (DEVNULL): when this runs inside the MCP server,
        inheriting the held-open MCP stdio pipe makes Git-for-Windows
        children stall, defeating the timeout below.
        """
        try:
            root = Path(self.db_path).resolve().parent.parent
            out = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                return out.stdout.strip() or None
        except Exception:
            pass
        return None

    def write(self, category: str, content: str, rationale: Optional[str] = None,
              confidence: str = "assumed", session_id: Optional[str] = None,
              file_path: Optional[str] = None, tags: Optional[str] = None,
              importance: int = 3, line_start: Optional[int] = None,
              line_end: Optional[int] = None, code_hash: Optional[str] = None,
              git_commit_sha: Optional[str] = None,
              force: bool = False, embed: bool = True) -> int:
        """Write a new fact into the live `memory` table. Returns positive ID,
        or negative ID sentinel if a near-duplicate exists.

        Intake contract (2026-08-31): `memory` is the live intake table by
        contract. episodic_memories holds the genesis-migrated historical
        archive plus branch/revision bookkeeping and is not an intake target.
        write / write_batch / checkpoint_task and every lifecycle path
        (supersede, deprecate, consolidate, prune, provenance, edges) are
        memory-bound by design, not by accident.

        Args:
            force: If True, bypass the auto-dedup guard and always write (for batch/internal ops).
            git_commit_sha: Auto-stamped from the repo HEAD for anchored facts
                (file_path + line_start) when omitted and the DB lives in a
                git repository — the provenance time axis.
        """
        category = category.lower().strip()
        confidence = confidence.lower().strip()
        if category not in VALID_CATEGORIES:
            raise ValueError(f"category must be one of {sorted(VALID_CATEGORIES)}, got {category!r}")
        if confidence not in VALID_CONFIDENCE:
            raise ValueError(f"confidence must be one of {sorted(VALID_CONFIDENCE)}, got {confidence!r}")
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        if looks_like_secret(content) or looks_like_secret(rationale):
            raise ValueError(
                "refused: this looks like it contains a credential or secret. "
                "Store where the secret is configured instead of its value."
            )
        # Normalize tags: accept list[str] or a comma-joined string
        if isinstance(tags, (list, tuple)):
            tags = ",".join(str(t).strip() for t in tags) or None
        elif tags:
            tags = tags.strip() or None
        else:
            tags = None
        if tags is None:
            tags = self._auto_tags(category, content, file_path)

        # --- Auto-dedup guard ---
        # Before writing, check semantic + tokenized similarity against existing facts.
        # If a near-duplicate exists (similarity > 0.88), return its ID with a warning
        # embedded in a special marker so callers can detect and supersede instead.
        # Bypass with force=True for batch/internal operations (consolidation, backfill, etc.)
        combined_text = f"{content} {rationale or ''}"
        if not force:
            dup_id = self._find_near_duplicate(combined_text, file_path=file_path, threshold=0.88)
            if dup_id is not None:
                # Return a negative sentinel: caller receives -(existing_id) to signal
                # "use supersede instead of writing a new fact". Positive ID = new fact.
                return -(dup_id)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # Normalize file_path to use forward slashes for consistent matching
        norm_file_path = file_path.strip().replace("\\", "/") if file_path else None
        # Anchor facts tied to code carry the commit the code had at write
        # time, so future agents can ask "what changed since this fact".
        if git_commit_sha is None and norm_file_path and line_start is not None:
            git_commit_sha = self._current_git_commit()
        cur = self.conn.execute(
            "INSERT INTO memory (category, content, rationale, confidence, created_at, session_id, "
            "file_path, tags, importance, line_start, line_end, code_hash, git_commit_sha) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (category, content.strip() if content else "", rationale, confidence, now, session_id,
             norm_file_path, tags,
             max(1, min(5, int(importance))), line_start, line_end, code_hash, git_commit_sha),
        )
        new_id = cur.lastrowid
        # Persist semantic embedding asynchronously (best-effort) — never silently swallow
        if embed:
            try:
                self._store_embedding(new_id, combined_text)
            except Exception as e:
                logger.warning("memory embedding failed for fact_id %s: %r", new_id, e)
        return new_id

    def _find_near_duplicate(self, text: str, file_path: Optional[str] = None, threshold: float = 0.88) -> Optional[int]:
        """
        Returns the ID of an existing active fact that is semantically near-duplicate
        to *text*, or None if no duplicate exists.
        Uses semantic cosine similarity when available, falls back to tokenized Jaccard.
        Scoped by file_path when provided to avoid cross-file collisions.
        """
        # Backward-compat: caller may pass threshold as second positional arg
        if isinstance(file_path, (int, float)) and not isinstance(file_path, bool):
            threshold = file_path
            file_path = None
        if not text or not text.strip():
            return None
        # Normalize file_path for scoping
        norm_fp = file_path.strip().replace("\\", "/").lower() if file_path else None
        query_vec = _encode(text)
        if query_vec is not None:
            # Prefer vec KNN (K=64) when sqlite-vec is available — 10x faster than brute scan
            if getattr(self, '_vec_available', False):
                try:
                    scores = self._semantic_scores_vec(query_vec)
                    # vec returns top-K only; if empty due to no embeddings, fallback to brute
                    if not scores:
                        scores = self._semantic_scores(query_vec)
                except Exception:
                    scores = self._semantic_scores(query_vec)
            else:
                scores = self._semantic_scores(query_vec)
            # Filter to only active facts, scoped by file_path if provided
            if norm_fp:
                rows = self.conn.execute(
                    "SELECT id, file_path FROM memory WHERE deprecated_at IS NULL"
                ).fetchall()
                active_ids = set()
                for rr in rows:
                    fp = rr["file_path"]
                    fp_norm = fp.strip().replace("\\", "/").lower() if fp else None
                    if fp_norm == norm_fp and rr["id"] in scores:
                        active_ids.add(rr["id"])
                if not active_ids:
                    # No scored (embedded) facts in this file scope — fall
                    # through to Jaccard so embed=False facts are still caught.
                    pass
            else:
                active_ids = set(
                    r["id"] for r in self.conn.execute(
                        "SELECT id FROM memory WHERE deprecated_at IS NULL"
                    ).fetchall()
                )
            for fid, sim in sorted(scores.items(), key=lambda x: x[1], reverse=True):
                if fid in active_ids and sim >= threshold:
                    return fid
            # No semantic match — fall through to tokenized-Jaccard fallback
            # below so facts stored with embed=False (no vector) are still
            # caught by dedup instead of escaping via an early None.
        # Fallback: tokenized Jaccard
        query_tokens = tokenize_text(text)
        if not query_tokens:
            return None
        if norm_fp:
            active = self.conn.execute(
                "SELECT id, content, rationale, file_path FROM memory WHERE deprecated_at IS NULL AND file_path IS NOT NULL"
            ).fetchall()
            # Filter to same file_path only
            active = [r for r in active if r["file_path"] and r["file_path"].strip().replace("\\", "/").lower() == norm_fp]
            if not active:
                return None
        else:
            active = self.conn.execute(
                "SELECT id, content, rationale FROM memory WHERE deprecated_at IS NULL"
            ).fetchall()
        best_sim, best_id = 0.0, None
        for row in active:
            doc_tokens = tokenize_text(f"{row['content']} {row['rationale'] or ''}")
            if doc_tokens:
                inter = len(query_tokens & doc_tokens)
                union = len(query_tokens | doc_tokens)
                sim = inter / union if union else 0.0
                if sim > best_sim:
                    best_sim, best_id = sim, row["id"]
        return best_id if best_sim >= threshold else None

    def write_batch(self, facts: List[Dict[str, Any]], force: bool = False, embed: bool = True,
                    embed_errors: Optional[List[int]] = None) -> List[int]:
        """Atomically write multiple facts into the live `memory` table (see write() intake contract).

        Args:
            embed_errors: optional list that receives the IDs of facts whose
                post-commit embedding failed, so callers can report (and heal
                via backfill) instead of silently missing semantic coverage.
        """
        if not facts:
            return []
        new_ids = []
        # Defer embedding until after COMMIT so the RESERVED write lock is not
        # held across slow model inference. Inserts stay atomic; embeddings are
        # best-effort per fact, matching write(embed=True) semantics.
        pending_embeddings: List[Tuple[int, str]] = []
        # Resolve the git HEAD once, BEFORE the transaction: one subprocess
        # instead of one per anchored fact, and no process spawn under lock.
        batch_sha: Optional[str] = None
        if any(
            not f.get("git_commit_sha") and f.get("file_path") and f.get("line_start") is not None
            for f in facts if isinstance(f, dict)
        ):
            try:
                batch_sha = self._current_git_commit()
            except Exception:
                batch_sha = None
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for f in facts:
                nid = self.write(
                    category=f.get("category", ""),
                    content=f.get("content", ""),
                    rationale=f.get("rationale"),
                    confidence=f.get("confidence", "assumed"),
                    session_id=f.get("session_id"),
                    file_path=f.get("file_path"),
                    tags=f.get("tags"),
                    importance=f.get("importance", 3),
                    line_start=f.get("line_start"),
                    line_end=f.get("line_end"),
                    code_hash=f.get("code_hash"),
                    git_commit_sha=f.get("git_commit_sha") or batch_sha,
                    force=force,
                    embed=False,
                )
                new_ids.append(nid)
                if embed and isinstance(nid, int) and nid > 0:
                    content = f.get("content", "") or ""
                    rationale = f.get("rationale") or ""
                    pending_embeddings.append((nid, f"{content} {rationale}"))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        if embed:
            for nid, text in pending_embeddings:
                try:
                    self._store_embedding(nid, text)
                except Exception as e:
                    logger.warning("memory embedding failed for fact_id %s: %r", nid, e)
                    if embed_errors is not None:
                        embed_errors.append(nid)
        return new_ids

    def supersede(self, old_id: int, new_content: str, rationale: Optional[str] = None,
                  confidence: str = "assumed", session_id: Optional[str] = None,
                  file_path: Optional[str] = None, tags: Optional[str] = None,
                  expected_version: Optional[int] = None) -> int:
        row = self.conn.execute("SELECT * FROM memory WHERE id = ?", (old_id,)).fetchone()
        if row is None:
            raise ValueError(f"no memory row with id {old_id}")
        if expected_version is not None and row["version"] != expected_version:
            raise ValueError(f"optimistic locking error: fact {old_id} has version {row['version']}, expected {expected_version}")
        if not new_content or not new_content.strip():
            raise ValueError("new_content must not be empty")
        confidence = confidence.lower().strip()
        if confidence not in VALID_CONFIDENCE:
            raise ValueError(f"confidence must be one of {sorted(VALID_CONFIDENCE)}, got {confidence!r}")
        if looks_like_secret(new_content) or looks_like_secret(rationale):
            raise ValueError("refused: this looks like it contains a credential or secret.")
        
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        new_version = row["version"] + 1
        # Normalize file_path to use forward slashes for consistent matching
        norm_file_path = file_path.strip().replace("\\", "/") if file_path else row["file_path"]
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.execute(
                "INSERT INTO memory (category, content, rationale, confidence, created_at, session_id, "
                "version, importance, file_path, tags, line_start, line_end, code_hash, git_commit_sha) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["category"], new_content.strip(), rationale, confidence, now, session_id,
                 new_version, row["importance"],
                 norm_file_path,
                 tags.strip() if tags else row["tags"],
                 row["line_start"], row["line_end"], row["code_hash"], row["git_commit_sha"]),
            )
            new_id = cur.lastrowid
            self.conn.execute(
                "UPDATE memory SET superseded_by = ?, deprecated_at = ? WHERE id = ?",
                (new_id, now, old_id),
            )
            # Transfer graph edges to the successor so knowledge stays connected
            self.conn.execute(
                "UPDATE OR IGNORE memory_edges SET source_id = ? WHERE source_id = ?",
                (new_id, old_id),
            )
            self.conn.execute(
                "UPDATE OR IGNORE memory_edges SET target_id = ? WHERE target_id = ?",
                (new_id, old_id),
            )
            # Drop any edge still touching the deprecated fact (collision leftovers)
            self.conn.execute(
                "DELETE FROM memory_edges WHERE source_id = ? OR target_id = ?",
                (old_id, old_id),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        # Embed the replacement fact so semantic search stays current
        try:
            self._store_embedding(new_id, f"{new_content} {rationale or ''}")
        except Exception as e:
            logger.warning("supersede embedding failed for new_id %s: %r", new_id, e)

        return new_id

    def deprecate(self, id: int, reason: Optional[str] = None) -> None:
        row = self.conn.execute("SELECT id FROM memory WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise ValueError(f"no memory row with id {id}")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE memory SET deprecated_at = ?, deprecated_reason = ? WHERE id = ?",

            (now, reason, id),
        )

    def revision_transaction(self, repo_id: int) -> "RevisionTransaction":

        """Phase 0 revision protocol entry point: allocate a revision number atomically."""
        return RevisionTransaction(self, repo_id)

    def _paths_match(self, clean_path: str, fp: str) -> bool:
        """Match stored file_path against a query path without cross-directory false positives."""
        if clean_path == fp:
            return True
        if clean_path in fp or fp in clean_path:
            return True
        # Bare-filename fallback ONLY when one side has no directory component

        p1, p2 = Path(clean_path), Path(fp)
        if p1.name == p2.name and (len(p1.parts) == 1 or len(p2.parts) == 1):
            return True
        return False

    def get_by_id(self, fact_id: int) -> Optional[Dict[str, Any]]:
        """Retrieve a specific fact by its ID."""
        row = self.conn.execute("SELECT * FROM memory WHERE id = ?", (fact_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def get_by_ids(self, fact_ids: List[int]) -> List[Dict[str, Any]]:
        """Retrieve multiple facts by IDs preserving order."""
        if not fact_ids:
            return []
        placeholders = ",".join("?" * len(fact_ids))
        rows = self.conn.execute(f"SELECT * FROM memory WHERE id IN ({placeholders})", fact_ids).fetchall()
        row_dict = {r["id"]: self._row_to_dict(r) for r in rows}
        return [row_dict[fid] for fid in fact_ids if fid in row_dict]

    def get_by_file(self, file_path: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve active facts recorded for a specific file path."""
        clean_path = file_path.strip().replace("\\", "/").lower()
        all_active = self.get_state(limit=MAX_LIMIT)
        matches = []

        for f in all_active:
            if f["file_path"]:
                fp = f["file_path"].strip().replace("\\", "/").lower()
                if self._paths_match(clean_path, fp):
                    matches.append(f)
                    if len(matches) >= limit:
                        break
        return matches

    def link_facts(self, source_id: int, target_id: int, relation: str) -> int:
        relation = relation.lower().strip()
        if relation not in VALID_RELATIONS:
            raise ValueError(f"relation must be one of {sorted(VALID_RELATIONS)}, got {relation!r}")
        s_row = self.conn.execute("SELECT id FROM memory WHERE id = ?", (source_id,)).fetchone()
        t_row = self.conn.execute("SELECT id FROM memory WHERE id = ?", (target_id,)).fetchone()

        if not s_row or not t_row:
            raise ValueError(f"source_id {source_id} or target_id {target_id} does not exist")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT OR REPLACE INTO memory_edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
            (source_id, target_id, relation, now),

        )
        return cur.lastrowid

    def unlink_facts(self, source_id: int, target_id: int, relation: str) -> None:
        self.conn.execute(
            "DELETE FROM memory_edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (source_id, target_id, relation.lower().strip()),
        )

    def get_graph(self, root_id: int, max_depth: int = 3) -> List[Dict[str, Any]]:
        sql = """
        WITH RECURSIVE fact_graph AS (
            SELECT source_id, target_id, relation, 1 AS depth
            FROM memory_edges
            WHERE source_id = ?
            
            UNION ALL
            
            SELECT e.source_id, e.target_id, e.relation, g.depth + 1
            FROM memory_edges e
            JOIN fact_graph g ON e.source_id = g.target_id
            WHERE g.depth < ?
        )
        SELECT g.depth, g.relation,
               m1.id as source_id, m1.category as source_category, m1.content as source_content,

               m2.id as target_id, m2.category as target_category, m2.content as target_content
        FROM fact_graph g
         JOIN memory m1 ON g.source_id = m1.id AND m1.deprecated_at IS NULL
         JOIN memory m2 ON g.target_id = m2.id AND m2.deprecated_at IS NULL;
        """
        rows = self.conn.execute(sql, (root_id, max_depth)).fetchall()
        return [dict(r) for r in rows]

    def get_incoming_graph(self, target_id: int) -> List[Dict[str, Any]]:
        """Retrieve all incoming edges (facts that depend on or implement this fact)."""
        sql = """
        SELECT e.relation,
               m.id as source_id, m.category as source_category, m.content as source_content, m.file_path as source_file_path
        FROM memory_edges e

        JOIN memory m ON e.source_id = m.id
        WHERE e.target_id = ? AND m.deprecated_at IS NULL;
        """
        rows = self.conn.execute(sql, (target_id,)).fetchall()
        return [dict(r) for r in rows]

    def acquire_lock(self, resource: str, locked_by: str, ttl_seconds: int = 60) -> bool:
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now_str = now_dt.isoformat()
        expires_str = (now_dt + datetime.timedelta(seconds=ttl_seconds)).isoformat()

        # Delete-check-insert must be atomic: two agents racing here must not
        # both observe "no row" and both INSERT. BEGIN IMMEDIATE serializes.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # Remove expired locks
            self.conn.execute("DELETE FROM memory_locks WHERE expires_at <= ?", (now_str,))
            row = self.conn.execute("SELECT * FROM memory_locks WHERE resource = ?", (resource,)).fetchone()
            if row:
                if row["locked_by"] == locked_by:
                    # Extend lease
                    self.conn.execute(
                        "UPDATE memory_locks SET locked_at = ?, expires_at = ? WHERE resource = ?",
                        (now_str, expires_str, resource),
                    )
                    self.conn.execute("COMMIT")
                    return True
                self.conn.execute("COMMIT")
                return False

            try:
                self.conn.execute(
                    "INSERT INTO memory_locks (resource, locked_by, locked_at, expires_at) VALUES (?, ?, ?, ?)",
                    (resource, locked_by, now_str, expires_str),
                )
                self.conn.execute("COMMIT")
                return True
            except sqlite3.IntegrityError:
                try:
                    self.conn.execute("ROLLBACK")
                except Exception as e:
                    logger.warning("lock rollback failed (resource=%s): %r", resource, e)
                return False
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception as e:
                logger.warning("lock rollback failed (resource=%s): %r", resource, e)
            raise

    def release_lock(self, resource: str, locked_by: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM memory_locks WHERE resource = ? AND locked_by = ?",
            (resource, locked_by),
        )
        return cur.rowcount > 0

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        keys = row.keys()
        return {
            "id": row["id"],
            "category": row["category"],
            "content": row["content"],
            "rationale": row["rationale"],
            "confidence": row["confidence"],
            "created_at": row["created_at"],
            "session_id": row["session_id"],
            "version": row["version"] if "version" in keys else 1,
            "importance": row["importance"] if "importance" in keys else 3,
            "access_count": row["access_count"] if "access_count" in keys else 0,
            "last_accessed_at": row["last_accessed_at"] if "last_accessed_at" in keys else None,
            "superseded_by": row["superseded_by"],
            "deprecated_at": row["deprecated_at"],
            "deprecated_reason": row["deprecated_reason"],
            "file_path": row["file_path"] if "file_path" in keys else None,
            "line_start": row["line_start"] if "line_start" in keys else None,
            "line_end": row["line_end"] if "line_end" in keys else None,
            "code_hash": row["code_hash"] if "code_hash" in keys else None,
            "git_commit_sha": row["git_commit_sha"] if "git_commit_sha" in keys else None,
            "tags": row["tags"] if "tags" in keys else None,
        }

    def get_state(self, category: Optional[str] = None, limit: int = 200,
                session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_LIMIT))
        sql = "SELECT * FROM memory WHERE deprecated_at IS NULL"
        params: List[Any] = []
        if category:
            sql += " AND category = ?"
            params.append(category.lower().strip())
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()

        return [self._row_to_dict(r) for r in rows]

    def get_episodic_state(self, category: Optional[str] = None, limit: int = 200,
                           repo_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Mirror of get_state for episodic_memories, filtered to root branch ACTIVE."""
        limit = max(1, min(int(limit), MAX_LIMIT))
        # If repo_id is None, derive single "own" repo (first repo) — single-repo deployment.
        # This avoids unioning all roots in multi-repo (which would leak cross-repo canonical content).
        if repo_id is None:
            repo_id = self._derive_default_repo_id()
        if repo_id is not None:
            sql = "SELECT em.* FROM episodic_memories em JOIN memory_branches mb ON em.branch_id = mb.branch_id WHERE mb.is_root=1 AND mb.repo_id=? AND em.status='ACTIVE' AND em.closed_revision IS NULL"
            params: List[Any] = [repo_id]
        else:
            sql = "SELECT em.* FROM episodic_memories em JOIN memory_branches mb ON em.branch_id = mb.branch_id WHERE mb.is_root=1 AND em.status='ACTIVE' AND em.closed_revision IS NULL"
            params = []
        if category:
            sql += " AND em.category = ?"
            params.append(category.lower().strip())
        sql += " ORDER BY em.created_at DESC LIMIT ?"
        params.append(limit)
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # episodic_memories may not exist on old DBs
            return []
        # Reuse _row_to_dict shape where possible, but episodic has different columns
        # Return a dict with same keys as memory where applicable, plus source discriminator
        result = []
        for r in rows:
            d = dict(r)
            # normalize to same shape as memory for tokenized lane: ensure 'id' key exists
            d["id"] = r["memory_id"]
            d["source"] = "episodic"
            result.append(d)
        return result

    def stats(self) -> Dict[str, Any]:
        rows = self.conn.execute(
            "SELECT category, COUNT(*) as n FROM memory WHERE deprecated_at IS NULL GROUP BY category"
        ).fetchall()
        total_active = sum(r["n"] for r in rows)
        total_deprecated = self.conn.execute(
            "SELECT COUNT(*) as n FROM memory WHERE deprecated_at IS NOT NULL"
        ).fetchone()["n"]
        total_edges = self.conn.execute("SELECT COUNT(*) as n FROM memory_edges").fetchone()["n"]
        embedded = self.conn.execute(
            "SELECT COUNT(*) as n FROM memory_embeddings").fetchone()["n"]
        # Intake contract (2026-08-31): the `memory` table is the live intake
        # store by contract; episodic_memories holds the genesis-migrated
        # historical archive. Rows added after genesis are expected live
        # facts, not drift. The counters below keep the split visible.
        try:
            genesis_migrated = self.conn.execute(
                "SELECT COUNT(*) as n FROM memory WHERE deprecated_reason='migrated to episodic via genesis'"
            ).fetchone()["n"]
        except sqlite3.OperationalError as e:
            # Expected pre-genesis / pre-slice: table/column not yet present — not an error
            if "no such table" in str(e) or "no such column" in str(e):
                genesis_migrated = 0
            else:
                logger.warning("stats genesis_migrated count failed: %r", e)
                genesis_migrated = 0
        except Exception as e:
            logger.warning("stats genesis_migrated count failed: %r", e)
            genesis_migrated = 0
        try:
            episodic_active = self.conn.execute(
                "SELECT COUNT(*) as n FROM episodic_memories WHERE status='ACTIVE' AND closed_revision IS NULL"
            ).fetchone()["n"]
        except sqlite3.OperationalError as e:
            if "no such table" in str(e) or "no such column" in str(e):
                episodic_active = 0
            else:
                logger.warning("stats episodic_active count failed: %r", e)
                episodic_active = 0
        except Exception as e:
            logger.warning("stats episodic_active count failed: %r", e)
            episodic_active = 0
        active_post_genesis = total_active  # alias for visibility; after genesis all ACTIVE are post-genesis drift
        return {
            "active_by_category": {r["category"]: r["n"] for r in rows},
            "total_active": total_active,
            "active_post_genesis": active_post_genesis,
            "active_post_genesis_count": active_post_genesis,  # explicit alias requested in task
            "genesis_migrated": genesis_migrated,
            "episodic_active": episodic_active,
            "total_deprecated": total_deprecated,
            "total_edges": total_edges,
            "embedded_facts": embedded,
            "semantic_backend": _SEMANTIC_AVAILABLE,
            "background_failures": dict(_BACKGROUND_FAILURES),
            "db_path": self.db_path,
        }

    def get_stats(self) -> Dict[str, Any]:
        """Alias for stats() method."""
        return self.stats()



# --- SINGLETON & CONVENIENCE INTERFACE ---
class RevisionTransaction:
    """
    Phase 0 revision protocol wrapper (blueprint Sections 28-31).

    Usage:
        with store.revision_transaction(repo_id) as rt:
            conn.execute(
                "INSERT INTO observations (..., created_revision) VALUES (..., ?)",
                (..., rt.revision),
            )

    __enter__ opens BEGIN IMMEDIATE and bumps current_revision, exposing the
    allocated value as .revision. The write lock guarantees uniqueness across
    concurrent transactions; the phase0 INSERT trigger enforces that every
    observation stamped with .revision matches current_revision at insert time.

    __exit__ COMMITs on clean exit; on exception it ROLLBACKs (discarding both
    the revision bump and any partial inserts) and re-raises the original
    exception.
    """

    def __init__(self, store, repo_id: int):
        self.store = store
        self.conn = store.conn
        self.repo_id = repo_id
        self.revision: Optional[int] = None

    def __enter__(self) -> "RevisionTransaction":
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "UPDATE repositories SET current_revision = current_revision + 1 WHERE repo_id = ?",
                (self.repo_id,),
            )
            if cur.rowcount != 1:
                raise ValueError(f"no repository with repo_id {self.repo_id}")
            row = self.conn.execute(
                "SELECT current_revision FROM repositories WHERE repo_id = ?", (self.repo_id,)
            ).fetchone()
            self.revision = row[0]
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


_GLOBAL_STORE_LOCK = threading.Lock()
_THREAD_STORES = threading.local()


def get_store(db_path: Optional[str] = None) -> MemoryStore:
    """Returns a per-thread MemoryStore for the requested DB path.

    sqlite3 connections are bound to their creating thread, so a single
    process-wide singleton raises ProgrammingError the moment a second
    thread touches it. Each thread gets its own connection on the same WAL
    database (per decision: separate connections serialize via SQLite
    itself); same-thread callers still share one instance.
    """
    from pm_common import default_db_path as _default_db_path
    key = db_path or _default_db_path()
    cache = getattr(_THREAD_STORES, "stores", None)
    if cache is None:
        cache = {}
        _THREAD_STORES.stores = cache
    store = cache.get(key)
    if store is None:
        with _GLOBAL_STORE_LOCK:
            store = cache.get(key)
            if store is None:
                store = MemoryStore(key)
                cache[key] = store
    return store


def remember(category: str, content: str, rationale: Optional[str] = None, **kwargs) -> int:
    """Convenience helper to record a new fact into project memory."""
    return get_store().write(category, content, rationale=rationale, **kwargs)


def recall(query: str, limit: int = 5) -> str:
    """Convenience helper to retrieve Markdown context for a topic or question."""
    return get_store().get_context(query, limit=limit)


def file_briefing(file_path: str) -> str:
    """Convenience helper to get impact analysis briefing before editing a file."""
    return get_store().get_file_context(file_path)


def prepare_task(intent: str, file_paths: Optional[List[str]] = None, limit: int = 5) -> str:
    """Convenience helper for pre-coding flight briefing."""
    return get_store().prepare_task(intent, file_paths=file_paths, limit=limit)


def checkpoint_task(summary: str, **kwargs) -> Dict[str, Any]:
    """Convenience helper for post-coding task checkpoint."""
    return get_store().checkpoint_task(summary, **kwargs)
