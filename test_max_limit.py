"""MAX_LIMIT must not silently truncate full-corpus reads.

MAX_LIMIT = 200 dates from the initial commit, when the corpus was tiny and the
constant was a "don't dump everything" guard. It became a correctness bug once
the corpus passed 200 facts, and it is now load-bearing in the wrong direction:
the live corpus has 550 active facts, so every internal site that means "load
the whole corpus" silently loaded 36% of it.

Proven consequences (all silent, no error, no warning):
  - export_markdown() emitted 200 facts, dropping 350
  - get_file_context() / get_dependency_map() could not see facts about a file
    beyond position 200, so the pre-edit briefing under-reported impact
  - get_by_file() likewise

The fix separates two meanings the single constant conflated:
  - internal full-corpus reads  -> limit=None, no ceiling
  - caller-facing result counts -> still clamped, and MAX_LIMIT keeps its
    original meaning as the default ceiling

NOT in scope, deliberately: the structural lane's own `LIMIT 200` over
structural_nodes (pm_search._structural_scores). That is a separate, known
silent-truncation bug which is NOT safely removable -- both candidate fixes
fail the zero-tolerance eval gate -- and carries a standing rule not to touch
it without a retrieval re-tuning pass. See memory fact #498.

Run with: python -m pytest test_max_limit.py -v
"""
import os
import tempfile

import pytest

from memory_store import MemoryStore

TOTAL = 260  # deliberately above MAX_LIMIT (200)


@pytest.fixture
def store():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    s = MemoryStore(db_path=tmp.name)
    s._current_git_commit = lambda: None
    for i in range(TOTAL):
        s.write(category="other", content=f"fact number {i} about topic {i}",
                embed=False, force=True)
    yield s
    try:
        s.close()
    except Exception:
        pass
    try:
        os.unlink(tmp.name)
    except Exception:
        pass


# --- the escape hatch ----------------------------------------------------

def test_get_state_none_returns_everything(store):
    assert len(store.get_state(limit=None)) == TOTAL


def test_get_state_caller_limit_still_clamped(store):
    """A caller asking for more than MAX_LIMIT is still capped, not unbounded."""
    from pm_common import MAX_LIMIT
    assert len(store.get_state(limit=10_000)) == MAX_LIMIT


def test_get_episodic_state_none_allowed(store):
    """Signature parity: the episodic mirror accepts the same sentinel."""
    store.get_episodic_state(limit=None)  # must not raise


# --- no silent truncation in the reporting paths ------------------------

def test_export_markdown_exports_every_fact(store):
    md = store.export_markdown()
    assert md.count("\n- **[") == TOTAL, (
        "export_markdown silently dropped facts -- this is data loss")


def test_export_interactive_html_includes_every_fact(store, tmp_path):
    out = tmp_path / "graph.html"
    store.export_interactive_html(out_path=str(out))
    html = out.read_text(encoding="utf-8")
    for i in (0, TOTAL - 1):
        assert f"fact number {i} " in html, f"fact {i} missing from the HTML export"


def test_get_dependency_map_sees_edges_past_the_cap(store):
    """An internal edge is only found if BOTH endpoint facts are in the read.

    get_dependency_map builds facts_by_file from active facts, then looks for
    memory_edges between them. If either endpoint was truncated away, the edge
    silently vanished from the multi-file refactor briefing.
    """
    # Write the edge endpoints FIRST, then bury them under newer facts so they
    # sit past the 200 most recent (get_state orders by created_at DESC).
    a = store.write(category="decision", content="module a depends on b",
                    file_path="alpha.py", line_start=1, line_end=5,
                    embed=False, force=True)
    b = store.write(category="decision", content="module b is depended on",
                    file_path="beta.py", line_start=1, line_end=5,
                    embed=False, force=True)
    store.link_facts(a, b, "depends_on")
    for i in range(TOTAL):
        store.write(category="other", content=f"filler {i} topic {i}",
                    embed=False, force=True)

    out = store.get_dependency_map(["alpha.py", "beta.py"])
    # The map renders file-level relations, not endpoint fact text.
    assert "[depends_on]" in out, (
        "get_dependency_map could not see an edge whose endpoint was truncated")
    assert "alpha.py" in out and "beta.py" in out


def test_get_by_file_sees_facts_past_the_cap(store):
    store.write(category="component", content="early component about zzz.py",
                file_path="zzz.py", line_start=1, line_end=3,
                embed=False, force=True)
    for i in range(TOTAL):
        store.write(category="other", content=f"filler {i} topic {i}",
                    embed=False, force=True)

    rows = store.get_by_file("zzz.py")
    assert any("early component about zzz.py" in str(r) for r in rows), (
        "get_by_file could not see a fact past the truncation point")


def test_get_file_context_sees_facts_past_the_cap(store):
    store.write(category="constraint", content="early constraint about qqq.py",
                file_path="qqq.py", line_start=1, line_end=4,
                embed=False, force=True)
    for i in range(TOTAL):
        store.write(category="other", content=f"filler {i} topic {i}",
                    embed=False, force=True)

    ctx = store.get_file_context("qqq.py")
    assert "early constraint about qqq.py" in str(ctx), (
        "get_file_context could not see a constraint past the truncation point")
