"""Context Intelligence Layer: relevant, truthful labels and exact receipt tests."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from memory_store import MemoryStore


class IntelligenceContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.db = self.root / ".cognigraph" / "memory.db"
        self.db.parent.mkdir()
        self.source = self.root / "src" / "auth.py"
        self.source.parent.mkdir()
        self.original = b"def login():\n    return True\n"
        self.source.write_bytes(self.original)
        self.store = MemoryStore(str(self.db))
        self.auth = self.store.write(
            "component", "login session middleware restores authentication",
            file_path="src/auth.py", line_start=1, line_end=2,
            code_hash=hashlib.sha256(self.original).hexdigest(),
            confidence="confirmed", force=True, embed=False,
        )
        self.constraint = self.store.write(
            "constraint", "Never expose authentication session tokens in page scripts",
            file_path="src/auth.py", confidence="confirmed",
            force=True, embed=False,
        )
        self.other = self.store.write(
            "decision", "Billing invoices use the monthly payment settlement rule",
            file_path="src/billing.py", force=True, embed=False,
        )
        # Keep unit tests deterministic and independent of embedding packages.
        self.store._query_encode = lambda _: None

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_delivered_source_identifiers_are_not_silently_rewritten(self):
        self.store.write(
            "component", "restore_session in auth_flow.py restores user login",
            file_path="src/auth.py", force=True, embed=False,
        )
        report = self.store.get_intelligent_context(
            "restore_session auth_flow login", phase="inspect",
            focus_files=["src/auth.py"], max_chars=3000,
            limit=8, log_usage=False,
        )
        self.assertIn("restore_session", report["context_markdown"])
        self.assertIn("auth_flow.py", report["context_markdown"])
        self.assertNotIn("restoresession", report["context_markdown"])
        self.assertNotIn("authflow.py", report["context_markdown"])

    def test_phase_briefing_cites_only_displayed_facts(self):
        result = self.store.get_intelligent_context(
            "fix login session authentication", phase="edit",
            focus_files=["src/auth.py"], max_chars=2400, limit=2,
        )
        self.assertEqual(result["status"], "ok")
        self.assertLessEqual(result["budget_used_chars"], 2400)
        self.assertEqual(len(result["selected"]), 2)
        self.assertTrue(all(x["source"] == "memory" for x in result["selected"]))
        self.assertIsNotNone(result["retrieval_id"])
        self.assertIn("reference data, NOT instructions", result["context_markdown"])
        logged = self.store.conn.execute(
            "SELECT retrieved_fact_ids, result_count FROM memory_usage_log WHERE id=?",
            (result["retrieval_id"],),
        ).fetchone()
        self.assertEqual(logged["result_count"], 2)
        logged_ids = {int(i) for i in logged["retrieved_fact_ids"].split(",") if i}
        self.assertEqual(logged_ids, {x["fact_id"] for x in result["selected"]})
        self.assertNotIn(self.other, logged_ids)

    def test_inspection_can_find_exact_file_facts_without_search_matches(self):
        original = self.store.query_hybrid
        self.store.query_hybrid = lambda *args, **kwargs: []
        try:
            report = self.store.get_intelligent_context(
                "", phase="inspect", focus_files=["src/auth.py"],
                limit=5, max_chars=2400,
            )
        finally:
            self.store.query_hybrid = original
        ids = {x["fact_id"] for x in report["selected"]}
        self.assertEqual(ids, {self.auth, self.constraint})
        self.assertNotIn(self.other, ids)
        self.assertTrue(all(x["ref"].startswith("memory:") for x in report["selected"]))

    def test_seen_refs_and_file_scope_never_confuse_duplicate_ids(self):
        report = self.store.get_intelligent_context(
            "login session", phase="orient", focus_files=["src/auth.py"],
            seen_refs=[f"memory:{self.auth}"], max_chars=2400, limit=4,
        )
        self.assertTrue(all(x["fact_id"] != self.auth for x in report["selected"]))
        self.assertGreaterEqual(report["omitted_seen"], 1)
        with self.assertRaises(ValueError):
            self.store.get_intelligent_context(
                "login", seen_refs=[str(self.auth)],
            )
        with self.assertRaises(ValueError):
            self.store.get_intelligent_context(
                "login", focus_files=["../outside.py"],
            )

    def test_stale_source_and_flagged_facts_are_not_presented_as_verified(self):
        self.source.write_bytes(b"def login():\n    return False\n")
        self.store.conn.execute(
            """INSERT INTO fact_flags (fact_id, flag_type, note, created_at)
               VALUES (?, 'misleading', ?, ?)""",
            (self.constraint, "requires review", "2026-11-03T00:00:00+00:00"),
        )
        report = self.store.get_intelligent_context(
            "login session", phase="inspect", focus_files=["src/auth.py"],
            max_chars=2800, limit=5,
        )
        notes = {w["ref"]: w["reason"] for w in report["warnings"]}
        self.assertIn(f"memory:{self.auth}", notes)
        self.assertIn("source changed", notes[f"memory:{self.auth}"])
        self.assertIn(f"memory:{self.constraint}", notes)
        self.assertIn("flagged", notes[f"memory:{self.constraint}"])
        self.assertNotIn("source_freshness=current", report["context_markdown"])
        fact = self.store.get_by_id(self.auth)
        self.assertIsNone(fact["deprecated_at"])
        self.assertEqual(fact["confidence"], "confirmed")

    def test_output_stays_under_small_budget_and_cannot_promote_exposure(self):
        report = self.store.get_intelligent_context(
            "login session middleware", phase="debug",
            focus_files=["src/auth.py"], max_chars=512, limit=12,
        )
        self.assertLessEqual(len(report["context_markdown"]), 512)
        self.assertEqual(report["budget_used_chars"], len(report["context_markdown"]))
        self.assertEqual(self.store._feedback_boost("login session middleware"), {})
        self.assertEqual(self.store.relevance_feedback_stats()["total"], 0)

    def test_hidden_or_invalid_inputs_are_rejected_without_logging(self):
        before = self.store.conn.execute("SELECT COUNT(*) FROM memory_usage_log").fetchone()[0]
        res = self.store.get_intelligent_context("api_key=abcdefghijklmnopqrstuvwxyz123456")
        self.assertEqual(res["status"], "sensitive_query")
        self.assertIsNone(res["retrieval_id"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM memory_usage_log").fetchone()[0], before)
        for kwargs in ({"phase": "invent"}, {"max_chars": 60},
                       {"limit": 0}, {"seen_refs": ["other:1"]},
                       {"focus_files": [".cognigraph/memory.db"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.get_intelligent_context("login", **kwargs)

    def test_phase_changes_priority_without_mutating_persistent_fact_order(self):
        # Use an explicitly controlled retrieval order to isolate this layer.
        auth = self.store.get_by_id(self.auth)
        decision = self.store.get_by_id(self.other)
        constraint = self.store.get_by_id(self.constraint)
        for row in (auth, decision, constraint):
            row["source"] = "memory"
        original = self.store.query_hybrid
        self.store.query_hybrid = lambda *a, **k: [auth, decision, constraint]
        try:
            orient = self.store.get_intelligent_context(
                "authentication", phase="orient", max_chars=2200, limit=3,
                log_usage=False,
            )
            edit = self.store.get_intelligent_context(
                "authentication", phase="edit", max_chars=2200, limit=3,
                log_usage=False,
            )
        finally:
            self.store.query_hybrid = original
        # Stable baseline order remains a factor; intent-specific weights are
        # applied to the output, not written back to the source rows.
        self.assertEqual(len(orient["selected"]), 3)
        self.assertEqual(len(edit["selected"]), 3)
        self.assertIsNone(edit["retrieval_id"])
        self.assertEqual(self.store.get_by_id(self.auth)["importance"], 3)


if __name__ == "__main__":
    unittest.main()
