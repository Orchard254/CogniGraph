"""Tests for pm_backup timestamped DB backup + retention pruning."""
import sqlite3
from pathlib import Path

import pytest

from pm_backup import timestamped_backup


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY, content TEXT)")
        conn.execute("INSERT INTO memory (content) VALUES ('keep me')")
        conn.commit()
    finally:
        conn.close()


def test_backup_creates_verified_timestamped_copy(tmp_path):
    db = tmp_path / "memory.db"
    _make_db(db)
    res = timestamped_backup(db, tag="test")
    backup = Path(res["backup_path"])
    assert backup.parent == tmp_path
    assert ".backup-test-" in backup.name and backup.name.endswith(".bak")
    assert res["bytes"] > 0 and res["kept"] == 1 and res["pruned"] == []
    conn = sqlite3.connect(str(backup))
    try:
        assert conn.execute("SELECT content FROM memory").fetchone()[0] == "keep me"
    finally:
        conn.close()


def test_backup_tag_sanitized(tmp_path):
    db = tmp_path / "memory.db"
    _make_db(db)
    res = timestamped_backup(db, tag="pre corpus:hygiene")
    assert ".backup-pre-corpus-hygiene-" in Path(res["backup_path"]).name


def test_backup_refuses_missing_or_empty(tmp_path):
    with pytest.raises(ValueError):
        timestamped_backup(tmp_path / "missing.db")
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        timestamped_backup(empty)


def test_backup_prunes_beyond_keep(tmp_path):
    db = tmp_path / "memory.db"
    _make_db(db)
    # Seed 8 older backups with ascending mtimes; glob alone cannot order them.
    for i in range(8):
        f = tmp_path / f"memory.db.backup-routine-20260101-00000{i}.bak"
        f.write_bytes(b"x")
        import os
        os.utime(f, (1000 + i, 1000 + i))
    res = timestamped_backup(db, tag="test", keep=8)
    assert res["kept"] == 8 and len(res["pruned"]) == 1
    survivors = sorted(p.name for p in tmp_path.glob("memory.db.backup-*"))
    assert len(survivors) == 8
    assert Path(res["pruned"][0]).name == "memory.db.backup-routine-20260101-000000.bak"
    # Historical ad-hoc backups outside the routine pattern are never touched.
    legacy = tmp_path / "memory.db.pre-phase0-backup-20260823-224103.bak"
    legacy.write_bytes(b"x")
    timestamped_backup(db, tag="test", keep=2)
    assert legacy.exists()


def test_backup_prune_removes_sidecars_with_parent(tmp_path):
    import os
    db = tmp_path / "memory.db"
    _make_db(db)
    # Two old snapshots, each with -shm/-wal siblings; old0 is prunable.
    old0 = tmp_path / "memory.db.backup-routine-20260101-000000.bak"
    old1 = tmp_path / "memory.db.backup-routine-20260101-000001.bak"
    for f in (old0, old1):
        f.write_bytes(b"x")
        (tmp_path / (f.name + "-shm")).write_bytes(b"s")
        (tmp_path / (f.name + "-wal")).write_bytes(b"")
    # A dangling sidecar whose parent .bak is long gone (legacy orphan).
    (tmp_path / "memory.db.backup-routine-20250101-000000.bak-wal").write_bytes(b"")
    os.utime(old0, (1000, 1000))
    os.utime(old1, (2000, 2000))
    res = timestamped_backup(db, tag="test", keep=2)
    pruned_names = {Path(p).name for p in res["pruned"]}
    assert old0.name in pruned_names
    assert not old0.exists()
    assert not (tmp_path / (old0.name + "-shm")).exists()
    assert not (tmp_path / (old0.name + "-wal")).exists()
    # The kept snapshot and the dangling orphan are handled correctly.
    assert old1.exists()
    assert not (tmp_path / "memory.db.backup-routine-20250101-000000.bak-wal").exists()
    # Fresh backups are stored sidecar-free.
    fresh = Path(res["backup_path"])
    assert not (tmp_path / (fresh.name + "-shm")).exists()
    assert not (tmp_path / (fresh.name + "-wal")).exists()


def test_corpus_hygiene_apply_backs_up_first(tmp_path):
    from corpus_hygiene import corpus_hygiene
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE memory (id INTEGER PRIMARY KEY, category TEXT, content TEXT, "
            "deprecated_at TEXT, deprecated_reason TEXT, file_path TEXT)")
        conn.execute(
            "INSERT INTO memory (category, content, file_path) VALUES "
            "('decision', 'Source file: x.py (python, 10 lines, hash abc)', 'x.py')")
        conn.commit()
    finally:
        conn.close()
    res = corpus_hygiene(db, apply=True)
    assert len(res["deprecate"]) == 1
    backup = Path(res["backup"]["backup_path"])
    assert backup.exists() and ".backup-pre-corpus-hygiene-" in backup.name