"""supersede() must not silently inherit a dangling or wrong provenance anchor.

Defect: supersede() computed

    norm_file_path = file_path.strip() if file_path else row["file_path"]

so passing file_path=None (the default, and what every caller does) INHERITED the
old fact's anchor. Two consequences, both silent:

1. An anchor could never be cleared. A fact whose file had been deleted stayed
   anchored to the deleted path forever, and any supersede of it propagated the
   dangling anchor. This is not hypothetical: #728 states in its own text "Deliberately
   carries no file_path anchor" and is anchored anyway, because the author asked
   for no anchor and the call silently refused.

2. line_start / line_end / code_hash were inherited the same way. Superseding a
   fact about code that has since MOVED carries the old line range and the old
   snippet hash, so verify_provenance re-hashes the wrong span -- or fails, and
   the failure looks like tampering rather than a stale anchor.

Fix: `inherit_provenance=True` keeps today's behaviour (backward compatible).
Pass False to take the provenance fields exactly as supplied, so None clears
them. An empty string is also treated as "clear" for the string fields, since
that is the only value a caller can otherwise pass to express it.

Run with: python -m pytest test_supersede_provenance.py -v
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


def _anchored(store, path="old_module.py"):
    return store.write(
        category="component", content="anchored fact about old_module",
        file_path=path, line_start=10, line_end=20,
        code_hash="deadbeef" * 8, embed=False, force=True)


# --- the inheritance behaviour is preserved by default ------------------

def test_default_still_inherits_provenance(store):
    fid = _anchored(store)
    new = store.supersede(fid, "corrected content about old_module")
    row = store.conn.execute(
        "SELECT file_path, line_start, line_end, code_hash FROM memory WHERE id=?",
        (new,)).fetchone()
    assert row["file_path"] == "old_module.py"
    assert row["line_start"] == 10
    assert row["code_hash"] == "deadbeef" * 8


# --- ...and can be declined --------------------------------------------

def test_inherit_false_clears_file_path(store):
    fid = _anchored(store)
    new = store.supersede(fid, "the file this described is gone",
                          inherit_provenance=False)
    row = store.conn.execute(
        "SELECT file_path, line_start, line_end, code_hash FROM memory WHERE id=?",
        (new,)).fetchone()
    assert row["file_path"] is None
    assert row["line_start"] is None
    assert row["line_end"] is None
    assert row["code_hash"] is None


def test_inherit_false_keeps_explicitly_supplied_provenance(store):
    fid = _anchored(store)
    new = store.supersede(fid, "moved to a new location",
                          file_path="new_module.py", line_start=1, line_end=5,
                          code_hash="c0ffee" * 8, inherit_provenance=False)
    row = store.conn.execute(
        "SELECT file_path, line_start, code_hash FROM memory WHERE id=?",
        (new,)).fetchone()
    assert row["file_path"] == "new_module.py"
    assert row["line_start"] == 1
    assert row["code_hash"] == "c0ffee" * 8


def test_empty_string_clears_the_anchor(store):
    """A caller with no other way to express 'no anchor'."""
    fid = _anchored(store)
    new = store.supersede(fid, "unanchored correction", file_path="")
    row = store.conn.execute(
        "SELECT file_path FROM memory WHERE id=?", (new,)).fetchone()
    assert row["file_path"] is None


def test_clearing_the_anchor_survives_deprecation_of_the_old_fact(store):
    """The end-to-end shape: supersede a fact about a deleted file, unanchored,
    and the replacement must not carry the dead path."""
    fid = _anchored(store, path="deleted_module.py")
    new = store.supersede(fid, "deleted_module.py no longer exists",
                          inherit_provenance=False)
    assert store.conn.execute(
        "SELECT file_path FROM memory WHERE id=?", (new,)).fetchone()[0] is None


def test_other_inherited_fields_are_unaffected(store):
    """category / importance / tags inheritance is deliberate and unchanged."""
    fid = store.write(category="constraint", content="tagged fact",
                      tags="alpha,beta", importance=5, embed=False, force=True)
    new = store.supersede(fid, "corrected tagged fact", inherit_provenance=False)
    row = store.conn.execute(
        "SELECT category, importance, tags FROM memory WHERE id=?", (new,)).fetchone()
    assert row["category"] == "constraint"
    assert row["importance"] == 5
    assert row["tags"] == "alpha,beta"
