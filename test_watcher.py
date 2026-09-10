"""Deterministic tests for watcher.py.

No real database, no watchdog (not installed here — the polling fallback is
what runs), no real 2-second debounce waits: timers, sleeps, and the thread
store are all stubbed. What IS tested is the logic: watch filtering,
trailing-sync debounce, per-thread store caching, the _sync_file upsert paths,
and start/stop lifecycle.
"""
import hashlib
import os
import threading
import time

import pytest

import watcher
from watcher import (_Handler, _should_watch, _sync_file, _thread_store,
                     close_thread_stores, start_watcher, stop_watcher)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, existing=()):
        self.existing = list(existing)
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((sql, tuple(params)))
        if sql.startswith("SELECT id, code_hash"):
            return FakeResult(self.existing)
        return FakeResult([])


class FakeStore:
    def __init__(self, db_path=":fake:"):
        self.db_path = db_path
        self.conn = FakeConn()
        self.writes = []
        self.synced_nodes = []
        self.re_embedded = []
        self.linked = []
        self.closed = False

    def sync_structural_nodes(self, rel, nodes, project_root=None):
        self.synced_nodes.append((rel, nodes))

    def _store_embedding(self, fid, content):
        self.re_embedded.append((fid, content))
        return True

    def write(self, **kwargs):
        self.writes.append(kwargs)
        return 99

    def link_facts_to_structural_nodes(self, rel=None):
        self.linked.append(rel)

    def close(self):
        self.closed = True


class FakeEvent:
    def __init__(self, src_path, is_directory=False):
        self.src_path = src_path
        self.is_directory = is_directory


@pytest.fixture
def root(tmp_path):
    return tmp_path


@pytest.fixture
def pyfile(root):
    f = root / "svc.py"
    f.write_bytes(b"def helper():\n    return 1\n")
    return f


@pytest.fixture
def no_thread_swap(monkeypatch):
    """_sync_file swaps in a per-thread real store unless disabled."""
    monkeypatch.setattr(watcher, "MemoryStore", None)


@pytest.fixture
def fake_nodes(monkeypatch):
    monkeypatch.setattr(watcher, "extract_structures",
                        lambda p: [{"type": "function", "name": "helper",
                                    "line_start": 1, "line_end": 2}])


# --------------------------------------------------------------------------
# _should_watch
# --------------------------------------------------------------------------

def test_should_watch_matrix(root, pyfile):
    assert _should_watch(pyfile, root) is True
    txt = root / "notes.txt"
    txt.write_text("hi")
    assert _should_watch(txt, root) is False  # unwatched extension
    for d in ("node_modules", ".git", "venv", ".project-memory", ".cognigraph", "__pycache__"):
        sub = root / d
        sub.mkdir(exist_ok=True)
        f = sub / "x.py"
        f.write_text("x=1")
        assert _should_watch(f, root) is False, d
    assert _should_watch(root / "missing.py", root) is False
    outside = root.parent / "sibling.py"
    outside.write_text("x=1")
    assert _should_watch(outside, root) is False  # escapes root
    big = root / "big.py"
    big.write_bytes(b"x" * (1_000_000 + 1))
    assert _should_watch(big, root) is False  # size gate
    egg = root / "pkg.egg-info"
    egg.mkdir(exist_ok=True)
    ef = egg / "PKG.py"
    ef.write_text("x=1")
    assert _should_watch(ef, root) is False


# --------------------------------------------------------------------------
# per-thread store cache
# --------------------------------------------------------------------------

def test_thread_store_caches_and_isolates(monkeypatch):
    made = []

    class S:
        def __init__(self, db):
            self.db = db
            made.append(db)
        def close(self):
            pass

    monkeypatch.setattr(watcher, "MemoryStore", S)
    watcher._thread_local.__dict__.pop("stores", None)
    try:
        a = _thread_store("A")
        assert _thread_store("A") is a and made == ["A"]
        assert _thread_store("B") is not a
        other = []
        t = threading.Thread(target=lambda: other.append(_thread_store("A")))
        t.start(); t.join()
        assert other[0] is not a  # thread-local: no cross-thread reuse
    finally:
        watcher._thread_local.__dict__.pop("stores", None)


def test_close_thread_stores(monkeypatch):
    closed = []

    class S:
        def __init__(self, db):
            self.db = db
        def close(self):
            closed.append(self.db)

    monkeypatch.setattr(watcher, "MemoryStore", S)
    watcher._thread_local.__dict__.pop("stores", None)
    try:
        _thread_store("A"); _thread_store("B")
        close_thread_stores("A")
        assert closed == ["A"]
        close_thread_stores()
        assert closed == ["A", "B"]
        close_thread_stores()  # empty cache: no-op
    finally:
        watcher._thread_local.__dict__.pop("stores", None)


def test_close_thread_stores_swallows_close_errors(monkeypatch):
    class Bad:
        def __init__(self, db):
            pass
        def close(self):
            raise RuntimeError("nope")
    monkeypatch.setattr(watcher, "MemoryStore", Bad)
    watcher._thread_local.__dict__.pop("stores", None)
    try:
        _thread_store("Z")
        close_thread_stores()  # must not raise
    finally:
        watcher._thread_local.__dict__.pop("stores", None)


# --------------------------------------------------------------------------
# _Handler debounce (timers stubbed — no real waiting)
# --------------------------------------------------------------------------

class FakeTimer:
    fired = []

    def __init__(self, interval, fn, args=()):
        self.interval = interval
        self.fn = fn
        self.args = args
        FakeTimer.fired.append(self)
        self.daemon = False

    def start(self):
        pass  # test fires manually


@pytest.fixture
def timers(monkeypatch):
    FakeTimer.fired.clear()
    monkeypatch.setattr(threading, "Timer", FakeTimer)
    return FakeTimer


def test_handler_immediate_then_debounced_trailing(root, pyfile, timers, monkeypatch):
    synced = []
    monkeypatch.setattr(watcher, "_sync_file",
                        lambda p, r, s: synced.append(str(p)))
    h = _Handler(root, FakeStore())
    h.on_modified(FakeEvent(str(pyfile)))
    assert synced == [str(pyfile)]  # first event: immediate
    assert timers.fired == []
    h.on_modified(FakeEvent(str(pyfile)))
    assert synced == [str(pyfile)]  # second: debounced, no immediate sync
    assert len(timers.fired) == 1
    assert 0 < timers.fired[0].interval <= 2.1
    h.on_modified(FakeEvent(str(pyfile)))
    assert len(timers.fired) == 1  # already pending: no second timer
    timers.fired[0].fn(*timers.fired[0].args)  # fire trailing manually
    assert synced == [str(pyfile), str(pyfile)]


def test_handler_ignores_dirs_unwatched_and_foreign(root, pyfile, timers, monkeypatch):
    synced = []
    monkeypatch.setattr(watcher, "_sync_file",
                        lambda p, r, s: synced.append(str(p)))
    h = _Handler(root, FakeStore())
    h.on_modified(FakeEvent(str(pyfile), is_directory=True))
    txt = root / "n.txt"
    txt.write_text("x")
    h.on_modified(FakeEvent(str(txt)))
    h.on_created(FakeEvent(str(txt)))  # created delegates to modified
    assert synced == [] and timers.fired == []


def test_trailing_sync_foreign_path_returns(root, pyfile, monkeypatch):
    synced = []
    monkeypatch.setattr(watcher, "_sync_file",
                        lambda p, r, s: synced.append(str(p)))
    h = _Handler(root, FakeStore())
    h._trailing_sync(pyfile, "k")  # valid: syncs (pending bookkeeping cleared)
    assert synced == [str(pyfile)]
    synced.clear()
    h._trailing_sync(root.parent / "sibling.py", "k2")  # outside root: no sync
    assert synced == []


# --------------------------------------------------------------------------
# _sync_file upsert paths
# --------------------------------------------------------------------------

def _file_hash(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_sync_new_file_writes_with_force_embed(root, pyfile, no_thread_swap, fake_nodes):
    store = FakeStore()
    _sync_file(pyfile, root, store)
    assert store.synced_nodes == [("svc.py", [{"type": "function", "name": "helper",
                                              "line_start": 1, "line_end": 2}])]
    assert len(store.writes) == 1
    w = store.writes[0]
    assert w["file_path"] == "svc.py" and w["force"] is True and w["embed"] is True
    assert w["code_hash"] == _file_hash(pyfile)
    assert w["category"] == "other"  # template stub, not curated knowledge
    assert store.linked == ["svc.py"]


def test_sync_lookup_matches_stub_rows_only(root, pyfile, no_thread_swap, fake_nodes):
    """The upsert lookup must exclude curated facts anchored to the file."""
    store = FakeStore()
    _sync_file(pyfile, root, store)
    lookups = [sql for sql, _ in store.conn.sql if sql.startswith("SELECT id, code_hash")]
    assert lookups and "content LIKE 'Source file:%'" in lookups[0]


def test_sync_never_clobbers_curated_fact(root, pyfile, no_thread_swap, fake_nodes):
    """A curated fact anchored to the file must survive a file change.

    The pre-guard lookup returned every active row for the file_path and the
    UPDATE rewrote its content with the stub template — that destroyed 7
    curated facts in the live corpus. With the stub-only lookup a curated row
    is invisible to the sync: no update, no re-embed, and the new stub goes
    out as a separate write.
    """
    store = FakeStore()
    # One existing stub (stale hash) plus one curated fact on the same file.
    store.conn = FakeConn(existing=[
        {"id": 5, "code_hash": "stale"},
    ])
    captured = {}
    real_execute = store.conn.execute

    def execute(sql, params=()):
        captured.setdefault("sqls", []).append(sql)
        return real_execute(sql, params)

    store.conn.execute = execute
    _sync_file(pyfile, root, store)
    updates = [sql for sql in captured["sqls"] if "UPDATE memory" in sql]
    assert len(updates) == 1  # only the stub row, never the curated one
    assert store.re_embedded and store.re_embedded[0][0] == 5


def test_sync_unchanged_hash_updates_nothing(root, pyfile, no_thread_swap, fake_nodes):
    store = FakeStore()
    store.conn = FakeConn(existing=[{"id": 5, "code_hash": _file_hash(pyfile)}])
    _sync_file(pyfile, root, store)
    assert store.writes == []
    assert not any("UPDATE memory" in sql for sql, _ in store.conn.sql)
    assert store.re_embedded == []


def test_sync_changed_hash_updates_and_reembeds(root, pyfile, no_thread_swap, fake_nodes):
    store = FakeStore()
    store.conn = FakeConn(existing=[{"id": 5, "code_hash": "stale"}])
    _sync_file(pyfile, root, store)
    updates = [p for sql, p in store.conn.sql if "UPDATE memory" in sql]
    assert len(updates) == 1 and updates[0][0] == _file_hash(pyfile)
    assert len(store.re_embedded) == 1 and store.re_embedded[0][0] == 5


def test_sync_tolerates_extractor_failure(root, pyfile, no_thread_swap, monkeypatch):
    def boom(p):
        raise RuntimeError("tree-sitter down")
    monkeypatch.setattr(watcher, "extract_structures", boom)
    store = FakeStore()
    _sync_file(pyfile, root, store)  # must not raise; file fact still written
    assert store.synced_nodes == []
    assert len(store.writes) == 1


def test_sync_no_extractor_module(root, pyfile, no_thread_swap, monkeypatch):
    monkeypatch.setattr(watcher, "extract_structures", None)
    store = FakeStore()
    _sync_file(pyfile, root, store)
    assert store.synced_nodes == [] and len(store.writes) == 1


def test_sync_missing_file_never_raises(root, no_thread_swap, fake_nodes):
    _sync_file(root / "gone.py", root, FakeStore())  # deleted between event and handler


# --------------------------------------------------------------------------
# start/stop lifecycle (polling fallback — watchdog not installed)
# --------------------------------------------------------------------------

def test_start_missing_root_returns_none():
    assert start_watcher(project_root="/nonexistent-root-xyz") is None


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_poll_first_scan_silent_second_syncs(root, pyfile, monkeypatch):
    """First poll only records mtimes; a file touched afterwards is synced."""
    monkeypatch.setattr(watcher, "_WATCHDOG_AVAILABLE", False)  # force polling path
    sleeps = []
    """First poll only records mtimes; a file touched afterwards is synced."""
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 1:
            os.utime(pyfile, (time.time() + 5, time.time() + 5))  # touch after scan 1
            return
        raise SystemExit  # silently ends the daemon thread
    monkeypatch.setattr(time, "sleep", fake_sleep)
    store = FakeStore()
    monkeypatch.setattr(watcher, "_thread_store", lambda db: store)
    monkeypatch.setattr(watcher, "extract_structures", lambda p: [])
    t = start_watcher(project_root=root, store=store)
    assert t is not None and t.daemon is True
    t.join(timeout=10)
    assert not t.is_alive()
    assert sleeps == [30, 30]
    assert len(store.writes) == 1 and store.writes[0]["file_path"] == "svc.py"


def test_stop_watcher_with_handle_and_without(monkeypatch):
    calls = []

    class Handle:
        def stop(self):
            calls.append("stop")
        def join(self, timeout=None):
            calls.append(("join", timeout))

    closed = []
    monkeypatch.setattr(watcher, "close_thread_stores", lambda db=None: closed.append(db))
    stop_watcher(Handle(), db_path="X")
    assert calls[0] == "stop" and calls[1][0] == "join"
    assert closed == ["X"]

    class BadHandle:
        def stop(self):
            raise RuntimeError("stuck")
        def join(self, timeout=None):
            raise RuntimeError("stuck")
    stop_watcher(BadHandle())  # stop/join errors swallowed, stores still closed
    assert closed == ["X", None]
    stop_watcher(None)
    assert closed == ["X", None, None]


# --------------------------------------------------------------------------
# watchdog observer branch (runs only when watchdog is installed)
# --------------------------------------------------------------------------

watchdog = pytest.importorskip("watchdog")


def test_observer_detects_change_and_stop(root, monkeypatch):
    """End-to-end through the real Observer: file change → sync → stop."""
    store = FakeStore()
    monkeypatch.setattr(watcher, "_thread_store", lambda db: store)
    monkeypatch.setattr(watcher, "extract_structures", lambda p: [])
    assert watcher._WATCHDOG_AVAILABLE is True
    handle = start_watcher(project_root=root, store=store)
    assert handle is not None
    try:
        target = root / "live.py"
        target.write_text("x = 1\n")
        deadline = time.time() + 10
        while not store.writes and time.time() < deadline:
            time.sleep(0.1)
        assert len(store.writes) == 1
        assert store.writes[0]["file_path"] == "live.py"
        time.sleep(2.5)  # drain trailing-sync timers while this test's patches hold
    finally:
        closed = []
        monkeypatch.setattr(watcher, "close_thread_stores",
                            lambda db=None: closed.append(db))
        stop_watcher(handle, db_path="D")
    assert not handle.is_alive()
    assert closed == ["D"]


def test_observer_ignores_unwatched_extensions(root, monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(watcher, "_thread_store", lambda db: store)
    handle = start_watcher(project_root=root, store=store)
    assert handle is not None
    try:
        (root / "notes.txt").write_text("hello\n")
        time.sleep(2.5)  # past the debounce window: any sync would have fired
        assert store.writes == []
    finally:
        stop_watcher(handle)
