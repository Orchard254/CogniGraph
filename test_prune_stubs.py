"""Bootstrap should prune whole-file stubs whose file no longer exists.

Measured before building: 29 active 'Source file:' stubs on the live corpus
point at files that are gone. They are not a live bug -- they are excluded
from the audit, from the structural lane, and from the curated count -- but
`stats().active_stubs` reported 115 when roughly 86 were real, and bootstrap
has no step that can ever remove one, because scan_file_tree only ever WRITES
stubs for files that exist. The next burst of scratch files recreates the
condition.

So this adds the missing half: after a scan, deprecate active stubs whose file
is absent from the scanned root.

Scope rules, each one earning its place:

- STUBS ONLY. A curated fact that happens to carry a file_path is knowledge,
  not inventory, and is never touched. A dangling curated anchor is a
  different problem with a different answer (null the column, keep the fact --
  see fact #804).
- RELATIVE PATHS ONLY. An absolute path or one with a drive letter was not
  produced by scan_file_tree's relative-path convention, so it is not ours to
  judge.
- ROOT-SCOPED, and opt-in. `memory` has no repo_id column, so a stub is
  identified only by file_path. If one database ever holds two project roots,
  pruning from the wrong root would delete the other root's inventory. The
  per-project layout (.cognigraph/memory.db per root) makes this correct, but
  the check is therefore not run implicitly: it is a --prune-stubs flag, dry-run
  by default, that shows exactly what it would remove.

Run with: python -m pytest test_prune_stubs.py -v
"""
import os
import tempfile
from pathlib import Path

import pytest

from memory_store import MemoryStore


@pytest.fixture
def repo():
    """A throwaway project root with a store attached to it."""
    root = Path(tempfile.mkdtemp(prefix="prunestubs_"))
    (root / "pkg").mkdir()
    (root / "pkg" / "alpha.py").write_text("def alpha():\n    return 1\n",
                                           encoding="utf-8")
    (root / "pkg" / "beta.py").write_text("def beta():\n    return 2\n",
                                          encoding="utf-8")
    db = root / "memory.db"
    s = MemoryStore(db_path=str(db))
    s._current_git_commit = lambda: None
    yield root, s
    try:
        s.close()
    except Exception:
        pass


def _stubs(store):
    from corpus_hygiene import STUB_PREFIXES
    rows = store.conn.execute(
        "SELECT id, content, file_path FROM memory WHERE deprecated_at IS NULL "
        "AND file_path IS NOT NULL").fetchall()
    return [r for r in rows if (r["content"] or "").startswith(STUB_PREFIXES)]


def _deprecate_reason(store, fact_id):
    return store.conn.execute(
        "SELECT deprecated_reason FROM memory WHERE id=?", (fact_id,)
    ).fetchone()[0]


# --- the core behaviour --------------------------------------------------

def test_prunes_a_stub_whose_file_vanished(repo):
    root, store = repo
    store.write(category="component", embed=False, force=True,
                file_path="pkg/alpha.py", line_start=1, line_end=2,
                content="Source file: pkg/alpha.py (python, 2 lines, hash deadbeef)")
    (root / "pkg" / "alpha.py").unlink()
    pruned = store.prune_stale_stubs(root, dry_run=False)
    assert pruned["pruned"] == 1
    assert _deprecate_reason(store, 1) is not None


def test_defaults_to_dry_run(repo):
    """The safe default is the whole point: nothing is removed implicitly."""
    root, store = repo
    store.write(category="component", embed=False, force=True,
                file_path="pkg/alpha.py",
                content="Source file: pkg/alpha.py (python, 2 lines, hash deadbeef)")
    (root / "pkg" / "alpha.py").unlink()
    res = store.prune_stale_stubs(root)
    assert res["dry_run"] is True
    assert res["pruned"] == 0
    assert store.conn.execute(
        "SELECT deprecated_at FROM memory WHERE id=1").fetchone()[0] is None


def test_keeps_a_stub_whose_file_exists(repo):
    root, store = repo
    store.write(category="component", embed=False, force=True,
                file_path="pkg/beta.py", line_start=1, line_end=2,
                content="Source file: pkg/beta.py (python, 2 lines, hash cafebabe)")
    assert store.prune_stale_stubs(root)["pruned"] == 0
    assert _stubs(store), "the live stub must survive"


def test_is_idempotent(repo):
    """Second run finds nothing, so a repeated bootstrap does not churn."""
    root, store = repo
    store.write(category="component", embed=False, force=True,
                file_path="pkg/alpha.py",
                content="Source file: pkg/alpha.py (python, 2 lines, hash deadbeef)")
    (root / "pkg" / "alpha.py").unlink()
    first = store.prune_stale_stubs(root, dry_run=False)
    second = store.prune_stale_stubs(root, dry_run=False)
    assert first["pruned"] == 1
    assert second["pruned"] == 0
    assert second["candidate_count"] == 0


# --- the scope rules -----------------------------------------------------

def test_never_touches_a_curated_fact_with_a_dangling_anchor(repo):
    """A curated fact is knowledge, not inventory. See fact #804."""
    root, store = repo
    store.write(category="decision", embed=False, force=True,
                file_path="pkg/deleted.py",
                content="Chose the RRF k=5 constant after a 52-pair sweep.")
    (root / "pkg" / "alpha.py").unlink()
    (root / "pkg" / "beta.py").unlink()
    res = store.prune_stale_stubs(root)
    assert res["pruned"] == 0
    assert store.conn.execute(
        "SELECT deprecated_at FROM memory WHERE id=1").fetchone()[0] is None


def test_ignores_absolute_paths(repo):
    """An absolute path is not scan_file_tree's convention, so not ours to judge."""
    root, store = repo
    store.write(category="component", embed=False, force=True,
                file_path="C:/elsewhere/thing.py",
                content="Source file: C:/elsewhere/thing.py (python, 1 lines, hash aa)")
    assert store.prune_stale_stubs(root)["pruned"] == 0


def test_ignores_paths_that_escape_the_root(repo):
    """A '../' path would resolve outside the project; refusing is the safe call."""
    root, store = repo
    store.write(category="component", embed=False, force=True,
                file_path="../sibling/thing.py",
                content="Source file: ../sibling/thing.py (python, 1 lines, hash bb)")
    assert store.prune_stale_stubs(root)["pruned"] == 0


def test_dry_run_reports_without_deprecating(repo):
    root, store = repo
    store.write(category="component", embed=False, force=True,
                file_path="pkg/alpha.py",
                content="Source file: pkg/alpha.py (python, 2 lines, hash deadbeef)")
    (root / "pkg" / "alpha.py").unlink()
    res = store.prune_stale_stubs(root, dry_run=True)
    assert res["candidate_count"] == 1
    assert res["pruned"] == 0
    assert res["dry_run"] is True
    assert store.conn.execute(
        "SELECT deprecated_at FROM memory WHERE id=1").fetchone()[0] is None


def test_result_names_each_candidate(repo):
    """A maintainer must be able to see WHAT would go before letting it go."""
    root, store = repo
    store.write(category="component", embed=False, force=True,
                file_path="pkg/alpha.py",
                content="Source file: pkg/alpha.py (python, 2 lines, hash deadbeef)")
    (root / "pkg" / "alpha.py").unlink()
    res = store.prune_stale_stubs(root, dry_run=True)
    assert res["candidates"][0]["file_path"] == "pkg/alpha.py"
    assert res["candidates"][0]["id"] == 1


def test_already_deprecated_stub_is_not_recounted(repo):
    root, store = repo
    fid = store.write(category="component", embed=False, force=True,
                      file_path="pkg/alpha.py",
                      content="Source file: pkg/alpha.py (python, 2 lines, hash deadbeef)")
    store.deprecate(fid, reason="earlier")
    (root / "pkg" / "alpha.py").unlink()
    assert store.prune_stale_stubs(root)["candidate_count"] == 0


def test_missing_root_is_refused(repo):
    """Pruning against a root that does not exist would remove everything."""
    root, store = repo
    store.write(category="component", embed=False, force=True,
                file_path="pkg/alpha.py",
                content="Source file: pkg/alpha.py (python, 2 lines, hash deadbeef)")
    bogus = root / "no-such-dir"
    with pytest.raises(ValueError):
        store.prune_stale_stubs(bogus)
    assert store.conn.execute(
        "SELECT deprecated_at FROM memory WHERE id=1").fetchone()[0] is None
