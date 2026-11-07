"""Regression tests for the get_file_context SQL basename prefilter.

The prefilter (_file_context_candidates in pm_context.py) must return the
same rows -- in the same order -- as the original get_state(limit=None)
full-corpus scan filtered by _paths_match, for clean AND dirty paths
(backslash, ./ prefix, //, trailing space, ../, trailing slash, dot
segments and trailing '..' on both stored and query paths), special LIKE
characters, deprecated rows, empty/None file_path rows, and non-ASCII
basenames (which fall back to the full scan because SQLite lower() is
ASCII-only).

Run with: python -m pytest test_file_context.py -v
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
    # Temp DBs live outside any git repo: neutralize the probe like
    # test_llm_priming.temp_store does.
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


def _insert_raw(store, file_path, content, i, category="component", deprecated=False):
    """Insert one row with an explicit, distinct created_at (tie-free order)."""
    created_at = f"2026-01-01T00:00:{i:02d}+00:00"
    store.conn.execute(
        "INSERT INTO memory (category, content, created_at, file_path, deprecated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (category, content, created_at, file_path, created_at if deprecated else None),
    )
    store.conn.commit()


def _ids(rows):
    return [r["id"] for r in rows]


def _old_direct(store, clean_path):
    """Reference: the original full-scan path, verbatim."""
    out = []
    for f in store.get_state(limit=None):
        if f["file_path"]:
            fp = f["file_path"].strip().replace("\\", "/").lower()
            if store._paths_match(clean_path, fp):
                out.append(f)
    return out


def _new_direct(store, clean_path):
    """Prefilter candidates through the same Python arbiter as get_file_context."""
    out = []
    for f in store._file_context_candidates(clean_path):
        if f["file_path"]:
            fp = f["file_path"].strip().replace("\\", "/").lower()
            if store._paths_match(clean_path, fp):
                out.append(f)
    return out


@pytest.fixture
def dirty_store(store):
    _insert_raw(store, "src/target.py", "clean target fact", 1, category="constraint")
    _insert_raw(store, "SRC\\TARGET.py", "backslash target fact", 2)
    _insert_raw(store, "./src/target.py", "dot-slash target fact", 3)
    _insert_raw(store, "src//target.py", "double-slash target fact", 4)
    _insert_raw(store, "x/src/target.py", "nested target fact", 5)
    _insert_raw(store, "src/target.py ", "trailing-space target fact", 6)
    _insert_raw(store, "src/foo/../mod_1.py", "dotdot mod fact", 7)
    _insert_raw(store, "src/other.py", "unrelated fact", 8)
    _insert_raw(store, None, "null-path fact", 9)
    _insert_raw(store, "", "empty-path fact", 10)
    _insert_raw(store, "src/target.py", "deprecated target fact", 11, deprecated=True)
    for i in range(12, 40):
        _insert_raw(store, f"filler/dir_{i}.py", f"filler fact {i}", i)
    # Trailing-slash / dot-segment stored forms (REDO): each canonicalises
    # to 'src/target.py' or 'src' under _canonical_path, so they must be
    # candidates and direct facts for those queries.
    _insert_raw(store, "src/target.py/", "trailing-slash target fact", 40)
    _insert_raw(store, "src/target.py/.", "dot-segment target fact", 41)
    _insert_raw(store, "src/target.py//", "double trailing-slash target fact", 42)
    _insert_raw(store, "src/.", "dot-segment src fact", 43, category="constraint")
    _insert_raw(store, "src/foo/..", "dotdot src fact", 44, category="constraint")
    return store


QUERIES = [
    "src/target.py",
    "target.py",
    "SRC\\TARGET.py",
    "./src/target.py",
    "src//target.py",
    "src/target.py ",
    "src/target.py/",
    "src/target.py/.",
    "src/target.py//",
    "src",
    "src/.",
    "src/foo/..",
    "src/foo/../",
    "src/foo/../mod_1.py",
    "mod_1.py",
    "src/other.py",
    "nope/missing.py",
    "",
]


def test_candidate_ids_identical_to_full_scan(dirty_store):
    for q in QUERIES:
        clean = q.strip().replace("\\", "/").lower()
        assert _ids(_new_direct(dirty_store, clean)) == _ids(_old_direct(dirty_store, clean)), (
            f"candidate mismatch for query {q!r}")


def test_prefilter_is_actually_selective(dirty_store):
    total = len(dirty_store.get_state(limit=None))
    cands = dirty_store._file_context_candidates("src/target.py")
    assert 0 < len(cands) < total, "prefilter must be selective, not a silent full scan"
    # Deprecated target row is never a candidate.
    deprecated_ids = {
        r["id"] for r in dirty_store.conn.execute(
            "SELECT id FROM memory WHERE deprecated_at IS NOT NULL").fetchall()}
    assert not (set(_ids(cands)) & deprecated_ids)


def test_candidate_order_matches_get_state_order(dirty_store):
    clean = "src/target.py"
    old_order = _ids(_old_direct(dirty_store, clean))
    new_order = _ids(_new_direct(dirty_store, clean))
    assert new_order == old_order
    assert len(old_order) > 1, "fixture must exercise ordering"


def test_briefing_byte_identical_to_full_scan_path(dirty_store):
    original = dirty_store._file_context_candidates
    for q in ["src/target.py", "target.py", "SRC\\TARGET.py",
              "./src/target.py", "src/foo/../mod_1.py",
              "src/target.py/", "src/target.py/.", "src/target.py//",
              "src", "src/.", "src/foo/..", "src/foo/../",
              "nope/missing.py", "src/other.py"]:
        dirty_store._file_context_candidates = lambda clean_path: dirty_store.get_state(limit=None)
        old_out = dirty_store.get_file_context(q)
        dirty_store._file_context_candidates = original
        new_out = dirty_store.get_file_context(q)
        assert new_out == old_out, f"briefing differs for {q!r}"


def test_trailing_slash_and_dot_segment_rows_are_candidates(dirty_store):
    # Stored forms that canonicalise away a trailing '/' or dot segment
    # must not be missed by the prefilter (REDO review finding).
    for q, expected in [
        ("src/target.py", ["trailing-slash target fact",
                           "dot-segment target fact",
                           "double trailing-slash target fact"]),
        ("src", ["dot-segment src fact", "dotdot src fact"]),
    ]:
        briefing = dirty_store.get_file_context(q)
        for content in expected:
            assert content in briefing, f"{content!r} missing from briefing for {q!r}"
        clean = q.strip().replace("\\", "/").lower()
        assert _ids(_new_direct(dirty_store, clean)) == _ids(_old_direct(dirty_store, clean)), q
    # Candidate set itself must contain those rows (not just FTS fallback).
    target_cands = [r["content"] for r in dirty_store._file_context_candidates("src/target.py")]
    for content in ["trailing-slash target fact", "dot-segment target fact",
                    "double trailing-slash target fact"]:
        assert content in target_cands
    src_cands = [r["content"] for r in dirty_store._file_context_candidates("src")]
    assert "dot-segment src fact" in src_cands
    assert "dotdot src fact" in src_cands


def test_non_ascii_basename_matches_via_fallback(store):
    _insert_raw(store, "src/café.py", "cafe module fact", 1, category="constraint")
    _insert_raw(store, "src/other.py", "unrelated fact", 2)
    for q in ["src/café.py", "SRC/CAFÉ.py", "café.py"]:
        clean = q.strip().replace("\\", "/").lower()
        assert _ids(_new_direct(store, clean)) == _ids(_old_direct(store, clean)), q
    assert "cafe module fact" in store.get_file_context("src/café.py")


def test_like_metachars_in_basename_are_literal(store):
    _insert_raw(store, "src/100%_x.py", "percent-underscore fact", 1)
    _insert_raw(store, "src/100ax.py", "lookalike fact", 2)
    _insert_raw(store, "mod_1.py", "bare underscore fact", 3)
    _insert_raw(store, "modX1.py", "lookalike bare fact", 4)
    clean = "src/100%_x.py"
    new_ids = _ids(_new_direct(store, clean))
    assert new_ids == _ids(_old_direct(store, clean))
    contents = [r["content"] for r in store._file_context_candidates(clean)]
    assert "percent-underscore fact" in contents
    assert "lookalike fact" not in contents
    # Bare filename stored row must match via basename equality, not only LIKE.
    assert _ids(_new_direct(store, "mod_1.py")) == _ids(_old_direct(store, "mod_1.py"))
    assert "bare underscore fact" in store.get_file_context("mod_1.py")
    assert "lookalike bare fact" not in store.get_file_context("mod_1.py")


def test_empty_query_cannot_match(store):
    _insert_raw(store, "src/target.py", "target fact", 1)
    _insert_raw(store, None, "null-path fact", 2)
    _insert_raw(store, "", "empty-path fact", 3)
    out = store.get_file_context("")
    assert "[COMPONENT #]" not in out
    assert _ids(_new_direct(store, "")) == _ids(_old_direct(store, "")) == []


def test_facts_past_cap_still_visible(store):
    """No LIMIT: the briefing must see target facts buried under 300+ newer rows."""
    _insert_raw(store, "buried.py", "buried target fact", 1, category="constraint")
    for i in range(2, 320):
        _insert_raw(store, None, f"filler fact {i}", i)
    out = store.get_file_context("buried.py")
    assert "buried target fact" in out
