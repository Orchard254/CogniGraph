"""Isolated regression tests for MEMORY_AUTO_BACKUP gating routine startup backups."""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bootstrap
import pm_backup
import server


class FakeStore:
    def __init__(self, db_path):
        self.db_path = db_path
        self.scan_calls = 0
        self.backfill_calls = 0
        self.optimize_calls = 0
        self.closed_calls = 0

    def close(self):
        self.closed_calls += 1

    def scan_provenance(self, *args, **kwargs):
        self.scan_calls += 1

    def backfill_embeddings(self, *args, **kwargs):
        self.backfill_calls += 1

    def optimize_db(self, *args, **kwargs):
        self.optimize_calls += 1


def _make_backup_spy(calls):
    def _spy(db_path, tag=None, keep=None):
        calls.append({"db_path": db_path, "tag": tag, "keep": keep})
        return {"backup_path": "synthetic.bak", "kept": 0, "pruned": []}

    return _spy


def _make_db(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY, content TEXT)")
        conn.execute("INSERT INTO memory (content) VALUES ('keep me')")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def patched_maintenance(monkeypatch, tmp_path):
    db_path = str(tmp_path / "auto-maint.db")
    fake = FakeStore(db_path)
    calls = []

    monkeypatch.setattr(server, "_get_store", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr(pm_backup, "timestamped_backup", _make_backup_spy(calls), raising=False)
    monkeypatch.setattr(bootstrap, "bootstrap_project", lambda *a, **k: None, raising=False)

    return fake, calls


def test_memory_auto_backup_disabled_skips_routine_but_maintenance_continues(
    monkeypatch, patched_maintenance
):
    monkeypatch.setenv("MEMORY_AUTO_BACKUP", "0")
    fake, backup_calls = patched_maintenance

    server._auto_maintenance()

    assert backup_calls == []
    assert fake.scan_calls == 1
    assert fake.backfill_calls == 1
    assert fake.optimize_calls == 1
    assert fake.closed_calls == 1


def test_memory_auto_backup_default_enabled_runs_routine(
    monkeypatch, patched_maintenance
):
    monkeypatch.delenv("MEMORY_AUTO_BACKUP", raising=False)
    fake, backup_calls = patched_maintenance

    server._auto_maintenance()

    assert len(backup_calls) == 1
    assert backup_calls[0]["tag"] == "routine"
    assert backup_calls[0]["db_path"] == fake.db_path
    assert fake.scan_calls == 1
    assert fake.backfill_calls == 1
    assert fake.optimize_calls == 1


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_memory_auto_backup_opt_in_values_run_routine(
    value, monkeypatch, patched_maintenance
):
    monkeypatch.setenv("MEMORY_AUTO_BACKUP", value)
    fake, backup_calls = patched_maintenance

    server._auto_maintenance()

    assert len(backup_calls) == 1
    assert backup_calls[0]["tag"] == "routine"


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
def test_memory_auto_backup_false_values_skip_routine(
    value, monkeypatch, patched_maintenance
):
    monkeypatch.setenv("MEMORY_AUTO_BACKUP", value)
    fake, backup_calls = patched_maintenance

    server._auto_maintenance()

    assert backup_calls == []
    assert fake.scan_calls == 1
    assert fake.backfill_calls == 1
    assert fake.optimize_calls == 1


@pytest.mark.parametrize("value", ["", "maybe", "2", "   "])
def test_memory_auto_backup_unknown_values_preserve_enabled(
    value, monkeypatch, patched_maintenance
):
    monkeypatch.setenv("MEMORY_AUTO_BACKUP", value)
    fake, backup_calls = patched_maintenance

    server._auto_maintenance()

    assert len(backup_calls) == 1


def test_memory_auto_backup_real_sqlite_backup_lifecycle(monkeypatch, tmp_path):
    db_path = tmp_path / "real-maint.db"
    _make_db(db_path)
    fake = FakeStore(str(db_path))

    monkeypatch.setattr(server, "_get_store", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr(bootstrap, "bootstrap_project", lambda *a, **k: None, raising=False)

    # Disabled: no routine backup may be created, but maintenance continues.
    monkeypatch.setenv("MEMORY_AUTO_BACKUP", " 0 ")
    server._auto_maintenance()
    assert list(tmp_path.glob("*.bak")) == []
    assert fake.scan_calls == 1
    assert fake.backfill_calls == 1
    assert fake.optimize_calls == 1
    assert fake.closed_calls == 1

    # Default/unset: the same temp DB gets one verified routine backup.
    monkeypatch.delenv("MEMORY_AUTO_BACKUP", raising=False)
    server._auto_maintenance()
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1
    assert fake.scan_calls == 2
    assert fake.backfill_calls == 2
    assert fake.optimize_calls == 2
    assert fake.closed_calls == 2

    backup = backups[0]
    conn = sqlite3.connect(str(backup))
    try:
        assert conn.execute("SELECT content FROM memory").fetchone()[0] == "keep me"
    finally:
        conn.close()