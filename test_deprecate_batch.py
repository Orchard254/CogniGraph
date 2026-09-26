"""Batch deprecate: one unknown id must not abandon the rest of a cleanup.

Gap: memory_deprecate took a single int, so a corpus cleanup cost one tool call
per fact. The 2026-09-27 cleanup deprecated 8 artifacts in 8 sequential calls,
and an earlier pass deprecated 3 more one at a time.

The parameter is now Union[int, List[int]]. A single int keeps the ORIGINAL
return shape ({"id": id, "status": "deprecated"}) so existing callers and
assertions are unaffected; the batch form adds per-id results.

Run with: python -m pytest test_deprecate_batch.py -v
"""
import os
import tempfile

import pytest

from memory_store import MemoryStore


@pytest.fixture
def store():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    s = MemoryStore(db_path=tmp.name)
    s._current_git_commit = lambda: None
    yield s
    try:
        s.close()
    except Exception:
        pass
    try:
        os.unlink(tmp.name)
    except Exception:
        pass


def _mk(store, n):
    return [store.write(category="other", content=f"cleanup target {i}",
                        embed=False, force=True) for i in range(n)]


def test_deprecate_many_reports_each_outcome(store):
    ids = _mk(store, 3)
    out = store.deprecate_many(ids, "test batch")
    assert [r["status"] for r in out] == ["deprecated"] * 3
    assert [r["id"] for r in out] == ids


def test_deprecate_many_continues_past_a_missing_id(store):
    ids = _mk(store, 2)
    out = store.deprecate_many([ids[0], 999_999, ids[1]], "test batch")
    assert [r["status"] for r in out] == ["deprecated", "not_found", "deprecated"]
    for fid in ids:
        assert store.conn.execute(
            "SELECT deprecated_at FROM memory WHERE id=?", (fid,)).fetchone()[0]


def test_deprecate_many_records_the_reason(store):
    ids = _mk(store, 1)
    store.deprecate_many(ids, "corpus cleanup 2026-09-27")
    reason = store.conn.execute(
        "SELECT deprecated_reason FROM memory WHERE id=?", (ids[0],)).fetchone()[0]
    assert reason == "corpus cleanup 2026-09-27"


def test_deprecate_many_empty_list(store):
    assert store.deprecate_many([], "noop") == []


def test_deprecate_many_leaves_other_facts_alone(store):
    ids = _mk(store, 3)
    keeper = store.write(category="other", content="keeper fact",
                         embed=False, force=True)
    store.deprecate_many(ids[:2], "test")
    assert store.conn.execute(
        "SELECT deprecated_at FROM memory WHERE id=?", (keeper,)).fetchone()[0] is None


# --- the tool wrapper keeps the single-id contract -----------------------

def test_single_id_payload_keeps_legacy_shape(store, monkeypatch):
    """Existing callers assert on {"id": ..., "status": "deprecated"}."""
    import server

    fid = store.write(category="other", content="single deprecate target",
                      embed=False, force=True)
    monkeypatch.setattr(server, "_store", lambda: _null_ctx(store))
    monkeypatch.setattr(server, "_ensure_first_time_setup", lambda: None)

    out = server.memory_deprecate(fid, "because")
    assert out["id"] == fid
    assert out["status"] == "deprecated"
    assert out["count"] == 1
    assert out["not_found"] == []


def test_batch_payload_shape(store, monkeypatch):
    import server

    ids = _mk(store, 2)
    monkeypatch.setattr(server, "_store", lambda: _null_ctx(store))
    monkeypatch.setattr(server, "_ensure_first_time_setup", lambda: None)

    out = server.memory_deprecate(ids + [424_242], "because")
    assert out["id"] is None, "batch form must not claim a single id"
    assert out["ids"] == ids + [424_242]
    assert out["count"] == 2
    assert out["not_found"] == [424_242]
    assert out["status"] == "deprecated"


class _null_ctx:
    def __init__(self, store):
        self._s = store

    def __enter__(self):
        return self._s

    def __exit__(self, *exc):
        return False
