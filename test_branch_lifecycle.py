"""Adversarial tests for the canonical experimental-branch lifecycle.

These exercise the real SQLite triggers and RevisionTransaction boundaries,
not only CLI dispatch mocks.
"""
from datetime import datetime, timezone
import sqlite3

import pytest

import branch_promotion
from memory_store import MemoryStore


def _now():
    return datetime.now(timezone.utc).isoformat()


def _new_database(tmp_path, repository_count=1):
    db_path = tmp_path / "memory.db"
    store = MemoryStore(str(db_path))
    repository_ids = []
    root_ids = []
    try:
        for index in range(repository_count):
            now = _now()
            repo_id = store.conn.execute(
                "INSERT INTO repositories (root_path, current_revision, created_at) "
                "VALUES (?, 1, ?)",
                (str(tmp_path / f"repository-{index}"), now),
            ).lastrowid
            root_id = store.conn.execute(
                "INSERT INTO memory_branches "
                "(repo_id, parent_branch_id, is_root, base_revision, base_timestamp, status, created_at) "
                "VALUES (?, NULL, 1, 1, ?, 'ACTIVE', ?)",
                (repo_id, now, now),
            ).lastrowid
            repository_ids.append(repo_id)
            root_ids.append(root_id)
    finally:
        store.close()
    if repository_count == 1:
        return db_path, repository_ids[0], root_ids[0]
    return db_path, repository_ids, root_ids


def _insert_memory(db_path, repo_id, branch_id, content, anchor_id=None):
    store = MemoryStore(str(db_path))
    try:
        with store.revision_transaction(repo_id) as rt:
            cur = store.conn.execute(
                "INSERT INTO episodic_memories "
                "(repo_id, branch_id, anchor_id, content, category, confidence, status, "
                "superseded_by_memory_id, created_revision, closed_revision, created_at, importance, tags) "
                "VALUES (?, ?, ?, ?, 'decision', 'assumed', 'ACTIVE', NULL, ?, NULL, ?, 3, 'branch-test')",
                (repo_id, branch_id, anchor_id, content, rt.revision, _now()),
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


def _scalar(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _create_experiment(db_path, repo_id):
    return branch_promotion.create_branch(str(db_path), repo_id=repo_id)["branch_id"]


def test_create_branch_records_its_snapshot_and_created_lineage(tmp_path):
    db_path, repo_id, root_id = _new_database(tmp_path)

    created = branch_promotion.create_branch(str(db_path), repo_id=repo_id)

    assert created["parent_branch_id"] == root_id
    assert created["base_revision"] == _scalar(
        db_path, "SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)
    )
    branch = _rows(
        db_path, "SELECT * FROM memory_branches WHERE branch_id=?", (created["branch_id"],)
    )[0]
    assert branch["status"] == "ACTIVE"
    assert branch["is_root"] == 0
    assert branch["repo_id"] == repo_id

    event = _rows(
        db_path,
        "SELECT * FROM memory_lineage_events WHERE branch_id=? AND event_type='CREATED'",
        (created["branch_id"],),
    )[0]
    assert event["created_revision"] == created["base_revision"]
    assert event["related_promotion_id"] is None
    assert event["related_tombstone_id"] is None


@pytest.mark.parametrize("inject_after", [1, 2])
def test_discard_failure_rolls_back_tombstone_lineage_status_and_revision(
    tmp_path, inject_after
):
    db_path, repo_id, _ = _new_database(tmp_path)
    branch_id = _create_experiment(db_path, repo_id)
    revision_before = _scalar(
        db_path, "SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)
    )

    with pytest.raises(RuntimeError, match="injected failure"):
        branch_promotion.discard_branch(
            str(db_path), branch_id, reason="rollback probe", inject_failure_after=inject_after
        )

    assert _scalar(
        db_path, "SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)
    ) == revision_before
    assert _scalar(
        db_path, "SELECT status FROM memory_branches WHERE branch_id=?", (branch_id,)
    ) == "ACTIVE"
    assert _scalar(
        db_path, "SELECT count(*) FROM branch_tombstones WHERE branch_id=?", (branch_id,)
    ) == 0
    assert _scalar(
        db_path,
        "SELECT count(*) FROM memory_lineage_events WHERE branch_id=? AND event_type='DISCARDED'",
        (branch_id,),
    ) == 0


def test_discard_commits_matching_tombstone_lineage_and_terminal_status(tmp_path):
    db_path, repo_id, _ = _new_database(tmp_path)
    branch_id = _create_experiment(db_path, repo_id)

    result = branch_promotion.discard_branch(
        str(db_path), branch_id, reason="experiment no longer needed"
    )

    branch = _rows(
        db_path, "SELECT status FROM memory_branches WHERE branch_id=?", (branch_id,)
    )[0]
    tombstone = _rows(
        db_path, "SELECT * FROM branch_tombstones WHERE branch_id=?", (branch_id,)
    )[0]
    event = _rows(
        db_path,
        "SELECT * FROM memory_lineage_events WHERE branch_id=? AND event_type='DISCARDED'",
        (branch_id,),
    )[0]
    current_revision = _scalar(
        db_path, "SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)
    )

    assert result["discarded_revision"] == current_revision
    assert branch["status"] == "DISCARDED"
    assert tombstone["discarded_revision"] == event["created_revision"] == current_revision
    assert event["related_tombstone_id"] == tombstone["tombstone_id"]
    assert tombstone["reason"] == "experiment no longer needed"


def test_promotion_copies_addition_and_convergent_state_with_ledger(tmp_path, monkeypatch):
    # Embedding inference is deliberately out of scope for this transaction test.
    monkeypatch.setattr(MemoryStore, "_store_episodic_embedding", lambda self, *_args: False)
    db_path, repo_id, root_id = _new_database(tmp_path)
    _insert_memory(db_path, repo_id, root_id, "canonical memory shared with experiment")
    branch_id = _create_experiment(db_path, repo_id)
    _insert_memory(db_path, repo_id, branch_id, "new experimental finding")
    _insert_memory(db_path, repo_id, branch_id, "canonical memory shared with experiment")

    result = branch_promotion.promote_branch(str(db_path), branch_id)

    assert result["added"] == 1
    assert result["convergent"] == 1
    assert result["unchanged"] == 0
    assert _scalar(
        db_path,
        "SELECT count(*) FROM episodic_memories WHERE repo_id=? AND branch_id=? "
        "AND status='ACTIVE' AND content='new experimental finding'",
        (repo_id, root_id),
    ) == 1
    # KEEP_BOTH preserves the pre-existing canonical row and copies the convergent
    # branch row with a new memory_id rather than silently coalescing the records.
    assert _scalar(
        db_path,
        "SELECT count(*) FROM episodic_memories WHERE repo_id=? AND branch_id=? "
        "AND status='ACTIVE' AND content='canonical memory shared with experiment'",
        (repo_id, root_id),
    ) == 2
    assert _scalar(
        db_path, "SELECT status FROM memory_branches WHERE branch_id=?", (branch_id,)
    ) == "PROMOTED"

    promotion = _rows(
        db_path, "SELECT * FROM branch_promotions WHERE source_branch_id=?", (branch_id,)
    )[0]
    event = _rows(
        db_path,
        "SELECT * FROM memory_lineage_events WHERE branch_id=? AND event_type='PROMOTED'",
        (branch_id,),
    )[0]
    assert promotion["created_revision"] == event["created_revision"]
    assert event["related_promotion_id"] == promotion["promotion_id"]


def test_promotion_injected_failure_rolls_back_copies_ledger_and_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(MemoryStore, "_store_episodic_embedding", lambda self, *_args: False)
    db_path, repo_id, root_id = _new_database(tmp_path)
    branch_id = _create_experiment(db_path, repo_id)
    _insert_memory(db_path, repo_id, branch_id, "rollback only finding")
    revision_before = _scalar(
        db_path, "SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)
    )
    canonical_count_before = _scalar(
        db_path,
        "SELECT count(*) FROM episodic_memories WHERE repo_id=? AND branch_id=?",
        (repo_id, root_id),
    )

    with pytest.raises(RuntimeError, match="injected failure mid-copy"):
        branch_promotion.promote_branch(
            str(db_path), branch_id, inject_failure_after=1
        )

    assert _scalar(
        db_path, "SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)
    ) == revision_before
    assert _scalar(
        db_path, "SELECT status FROM memory_branches WHERE branch_id=?", (branch_id,)
    ) == "ACTIVE"
    assert _scalar(
        db_path, "SELECT count(*) FROM branch_promotions WHERE source_branch_id=?", (branch_id,)
    ) == 0
    assert _scalar(
        db_path,
        "SELECT count(*) FROM memory_lineage_events WHERE branch_id=? AND event_type='PROMOTED'",
        (branch_id,),
    ) == 0
    assert _scalar(
        db_path,
        "SELECT count(*) FROM episodic_memories WHERE repo_id=? AND branch_id=?",
        (repo_id, root_id),
    ) == canonical_count_before


@pytest.mark.parametrize(
    ("column", "value_index"),
    [
        ("repo_id", "other_repo"),
        ("parent_branch_id", "other_root"),
        ("is_root", 1),
        ("base_revision", "increment"),
        ("base_timestamp", "rewrite"),
        ("created_at", "rewrite"),
    ],
)
def test_branch_identity_and_fork_snapshot_are_immutable(tmp_path, column, value_index):
    db_path, repo_ids, root_ids = _new_database(tmp_path, repository_count=2)
    branch_id = _create_experiment(db_path, repo_ids[0])
    row = _rows(
        db_path, "SELECT * FROM memory_branches WHERE branch_id=?", (branch_id,)
    )[0]

    if value_index == "other_repo":
        value = repo_ids[1]
    elif value_index == "other_root":
        value = root_ids[1]
    elif value_index == "increment":
        value = row["base_revision"] + 1
    else:
        value = "rewritten-snapshot"

    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="branch identity is immutable"):
            conn.execute(
                f"UPDATE memory_branches SET {column}=? WHERE branch_id=?",
                (value, branch_id),
            )
    finally:
        conn.close()

    after = _rows(
        db_path, "SELECT * FROM memory_branches WHERE branch_id=?", (branch_id,)
    )[0]
    assert after[column] == row[column]
