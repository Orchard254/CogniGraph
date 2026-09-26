"""detect_pollution: find throwaway test/stress artifacts in the live corpus.

Gap: prune_stale selects purely by 90-day inactivity AND only considers
category ('state','other') with importance <= 3. The 8 artifacts cleaned on
2026-09-27 were all category 'component' and had been retrieved that same
session, so prune reported 0 candidates while junk sat in a retrieval-boosted
category occupying result slots. Inactivity-pruning and content-pollution are
different jobs; only the former existed.

Two lessons shape the detection rule (both recorded from earlier sessions):
  1. A loose prefix regex flagged a real decision (#183 'Placeholder GitHub
     URLs removed...'). So markers must be ANCHORED and dominate the content.
  2. Never auto-deprecate. Deciding whether a fact matters needs judgment, so
     this reports a worklist only, like the audit sweeper.

Run with: python -m pytest test_pollution.py -v
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


def _w(store, content, category="component", rationale=None):
    return store.write(category=category, content=content, rationale=rationale,
                       embed=False, force=True)


def test_flags_synthetic_index_fixtures(temp_store):
    a = _w(temp_store, "Batch fact 0")
    b = _w(temp_store, "Stress test fact 1")
    d = _w(temp_store, "fact 12")
    res = temp_store.detect_pollution()
    flagged = {p["id"] for p in res["pollution"]}
    assert {a, b, d} <= flagged


def test_flags_explicit_test_scaffold_phrases(temp_store):
    a = _w(temp_store, "Test fact for MCP verification", rationale="verify tools work")
    b = _w(temp_store, "Latency test fact", rationale="timing")
    c = _w(temp_store, "test batch 1")
    res = temp_store.detect_pollution()
    flagged = {p["id"] for p in res["pollution"]}
    assert {a, b, c} <= flagged


def test_does_not_flag_real_knowledge_starting_with_similar_words(temp_store):
    """Regression guard for the #183 false positive ('Placeholder GitHub URLs')."""
    a = _w(temp_store, "Placeholder GitHub URLs removed from package.json rather than replaced")
    b = _w(temp_store, "Batch size is capped at 500 to bound lock hold time")
    c = _w(temp_store, "Test runner discovers test-js/*.test.js via readdirSync")
    res = temp_store.detect_pollution()
    flagged = {p["id"] for p in res["pollution"]}
    assert a not in flagged and b not in flagged and c not in flagged


def test_ignores_template_stubs(temp_store):
    """Stubs are corpus_hygiene's job, not pollution detection's."""
    _w(temp_store, "Source file: wire.py (python, 761 lines, hash b855f304)")
    res = temp_store.detect_pollution()
    assert res["pollution"] == []


def test_ignores_deprecated_and_non_boosted_categories(temp_store):
    a = _w(temp_store, "Batch fact 0")
    store_dep = temp_store.write(category="component", content="test fact",
                                 embed=False, force=True)
    temp_store.deprecate(store_dep, "test")
    _w(temp_store, "test batch 1", category="decision")
    res = temp_store.detect_pollution()
    flagged = {p["id"] for p in res["pollution"]}
    assert a in flagged
    assert store_dep not in flagged, "already-deprecated rows must not be reported"


def test_is_read_only(temp_store):
    _w(temp_store, "Batch fact 0")
    _w(temp_store, "test fact")

    def snap():
        return temp_store.conn.execute(
            "SELECT id, category, content, deprecated_at, version FROM memory "
            "ORDER BY id").fetchall()

    before = [tuple(r) for r in snap()]
    temp_store.detect_pollution()
    after = [tuple(r) for r in snap()]
    assert before == after


def test_reports_reason_and_counts(temp_store):
    _w(temp_store, "Batch fact 0")
    res = temp_store.detect_pollution()
    assert res["read_only"] is True
    assert res["count"] == len(res["pollution"])
    assert res["scanned"] >= 1
    item = res["pollution"][0]
    for key in ("id", "content", "category", "reason"):
        assert key in item
    assert item["reason"]
