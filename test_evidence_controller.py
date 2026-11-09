"""Behavior contracts for CogniGraph's evidence-first context controller."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pm_evidence import (
    required_evidence, evidence_bonus, evaluate_evidence, format_evidence_plan,
)


class EvidencePlannerUnitTests(unittest.TestCase):
    def test_task_intent_changes_required_evidence_before_retrieval(self):
        self.assertIn("failure", required_evidence("fix a broken login session", "orient"))
        self.assertIn("dependencies", required_evidence("implement billing", "orient"))
        self.assertNotIn("failure", required_evidence("read the login source", "inspect"))
        self.assertEqual(required_evidence("fix login", "verify"),
                         ("behavior", "verification"))

    def test_marginal_gain_rewards_first_distinct_need_not_redundancy(self):
        needs = required_evidence("fix auth", "debug")
        component = {"source": "memory", "category": "component", "file_path": "auth.py"}
        constraint = {"source": "memory", "category": "constraint"}
        self.assertGreater(evidence_bonus(component, needs, set()), 0)
        self.assertEqual(evidence_bonus(component, needs, {"behavior"}), 0)
        self.assertGreater(evidence_bonus(constraint, needs, {"behavior"}), 0)
        self.assertEqual(evidence_bonus({**component, "file_path": ""}, needs, set()), 0)
        self.assertEqual(evidence_bonus({**component, "source": "episodic"}, needs, set()), 0)

    def test_memory_claim_does_not_prove_test_execution_or_source_truth(self):
        facts = [
            {"ref": "memory:1", "category": "component", "file_path": "src/auth.py",
             "freshness": "CURRENT"},
            {"ref": "memory:2", "category": "constraint", "file_path": "",
             "freshness": "UNVERIFIED"},
            {"ref": "memory:3", "category": "state", "file_path": "",
             "freshness": "UNVERIFIED", "evidence_type": "test_result"},
        ]
        plan = evaluate_evidence("fix login", "debug", facts, [])
        states = {r["id"]: r["status"] for r in plan["requirements"]}
        self.assertEqual(states["behavior"], "reference_available")
        self.assertEqual(states["constraints"], "reference_available")
        self.assertEqual(states["failure"], "missing")
        self.assertEqual(states["verification"], "missing")
        self.assertFalse(plan["verified"])
        self.assertIn("src/auth.py", plan["next_action"])
        self.assertIn("Next:", "".join(format_evidence_plan(plan)))

    def test_missing_memory_still_produces_next_investigation_step(self):
        plan = evaluate_evidence("implement feature", "orient", [], [])
        self.assertIn("Locate the current implementation", plan["next_action"])
        self.assertGreater(plan["unresolved_count"], 0)
        self.assertNotEqual(plan["requirements"], [])

    def test_indexed_leads_are_not_marked_as_verified(self):
        plan = evaluate_evidence(
            "update auth", "edit", [],
            [{"file_path": "src/auth.py", "kind": "structural_lead"},
             {"file_path": "src/routes.py", "kind": "linked_fact"}],
        )
        states = {r["id"]: r["status"] for r in plan["requirements"]}
        self.assertEqual(states["behavior"], "inspection_lead")
        self.assertEqual(states["dependencies"], "inspection_lead")
        self.assertFalse(plan["verified"])


    def test_structural_graph_locates_a_caller_without_text_similarity(self):
        from pm_evidence import structural_dependency_leads
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE structural_nodes (id INTEGER PRIMARY KEY, file_path TEXT);
            CREATE TABLE structural_edges (caller_id INTEGER, callee_id INTEGER,
                                           edge_type TEXT, id INTEGER PRIMARY KEY);
            INSERT INTO structural_nodes VALUES (1,'src/auth.py'),(2,'src/route.py'),
                                                (3,'../escape.py');
            INSERT INTO structural_edges VALUES (2,1,'calls',1);
            INSERT INTO structural_edges VALUES (3,1,'calls',2);
        """)
        safe = {"src/auth.py", "src/route.py"}
        leads = structural_dependency_leads(
            conn, ["src/auth.py"], lambda p: p if p in safe else None,
        )
        self.assertEqual([x["file_path"] for x in leads], ["src/route.py"])
        self.assertEqual(leads[0]["kind"], "structural_dependency")
        conn.close()

    def test_read_observation_advances_next_action_without_claiming_truth(self):
        facts = [{"ref": "memory:1", "category": "component",
                  "file_path": "src/auth.py", "freshness": "UNVERIFIED"}]
        targets = [{"file_path": "src/auth.py", "kind": "source_anchor"}]
        plan = evaluate_evidence("fix login", "inspect", facts, targets,
                                 observed_files=("src/auth.py",))
        self.assertEqual(plan["requirements"][0]["status"], "source_read_observed")
        self.assertNotIn("Inspect current source at src/auth.py", plan["next_action"])
        self.assertFalse(plan["verified"])


class IntegratedEvidenceSelectionTests(unittest.TestCase):
    def setUp(self):
        from memory_store import MemoryStore
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / ".cognigraph").mkdir()
        (root / "src").mkdir()
        (root / "src" / "auth.py").write_text("def login(): return True\n")
        self.store = MemoryStore(str(root / ".cognigraph" / "memory.db"))
        self.store._query_encode = lambda _: None

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_delivery_covers_constraint_not_only_similar_components(self):
        ids = [
            self.store.write("component", f"Authentication handler {i} reads the login session",
                             file_path="src/auth.py", force=True, embed=False)
            for i in range(9)
        ]
        constraint = self.store.write(
            "constraint", "Existing session cookies must survive login repair",
            file_path="src/auth.py", force=True, embed=False,
        )
        # Isolate evidence selection from the search provider so a long run
        # of near-identical components cannot consume the entire briefing.
        candidates = [self.store.get_by_id(i) for i in ids + [constraint]]
        for fact in candidates:
            fact["source"] = "memory"
        self.store.query_hybrid = lambda *a, **k: candidates
        report = self.store.get_intelligent_context(
            "fix broken login", phase="edit", max_chars=2400,
            limit=2, log_usage=False,
        )
        categories = {f["category"] for f in report["selected"]}
        self.assertEqual(categories, {"component", "constraint"})
        self.assertIsNone(report["retrieval_id"])
        self.assertEqual(report["evidence_plan"]["requirements"][0]["id"], "behavior")
        self.assertIn("Evidence requirements", report["context_markdown"])
        self.assertLessEqual(report["budget_used_chars"], 2400)

    def test_observed_focus_without_memory_does_not_repeat_inspect_instruction(self):
        self.store.query_hybrid = lambda *a, **k: []
        report = self.store.get_intelligent_context(
            "read authentication", phase="inspect",
            focus_files=["src/auth.py"], observed_files=["src/auth.py"],
            max_chars=1600, log_usage=False,
        )
        self.assertEqual(report["evidence_plan"]["requirements"][0]["status"],
                         "source_read_observed")
        self.assertNotIn("inspect current source", report["context_markdown"].lower())
        self.assertFalse(report["evidence_plan"]["verified"])

    def test_zero_hit_is_guidance_without_fabricated_evidence(self):
        self.store.query_hybrid = lambda *a, **k: []
        report = self.store.get_intelligent_context(
            "fix the failing login path", phase="debug", max_chars=1500,
            log_usage=False,
        )
        self.assertEqual(report["status"], "no_new_context")
        self.assertFalse(report["selected"])
        self.assertIsNone(report["retrieval_id"])
        self.assertFalse(report["evidence_plan"]["verified"])
        self.assertIn("Next:", report["context_markdown"])


if __name__ == "__main__":
    unittest.main()
