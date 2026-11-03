"""Tests for task-observation and fact-specific review. No self-training."""
import tempfile
import unittest
from pathlib import Path

from memory_store import MemoryStore
from pm_outcomes import _relative_source_path


class TaskOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / ".cognigraph" / "memory.db"
        db.parent.mkdir()
        self.store = MemoryStore(str(db))
        self.fact1 = self.store.write(
            "component", "OAuth session refresher uses signed cookie middleware",
            file_path="src/auth.py", force=True, embed=False,
        )
        self.fact2 = self.store.write(
            "decision", "OAuth refresh should not retry invalid grants",
            file_path="src/other.py", force=True, embed=False,
        )
        self.fact3 = self.store.write(
            "constraint", "Never send a refresh token to the browser",
            file_path="src/auth.py", force=True, embed=False,
        )
        self.store.conn.execute(
            """INSERT INTO memory_usage_log
               (id, query_text, retrieved_fact_ids, result_count, logged_at)
               VALUES (?, ?, ?, ?, ?)""",
            (811, "fix oauth refresh cookie session", f"{self.fact1},{self.fact2}", 2,
             "2026-11-03T12:00:00+00:00"),
        )
        self.store._current_git_commit = lambda: "a" * 40

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_review_candidates_are_only_exposed_and_edited_facts(self):
        result = self.store.record_task_outcome(
            811, ["src/auth.py", "src/auth.py", "src/other.py"])
        self.assertEqual(result["review_candidate_fact_ids"], sorted([self.fact1, self.fact2]))
        self.assertFalse(result["ranking_updated"])
        self.assertEqual(self.store._feedback_boost("fix oauth refresh cookie session"), {})
        queue = self.store.task_outcome_review_queue()["outcomes"][0]
        self.assertEqual(queue["test_status"], "no_test_report")
        self.assertTrue(all(c["review_status"] == "pending" for c in queue["candidates"]))
        self.assertNotIn(self.fact3, [c["fact_id"] for c in queue["candidates"]])

    def test_duplicate_report_updates_not_multiplies(self):
        first = self.store.record_task_outcome(811, ["src/auth.py"])
        again = self.store.record_task_outcome(811, ["src/other.py"])
        self.assertEqual(first["outcome_id"], again["outcome_id"])
        self.assertEqual(self.store.task_outcome_review_queue()["count"], 1)
        self.assertEqual(again["review_candidate_fact_ids"], [self.fact2])

    def test_test_metadata_not_conflated_with_actual_verification(self):
        result = self.store.record_task_outcome(811, ["src/auth.py"],
            test_evidence={"command": "python -m pytest -q", "exit_code": 0,
                           "commit_sha": "a" * 40})
        self.assertEqual(result["test_status"], "reported_pass_at_head")
        self.assertFalse(result["ranking_updated"])
        stored = self.store.conn.execute(
            "SELECT test_command_digest, test_commit_sha FROM task_outcomes WHERE id=?",
            (result["outcome_id"],)).fetchone()
        self.assertEqual(len(stored["test_command_digest"]), 16)
        self.assertEqual(stored["test_commit_sha"], "a" * 40)
        mismatch = self.store.record_task_outcome(811, ["src/auth.py"],
            test_evidence={"command": "pytest", "exit_code": 1,
                           "commit_sha": "b" * 40})
        self.assertEqual(mismatch["test_status"], "reported_unmatched_revision")

    def test_review_status_updates_only_after_explicit_feedback(self):
        self.store.record_task_outcome(811, ["src/auth.py"])
        self.store.record_relevance_feedback(
            811, [{"fact_id": self.fact1, "rating": "helpful"}], "user")
        q = self.store.task_outcome_review_queue()
        self.assertEqual(q["outcomes"][0]["candidates"][0]["review_status"], "rated")
        self.assertEqual(q["outcomes"][0]["candidates"][0]["rating"], "helpful")
        self.assertGreater(self.store._feedback_boost("fix oauth refresh cookie session")[self.fact1], 1)

    def test_paths_never_leave_project_or_enter_hidden_dirs(self):
        for p in ["/etc/passwd", "../creds.py", "C:\\Users\\bob\\secret.py",
                  "src/.env", ".cognigraph/memory.db", "src//file.py",
                  "vendor/import.py", "src/../file.py"]:
            with self.subTest(path=p), self.assertRaises(ValueError):
                _relative_source_path(p)
        self.assertEqual(_relative_source_path("src\\auth.py"), "src/auth.py")
        self.assertEqual(_relative_source_path("src/auth.py"), "src/auth.py")

    def test_invalid_batch_or_evidence_does_not_create_outcome(self):
        with self.assertRaises(ValueError):
            self.store.record_task_outcome(811, ["src/auth.py", "../bad.py"])
        with self.assertRaises(ValueError):
            self.store.record_task_outcome(811, ["src/auth.py"],
                test_evidence={"command": "token=VERY_SECRET_VALUE", "exit_code": 0})
        self.assertEqual(self.store.task_outcome_review_queue()["count"], 0)

    def test_unknown_receipt_and_deprecated_fact(self):
        with self.assertRaises(ValueError):
            self.store.record_task_outcome(812, ["src/auth.py"])
        self.store.deprecate(self.fact1, reason="superseded")
        result = self.store.record_task_outcome(811, ["src/auth.py"])
        self.assertEqual(result["review_candidate_fact_ids"], [])


if __name__ == "__main__":
    unittest.main()
