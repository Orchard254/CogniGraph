"""Database-level append-only guarantees for audit and provenance ledgers."""

from datetime import datetime, timezone
import sqlite3

import pytest

import branch_promotion
from memory_store import MemoryStore


def _now():
    return datetime.now(timezone.utc).isoformat()


def _new_database(tmp_path):
    db_path = tmp_path / "memory.db"
    store = MemoryStore(str(db_path))
    now = _now()
    try:
        repo_id = store.conn.execute(
            "INSERT INTO repositories (root_path, current_revision, created_at) VALUES (?, 1, ?)",
            (str(tmp_path / "project"), now),
        ).lastrowid
        root_id = store.conn.execute(
            "INSERT INTO memory_branches "
            "(repo_id, parent_branch_id, is_root, base_revision, base_timestamp, status, created_at) "
            "VALUES (?, NULL, 1, 1, ?, 'ACTIVE', ?)",
            (repo_id, now, now),
        ).lastrowid
    finally:
        store.close()
    return db_path, repo_id, root_id


def _create_branch(db_path, repo_id):
    return branch_promotion.create_branch(str(db_path), repo_id=repo_id)["branch_id"]


def _insert_memory(db_path, repo_id, branch_id, content):
    store = MemoryStore(str(db_path))
    try:
        with store.revision_transaction(repo_id) as rt:
            cur = store.conn.execute(
                "INSERT INTO episodic_memories "
                "(repo_id, branch_id, anchor_id, content, category, confidence, status, "
                "superseded_by_memory_id, created_revision, closed_revision, created_at, importance, tags) "
                "VALUES (?, ?, NULL, ?, 'decision', 'assumed', 'ACTIVE', NULL, ?, NULL, ?, 3, 'audit-test')",
                (repo_id, branch_id, content, rt.revision, _now()),
            )
            return cur.lastrowid
    finally:
        store.close()


def _rows(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _mutate_or_delete(db_path, table, primary_key, row_id, column, value):
    # The table/column names are a fixed allowlist declared below, not user input.
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="append-only audit ledger"):
            conn.execute(
                f"UPDATE {table} SET {column}=? WHERE {primary_key}=?",
                (value, row_id),
            )
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="append-only audit ledger"):
            conn.execute(f"DELETE FROM {table} WHERE {primary_key}=?", (row_id,))


def test_branch_and_provenance_ledgers_reject_update_and_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(MemoryStore, "_store_episodic_embedding", lambda self, *_args: False)
    db_path, repo_id, root_id = _new_database(tmp_path)

    discard_branch_id = _create_branch(db_path, repo_id)
    promote_branch_id = _create_branch(db_path, repo_id)
    _insert_memory(db_path, repo_id, promote_branch_id, "audit ledger promotion fixture")

    discard_result = branch_promotion.discard_branch(
        str(db_path), discard_branch_id, reason="append-only audit fixture"
    )
    promote_result = branch_promotion.promote_branch(str(db_path), promote_branch_id)

    # Provenance is created only by genesis migration in production; use a valid
    # existing canonical memory to exercise the same table's DB-level contract.
    canonical_memory_id = _rows(
        db_path,
        "SELECT memory_id FROM episodic_memories WHERE repo_id=? AND branch_id=? "
        "AND content='audit ledger promotion fixture'",
        (repo_id, root_id),
    )[0]["memory_id"]
    store = MemoryStore(str(db_path))
    try:
        provenance_id = store.conn.execute(
            "INSERT INTO legacy_provenance "
            "(repo_id, episodic_memory_id, legacy_memory_id, legacy_table, file_path, migrated_at) "
            "VALUES (?, ?, 99999, 'memory', 'src/original.py', ?)",
            (repo_id, canonical_memory_id, _now()),
        ).lastrowid
    finally:
        store.close()

    tombstone_id = discard_result["tombstone_id"]
    promotion_id = promote_result["promotion_id"]
    created_event_id = _rows(
        db_path,
        "SELECT event_id FROM memory_lineage_events WHERE branch_id=? AND event_type='CREATED'",
        (promote_branch_id,),
    )[0]["event_id"]

    cases = [
        (
            "branch_tombstones", "tombstone_id", tombstone_id, "reason",
            "rewritten after discard", "SELECT reason FROM branch_tombstones WHERE tombstone_id=?",
        ),
        (
            "branch_promotions", "promotion_id", promotion_id, "promoted_at",
            "rewritten after promotion", "SELECT promoted_at FROM branch_promotions WHERE promotion_id=?",
        ),
        (
            "memory_lineage_events", "event_id", created_event_id, "occurred_at",
            "rewritten lineage timestamp", "SELECT occurred_at FROM memory_lineage_events WHERE event_id=?",
        ),
        (
            "legacy_provenance", "id", provenance_id, "file_path",
            "rewritten provenance path", "SELECT file_path FROM legacy_provenance WHERE id=?",
        ),
    ]
    before = {}
    for table, pk, row_id, column, value, query in cases:
        before[(table, row_id)] = _rows(db_path, query, (row_id,))[0]
        _mutate_or_delete(db_path, table, pk, row_id, column, value)
        assert _rows(db_path, query, (row_id,))[0] == before[(table, row_id)]

    # The ledger rows remain available and tied to the terminal branch states.
    assert _rows(
        db_path, "SELECT status FROM memory_branches WHERE branch_id=?", (discard_branch_id,)
    )[0]["status"] == "DISCARDED"
    assert _rows(
        db_path, "SELECT status FROM memory_branches WHERE branch_id=?", (promote_branch_id,)
    )[0]["status"] == "PROMOTED"
