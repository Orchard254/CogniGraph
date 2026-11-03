"""Regression tests for the context-delivery diagnostic's scoring contract."""
import unittest

from scripts.benchmark_context_delivery import assess_cases, legacy_refs


class ContextDeliveryBenchmarkTests(unittest.TestCase):
    def test_legacy_primary_refs_not_graph_mentions(self):
        markdown = (
            "### CogniGraph Context\n"
            "1. **[📦 COMPONENT #25]** current authentication entrypoint\n"
            "   - 🔗 **[depends_on]** → [DECISION #91] a related edge\n"
            "2. **[EPISODIC/STATE #25]** historical note\n"
            "3. **[⚠️ CONSTRAINT #17]** must not leak tokens\n"
        )
        self.assertEqual(
            legacy_refs(markdown),
            ["memory:25", "episodic:25", "memory:17"],
        )

    def test_delivery_metrics_count_primary_target_not_unrelated_fact(self):
        rows = [
            {"expected_fact_id": 42, "refs": ["memory:5", "memory:42"],
             "chars": 200, "budget_chars": 250, "elapsed_ms": 3.0},
            {"expected_fact_id": 99, "refs": ["episodic:99", "memory:7"],
             "chars": 270, "budget_chars": 250, "elapsed_ms": 5.0},
        ]
        result = assess_cases(rows, limit=5)
        self.assertEqual(result["n"], 2)
        self.assertEqual(result["target_recall_at_k"], 0.5)
        self.assertEqual(result["target_mrr"], 0.25)
        self.assertEqual(result["mean_delivered_chars"], 235.0)
        self.assertEqual(result["median_latency_ms"], 4.0)
        self.assertEqual(result["budget_violations"], 1)

    def test_empty_set_is_reportable_without_division_error(self):
        result = assess_cases([], limit=5)
        self.assertEqual(result["n"], 0)
        self.assertIsNone(result["target_recall_at_k"])
        self.assertIsNone(result["mean_delivered_chars"])


if __name__ == "__main__":
    unittest.main()
