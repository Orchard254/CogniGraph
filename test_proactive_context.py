"""Regression tests for evidence-grounded, automatic context investigation leads."""
import tempfile
import unittest
from pathlib import Path

from memory_store import MemoryStore


class ProactiveContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / ".cognigraph").mkdir()
        self.db = self.root / ".cognigraph" / "memory.db"
        (self.root / "src").mkdir()
        (self.root / "src" / "auth.py").write_text("def login(): return True\n")
        (self.root / "src" / "session.py").write_text("from .auth import login\n")
        (self.root / "src" / "unknown.py").write_text("pass\n")
        self.store = MemoryStore(str(self.db))
        self.store._query_encode = lambda _: None
        self.auth = self.store.write(
            "component", "Login authentication and session restoration entrypoint",
            file_path="src/auth.py", force=True, embed=False,
        )
        self.session = self.store.write(
            "component", "Session persistence depends on authentication",
            file_path="src/session.py", force=True, embed=False,
        )
        self.store.link_facts(self.auth, self.session, "depends_on")
        self.original_query = self.store.query_hybrid

    def tearDown(self):
        self.store.query_hybrid = self.original_query
        self.store.close()
        self.temp.cleanup()

    def only(self, fact_id):
        fact = self.store.get_by_id(fact_id)
        fact["source"] = "memory"
        self.store.query_hybrid = lambda *args, **kwargs: [fact]

    def test_selected_fact_proactively_recommends_existing_linked_source(self):
        self.only(self.auth)
        result = self.store.get_intelligent_context(
            "fix login session", max_chars=3000, limit=1, log_usage=False,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual([x["fact_id"] for x in result["selected"]], [self.auth])
        self.assertEqual(
            [(x["file_path"], x["kind"]) for x in result["inspection_targets"]],
            [("src/auth.py", "source_anchor"), ("src/session.py", "linked_fact")],
        )
        self.assertIn("stored `depends_on` relationship", result["context_markdown"])
        self.assertIn("Suggested source inspection", result["context_markdown"])
        self.assertNotIn(f"[memory:{self.session}]", result["context_markdown"])
        self.assertIsNone(result["retrieval_id"])
        self.assertEqual(
            self.store.conn.execute("SELECT COUNT(*) FROM memory_usage_log").fetchone()[0], 0,
        )

    def test_focus_without_delivered_fact_reports_gap_not_missing_project_truth(self):
        self.only(self.auth)
        result = self.store.get_intelligent_context(
            "repair login", phase="inspect", focus_files=["src/unknown.py"],
            max_chars=3000, limit=1, log_usage=False,
        )
        self.assertEqual(result["knowledge_gaps"][0]["kind"], "missing_focused_evidence")
        self.assertEqual(result["knowledge_gaps"][0]["file_path"], "src/unknown.py")
        self.assertIn("No displayed fact", result["context_markdown"])
        self.assertNotIn("no facts exist", result["context_markdown"].lower())

    def test_edit_phase_exposes_missing_constraints_as_requirement(self):
        self.only(self.auth)
        result = self.store.get_intelligent_context(
            "change login", phase="edit", max_chars=3000, limit=1,
            log_usage=False,
        )
        requirements = {
            item["id"]: item for item in result["evidence_plan"]["requirements"]
        }
        self.assertEqual(requirements["constraints"]["status"], "missing")
        self.assertIn("Protect existing decisions and constraints: missing",
                      result["context_markdown"])
        # The new evidence contract replaces, rather than duplicates, the
        # previous generic no_delivered_constraint warning.
        self.assertNotIn("no_delivered_constraint",
                         {x["kind"] for x in result["knowledge_gaps"]})

    def test_missing_and_external_symlink_anchors_are_not_recommended(self):
        self.only(self.auth)
        outside = Path(self.temp.name) / "outside.py"
        outside.write_text("secret = 1\n")
        link = self.root / "src" / "external.py"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unavailable")
        self.store.conn.execute(
            "UPDATE memory SET file_path=? WHERE id=?",
            ("src/external.py", self.auth),
        )
        # Recreate the retrieved row after the mutation so the test really
        # exercises the hostile symlink rather than the prior safe anchor.
        self.only(self.auth)
        result = self.store.get_intelligent_context(
            "fix login", max_chars=3000, limit=1, log_usage=False,
        )
        self.assertNotIn("src/external.py", [x["file_path"] for x in result["inspection_targets"]])
        self.assertIn("src/session.py", [x["file_path"] for x in result["inspection_targets"]])

    def test_inspection_path_keeps_underscores_exact(self):
        (self.root / "src" / "auth_flow.py").write_text("pass\n")
        fact_id = self.store.write(
            "component", "Authentication flow helper",
            file_path="src/auth_flow.py", force=True, embed=False,
        )
        self.only(fact_id)
        result = self.store.get_intelligent_context(
            "authentication flow", max_chars=2600, limit=1, log_usage=False,
        )
        self.assertIn(
            "src/auth_flow.py", [x["file_path"] for x in result["inspection_targets"]],
        )
        self.assertIn("`src/auth_flow.py`", result["context_markdown"])
        self.assertNotIn("`src/authflow.py`", result["context_markdown"])

    def test_small_budget_never_appends_unpaid_for_suggestions(self):
        self.only(self.auth)
        report = self.store.get_intelligent_context(
            "fix login", phase="edit", max_chars=512, limit=1,
            log_usage=False,
        )
        self.assertLessEqual(len(report["context_markdown"]), 512)
        for item in report["inspection_targets"]:
            self.assertIn(item["file_path"], report["context_markdown"])
        for item in report["knowledge_gaps"]:
            self.assertIn(item["action"], report["context_markdown"])


if __name__ == "__main__":
    unittest.main()
