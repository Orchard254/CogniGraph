"""Durable task observations and provenance invalidation."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from task_evidence import (
    record_observation, task_snapshot, format_task_snapshot, safe_project_file,
)


class TaskEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.file = self.root / "src" / "auth.py"
        self.file.parent.mkdir()
        self.file.write_text("def authenticate():\n    return True\n")

    def tearDown(self):
        self.temp.cleanup()

    def record(self, kind="read", session="ide-123", task="fix authentication"):
        return record_observation(self.root, session, task, kind, "src/auth.py")

    def snapshot(self, session="ide-123", task="fix authentication"):
        return task_snapshot(self.root, session, task)

    def test_durable_across_reopen_and_no_raw_task_or_session_stored(self):
        self.assertTrue(self.record()["recorded"])
        observed = self.snapshot()
        self.assertEqual(observed["observed_files"], ["src/auth.py"])
        self.assertFalse(observed["verified"])
        db = self.root / ".cognigraph" / "task_evidence.sqlite3"
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT session_key,task_key,kind,file_path,source_hash "
            "FROM task_evidence_events"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0][0]), 64)
        self.assertEqual(len(rows[0][1]), 64)
        self.assertNotIn("fix authentication", str(rows))
        self.assertNotIn("ide-123", str(rows))

    def test_edit_invalidates_read_and_reread_restores_observation(self):
        self.record("read")
        self.file.write_text("def authenticate():\n    return False\n")
        self.record("edit")
        after_edit = self.snapshot()
        self.assertEqual(after_edit["observed_files"], [])
        self.assertEqual(after_edit["edited_files"], ["src/auth.py"])
        self.record("read")
        after_read = self.snapshot()
        self.assertEqual(after_read["observed_files"], ["src/auth.py"])
        self.assertEqual(after_read["edited_files"], [])
        self.assertFalse(after_read["verified"])

    def test_external_edit_invalidates_source_digest(self):
        self.record()
        self.file.write_text("def authenticate():\n    return False\n")
        state = self.snapshot()
        self.assertEqual(state["observed_files"], [])
        self.assertEqual(state["stale_files"], ["src/auth.py"])

    def test_task_session_and_project_are_isolated(self):
        self.record()
        self.assertFalse(self.snapshot(session="other-session")["event_count"])
        self.assertFalse(self.snapshot(task="different objective")["event_count"])
        outside = Path(self.temp.name) / "other"
        outside.mkdir()
        self.assertFalse(task_snapshot(outside, "ide-123", "fix authentication")["event_count"])

    def test_unsafe_path_rejected_without_creating_ledger(self):
        before = self.root / ".cognigraph" / "task_evidence.sqlite3"
        (self.root / ".env").write_text("password=example\n")
        external = Path(self.temp.name) / "outside.py"
        external.write_text("private_token = 7\n")
        for path in ("../outside.py", ".env", "src/missing.py",
                     "src/auth.py\nx", "src/auth.py" + chr(96)):
            self.assertFalse(record_observation(
                self.root, "ide-123", "fix authentication", "read", path
            )["recorded"])
        try:
            (self.root / "src" / "link.py").symlink_to(external)
        except (OSError, NotImplementedError):
            pass
        else:
            self.assertFalse(record_observation(
                self.root, "ide-123", "fix authentication", "read", "src/link.py"
            )["recorded"])
        self.assertFalse(before.exists())
        with self.assertRaises(ValueError):
            record_observation(self.root, "ide-123", "fix authentication",
                               "test_pass", "src/auth.py")

    def test_data_directory_symlink_cannot_escape_project(self):
        external = Path(self.temp.name) / "other-data"
        external.mkdir()
        try:
            (self.root / ".cognigraph").symlink_to(external)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ValueError):
            self.record()
        self.assertFalse((external / "task_evidence.sqlite3").exists())

    def test_large_source_not_hashed_as_completed_read(self):
        self.file.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        result = self.record()
        self.assertEqual(result["status"], "source_unavailable")
        self.assertFalse((self.root / ".cognigraph" / "task_evidence.sqlite3").exists())

    def test_bounded_history_without_turning_edit_into_verification(self):
        for i in range(101):
            self.record("edit" if i % 2 else "read")
        state = self.snapshot()
        self.assertEqual(state["event_count"], 96)
        self.assertEqual(state["edited_files"], [], "final event is a read")
        self.assertFalse(state["verified"])

    def test_summary_does_not_truncate_paths(self):
        self.record()
        snap = self.snapshot()
        self.assertEqual(format_task_snapshot(snap, max_chars=50), "")
        rendered = format_task_snapshot(snap, max_chars=380)
        self.assertLessEqual(len(rendered), 380)
        self.assertIn("src/auth.py", rendered)
        self.assertIn("not proof", rendered)


class IDEBridgeEvidenceTests(unittest.TestCase):
    def test_real_bridge_persists_read_across_separate_context_invocations(self):
        from opencode_context import get_plugin_context, record_plugin_evidence
        from memory_store import MemoryStore
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".cognigraph").mkdir()
            file = root / "src/auth.py"
            file.parent.mkdir()
            file.write_text("def login():\n    return True\n")
            store = MemoryStore(str(root / ".cognigraph" / "memory.db"))
            try:
                store.write("component", "Login handler restores sessions",
                            file_path="src/auth.py", force=True, embed=False)
            finally:
                store.close()
            report = record_plugin_evidence(
                d, "session-42", "fix login", "read", "src/auth.py",
            )
            self.assertTrue(report["recorded"])
            first = get_plugin_context(
                d, "fix login", session_id="session-42",
                file_paths=["src/auth.py"], phase="inspect", max_chars=1600,
            )
            self.assertEqual(first["task_evidence"]["observed_files"], ["src/auth.py"])
            self.assertIn("source read observed", first["context_markdown"])
            self.assertIn("Read and unchanged", first["context_markdown"])
            file.write_text("def login():\n    return False\n")
            record_plugin_evidence(d, "session-42", "fix login", "edit", "src/auth.py")
            second = get_plugin_context(
                d, "fix login", session_id="session-42",
                file_paths=["src/auth.py"], phase="verify", max_chars=1600,
            )
            self.assertEqual(second["task_evidence"]["edited_files"], ["src/auth.py"])
            self.assertNotIn("source read observed", second["context_markdown"])
            self.assertIn("Edited; source re-read needed", second["context_markdown"])

    def test_prior_source_read_guides_new_base_context_without_file_focus(self):
        from opencode_context import get_plugin_context, record_plugin_evidence
        from memory_store import MemoryStore
        from unittest.mock import patch
        import sys
        import types
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".cognigraph").mkdir()
            file = root / "src/auth.py"
            file.parent.mkdir()
            file.write_text("def login():\n    return True\n")
            store = MemoryStore(str(root / ".cognigraph" / "memory.db"))
            try:
                store.write("component", "Login authentication handler",
                            file_path="src/auth.py", force=True, embed=False)
            finally:
                store.close()
            record_plugin_evidence(d, "session-r", "fix login", "read", "src/auth.py")
            class NoVectorStore(MemoryStore):
                def _query_encode(self, text):
                    return None
            with patch.dict(sys.modules, {
                "memory_store": types.SimpleNamespace(MemoryStore=NoVectorStore),
            }):
                answer = get_plugin_context(d, "fix login", session_id="session-r",
                                            max_chars=2400)
            self.assertIn("Read and unchanged", answer["context_markdown"])
            self.assertIn("source read observed", answer["context_markdown"])
            self.assertFalse(answer["task_evidence"]["verified"])

    def test_ledger_still_helps_before_memory_database_is_initialized(self):
        from opencode_context import get_plugin_context, record_plugin_evidence
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "x.py").write_text("value = 1\n")
            record_plugin_evidence(d, "session", "inspect x", "read", "x.py")
            response = get_plugin_context(d, "inspect x", session_id="session")
            self.assertEqual(response["status"], "ok")
            self.assertIn("x.py", response["context_markdown"])
            self.assertIsNone(response["retrieval_id"])


if __name__ == "__main__":
    unittest.main()
