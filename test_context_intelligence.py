"""Context Intelligence Layer: strict delivery, provenance and receipt tests."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from memory_store import MemoryStore


class IntelligenceLayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        db = self.root / ".cognigraph" / "memory.db"
        db.parent.mkdir()
        self.file = self.root / "src" / "auth.py"
        self.file.parent.mkdir()
        self.file.write_bytes(b"def refresh_session():\n    return True\n")
        self.store = MemoryStore(str(db))
        self.store._query_encode = lambda text: None

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _fact(self, category, text, *, file_path="src/auth.py", importance=3,
              code_hash=None, confidence="confirmed"):
        return self.store.write(category, text, file_path=file_path,
                                importance=importance, code_hash=code_hash,
                                confidence=confidence, force=True, embed=False)

    def test_only_rendered_facts_are_in_receipt(self):
        ids = [
            self._fact("component", f"OAuth session refresh login process detail {i} " + "x" * 140)
            for i in range(8)
        ]
        result = self.store.get_intelligent_context(
            "fix OAuth session refresh login", max_chars=440, max_facts=8)
        self.assertLessEqual(len(result["context_markdown"]), 440)
        self.assertGreaterEqual(len(result["fact_ids"]), 1)
        self.assertLess(len(result["fact_ids"]), 8)
        rows = self.store.conn.execute(
            "SELECT retrieved_fact_ids FROM memory_usage_log WHERE id=?",
            (result["retrieval_id"],)).fetchone()
        self.assertEqual(
            {int(i) for i in rows["retrieved_fact_ids"].split(",")},
            set(result["fact_ids"]),
        )
        for fid in result["fact_ids"]:
            self.assertIn(f"MEM #{fid}", result["context_markdown"])
        for fid in set(ids) - set(result["fact_ids"]):
            self.assertNotIn(f"MEM #{fid} |", result["context_markdown"])

    def test_seen_facts_are_excluded_and_no_receipt_when_all_seen(self):
        fact = self._fact("component", "Authentication login cookie refresh handler")
        initial = self.store.get_intelligent_context(
            "fix authentication login cookie refresh", max_facts=1)
        self.assertIn(fact, initial["fact_ids"])
        next_result = self.store.get_intelligent_context(
            "fix authentication login cookie refresh", seen_fact_ids=[fact],
            max_facts=1)
        self.assertEqual(next_result["fact_ids"], [])
        self.assertIsNone(next_result["retrieval_id"])

    def test_source_freshness_warns_but_does_not_mutate_memory(self):
        original = self.file.read_bytes()
        fid = self._fact(
            "constraint", "OAuth session cookie refresh must remain backward compatible",
            code_hash=hashlib.sha256(original).hexdigest())
        current = self.store.get_intelligent_context("fix OAuth session cookie refresh")
        self.assertIn("source_bytes=current", current["context_markdown"])
        self.file.write_bytes(b"def refresh_session():\n    return False\n")
        updated = self.store.get_intelligent_context("fix OAuth session cookie refresh")
        self.assertIn("source=possibly_stale", updated["context_markdown"])
        self.assertIn("Potentially outdated or flagged", updated["context_markdown"])
        fact = self.store.get_by_id(fid)
        self.assertEqual(fact["confidence"], "confirmed")
        self.assertIsNone(fact["deprecated_at"])

    def test_stage_and_file_focused_context(self):
        decision = self._fact("decision", "OAuth login uses HTTP-only cookies")
        component = self._fact("component", "OAuth login session adapter")
        result = self.store.get_intelligent_context(
            "inspect OAuth login session", stage="inspect",
            file_paths=["src/auth.py"], max_facts=2)
        self.assertEqual(result["stage"], "inspect")
        self.assertTrue({decision, component}.issubset(set(result["fact_ids"])))
        self.assertIn("Inspecting code", result["context_markdown"])
        self.assertIn("src/auth.py", result["context_markdown"])

    def test_untrusted_and_sensitive_queries_rejected(self):
        before = self.store.conn.execute("SELECT COUNT(*) FROM memory_usage_log").fetchone()[0]
        result = self.store.get_intelligent_context(
            "fix API_KEY=abcdefghijklmnopqrstuv authentication")
        self.assertEqual(result["status"], "sensitive_query")
        self.assertIsNone(result["retrieval_id"])
        after = self.store.conn.execute("SELECT COUNT(*) FROM memory_usage_log").fetchone()[0]
        self.assertEqual(before, after)

    def test_invalid_stage_budget_paths_and_seen_are_rejected(self):
        for opts in (
            {"stage": "execute-shell"},
            {"max_chars": 200},
            {"max_chars": 7000},
            {"max_facts": 0},
            {"file_paths": ["../outside.py"]},
            {"file_paths": [".env"]},
            {"seen_fact_ids": [-1]},
        ):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                self.store.get_intelligent_context("fix oauth refresh", **opts)

    def test_receipt_cannot_rate_undelivered_fact(self):
        good = self._fact("decision", "Fix OAuth refresh without changing the cookie format")
        hidden = self._fact("other", "Completely unrelated deployment infrastructure record")
        result = self.store.get_intelligent_context(
            "fix OAuth refresh cookie", max_facts=1)
        self.assertIn(good, result["fact_ids"])
        self.assertNotIn(hidden, result["fact_ids"])
        with self.assertRaises(ValueError):
            self.store.record_relevance_feedback(
                result["retrieval_id"],
                [{"fact_id": hidden, "rating": "helpful"}],
                evidence_type="user")

    def test_episodic_and_live_fact_ids_do_not_collide(self):
        fid = self._fact("component", "OAuth cookie session restoration")
        original = self.store.query_hybrid
        try:
            self.store.query_hybrid = lambda *a, **kw: [
                {"source": "episodic", "id": fid, "category": "decision",
                 "content": "Earlier branch explored session restoration",
                 "importance": 3, "confidence": "assumed"},
                {"source": "memory", "id": fid, "category": "component",
                 "content": "OAuth cookie session restoration",
                 "importance": 3, "confidence": "confirmed"},
            ]
            result = self.store.get_intelligent_context(
                "OAuth cookie session restoration", max_facts=2)
            self.assertIn(f"EPISODIC #{fid}", result["context_markdown"])
            self.assertIn(f"MEM #{fid}", result["context_markdown"])
            self.assertEqual(result["fact_ids"], [fid])
            self.assertEqual(len(result["source_refs"]), 2)
        finally:
            self.store.query_hybrid = original

    def test_no_unverified_autolearning(self):
        fid = self._fact("component", "OAuth session login refresh adapter")
        out = self.store.get_intelligent_context("fix OAuth session login refresh")
        self.assertIn(fid, out["fact_ids"])
        self.assertEqual(self.store.relevance_feedback_stats()["total"], 0)
        self.assertEqual(self.store._feedback_boost("fix OAuth session login refresh"), {})


if __name__ == "__main__":
    unittest.main()
