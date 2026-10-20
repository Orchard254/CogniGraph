import os

import pytest

@pytest.fixture
def real_db(tmp_path, monkeypatch):
    import server
    from memory_store import MemoryStore
    db = str(tmp_path / "rt" / "memory.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)

    def _factory(*a, **k):
        return MemoryStore(db)

    monkeypatch.setattr(server, "_get_store", _factory)
    return db

import os
import subprocess


def test_checkpoint_verification_requires_commit_for_evidence_verified(real_db, monkeypatch):
    import server
    monkeypatch.setattr(server, "memory_checkpoint_task", server.memory_checkpoint_task)
    out = server.memory_checkpoint_task(
        summary="test",
        verified=True,
        verification_evidence={"command": "pytest", "exit_code": 0},
    )
    assert out.get("verification_status") != "evidence_verified"
    assert out.get("verification_status") in ("caller_reported", "unverified")


def test_checkpoint_verification_accepts_current_commit(real_db, monkeypatch):
    import server
    with server._store() as _s:
        commit = _s._current_git_commit()
    if not commit:
        import pytest
        pytest.skip("no git commit available")
    out = server.memory_checkpoint_task(
        summary="test",
        verified=True,
        verification_evidence={"command": "pytest", "exit_code": 0, "commit_sha": commit},
    )
    assert out.get("verification_status") == "evidence_verified"


def test_check_safety_fails_closed_on_external_error(real_db, monkeypatch):
    import server
    import pm_lifecycle
    # force failure inside external dependency scan by making conn execute blow up
    def raising(conn_execute):
        raise RuntimeError("boom")
    # monkeypatch execute on the store returned by _store
    orig = server._store

    class ExplodingStore(pm_lifecycle.LifecycleMixin):
        pass

    # Simpler: call lower level and monkeypatch the conn
    with server._store() as _s:
        real_execute = _s.conn.execute

        class _ConnProxy:
            def __getattr__(self, name):
                return getattr(real_execute.__self__, name)
            def execute(self, sql, *a, **k):
                if "FROM structural_edges e" in str(sql):
                    raise RuntimeError("boom")
                return real_execute(sql, *a, **k)

        _s.conn = _ConnProxy()
        out = _s.check_safety(["src/a.py", "src/b.py"])
    assert out.get("analysis_complete") is False
    assert out.get("safe_to_continue") is False
    assert out.get("verdict") == "REVIEW_REQUIRED"


def test_memory_resolve_conflict_rejects_same_id(real_db, monkeypatch):
    import server
    import pytest
    # create two facts
    r1 = server.memory_write(category="other", content="fact one unique A")
    r2 = server.memory_write(category="other", content="fact two unique B")
    fid1 = r1["id"]
    fid2 = r2["id"]
    if isinstance(fid1, dict):
        fid1 = fid1["id"]
    if isinstance(fid2, dict):
        fid2 = fid2["id"]
    if fid1 < 0:
        fid1 = -fid1
    if fid2 < 0:
        fid2 = -fid2
    try:
        server.memory_resolve_conflict(fid1, fid1)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_memory_audit_disagreements_exposed(real_db):
    import server
    out = server.memory_audit_disagreements(disagreements_only=True)
    assert isinstance(out, dict)
