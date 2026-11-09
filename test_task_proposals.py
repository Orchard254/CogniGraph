"""Behavior-driven checks of goal candidate discovery, challenge and adoption."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from task_proposals import (
    suggest_goal_contract, format_goal_proposal, audit_proposal,
)
from task_goal import propose_goal_ref, goal_snapshot


class ProposalDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        self.source = self.root / "src/login.py"
        self.source.write_text("def login(): return False\n")
        self.test = self.root / "tests/test_login.py"
        self.test.write_text("def test_login(): assert True\n")
        self.task = "Fix login bug"

    def tearDown(self):
        self.tmp.cleanup()

    def propose(self, task=None, **kwargs):
        return suggest_goal_contract(self.root, task or self.task, **kwargs)

    def test_python_repair_discovers_only_existing_targeted_test(self):
        before = self.root / ".cognigraph" / "task_evidence.sqlite3"
        plan = self.propose()
        self.assertEqual(plan["status"], "review_required")
        self.assertEqual(plan["profile"], "repair")
        self.assertEqual(plan["source_files"], ["src/login.py"])
        self.assertEqual(plan["source_basis"], "filename_match")
        self.assertEqual(plan["criteria"], [
            {"id": "source_1", "type": "source_read", "path": "src/login.py"},
            {"id": "regression_1", "type": "regression",
             "command": "python -m pytest -q tests/test_login.py"},
        ])
        self.assertTrue(plan["challenge"]["registerable"])
        self.assertFalse(plan["accepted_by_owner"])
        self.assertFalse(plan["registered"])
        self.assertFalse(plan["verified"])
        self.assertFalse(before.exists(), "candidate generation must be read-only")
        self.assertTrue(any(
            "Test filename matching does not prove" in warning
            for warning in plan["challenge"]["warnings"]
        ))

    def test_missing_test_never_invents_check_or_registerable_repair(self):
        self.test.unlink()
        plan = self.propose()
        self.assertEqual(plan["status"], "incomplete")
        self.assertEqual(len(plan["criteria"]), 1)
        self.assertEqual(plan["test_candidates"], [])
        self.assertFalse(plan["challenge"]["registerable"])
        self.assertIn("No existing source-related runnable test",
                      plan["challenge"]["blockers"][0])

    def test_unknown_or_equally_named_source_does_not_choose_arbitrarily(self):
        (self.root / "src/wallet.py").write_text("pass\n")
        (self.root / "src/account.py").write_text("pass\n")
        missing = self.propose("Fix mystery behavior")
        self.assertEqual(missing["status"], "incomplete")
        self.assertEqual(missing["source_files"], [])
        self.assertEqual(missing["criteria"], [])
        (self.root / "src/legacy").mkdir()
        (self.root / "src/legacy/login.py").write_text("pass\n")
        ambiguous = self.propose()
        self.assertEqual(ambiguous["status"], "incomplete")
        self.assertEqual(ambiguous["source_files"], [])
        self.assertTrue(ambiguous["plausible_sources"])
        self.assertIn("Multiple equally plausible",
                      ambiguous["challenge"]["blockers"][0])

    def test_observed_focus_disambiguates_but_does_not_verify(self):
        (self.root / "src/legacy").mkdir()
        (self.root / "src/legacy/login.py").write_text("pass\n")
        plan = self.propose(focus_files=["src/login.py"])
        self.assertEqual(plan["source_files"], ["src/login.py"])
        self.assertEqual(plan["source_basis"], "observed_focus")
        self.assertEqual(plan["status"], "review_required")
        self.assertFalse(plan["verified"])

    def test_explicit_task_path_and_memory_inspection_lead_are_only_leads(self):
        path = self.propose("Fix the behavior in src/login.py")
        self.assertEqual(path["source_files"], ["src/login.py"])
        lead = self.propose("Fix authentication problems",
                            inspection_leads=["src/login.py"])
        self.assertEqual(lead["source_basis"], "recorded_inspection_lead")
        self.assertEqual(lead["source_files"], ["src/login.py"])
        self.assertFalse(lead["accepted_by_owner"])

    def test_path_traversal_and_symlink_escape_cannot_be_proposed(self):
        outside = Path(self.tmp.name).parent / "outside-candidate.py"
        try:
            outside.write_text("secret = 1\n")
            invalid = self.propose("Fix login",
                                   focus_files=["../outside-candidate.py", ".env"])
            self.assertEqual(invalid["source_files"], ["src/login.py"])
            link = self.root / "src/link.py"
            try:
                link.symlink_to(outside)
            except (NotImplementedError, OSError):
                pass
            else:
                leak = self.propose("Fix link", focus_files=["src/link.py"])
                self.assertEqual(leak["source_files"], [])
        finally:
            outside.unlink(missing_ok=True)

    def test_static_javascript_has_direct_node_test_candidate(self):
        (self.root / "src/balance.js").write_text("export const balance = 1\n")
        (self.root / "tests/balance.test.js").write_text("import 'node:test'\n")
        plan = self.propose("Fix balance bug")
        self.assertEqual(plan["source_files"], ["src/balance.js"])
        self.assertEqual(plan["test_candidates"][0]["command"],
                         "node --test tests/balance.test.js")
        self.assertEqual(plan["test_candidates"][0]["scope"], "targeted")

    def test_typescript_test_and_safe_package_script_are_broad_and_challenged(self):
        (self.root / "src/wallet.ts").write_text("export const wallet = 1\n")
        (self.root / "tests/wallet.test.ts").write_text("export {}\n")
        (self.root / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest run"}})
        )
        plan = self.propose("Fix wallet bug")
        self.assertEqual(plan["test_candidates"][0]["command"], "npm test")
        self.assertEqual(plan["test_candidates"][0]["scope"], "project_wide")
        self.assertTrue(any(
            "Project-wide check may not exercise" in warning
            for warning in plan["challenge"]["warnings"]
        ))

    def test_masked_npm_script_cannot_be_validation_evidence(self):
        (self.root / "src/wallet.ts").write_text("export const wallet = 1\n")
        (self.root / "tests/wallet.test.ts").write_text("export {}\n")
        (self.root / "package.json").write_text(json.dumps(
            {"scripts": {"test": "vitest run || true"}}
        ))
        plan = self.propose("Fix wallet bug")
        self.assertEqual(plan["test_candidates"], [])
        self.assertFalse(plan["challenge"]["registerable"])

    def test_inspection_does_not_require_unnecessary_test_or_edit(self):
        self.test.unlink()
        plan = self.propose("Inspect login behavior")
        self.assertEqual(plan["profile"], "inspection")
        self.assertEqual(plan["criteria"],
                         [{"id": "source_1", "type": "source_read",
                           "path": "src/login.py"}])
        self.assertTrue(plan["challenge"]["registerable"])

    def test_sensitive_task_and_duplicate_checks_are_not_promoted(self):
        secret = self.propose("fix login password=supersecretvalue")
        self.assertEqual(secret["status"], "unavailable")
        self.assertEqual(secret["criteria"], [])
        (self.root / "src/login.test.py").write_text("def test_login(): pass\n")
        candidate = self.propose()
        commands = [c["command"] for c in candidate["test_candidates"]]
        self.assertEqual(len(commands), len(set(commands)))

    def test_render_bounded_and_contains_complete_pre_registration_json(self):
        plan = self.propose()
        text = format_goal_proposal(plan, max_chars=1000)
        self.assertIn("not registered or approved", text)
        self.assertIn("Proposed JSON:", text)
        self.assertIn("python -m task_goal --project-root", text)
        self.assertIn(json.dumps(plan["criteria"], separators=(",", ":")), text)
        self.assertLessEqual(len(text), 1000)
        tiny = format_goal_proposal(plan, max_chars=125)
        self.assertLessEqual(len(tiny), 125)
        self.assertNotIn("Proposed JSON:", tiny)
        self.assertNotIn("evidence ready", text)

    def test_proposal_adoption_freezes_future_evidence_not_prior_checks(self):
        from task_evidence import record_observation
        from task_checks import record_check
        task = self.task
        before = self.propose()
        ref = goal_snapshot(self.root, "session-9", task)["registration_ref"]
        registered = propose_goal_ref(
            self.root, ref, before["profile"], before["criteria"],
        )
        self.assertEqual(registered["status"], "registered_proposal")
        self.assertEqual(goal_snapshot(self.root, "session-9", task)["met"], 0)
        record_observation(self.root, "session-9", task, "read", "src/login.py")
        command = before["criteria"][1]["command"]
        record_check(self.root, "session-9", task, command, 1, origin="self_run")
        self.source.write_text("def login(): return True\n")
        record_observation(self.root, "session-9", task, "edit", "src/login.py")
        record_observation(self.root, "session-9", task, "read", "src/login.py")
        record_check(self.root, "session-9", task, command, 0, origin="self_run")
        result = goal_snapshot(self.root, "session-9", task)
        self.assertEqual(result["status"], "evidence_ready_for_review")
        self.assertFalse(result["verified"])
        self.assertFalse(result["accepted_by_owner"])

    def test_real_red_green_pilot_from_discovery_to_evidence_review(self):
        """A fixture pilot with actual pytest subprocess exits, not mock claims."""
        from task_evidence import record_observation
        from task_checks import run_check
        self.test.write_text(
            "from src.login import login\n"
            "def test_login():\n"
            "    assert login()\n"
        )
        plan = self.propose()
        ref = goal_snapshot(self.root, "pilot-session", self.task)["registration_ref"]
        propose_goal_ref(self.root, ref, plan["profile"], plan["criteria"])
        record_observation(self.root, "pilot-session", self.task,
                           "read", "src/login.py")
        argv = ["python", "-m", "pytest", "-q", "tests/test_login.py"]
        red = run_check(self.root, "pilot-session", self.task, argv, timeout=60)
        self.assertTrue(red["recorded"])
        self.assertNotEqual(red["exit_code"], 0, "actual initial fixture must fail")
        self.source.write_text("def login(): return True\n")
        record_observation(self.root, "pilot-session", self.task,
                           "edit", "src/login.py")
        record_observation(self.root, "pilot-session", self.task,
                           "read", "src/login.py")
        green = run_check(self.root, "pilot-session", self.task, argv, timeout=60)
        self.assertTrue(green["recorded"])
        self.assertEqual(green["exit_code"], 0, "actual repaired fixture must pass")
        result = goal_snapshot(self.root, "pilot-session", self.task)
        self.assertEqual(result["status"], "evidence_ready_for_review")
        self.assertFalse(result["verified"])
        self.assertFalse(result["accepted_by_owner"])

    def test_bridge_only_previews_until_explicit_registration(self):
        from opencode_context import get_plugin_context
        result = get_plugin_context(
            str(self.root), self.task, max_chars=3600, session_id="s42",
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["task_goal"]["status"], "unregistered")
        self.assertEqual(result["task_goal_proposal"]["status"], "review_required")
        self.assertIn("Suggested goal contract", result["context_markdown"])
        self.assertIn("Proposed JSON:", result["context_markdown"])
        self.assertNotIn("evidence ready", result["context_markdown"])
        self.assertFalse((self.root / ".cognigraph" / "task_evidence.sqlite3").exists())
        ref = result["task_goal"]["registration_ref"]
        plan = result["task_goal_proposal"]
        propose_goal_ref(self.root, ref, plan["profile"], plan["criteria"])
        after = get_plugin_context(
            str(self.root), self.task, max_chars=3600, session_id="s42",
        )
        self.assertEqual(after["task_goal"]["status"], "needs_evidence")
        self.assertIsNone(after["task_goal_proposal"])
        self.assertNotIn("Suggested goal contract", after["context_markdown"])


if __name__ == "__main__":
    unittest.main()
