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

from scripts.check_corpus_truth import check_all, check_tool_count, _claimed_counts

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
