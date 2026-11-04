"""Unit tests for the delivered-context benchmark's scoring contract.

A golden fact merely mentioned as a graph neighbor must NOT be counted as
a selected fact; unjudged results are not false positives.
"""
import unittest

from scripts.eval_context_delivery import (
    displayed_rank, legacy_displayed_refs, summarize,
)


class ContextDeliveryEvalTests(unittest.TestCase):
    def test_legacy_parser_counts_only_rendered_primary_entries(self):
        text = (
            "### CogniGraph Context for: 'auth'\n"
            "1. **[📦 COMPONENT #12]** Auth module\n"
            "   - 🔗 **[depends_on]** → [COMPONENT #99] Not primary\n"
            "2. **[EPISODIC/DECISION #12]** Earlier decision\n"
            "3. **[⚠️ CONSTRAINT #43]** Token constraint\n"
            "4. **[COMPONENT #12]** Repeated label\n"
        )
        self.assertEqual(
            legacy_displayed_refs(text),
            ["memory:12", "episodic:12", "memory:43"],
        )
        self.assertEqual(displayed_rank(legacy_displayed_refs(text), "memory:43"), 3)
        self.assertEqual(displayed_rank(legacy_displayed_refs(text), "memory:99"), None)
        self.assertIsNone(displayed_rank(["episodic:12"], "memory:12"))

    def test_truncated_unfinished_entry_is_not_counted(self):
        text = "1. **[📦 COMPONENT #12]** Good\n2. **[📦 COMPONENT #4"
        self.assertEqual(legacy_displayed_refs(text), ["memory:12"])

    def test_metrics_do_not_invent_false_positive_rate(self):
        items = [
            {
                "fact_id": 3,
                "legacy": {
                    "rank": 1, "elapsed_ms": 1.4, "displayed_count": 5,
                    "context_chars": 250,
                },
            },
            {
                "fact_id": 7,
                "legacy": {
                    "rank": None, "elapsed_ms": 4.1, "displayed_count": 4,
                    "context_chars": 330,
                },
            },
        ]
        result = summarize(items, "legacy", budget=300)
        self.assertEqual(result["gold_delivered_at_5"], 0.5)
        self.assertEqual(result["delivered_mrr"], 0.5)
        self.assertEqual(result["avg_displayed_facts"], 4.5)
        self.assertEqual(result["budget_breaches"], 1)
        self.assertEqual(result["missed_fact_ids"], [7])
        self.assertNotIn("false_positive_rate", result)
        self.assertEqual(result["latency_p95_ms"], 4.1)

    def test_empty_rows_rejected(self):
        with self.assertRaises(ValueError):
            summarize([], "legacy", 2900)


if __name__ == "__main__":
    unittest.main()
