"""Audit sweeper: audit() must find stale/contradictory ACTIVE fact pairs.

Four defects in the original audit() that these tests pin down:

1. TRUNCATION. It read facts via get_state(limit=MAX_LIMIT) where
   MAX_LIMIT == 200, so on a 508-fact corpus it silently examined only the
   first 200 and reported "Total Active Checked : 200".
2. LEXICAL-ONLY. It compared tokenized Jaccard >= 0.65, so pairs that are
   near-identical in MEANING but differently worded (the common case for
   stale state facts) scored below threshold and were never flagged.
3. STUB BLIND SPOT. It had no template-stub exclusion, so whole-file
   template stubs with identical boilerplate ("Directory '.zed/': 1 files"
   vs "Directory '.claude/': 1 files") were reported as duplicates of each
   other -- a false positive, and the only thing the live audit found.
4. CATEGORY GATE. It only compared facts within the SAME category, so a
   stale 'state' fact contradicting a fresh 'component' fact was invisible.

audit() stays strictly read-only: it reports, it never supersedes.

Run with: python -m pytest test_audit_sweeper.py -v
"""
import os
import tempfile

import numpy as np
import pytest

from memory_store import MemoryStore
from pm_common import _vec_to_blob


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


DIM = 384


def _put_embedding(store, fact_id, vec):
    """Store a raw float32 embedding blob for a fact (skips the ML model)."""
    v = np.asarray(vec, dtype=np.float32)
    v = v / (np.linalg.norm(v) + 1e-9)  # audit() assumes L2-normalized
    store.conn.execute(
        "INSERT OR REPLACE INTO memory_embeddings (fact_id, model, embedding) "
        "VALUES (?, ?, ?)",
        (fact_id, "test:unit", _vec_to_blob(v)),
    )


def _pair_vectors(cos):
    """Two DIM-dim unit vectors whose cosine similarity is exactly `cos`.

    The memory_embeddings column is a sqlite-vec virtual table that rejects any
    vector that is not exactly 384 dimensions, so tests cannot use short
    2/4-dim vectors. a = e0, b = cos*e0 + sqrt(1-cos^2)*e1 gives cos(a, b) = cos.
    """
    a = np.zeros(DIM, dtype=np.float32)
    a[0] = 1.0
    b = np.zeros(DIM, dtype=np.float32)
    b[0] = cos
    b[1] = np.sqrt(max(0.0, 1.0 - cos * cos))
    return a, b


def _put_pair(store, id_a, id_b, cos):
    va, vb = _pair_vectors(cos)
    _put_embedding(store, id_a, va)
    _put_embedding(store, id_b, vb)


def _pair_of(store, res, a, b):
    """Find the reported pair matching (a, b) in either order."""
    for item in res["potential_duplicates"]:
        if {item["fact1_id"], item["fact2_id"]} == {a, b}:
            return item
    return None


# --- 1. truncation -------------------------------------------------------

def test_audit_examines_more_than_max_limit(temp_store):
    """The 200-row cap silently dropped 60% of the live corpus."""
    for i in range(210):
        temp_store.write(
            category="other",
            content=f"distinct fact number {i} about topic {i * 7 % 211}",
            embed=False, force=True,
        )
    res = temp_store.audit()
    assert res["total_active_checked"] == 210


# --- 2. template stubs must never be reported ----------------------------

def test_audit_ignores_identical_template_stubs(temp_store):
    """The live audit's ONLY hit was stub boilerplate across two directories."""
    a = temp_store.write(
        category="other", content="Directory '.zed/': 1 files, ~0 code lines",
        embed=False, force=True)
    b = temp_store.write(
        category="other", content="Directory '.claude/': 1 files, ~0 code lines",
        embed=False, force=True)
    res = temp_store.audit()
    assert _pair_of(temp_store, res, a, b) is None
    assert res["potential_duplicates_count"] == 0


def test_audit_ignores_source_file_stubs(temp_store):
    a = temp_store.write(
        category="other", content="Source file: wire.py (python, 761 lines, hash b855f304)",
        embed=False, force=True)
    b = temp_store.write(
        category="other", content="Source file: wire.py (python, 761 lines, hash b855f304)",
        embed=False, force=True)
    res = temp_store.audit()
    assert _pair_of(temp_store, res, a, b) is None


# --- 3. semantic detection, incl. cross-category ------------------------

def test_audit_flags_cross_category_semantic_duplicate(temp_store):
    """A stale 'state' fact and a fresh 'component' fact saying the same thing."""
    a = temp_store.write(
        category="state",
        content="README tool table was regenerated to 15 tools at commit ba517be",
        embed=False, force=True)
    b = temp_store.write(
        category="component",
        content="README tool table regenerated to 15 tools via gen_tool_table at ba517be",
        embed=False, force=True)
    _put_pair(temp_store, a, b, 0.95)

    res = temp_store.audit()
    hit = _pair_of(temp_store, res, a, b)
    assert hit is not None, "cross-category semantic duplicate was missed"
    assert hit["cross_category"] is True
    assert hit["method"] == "semantic"
    assert hit["similarity"] >= 0.80


def test_audit_respects_semantic_threshold(temp_store):
    """Unrelated facts must not be linked just because they both have vectors."""
    a = temp_store.write(category="other", content="watcher debounce is 1.5 seconds",
                         embed=False, force=True)
    b = temp_store.write(category="other", content="wire writes agent config atomically",
                         embed=False, force=True)
    _put_pair(temp_store, a, b, 0.0)

    res = temp_store.audit()
    assert _pair_of(temp_store, res, a, b) is None


def test_audit_honors_explicit_threshold(temp_store):
    a = temp_store.write(category="other", content="alpha bravo charlie delta echo",
                         embed=False, force=True)
    b = temp_store.write(category="other", content="alpha bravo charlie delta foxtrot",
                         embed=False, force=True)
    _put_pair(temp_store, a, b, 0.87)

    assert _pair_of(temp_store, temp_store.audit(), a, b) is not None
    assert _pair_of(temp_store, temp_store.audit(semantic_threshold=0.95), a, b) is None


# --- 4. lexical fallback for facts with no embedding ---------------------

def test_audit_falls_back_to_jaccard_without_embeddings(temp_store):
    a = temp_store.write(
        category="decision",
        content="npm test runs node --test glob for test-js instead of bare directory",
        embed=False, force=True)
    b = temp_store.write(
        category="decision",
        content="npm test runs node --test glob for test-js instead of the bare directory",
        embed=False, force=True)
    res = temp_store.audit()
    hit = _pair_of(temp_store, res, a, b)
    assert hit is not None
    assert hit["method"] == "lexical"


def test_audit_ignores_deprecated_pairs(temp_store):
    a = temp_store.write(category="other", content="gamma delta epsilon zeta eta theta",
                         embed=False, force=True)
    b = temp_store.write(category="other", content="gamma delta epsilon zeta eta theta",
                         embed=False, force=True)
    temp_store.conn.execute(
        "UPDATE memory SET deprecated_at='2026-01-01T00:00:00+00:00' WHERE id=?", (b,))
    res = temp_store.audit()
    assert _pair_of(temp_store, res, a, b) is None
    assert res["total_active_checked"] == 1


# --- contract guarantees ------------------------------------------------

def test_audit_is_read_only(temp_store):
    """The sweeper reports; a human decides. Nothing may be mutated."""
    a = temp_store.write(category="other", content="identical wording here for both facts",
                         embed=False, force=True)
    b = temp_store.write(category="other", content="identical wording here for both facts",
                         embed=False, force=True)
    _put_pair(temp_store, a, b, 1.0)

    def snapshot():
        return temp_store.conn.execute(
            "SELECT id, category, content, deprecated_at, version, access_count "
            "FROM memory ORDER BY id").fetchall()

    before = snapshot()
    temp_store.audit()
    after = snapshot()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_audit_keeps_backward_compatible_keys(temp_store):
    """memory_cli.py audit and test_memory_cli.py depend on these keys."""
    res = temp_store.audit()
    for key in ("total_active_checked", "potential_duplicates_count",
                "potential_duplicates"):
        assert key in res
    for item in res["potential_duplicates"]:
        for key in ("fact1_id", "fact1_content", "fact2_id", "fact2_content",
                    "similarity", "category"):
            assert key in item


def test_audit_reports_method_breakdown(temp_store):
    a = temp_store.write(category="other", content="semantic pair alpha beta gamma delta",
                         embed=False, force=True)
    b = temp_store.write(category="other", content="semantic pair alpha beta gamma delta",
                         embed=False, force=True)
    _put_pair(temp_store, a, b, 1.0)
    res = temp_store.audit()
    assert res["method"] == "semantic"
    assert res["thresholds"] == {"semantic": 0.80, "lexical": 0.65}
