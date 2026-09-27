"""ux_stub_once must cover ALL THREE stub prefixes, not just 'Source file:'.

The original #530 index was scoped to `content LIKE 'Source file:%'` only. But
STUB_PREFIXES has three members, and the other two -- 'Key file:' and
"Directory '" -- are equally auto-generated inventory asserting that something
exists. They were equally unconstrained, so the guarantee was half-done: the
live corpus had already grown #247/#248, an identical `Key file: README.md`
pair, that the index could never have prevented.

Two things had to be right for the widening to actually take effect, and both
were found the hard way:

1. SQLite REFUSES bound parameters in a partial index WHERE clause
   ("parameters prohibited in partial index WHERE clauses"). The predicate
   therefore needs a literal form for DDL while the pre-check keeps the bound
   form, so both live in corpus_hygiene next to each other and this file
   asserts they agree.

2. `CREATE INDEX IF NOT EXISTS` does NOT upgrade a stale definition. The name
   matches, so the old narrower index survives untouched and the widening looks
   applied while enforcing nothing extra. The store now reads sqlite_master and
   rebuilds when the stored SQL does not mention every prefix.

Run with: python -m pytest test_stub_index_coverage.py -v
"""
import os
import sqlite3
import tempfile

import pytest

from corpus_hygiene import (STUB_PREFIXES, _stub_params, _stub_where,
                            _stub_where_inline)
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


def _index_sql(store):
    row = store.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='ux_stub_once'").fetchone()
    return (row["sql"] if row else "") or ""


def _sample(prefix):
    """A plausible auto-generated stub body for each prefix."""
    return {
        "Source file:": "Source file: pkg/alpha.py (python, 12 lines, hash abcd1234)",
        "Key file:": "Key file: README.md (hash a3fa3a82)",
        "Directory '": "Directory 'src/api' (14 files)",
    }[prefix]


# --- the predicate forms must agree --------------------------------------

def test_inline_and_bound_predicates_cover_the_same_prefixes():
    for p in STUB_PREFIXES:
        assert p.replace("'", "''") in _stub_where_inline(), p
    assert _stub_where().count("?") == len(_stub_where_inline().split("LIKE")) - 1
    assert _stub_params() == tuple(f"{p}%" for p in STUB_PREFIXES)


def test_inline_predicate_escapes_the_quote_in_directory_prefix():
    """\"Directory '\" must become \"Directory ''%\" or the SQL breaks."""
    assert "content LIKE 'Directory ''%'" in _stub_where_inline()


def test_inline_predicate_escaping_is_executable(store):
    store.conn.execute(
        f"SELECT 1 FROM memory WHERE ({_stub_where_inline()}) LIMIT 1")


# --- the index itself covers all three ----------------------------------

def test_index_covers_every_stub_prefix(store):
    sql = _index_sql(store)
    assert sql, "ux_stub_once should exist on a fresh database"
    for p in STUB_PREFIXES:
        assert p.replace("'", "''") in sql, f"index does not cover {p!r}"


@pytest.mark.parametrize("prefix", STUB_PREFIXES)
def test_duplicate_stub_is_rejected_for_each_prefix(store, prefix):
    """file_path is part of the key, so it must be supplied -- see the NULL note."""
    body = _sample(prefix)
    first = store.write(category="component", content=body, file_path="pkg/x.py",
                        embed=False, force=True)
    assert first > 0
    second = store.write(category="component", content=body, file_path="pkg/x.py",
                         embed=False, force=True)
    assert second == -1, (
        f"{prefix!r} duplicate was accepted; the index is not covering it")


@pytest.mark.parametrize("prefix", STUB_PREFIXES)
def test_deprecated_stub_does_not_block_a_fresh_one(store, prefix):
    """Partial index keys on active rows only, so a deprecation frees the slot."""
    body = _sample(prefix)
    fid = store.write(category="component", content=body, file_path="pkg/x.py",
                      embed=False, force=True)
    store.deprecate(fid, reason="test")
    assert store.write(category="component", content=body, file_path="pkg/x.py",
                       embed=False, force=True) > 0


def test_stub_without_a_file_path_bypasses_the_index(store):
    """Documented limitation, not an oversight.

    The index keys on (file_path, content), and SQLite treats NULLs as distinct
    in a UNIQUE index, so two stubs with the same content and NO file_path do not
    collide. In practice scan_file_tree always sets file_path, so every real stub
    is covered -- but a caller that writes a bare stub with no anchor gets no
    singleton guarantee from the database and relies on write()'s similarity
    guard alone. Pinned so the limitation is not discovered later as a bug.
    """
    body = "Source file: mystery.py (python, 3 lines, hash ffff0000)"
    a = store.write(category="component", content=body, embed=False, force=True)
    b = store.write(category="component", content=body, embed=False, force=True)
    assert a > 0
    assert b != -1 or a == -1, (
        "if SQLite ever coalesces NULLs in a partial unique index, this test "
        "should be revisited -- today it is accepted as a distinct row")


def test_curated_facts_are_never_constrained(store):
    """Two identical curated facts are near-duplicates, not index violations."""
    a = store.write(category="decision", content="Chose RRF k=5 after a sweep.",
                    embed=False, force=True)
    b = store.write(category="decision", content="Chose RRF k=5 after a sweep.",
                    embed=False, force=True)
    assert a > 0
    # Either accepted as a distinct row or refused as a near-duplicate; what it
    # must NOT be is an IntegrityError escaping write().
    assert b > 0 or b == -1


# --- the widening actually reaches an existing database ------------------

def test_narrow_existing_index_is_rebuilt_on_open(tmp_path):
    """The IF NOT EXISTS trap, as a test.

    Build a database that already carries the OLD single-prefix index, then
    open it with the current store. The index must end up covering all three.
    """
    db = tmp_path / "legacy.db"
    s = MemoryStore(db_path=str(db))
    s._current_git_commit = lambda: None
    s.conn.execute("DROP INDEX IF EXISTS ux_stub_once")
    s.conn.execute(
        "CREATE UNIQUE INDEX ux_stub_once ON memory(file_path, content) "
        "WHERE content LIKE 'Source file:%' AND deprecated_at IS NULL")
    s.close()

    s2 = MemoryStore(db_path=str(db))
    s2._current_git_commit = lambda: None
    try:
        sql = _index_sql(s2)
        assert "Key file:" in sql, "narrow index was not rebuilt"
        assert "Directory ''" in sql, "narrow index was not rebuilt"
    finally:
        s2.close()


def test_duplicate_key_file_defers_index_creation(tmp_path):
    """The pre-check must see a duplicate in ANY prefix, not just the first."""
    db = tmp_path / "dupe.db"
    s = MemoryStore(db_path=str(db))
    s._current_git_commit = lambda: None
    s.conn.execute("DROP INDEX IF EXISTS ux_stub_once")
    body = "Key file: README.md (hash a3fa3a82)"
    now = "2026-09-28T00:00:00+00:00"
    for _ in range(2):
        s.conn.execute(
            "INSERT INTO memory (category, content, file_path, created_at) "
            "VALUES ('component', ?, 'README.md', ?)", (body, now))
    s.close()

    s2 = MemoryStore(db_path=str(db))
    s2._current_git_commit = lambda: None
    try:
        assert not _index_sql(s2), (
            "index was created despite a duplicate Key file: stub; the pre-check "
            "is still only looking at the first prefix")
    finally:
        s2.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(str(db) + suffix)
            except OSError:
                pass


def test_literal_index_predicate_is_required_by_sqlite(store):
    """Documents WHY the literal form exists, so nobody 'simplifies' it back."""
    with pytest.raises(sqlite3.OperationalError, match="parameters"):
        store.conn.execute(
            "CREATE UNIQUE INDEX ux_bad ON memory(file_path, content) "
            f"WHERE ({_stub_where()})",
            _stub_params())
