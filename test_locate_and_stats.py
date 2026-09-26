"""locate_fact() and a reconcilable stats() breakdown.

Two gaps closed here, both found by being unable to answer a question the store
should have answered:

1. DUAL-STORE MEMBERSHIP WAS INVISIBLE. A fact id can exist in `memory` AND in
   `episodic_memories` at once, with different liveness in each, and nothing
   surfaced that. This is the same class of defect as the retrieval leak fixed
   in f28109f: the system could not tell the agent something it needed, and the
   only reason it was found was luck. locate_fact() answers "where does this id
   live, and is it live?" in one call.

   The distinction it makes explicit: `memory.deprecated_at` is the LIVENESS
   authority. `episodic_memories.status` is BRANCH state (ACTIVE / SUPERSEDED /
   INVALIDATED / ORPHANED) and its triggers enforce that reading -- a status
   update to SUPERSEDED is rejected unless superseded_by_memory_id is set,
   because a deprecation is not a supersession. An episodic row reading ACTIVE
   therefore does NOT mean the fact is served, and query_hybrid agrees: its
   episodic lanes drop any candidate whose `memory` row is deprecated.

2. STATS DID NOT RECONCILE. memory_stats reported 565 active while audit
   reported 456 checked. Both were true and nothing explained the gap, because
   109 of the active facts are whole-file template stubs. stats() now breaks the
   active total down so audit's number is derivable instead of puzzling.

Run with: python -m pytest test_locate_and_stats.py -v
"""
import os
import sqlite3
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


def _seed_repo(store):
    now = "2026-01-01T00:00:00+00:00"
    cur = store.conn.execute(
        "INSERT INTO repositories (root_path, current_revision, created_at) "
        "VALUES ('/tmp/locate-test', 1, ?)", (now,))
    repo_id = cur.lastrowid
    cur = store.conn.execute(
        "INSERT INTO memory_branches (repo_id, parent_branch_id, is_root, "
        "base_revision, base_timestamp, status, created_at) "
        "VALUES (?, NULL, 1, 1, ?, 'ACTIVE', ?)", (repo_id, now, now))
    return repo_id, cur.lastrowid


def _seed_episodic(store, memory_id, repo_id, branch_id, content, status="ACTIVE"):
    rev = store.conn.execute(
        "SELECT current_revision FROM repositories WHERE repo_id=?",
        (repo_id,)).fetchone()[0]
    store.conn.execute(
        "INSERT OR REPLACE INTO episodic_memories "
        "(memory_id, repo_id, branch_id, category, content, confidence, status, "
        " created_revision, closed_revision, created_at, importance) "
        "VALUES (?, ?, ?, 'other', ?, 'assumed', ?, ?, NULL, ?, 3)",
        (memory_id, repo_id, branch_id, content, status, rev, "2026-01-01T00:00:00+00:00"))


# --- locate_fact --------------------------------------------------------

def test_locate_reports_a_live_memory_only_fact(store):
    fid = store.write(category="other", content="memory only fact",
                      embed=False, force=True)
    out = store.locate_fact(fid)
    assert out["fact_id"] == fid
    assert out["in_memory"] is True
    assert out["liveness"] == "active"
    assert out["episodic"] == []
    assert out["liveness_authority"] == "memory.deprecated_at"


def test_locate_reports_dual_membership(store):
    """The whole point: an id in BOTH stores, with liveness from `memory`."""
    repo_id, branch_id = _seed_repo(store)
    content = "fact present in both stores"
    fid = store.write(category="other", content=content, embed=False, force=True)
    _seed_episodic(store, fid, repo_id, branch_id, content)

    out = store.locate_fact(fid)
    assert out["in_memory"] is True
    assert len(out["episodic"]) == 1
    assert out["episodic"][0]["status"] == "ACTIVE"
    # Episodic says ACTIVE, but `memory` is the authority.
    assert out["liveness"] == "active"
    assert out["episodic_active_but_not_served"] is False


def test_locate_flags_deprecated_memory_with_active_episodic(store):
    """The exact shape that caused the retrieval leak, now visible up front."""
    repo_id, branch_id = _seed_repo(store)
    content = "deprecated in memory, still ACTIVE in episodic"
    fid = store.write(category="other", content=content, embed=False, force=True)
    _seed_episodic(store, fid, repo_id, branch_id, content)
    store.deprecate(fid, "test")

    out = store.locate_fact(fid)
    assert out["liveness"] == "deprecated"
    assert out["memory_deprecated_at"] is not None
    assert out["episodic"][0]["status"] == "ACTIVE"
    assert out["episodic_active_but_not_served"] is True, (
        "an ACTIVE episodic row whose memory row is deprecated is NOT served")


def test_locate_unknown_id_is_not_an_error(store):
    out = store.locate_fact(999_999)
    assert out["liveness"] == "absent"
    assert out["in_memory"] is False
    assert out["episodic"] == []


def test_locate_includes_graph_edges(store):
    a = store.write(category="other", content="edge source alpha",
                    embed=False, force=True)
    b = store.write(category="other", content="edge target beta",
                    embed=False, force=True)
    store.link_facts(a, b, "depends_on")
    out = store.locate_fact(a)
    assert any(e["target_id"] == b for e in out["edges"])


# --- stats reconciliation ----------------------------------------------

def test_stats_active_total_splits_into_stubs_and_curated(store):
    store.write(category="component", content="curated component fact",
                embed=False, force=True)
    store.write(category="other",
                content="Source file: wire.py (python, 761 lines, hash abc123)",
                embed=False, force=True)
    store.write(category="other", content="Key file: AGENTS.md",
                embed=False, force=True)

    st = store.stats()
    assert st["total_active"] == 3
    assert st["active_stubs"] == 2
    assert st["active_curated"] == 1
    assert st["active_curated"] + st["active_stubs"] == st["total_active"], (
        "the audit's 'checked' number must be derivable from stats")


def test_stats_excludes_deprecated_from_the_split(store):
    fid = store.write(category="other", content="curated then deprecated",
                      embed=False, force=True)
    store.deprecate(fid, "test")
    st = store.stats()
    assert st["total_active"] == 0
    assert st["active_curated"] == 0
    assert st["active_stubs"] == 0
    assert st["total_deprecated"] == 1


def test_audit_checked_count_equals_stats_curated_on_live_db(store):
    """The reconciliation this fixes: audit's 'checked' == stats' curated."""
    for i in range(5):
        store.write(category="other", content=f"curated {i}", embed=False, force=True)
    for i in range(3):
        store.write(category="other",
                    content=f"Source file: f{i}.py (python, 10 lines, hash h{i})",
                    embed=False, force=True)
    assert store.audit()["total_active_checked"] == store.stats()["active_curated"]


def test_stats_keys_are_backward_compatible(store):
    store.write(category="other", content="a fact", embed=False, force=True)
    st = store.stats()
    for key in ("total_active", "total_deprecated", "total_edges",
                "active_by_category"):
        assert key in st, f"removed pre-existing key: {key}"


def test_stats_on_a_fresh_store_with_no_episodic_tables(tmp_path):
    """No reliance on episodic schema for the split."""
    p = tmp_path / "bare.db"
    s = MemoryStore(db_path=str(p))
    s._current_git_commit = lambda: None
    try:
        st = s.stats()
        assert st["total_active"] == 0
        assert "active_stubs" in st and "active_curated" in st
    finally:
        s.close()
    assert isinstance(sqlite3.connect(str(p)), sqlite3.Connection)
