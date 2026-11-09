"""Goal acceptance is based on predeclared observed evidence, not narratives."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from task_goal import propose_goal, goal_snapshot, format_goal_snapshot
from task_evidence import record_observation
from task_checks import record_check, check_snapshot


class GoalGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        src = self.root / "src"
        src.mkdir()
        self.file = src / "auth.py"
        self.file.write_text("def login(): return False\n")
        self.task = "fix login bug"
        self.session = "opencode-123"
        self.tests = "python -m pytest -q test_auth.py"

    def tearDown(self):
        self.tmp.cleanup()

    def contract(self, profile="implementation", *, regression=False,
                 origin="self_run"):
        criteria = [
            {"id": "auth_source", "type": "source_read", "path": "src/auth.py"},
            {"id": "auth_test", "type": "regression" if regression else "check",
             "command": self.tests, "origin": origin},
        ]
        return propose_goal(self.root, self.session, self.task, profile, criteria)

    def read(self):
        return record_observation(self.root, self.session, self.task,
                                  "read", "src/auth.py")

    def edit(self, new_text=None):
        if new_text is not None:
            self.file.write_text(new_text)
        return record_observation(self.root, self.session, self.task,
                                  "edit", "src/auth.py")

    def check(self, code=0, command=None, origin="self_run"):
        return record_check(
            self.root, self.session, self.task, command or self.tests,
            code, origin=origin,
        )

    def state(self):
        return goal_snapshot(self.root, self.session, self.task)

    def statuses(self):
        return {r["id"]: r["status"] for r in self.state()["criteria"]}

    def test_real_cli_register_and_show_roundtrip(self):
        criteria = [
            {"id": "auth_source", "type": "source_read", "path": "src/auth.py"},
            {"id": "auth_test", "type": "check", "command": self.tests},
        ]
        base = [sys.executable, "-m", "task_goal", "--project-root",
                str(self.root), "--session-id", self.session,
                "--task", self.task]
        registered = subprocess.run(
            base + ["--profile", "implementation"],
            input=json.dumps(criteria), text=True, capture_output=True,
            check=False, timeout=20,
        )
        self.assertEqual(registered.returncode, 0, registered.stderr)
        self.assertEqual(json.loads(registered.stdout)["status"],
                         "registered_proposal")
        shown = subprocess.run(
            base + ["--show"], text=True, capture_output=True,
            check=False, timeout=20,
        )
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["status"], "needs_evidence")

    def test_unscoped_check_does_not_satisfy_implementation(self):
        self.contract()
        self.check(0)
        self.assertEqual(self.statuses()["auth_test"], "unscoped_check")
        self.edit()
        self.read()
        self.assertNotEqual(self.state()["status"], "evidence_ready_for_review")
        self.check(0)
        self.assertEqual(self.state()["status"], "evidence_ready_for_review")

    def test_unregistered_goal_cannot_claim_completion(self):
        report = self.state()
        self.assertEqual(report["status"], "unregistered")
        self.assertFalse(report["verified"])
        self.assertFalse(report["accepted_by_owner"])
        self.assertIn("Define", report["next_action"])

    def test_predeclared_goal_requires_new_source_edit_and_exact_check(self):
        self.contract()
        first = self.state()
        self.assertEqual(first["status"], "needs_evidence")
        self.assertEqual(first["met"], 0)
        self.read()
        self.edit("def login(): return True\n")
        self.read()
        self.check(0)
        last = self.state()
        self.assertEqual(last["status"], "evidence_ready_for_review")
        self.assertEqual(last["met"], 2)
        self.assertEqual(last["total"], 2)
        self.assertFalse(last["verified"])
        self.assertFalse(last["accepted_by_owner"])
        self.assertIn("Review the implementation", last["next_action"])
        self.assertIn("evidence ready for review", format_goal_snapshot(last))

    def test_check_before_contract_never_satisfies_later_goal(self):
        self.read()
        self.edit()
        self.read()
        self.check(0)
        self.contract()
        self.assertEqual(self.state()["met"], 0)
        self.assertEqual(self.statuses()["auth_test"], "needs_check")
        self.assertEqual(self.statuses()["auth_source"], "needs_source_read")

    def test_registering_identical_contract_does_not_erase_evidence(self):
        self.contract()
        self.edit()
        self.read()
        self.check(0)
        self.assertEqual(self.state()["status"], "evidence_ready_for_review")
        unchanged = self.contract()
        self.assertEqual(unchanged["status"], "unchanged")
        self.assertEqual(self.state()["status"], "evidence_ready_for_review")

    def test_changed_criteria_resets_prior_evidence_instead_of_goalpost_shift(self):
        self.contract()
        self.edit()
        self.read()
        self.check(0)
        self.assertEqual(self.state()["status"], "evidence_ready_for_review")
        changed = propose_goal(self.root, self.session, self.task, "implementation", [
            {"id": "auth_source", "type": "source_read", "path": "src/auth.py"},
            {"id": "auth_test", "type": "check", "command": self.tests},
            {"id": "separate_lint", "type": "check",
             "command": "python -m ruff check src/auth.py"},
        ])
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(self.state()["met"], 0)
        self.assertEqual(self.state()["status"], "needs_evidence")

    def test_unrelated_check_or_runtime_metadata_cannot_substitute(self):
        self.contract()
        self.edit()
        self.read()
        self.check(0, command="ruff check src/auth.py")
        self.assertEqual(self.statuses()["auth_test"], "needs_check")
        self.check(0, origin="ide_runtime")
        self.assertEqual(self.statuses()["auth_test"], "wrong_evidence_origin")
        self.check(0, origin="self_run")
        self.assertEqual(self.state()["status"], "evidence_ready_for_review")

    def test_failing_or_stale_source_blocks_goal(self):
        self.contract()
        self.edit()
        self.read()
        self.check(0)
        self.file.write_text("def login(): return None\n")
        stale = self.state()
        self.assertEqual(stale["status"], "blocked")
        self.assertEqual(self.statuses()["auth_source"], "stale_source")
        self.assertEqual(self.statuses()["auth_test"], "stale_check")
        self.read()
        self.check(1)
        self.assertEqual(self.statuses()["auth_test"], "failed_check")
        self.check(0)
        self.assertEqual(self.state()["status"], "evidence_ready_for_review")

    def test_red_green_repair_requires_failure_before_edit_then_pass(self):
        self.contract(profile="repair", regression=True)
        self.read()
        self.check(1)
        self.edit("def login(): return True\n")
        self.read()
        self.check(0)
        state = self.state()
        self.assertEqual(state["status"], "evidence_ready_for_review")
        self.assertEqual(self.statuses()["auth_test"], "evidence_observed")

    def test_repair_cannot_manufacture_red_green_without_intervening_edit(self):
        self.contract(profile="repair", regression=True)
        self.edit()
        self.read()
        self.check(1)
        self.check(0)
        self.assertEqual(self.statuses()["auth_test"], "needs_red_green_sequence")

    def test_failed_check_cannot_be_cleared_by_different_passing_command(self):
        self.contract()
        self.edit()
        self.read()
        self.check(1)
        self.check(0, command="ruff check src/auth.py")
        self.assertEqual(self.statuses()["auth_test"], "failed_check")
        self.assertEqual(self.state()["status"], "blocked")

    def test_manual_review_is_never_self_attested(self):
        criteria = [
            {"id": "auth_source", "type": "source_read", "path": "src/auth.py"},
            {"id": "auth_test", "type": "check", "command": self.tests},
            {"id": "reviewer", "type": "manual_review"},
        ]
        propose_goal(self.root, self.session, self.task, "implementation", criteria)
        self.edit()
        self.read()
        self.check(0)
        self.assertEqual(self.statuses()["reviewer"], "requires_external_review")
        self.assertEqual(self.state()["status"], "needs_evidence")
        self.assertFalse(self.state()["accepted_by_owner"])

    def test_invalid_or_trivial_contract_is_rejected(self):
        bad = [
            [{"id": "only_test", "type": "check", "command": self.tests}],
            [{"id": "bad inject\n", "type": "source_read", "path": "src/auth.py"}],
            [{"id": "path", "type": "source_read", "path": "../secret.py"},
             {"id": "check", "type": "check", "command": self.tests}],
            [{"id": "source", "type": "source_read", "path": "src/auth.py"},
             {"id": "cmd", "type": "check", "command": "pytest || true"}],
        ]
        for criteria in bad:
            with self.subTest(criteria=criteria):
                with self.assertRaises(ValueError):
                    propose_goal(self.root, self.session, self.task,
                                 "implementation", criteria)
        self.assertEqual(self.state()["status"], "unregistered")

    def test_task_session_separation_and_no_raw_commands_in_db(self):
        self.contract()
        self.assertEqual(goal_snapshot(self.root, "other", self.task)["status"],
                         "unregistered")
        self.assertEqual(goal_snapshot(self.root, self.session, "other task")["status"],
                         "unregistered")
        db = self.root / ".cognigraph" / "task_evidence.sqlite3"
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT session_key,task_key,criteria_json "
                           "FROM task_goal_contracts").fetchone()
        conn.close()
        self.assertNotIn(self.task, str(row))
        self.assertNotIn(self.session, str(row))
        self.assertNotIn(self.tests, str(row))
        self.assertEqual(len(row[0]), 64)
        self.assertEqual(len(row[1]), 64)
        self.assertEqual(json.loads(row[2])[1]["check_kind"], "pytest")

    def test_symlink_source_cannot_be_falsely_accepted(self):
        self.contract()
        self.edit()
        self.read()
        self.check(0)
        external = Path(self.tmp.name) / "external.py"
        external.write_text(self.file.read_text())
        self.file.unlink()
        try:
            self.file.symlink_to(external)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertEqual(self.statuses()["auth_source"], "stale_source")
        self.assertNotEqual(self.state()["status"], "evidence_ready_for_review")

    def test_exact_check_query_retains_multiple_independent_commands(self):
        self.read()
        self.check(1, command=self.tests)
        self.check(0, command="ruff check src/auth.py")
        first = check_snapshot(self.root, self.session, self.task,
                               command=self.tests)
        self.assertEqual(first["status"], "observed_nonzero_exit")
        second = check_snapshot(self.root, self.session, self.task,
                                command="ruff check src/auth.py")
        self.assertEqual(second["status"], "observed_zero_exit")


class GoalBridgeTests(unittest.TestCase):
    def test_goal_controls_ide_next_action_and_survives_context_reconstruction(self):
        from memory_store import MemoryStore
        from opencode_context import get_plugin_context
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".cognigraph").mkdir()
            file = root / "src/auth.py"
            file.parent.mkdir()
            file.write_text("def login(): return False\n")
            store = MemoryStore(str(root / ".cognigraph" / "memory.db"))
            try:
                store.write("component", "Login handler in auth.py",
                            file_path="src/auth.py", force=True, embed=False)
            finally:
                store.close()
            propose_goal(root, "s1", "fix login", "repair", [
                {"id": "source", "type": "source_read", "path": "src/auth.py"},
                {"id": "regression", "type": "regression",
                 "command": "python -m pytest -q test_auth.py"},
            ])
            # First context has no goal evidence. No task-completion claim.
            first = get_plugin_context(directory, "fix login",
                                       max_chars=2900, session_id="s1")
            self.assertEqual(first["task_goal"]["status"], "needs_evidence")
            self.assertIn("Goal gate", first["context_markdown"])
            self.assertIn("source", first["context_markdown"])
            record_observation(root, "s1", "fix login", "read", "src/auth.py")
            record_check(root, "s1", "fix login",
                         "python -m pytest -q test_auth.py", 1,
                         origin="self_run")
            file.write_text("def login(): return True\n")
            record_observation(root, "s1", "fix login", "edit", "src/auth.py")
            record_observation(root, "s1", "fix login", "read", "src/auth.py")
            record_check(root, "s1", "fix login",
                         "python -m pytest -q test_auth.py", 0,
                         origin="self_run")
            second = get_plugin_context(directory, "fix login",
                                        max_chars=2900, session_id="s1",
                                        refresh=True)
            self.assertEqual(second["task_goal"]["status"],
                             "evidence_ready_for_review")
            self.assertFalse(second["task_goal"]["verified"])
            self.assertIn("Review the implementation", second["context_markdown"])

    def test_goal_without_memory_index_still_visible(self):
        from opencode_context import get_plugin_context
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src/x.py").write_text("pass\n")
            propose_goal(root, "s2", "implement new behavior", "implementation", [
                {"id": "src", "type": "source_read", "path": "src/x.py"},
                {"id": "test", "type": "check", "command": "pytest -q"},
            ])
            result = get_plugin_context(
                directory, "implement new behavior",
                session_id="s2", max_chars=1200,
            )
            self.assertEqual(result["status"], "ok")
            self.assertIn("Goal gate", result["context_markdown"])
            self.assertFalse(result["task_goal"]["verified"])


if __name__ == "__main__":
    unittest.main()
