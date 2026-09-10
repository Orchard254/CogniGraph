"""Hermetic pytest module for server.py MCP tools.

Strategy (per prior research):
- server.py exposes plain @mcp.tool() functions that stay directly callable.
- Import is side-effect safe (no MemoryStore at import, only FastMCP + flags).
- Each tool calls _ensure_first_time_setup() then `with _store() as _s`.

Hermeticity:
- An autouse fixture points MEMORY_DB_PATH at tmp_path and stubs
  server._ensure_first_time_setup to a no-op, so the live DB
  (C:/PROJECT/project-memory/.cognigraph/memory.db) is never touched,
  even if a test forgets to install a store fixture.
- Real-DB round-trips are limited to 2 tool writes total (embedding cost);
  seeds use embed=False/force=True where semantics are not under test.
- No subprocess, no network, no git dependence.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server
from memory_store import MemoryStore
from pm_common import default_db_path

LIVE_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".cognigraph", "memory.db"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _hermetic_guard(tmp_path, monkeypatch):
    """Fence every test off the live DB, even if it installs no store."""
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "guard.db"))
    monkeypatch.setattr(server, "_ensure_first_time_setup", lambda: None)


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    """Route server._get_store at a fresh REAL MemoryStore on tmp_path.

    A factory lambda is used (not a singleton) because server._store()
    closes the handle after every tool call.
    """
    db = str(tmp_path / "rt" / "memory.db")

    def _factory():
        return MemoryStore(db)

    monkeypatch.setattr(server, "_get_store", _factory)
    return db


class FakeStore:
    """Minimal stand-in for MemoryStore: records calls, returns canned data."""

    def __init__(self):
        self.calls = []
        self.closed = 0

    def _rec(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return kwargs

    # -- intake ----------------------------------------------------------
    def write(self, **kwargs):
        self._rec("write", **kwargs)
        return 5

    def write_batch(self, facts, embed_errors=None):
        self._rec("write_batch", facts=facts)
        return [3, -4]

    # -- retrieval -------------------------------------------------------
    def query(self, text, category=None, tag=None, limit=10, session_id=None):
        self._rec(
            "query",
            text=text,
            category=category,
            tag=tag,
            limit=limit,
            session_id=session_id,
        )
        return [{"id": 1, "content": "canned"}]

    def query_hybrid(
        self,
        text,
        category=None,
        tag=None,
        limit=10,
        expand_graph=True,
        session_id=None,
        repo_id=None,
        intent="",
        agent_type="",
    ):
        self._rec(
            "query_hybrid",
            text=text,
            limit=limit,
            expand_graph=expand_graph,
            repo_id=repo_id,
            intent=intent,
            agent_type=agent_type,
        )
        return [{"id": 2, "content": "canned-hybrid"}]

    def get_context(
        self, query, limit=5, repo_id=None, max_chars=8000, intent="", agent_type=""
    ):
        self._rec(
            "get_context",
            query=query,
            limit=limit,
            repo_id=repo_id,
            max_chars=max_chars,
            intent=intent,
            agent_type=agent_type,
        )
        return "# canned context"

    def _derive_default_repo_id(self):
        return 7

    def _detect_intent(self, query):
        return "fix"

    # -- lifecycle -------------------------------------------------------
    def supersede(self, old_id, new_content, **kwargs):
        self._rec("supersede", old_id=old_id, new_content=new_content, **kwargs)
        return 9

    def deprecate(self, id, reason=None):
        self._rec("deprecate", id=id, reason=reason)
        return None

    def get_state(self, category=None, session_id=None):
        self._rec("get_state", category=category, session_id=session_id)
        return [{"id": 1, "content": "canned-state"}]

    def stats(self):
        self._rec("stats")
        return {"facts": 0}

    def close(self):
        self.closed += 1


@pytest.fixture
def fake_store(monkeypatch):
    """Route server._get_store at a shared FakeStore (close() is a no-op)."""
    fake = FakeStore()
    monkeypatch.setattr(server, "_get_store", lambda: fake)
    return fake


def _last_call(fake, method):
    for name, kwargs in reversed(fake.calls):
        if name == method:
            return kwargs
    raise AssertionError(f"no call to {method} recorded: {fake.calls!r}")


# ---------------------------------------------------------------------------
# Clamp helpers (pure unit tests — no store needed)
# ---------------------------------------------------------------------------


def test_clamp_limit_bounds():
    assert server._clamp_limit(9999) == 100
    assert server._clamp_limit(0) == 1
    assert server._clamp_limit(-5) == 1
    assert server._clamp_limit("garbage") == 10
    assert server._clamp_limit(None) == 10
    assert server._clamp_limit(7) == 7


def test_clamp_chars_bounds():
    assert server._clamp_chars(10**9) == 128000
    assert server._clamp_chars(0) == 256
    assert server._clamp_chars(-100) == 256
    assert server._clamp_chars("garbage") == 8000
    assert server._clamp_chars(4000) == 4000


def test_clamp_ttl_and_importance():
    assert server._clamp_ttl(-30) == 1
    assert server._clamp_ttl(10**9) == 86400
    assert server._clamp_ttl("garbage") == 60
    assert server._clamp_importance(99) == 5
    assert server._clamp_importance(0) == 1
    assert server._clamp_importance("garbage") == 3
    assert server._clamp_importance(4) == 4


# ---------------------------------------------------------------------------
# Real-DB round-trips on tmp_path (2 tool writes total in this module)
# ---------------------------------------------------------------------------


def test_write_then_query_roundtrip(real_db):
    marker = "hermetic roundtrip token zxqk7"
    res = server.memory_write(
        category="component",
        content=f"server tools test fact: {marker}",
        rationale="round-trip check",
    )
    assert res["status"] == "written"
    assert res["id"] > 0

    out = server.memory_query(marker)
    assert out["count"] >= 1
    assert any(marker in r["content"] for r in out["results"])


def test_duplicate_write_status_path(real_db):
    content = "server tools duplicate probe content wv41"
    seed = MemoryStore(real_db)
    try:
        # NOTE: the seed must carry an embedding (embed=True): the store's
        # dedup guard takes the semantic path when an encoder is available,
        # and unembedded facts are invisible to it (no Jaccard fallback).
        seed_id = seed.write(category="decision", content=content, force=True)
    finally:
        seed.close()
    assert seed_id > 0

    res = server.memory_write(category="decision", content=content)
    assert res["status"] == "DUPLICATE_DETECTED"
    assert res["id"] == seed_id


# ---------------------------------------------------------------------------
# Delegation / clamping via FakeStore (no embedding cost)
# ---------------------------------------------------------------------------


def test_write_batch_reports_duplicates(fake_store):
    res = server.memory_write_batch(
        [{"category": "component", "content": "a"},
         {"category": "component", "content": "b"}]
    )
    assert res["status"] == "written_batch"
    assert res["written"] == 1
    assert res["duplicates_detected"] == 1
    assert res["duplicate_of_existing_ids"] == [4]


def test_query_clamps_limit(fake_store):
    server.memory_query("x", limit=9999)
    assert _last_call(fake_store, "query_hybrid")["limit"] == 100
    server.memory_query("x", limit=0)
    assert _last_call(fake_store, "query_hybrid")["limit"] == 1


def test_query_clamps_and_derives_repo(fake_store):
    out = server.memory_query("x", limit=500)
    call = _last_call(fake_store, "query_hybrid")
    assert call["limit"] == 100
    assert call["repo_id"] == 7  # FakeStore._derive_default_repo_id()
    assert out["count"] == 1


def test_get_context_clamps_and_detects_intent(fake_store):
    out = server.memory_get_context("fix the bug", limit=999, max_tokens=10**9)
    call = _last_call(fake_store, "get_context")
    assert call["limit"] == 50
    # max_tokens=1e9 clamps to 128000 chars, then per-agent cap (default 8000) applies
    assert call["max_chars"] == 8000
    assert out["detected_intent"] == "fix"  # FakeStore._detect_intent()
    assert "canned context" in out["context_markdown"]


def test_get_context_unknown_agent_falls_back(fake_store):
    out = server.memory_get_context("q", agent_type="nonsense")
    assert out["agent_type"] == "default"
    assert out["format_info"].get("auto_selected") is None  # plain tool, no _tag
    call = _last_call(fake_store, "get_context")
    assert call["agent_type"] == "default"


def test_supersede_deprecate_shapes(fake_store):
    sup = server.memory_supersede(old_id=1, new_content="new")
    assert sup == {"new_id": 9, "old_id": 1, "status": "superseded"}

    dep = server.memory_deprecate(id=2, reason="stale")
    assert dep == {"id": 2, "status": "deprecated"}


# ---------------------------------------------------------------------------
# Invalid-arg errors surface cleanly (real store, no embedding: validation
# raises before any write/embedding work happens)
# ---------------------------------------------------------------------------


def test_invalid_category_raises(real_db):
    with pytest.raises(ValueError):
        server.memory_write(category="bogus", content="whatever")


def test_empty_content_raises(real_db):
    with pytest.raises(ValueError):
        server.memory_write(category="component", content="   ")


def test_supersede_missing_id_raises(real_db):
    with pytest.raises(ValueError, match="no memory row"):
        server.memory_supersede(old_id=999999, new_content="new")


# ---------------------------------------------------------------------------
# Env override + import safety
# ---------------------------------------------------------------------------


def test_memory_db_path_env_override(tmp_path, monkeypatch):
    override = str(tmp_path / "env-override" / "memory.db")
    monkeypatch.setenv("MEMORY_DB_PATH", override)
    assert default_db_path() == override
    assert os.path.abspath(override) != os.path.abspath(LIVE_DB)

    store = MemoryStore()  # no arg -> must honor the env override
    try:
        assert os.path.abspath(store.db_path) == os.path.abspath(override)
    finally:
        store.close()
    assert os.path.isfile(override)


def test_import_is_side_effect_safe():
    # Research claim: no MemoryStore instance at import, only FastMCP + flags.
    assert server.mcp is not None
    assert isinstance(server._first_time_setup_done, bool)
    holders = [
        name
        for name, value in vars(server).items()
        if isinstance(value, MemoryStore)
    ]
    assert holders == []
