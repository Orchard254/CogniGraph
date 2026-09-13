"""Stub-fact singleton (#530): ux_stub_once index + ingest pre-checks.

The whole-file template stub ("Source file: ...") must exist at most once
per (file_path, content) among active facts. Check-then-write in
watcher/bootstrap cannot close cross-process races, so the database
enforces it (partial UNIQUE index) and write() maps violations back to
the standard near-duplicate sentinel.

Run with: python -m pytest test_stub_dedup.py -v
"""
import os
import tempfile
import threading

import pytest

import watcher
from bootstrap import _stub_already_present, bootstrap_project
from memory_store import MemoryStore
from test_watcher import FakeResult, FakeStore
from watcher import _sync_file


STUB = "Source file: svc.py (python, 10 lines, hash abc12345)"
STUB_OTHER_HASH = "Source file: svc.py (python, 10 lines, hash deadbeef)"


@pytest.fixture
def temp_store():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    store = MemoryStore(db_path=tmp.name)
    store._current_git_commit = lambda: None
    yield store
    try:
        store.close()
    except Exception:
        pass
    try:
        os.unlink(tmp.name)
    except Exception:
        pass


def _stub_count(store):
    return store.conn.execute(
        "SELECT COUNT(*) c FROM memory WHERE deprecated_at IS NULL "
        "AND content LIKE 'Source file:%'").fetchone()["c"]


def _index_present(store):
    return store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='ux_stub_once'").fetchone() is not None


# --------------------------------------------------------------------------
# index + sentinel mapping (real DB)
# --------------------------------------------------------------------------

def test_unique_index_created_on_init(temp_store):
    row = temp_store.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='ux_stub_once'").fetchone()
    assert row is not None
    assert "WHERE" in row["sql"]  # partial index, not a table-wide constraint


def test_second_force_write_returns_existing_sentinel(temp_store):
    first = temp_store.write(
        category="other", content=STUB, file_path="svc.py",
        tags="bootstrap, file, auto-watched", importance=2,
        line_start=1, line_end=10, code_hash="abc12345",
        force=True, embed=False)
    assert first > 0
    second = temp_store.write(
        category="other", content=STUB, file_path="svc.py",
        tags="bootstrap, file, auto-watched", importance=2,
        line_start=1, line_end=10, code_hash="abc12345",
        force=True, embed=False)
    assert second == -first
    assert _stub_count(temp_store) == 1


def test_racing_connections_converge_on_one_row(temp_store):
    """Two connections (the cross-process race shape) writing the identical
    stub with force=True must leave exactly one row; the loser converges
    on the winner via the sentinel instead of raising. Each thread opens
    its own connection, mirroring the per-thread/per-process watcher."""
    db_path = temp_store.db_path
    barrier = threading.Barrier(2)
    results = {}

    def racer(name):
        st = MemoryStore(db_path=db_path)
        st._current_git_commit = lambda: None
        try:
            barrier.wait(timeout=10)
            results[name] = st.write(
                category="other", content=STUB, file_path="svc.py",
                tags="bootstrap, file, auto-watched", importance=2,
                line_start=1, line_end=10, code_hash="abc12345",
                force=True, embed=False)
        finally:
            st.close()

    threads = [threading.Thread(target=racer, args=("a",)),
               threading.Thread(target=racer, args=("b",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads)
    positives = sorted(r for r in results.values() if r > 0)
    negatives = sorted(r for r in results.values() if r < 0)
    assert len(positives) == 1 and len(negatives) == 1
    assert -negatives[0] == positives[0]
    assert _stub_count(temp_store) == 1


def test_non_stub_content_unaffected_by_index(temp_store):
    # Identical curated facts with force=True still insert twice each:
    # the index only constrains the stub template.
    a = temp_store.write(category="decision", content="Use WAL mode",
                         file_path="svc.py", force=True, embed=False)
    b = temp_store.write(category="decision", content="Use WAL mode",
                         file_path="svc.py", force=True, embed=False)
    assert a > 0 and b > 0 and b != a
    # Same file, different stub content (changed hash) is a different
    # (file_path, content) key — no collision.
    c = temp_store.write(category="other", content=STUB, file_path="svc.py",
                         force=True, embed=False)
    d = temp_store.write(category="other", content=STUB_OTHER_HASH,
                         file_path="svc.py", force=True, embed=False)
    assert c > 0 and d > 0 and d != c
    # "Source file:" not at content start is not template content.
    e = temp_store.write(category="component",
                         content="Notes on Source file: handling in sync",
                         file_path="svc.py", force=True, embed=False)
    f = temp_store.write(category="component",
                         content="Notes on Source file: handling in sync",
                         file_path="svc.py", force=True, embed=False)
    assert e > 0 and f > 0 and f != e


def test_deprecated_stub_frees_the_slot(temp_store):
    first = temp_store.write(category="other", content=STUB,
                             file_path="svc.py", force=True, embed=False)
    temp_store.deprecate(first, "test")
    second = temp_store.write(category="other", content=STUB,
                              file_path="svc.py", force=True, embed=False)
    assert second > 0 and second != first


def test_migration_defers_index_until_backfill(tmp_path):
    """A DB that still holds pre-fix duplicates migrates cleanly (no index,
    no failed init) and self-heals: after deprecating the dupe, the next
    open creates the index."""
    path = str(tmp_path / "dupes.db")
    store = MemoryStore(db_path=path)
    store._current_git_commit = lambda: None
    assert _index_present(store)
    store.conn.execute("DROP INDEX ux_stub_once")
    for _ in range(2):
        store.conn.execute(
            "INSERT INTO memory (category, content, confidence, created_at, "
            "file_path) VALUES ('other', ?, 'assumed', '2026-01-01T00:00:00', "
            "'svc.py')", (STUB,))
    store.conn.commit()
    store.close()
    reopened = MemoryStore(db_path=path)
    try:
        assert not _index_present(reopened)
        dupe_id = reopened.conn.execute(
            "SELECT MAX(id) m FROM memory WHERE content LIKE 'Source file:%'"
        ).fetchone()["m"]
        reopened.deprecate(dupe_id, "test backfill")
    finally:
        reopened.close()
    healed = MemoryStore(db_path=path)
    try:
        assert _index_present(healed)
    finally:
        healed.close()


# --------------------------------------------------------------------------
# pre-checks (fakes + real bootstrap loop)
# --------------------------------------------------------------------------

class _ExactHitConn:
    """Fake connection: no stub-template rows for the legacy lookup, but the
    exact-match pre-check hits (simulates a concurrent sync winning)."""

    def __init__(self):
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((sql, tuple(params)))
        if sql.startswith("SELECT id, code_hash"):
            return FakeResult([])
        return FakeResult([{"id": 7}])


@pytest.fixture
def svc_root(tmp_path):
    return tmp_path


@pytest.fixture
def svc_file(svc_root):
    f = svc_root / "svc.py"
    f.write_bytes(b"def helper():\n    return 1\n")
    return f


@pytest.fixture
def _no_swap(monkeypatch):
    monkeypatch.setattr(watcher, "MemoryStore", None)


@pytest.fixture
def _nodes(monkeypatch):
    monkeypatch.setattr(watcher, "extract_structures",
                        lambda p: [{"type": "function", "name": "helper",
                                    "line_start": 1, "line_end": 2}])


def test_watcher_precheck_skips_write_on_exact_match(
        svc_root, svc_file, _no_swap, _nodes):
    store = FakeStore()
    store.conn = _ExactHitConn()
    _sync_file(svc_file, svc_root, store)
    assert store.writes == []
    assert any("SELECT id FROM memory WHERE file_path" in sql
               for sql, _ in store.conn.sql)


def test_watcher_precheck_miss_still_writes_with_force(
        svc_root, svc_file, _no_swap, _nodes):
    store = FakeStore()
    _sync_file(svc_file, svc_root, store)
    assert len(store.writes) == 1
    assert store.writes[0]["force"] is True


def test_stub_already_present_fails_open_without_conn():
    class _NoConn:
        pass
    assert _stub_already_present(_NoConn(), "svc.py", STUB) is False


def test_bootstrap_full_second_run_writes_no_new_stubs(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")
    (proj / "b.py").write_text("y = 2\n", encoding="utf-8")
    store = MemoryStore(db_path=str(tmp_path / "boot.db"))
    store._current_git_commit = lambda: None
    try:
        first = bootstrap_project(proj, store=store, dry_run=False,
                                  full=True, max_facts=50,
                                  install_hook=False, drop_protocol=False)
        assert first["status"] == "BOOTSTRAPPED"
        stubs_after_first = _stub_count(store)
        assert stubs_after_first >= 2
        total_after_first = store.conn.execute(
            "SELECT COUNT(*) c FROM memory "
            "WHERE deprecated_at IS NULL").fetchone()["c"]
        second = bootstrap_project(proj, store=store, dry_run=False,
                                   full=True, max_facts=50,
                                   install_hook=False, drop_protocol=False)
        assert second["facts_written"] == 0
        assert _stub_count(store) == stubs_after_first
        assert store.conn.execute(
            "SELECT COUNT(*) c FROM memory "
            "WHERE deprecated_at IS NULL").fetchone()["c"] == total_after_first
    finally:
        store.close()
