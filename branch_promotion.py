"""
Promotion algorithm for Phase 2 administrative layer.
Reads an experimental branch's episodic_memories and produces new canonical
records under KEEP_BOTH, using the already-deployed triggers.

Confirmed via real trigger inspection (2026-08-24):
- No per-record tombstone mechanism exists for a branch to locally hide an
  inherited canonical episodic_memory short of UPDATE-ing the foreign row
  (branch_tombstones is per-branch UNIQUE(branch_id), memory_lineage_events
  is per-branch CREATED/PROMOTED/DISCARDED, not per-memory SUPERSEDES).
- trg_episodic_update_revision makes content-level MODIFICATION structurally
  impossible post-insert (WHEN NEW.content != OLD.content -> ABORT
  'immutable-field mutation'). Only status transitions ACTIVE->SUPERSEDED/
  INVALIDATED with same content are permitted.

Therefore for this slice REMOVAL and MODIFICATION are UNREACHABLE; only
ADDITION, CONVERGENT, and UNCHANGED are real classification outcomes
(UNREACHABLE stated plainly, not "less common").

Section 21-23 verbatim confirms KEEP_BOTH still requires full diff
classification (ADDITION/CONVERGENT/UNCHANGED etc.) — not "just copy
everything".

Section 18-19 defines "canonical state at base_revision" as revision-bounded
visibility: canonical root episodic_memories where branch_id=target,
status='ACTIVE', created_revision <= base_revision AND
(closed_revision IS NULL OR closed_revision > base_revision), not live
canonical. Promotion diff per Section 21 is experimental effective state
vs current canonical (at promotion revision).

Section 25 enforcement already covered by existing triggers; no new
enforcement needed for this slice beyond what is deployed.
"""
import datetime
import logging
from pathlib import Path
from typing import Dict, List

from memory_store import MemoryStore

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def promote_branch(
    db_path: str | Path,
    source_branch_id: int,
    convergence_policy: str = "KEEP_BOTH",
    inject_failure_after: int | None = None,
) -> Dict:
    """
    Promote an experimental branch's episodic_memories into its canonical root
    under KEEP_BOTH. One atomic RevisionTransaction.

    Steps (all in one transaction, one revision R):
      a. compute experimental effective state (canonical@base_revision plus
         branch-local ACTIVE) — per #1 there is no per-record tombstone to
         subtract, so minus set is empty.
      b. classify each branch-local record: UNCHANGED (same memory_id in
         canonical@current) / ADDITION (no logical-identity match) /
         CONVERGENT (match on (anchor_id,content) with different memory_id)
         — REMOVAL/MODIFICATION unreachable per trigger inspection.
      c. for ADDITION and CONVERGENT: insert new episodic_memories under
         target_branch_id (root), new memory_id, created_revision=R.
         CONVERGENT keeps both rows (Section 23).
      d. insert branch_promotions, insert memory_lineage_events PROMOTED,
         update source branch status to PROMOTED — same R.

    inject_failure_after: test-only hook to raise RuntimeError after N new
    canonical inserts, to verify atomic rollback (no partial state).

    Returns dict with counts and new_memory_ids.
    """
    if convergence_policy != "KEEP_BOTH":
        raise ValueError("Only KEEP_BOTH implemented in Phase 0")

    store = MemoryStore(db_path=str(db_path))
    try:
        src = store.conn.execute(
            "SELECT branch_id, repo_id, parent_branch_id, is_root, status, base_revision FROM memory_branches WHERE branch_id=?",
            (source_branch_id,),
        ).fetchone()
        if src is None:
            raise ValueError(f"source branch {source_branch_id} not found")
        if src["is_root"] != 0:
            raise ValueError("cannot promote root branch")
        if src["status"] != "ACTIVE":
            raise ValueError("only ACTIVE branches can be promoted")
        repo_id = src["repo_id"]
        target_branch_id = src["parent_branch_id"]
        if target_branch_id is None:
            raise ValueError("source branch has no parent")
        tgt = store.conn.execute(
            "SELECT branch_id, is_root, repo_id FROM memory_branches WHERE branch_id=?",
            (target_branch_id,),
        ).fetchone()
        if tgt is None or tgt["is_root"] != 1 or tgt["repo_id"] != repo_id:
            raise ValueError("target must be root in same repo")
        result: Dict = {"added": 0, "convergent": 0, "unchanged": 0, "new_memory_ids": [],
                        "embedding_failed": []}
        # Embeddings are deferred until after COMMIT: model inference must not
        # hold the revision transaction's write lock (same rationale as
        # MemoryStore.write_batch). Rows are committed first; a failed embed
        # is reported, never rolled back.
        pending_embeds: List[tuple] = []

        with store.revision_transaction(repo_id) as rt:
            R = rt.revision
            now_ts = _now()
            # Canonical snapshot revision is R-1 (before this transaction's inserts)
            canon_rev = R - 1
            inserts_done = 0

            # Canonical at current (before new inserts) — visible at canon_rev
            canonical_at_current_rows = store.conn.execute(
                "SELECT memory_id, anchor_id, content FROM episodic_memories "
                "WHERE repo_id=? AND branch_id=? AND status='ACTIVE' "
                "AND created_revision <= ? AND (closed_revision IS NULL OR closed_revision > ?)",
                (repo_id, target_branch_id, canon_rev, canon_rev),
            ).fetchall()
            # Map logical identity -> memory_id (first occurrence)
            canon_map: Dict[tuple, int] = {}
            canon_ids = set()
            for r in canonical_at_current_rows:
                key = (r["anchor_id"], r["content"])
                canon_ids.add(r["memory_id"])
                # KEEP_BOTH: keep first mapping; existence of key is what matters for ADDITION vs CONVERGENT
                canon_map.setdefault(key, r["memory_id"])

            # Branch-local ACTIVE records (the experimental effective state's local part)
            # No per-record tombstone to subtract per #1, so take all ACTIVE branch-local
            branch_locals = store.conn.execute(
                "SELECT memory_id, anchor_id, content, category, confidence, importance, tags, created_revision, created_at "
                "FROM episodic_memories WHERE repo_id=? AND branch_id=? AND status='ACTIVE' "
                "AND (closed_revision IS NULL OR closed_revision > ?)",
                (repo_id, source_branch_id, canon_rev),
            ).fetchall()

            for bl in branch_locals:
                key = (bl["anchor_id"], bl["content"])
                # UNCHANGED: same memory_id already in canonical@current (should not happen for branch-local vs canonical, but handle)
                if bl["memory_id"] in canon_ids:
                    result["unchanged"] += 1
                    continue
                if key not in canon_map:
                    # ADDITION
                    new_id = store.conn.execute(
                        "INSERT INTO episodic_memories (repo_id, branch_id, anchor_id, content, category, confidence, status, superseded_by_memory_id, created_revision, closed_revision, created_at, importance, tags) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', NULL, ?, NULL, ?, ?, ?)",
                        (
                            repo_id,
                            target_branch_id,
                            bl["anchor_id"],
                            bl["content"],
                            bl["category"],
                            bl["confidence"],
                            R,
                            now_ts,
                            bl["importance"],
                            bl["tags"],
                        ),
                    ).lastrowid
                    result["added"] += 1
                    result["new_memory_ids"].append(new_id)
                    inserts_done += 1
                    if inject_failure_after is not None and inserts_done >= inject_failure_after:
                        raise RuntimeError("injected failure mid-copy for atomicity test")
                    # episodic embeddings are isolated from memory embeddings (separate ID space)
                    pending_embeds.append((new_id, f"{bl['content']} {bl['category'] or ''} {bl['tags'] or ''}"))
                else:
                    # CONVERGENT: logical identity matches but different memory_id
                    # Under KEEP_BOTH, still insert new row and keep both
                    canon_mid = canon_map[key]
                    if canon_mid == bl["memory_id"]:
                        result["unchanged"] += 1
                        continue
                    new_id = store.conn.execute(
                        "INSERT INTO episodic_memories (repo_id, branch_id, anchor_id, content, category, confidence, status, superseded_by_memory_id, created_revision, closed_revision, created_at, importance, tags) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', NULL, ?, NULL, ?, ?, ?)",
                        (
                            repo_id,
                            target_branch_id,
                            bl["anchor_id"],
                            bl["content"],
                            bl["category"],
                            bl["confidence"],
                            R,
                            now_ts,
                            bl["importance"],
                            bl["tags"],
                        ),
                    ).lastrowid
                    result["convergent"] += 1
                    result["new_memory_ids"].append(new_id)
                    inserts_done += 1
                    if inject_failure_after is not None and inserts_done >= inject_failure_after:
                        raise RuntimeError("injected failure mid-copy for atomicity test")
                    pending_embeds.append((new_id, f"{bl['content']} {bl['category'] or ''} {bl['tags'] or ''}"))

            # Insert promotion ledger
            promo_id = store.conn.execute(
                "INSERT INTO branch_promotions (repo_id, source_branch_id, target_branch_id, convergence_policy, promoted_at, created_revision) VALUES (?, ?, ?, ?, ?, ?)",
                (repo_id, source_branch_id, target_branch_id, convergence_policy, now_ts, R),
            ).lastrowid
            # Insert lineage PROMOTED event
            store.conn.execute(
                "INSERT INTO memory_lineage_events (repo_id, branch_id, event_type, created_revision, occurred_at, related_promotion_id, related_tombstone_id) VALUES (?, ?, 'PROMOTED', ?, ?, ?, NULL)",
                (repo_id, source_branch_id, R, now_ts, promo_id),
            )
            # Mark source branch PROMOTED — must be in same revision as ledger (enforced by trg_branches_promotion_requires_ledger)
            store.conn.execute("UPDATE memory_branches SET status='PROMOTED' WHERE branch_id=?", (source_branch_id,))

        for new_id, text in pending_embeds:
            try:
                store._store_episodic_embedding(new_id, text)
            except Exception as e:
                logger.warning("episodic embedding failed for new_memory_id %s: %r", new_id, e)
                result["embedding_failed"].append(new_id)

        return result
    finally:
        store.close()


def discard_branch(
    db_path: str | Path,
    source_branch_id: int,
    reason: str | None = None,
    inject_failure_after: int | None = None,
) -> Dict:
    """
    Discard an experimental branch — the terminal alternative to promotion.

    Inserts one branch_tombstones row (UNIQUE branch_id) and one
    memory_lineage_events DISCARDED row, then transitions the branch
    status to DISCARDED — all atomically in one RevisionTransaction
    at a single revision R. Enforced by triggers:

      - trg_branches_discard_requires_tombstone: UPDATE to DISCARDED
        requires pre-existing tombstone at same discarded_revision==R.
      - trg_tombstones_revision: discarded_revision==current_revision.
      - trg_lineage_revision: DISCARDED lineage requires related_tombstone_id
        and created_revision==R.
      - trg_branches_terminal_immutable: once DISCARDED, branch is immutable.

    Mirrors promote_branch in discipline: pre-validates source (exists,
    non-root, ACTIVE), then does the three writes in one revision.

    reason: optional human-readable discard rationale (stored in
            branch_tombstones.reason, no CHECK constraint beyond text).
    inject_failure_after: test-only hook — raise RuntimeError after N steps
                          within the transaction to verify atomic rollback
                          (1 == after tombstone insert, 2 == after lineage).

    Returns dict with branch_id, repo_id, tombstone_id, event_id, discarded_revision, discarded_at, reason.
    """
    import sqlite3

    store = MemoryStore(db_path=str(db_path))
    try:
        src = store.conn.execute(
            "SELECT branch_id, repo_id, parent_branch_id, is_root, status FROM memory_branches WHERE branch_id=?",
            (source_branch_id,),
        ).fetchone()
        if src is None:
            raise ValueError(f"source branch {source_branch_id} not found")
        if src["is_root"] != 0:
            raise ValueError("cannot discard root branch")
        if src["status"] != "ACTIVE":
            raise ValueError("only ACTIVE branches can be discarded")
        repo_id = src["repo_id"]

        with store.revision_transaction(repo_id) as rt:
            R = rt.revision
            now_ts = _now()

            # 1. tombstone
            try:
                cur = store.conn.execute(
                    "INSERT INTO branch_tombstones (repo_id, branch_id, discarded_at, discarded_revision, reason) VALUES (?, ?, ?, ?, ?)",
                    (repo_id, source_branch_id, now_ts, R, reason),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(str(e)) from e
            tombstone_id = cur.lastrowid
            if inject_failure_after is not None and inject_failure_after <= 1:
                raise RuntimeError("injected failure after tombstone insert")

            # 2. DISCARDED lineage event
            try:
                cur2 = store.conn.execute(
                    "INSERT INTO memory_lineage_events (repo_id, branch_id, event_type, created_revision, occurred_at, related_promotion_id, related_tombstone_id) VALUES (?, ?, 'DISCARDED', ?, ?, NULL, ?)",
                    (repo_id, source_branch_id, R, now_ts, tombstone_id),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(str(e)) from e
            event_id = cur2.lastrowid
            if inject_failure_after is not None and inject_failure_after <= 2:
                raise RuntimeError("injected failure after discarded lineage insert")

            # 3. mark branch DISCARDED — trigger enforces tombstone existence + revision match
            try:
                store.conn.execute("UPDATE memory_branches SET status='DISCARDED' WHERE branch_id=?", (source_branch_id,))
            except sqlite3.IntegrityError as e:
                raise ValueError(str(e)) from e

        return {
            "branch_id": source_branch_id,
            "repo_id": repo_id,
            "tombstone_id": tombstone_id,
            "event_id": event_id,
            "discarded_revision": R,
            "discarded_at": now_ts,
            "reason": reason,
        }
    finally:
        store.close()


def list_branches(
    db_path: str | Path,
    repo_id: int | None = None,
    status: str | None = None,
) -> Dict:
    """
    Read-only branch listing for a repo.

    Returns branch_id, status, base_revision, created_at, parent info.
    Repo derivation mirrors create_branch/query_hybrid: if repo_id is None,
    derive the single-repo default via SELECT repo_id FROM repositories
    ORDER BY repo_id LIMIT 1 (not unioning all repos). Status filter is
    optional and case-insensitive; when omitted, all statuses are returned.
    No writes, no revision bump — safe for CLI/MCP inspection.

    Returns dict with repo_id, branches (list), count.
    """

    store = MemoryStore(db_path=str(db_path))
    try:
        if repo_id is None:
            repo_id_to_use = store._derive_default_repo_id()
            if repo_id_to_use is None:
                return {"repo_id": None, "branches": [], "count": 0}
        else:
            exists = store.conn.execute("SELECT 1 FROM repositories WHERE repo_id=?", (repo_id,)).fetchone()
            if not exists:
                raise ValueError(f"repo_id {repo_id} does not exist")
            repo_id_to_use = repo_id

        sql = "SELECT branch_id, repo_id, parent_branch_id, is_root, base_revision, base_timestamp, status, created_at FROM memory_branches WHERE repo_id=?"
        params: list = [repo_id_to_use]
        if status is not None:
            s = status.strip().upper()
            if s not in ("ACTIVE", "PROMOTED", "DISCARDED"):
                raise ValueError(f"status must be one of ACTIVE/PROMOTED/DISCARDED, got {status!r}")
            sql += " AND status=?"
            params.append(s)
        sql += " ORDER BY branch_id"
        rows = store.conn.execute(sql, params).fetchall()
        branches = [dict(r) for r in rows]
        return {"repo_id": repo_id_to_use, "branches": branches, "count": len(branches)}
    finally:
        store.close()


def create_branch(db_path: str | Path, repo_id: int | None = None) -> Dict:
    """
    Create a new experimental branch under the canonical root.

    Per Section 18, base_revision must be the canonical root's CURRENT revision
    at fork time, read inside the same RevisionTransaction that performs the INSERT
    so it cannot race with a concurrent canonical write. Per Section 16, the new
    branch is always a direct child of the root (single-level); the trigger
    trg_branches_single_level enforces this.

    repo_id: if provided, the new branch belongs to that repo (validated to exist);
             if None, derive single-repo stopgap via SELECT repo_id FROM repositories
             ORDER BY repo_id LIMIT 1 — same derivation reused verbatim from
             query_hybrid/get_episodic_state. No second version of "find my repo".

    No name/label column exists in memory_branches (8 columns only); blueprint
    Sections 16-19 specify no name. Ship without one for now; branches are
    addressable by branch_id. Adding a column would be scope creep.

    Minimal validation beyond triggers: explicit repo existence pre-check when
    repo_id is passed (clean ValueError, not raw FK), and clean wrapping of
    trigger-raised IntegrityErrors (e.g. no root branch found for repo).

    Returns dict with branch_id, repo_id, parent_branch_id, base_revision, base_timestamp.
    """
    import sqlite3

    store = MemoryStore(db_path=str(db_path))
    try:
        # Derive repo_id if not provided — same single-repo stopgap as query_hybrid
        if repo_id is None:
            repo_id_to_use = store._derive_default_repo_id()
            if repo_id_to_use is None:
                raise ValueError("no repository found to create branch under")
        else:
            # Explicit repo_id: verify it exists (clean ValueError, not FK)
            exists = store.conn.execute("SELECT 1 FROM repositories WHERE repo_id=?", (repo_id,)).fetchone()
            if not exists:
                raise ValueError(f"repo_id {repo_id} does not exist")
            repo_id_to_use = repo_id

        # Find the root branch for this repo (Section 7: exactly one canonical root auto-created)
        root_row = store.conn.execute(
            "SELECT branch_id FROM memory_branches WHERE repo_id=? AND is_root=1", (repo_id_to_use,)
        ).fetchone()
        if root_row is None:
            raise ValueError(f"no root branch found for repo {repo_id_to_use} — Section 7 invariant violated")
        parent_branch_id = root_row[0]

        # Atomic creation: read current_revision inside the same RevisionTransaction as the INSERT
        with store.revision_transaction(repo_id_to_use) as rt:
            R = rt.revision
            now_ts = _now()
            try:
                cur = store.conn.execute(
                    "INSERT INTO memory_branches (repo_id, parent_branch_id, is_root, base_revision, base_timestamp, status, created_at) "
                    "VALUES (?, ?, 0, ?, ?, 'ACTIVE', ?)",
                    (repo_id_to_use, parent_branch_id, R, now_ts, now_ts),
                )
            except sqlite3.IntegrityError as e:
                # Wrap trigger-raised errors as clean ValueErrors (e.g. hierarchy violation)
                raise ValueError(str(e)) from e
            branch_id = cur.lastrowid
            # CREATED lineage event auto-fires via trg_branches_created_lineage (AFTER INSERT),
            # but we verify it exists at the same R in the test.

        return {
            "branch_id": branch_id,
            "repo_id": repo_id_to_use,
            "parent_branch_id": parent_branch_id,
            "base_revision": R,
            "base_timestamp": now_ts,
        }
    finally:
        store.close()
