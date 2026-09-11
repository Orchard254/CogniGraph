import pytest
from pathlib import Path

# Cache for MemoryStore._current_git_commit keyed by repo root.
# Temp DBs all share one root (parent.parent of %TEMP%), so hundreds of
# ~0.054s `git rev-parse` probes collapse to a single subprocess.
# Real repo DBs (e.g. TestGitCommitAnchoring's tmp repos) keep distinct
# roots, so their first probe still runs git and the value is preserved.
_git_commit_cache = {}


@pytest.fixture(autouse=True)
def _fast_git_probe(monkeypatch):
    """Cache _current_git_commit per DB root; values unchanged, subprocesses deduped."""
    try:
        from memory_store import MemoryStore
    except Exception:
        yield
        return
    orig = MemoryStore._current_git_commit

    def fast_probe(self):
        try:
            root = str(Path(self.db_path).resolve().parent.parent)
        except Exception:
            return None
        if root in _git_commit_cache:
            return _git_commit_cache[root]
        try:
            res = orig(self)
        except Exception:
            res = None
        _git_commit_cache[root] = res
        return res

    monkeypatch.setattr(MemoryStore, "_current_git_commit", fast_probe)
    yield
    # Cache persists across tests on purpose (roots are stable); do not clear.

