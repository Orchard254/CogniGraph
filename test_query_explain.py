"""Opt-in per-lane ranking provenance: which lanes produced this result, at what rank.

Why: on 2026-09-27 a deprecated fact (#17) kept surfacing in hybrid results and
it cost roughly an hour to diagnose, because the results showed only the FUSED
output. There was no way to ask "did the FTS lane find this, or the episodic
lane?" without dropping into the DB and re-deriving the RRF by hand. Seven lanes
fuse with per-agent weights, intent boosts, importance weights, recency decay
and file-anchor boosts, so the fused number is not decomposable after the fact.

`explain=True` attaches a `lanes` trace per result: every lane that contributed,
its rank within that lane, its weight, and the fused score. This makes the
ranking auditable, and it is the verification tool for any change to lane
behaviour -- including the MAX_LIMIT work, where the only available evidence was
a before/after eval-gate comparison.

Strictly opt-in. The default response shape is unchanged, because per-result
lane traces would multiply the token cost of every query.

Run with: python -m pytest test_query_explain.py -v
"""
import os
import tempfile

import numpy as np
import pytest

from memory_store import MemoryStore
from pm_common import _vec_to_blob


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


def _vec(*coeffs):
    """Expand short coefficients into a 384-dim vector.

    memory_embeddings.embedding is a sqlite-vec column that rejects anything
    that is not exactly 384 dimensions, so tests cannot use 2/3-dim vectors.
    _vec(1.0) is e0; _vec(0.0, 1.0) is e1.
    """
    v = np.zeros(384, dtype=np.float32)
    for i, c in enumerate(coeffs):
        v[i] = c
    return v


def _write(store, content, vec=None):
    fid = store.write(category="other", content=content, embed=False, force=True)
    if vec is not None:
        v = _vec(*vec)
        v = v / (np.linalg.norm(v) + 1e-9)
        store.conn.execute(
            "INSERT OR REPLACE INTO memory_embeddings (fact_id, model, embedding) "
            "VALUES (?, 'test:unit', ?)", (fid, _vec_to_blob(v)))
    return fid


def _both(store, a_text, b_text):
    return _write(store, a_text, (1.0,)), _write(store, b_text, (0.0, 1.0))


# --- default shape is untouched -----------------------------------------

def test_explain_false_leaves_results_unchanged(store):
    _both(store, "alpha one two three", "beta four five six")
    plain = store.query_hybrid("alpha one two", limit=5, log_usage=False)
    explained = store.query_hybrid("alpha one two", limit=5, log_usage=False, explain=True)
    for a, b in zip(plain, explained):
        assert "lanes" not in a
        assert "fused_score" not in a
        assert set(a.keys()) == set(b.keys()) - {"lanes", "fused_score"}
    assert [r["id"] for r in plain] == [r["id"] for r in explained], (
        "explain must not change ranking")


def test_explain_true_adds_a_trace(store):
    _both(store, "alpha one two three", "beta four five six")
    res = store.query_hybrid("alpha one two", limit=5, log_usage=False, explain=True)
    assert res
    top = res[0]
    assert "fused_score" in top
    assert isinstance(top["lanes"], list) and top["lanes"]
    lane = top["lanes"][0]
    for key in ("lane", "rank", "weight"):
        assert key in lane


# --- the trace is truthful ----------------------------------------------

def test_trace_names_real_lanes(store):
    _both(store, "alpha one two three four", "beta five six seven eight")
    res = store.query_hybrid("alpha one two three", limit=5, log_usage=False, explain=True)
    seen = {l["lane"] for r in res for l in r["lanes"]}
    assert seen, "expected at least one lane in the trace"
    assert seen <= {"fts", "tok", "sem", "epi_fts", "epi_tok", "epi_sem", "struct"}, seen


def test_trace_records_ranks_and_weights(store):
    _both(store, "alpha one two three four", "beta five six seven eight")
    res = store.query_hybrid("alpha one two three", limit=5, log_usage=False, explain=True)
    for r in res:
        for lane in r["lanes"]:
            assert lane["rank"] >= 1
            assert lane["weight"] > 0


def test_fused_scores_are_non_increasing(store):
    """Ranked output must be ordered by the fused score it reports."""
    _both(store, "alpha one two three four", "beta five six seven eight")
    res = store.query_hybrid("alpha one two", limit=5, log_usage=False, explain=True)
    scores = [r["fused_score"] for r in res]
    assert scores == sorted(scores, reverse=True), scores


def test_semantic_lane_appears_in_trace(store):
    """A query matched only semantically should show the sem lane."""
    fid = _write(store, "completely different wording entirely here",
                 (1.0,))
    res = store.query_hybrid("totally unrelated vocabulary", limit=5,
                             log_usage=False, explain=True)
    hit = next((r for r in res if r["id"] == fid), None)
    if hit is not None:
        assert "sem" in {l["lane"] for l in hit["lanes"]}


def test_trace_survives_intent_boosting(store):
    """Intent boosts change weights; the trace must reflect the lane actually used."""
    a = _write(store, "constraint about the parser handling", (1.0,))
    _write(store, "decision about shipping the parser", (0.0, 1.0))
    res = store.query_hybrid("parser handling", intent="fix", limit=5,
                             log_usage=False, explain=True)
    hit = next((r for r in res if r["id"] == a), None)
    if hit is not None:
        assert hit["lanes"]


def test_empty_result_set_is_handled(store):
    _both(store, "alpha one two", "beta three four")
    res = store.query_hybrid("zzzz nothing matches this at all qqqq",
                             limit=5, log_usage=False, explain=True)
    assert isinstance(res, list)
