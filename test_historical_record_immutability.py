"""Direct-SQL tests for preserving canonical record history and immutable fields."""

from datetime import datetime, timezone
import sqlite3

import pytest

import memory_store
from memory_store import MemoryStore


def _now():
    return datetime.now(timezone.utc).isoformat()


def _fixture(tmp_path, monkeypatch):
    # Isolate canonical-row retention from optional vec0 extension triggers.
    # The test uses raw sqlite3 connections, which intentionally do not load
    # sqlite-vec; a production DB with vector tables requires that extension
    # on every connection that executes its maintenance triggers.
    monkeypatch.setattr(memory_store, "_SQLITE_VEC_PKG_AVAILABLE", False)
    db_path = tmp_path / "history.db"
    store = MemoryStore(str(db_path))
    now = _now()
    try:
        repo_id = store.conn.execute(
            "INSERT INTO repositories (root_path, current_revision, created_at) VALUES (?, 1, ?)",
            (str(tmp_path / "project"), now),
        ).lastrowid
        root_id = store.conn.execute(
            "INSERT INTO memory_branches "
            "(repo_id, parent_branch_id, is_root, base_revision, base_timestamp, status, created_at) "
            "VALUES (?, NULL, 1, 1, ?, 'ACTIVE', ?)",
            (repo_id, now, now),
        ).lastrowid

        entity_id = store.conn.execute(
            "INSERT INTO entities "
            "(repo_id, entity_type, file_path, qualified_name, class_name, created_at) "
            "VALUES (?, 'FUNCTION', 'src/main.py', 'main', '', ?)",
            (repo_id, now),
        ).lastrowid
        anchor_id = store.conn.execute(
            "INSERT INTO anchors (entity_id, repo_id, created_at) VALUES (?, ?, ?)",
            (entity_id, repo_id, now),
        ).lastrowid

        # A deliberately unreferenced anchor proves the DELETE guard itself fires,
        # rather than an ordinary foreign-key constraint.
        unused_entity_id = store.conn.execute(
            "INSERT INTO entities "
            "(repo_id, entity_type, file_path, qualified_name, class_name, created_at) "
            "VALUES (?, 'FUNCTION', 'src/unused.py', 'unused', '', ?)",
            (repo_id, now),
        ).lastrowid
        unused_anchor_id = store.conn.execute(
            "INSERT INTO anchors (entity_id, repo_id, created_at) VALUES (?, ?, ?)",
            (unused_entity_id, repo_id, now),
        ).lastrowid

        observation_id = store.conn.execute(
            "INSERT INTO observations "
            "(anchor_id, branch_id, repo_id, file_path, ast_symbol, content_hash, recorded_at, "
            "valid_from, valid_until, created_revision, closed_revision) "
            "VALUES (?, ?, ?, 'src/main.py', NULL, 'hash-observation', ?, ?, NULL, 1, NULL)",
            (anchor_id, root_id, repo_id, now, now),
        ).lastrowid

        def insert_memory(content, assigned_anchor):
            return store.conn.execute(
                "INSERT INTO episodic_memories "
                "(repo_id, branch_id, anchor_id, content, category, confidence, status, "
                "superseded_by_memory_id, created_revision, closed_revision, created_at, importance, tags) "
                "VALUES (?, ?, ?, ?, 'decision', 'assumed', 'ACTIVE', NULL, 1, NULL, ?, 3, 'original-tags')",
                (repo_id, root_id, assigned_anchor, content, now),
            ).lastrowid

        nullable_anchor_memory_id = insert_memory("nullable-anchor memory", anchor_id)
        dependency_target_id = insert_memory("dependency target", None)
        standalone_memory_id = insert_memory("standalone history row", None)

        dependency_id = store.conn.execute(
            "INSERT INTO memory_dependencies "
            "(repo_id, source_memory_id, target_memory_id, relation_type, created_revision) "
            "VALUES (?, ?, ?, 'SUPPORTS', 1)",
            (repo_id, nullable_anchor_memory_id, dependency_target_id),
        ).lastrowid
        store.conn.commit()
        return {
            "path": db_path,
            "repo_id": repo_id,
            "root_id": root_id,
            "anchor_id": anchor_id,
            "unused_anchor_id": unused_anchor_id,
            "observation_id": observation_id,
            "nullable_anchor_memory_id": nullable_anchor_memory_id,
            "dependency_target_id": dependency_target_id,
            "standalone_memory_id": standalone_memory_id,
            "dependency_id": dependency_id,
        }
    finally:
        store.close()


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_historical_rows_cannot_be_deleted_even_without_referencing_rows(tmp_path, monkeypatch):
    data = _fixture(tmp_path, monkeypatch)
    attempts = [
        (
            "anchors",
            "anchor_id",
            data["unused_anchor_id"],
            "anchors are immutable: deletion is never permitted",
        ),
        (
            "observations",
            "observation_id",
            data["observation_id"],
            "observation history is immutable: deletion is never permitted",
        ),
        (
            "episodic_memories",
            "memory_id",
            data["standalone_memory_id"],
            "episodic history is immutable: deletion is never permitted",
        ),
        (
            "memory_dependencies",
            "dependency_id",
            data["dependency_id"],
            "dependency history is immutable: deletion is never permitted",
        ),
    ]
    for table, key, row_id, message in attempts:
        conn = _connect(data["path"])
        try:
            with pytest.raises(sqlite3.IntegrityError, match=message):
                conn.execute(f"DELETE FROM {table} WHERE {key}=?", (row_id,))
        finally:
            conn.close()


def test_dependency_relation_cannot_be_rewritten_directly(tmp_path, monkeypatch):
    data = _fixture(tmp_path, monkeypatch)
    with _connect(data["path"]) as conn:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="dependency history is immutable: direct updates are not permitted",
        ):
            conn.execute(
                "UPDATE memory_dependencies SET relation_type='CONTRADICTS' WHERE dependency_id=?",
                (data["dependency_id"],),
            )


def test_observation_nullable_ast_symbol_is_immutable_during_closure(tmp_path, monkeypatch):
    data = _fixture(tmp_path, monkeypatch)
    with _connect(data["path"]) as conn:
        conn.execute(
            "UPDATE repositories SET current_revision=2 WHERE repo_id=?",
            (data["repo_id"],),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable-field mutation"):
            conn.execute(
                "UPDATE observations SET ast_symbol='main', closed_revision=2 "
                "WHERE observation_id=?",
                (data["observation_id"],),
            )


def test_episodic_nullable_anchor_cannot_change_during_closure(tmp_path, monkeypatch):
    data = _fixture(tmp_path, monkeypatch)
    with _connect(data["path"]) as conn:
        conn.execute(
            "UPDATE repositories SET current_revision=2 WHERE repo_id=?",
            (data["repo_id"],),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable-field mutation"):
            conn.execute(
                "UPDATE episodic_memories SET anchor_id=NULL, status='INVALIDATED', closed_revision=2 "
                "WHERE memory_id=?",
                (data["nullable_anchor_memory_id"],),
            )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("tags", "rewritten-tags"),
        ("importance", 5),
    ],
)
def test_episodic_metadata_cannot_be_rewritten_as_part_of_closure(tmp_path, monkeypatch, column, value):
    data = _fixture(tmp_path, monkeypatch)
    with _connect(data["path"]) as conn:
        conn.execute(
            "UPDATE repositories SET current_revision=2 WHERE repo_id=?",
            (data["repo_id"],),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable-field mutation"):
            conn.execute(
                f"UPDATE episodic_memories SET {column}=?, status='INVALIDATED', closed_revision=2 "
                "WHERE memory_id=?",
                (value, data["dependency_target_id"]),
            )
