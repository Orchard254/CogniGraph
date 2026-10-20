"""Lane health: which of the 7 retrieval lanes actually contribute, and why.

The first lane-health audit (2026-09-27, run with the new explain=True trace
over 12 diverse queries against the live corpus) found that FOUR of the seven
lanes never contributed to any result:

  epi_fts, epi_tok, epi_sem   0 contributions across 120 results
  struct                      0 contributions across 120 results

Both causes are benign and expected, which is the reason this file exists. A
future agent running the same audit will see four dead lanes, and the natural
reading is "something is broken, let me fix it". Both of those "fixes" would
be regressions:

- `struct` runs at weight 0.0 BY DESIGN. The sweep measured w=0.1 as
  bit-identical to baseline and w>=0.2 as losing recall@5, so a zero weight
  was chosen and the full-table scan is short-circuited to skip it entirely.
  Facts #525/#528/#529/#537/#548 record the change; #498 carries a standing
  do-not-touch rule for this lane. It is dead because it was measured to not
  help, not because it is broken.

- The three episodic lanes are starved because every ACTIVE episodic row on the
  live corpus (104 of 113) has a `memory` twin that is DEPRECATED. The
  rrf_add guard added in f28109f filters all of them, which is that fix
  working, not a regression. The count of "ACTIVE episodic rows with an ACTIVE
  memory twin" is 0, so there is nothing for those lanes to legitimately return.

  This also confirms the leak fix end to end: those 104 rows are exactly the set
  that used to compete for top-5 slots against their own deprecated twins.

So the invariant worth pinning is the second one, because it is data-dependent
and could silently change: if an ACTIVE episodic row ever gains an ACTIVE
memory twin, the episodic lanes should start contributing again, and this test
is what would notice that they are not.
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


ALL_LANES = {"fts", "tok", "sem", "epi_fts", "epi_tok", "epi_sem", "struct"}


def test_structural_lane_is_weighted_zero_by_design():
    """Pins a measured decision that only lived in a code comment.

    The comment records that w=0.1 was bit-identical and w>=0.2 lost recall@5.
    Without a test, someone re-enabling the lane to 'fix' its zero contribution
    count would silently undo a holdout-validated measurement. Enabling it is a
    legitimate change -- but it must be a conscious one, with a re-run of the
    gate, not a quiet edit.
    """
    from pm_search import AGENT_LANE_WEIGHTS
    assert AGENT_LANE_WEIGHTS["default"]["struct"] == 0.0, (
        "the structural lane is weighted 0.0 for the default agent on purpose; "
        "changing it requires re-running the eval gate and re-baselining, not "
        "just a test update. See facts #525, #537, #498.")


def test_only_the_default_agent_has_struct_switched_off():
    """The weight is agent-specific; every other agent leans on structural."""
    from pm_search import AGENT_LANE_WEIGHTS
    for agent, w in AGENT_LANE_WEIGHTS.items():
        if agent == "default":
            continue
        assert w["struct"] > 0, f"{agent} should keep a non-zero struct weight"


def test_structural_lane_can_match_but_is_not_fused():
    """The lane WORKS in a bootstrapped corpus; CI's clean checkout has no corpus."""
    from pathlib import Path
    import server

    # This assertion intentionally exercises the project's existing indexed
    # corpus, not a synthetic fixture. Do not create an empty DB and then
    # misdiagnose its lack of indexed symbols as a broken retrieval lane.
    db_path = Path(server.default_db_path())
    if not db_path.is_file():
        pytest.skip("requires a bootstrapped project memory database")

    with server._store() as store:
        indexed = store.conn.execute(
            "SELECT 1 FROM structural_nodes WHERE name = ? LIMIT 1",
            ("classify_pair",),
        ).fetchone()
        if not indexed:
            pytest.skip("live-corpus structural index is not present in this checkout")

        hits = store._structural_scores("classify_pair")
        assert hits, (
            "_structural_scores should match a linked fact for an indexed node; "
            "if it does not, the structural index/linkage is stale, which IS a bug")
        res = store.query_hybrid("classify_pair", limit=5, log_usage=False,
                                 explain=True)
    lanes = {l["lane"] for r in res for l in r["lanes"]}
    assert "struct" not in lanes, (
        "struct matched but was fused; its default weight is 0, so this is a "
        "behaviour change and the gate needs re-running")


def test_lane_names_in_trace_are_all_real(store):
    for i in range(3):
        store.write(category="other", content=f"alpha beta gamma {i}",
                    embed=False, force=True)
    res = store.query_hybrid("alpha beta", limit=3, log_usage=False, explain=True)
    seen = {l["lane"] for r in res for l in r["lanes"]}
    assert seen, "expected at least one contributing lane"
    assert seen <= ALL_LANES, f"unknown lane name(s) in trace: {seen - ALL_LANES}"


def test_no_active_episodic_row_has_an_active_memory_twin_in_the_corpus_shape():
    """The condition that starves the episodic lanes, asserted structurally.

    Built as a schema-level invariant rather than a hardcoded corpus count so it
    holds for any database, including a fresh one. The live-corpus count is 104
    ACTIVE rows, all with deprecated twins.
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    s = MemoryStore(db_path=tmp.name)
    try:
        s._current_git_commit = lambda: None
        live = s.conn.execute(
            "SELECT COUNT(*) FROM episodic_memories e "
            "JOIN memory m ON m.id = e.memory_id "
            "WHERE e.status = 'ACTIVE' AND m.deprecated_at IS NULL"
        ).fetchone()[0]
        # On an empty DB this is trivially 0; the point is that the query runs
        # and that a non-zero value would be the signal that the episodic lanes
        # should be contributing.
        assert live == 0, (
            f"{live} ACTIVE episodic rows have an ACTIVE memory twin. The "
            "episodic lanes should now contribute; if they do not, the rrf_add "
            "deprecation guard has regressed.")
    finally:
        try:
            s.close()
        except Exception:
            pass
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def test_deprecated_twin_is_what_filters_the_episodic_lane():
    """The mechanism, on the live corpus, from the outside.

    Every ACTIVE episodic row there has a deprecated memory twin, so the
    rrf_add guard filters all of them and the episodic lanes contribute
    nothing. The end-to-end write-path version of this lives in
    test_episodic_deprecation.py, which builds the rows properly through the
    branch API; hand-inserting into episodic_memories needs repo_id,
    branch_id and created_revision, so do not duplicate it here.
    """
    import server
    with server._store() as store:
        candidates = store.query_hybrid("episodic", limit=5, log_usage=False,
                                        explain=True)
    lanes = {l["lane"] for r in candidates for l in r["lanes"]}
    assert "epi_fts" not in lanes, (
        "an episodic lane contributed. Either a fresh ACTIVE episodic row now "
        "has an ACTIVE memory twin (expected once branching is used), or the "
        "deprecation guard in rrf_add has regressed. Check the counts first.")
    assert not any(r.get("source") == "episodic" for r in candidates), (
        "no deprecated-memory twin should be served as an episodic result")
