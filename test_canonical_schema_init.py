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
    "trg_episodic_insert_revision",
    "trg_episodic_update_revision",
    "trg_episodic_repo_isolation",
    "trg_dependencies_insert_revision",
    "trg_dependencies_repo_isolation",
    "trg_branches_single_level",
    "trg_branches_promotion_requires_ledger",
    "trg_branches_discard_requires_tombstone",
    "trg_promotions_branch_roles",
    "trg_tombstones_repo_isolation",
    "trg_lineage_repo_isolation",
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
