"""Expanded coverage for newer memory features."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server
from memory_store import MemoryStore
from structural_extract import extract_structures


@pytest.fixture(autouse=True)
def _hermetic_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "guard.db"))
    monkeypatch.setattr(server, "_ensure_first_time_setup", lambda: None)


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


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    db = str(tmp_path / "rt" / "memory.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)

    def _factory():
        return MemoryStore(db)

    monkeypatch.setattr(server, "_get_store", _factory)
    return db


def test_write_branch_id_and_trust_level_persist(store):
    fid = store.write(
        category="other", content="fact with branch and trust",
        branch_id=7, trust_level=2, force=True, embed=False,
    )
    assert fid and fid > 0
    d = store.get_by_id(fid)
    assert d["branch_id"] == 7
    assert d["trust_level"] == 2


def test_write_created_by_agent_persists(store):
    fid = store.write(
        category="other", content="fact with created_by_agent",
        created_by_agent="test-agent", force=True, embed=False,
    )
    assert fid and fid > 0
    d = store.get_by_id(fid)
    assert d["created_by_agent"] == "test-agent"


def test_memory_search_symbols_returns_expected_symbol(store, tmp_path):
    src = tmp_path / "sample_mod.py"
    src.write_text("def unique_search_symbol_xyz():\n    return 42\n", encoding="utf-8")
    try:
        nodes = extract_structures(src)
    except Exception as exc:
        pytest.skip(f"structural extraction unavailable: {exc}")
    if not nodes:
        pytest.skip("structural index sync requires nodes from tree-sitter; none extracted")
    res = store.sync_structural_nodes("sample_mod.py", nodes, project_root=str(tmp_path))
    assert res is not None
    out = store.search_symbols("unique_search_symbol_xyz")
    assert out["count"] >= 1
    assert any(h.get("name") == "unique_search_symbol_xyz" for h in out["hits"])


def test_resolve_conflict_deprecates_loser_and_creates_edge(store):
    keep_id = store.write(category="other", content="kept fact", force=True, embed=False)
    lose_id = store.write(category="other", content="losing fact", force=True, embed=False)
    res = store.resolve_conflict(keep_id, lose_id, rationale="unit test")
    assert res["status"] == "resolved"
    assert res["keep_id"] == keep_id
    assert res["deprecated_id"] == lose_id
    loser = store.get_by_id(lose_id)
    assert loser is not None
    assert loser["deprecated_at"] is not None
    edges = store.conn.execute(
        "SELECT COUNT(*) AS c FROM memory_edges WHERE source_id=? AND target_id=? AND relation='conflicts_with'",
        (keep_id, lose_id),
    ).fetchone()
    assert edges["c"] >= 1


def test_acquire_and_release_lock_behaviour(store):
    assert store.acquire_lock("res1", "agent-a", 60) is True
    assert store.acquire_lock("res1", "agent-b", 60) is False
    assert store.acquire_lock("res1", "agent-a", 60) is True
    assert store.release_lock("res1", "agent-b") is False
    assert store.release_lock("res1", "agent-a") is True
    assert store.acquire_lock("res1", "agent-b", 60) is True
    store.release_lock("res1", "agent-b")


def test_memory_check_security_returns_status_and_handles_missing_file(real_db, tmp_path):
    missing = str(tmp_path / "definitely_missing_xyz.py")
    out = server.memory_check_security([missing])
    assert isinstance(out, dict)
    assert out.get("status") == "CHECKED"
    assert out.get("hit_count", 0) == 0
    assert any("file not found" in str(lim) for lim in out.get("limitations", []))


def test_memory_check_security_detects_pattern(real_db, tmp_path):
    f = tmp_path / "scan_me.py"
    f.write_text("password = 'supersecret'\neval('1+1')\n", encoding="utf-8")
    out = server.memory_check_security([str(f)])
    assert out.get("status") == "CHECKED"
    assert out.get("hit_count", 0) >= 1
