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
- Real-DB round-trips keep embedded writes few (embedding cost); seeds use
  embed=False/force=True where semantics are not under test.
- No subprocess, no network, no git dependence.
"""

import os
import sys
import hashlib

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

    def deprecate_many(self, ids, reason=None):
        self._rec("deprecate_many", ids=list(ids), reason=reason)
        return [{"id": i, "status": "deprecated"} for i in ids]

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


def test_stats_real_db(real_db):
    out = server.memory_stats()
    for key in ("active_by_category", "total_active", "total_deprecated",
                "total_edges", "embedded_facts", "semantic_backend",
                "background_failures", "db_path"):
        assert key in out
    assert out["total_active"] == 0
    assert out["background_failures"] == {
        "vec_sync": 0, "structural_sync": 0, "re_embed": 0, "fact_linking": 0,
    }


def test_get_file_context_real_db(real_db):
    marker = "file-context probe fact kx92"
    seed = MemoryStore(real_db)
    try:
        seed.write(category="component", content=marker,
                   file_path="src/widget.py", force=True, embed=False)
    finally:
        seed.close()
    out = server.memory_get_file_context("src/widget.py")
    assert out["file_path"] == "src/widget.py"
    assert marker in out["briefing_markdown"]


def test_check_safety_real_db(real_db):
    out = server.memory_check_safety(["src/a.py", "src/b.py"])
    assert out["status"] == "CHECKED"
    assert out["files_analyzed"] == 2
    assert out["cross_dependencies"] == []
    assert set(out["briefings"]) == {"src/a.py", "src/b.py"}

    empty = server.memory_check_safety([])
    assert empty["status"] == "NO_FILES_PROVIDED"


def test_prepare_task_real_db(real_db):
    out = server.memory_prepare_task("implement caching layer")
    assert out["intent"] == "implement caching layer"
    assert isinstance(out["briefing_markdown"], str)
    assert out["briefing_markdown"]


def test_checkpoint_task_real_db(real_db):
    out = server.memory_checkpoint_task(
        summary="step-4 checkpoint probe qj17",
        components=[{"content": "component probe qj17"}],
        decisions=[{"content": "decision probe qj17"}],
        test_baseline="n/a",
        failures=[{"content": "failed approach probe qj17", "rationale": "probe"}],
    )
    assert out["status"] == "CHECKPOINT_RECORDED"
    assert out["state_id"] > 0
    assert out["total_facts_written"] == 4  # failure + decision + component + state
    assert out["duplicates_skipped"] == 0
    assert all(i > 0 for i in out["fact_ids"])


def test_who_calls_real_db(real_db):
    now = "2026-09-11T00:00:00"
    seed = MemoryStore(real_db)
    try:
        seed.conn.execute(
            "INSERT INTO structural_nodes (node_type, name, class_name, file_path, "
            "line_start, line_end, language, created_at, last_seen_at) "
            "VALUES ('function', 'probe_fn_qj17', '', 'src/probe.py', 1, 5, 'python', ?, ?)",
            (now, now),
        )
    finally:
        seed.close()

    out = server.memory_who_calls("probe_fn_qj17")
    assert out["found"] is True
    assert [d["file_path"] for d in out["definitions"]] == ["src/probe.py"]
    assert out["callers"] == []

    missing = server.memory_who_calls("no_such_symbol_qj17")
    assert missing["found"] is False
    assert missing["definitions"] == []


def test_blast_radius_real_db(real_db):
    now = "2026-09-11T00:00:00"
    seed = MemoryStore(real_db)
    try:
        seed.conn.execute(
            "INSERT INTO structural_nodes (node_type, name, class_name, file_path, "
            "line_start, line_end, language, created_at, last_seen_at) "
            "VALUES ('function', 'leaf_fn_qj17', '', 'src/leaf.py', 1, 5, 'python', ?, ?)",
            (now, now),
        )
        seed.conn.execute(
            "INSERT INTO structural_nodes (node_type, name, class_name, file_path, "
            "line_start, line_end, language, created_at, last_seen_at) "
            "VALUES ('function', 'mid_fn_qj17', '', 'src/mid.py', 1, 8, 'python', ?, ?)",
            (now, now),
        )
        leaf = seed.conn.execute(
            "SELECT id FROM structural_nodes WHERE name = 'leaf_fn_qj17'").fetchone()["id"]
        mid = seed.conn.execute(
            "SELECT id FROM structural_nodes WHERE name = 'mid_fn_qj17'").fetchone()["id"]
        seed.conn.execute(
            "INSERT INTO structural_edges (caller_id, callee_id, edge_type, created_at) "
            "VALUES (?, ?, 'calls', ?)", (mid, leaf, now))
    finally:
        seed.close()

    out = server.memory_blast_radius("leaf_fn_qj17")
    assert out["found"] is True
    assert out["total_callers"] == 1
    assert out["total_files"] == 1
    assert out["depth_counts"] == {1: 1} or out["depth_counts"] == {"1": 1}
    assert not any("not yet tracked" in lim for lim in out["known_limitations"])
    # Hand-inserted edge with no call_sites rows: symbols still carry the
    # (empty) call_lines field rather than omitting it.
    assert all("call_lines" in s for f in out["files"] for s in f["symbols"])
    assert all(s["call_lines"] == [] for f in out["files"] for s in f["symbols"])

    missing = server.memory_blast_radius("no_such_symbol_qj17")
    assert missing["found"] is False


def test_project_shape_real_db(real_db):
    out = server.memory_project_shape()
    assert "Project Shape" in out["markdown"]


def test_flag_fact_real_db(real_db):
    seed = MemoryStore(real_db)
    try:
        fid = seed.write(category="decision", content="flag probe fact mt63",
                         force=True, embed=False)
    finally:
        seed.close()
    assert fid > 0

    out = server.memory_flag_fact(fact_id=fid, note="misleading probe")
    assert out == {"status": "FLAGGED", "fact_id": fid, "flag_type": "misleading"}

    verify = MemoryStore(real_db)
    try:
        row = verify.conn.execute(
            "SELECT note FROM fact_flags WHERE fact_id = ?", (fid,)
        ).fetchone()
    finally:
        verify.close()
    assert row is not None and row["note"] == "misleading probe"


def test_flag_fact_missing_id_raises(real_db):
    with pytest.raises(ValueError, match="no fact with id"):
        server.memory_flag_fact(fact_id=999999)


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
    # Backward-compatible single-id shape. The batch form (id as a list) adds
    # results/count/not_found/ids on top; see test_deprecate_batch.py.
    assert dep["id"] == 2
    assert dep["status"] == "deprecated"
    assert dep["count"] == 1
    assert dep["not_found"] == []


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


# ---------------------------------------------------------------------------
# memory_read_code (scoped-line code reader)
#
# Tmp-DB layout note: real_db lives at tmp_path/"rt"/memory.db, so
# read_code's project-root derivation (db grandparent, same as
# _blast_unresolved) resolves to tmp_path itself — seed files land there.
# ---------------------------------------------------------------------------


def _rc_write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _rc_hash(text, ls, le):
    lines = text.splitlines(keepends=True)
    return hashlib.sha256("".join(lines[ls - 1:le]).encode("utf-8")).hexdigest()


def _rc_seed(db, name, file_path, ls, le, node_type="function",
             class_name="", code_hash=None,
             last_seen="2026-09-11T00:00:00+00:00"):
    seed = MemoryStore(db)
    try:
        seed.conn.execute(
            "INSERT INTO structural_nodes (node_type, name, class_name, file_path, "
            "line_start, line_end, language, code_hash, created_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'python', ?, ?, ?)",
            (node_type, name, class_name, file_path, ls, le,
             code_hash, "2026-09-11T00:00:00+00:00", last_seen),
        )
        seed.conn.commit()
    finally:
        seed.close()


def test_read_code_symbol_hit(real_db, tmp_path):
    text = "import os\n\n\ndef probe_rc1():\n    a = 1\n    return a\n\n\nprint('t')\n"
    _rc_write(tmp_path, "src/rc1.py", text)
    _rc_seed(real_db, "probe_rc1", "src/rc1.py", 4, 6,
             code_hash=_rc_hash(text, 4, 6))

    out = server.memory_read_code([{"symbol": "probe_rc1"}])
    assert len(out["results"]) == 1
    r = out["results"][0]
    assert r["ok"] is True
    assert r["kind"] == "symbol"
    assert r["file_path"] == "src/rc1.py"
    assert r["requested_lines"] == [4, 6]
    assert r["returned_lines"] == [4, 6]
    assert r["truncated"] is False
    assert r["stale"] is False
    assert r["stale_reason"] is None
    assert r["text"] == "def probe_rc1():\n    a = 1\n    return a\n"


def test_read_code_ambiguous_union(real_db, tmp_path):
    for rel in ("src/a_rc.py", "src/b_rc.py"):
        text = "def dup_rc():\n    pass\n"
        _rc_write(tmp_path, rel, text)
        _rc_seed(real_db, "dup_rc", rel, 1, 2,
                 last_seen="2999-01-01T00:00:00+00:00")

    out = server.memory_read_code([{"symbol": "dup_rc"}])
    assert len(out["results"]) == 2
    assert sorted(r["file_path"] for r in out["results"]) == ["src/a_rc.py", "src/b_rc.py"]
    assert all(r["ok"] and r["ambiguous"] is True for r in out["results"])
    assert all(r["text"] == "def dup_rc():\n    pass\n" for r in out["results"])


def test_read_code_explicit_range(real_db, tmp_path):
    _rc_write(tmp_path, "src/range_rc.py", "".join(f"line{i}\n" for i in range(1, 11)))

    out = server.memory_read_code(
        [{"file_path": "src/range_rc.py", "line_start": 4, "line_end": 6}])
    r = out["results"][0]
    assert r["ok"] is True
    assert r["kind"] == "range"
    assert r["requested_lines"] == [4, 6]
    assert r["returned_lines"] == [4, 6]
    assert r["text"] == "line4\nline5\nline6\n"
    # File has no indexed nodes: nothing to be stale against.
    assert r["stale"] is False
    assert r["stale_reason"] == "file_not_indexed"


def test_read_code_truncation(real_db, tmp_path):
    text = "".join(f"line{i}\n" for i in range(1, 11))
    _rc_write(tmp_path, "src/big_rc.py", text)
    _rc_seed(real_db, "big_rc", "src/big_rc.py", 1, 10,
             code_hash=_rc_hash(text, 1, 10))

    out = server.memory_read_code([{"symbol": "big_rc"}], max_lines_per_item=4)
    r = out["results"][0]
    assert r["ok"] is True
    assert r["truncated"] is True
    assert r["actual_lines"] == [1, 10]
    assert r["returned_lines"] == [1, 4]
    assert r["text"] == "line1\nline2\nline3\nline4\n"
    # Hash is over the full span, so capping never fakes staleness.
    assert r["stale"] is False


def test_read_code_context_margin(real_db, tmp_path):
    _rc_write(tmp_path, "src/ctx_rc.py", "".join(f"line{i}\n" for i in range(1, 11)))

    out = server.memory_read_code(
        [{"file_path": "src/ctx_rc.py", "line_start": 5, "line_end": 5, "context": 2}])
    r = out["results"][0]
    assert r["ok"] is True
    assert r["requested_lines"] == [5, 5]
    assert r["returned_lines"] == [3, 7]
    assert r["text"] == "line3\nline4\nline5\nline6\nline7\n"


def test_read_code_stale_hash_mismatch(real_db, tmp_path):
    text = "def stale_rc():\n    return 1\n"
    _rc_write(tmp_path, "src/stale_rc.py", text)
    _rc_seed(real_db, "stale_rc", "src/stale_rc.py", 1, 2, code_hash="0" * 64)

    out = server.memory_read_code([{"symbol": "stale_rc"}])
    r = out["results"][0]
    assert r["ok"] is True
    assert r["text"] == text
    assert r["stale"] is True
    assert r["stale_reason"] == "snippet_hash_mismatch"


def test_read_code_stale_mtime_fallback(real_db, tmp_path):
    _rc_write(tmp_path, "src/old_rc.py", "a = 1\nb = 2\n")
    _rc_seed(real_db, "old_fn_rc", "src/old_rc.py", 1, 2,
             last_seen="2000-01-01T00:00:00+00:00")
    _rc_write(tmp_path, "src/new_rc.py", "a = 1\nb = 2\n")
    _rc_seed(real_db, "new_fn_rc", "src/new_rc.py", 1, 2,
             last_seen="2999-01-01T00:00:00+00:00")

    out = server.memory_read_code([
        {"file_path": "src/old_rc.py", "line_start": 1, "line_end": 2},
        {"file_path": "src/new_rc.py", "line_start": 1, "line_end": 2},
    ])
    old, new = out["results"]
    assert old["ok"] and old["stale"] is True
    assert old["stale_reason"] == "file_modified_since_sync"
    assert new["ok"] and new["stale"] is False
    assert new["stale_reason"] is None


def test_read_code_batch_error_isolation(real_db, tmp_path):
    text = "x = 1\ny = 2\n"
    _rc_write(tmp_path, "src/iso_rc.py", text)
    _rc_seed(real_db, "iso_rc", "src/iso_rc.py", 1, 2,
             code_hash=_rc_hash(text, 1, 2))

    out = server.memory_read_code([
        {"symbol": "iso_rc"},
        {"symbol": "no_such_rc_xyz"},
        {"file_path": "src/iso_rc.py", "line_start": 2, "line_end": 2},
    ])
    assert [r["ok"] for r in out["results"]] == [True, False, True]
    assert out["results"][1]["error"] == "symbol_not_found"
    assert out["results"][2]["text"] == "y = 2\n"


def test_read_code_total_budget(real_db, tmp_path):
    _rc_write(tmp_path, "src/budget_rc.py", "".join("x" * 100 + "\n" for _ in range(10)))

    out = server.memory_read_code([
        {"file_path": "src/budget_rc.py", "line_start": 1, "line_end": 5},
        {"file_path": "src/budget_rc.py", "line_start": 6, "line_end": 10},
    ], max_total_chars=1000)
    first, second = out["results"]
    assert first["ok"] is True
    assert len(first["text"]) == 505
    assert second["ok"] is False
    assert second["error"] == "total_budget_exceeded"
    assert "text" not in second
    assert out["total_chars"] == 505


def test_read_code_invalid_spec(real_db):
    out = server.memory_read_code([
        {"symbol": "x", "file_path": "f.py", "line_start": 1, "line_end": 2},
        {},
    ])
    assert [r["ok"] for r in out["results"]] == [False, False]
    assert [r["error"] for r in out["results"]] == ["invalid_spec", "invalid_spec"]


def test_read_code_invalid_range(real_db, tmp_path):
    _rc_write(tmp_path, "src/range_rc.py", "a = 1\n")
    out = server.memory_read_code(
        [{"file_path": "src/range_rc.py", "line_start": "top", "line_end": 2}])
    r = out["results"][0]
    assert r["ok"] is False
    assert r["error"] == "invalid_range"


def test_read_code_path_escape_dotdot_and_absolute(real_db, tmp_path):
    outside = os.path.abspath(os.path.join(str(tmp_path), "..", "..", "rc_escape_outside.py"))
    out = server.memory_read_code([
        {"file_path": "src/../../rc_escape.py", "line_start": 1, "line_end": 5},
        {"file_path": outside, "line_start": 1, "line_end": 5},
    ])
    assert [r["ok"] for r in out["results"]] == [False, False]
    assert [r["error"] for r in out["results"]] == ["path_escape", "path_escape"]


def test_read_code_path_escape_symlink(real_db, tmp_path):
    import tempfile
    fd, target = tempfile.mkstemp(suffix="_rc_outside.py")
    os.close(fd)
    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("secret = 1\n")
        link = str(tmp_path / "link_rc.py")
        try:
            os.symlink(target, link)
        except OSError:
            pytest.skip("symlinks unavailable on this platform")
        out = server.memory_read_code(
            [{"file_path": "link_rc.py", "line_start": 1, "line_end": 5}])
        assert out["results"][0]["ok"] is False
        assert out["results"][0]["error"] == "path_escape"
    finally:
        try:
            os.unlink(target)
        except OSError:
            pass


def test_uri_to_path_and_resolve_db(tmp_path):
    proj = tmp_path / "custom_proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    db_file = proj / ".cognigraph" / "memory.db"
    db_file.parent.mkdir()
    db_file.write_text("", encoding="utf-8")

    assert server._uri_to_path(f"file:///{proj.as_posix()}") == proj
    assert server._resolve_db_from_root(proj) == str(db_file)


@pytest.mark.anyio
async def test_dynamic_call_tool_routes_by_roots(tmp_path, monkeypatch):
    proj_a = tmp_path / "proj_a"
    proj_a.mkdir()
    db_a = proj_a / ".cognigraph" / "memory.db"
    store_a = MemoryStore(str(db_a))
    store_a.write("component", "Alpha component in project A")
    store_a.close()

    class FakeRoot:
        def __init__(self, uri):
            self.uri = uri

    class FakeRootsResult:
        def __init__(self, roots):
            self.roots = roots

    class FakeSession:
        async def list_roots(self):
            return FakeRootsResult([FakeRoot(f"file:///{proj_a.as_posix()}")])

    class FakeContext:
        session = FakeSession()

    monkeypatch.setattr(server.mcp, "get_context", lambda: FakeContext())
    res = await server.mcp.call_tool("memory_stats", {})
    import json
    data = json.loads(res[0].text)
    assert data["db_path"] == str(db_a)
    assert data["total_active"] == 1


@pytest.mark.anyio
async def test_dynamic_call_tool_routes_by_file_path(tmp_path, monkeypatch):
    proj_b = tmp_path / "proj_b"
    proj_b.mkdir()
    (proj_b / ".git").mkdir()
    src_dir = proj_b / "src"
    src_dir.mkdir()
    sample_file = src_dir / "service.py"
    sample_file.write_text("def run(): pass\n", encoding="utf-8")

    db_b = proj_b / ".cognigraph" / "memory.db"
    store_b = MemoryStore(str(db_b))
    store_b.write("decision", "Beta decision in project B", file_path="src/service.py")
    store_b.close()

    class EmptyContext:
        session = None

    monkeypatch.setattr(server.mcp, "get_context", lambda: EmptyContext())
    res = await server.mcp.call_tool("memory_get_file_context", {"file_path": str(sample_file)})
    import json
    data = json.loads(res[0].text)
    assert "Beta decision" in data.get("briefing_markdown", "")

