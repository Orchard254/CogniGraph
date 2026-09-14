"""Hermetic tests for LifecycleMixin.check_hygiene + server.memory_check_hygiene.

Strategy (NOT part of test_server_tools.py on purpose):
- test_server_tools.py declares "No subprocess, no network, no git dependence"
  and check_hygiene shells out to git by design, so these tests live here.
- Every test builds scratch git repos under tmp_path and always passes an
  explicit project_root — a bare check_hygiene() call under pytest would probe
  this repo's own live working tree (cf. the episodic #84 wrong-directory
  precedent), so no test may rely on CWD.
- Real git binary required (mirrors test_wire_bootstrap.py): the suite skips
  when git is unavailable. No network: file-path clones only.
"""

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server
from memory_store import MemoryStore

need_git = pytest.mark.skipif(shutil.which("git") is None, reason="git binary unavailable")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, timeout=30)


def _scratch_repo(path, name="repo"):
    """Init a scratch repo with user config + one commit. Returns repo path."""
    repo = path / name
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def tmp_store(tmp_path):
    """Real MemoryStore on a scratch DB (never the live DB)."""
    db = str(tmp_path / "hygiene.db")
    store = MemoryStore(db)
    yield store
    store.close()


@need_git
def test_clean_repo_no_upstream(tmp_path, tmp_store):
    repo = _scratch_repo(tmp_path)
    out = tmp_store.check_hygiene(str(repo))
    assert out["status"] == "CLEAN"
    assert out["branch"] in ("main", "master")
    assert out["detached"] is False
    assert out["head"] and len(out["head"]) == 40
    assert out["upstream"] is None
    assert out["ahead"] == 0 and out["behind"] == 0
    assert out["dirty_files"] == [] and out["untracked_count"] == 0
    assert out["stale_branches"] == []
    assert any("no upstream" in lim for lim in out["limitations"])
    assert any("CI-status" in lim for lim in out["limitations"])


@need_git
def test_dirty_tracked_modification(tmp_path, tmp_store):
    repo = _scratch_repo(tmp_path)
    (repo / "a.txt").write_text("a\nmodified\n", encoding="utf-8")
    out = tmp_store.check_hygiene(str(repo))
    assert out["status"] == "DIRTY"
    assert out["dirty_files"] == ["a.txt"]
    assert out["dirty_total"] == 1
    assert out["dirty_truncated"] is False
    assert out["untracked_count"] == 0


@need_git
def test_untracked_files_counted_not_listed(tmp_path, tmp_store):
    repo = _scratch_repo(tmp_path)
    (repo / "new1.txt").write_text("x\n", encoding="utf-8")
    (repo / "new2.txt").write_text("y\n", encoding="utf-8")
    out = tmp_store.check_hygiene(str(repo))
    assert out["status"] == "DIRTY"
    assert out["untracked_count"] == 2
    assert out["dirty_files"] == []  # untracked counted separately


@need_git
def test_dirty_list_capped(tmp_path, tmp_store):
    repo = _scratch_repo(tmp_path)
    for i in range(8):
        (repo / f"f{i}.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "eight files")
    for i in range(8):
        (repo / f"f{i}.txt").write_text("v2\n", encoding="utf-8")
    out = tmp_store.check_hygiene(str(repo), max_dirty_files=3)
    assert out["status"] == "DIRTY"
    assert out["dirty_total"] == 8
    assert len(out["dirty_files"]) == 3
    assert out["dirty_truncated"] is True


def _clone_pair(tmp_path):
    """Bare origin + two clones. Returns (origin, clone_a, clone_b)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True,
                   capture_output=True, timeout=30)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True,
                   capture_output=True, timeout=30)
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "a.txt").write_text("a\n", encoding="utf-8")
    _git(seed, "add", "a.txt")
    _git(seed, "commit", "-m", "init")
    branch = _git(seed, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(seed, "push", "-u", "origin", branch)
    clone_a = tmp_path / "a"
    clone_b = tmp_path / "b"
    for dst in (clone_a, clone_b):
        subprocess.run(["git", "clone", str(origin), str(dst)], check=True,
                       capture_output=True, timeout=30)
        _git(dst, "config", "user.email", "t@t")
        _git(dst, "config", "user.name", "t")
    return origin, clone_a, clone_b


def _commit(repo, name, text="x"):
    (repo / name).write_text(text + "\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)


@need_git
def test_ahead(tmp_path, tmp_store):
    _, clone_a, _ = _clone_pair(tmp_path)
    _commit(clone_a, "ahead.txt")
    out = tmp_store.check_hygiene(str(clone_a))
    assert out["status"] == "AHEAD"
    assert out["ahead"] == 1 and out["behind"] == 0
    assert out["upstream"] == "origin/" + out["branch"]


@need_git
def test_behind(tmp_path, tmp_store):
    _, clone_a, clone_b = _clone_pair(tmp_path)
    _commit(clone_b, "other.txt")
    branch = _git(clone_b, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(clone_b, "push", "origin", branch)
    _git(clone_a, "fetch", "origin")
    out = tmp_store.check_hygiene(str(clone_a))
    assert out["status"] == "BEHIND"
    assert out["ahead"] == 0 and out["behind"] == 1


@need_git
def test_diverged(tmp_path, tmp_store):
    _, clone_a, clone_b = _clone_pair(tmp_path)
    _commit(clone_a, "mine.txt")
    _commit(clone_b, "theirs.txt")
    branch = _git(clone_b, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(clone_b, "push", "origin", branch)
    _git(clone_a, "fetch", "origin")
    out = tmp_store.check_hygiene(str(clone_a))
    assert out["status"] == "DIVERGED"
    assert out["ahead"] == 1 and out["behind"] == 1


@need_git
def test_stale_branch_gone_upstream(tmp_path, tmp_store):
    _, clone_a, clone_b = _clone_pair(tmp_path)
    branch = _git(clone_a, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(clone_a, "checkout", "-b", "feature")
    _commit(clone_a, "feat.txt")
    _git(clone_a, "push", "-u", "origin", "feature")
    _git(clone_b, "fetch", "origin")
    _git(clone_b, "checkout", "feature")
    _git(clone_a, "push", "origin", "--delete", "feature")
    _git(clone_b, "fetch", "--prune", "origin")
    out = tmp_store.check_hygiene(str(clone_b))
    assert out["branch"] == "feature"
    assert {"branch": "feature", "upstream": "origin/feature"} in out["stale_branches"]


@need_git
def test_not_a_repo(tmp_path, tmp_store):
    plain = tmp_path / "plain"
    plain.mkdir()
    out = tmp_store.check_hygiene(str(plain))
    assert out["status"] == "NOT_A_REPO"
    assert out["head"] is None and out["branch"] is None


def test_missing_root(tmp_store):
    out = tmp_store.check_hygiene("C:\\definitely\\not\\here\\nope")
    assert out["status"] == "NOT_A_REPO"
    assert any("does not exist" in lim for lim in out["limitations"])


@need_git
def test_git_unavailable_simulated(tmp_path, tmp_store, monkeypatch):
    repo = _scratch_repo(tmp_path)
    monkeypatch.setenv("PATH", "")  # git no longer resolvable
    out = tmp_store.check_hygiene(str(repo))
    assert out["status"] == "GIT_UNAVAILABLE"
    assert any("not found on PATH" in lim for lim in out["limitations"])


@need_git
def test_server_wrapper_reports_clean(tmp_path, monkeypatch):
    repo = _scratch_repo(tmp_path)
    db = str(tmp_path / "srv.db")

    def _factory():
        return MemoryStore(db)

    monkeypatch.setattr(server, "_get_store", _factory)
    out = server.memory_check_hygiene(str(repo))
    assert out["status"] == "CLEAN"
    assert out["project_root"] == str(repo)
