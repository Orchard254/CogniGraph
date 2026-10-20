"""Regression coverage for the one-time legacy-to-episodic genesis migration."""

import sqlite3
from datetime import datetime, timezone

import pytest

import memory_store
import pm_backup
from genesis_migrate import genesis_migrate
from memory_store import MemoryStore


_MIGRATED_REASON = "migrated to episodic via genesis"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _legacy_database(tmp_path):
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

        # Seed the active successor first so the closed source row can resolve
        # its legacy superseded_by link during the migration's active-first pass.
        active_id = store.write(
            category="decision",
            content="Current canonical decision",
            confidence="confirmed",
            file_path="src/current.py",
            line_start=4,
            line_end=8,
            code_hash="activehash",
            tags="migration-test",
            force=True,
            embed=False,
        )
        closed_id = store.write(
            category="decision",
            content="Older decision superseded by the current one",
            confidence="confirmed",
            file_path="src/old.py",
            line_start=10,
            line_end=13,
            code_hash="closedhash",
            tags="migration-test",
            force=True,
            embed=False,
        )
        closed_at = _now()
        store.conn.execute(
            "UPDATE memory SET deprecated_at=?, deprecated_reason=?, superseded_by=? WHERE id=?",
            (closed_at, "superseded during test setup", active_id, closed_id),
        )
    finally:
        store.close()

    return db_path, repo_id, root_id, active_id, closed_id, closed_at


def _scalar(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchone()[0]
    finally:
        conn.close()


def _rows(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def _disable_external_work(monkeypatch):
    # The migration contract is data/revision correctness; embedding inference
    # is optional and independently tested. Keep these tests offline and fast.
    monkeypatch.setattr(memory_store, "_encode", lambda _text: None)
    monkeypatch.setattr(
        pm_backup,
        "timestamped_backup",
        lambda *_args, **_kwargs: {"backup_path": "test-backup"},
    )


def test_genesis_migration_preserves_lifecycle_provenance_and_revision_order(
    tmp_path, monkeypatch
):
    _disable_external_work(monkeypatch)
    db_path, repo_id, root_id, active_id, closed_id, original_closed_at = _legacy_database(tmp_path)

    result = genesis_migrate(db_path)

    assert result["status"] == "migrated"
    assert result["active_migrated"] == 1
    assert result["closed_migrated"] == 1
    assert result["total"] == 2
    assert set(result["old_to_new"]) == {active_id, closed_id}

    active_new_id = result["old_to_new"][active_id]
    closed_new_id = result["old_to_new"][closed_id]
    active = _rows(
        db_path, "SELECT * FROM episodic_memories WHERE memory_id=?", (active_new_id,)
    )[0]
    closed = _rows(
        db_path, "SELECT * FROM episodic_memories WHERE memory_id=?", (closed_new_id,)
    )[0]
    assert active["repo_id"] == closed["repo_id"] == repo_id
    assert active["branch_id"] == closed["branch_id"] == root_id
    assert active["content"] == "Current canonical decision"
    assert active["status"] == "ACTIVE"
    assert active["created_revision"] == result["new_revision"]

    assert closed["content"] == "Older decision superseded by the current one"
    assert closed["status"] == "SUPERSEDED"
    assert closed["superseded_by_memory_id"] == active_new_id
    assert closed["created_revision"] == result["new_revision"]
    assert closed["closed_revision"] == result["new_revision"] + 1

    provenance = _rows(
        db_path,
        "SELECT * FROM legacy_provenance ORDER BY legacy_memory_id",
    )
    assert len(provenance) == 2
    by_legacy_id = {row["legacy_memory_id"]: row for row in provenance}
    assert by_legacy_id[active_id]["episodic_memory_id"] == active_new_id
    assert by_legacy_id[closed_id]["episodic_memory_id"] == closed_new_id
    assert by_legacy_id[active_id]["file_path"] == "src/current.py"
    assert by_legacy_id[closed_id]["file_path"] == "src/old.py"

    legacy_rows = _rows(db_path, "SELECT * FROM memory ORDER BY id")
    assert len(legacy_rows) == 2
    assert all(row["deprecated_reason"] == _MIGRATED_REASON for row in legacy_rows)
    assert all(row["deprecated_at"] is not None for row in legacy_rows)
    assert legacy_rows[1]["deprecated_at"] != original_closed_at

    # Closed-history rows need the additional counter step; the counter must
    # never trail a created_revision/closed_revision reference in canonical data.
    current_revision = _scalar(
        db_path, "SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)
    )
    assert current_revision == result["new_revision"] + 1
    assert current_revision >= _scalar(
        db_path,
        "SELECT max(max_created, max_closed) FROM "
        "(SELECT max(created_revision) AS max_created, max(closed_revision) AS max_closed "
        " FROM episodic_memories WHERE repo_id=?)",
        (repo_id,),
    )


@pytest.mark.parametrize("inject_after", [1, 2])
def test_genesis_failure_rolls_back_data_revision_and_trigger_ddl(
    tmp_path, monkeypatch, inject_after
):
    _disable_external_work(monkeypatch)
    db_path, repo_id, _root_id, active_id, closed_id, closed_at = _legacy_database(tmp_path)

    with pytest.raises(RuntimeError, match="injected failure mid-migration"):
        genesis_migrate(db_path, inject_failure_after=inject_after)

    # No canonical rows or provenance may survive a failed one-transaction import.
    assert _scalar(
        db_path, "SELECT count(*) FROM episodic_memories WHERE repo_id=?", (repo_id,)
    ) == 0
    assert _scalar(db_path, "SELECT count(*) FROM legacy_provenance") == 0
    assert _scalar(
        db_path, "SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)
    ) == 1

    rows = _rows(db_path, "SELECT * FROM memory ORDER BY id")
    assert len(rows) == 2
    active = next(row for row in rows if row["id"] == active_id)
    closed = next(row for row in rows if row["id"] == closed_id)
    assert active["deprecated_at"] is None
    assert active["deprecated_reason"] is None
    assert closed["deprecated_at"] == closed_at
    assert closed["deprecated_reason"] == "superseded during test setup"
    assert closed["superseded_by"] == active_id

    # The migration temporarily drops these triggers for already-closed rows.
    # Injecting failure in pass 2 proves SQLite rollback restores the DDL too.
    trigger_names = {
        row["name"]
        for row in _rows(
            db_path,
            "SELECT name FROM sqlite_master WHERE type='trigger'",
        )
    }
    assert "trg_episodic_insert_revision" in trigger_names
    assert "trg_episodic_update_revision" in trigger_names


def test_genesis_migration_refuses_a_second_execution_without_mutation(tmp_path, monkeypatch):
    _disable_external_work(monkeypatch)
    db_path, repo_id, _root_id, _active_id, _closed_id, _closed_at = _legacy_database(tmp_path)
    genesis_migrate(db_path)

    before_revision = _scalar(
        db_path, "SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)
    )
    before_episodic = _scalar(db_path, "SELECT count(*) FROM episodic_memories")
    before_provenance = _scalar(db_path, "SELECT count(*) FROM legacy_provenance")

    with pytest.raises(ValueError, match="genesis already executed"):
        genesis_migrate(db_path)

    assert _scalar(
        db_path, "SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)
    ) == before_revision
    assert _scalar(db_path, "SELECT count(*) FROM episodic_memories") == before_episodic
    assert _scalar(db_path, "SELECT count(*) FROM legacy_provenance") == before_provenance
