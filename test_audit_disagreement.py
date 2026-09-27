"""Audit pairs should say whether two similar facts AGREE or DISAGREE.

The audit sweeper answers "these 26 facts are similar." That is not the question
a maintainer has. Reading all 26 on 2026-09-27, most were benign co-topic pairs
-- "added X" beside "committed and pushed X" are two true things, not a conflict
-- and the six real defects were pairs that CONTRADICTED each other:

  14 tools      vs 18 tools        (README tool count)
  "six scripts" vs "seven scripts" (the priming rewrite)
  glob form     vs explicit path   (npm test mechanism)

So classify each pair, and surface only the disagreeing ones. This is the
difference between 26 line-items of reading and 6.

Design constraint learned the hard way earlier today: a classifier that cries
wolf gets ignored. So "unclear" is a first-class answer, only load-bearing
numeric/identifier contradictions count, and a pair with no comparable assertion
is "unclear" rather than guessed at.

Run with: python -m pytest test_audit_disagreement.py -v
"""
import os
import tempfile

import pytest

from memory_store import MemoryStore
from pm_lifecycle import classify_pair


# --- the six real disagreements from the 2026-09-27 corpus --------------

@pytest.mark.parametrize("a,b", [
    ("README tool table verified at 18 tools on the committed tree",
     "README tool table was regenerated to 15 tools at commit ba517be"),
    ("Hardening pass: rewrote all six legacy MCP test scripts",
     "Completed the pass: rewrote all seven legacy MCP test scripts"),
])
def test_detects_disagreement(a, b):
    verdict = classify_pair({"id": 1, "content": a}, {"id": 2, "content": b})
    assert verdict["verdict"] == "disagree", verdict


def test_mechanism_conflict_is_unclear_not_guessed():
    """glob vs explicit-path was a real defect but needs semantics, not numbers.

    Deliberately reported as `unclear` rather than guessed at: detecting it
    would need phrase-level comparison, and a classifier that guesses is one
    nobody trusts. An unclear verdict still puts the pair in front of a human.
    """
    v = classify_pair(
        {"id": 1, "content": "npm test runs node --test glob for test-js"},
        {"id": 2, "content": "node --test uses explicit file path test-js/lib.test.js"})
    assert v["verdict"] == "unclear"


def test_tool_count_disagreement_names_the_unit_and_values():
    v = classify_pair(
        {"id": 1, "content": "README table verified at 18 tools"},
        {"id": 2, "content": "regenerated to 15 tools at ba517be"})
    assert v["verdict"] == "disagree"
    assert v["conflicts"][0]["kind"] == "count"
    assert v["conflicts"][0]["unit"] == "tool"   # normalized to singular
    assert {v["conflicts"][0]["a_value"], v["conflicts"][0]["b_value"]} == {15, 18}


def test_script_count_disagreement():
    v = classify_pair(
        {"id": 1, "content": "rewrote all six legacy MCP test scripts"},
        {"id": 2, "content": "rewrote all seven legacy MCP test scripts"})
    assert v["verdict"] == "disagree"
    assert v["conflicts"][0]["kind"] == "word_number"


# --- benign co-topic pairs must NOT be flagged --------------------------

def test_added_then_committed_is_not_a_disagreement():
    """The single most common benign shape in this corpus."""
    v = classify_pair(
        {"id": 1, "content": "Added wire --force-protocol opt-in: backs up and "
                             "overwrites customized AGENTS.md"},
        {"id": 2, "content": "Committed and pushed --force-protocol feature "
                             "(98de93f) on main"})
    assert v["verdict"] in ("agree", "unclear"), v


def test_same_facts_agree():
    v = classify_pair(
        {"id": 1, "content": "Synced the memory-workflow skill in both scopes"},
        {"id": 2, "content": "Synced the memory-workflow skill in both scopes"})
    assert v["verdict"] == "agree"


def test_no_comparable_assertion_is_unclear_not_guessed():
    v = classify_pair(
        {"id": 1, "content": "per-thread store cache in watcher.py"},
        {"id": 2, "content": "per-thread connections preferred over shared locks"})
    assert v["verdict"] == "unclear"
    assert v["conflicts"] == []


def test_sequential_baselines_are_not_a_disagreement():
    """'baseline is now 141' then 'now 156' is history, not conflict."""
    v = classify_pair(
        {"id": 1, "content": "Suite baseline is now 141 passed (was 107) after "
                             "adding test_wire_bootstrap.py, commit c672c70"},
        {"id": 2, "content": "Suite baseline is now 156 passed (was 141) after "
                             "adding test_memory_cli.py, commit 9d5d3d7"})
    assert v["verdict"] in ("agree", "unclear"), v


def test_set_difference_is_a_conflict_when_neither_side_is_a_timeline():
    """A fact asserting 18 beside one asserting 20 is a real contradiction."""
    v = classify_pair(
        {"id": 1, "content": "README tool table verified at 18 tools on the "
                             "committed tree"},
        {"id": 2, "content": "README tool table verified at 20 tools on the "
                             "committed tree"})
    assert v["verdict"] == "disagree", v
    assert v["conflicts"][0]["unit"] == "tool"


def test_timeline_marker_suppresses_a_set_difference():
    """One side reading as history is enough to withhold the conflict."""
    v = classify_pair(
        {"id": 1, "content": "Tool count was 15 tools before the last regeneration"},
        {"id": 2, "content": "Tool count is now 20 tools"})
    assert v["verdict"] == "unclear", v


def test_historical_fact_that_also_states_the_current_value_is_unclear():
    """#634/#635 shape: one narrates 15-at-the-time AND says 18 is current.

    Not a contradiction -- #635 agrees with #634. Flagging it would be noise, so
    the shared value correctly suppresses the conflict.
    """
    v = classify_pair(
        {"id": 634, "content": "README tool table verified at 18 tools on "
                              "committed tree; the server registers 18 tools"},
        {"id": 635, "content": "README tool table was regenerated to 15 tools at "
                              "commit ba517be; as of 2026-09-15 the committed "
                              "tree registers 18 tools"})
    assert v["verdict"] == "unclear", v


def test_agreement_shape():
    v = classify_pair({"id": 1, "content": "same text here"},
                      {"id": 2, "content": "same text here"})
    for key in ("verdict", "reason", "conflicts", "a_id", "b_id"):
        assert key in v


# --- audit() surfaces them ----------------------------------------------

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


def _embed_pair(store, a_text, b_text, cos=0.95):
    import numpy as np
    from pm_common import _vec_to_blob
    a = store.write(category="other", content=a_text, embed=False, force=True)
    b = store.write(category="other", content=b_text, embed=False, force=True)
    va = np.zeros(384, dtype=np.float32)
    vb = np.zeros(384, dtype=np.float32)
    va[0], va[1] = 1.0, 0.0
    vb[0], vb[1] = cos, np.sqrt(max(0.0, 1 - cos * cos))
    for fid, v in ((a, va), (b, vb)):
        n = v / (np.linalg.norm(v) + 1e-9)
        store.conn.execute(
            "INSERT OR REPLACE INTO memory_embeddings (fact_id, model, embedding) "
            "VALUES (?, 'test:unit', ?)", (fid, _vec_to_blob(n)))
    return a, b


def test_audit_classifies_each_pair(store):
    _embed_pair(store, "README table verified at 18 tools",
                "README table regenerated to 15 tools")
    res = store.audit()
    assert res["potential_duplicates_count"] == 1
    item = res["potential_duplicates"][0]
    assert item["verdict"] == "disagree"
    assert item["conflicts"]


def test_audit_reports_agreement_counts(store):
    _embed_pair(store, "Synced the memory-workflow skill in both scopes",
                "Synced the memory-workflow skill in both scopes")
    _embed_pair(store, "README table verified at 18 tools",
                "README table regenerated to 15 tools")
    res = store.audit()
    # Four facts produce six pairs (the two intended ones plus cross-pairs), so
    # assert the invariants that matter rather than an exact verdict histogram.
    assert res["pairs_examined"] >= 2
    assert res["disagreement_count"] == 1
    assert res["pair_verdicts"]["disagree"] == 1
    assert res["pair_verdicts"]["agree"] >= 1
    assert sum(res["pair_verdicts"].values()) == res["pairs_examined"]


def test_audit_can_return_only_disagreements(store):
    _embed_pair(store, "Synced the memory-workflow skill in both scopes",
                "Synced the memory-workflow skill in both scopes")
    _embed_pair(store, "README table verified at 18 tools",
                "README table regenerated to 15 tools")
    res = store.audit(disagreements_only=True)
    assert len(res["potential_duplicates"]) == 1
    assert res["potential_duplicates"][0]["verdict"] == "disagree"
    assert res["disagreements_only"] is True
    # The count reflects the filtered set, but pairs_examined still reports the
    # full sweep so the filter is never mistaken for a smaller problem.
    assert res["disagreement_count"] == 1
    assert res["pairs_examined"] >= 2
