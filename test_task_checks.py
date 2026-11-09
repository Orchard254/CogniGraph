"""Verification must reflect process exits and code revision, never agent claims."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from task_checks import (
    classify_check, record_check, check_snapshot, format_check_snapshot,
    run_check,
)
from task_evidence import record_observation


class CheckEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        self.source = self.root / "src" / "auth.py"
        self.source.write_text("def login(): return True\n")
        self.session = "opencode-123"
        self.task = "fix login"

    def tearDown(self):
        self.tmp.cleanup()

    def edit(self):
        return record_observation(
            self.root, self.session, self.task, "edit", "src/auth.py"
        )

    def read(self):
        return record_observation(
            self.root, self.session, self.task, "read", "src/auth.py"
        )

    def check(self, code=0, cmd="python -m pytest -q"):
        return record_check(self.root, self.session, self.task, cmd, code)

    def snapshot(self):
        return check_snapshot(self.root, self.session, self.task)

    def test_strict_command_classifier_does_not_accept_masked_failures(self):
        allowed = (
            "pytest -q",
            "python -m pytest -q",
            "python3 -m unittest discover",
            "npm test",
            "npm run test",
            "node --test tests/example.js",
            "cargo test",
            "go test ./...",
            "ruff check .",
        )
        self.assertTrue(all(classify_check(command) for command in allowed))
        rejected = (
            "pytest || true", "pytest; echo passed",
            "pytest && echo success", "pytest | tee log.txt",
            "pytest > out.txt", "echo pytest",
            "python -c 'print(0)'",
            "python -m pytest\nexit 0",
            "npm install", "git status", "npm run deploy",
            "pytest $(echo -q)",
        )
        self.assertTrue(all(classify_check(command) is None for command in rejected))
        self.assertFalse(self.check(0, "pytest || true")["recorded"])
        self.assertEqual(self.snapshot()["status"], "not_observed")

    def test_numeric_runtime_exit_only_not_success_narration(self):
        self.read()
        with self.assertRaises(ValueError):
            self.check("0")
        with self.assertRaises(ValueError):
            self.check(True)
        with self.assertRaises(ValueError):
            self.check(None)
        report = self.check(1)
        self.assertTrue(report["recorded"])
        state = self.snapshot()
        self.assertEqual(state["status"], "observed_nonzero_exit")
        self.assertEqual(state["exit_code"], 1)
        self.assertFalse(state["verified"])
        self.assertIn("exited nonzero", format_check_snapshot(state))

    def test_pass_then_edit_then_reread_remains_stale_until_new_check(self):
        self.read()
        self.edit()
        self.assertTrue(self.check(0)["recorded"])
        self.assertEqual(self.snapshot()["status"], "observed_zero_exit")
        # A subsequent edit invalidates the prior check even when source bytes
        # happen to be the same (undo, formatter, repeated edit).
        self.edit()
        self.assertEqual(self.snapshot()["status"], "stale")
        self.read()
        self.assertEqual(self.snapshot()["status"], "stale")
        self.check(1)
        self.assertEqual(self.snapshot()["status"], "observed_nonzero_exit")
        self.check(0)
        self.assertEqual(self.snapshot()["status"], "observed_zero_exit")
        self.assertFalse(self.snapshot()["verified"])

    def test_external_source_change_invalidates_check(self):
        self.read()
        self.check(0)
        self.source.write_text("def login(): return False\n")
        self.assertEqual(self.snapshot()["status"], "stale")

    def test_untracked_check_is_real_exit_but_unscoped(self):
        report = self.check(0)
        self.assertTrue(report["recorded"])
        self.assertEqual(report["scope"], "unscoped")
        state = self.snapshot()
        self.assertEqual(state["status"], "unscoped_zero_exit")
        self.assertFalse(state["verified"])

    def test_task_identity_and_project_isolation(self):
        self.read()
        self.check(0)
        self.assertEqual(
            check_snapshot(self.root, "other-session", self.task)["status"],
            "not_observed",
        )
        self.assertEqual(
            check_snapshot(self.root, self.session, "different-task")["status"],
            "not_observed",
        )
        other = self.root / "other"
        other.mkdir()
        self.assertEqual(
            check_snapshot(other, self.session, self.task)["status"],
            "not_observed",
        )

    def test_no_raw_command_or_task_text_in_ledger(self):
        self.read()
        self.check(0, "python -m pytest -q")
        db = self.root / ".cognigraph" / "task_evidence.sqlite3"
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT session_key,task_key,command_hash,check_kind,exit_code "
            "FROM task_check_events"
        ).fetchone()
        conn.close()
        self.assertEqual(len(row[0]), 64)
        self.assertEqual(len(row[1]), 64)
        self.assertEqual(len(row[2]), 64)
        self.assertEqual(row[3], "pytest")
        self.assertEqual(row[4], 0)
        self.assertNotIn("fix login", str(row))
        self.assertNotIn("python -m pytest", str(row))

    def test_evidence_database_symlink_cannot_escape_repository(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "foreign.sqlite3"
            external.write_bytes(b"")
            directory = self.root / ".cognigraph"
            directory.mkdir()
            try:
                (directory / "task_evidence.sqlite3").symlink_to(external)
            except (OSError, NotImplementedError):
                self.skipTest("symlink unavailable")
            with self.assertRaises(ValueError):
                self.check(0)
            self.assertEqual(external.read_bytes(), b"")

    def test_external_cwd_is_rejected(self):
        outside = self.root.parent
        result = record_check(
            self.root, self.session, self.task, "pytest -q", 0,
            cwd=str(outside),
        )
        self.assertFalse(result["recorded"])
        self.assertEqual(self.snapshot()["status"], "not_observed")

    def test_bounded_check_history(self):
        self.read()
        for code in range(39):
            self.check(code % 2)
        db = self.root / ".cognigraph" / "task_evidence.sqlite3"
        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM task_check_events").fetchone()[0]
        conn.close()
        self.assertEqual(count, 32)

    def test_direct_runner_measures_actual_exit_without_shell(self):
        test_file = self.root / "test_demo.py"
        test_file.write_text(
            "import unittest\n"
            "class TestDemo(unittest.TestCase):\n"
            "    def test_truth(self): self.assertTrue(True)\n"
        )
        self.read()
        argv = [sys.executable, "-m", "unittest", "-q", "test_demo"]
        first = run_check(self.root, self.session, self.task, argv, timeout=30)
        self.assertEqual(first["exit_code"], 0)
        self.assertEqual(first["origin"], "self_run")
        self.assertEqual(self.snapshot()["status"], "observed_zero_exit")
        test_file.write_text(
            "import unittest\n"
            "class TestDemo(unittest.TestCase):\n"
            "    def test_truth(self): self.assertTrue(False)\n"
        )
        second = run_check(self.root, self.session, self.task, argv, timeout=30)
        self.assertNotEqual(second["exit_code"], 0)
        self.assertEqual(self.snapshot()["status"], "observed_nonzero_exit")

    def test_plan_does_not_promote_one_check_to_task_completion(self):
        from pm_evidence import evaluate_evidence
        self.read()
        self.check(0)
        plan = evaluate_evidence(
            self.task, "debug", [], [],
            check_observation=self.snapshot(),
        )
        statuses = {x["id"]: x["status"] for x in plan["requirements"]}
        self.assertEqual(statuses["verification"], "exit_zero_observed")
        self.assertFalse(plan["verified"])
        self.check(1)
        plan = evaluate_evidence(
            self.task, "debug", [], [],
            check_observation=self.snapshot(),
        )
        self.assertEqual(plan["next_action"],
                         "Investigate the observed nonzero check exit before declaring success.")
        self.assertGreater(plan["unresolved_count"], 0)


class CheckBridgeTests(unittest.TestCase):
    def test_bridge_reports_structured_exit_and_marks_it_stale(self):
        from memory_store import MemoryStore
        from opencode_context import (
            record_plugin_evidence, record_plugin_check, get_plugin_context,
        )
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".cognigraph").mkdir()
            src = root / "src" / "auth.py"
            src.parent.mkdir()
            src.write_text("def login(): return True\n")
            store = MemoryStore(str(root / ".cognigraph" / "memory.db"))
            try:
                store.write(
                    "component", "Login handler in auth module",
                    file_path="src/auth.py", force=True, embed=False,
                )
            finally:
                store.close()
            record_plugin_evidence(d, "session-verify", "fix login",
                                   "read", "src/auth.py")
            record_plugin_check(d, "session-verify", "fix login",
                                "python -m pytest -q", 0, d)
            report = get_plugin_context(
                d, "fix login", max_chars=2400, session_id="session-verify",
            )
            self.assertEqual(report["task_check"]["status"], "observed_zero_exit")
            self.assertIn("exit zero observed", report["context_markdown"])
            self.assertIn("Check observation", report["context_markdown"])
            src.write_text("def login(): return False\n")
            second = get_plugin_context(
                d, "fix login", max_chars=2400, session_id="session-verify",
            )
            self.assertEqual(second["task_check"]["status"], "stale")
            self.assertIn("stale check", second["context_markdown"])
            self.assertFalse(second["task_check"]["verified"])


if __name__ == "__main__":
    unittest.main()
