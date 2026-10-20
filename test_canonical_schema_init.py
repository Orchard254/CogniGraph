"""Regression tests for repeatable, recoverable canonical-schema initialization."""

import sqlite3
from pathlib import Path

import memory_store
from memory_store import MemoryStore


ROOT = Path(__file__).resolve().parent

_REQUIRED_TABLES = {
    "repositories",
    "memory_branches",
    "entities",
    "anchors",
    "observations",
    "episodic_memories",
    "episodic_embeddings",
    "memory_dependencies",
    "branch_tombstones",
    "branch_promotions",
    "memory_lineage_events",
    "episodic_fts",
    "legacy_provenance",
}

_REQUIRED_GUARDRAILS = {
    "trg_observations_insert_revision",
    "trg_observations_update_revision",
    "trg_observations_repo_isolation",
    "trg_repositories_revision_monotonic",
    "trg_anchors_immutable_delete",
    "trg_observations_immutable_delete",
    "trg_episodic_insert_revision",
    "trg_episodic_update_revision",
    "trg_episodic_repo_isolation",
    "trg_dependencies_insert_revision",
    "trg_dependencies_repo_isolation",
    "trg_episodic_immutable_delete",
    "trg_dependencies_immutable_update",
    "trg_dependencies_immutable_delete",
    "trg_branches_single_level",
    "trg_branches_identity_immutable",
    "trg_branches_promotion_requires_ledger",
    "trg_branches_discard_requires_tombstone",
    "trg_promotions_branch_roles",
    "trg_tombstones_repo_isolation",
    "trg_lineage_repo_isolation",
    "trg_tombstones_immutable_update",
    "trg_tombstones_immutable_delete",
    "trg_promotions_immutable_update",
    "trg_promotions_immutable_delete",
    "trg_lineage_immutable_update",
    "trg_lineage_immutable_delete",
    "trg_legacy_provenance_immutable_update",
    "trg_legacy_provenance_immutable_delete",
}


def _schema_objects(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')"
    ).fetchall()
    return {row["name"] for row in rows}


def _assert_canonical_schema_complete(store):
    objects = _schema_objects(store)
    assert _REQUIRED_TABLES <= objects, (
        f"missing canonical tables: {sorted(_REQUIRED_TABLES - objects)}"
    )
    assert _REQUIRED_GUARDRAILS <= objects, (
        f"missing canonical integrity triggers: {sorted(_REQUIRED_GUARDRAILS - objects)}"
    )


def _seed_previously_partial_schema(db_path):
    """Reproduce the on-disk state left by a startup that stopped at vec0 DDL."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        for filename in ("phase0_schema.sql", "phase1_episodic.sql"):
            conn.executescript((ROOT / filename).read_text(encoding="utf-8"))
        # phase1_episodic_embeddings.sql creates this table before it attempts
        # CREATE VIRTUAL TABLE ... USING vec0. The old loader could fail there,
        # leaving this table and Phase 0/1 objects but omitting all later scripts.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS episodic_embeddings (
                memory_id INTEGER PRIMARY KEY
                    REFERENCES episodic_memories(memory_id) ON DELETE CASCADE,
                model TEXT NOT NULL,
                embedding BLOB NOT NULL
            )
            """
        )
    finally:
        conn.close()


def test_fresh_install_without_sqlite_vec_creates_the_complete_core_schema(
    tmp_path, monkeypatch
):
    """Missing the optional vec0 extension must not abort later schema files."""
    monkeypatch.setattr(memory_store, "_SQLITE_VEC_PKG_AVAILABLE", False)
    db_path = tmp_path / "fresh.db"

    store = MemoryStore(str(db_path))
    try:
        _assert_canonical_schema_complete(store)
        objects = _schema_objects(store)
        assert "episodic_fts" in objects
        assert "trg_branches_promotion_requires_ledger" in objects
    finally:
        store.close()


def test_startup_repairs_a_partial_schema_created_by_the_old_loader(
    tmp_path, monkeypatch
):
    """Existing repositories/episodic tables must not mask missing Phase 2 DDL."""
    monkeypatch.setattr(memory_store, "_SQLITE_VEC_PKG_AVAILABLE", False)
    db_path = tmp_path / "partial.db"
    _seed_previously_partial_schema(db_path)

    store = MemoryStore(str(db_path))
    try:
        _assert_canonical_schema_complete(store)
    finally:
        store.close()

    # The repaired DB must remain safely reopenable, not just pass on first run.
    reopened = MemoryStore(str(db_path))
    try:
        _assert_canonical_schema_complete(reopened)
    finally:
        reopened.close()


def test_schema_repair_replaces_existing_null_unsafe_trigger_bodies(tmp_path, monkeypatch):
    """IF NOT EXISTS alone cannot upgrade an existing trigger's SQL body."""
    monkeypatch.setattr(memory_store, "_SQLITE_VEC_PKG_AVAILABLE", False)
    db_path = tmp_path / "old-trigger-definitions.db"
    _seed_previously_partial_schema(db_path)

    # Simulate an older install whose same-named triggers used != on nullable
    # fields. The partial database also lacks new Phase 2/provenance guardrails,
    # which should cause startup repair to run.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TRIGGER trg_observations_update_revision")
        conn.execute(
            """
            CREATE TRIGGER trg_observations_update_revision
            BEFORE UPDATE ON observations
            BEGIN
                SELECT CASE WHEN NEW.ast_symbol != OLD.ast_symbol
                    THEN RAISE(ABORT, 'old observation trigger') END;
            END
            """
        )
        conn.execute("DROP TRIGGER trg_episodic_update_revision")
        conn.execute(
            """
            CREATE TRIGGER trg_episodic_update_revision
            BEFORE UPDATE ON episodic_memories
            BEGIN
                SELECT CASE WHEN NEW.anchor_id != OLD.anchor_id
                    THEN RAISE(ABORT, 'old episodic trigger') END;
            END
            """
        )
    finally:
        conn.close()

    store = MemoryStore(str(db_path))
    try:
        objects = {
            row["name"]: row["sql"]
            for row in store.conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        assert "NEW.ast_symbol IS NOT OLD.ast_symbol" in objects[
            "trg_observations_update_revision"
        ]
        assert "old observation trigger" not in objects[
            "trg_observations_update_revision"
        ]
        assert "NEW.anchor_id IS NOT OLD.anchor_id" in objects[
            "trg_episodic_update_revision"
        ]
        assert "NEW.tags IS NOT OLD.tags" in objects[
            "trg_episodic_update_revision"
        ]
        assert "old episodic trigger" not in objects[
            "trg_episodic_update_revision"
        ]
        _assert_canonical_schema_complete(store)
    finally:
        store.close()

def test_repository_revision_must_advance_exactly_one_step(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "_SQLITE_VEC_PKG_AVAILABLE", False)
    store = MemoryStore(str(tmp_path / "revision-monotonicity.db"))
    now = "2026-10-20T00:00:00+00:00"
    try:
        repo_id = store.conn.execute(
            "INSERT INTO repositories (root_path, current_revision, created_at) "
            "VALUES (?, 1, ?)",
            (str(tmp_path / "monotonic-repo"), now),
        ).lastrowid

        # A legitimate next revision is permitted.
        store.conn.execute(
            "UPDATE repositories SET current_revision=2 WHERE repo_id=?",
            (repo_id,),
        )
        assert store.conn.execute(
            "SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)
        ).fetchone()[0] == 2

        # Neither rollback/no-op nor skipping revisions may rewrite the cursor.
        for invalid_revision in (1, 2, 4, 100):
            with pytest.raises(
                sqlite3.IntegrityError,
                match="current_revision must advance by exactly one",
            ):
                store.conn.execute(
                    "UPDATE repositories SET current_revision=? WHERE repo_id=?",
                    (invalid_revision, repo_id),
                )
        assert store.conn.execute(
            "SELECT current_revision FROM repositories WHERE repo_id=?", (repo_id,)
        ).fetchone()[0] == 2
    finally:
        store.close()

