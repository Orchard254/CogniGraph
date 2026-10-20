"""Regression tests for fact provenance fields (evidence_type / source_ref)."""
import os
import sqlite3
import tempfile

import pytest

from memory_store import MemoryStore
from pm_common import KNOWN_RESULT_FIELDS, project_fields


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


# 1. convention category write works and row has defaults.
def test_write_convention_has_evidence_defaults(store):
    fid = store.write(category="convention", content="follow the house style",
                      embed=False, force=True)
    assert fid and fid > 0
    row = store.conn.execute(
        "SELECT evidence_type, source_ref FROM memory WHERE id=?", (fid,)).fetchone()
    assert row["evidence_type"] == "inferred"
    assert row["source_ref"] is None
    d = store.get_by_id(fid)
    assert d["evidence_type"] == "inferred"
    assert d["source_ref"] is None


# 2. persist explicit provenance and project fields.
def test_write_explicit_provenance_persists_and_projects(store):
    fid = store.write(category="other", content="user asked to do X",
                      evidence_type="user_instruction", source_ref="docs/foo.md",
                      embed=False, force=True)
    assert fid > 0
    d = store.get_by_id(fid)
    assert d["evidence_type"] == "user_instruction"
    assert d["source_ref"] == "docs/foo.md"
    assert {"evidence_type", "source_ref"} <= set(KNOWN_RESULT_FIELDS)
    projected = project_fields([d], ["evidence_type", "source_ref"])
    assert projected[0]["evidence_type"] == "user_instruction"
    assert projected[0]["source_ref"] == "docs/foo.md"
    assert "id" in projected[0]


# 3. invalid evidence_type raises ValueError.
def test_invalid_evidence_type_raises(store):
    with pytest.raises(ValueError):
        store.write(category="other", content="x", evidence_type="bogus",
                    embed=False, force=True)


# 4. migration from an old DB whose category CHECK lacks convention.
def test_migration_adds_evidence_columns_and_allows_convention(tmp_path):
    db_path = str(tmp_path / "old.db")
    old_sql = """
    CREATE TABLE memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL CHECK(category IN ('component','decision','constraint','state','other')),
        content TEXT NOT NULL,
        rationale TEXT,
        confidence TEXT NOT NULL DEFAULT 'assumed' CHECK(confidence IN ('confirmed','assumed','uncertain')),
        created_at TEXT NOT NULL,
        session_id TEXT,
        version INTEGER NOT NULL DEFAULT 1,
        importance INTEGER NOT NULL DEFAULT 3,
        access_count INTEGER NOT NULL DEFAULT 0,
        last_accessed_at TEXT,
        superseded_by INTEGER,
        deprecated_at TEXT,
        deprecated_reason TEXT,
        file_path TEXT,
        line_start INTEGER,
        line_end INTEGER,
        code_hash TEXT,
        git_commit_sha TEXT,
        tags TEXT
    );
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(old_sql)
        conn.execute(
            "INSERT INTO memory (category, content, created_at) VALUES (?, ?, ?)",
            ("other", "old fact preserved", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        old_ids = [r[0] for r in conn.execute("SELECT id FROM memory").fetchall()]
        old_count = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    finally:
        conn.close()

    store = MemoryStore(db_path=db_path)
    try:
        store._current_git_commit = lambda: None
        cols = {c["name"] for c in store.conn.execute("PRAGMA table_info(memory)").fetchall()}
        assert "evidence_type" in cols
        assert "source_ref" in cols
        # prior data preserved
        new_count = store.conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        assert new_count == old_count
        for oid in old_ids:
            r = store.conn.execute("SELECT content FROM memory WHERE id=?", (oid,)).fetchone()
            assert r is not None
            assert r[0] == "old fact preserved"
        # convention now allowed
        fid = store.write(category="convention", content="new convention fact",
                          embed=False, force=True)
        assert fid > 0
    finally:
        store.close()
