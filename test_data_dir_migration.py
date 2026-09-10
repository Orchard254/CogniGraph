"""Hermetic tests for the Phase 1.9 data-dir migration (.project-memory -> .cognigraph).

All cases run under pytest's tmp_path: a real (minimal) SQLite DB is built
with sqlite3 directly, so no embedding model, no git, and no writes outside
tmp_path. Covers the contract from the rebrand spec:

- old dir + real DB -> migrate -> new path, data intact
- second run is a no-op (idempotent)
- both dirs exist -> use .cognigraph/, warn about the orphan, old untouched
- corrupt DB -> move back, keep using the old path, log to stderr, never delete
- ensure_startup_migration rewrites a legacy MEMORY_DB_PATH to the new path
- default_db_path prefers .cognigraph but still resolves a legacy-only tree
"""

import os
import sqlite3

import pytest

import pm_common
from pm_common import (
    DATA_DIR_NAME,
    LEGACY_DATA_DIR_NAME,
    default_db_path,
    ensure_startup_migration,
    maybe_migrate_legacy_data_dir,
)


@pytest.fixture
def _no_latch():
    """Reset the once-per-process migration latch around each test."""
    pm_common._migration_done = False
    try:
        yield
    finally:
        pm_common._migration_done = False


def _make_legacy_db(root, content="hello cognigraph"):
    """Build a legacy tree with a real-schema SQLite DB holding one row."""
    from pm_common import SCHEMA

    legacy = root / LEGACY_DATA_DIR_NAME
    legacy.mkdir(parents=True)
    db = legacy / "memory.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO memory (category, content, created_at) "
            "VALUES ('other', ?, datetime('now'))",
            (content,),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _read_contents(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[0] for r in conn.execute("SELECT content FROM memory ORDER BY id")]
    finally:
        conn.close()


def test_migrate_moves_dir_and_keeps_data(tmp_path):
    db = _make_legacy_db(tmp_path)
    assert _read_contents(db) == ["hello cognigraph"]

    active = maybe_migrate_legacy_data_dir(tmp_path)

    assert active == tmp_path / DATA_DIR_NAME
    assert (tmp_path / DATA_DIR_NAME / "memory.db").is_file()
    assert not (tmp_path / LEGACY_DATA_DIR_NAME).exists()
    assert _read_contents(active / "memory.db") == ["hello cognigraph"]


def test_migrate_opens_through_real_store(tmp_path):
    """The migrated DB must be usable by the real MemoryStore (schema upgrade path)."""
    from memory_store import MemoryStore

    _make_legacy_db(tmp_path)
    active = maybe_migrate_legacy_data_dir(tmp_path)

    store = MemoryStore(str(active / "memory.db"))
    try:
        assert store.stats()["db_path"] == str(active / "memory.db")
    finally:
        store.close()


def test_second_run_is_noop(tmp_path, capsys):
    _make_legacy_db(tmp_path)
    first = maybe_migrate_legacy_data_dir(tmp_path)
    capsys.readouterr()  # drain first-run output (none expected, but be strict only on rerun)

    second = maybe_migrate_legacy_data_dir(tmp_path)

    assert second == first == tmp_path / DATA_DIR_NAME
    assert capsys.readouterr().err == ""
    assert _read_contents(second / "memory.db") == ["hello cognigraph"]


def test_both_exist_uses_new_and_warns(tmp_path, capsys):
    new_db = tmp_path / DATA_DIR_NAME / "memory.db"
    new_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(new_db))
    conn.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO memory (content) VALUES ('new')")
    conn.commit()
    conn.close()
    old_db = _make_legacy_db(tmp_path, content="old-orphan")

    active = maybe_migrate_legacy_data_dir(tmp_path)

    assert active == tmp_path / DATA_DIR_NAME
    assert "orphan" in capsys.readouterr().err
    # Legacy orphan left untouched — never deleted, never overwritten.
    assert old_db.is_file()
    assert _read_contents(old_db) == ["old-orphan"]
    assert _read_contents(new_db) == ["new"]


def test_corrupt_db_rolls_back_and_keeps_old_path(tmp_path, capsys):
    legacy = tmp_path / LEGACY_DATA_DIR_NAME
    legacy.mkdir(parents=True)
    (legacy / "memory.db").write_bytes(b"this is not a sqlite database")

    active = maybe_migrate_legacy_data_dir(tmp_path)

    assert active == tmp_path / LEGACY_DATA_DIR_NAME
    assert (tmp_path / LEGACY_DATA_DIR_NAME / "memory.db").is_file()
    assert not (tmp_path / DATA_DIR_NAME).exists()
    assert "rolled back" in capsys.readouterr().err


def test_empty_legacy_dir_migrates_without_db(tmp_path):
    (tmp_path / LEGACY_DATA_DIR_NAME).mkdir(parents=True)

    active = maybe_migrate_legacy_data_dir(tmp_path)

    assert active == tmp_path / DATA_DIR_NAME
    assert (tmp_path / DATA_DIR_NAME).is_dir()
    assert not (tmp_path / LEGACY_DATA_DIR_NAME).exists()


def test_neither_dir_returns_new_without_writing(tmp_path, capsys):
    active = maybe_migrate_legacy_data_dir(tmp_path)

    assert active == tmp_path / DATA_DIR_NAME
    assert not (tmp_path / DATA_DIR_NAME).exists()  # pure resolution, no mkdir
    assert capsys.readouterr().err == ""


def test_startup_migration_rewrites_env_override(tmp_path, monkeypatch, _no_latch):
    legacy_db = _make_legacy_db(tmp_path)
    monkeypatch.setenv("MEMORY_DB_PATH", str(legacy_db))

    ensure_startup_migration(include_cwd=False)

    assert os.environ["MEMORY_DB_PATH"] == str(tmp_path / DATA_DIR_NAME / "memory.db")
    assert _read_contents(tmp_path / DATA_DIR_NAME / "memory.db") == ["hello cognigraph"]


def test_startup_migration_runs_once_per_process(tmp_path, monkeypatch, _no_latch):
    _make_legacy_db(tmp_path)
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / LEGACY_DATA_DIR_NAME / "memory.db"))

    ensure_startup_migration(include_cwd=False)
    assert os.environ["MEMORY_DB_PATH"].endswith(f"{DATA_DIR_NAME}/memory.db".replace("/", os.sep))

    # Second call is latched: even a fresh legacy tree is left alone.
    proj2 = tmp_path / "proj2"
    proj2.mkdir()
    _make_legacy_db(proj2)
    ensure_startup_migration(hints=[str(proj2)], include_cwd=False, include_env=False)
    assert (proj2 / LEGACY_DATA_DIR_NAME).is_dir()
    assert not (proj2 / DATA_DIR_NAME).exists()


def test_default_db_path_prefers_new_over_legacy(tmp_path, monkeypatch):
    new_db = tmp_path / DATA_DIR_NAME / "memory.db"
    new_db.parent.mkdir(parents=True)
    new_db.write_bytes(b"")
    _make_legacy_db(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEMORY_DB_PATH", raising=False)

    assert default_db_path() == str(new_db)


def test_default_db_path_resolves_legacy_only_tree(tmp_path, monkeypatch):
    legacy_db = _make_legacy_db(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEMORY_DB_PATH", raising=False)

    # Read-only resolution: legacy path returned, nothing renamed.
    assert default_db_path() == str(legacy_db)
    assert (tmp_path / LEGACY_DATA_DIR_NAME).is_dir()
    assert not (tmp_path / DATA_DIR_NAME).exists()
