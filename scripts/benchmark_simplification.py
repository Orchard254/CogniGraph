#!/usr/bin/env python3
"""Read-only, rank-only ablation of CogniGraph's retrieval lanes.

Four profiles use identical candidate generation, RRF, recency, feedback,
project scope, frozen query vectors and task labels. We zero selected
fusion weights rather than removing all disabled-lane computations. The
structural lane is an exception: existing code skips it at weight zero.
Rank/recall comparisons are valid for this ablation, but relative timing
is NOT a controlled implementation performance comparison.

Never opens the committed evaluation fixture for writing.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from eval_golden import aggregate, load_golden, load_query_embeddings, snapshot_db
from memory_store import MemoryStore
from pm_search import AGENT_LANE_WEIGHTS, PINNED_NOW_ENV

EXPECTATIONS = ROOT / "eval" / "fixtures" / "golden_expectations.json"
FIXTURE = ROOT / "eval" / "fixtures" / "golden_fixture.db"
SPLITS = {"tuning": "golden_tuning.json", "holdout": "golden_holdout.json"}
ALL = frozenset(("fts", "tok", "sem", "epi_fts", "epi_tok", "epi_sem", "struct"))
PROFILES = {
    "full": ALL,
    "core": frozenset(("fts", "tok", "sem")),
    "lexical": frozenset(("fts", "tok")),
    "fts_only": frozenset(("fts",)),
}


def weights_for(profile: str, baseline: dict[str, float]) -> dict[str, float]:
    if profile not in PROFILES:
        raise ValueError("unknown retrieval profile: " + str(profile))
    return {key: value if key in PROFILES[profile] else 0.0
            for key, value in baseline.items()}


def _rank_items(store: MemoryStore, items: list[dict], profile: str, agent_type: str = "default") -> tuple[list[dict], list[float]]:
    """Evaluate all golden pairs with no usage writes or ranking side effects."""
    out = []
    times_ms = []
    # Temporarily replace only the default agent's fusion weights. Restore
    # after each profile so no profile can contaminate another.
    baseline = dict(AGENT_LANE_WEIGHTS[agent_type])
    with patch.dict(AGENT_LANE_WEIGHTS, {agent_type: weights_for(profile, baseline)}):
        for item in items:
            query = item["query"]
            start = time.perf_counter()
            results = store.query_hybrid(
                query, agent_type=agent_type, limit=10,
                log_usage=False, expand_graph=False, explain=True,
            )
            times_ms.append((time.perf_counter() - start) * 1000)
            # Disabled lanes still create zero-score candidates in the
            # existing RRF implementation. They are NOT real matches in
            # an ablated profile and must not count toward recall.
            active = [row for row in results if row.get("fused_score", 0) > 0]
            rank = next(
                (i for i, row in enumerate(active, 1)
                 if row.get("source") == "memory" and row["id"] == item["fact_id"]),
                None,
            )
            out.append({
                "fact_id": item["fact_id"], "category": item["category"],
                "query": query, "rank": rank,
            })
    return out, times_ms


def compare_ranks(baseline: list[dict], other: list[dict]) -> dict:
    if [row["fact_id"] for row in baseline] != [row["fact_id"] for row in other]:
        raise ValueError("golden items differ between profiles")
    changed = []
    regressions = []
    gains = []
    for a, b in zip(baseline, other):
        if a["rank"] == b["rank"]:
            continue
        change = {"fact_id": a["fact_id"], "full_rank": a["rank"],
                  "profile_rank": b["rank"]}
        changed.append(change)
        # Missing = rank 11+ for this top-10 comparison.
        before = a["rank"] if a["rank"] is not None else 11
        after = b["rank"] if b["rank"] is not None else 11
        (regressions if after > before else gains).append(change)
    return {
        "changed": len(changed),
        "rank_regressions": len(regressions),
        "rank_improvements": len(gains),
        "regressions": regressions,
        "improvements": gains,
    }


@contextlib.contextmanager
def _eval_clock(clock: str):
    old = os.environ.get(PINNED_NOW_ENV)
    os.environ[PINNED_NOW_ENV] = clock
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(PINNED_NOW_ENV, None)
        else:
            os.environ[PINNED_NOW_ENV] = old


def benchmark(agent_type: str = "default") -> dict:
    if agent_type not in ("default", "opencode"):
        raise ValueError("benchmark agent must be default or opencode")
    exp = json.loads(EXPECTATIONS.read_text(encoding="utf-8"))
    import hashlib
    if hashlib.sha256(FIXTURE.read_bytes()).hexdigest() != exp["fixture"]["sha256"]:
        raise RuntimeError("Frozen fixture checksum mismatch; aborting analysis")
    report = {
        "agent_type": agent_type,
        "pinned_now": exp["pinned_now"],
        "fixture_sha256": exp["fixture"]["sha256"],
        "profiles": {name: sorted(keys) for name, keys in PROFILES.items()},
        "method": "zeroed fusion weights, exact positive-score ranking ablation; structural scan skipped at zero weight",
        "latency_caveat": "Most disabled lanes still execute candidate work; structural scanning is skipped at zero weight. Timings are NOT optimized implementation speedups.",
        "splits": {},
    }
    temp_dirs = []
    try:
        copy_path = snapshot_db(str(FIXTURE))
        temp_dirs.append(str(Path(copy_path).parent))
        # The frozen corpus predates one schema repair; the unrelated warning
        # does not change rankings. Keep logging available for real failures.
        import logging
        class _FixtureNoise(logging.Filter):
            def filter(self, record):
                return "ux_stub_once deferred" not in record.getMessage()
        logger = logging.getLogger("pm_common")
        flt = _FixtureNoise()
        logger.addFilter(flt)
        try:
            store = MemoryStore(copy_path)
        finally:
            logger.removeFilter(flt)
        try:
            vectors = load_query_embeddings(store)
            if not vectors:
                raise RuntimeError("Frozen fixture lacks precomputed query vectors")
            store._query_encode = lambda query: vectors[query]
            with _eval_clock(exp["pinned_now"]):
                for split_name, json_name in SPLITS.items():
                    items = load_golden(ROOT / "eval" / json_name)
                    profiles = {}
                    ranks = {}
                    for profile in PROFILES:
                        scored, timings = _rank_items(store, items, profile, agent_type)
                        ranks[profile] = scored
                        stats = aggregate(scored)
                        profiles[profile] = {
                            "n": stats["n"],
                            "recall@5": stats["recall@5"],
                            "recall@10": stats["recall@10"],
                            "mrr": stats["mrr"],
                        }
                        if profile == "full":
                            profiles[profile]["observed_latency_ms"] = {
                                "median": round(statistics.median(timings), 3),
                                "max": round(max(timings), 3),
                            }
                    deltas = {profile: compare_ranks(ranks["full"], ranks[profile])
                              for profile in PROFILES if profile != "full"}
                    report["splits"][split_name] = {
                        "profiles": profiles,
                        "relative_to_full": deltas,
                        "full_meets_committed_gate": (
                            all(profiles["full"][metric] >= exp["splits"][split_name][metric]
                                for metric in ("recall@5", "mrr"))
                            if agent_type == "default" else None),
                    }
        finally:
            store.close()
    finally:
        for directory in temp_dirs:
            shutil.rmtree(directory, ignore_errors=True)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only retrieval simplification ablation")
    parser.add_argument("--json", action="store_true", help="emit structured report")
    parser.add_argument("--agent", default="default", choices=("default", "opencode"),
                        help="agent-specific lane weights (default: default)")
    args = parser.parse_args(argv)
    result = benchmark(args.agent)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("CogniGraph retrieval simplification audit (frozen offline fixture, agent: " + args.agent + ")")
        print("Note:", result["latency_caveat"])
        for name, split in result["splits"].items():
            print(f"\n{name} ({split['profiles']['full']['n']} labeled queries)")
            for profile, stats in split["profiles"].items():
                print(f"  {profile:<9} recall@5={stats['recall@5']:.4f} "
                      f"recall@10={stats['recall@10']:.4f} MRR={stats['mrr']:.4f}")
            for profile, delta in split["relative_to_full"].items():
                print(f"  vs full: {profile:<9} {delta['rank_regressions']} worse, "
                      f"{delta['rank_improvements']} better, {delta['changed']} changed ranks")
    if args.agent == "default" and not all(
        split["full_meets_committed_gate"] for split in result["splits"].values()
    ):
        print("ERROR: Full profile does not meet current frozen gate", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
