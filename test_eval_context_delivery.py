"""Contract tests for the existing offline context-delivery benchmark.

Only primary facts count. A single-gold dataset cannot establish the
irrelevance of any other delivered fact.
"""
import unittest

from scripts.benchmark_context_delivery import assess_cases, legacy_refs


class ContextDeliveryEvalTests(unittest.TestCase):
    def test_legacy_parser_counts_only_unique_top_level_entries(self):
        text = (
            "### CogniGraph Context for: 'auth'\n"
            "1. **[📦 COMPONENT #12]** Auth module\n"
            "   - 🔗 **[depends_on]** → [COMPONENT #99] Not primary\n"
            "2. **[EPISODIC/DECISION #12]** Earlier decision\n"
            "3. **[⚠️ CONSTRAINT #43]** Token constraint\n"
            "4. **[COMPONENT #12]** Repeated label\n"
        )
        self.assertEqual(
            legacy_refs(text),
            ["memory:12", "episodic:12", "memory:43"],
        )

    def test_truncated_unfinished_entry_is_not_counted(self):
        text = "1. **[📦 COMPONENT #12]** Good\n2. **[📦 COMPONENT #4"
        self.assertEqual(legacy_refs(text), ["memory:12"])

    def test_metrics_include_delivery_not_unlabeled_false_positives(self):
        rows = [
            {
                "expected_fact_id": 3, "query": "query 1",
                "refs": ["memory:3", "memory:4"],
                "elapsed_ms": 1.4, "chars": 250, "budget_chars": 300,
            },
            {
                "expected_fact_id": 7, "query": "query 2",
                "refs": ["memory:9"],
                "elapsed_ms": 4.1, "chars": 330, "budget_chars": 300,
            },
        ]
        result = assess_cases(rows, limit=5)
        self.assertEqual(result["target_recall_at_k"], 0.5)
        self.assertEqual(result["target_mrr"], 0.5)
        self.assertEqual(result["mean_primary_facts"], 1.5)
        self.assertEqual(result["budget_violations"], 1)
        self.assertEqual(result["p95_latency_ms"], 4.1)
        self.assertNotIn("false_positive_rate", result)

    def test_empty_rows_rejected_without_inventing_score(self):
        result = assess_cases([], limit=5)
        self.assertIsNone(result["target_recall_at_k"])
        self.assertIsNone(result["target_mrr"])
        self.assertIsNone(result["p95_latency_ms"])


if __name__ == "__main__":
    unittest.main()
