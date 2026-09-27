"""check_corpus_truth: active facts must not contradict the running system.

Motivation: every expensive defect found in the 2026-09-27 audit was silent.
A fact asserting "The server registers 14 tools" when 18 were registered was
active and served for days, and nothing failed. Tests and the eval gate guard
CODE; the eval gate runs on a frozen fixture and so can never see corpus rot.

These tests pin both halves of the contract: real contradictions are caught,
and legitimate historical/resolved records are NOT flagged (a checker that
 cries wolf gets ignored, which is worse than no checker).

Run with: python -m pytest test_corpus_truth.py -v
"""
import sqlite3

import pytest

from scripts.check_corpus_truth import (
    check_all, check_tool_count, check_tool_references,
    known_identifiers, _claimed_counts,
)

ACTUAL = 18


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "truth.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""CREATE TABLE memory (
        id INTEGER PRIMARY KEY, category TEXT, content TEXT,
        deprecated_at TEXT)""")
    conn.commit()
    conn.close()
    return str(p)


def _add(db, content, category="other", deprecated_at=None):
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO memory (category, content, deprecated_at) VALUES (?,?,?)",
        (category, content, deprecated_at))
    conn.commit()
    conn.close()
    return cur.lastrowid


# --- catches real contradictions ----------------------------------------

def test_flags_fact_claiming_wrong_tool_count(db):
    """Asserts a present-tense count with no historical qualifier."""
    _add(db, "The server registers 14 tools, per the README tool table.")
    v = check_tool_count(db, ACTUAL)
    assert len(v) == 1, [x["content"] for x in v]
    assert v[0]["claimed"] == [14]
    assert v[0]["actual"] == ACTUAL


def test_flags_tool_catalog_phrasing(db):
    _add(db, "Documented complete MCP server 41-tool catalog for the release")
    v = check_tool_count(db, ACTUAL)
    assert len(v) == 1
    assert 41 in v[0]["claimed"]


def test_check_all_reports_the_registry_count(db):
    _add(db, "The server registers 14 tools, per the README table.")
    report = check_all(db, ACTUAL)
    assert report["tool_count"] == ACTUAL
    assert len(report["violations"]) == 1


# --- must NOT flag legitimate records -----------------------------------

def test_accepts_fact_matching_the_live_count(db):
    _add(db, f"The server registers {ACTUAL} tools and README agrees.")
    assert check_tool_count(db, ACTUAL) == []


def test_accepts_resolved_historical_record(db):
    _add(db, "RESOLVED 2026-09-13: README tool table regenerated to 15 tools "
             "via gen_tool_table.py; drift closed.")


def test_accepts_historical_attribution(db):
    _add(db, "README tool table was regenerated to 15 tools at commit ba517be")
    assert check_tool_count(db, ACTUAL) == []


def test_ignores_deprecated_facts(db):
    _add(db, "The server registers 14 tools", deprecated_at="2026-01-01T00:00:00+00:00")
    assert check_tool_count(db, ACTUAL) == []


def test_ignores_implausibly_small_counts(db):
    """"3 tools" is prose, not a registry size claim."""
    _add(db, "The fix touches 3 tools in the server module")
    assert check_tool_count(db, ACTUAL) == []


def test_ignores_facts_with_no_tool_claim(db):
    _add(db, "watcher debounce uses a 1.5 second settling window")
    assert check_tool_count(db, ACTUAL) == []


# --- the parser itself --------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("The server registers 14 tools", [14]),
    ("a complete MCP server 41-tool catalog", [41]),
    ("README says 18 tools", [18]),
    ("only 3 tools were touched", []),          # below plausibility floor
    ("no counts here at all", []),
])
def test_claimed_counts(text, expected):
    assert sorted(set(_claimed_counts(text))) == expected


# --- check 2: prescriptive facts citing a retired identifier -----------
#
# Measured before implementing: a naive "does this fact name an unknown
# memory_* thing" check flagged 73 facts, because `memory_cli` is a SCRIPT,
# `memory_store` a MODULE, and `memory_edges`/`memory_embeddings`/
# `memory_dependencies` are TABLES. Checking against the union of every valid
# identifier collapsed that to 5, and requiring PRESCRIPTIVE phrasing (a
# protocol telling an agent to call it) rather than a narrative mention left 2
# real defects out of 463 active facts.
#
# The distinction matters: a `state` fact narrating "ran a simulation using
# memory_query_hybrid" is accurate history, while a `constraint` fact reading
# "1. Call `memory_get_state`" is an instruction to call a tool that no longer
# exists.

TOOLS = {"memory_stats", "memory_query", "memory_prepare_task"}


def test_known_identifiers_include_tools_and_non_tools():
    known = known_identifiers(tools=TOOLS, extra={"memory_cli", "memory_edges",
                                                   "memory_store", "scratch_probe"})
    assert TOOLS <= known
    assert {"memory_cli", "memory_edges", "memory_store"} <= known


def test_flags_prescriptive_reference_to_a_retired_tool(db):
    _add(db, "Mandatory Protocol\nAt the start of every session:\n"
             "1. Call `memory_get_state` or `memory_stats` to load context.",
         category="constraint")
    v = check_tool_references(db, TOOLS)
    assert len(v) == 1
    assert v[0]["unknown"] == ["memory_get_state"]
    assert v[0]["prescriptive"] is True


def test_ignores_narrative_mention_of_a_retired_tool(db):
    """A `state` fact describing a past simulation is accurate history."""
    _add(db, "Ran an end-to-end agent-priming simulation (AGENTS.md protocol: "
             "memory_stats, memory_query_hybrid) and it passed.",
         category="state")
    assert check_tool_references(db, TOOLS) == []


def test_ignores_fact_that_correctly_says_a_tool_was_not_added(db):
    """Regression guard: negative knowledge names a non-existent tool on purpose."""
    _add(db, "Widened memory_deprecate instead of adding memory_deprecate_batch. "
             "The single-id return shape is preserved.",
         category="decision")
    assert check_tool_references(db, TOOLS) == []


def test_ignores_non_tool_identifiers(db):
    """memory_cli is a script, memory_store a module, memory_edges a table."""
    _add(db, "At the start of every session: 1. Call `memory_stats`, then run "
             "memory_cli.py wire; memory_edges links facts and memory_store "
             "holds the write path.",
         category="constraint")
    assert check_tool_references(
        db, TOOLS, extra={"memory_cli", "memory_edges", "memory_store"}) == []


def test_ignores_prescriptive_reference_to_a_live_tool(db):
    _add(db, "Mandatory Protocol\nAt the start of every session:\n"
             "1. Call `memory_stats` to load context.", category="constraint")
    assert check_tool_references(db, TOOLS) == []


def test_ignores_deprecated_facts(db):
    _add(db, "At the start of every session:\n1. Call `memory_get_state`.",
         category="constraint", deprecated_at="2026-01-01T00:00:00+00:00")
    assert check_tool_references(db, TOOLS) == []


def test_ignores_template_stubs(db):
    _add(db, "Source file: wire.py (python, 761 lines, hash b855f304)")
    assert check_tool_references(db, TOOLS) == []


def test_tool_reference_violation_shape(db):
    _add(db, "Protocol: 1. Call `memory_get_state` first.", category="constraint")
    item = check_tool_references(db, TOOLS)[0]
    for key in ("id", "content", "category", "unknown", "prescriptive", "check"):
        assert key in item
    assert item["check"] == "tool_reference"
