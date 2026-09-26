"""Episodic lanes must not resurrect facts deprecated in the `memory` table.

Bug: query_hybrid's episodic lanes (1b FTS5, 2b token Jaccard, 3b semantic
cosine) filtered only `episodic_memories.status='ACTIVE'`. Deprecating a fact
via MemoryStore.deprecate() sets `memory.deprecated_at` and leaves the
episodic row ACTIVE, so a deprecated fact kept ranking in hybrid results --
the default path behind memory_query, memory_get_context and prepare_task.

Proved on the live corpus before the fix: fact #17 was deprecated in `memory`
(lexical search correctly excluded it) yet a hybrid query returned it.

`memory` is the single source of truth for whether a fact is active. The
episodic store keeps the historical/branch copy; it must not decide
liveness on its own.

Run with: python -m pytest test_episodic_deprecation.py -v
"""
import os
import tempfile

import pytest

from memory_store import MemoryStore


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


def _seed_episodic_copy(store, memory_id, content):
    """Insert the episodic/branch-side copy of a fact, as genesis migration did.

    A fresh temp DB has the episodic schema but no repository rows, so seed
    repo + canonical root branch on first use. created_revision must equal
    repositories.current_revision (trg_episodic_insert_revision) and the
    branch's repo_id must match (trg_episodic_repo_isolation).
    """
    now = "2026-01-01T00:00:00+00:00"
    row = store.conn.execute(
        "SELECT repo_id FROM repositories ORDER BY repo_id LIMIT 1").fetchone()
    if row is None:
        cur = store.conn.execute(
            "INSERT INTO repositories (root_path, current_revision, created_at) "
            "VALUES ('/tmp/cognigraph-test', 1, ?)", (now,))
        repo_id = cur.lastrowid
        cur = store.conn.execute(
            "INSERT INTO memory_branches (repo_id, parent_branch_id, is_root, "
            "base_revision, base_timestamp, status, created_at) "
            "VALUES (?, NULL, 1, 1, ?, 'ACTIVE', ?)", (repo_id, now, now))
        branch_id = cur.lastrowid
    else:
        repo_id = row["repo_id"]
        branch_id = store.conn.execute(
            "SELECT branch_id FROM memory_branches WHERE repo_id=? AND is_root=1",
            (repo_id,)).fetchone()["branch_id"]

    rev = store.conn.execute(
        "SELECT current_revision FROM repositories WHERE repo_id=?",
        (repo_id,)).fetchone()[0]
    store.conn.execute(
        "INSERT OR REPLACE INTO episodic_memories "
        "(memory_id, repo_id, branch_id, category, content, confidence, status, "
        " created_revision, closed_revision, created_at, importance) "
        "VALUES (?, ?, ?, 'other', ?, 'assumed', 'ACTIVE', ?, NULL, ?, 3)",
        (memory_id, repo_id, branch_id, content, rev, now))


def _hybrid_ids(store, text, limit=20):
    return {r["id"] for r in store.query_hybrid(text, limit=limit, log_usage=False)}


def test_deprecated_fact_not_returned_by_hybrid(temp_store):
    """The core regression: deprecation must remove a fact from hybrid results."""
    content = ("zqxjkvw unique token blorptastic narrative that only exists here "
               "so the episodic lane can match it deterministically")
    fid = temp_store.write(category="other", content=content, embed=False, force=True)
    assert fid > 0
    _seed_episodic_copy(temp_store, fid, content)

    # Sanity: findable while active.
    assert fid in _hybrid_ids(temp_store, "zqxjkvw blorptastic")

    temp_store.deprecate(fid, "test: should disappear from hybrid search")

    assert fid not in _hybrid_ids(temp_store, "zqxjkvw blorptastic"), (
        "deprecated fact resurfaced through the episodic lanes")


def test_episodic_copy_stays_active_but_is_filtered(temp_store):
    """Documents WHY the filter is needed: deprecate() does not touch episodic."""
    content = "wqqxrt another uniquely tokenisable episodic leakage probe sentence"
    fid = temp_store.write(category="other", content=content, embed=False, force=True)
    _seed_episodic_copy(temp_store, fid, content)
    temp_store.deprecate(fid, "test: liveness check")

    ep = temp_store.conn.execute(
        "SELECT status FROM episodic_memories WHERE memory_id=?", (fid,)).fetchone()
    mem = temp_store.conn.execute(
        "SELECT deprecated_at FROM memory WHERE id=?", (fid,)).fetchone()

    assert ep is not None and ep["status"] == "ACTIVE", (
        "premise of the fix: the episodic row is still ACTIVE")
    assert mem["deprecated_at"] is not None
    assert fid not in _hybrid_ids(temp_store, "wqqxrt tokenisable")


def test_active_episodic_fact_is_still_served(temp_store):
    """Guard against over-filtering: legitimate episodic hits must survive."""
    content = "ppqqzz live episodic fact that must remain discoverable after the fix"
    fid = temp_store.write(category="other", content=content, embed=False, force=True)
    _seed_episodic_copy(temp_store, fid, content)

    assert fid in _hybrid_ids(temp_store, "ppqqzz discoverable")


def test_episodic_id_with_no_memory_row_is_still_served(temp_store):
    """An episodic row with no `memory` counterpart must not be dropped.

    Guards the filter against treating a missing row as a deprecated row.
    """
    _seed_episodic_copy(
        temp_store, 999999, "nnmmkk orphan episodic fact with no memory row at all")

    assert 999999 in _hybrid_ids(temp_store, "nnmmkk orphan"), (
        "episodic-only id was wrongly filtered out")


def test_lexical_and_hybrid_agree_on_deprecation(temp_store):
    """The invariant that was broken: both retrieval paths must agree."""
    keep = "keepme alpha one two three four five six seven eight"
    drop = "dropme alpha one two three four five six seven eight"
    k = temp_store.write(category="other", content=keep, embed=False, force=True)
    d = temp_store.write(category="other", content=drop, embed=False, force=True)
    _seed_episodic_copy(temp_store, k, keep)
    _seed_episodic_copy(temp_store, d, drop)

    temp_store.deprecate(d, "test: divergence check")

    lexical = {r["id"] for r in temp_store.query("alpha one two three", limit=20)}
    hybrid = _hybrid_ids(temp_store, "alpha one two three")

    assert k in hybrid and k in lexical
    assert d not in hybrid and d not in lexical
    assert (d in hybrid) == (d in lexical), (
        "lexical and hybrid disagreed about a deprecated fact")
