"""Read-only source freshness detection and model-visible warning tests."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from pm_freshness import (
    check_source_freshness,
    freshness_note,
    project_root_for_db,
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SourceFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.path = self.root / "src" / "handler.py"
        self.path.parent.mkdir()
        self.before = "def handler():\n    return 1\n"
        self.path.write_bytes(self.before.encode("utf-8"))
        self.db = self.root / ".cognigraph" / "memory.db"
        self.db.parent.mkdir()
        self.db.touch()
        self.fact = {
            "file_path": "src/handler.py",
            "line_start": 1,
            "line_end": 2,
            "code_hash": digest(self.before),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_matching_source_is_current(self):
        self.assertEqual(check_source_freshness(self.fact, self.root)["status"], "CURRENT")
        self.assertEqual(freshness_note(self.fact, str(self.db)), "")

    def test_changed_source_is_warning_not_deprecation(self):
        self.path.write_bytes(self.before.replace("return 1", "return 2").encode("utf-8"))
        report = check_source_freshness(self.fact, self.root)
        self.assertEqual(report["status"], "POSSIBLY_STALE")
        self.assertIn("SOURCE MAY HAVE CHANGED", freshness_note(self.fact, str(self.db)))

    def test_line_shift_detects_relocation_without_changing_anchor(self):
        self.path.write_bytes(("# new header\n" + self.before).encode("utf-8"))
        report = check_source_freshness(self.fact, self.root)
        self.assertEqual(report["status"], "RELOCATED")
        self.assertEqual(report["current_lines"], [2, 3])
        self.assertEqual(self.fact["line_start"], 1)

    def test_deleted_source_is_missing_not_auto_deprecated(self):
        self.path.unlink()
        self.assertEqual(check_source_freshness(self.fact, self.root)["status"], "SOURCE_MISSING")
        self.assertIn("SOURCE FILE MISSING", freshness_note(self.fact, str(self.db)))

    def test_whole_file_hash_supported(self):
        fact = dict(self.fact, line_start=None, line_end=None)
        self.assertEqual(check_source_freshness(fact, self.root)["status"], "CURRENT")
        self.path.write_bytes(("# change\n" + self.before).encode("utf-8"))
        self.assertEqual(check_source_freshness(fact, self.root)["status"], "POSSIBLY_STALE")

    def test_outside_project_and_absolute_path_rejected(self):
        for path in ["../outside.py", "/tmp/example.py", "C:\\temp\\escape.py"]:
            fact = dict(self.fact, file_path=path)
            report = check_source_freshness(fact, self.root)
            self.assertEqual(report["status"], "UNVERIFIED")
            self.assertEqual(report["reason"], "outside_project")

    def test_symlink_escape_does_not_read_source(self):
        outside = Path(self.tmp.name) / "outside.py"
        outside.write_text(self.before)
        link = self.root / "src" / "escape.py"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not available on this platform")
        fact = dict(self.fact, file_path="src/escape.py")
        report = check_source_freshness(fact, self.root)
        self.assertEqual(report["reason"], "outside_project")

    def test_no_hash_and_invalid_anchor_remain_unverified(self):
        fact = dict(self.fact, code_hash=None)
        self.assertEqual(check_source_freshness(fact, self.root)["status"], "UNVERIFIED")
        fact = dict(self.fact, line_start=4, line_end=2)
        self.assertEqual(check_source_freshness(fact, self.root)["status"], "UNVERIFIED")

    def test_large_sources_not_loaded(self):
        self.path.write_bytes(b"x" * 50)
        report = check_source_freshness(self.fact, self.root, max_bytes=10)
        self.assertEqual(report, {"status": "UNVERIFIED", "reason": "size_limit"})

    def test_non_project_database_does_not_guess_root(self):
        self.assertIsNone(project_root_for_db(str(self.root / "memory.db")))
        self.assertEqual(project_root_for_db(str(self.db)), self.root)


class LiveContextFreshnessTests(unittest.TestCase):
    def test_file_and_task_briefings_flag_drift_without_mutating_fact(self):
        from memory_store import MemoryStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".cognigraph" / "memory.db"
            db.parent.mkdir()
            file = root / "handler.py"
            file.write_bytes(b"def login_handler():\n    return 1\n")
            original = file.read_bytes()
            store = MemoryStore(str(db))
            try:
                fid = store.write(
                    "component", "login_handler validates session cookies for user login",
                    confidence="confirmed", file_path="handler.py",
                    line_start=1, line_end=2, code_hash=hashlib.sha256(original).hexdigest(),
                    force=True, embed=False,
                )
                self.assertIsInstance(fid, int)
                store._query_encode = lambda query: None
                self.assertNotIn("SOURCE MAY HAVE CHANGED", store.get_file_context("handler.py"))
                file.write_bytes(b"def login_handler():\n    return 2\n")
                file_ctx = store.get_file_context("handler.py")
                self.assertIn("SOURCE MAY HAVE CHANGED", file_ctx)
                task_ctx = store.get_context("login_handler validates session cookies", max_chars=4000)
                self.assertIn("SOURCE MAY HAVE CHANGED", task_ctx)
                unchanged = store.get_by_id(fid)
                self.assertEqual(unchanged["confidence"], "confirmed")
                self.assertIsNone(unchanged["deprecated_at"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
