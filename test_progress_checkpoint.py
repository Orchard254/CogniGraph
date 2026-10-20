"""Regression tests for progress-tracking checkpoint enhancements (section E)."""
import os
import sqlite3
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


def test_checkpoint_progress_writes_blocker_and_unfinished_state_facts(store):
    out = store.checkpoint_task(
        summary="progress checkpoint probe",
        unfinished=["finish docs", "add tests"],
        blockers=["CI flaky", "missing creds"],
        acceptance_criteria=["tests green", "docs updated", "lint clean"],
        verified=True,
        test_baseline="pytest -q",
    )
    assert out["status"] == "CHECKPOINT_RECORDED"
    # 2 unfinished + 2 blockers + 3 acceptance + 1 milestone = 8 state facts
    assert out["total_facts_written"] == 8
    assert out["recorded"]["unfinished"] == 2
    assert out["recorded"]["blockers"] == 2
    assert out["recorded"]["acceptance_criteria"] == 3
    assert "written_ids" in out

    rows = store.conn.execute(
        "SELECT content, tags FROM memory WHERE category='state' ORDER BY id"
    ).fetchall()
    tags_by_content = {r["content"]: r["tags"] for r in rows}
    assert tags_by_content.get("CI flaky") == "blocker,open"
    assert tags_by_content.get("finish docs") == "unfinished,backlog"
    assert tags_by_content.get("tests green") == "acceptance-criteria"

    milestone_rows = [r for r in rows if r["content"].startswith("progress checkpoint probe")]
    assert len(milestone_rows) == 1
    mc = milestone_rows[0]["content"]
    assert "Verified: caller_reported" in mc
    assert "Acceptance criteria: 3" in mc
    assert "Verified Baseline: pytest -q" in mc


def test_checkpoint_verified_marker_unknown_default(store):
    out = store.checkpoint_task(summary="default progress")
    assert out["total_facts_written"] == 1
    row = store.conn.execute(
        "SELECT content FROM memory WHERE category='state' AND content LIKE 'default progress%'"
    ).fetchone()
    assert "Verified: unknown" in row["content"]
    # no acceptance criteria provided => no count marker
    assert "Acceptance criteria:" not in row["content"];