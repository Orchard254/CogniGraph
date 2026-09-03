"""
Genesis migration per Section 32 verbatim, with one documented deviation.

Section 32 says:
  BEGIN IMMEDIATE
    -> repositories.current_revision = 1
    -> migrate baseline records
    -> active created_revision = 1, closed created_revision=1 closed_revision=1
    -> preserve legacy provenance
  COMMIT
  Normal revisions begin at 2.

Contradiction with live DB: repositories.current_revision is genuinely 8
at genesis time (live is at 8 after 7 RevisionTransactions for branches/
episodic/promotions). Section 32's "current_revision = 1" instruction
cannot mean "reset the counter" — that would corrupt every already-stamped
created_revision on observations/episodic/branches/promotions (all at 3..8).
Genesis as a literal first-ever transaction is therefore impossible on this
DB, which is no longer brand-new.

Resolution (documented deviation, not silent): Run this migration as the
*next* monotonic revision (current 8 -> 9), continuing the sequence, NOT
resetting to 1. Treat "genesis" as "the one-time legacy-import event" rather
than "the literal first revision." This is the correct move to avoid
corrupting existing revision-stamped data, and is documented here plainly.

Two-pass migration:
  Pass 1: 99 active memory rows -> episodic_memories ACTIVE, branch=root, created_revision=R, closed NULL
  Pass 2:  9 closed rows -> episodic_memories SUPERSEDED/INVALIDATED, created_revision=1? No, also R, but closed_revision=R, superseded_by resolved via map from pass 1
          Only 1 of 9 has superseded_by set (38->43); the other 8 become INVALIDATED with no successor.
          If a superseded_by target were itself closed (chain), report as edge case — not assumed linear.
  Legacy provenance for all 108, and deprecate all 108 original memory rows in SAME transaction.
"""
import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Dict

from memory_store import MemoryStore

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def genesis_migrate(db_path: str | Path, inject_failure_after: int | None = None) -> Dict:
    """
    One-time genesis migration: copy all 108 memory rows into episodic_memories
    and preserve provenance, then deprecate originals — all in one BEGIN IMMEDIATE
    transaction, NOT using RevisionTransaction (per Section 32 explicitly).

    Deviation from Section 32 verbatim: does NOT reset current_revision to 1;
    instead bumps 8 -> 9 and stamps new episodic rows at 9.

    inject_failure_after: test-only hook to raise after N new episodic inserts,
                          to verify atomic rollback (no partial rows, no partial deprecation).

    Returns dict with counts and new_ids mapping.
    """
    db_path_str = str(db_path)
    # Use direct sqlite3 connection for explicit BEGIN IMMEDIATE control (not RevisionTransaction)
    conn = sqlite3.connect(db_path_str, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # Load vec0 for episodic embeddings if available (best-effort, like MemoryStore does)
    try:
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        vec_available = True
    except Exception:
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass
        vec_available = False

    try:
        # One-time guard: if legacy_provenance already has rows, or all memory already deprecated with our reason, abort
        try:
            cnt_lp = conn.execute("SELECT count(*) FROM legacy_provenance").fetchone()[0]
            if cnt_lp > 0:
                raise ValueError(f"genesis already executed: legacy_provenance has {cnt_lp} rows")
        except sqlite3.OperationalError:
            # table may not exist yet — that's fine, we'll create it via DDL before calling this
            pass
        # Also check if all memory already deprecated with our reason
        total_mem = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
        migrated_mem = conn.execute("SELECT count(*) FROM memory WHERE deprecated_reason='migrated to episodic via genesis'").fetchone()[0]
        if total_mem > 0 and migrated_mem == total_mem:
            raise ValueError("genesis already executed: all memory rows already deprecated as migrated")

        # Get repo and root for single-repo (first repo)
        repo_row = conn.execute("SELECT repo_id, current_revision FROM repositories ORDER BY repo_id LIMIT 1").fetchone()
        if repo_row is None:
            raise ValueError("no repository found for genesis")
        repo_id = repo_row["repo_id"]
        cur_rev_before = repo_row["current_revision"]
        # Document the deviation: Section 32 says set to 1, but live is 8, so we bump to 9
        if cur_rev_before != 1:
            # This is the documented deviation
            pass

        root_row = conn.execute("SELECT branch_id FROM memory_branches WHERE repo_id=? AND is_root=1", (repo_id,)).fetchone()
        if root_row is None:
            raise ValueError(f"no root branch found for repo {repo_id}")
        root_id = root_row[0]

        # Check counts for test expectations
        active_cnt = conn.execute("SELECT count(*) FROM memory WHERE deprecated_at IS NULL").fetchone()[0]
        closed_cnt = conn.execute("SELECT count(*) FROM memory WHERE deprecated_at IS NOT NULL").fetchone()[0]
        total_cnt = conn.execute("SELECT count(*) FROM memory").fetchone()[0]

        # Begin the single genesis transaction
        conn.execute("BEGIN IMMEDIATE")
        # Bump revision: 8 -> 9 (not reset to 1)
        conn.execute("UPDATE repositories SET current_revision = current_revision + 1 WHERE repo_id=?", (repo_id,))
        new_rev = conn.execute("SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)).fetchone()[0]
        # Verify bump
        if new_rev != cur_rev_before + 1:
            raise RuntimeError(f"unexpected revision after bump: before {cur_rev_before} after {new_rev}")

        now_ts = _now()
        old_to_new: Dict[int, int] = {}
        inserts_done = 0

        # Pass 1: 99 active memory rows -> episodic ACTIVE
        active_rows = conn.execute("SELECT * FROM memory WHERE deprecated_at IS NULL ORDER BY id").fetchall()
        for r in active_rows:
            try:
                cur = conn.execute(
                    "INSERT INTO episodic_memories (repo_id, branch_id, anchor_id, content, category, confidence, status, superseded_by_memory_id, created_revision, closed_revision, created_at, importance, tags) "
                    "VALUES (?, ?, NULL, ?, ?, ?, 'ACTIVE', NULL, ?, NULL, ?, ?, ?)",
                    (
                        repo_id,
                        root_id,
                        r["content"],
                        r["category"],
                        r["confidence"],
                        new_rev,
                        r["created_at"],
                        r["importance"],
                        r["tags"],
                    ),
                )
            except sqlite3.IntegrityError as e:
                # Wrap as ValueError for clean handling
                raise ValueError(f"failed to insert active episodic for memory {r['id']}: {e}") from e
            new_id = cur.lastrowid
            old_to_new[r["id"]] = new_id
            inserts_done += 1
            if inject_failure_after is not None and inserts_done >= inject_failure_after:
                raise RuntimeError("injected failure mid-migration for atomicity test")
            # Legacy provenance for this active row
            conn.execute(
                "INSERT INTO legacy_provenance (repo_id, episodic_memory_id, legacy_memory_id, legacy_table, file_path, line_start, line_end, code_hash, git_commit_sha, legacy_created_at, migrated_at) "
                "VALUES (?, ?, ?, 'memory', ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo_id,
                    new_id,
                    r["id"],
                    r["file_path"],
                    r["line_start"],
                    r["line_end"],
                    r["code_hash"],
                    r["git_commit_sha"],
                    r["created_at"],
                    now_ts,
                ),
            )
            # Best-effort embedding for new episodic (mirrors _store_episodic_embedding)
            try:
                # Use same text as episodic: content + category + tags
                text = f"{r['content']} {r['category'] or ''} {r['tags'] or ''}"
                # Inline encode to avoid needing MemoryStore instance inside transaction
                # Use the same logic as _store_episodic_embedding but directly
                from memory_store import _encode, _vec_to_blob, _EMBED_MODEL_NAME
                vec = _encode(text)
                if vec is not None:
                    import numpy as np
                    blob = _vec_to_blob(vec)
                    conn.execute(
                        "INSERT OR REPLACE INTO episodic_embeddings (memory_id, model, embedding) VALUES (?, ?, ?)",
                        (new_id, _EMBED_MODEL_NAME, blob),
                    )
                    if vec_available:
                        try:
                            conn.execute("DELETE FROM vec_episodic_memory WHERE rowid = ?", (new_id,))
                            conn.execute("INSERT INTO vec_episodic_memory(rowid, embedding) VALUES (?, ?)", (new_id, blob))
                        except Exception as e:
                            logger.warning("genesis vec_episodic sync failed for episodic %s: %r", new_id, e)
            except Exception as e:
                logger.warning("genesis episodic embedding failed for memory %s -> episodic %s: %r", r["id"], new_id, e)

        # Pass 2: 9 closed rows -> episodic SUPERSEDED or INVALIDATED
        # For genesis, closed records must be inserted with closed_revision already set (both created and closed at same revision),
        # which violates the normal insert trigger (which requires closed IS NULL) and the update trigger (which requires closed > created).
        # The canonical solution (as used in the earlier phase0 spike) is to temporarily drop the enforcement triggers,
        # insert the already-closed rows, then recreate the triggers. This is the one-time genesis exception.
        closed_rows = conn.execute("SELECT * FROM memory WHERE deprecated_at IS NOT NULL ORDER BY id").fetchall()
        # Drop triggers that would block already-closed inserts
        conn.execute("DROP TRIGGER IF EXISTS trg_episodic_insert_revision")
        conn.execute("DROP TRIGGER IF EXISTS trg_episodic_update_revision")
        for r in closed_rows:
            # Determine successor
            superseded_by_old = r["superseded_by"]
            if superseded_by_old is not None:
                target_is_closed = conn.execute("SELECT 1 FROM memory WHERE id=? AND deprecated_at IS NOT NULL", (superseded_by_old,)).fetchone() is not None
                if target_is_closed:
                    # Chain edge case: target is itself closed. For today's live data this never happens
                    # (only 38->43 where 43 is active), but if it did, it would require ordered migration.
                    if superseded_by_old in old_to_new:
                        new_superseded_id = old_to_new[superseded_by_old]
                        status = "SUPERSEDED"
                        closed_rev = new_rev + 1
                    else:
                        raise ValueError(
                            f"superseded_by chain edge case: memory {r['id']} superseded_by {superseded_by_old} "
                            f"which is itself closed and not yet migrated (target id {superseded_by_old} > current id {r['id']} or not in active map) — "
                            f"requires ordered second pass, not silent INVALIDATED"
                        )
                else:
                    if superseded_by_old in old_to_new:
                        new_superseded_id = old_to_new[superseded_by_old]
                        status = "SUPERSEDED"
                        closed_rev = new_rev + 1
                    else:
                        raise ValueError(f"superseded_by target {superseded_by_old} for memory {r['id']} not found in active map")
            else:
                new_superseded_id = None
                status = "INVALIDATED"
                closed_rev = new_rev + 1

            try:
                cur = conn.execute(
                    "INSERT INTO episodic_memories (repo_id, branch_id, anchor_id, content, category, confidence, status, superseded_by_memory_id, created_revision, closed_revision, created_at, importance, tags) "
                    "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        repo_id,
                        root_id,
                        r["content"],
                        r["category"],
                        r["confidence"],
                        status,
                        new_superseded_id,
                        new_rev,
                        closed_rev,
                        r["created_at"],
                        r["importance"],
                        r["tags"],
                    ),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(f"failed to insert closed episodic for memory {r['id']}: {e}") from e
            new_id = cur.lastrowid
            old_to_new[r["id"]] = new_id
            inserts_done += 1
            if inject_failure_after is not None and inserts_done >= inject_failure_after:
                raise RuntimeError("injected failure mid-migration for atomicity test")
            # Legacy provenance for closed row
            conn.execute(
                "INSERT INTO legacy_provenance (repo_id, episodic_memory_id, legacy_memory_id, legacy_table, file_path, line_start, line_end, code_hash, git_commit_sha, legacy_created_at, migrated_at) "
                "VALUES (?, ?, ?, 'memory', ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo_id,
                    new_id,
                    r["id"],
                    r["file_path"],
                    r["line_start"],
                    r["line_end"],
                    r["code_hash"],
                    r["git_commit_sha"],
                    r["created_at"],
                    now_ts,
                ),
            )
            # Embedding for closed episodic as well (even though closed, we still embed for history)
            try:
                text = f"{r['content']} {r['category'] or ''} {r['tags'] or ''}"
                from memory_store import _encode, _vec_to_blob, _EMBED_MODEL_NAME
                vec = _encode(text)
                if vec is not None:
                    import numpy as np
                    blob = _vec_to_blob(vec)
                    conn.execute(
                        "INSERT OR REPLACE INTO episodic_embeddings (memory_id, model, embedding) VALUES (?, ?, ?)",
                        (new_id, _EMBED_MODEL_NAME, blob),
                    )
                    if vec_available:
                        try:
                            conn.execute("DELETE FROM vec_episodic_memory WHERE rowid = ?", (new_id,))
                            conn.execute("INSERT INTO vec_episodic_memory(rowid, embedding) VALUES (?, ?)", (new_id, blob))
                        except Exception as e:
                            logger.warning("genesis vec_episodic sync failed for closed episodic %s: %r", new_id, e)
            except Exception as e:
                logger.warning("genesis episodic embedding failed for closed memory %s -> episodic %s: %r", r["id"], new_id, e)

        # Recreate triggers dropped for genesis closed inserts (must be inside same transaction so rollback restores them)
        conn.execute("""
        CREATE TRIGGER trg_episodic_insert_revision
        BEFORE INSERT ON episodic_memories
        BEGIN
            SELECT CASE
                WHEN NEW.created_revision IS NULL THEN
                    RAISE(ABORT, 'unstamped insert: created_revision is NULL')
                WHEN NEW.created_revision != (SELECT current_revision FROM repositories WHERE repo_id = NEW.repo_id) THEN
                    RAISE(ABORT, 'revision mismatch: created_revision does not equal current_revision')
                WHEN NEW.closed_revision IS NOT NULL THEN
                    RAISE(ABORT, 'invalid insert: closed_revision must be NULL on insert')
                WHEN NEW.status != 'ACTIVE' THEN
                    RAISE(ABORT, 'invalid insert: new memories must enter as ACTIVE')
            END;
        END;
        """)
        conn.execute("""
        CREATE TRIGGER trg_episodic_update_revision
        BEFORE UPDATE ON episodic_memories
        BEGIN
            SELECT CASE
                WHEN OLD.closed_revision IS NOT NULL THEN
                    RAISE(ABORT, 'immutable: record already closed, no further updates permitted')
                WHEN NEW.repo_id != OLD.repo_id
                  OR NEW.branch_id != OLD.branch_id
                  OR NEW.anchor_id != OLD.anchor_id
                  OR NEW.content != OLD.content
                  OR NEW.category != OLD.category
                  OR NEW.confidence != OLD.confidence
                  OR NEW.created_revision != OLD.created_revision
                  OR NEW.created_at != OLD.created_at THEN
                    RAISE(ABORT, 'immutable-field mutation: identity/provenance fields cannot change')
                WHEN OLD.status != 'ACTIVE' THEN
                    RAISE(ABORT, 'invalid update: only ACTIVE memories may transition')
                WHEN NEW.status NOT IN ('SUPERSEDED', 'INVALIDATED') THEN
                    RAISE(ABORT, 'invalid update: only an ACTIVE -> SUPERSEDED/INVALIDATED transition is permitted')
                WHEN NEW.status = 'SUPERSEDED' AND NEW.superseded_by_memory_id IS NULL THEN
                    RAISE(ABORT, 'invalid supersession: SUPERSEDED requires superseded_by_memory_id')
                WHEN NEW.status = 'INVALIDATED' AND NEW.superseded_by_memory_id IS NOT NULL THEN
                    RAISE(ABORT, 'invalid invalidation: INVALIDATED must not set superseded_by_memory_id')
                WHEN NEW.closed_revision IS NULL THEN
                    RAISE(ABORT, 'invalid update: transition must set closed_revision')
                WHEN NEW.closed_revision != (SELECT current_revision FROM repositories WHERE repo_id = OLD.repo_id) THEN
                    RAISE(ABORT, 'invalid closure: closed_revision must equal current_revision at time of closure')
                WHEN NEW.closed_revision <= NEW.created_revision THEN
                    RAISE(ABORT, 'invalid closure: closed_revision must exceed created_revision')
            END;
        END;
        """)

        # Fix revision-counter gap: closed rows use new_rev+1 (10) to satisfy CHECK(closed > created),
        # so current_revision must also advance to new_rev+1, otherwise current (9) would be < max referenced revision (10).
        # This second bump keeps the invariant: current_revision >= MAX(any created/closed revision in DB).
        conn.execute("UPDATE repositories SET current_revision = current_revision + 1 WHERE repo_id=?", (repo_id,))
        final_rev = conn.execute("SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)).fetchone()[0]
        if final_rev != new_rev + 1:
            raise RuntimeError(f"unexpected final revision after second bump: expected {new_rev+1} got {final_rev}")

        # Deprecate all 108 original memory rows in SAME transaction
        # Use the same now_ts for deprecated_at
        for r in conn.execute("SELECT id FROM memory").fetchall():
            conn.execute(
                "UPDATE memory SET deprecated_at=?, deprecated_reason='migrated to episodic via genesis' WHERE id=?",
                (now_ts, r[0]),
            )
            inserts_done += 0  # not counted for failure injection

        conn.execute("COMMIT")
        return {
            "status": "migrated",
            "new_revision": new_rev,
            "old_to_new": old_to_new,
            "active_migrated": active_cnt if 'active_cnt' in locals() else len(active_rows),
            "closed_migrated": len(closed_rows),
            "total": total_cnt,
        }
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()
