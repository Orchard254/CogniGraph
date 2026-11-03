"""Safety and semantics of read-only retrieval simplification ablations."""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "benchmark_simplification", ROOT / "scripts" / "benchmark_simplification.py"
)
bench = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bench
SPEC.loader.exec_module(bench)


class SimplificationBenchmarkTests(unittest.TestCase):
    def test_full_profile_keeps_the_existing_lane_weights(self):
        from pm_search import AGENT_LANE_WEIGHTS
        original = dict(AGENT_LANE_WEIGHTS["default"])
        self.assertEqual(bench.weights_for("full", original), original)
        self.assertEqual(AGENT_LANE_WEIGHTS["default"], original)

    def test_opencode_full_profile_preserves_agent_specific_weights(self):
        from pm_search import AGENT_LANE_WEIGHTS
        original = dict(AGENT_LANE_WEIGHTS["opencode"])
        self.assertEqual(bench.weights_for("full", original), original)
        self.assertGreater(original["struct"], 0)

    def test_subset_profiles_zero_only_excluded_lane_weights(self):
        baseline = {"fts": 1.0, "tok": 0.85, "sem": 1.2,
                    "epi_fts": 1.0, "epi_tok": 0.85, "epi_sem": 1.2,
                    "struct": 0.5}
        expected = {
            "core": {"fts", "tok", "sem"},
            "lexical": {"fts", "tok"},
            "fts_only": {"fts"},
        }
        for profile, enabled in expected.items():
            with self.subTest(profile=profile):
                observed = bench.weights_for(profile, baseline)
                self.assertEqual(set(observed), set(baseline))
                for key, value in observed.items():
                    self.assertEqual(value, baseline[key] if key in enabled else 0)

    def test_invalid_profile_rejected(self):
        with self.assertRaises(ValueError):
            bench.weights_for("unsupported", {"fts": 1})

    def test_rank_comparison_counts_better_and_worse(self):
        full = [
            {"fact_id": 1, "rank": 2},
            {"fact_id": 2, "rank": 5},
            {"fact_id": 3, "rank": None},
            {"fact_id": 4, "rank": 1},
        ]
        ablated = [
            {"fact_id": 1, "rank": 1},
            {"fact_id": 2, "rank": None},
            {"fact_id": 3, "rank": 10},
            {"fact_id": 4, "rank": 1},
        ]
        result = bench.compare_ranks(full, ablated)
        self.assertEqual(result["changed"], 3)
        self.assertEqual(result["rank_regressions"], 1)
        self.assertEqual(result["rank_improvements"], 2)
        self.assertEqual(result["regressions"][0]["fact_id"], 2)

    def test_rank_comparison_never_mixes_golden_items(self):
        with self.assertRaises(ValueError):
            bench.compare_ranks(
                [{"fact_id": 1, "rank": 1}],
                [{"fact_id": 2, "rank": 1}],
            )

    def test_clock_context_restores_previous_value_even_after_failure(self):
        from pm_search import PINNED_NOW_ENV
        before = os.environ.get(PINNED_NOW_ENV)
        try:
            os.environ[PINNED_NOW_ENV] = "2030-01-01"
            with self.assertRaises(RuntimeError):
                with bench._eval_clock("2026-09-11"):
                    self.assertEqual(os.environ[PINNED_NOW_ENV], "2026-09-11")
                    raise RuntimeError("test")
            self.assertEqual(os.environ[PINNED_NOW_ENV], "2030-01-01")
        finally:
            if before is None:
                os.environ.pop(PINNED_NOW_ENV, None)
            else:
                os.environ[PINNED_NOW_ENV] = before


if __name__ == "__main__":
    unittest.main()
