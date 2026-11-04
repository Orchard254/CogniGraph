"""Offline, non-gating comparison of contexts ACTUALLY delivered to coding agents.

The existing golden fixture labels one relevant fact per query. This measures
whether that fact reaches the displayed primary briefing, not whether an agent
can solve a coding task, whether all other facts are irrelevant, or whether
either system is "intelligent". Never use these results as learning feedback.

Usage:
  python scripts/eval_context_delivery.py
  python scripts/eval_context_delivery.py --split tuning --max-items 8 --json

The frozen fixture is verified by SHA-256 and copied with SQLite's backup API;
all candidate retrievals suppress usage logging and popularity updates. Query
embeddings and the recency clock are pinned exactly as in the existing gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "eval"))

import eval_golden  # noqa: E402
from memory_store import MemoryStore  # noqa: E402
from pm_search import PINNED_NOW_ENV  # noqa: E402

FIXTURE = REPO_ROOT / "eval" / "fixtures" / "golden_fixture.db"
EXPECTATIONS = REPO_ROOT / "eval" / "fixtures" / "golden_expectations.json"
SPLITS = {
    "tuning": REPO_ROOT / "eval" / "golden_tuning.json",
    "holdout": REPO_ROOT / "eval" / "golden_holdout.json",
}
# Only numbered *primary* entries count. Graph-expanded incidental mentions
# are not an independently selected, attributable context fact.
LEGACY_ENTRY = re.compile(r"(?m)^\d+\.\s+\*\*\[([^\]\n]{1,100})\]\*\*")
FACT_ID = re.compile(r"#([1-9]\d*)$")
DEFAULT_BUDGET = 2900
MAX_ITEMS = 200


def legacy_displayed_refs(markdown: str) -> list[str]:
    """Extract source-qualified IDs from actual, rendered legacy primary facts."""
    refs = []
    for match in LEGACY_ENTRY.finditer(markdown):
        label = match.group(1)
        id_match = FACT_ID.search(label.strip())
        if not id_match:
            continue
        source = "episodic" if label.startswith("EPISODIC/") else "memory"
        ref = f"{source}:{id_match.group(1)}"
        if ref not in refs:
            refs.append(ref)
    return refs


def displayed_rank(refs: list[str], expected_ref: str) -> int | None:
    try:
        return refs.index(expected_ref) + 1
    except ValueError:
        return None


def summarize(items: list[dict], name: str, budget: int) -> dict:
    """Compute delivery metrics; extra unlabelled facts are NOT false positives."""
    rows = [item[name] for item in items]
    n = len(rows)
    if not n:
        raise ValueError("at least one benchmark item is required")
    durations = sorted(x["elapsed_ms"] for x in rows)
    return {
        "pairs": n,
        "gold_delivered_at_5": round(sum(x["rank"] is not None and x["rank"] <= 5 for x in rows) / n, 4),
        "gold_delivered_at_limit": round(sum(x["rank"] is not None for x in rows) / n, 4),
        "delivered_mrr": round(sum(1 / x["rank"] if x["rank"] else 0 for x in rows) / n, 4),
        "avg_displayed_facts": round(sum(x["displayed_count"] for x in rows) / n, 2),
        "avg_context_chars": round(sum(x["context_chars"] for x in rows) / n, 1),
        "budget_breaches": sum(x["context_chars"] > budget for x in rows),
        "latency_median_ms": round(statistics.median(durations), 2),
        "latency_p95_ms": round(durations[max(0, (95 * n + 99) // 100 - 1)], 2),
        "missed_fact_ids": [item["fact_id"] for item in items if item[name]["rank"] is None],
    }


def _frozen_store(db_path: str) -> MemoryStore:
    store = MemoryStore(db_path)
    vectors = eval_golden.load_query_embeddings(store)
    if vectors is None:
        store.close()
        raise RuntimeError("Frozen fixture has no precomputed query vectors")

    def lookup(query: str):
        if query not in vectors:
            raise RuntimeError(f"No frozen vector for query: {query!r}")
        return vectors[query]

    store._query_encode = lookup

    # Legacy get_context() calls query_hybrid() with its default log_usage=True.
    # Disable *only the accounting*, not its ranker, during comparison.
    actual_search = store.query_hybrid

    def no_accounting(*args, **kwargs):
        kwargs["log_usage"] = False
        return actual_search(*args, **kwargs)

    store.query_hybrid = no_accounting
    return store


def _measure_legacy(store: MemoryStore, query: str, char_budget: int, limit: int) -> dict:
    started = time.perf_counter()
    rendered = store.get_context(
        query, limit=limit, max_chars=char_budget, agent_type="opencode",
    )
    elapsed = (time.perf_counter() - started) * 1000
    refs = legacy_displayed_refs(rendered)
    return {
        "refs": refs, "displayed_count": len(refs),
        "context_chars": len(rendered), "elapsed_ms": round(elapsed, 3),
    }


def _measure_intelligent(store: MemoryStore, query: str, char_budget: int, limit: int) -> dict:
    started = time.perf_counter()
    result = store.get_intelligent_context(
        query, phase="orient", limit=limit, max_chars=char_budget,
        agent_type="opencode", log_usage=False,
    )
    elapsed = (time.perf_counter() - started) * 1000
    refs = [item["ref"] for item in result["selected"]]
    rendered = result["context_markdown"]
    return {
        "refs": refs, "displayed_count": len(refs),
        "context_chars": len(rendered), "elapsed_ms": round(elapsed, 3),
        "review_warnings": len(result["warnings"]),
    }


def run(*, split: str = "both", max_items: int | None = None,
        max_chars: int = DEFAULT_BUDGET, limit: int = 5) -> dict:
    if split not in (*SPLITS, "both"):
        raise ValueError("invalid split")
    if not isinstance(max_chars, int) or max_chars < 512 or max_chars > 12000:
        raise ValueError("max_chars must be between 512 and 12000")
    if not isinstance(limit, int) or not 1 <= limit <= 12:
        raise ValueError("limit must be between 1 and 12")
    if max_items is not None and (not isinstance(max_items, int) or not 1 <= max_items <= MAX_ITEMS):
        raise ValueError("max_items must be between 1 and 200")

    expected = json.loads(EXPECTATIONS.read_text(encoding="utf-8"))
    fixture_sha = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    if fixture_sha != expected["fixture"]["sha256"]:
        raise ValueError("frozen fixture hash mismatch: do not benchmark altered data")

    old_clock = os.environ.get(PINNED_NOW_ENV)
    os.environ[PINNED_NOW_ENV] = expected["pinned_now"]
    copy_path = eval_golden.snapshot_db(str(FIXTURE))
    try:
        store = _frozen_store(copy_path)
        try:
            usage_before = eval_golden.usage_log_count(copy_path)
            accessed_before = store.conn.execute(
                "SELECT COALESCE(SUM(access_count), 0) FROM memory"
            ).fetchone()[0]
            chosen = SPLITS if split == "both" else {split: SPLITS[split]}
            report = {
                "fixture_sha256": fixture_sha,
                "pinned_now": expected["pinned_now"],
                "budget_chars": max_chars,
                "primary_fact_limit": limit,
                "context_phase": "orient",
                "splits": {},
                "limitations": [
                    "Each question labels only one relevant fact, so unjudged extras are not false positives.",
                    "Fact delivery is a retrieval proxy, not a coding-task success or agent-learning metric.",
                    "Latency measurements are diagnostic and hardware/CI sensitive.",
                    "The frozen fixture has no corresponding current source tree; provenance freshness is not evaluated.",
                ],
            }
            for split_name, path in chosen.items():
                items = eval_golden.load_golden(path)
                if max_items is not None:
                    items = items[:max_items]
                comparisons = []
                for i, item in enumerate(items):
                    # Alternate arm order to reduce warm-cache/order bias.
                    if i % 2:
                        intelligence = _measure_intelligent(store, item["query"], max_chars, limit)
                        legacy = _measure_legacy(store, item["query"], max_chars, limit)
                    else:
                        legacy = _measure_legacy(store, item["query"], max_chars, limit)
                        intelligence = _measure_intelligent(store, item["query"], max_chars, limit)
                    ref = f"memory:{item['fact_id']}"
                    for arm in (legacy, intelligence):
                        arm["rank"] = displayed_rank(arm["refs"], ref)
                    comparisons.append({
                        "fact_id": item["fact_id"],
                        "category": item["category"],
                        "query": item["query"],
                        "legacy": legacy,
                        "intelligence": intelligence,
                    })
                report["splits"][split_name] = {
                    "legacy": summarize(comparisons, "legacy", max_chars),
                    "intelligence": summarize(comparisons, "intelligence", max_chars),
                    "items": comparisons,
                }
            usage_after = eval_golden.usage_log_count(copy_path)
            accessed_after = store.conn.execute(
                "SELECT COALESCE(SUM(access_count), 0) FROM memory"
            ).fetchone()[0]
            report["isolation"] = {
                "usage_log_unchanged": usage_before == usage_after,
                "access_count_unchanged": accessed_before == accessed_after,
                "source_fixture_sha_unchanged": (
                    hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == fixture_sha
                ),
            }
            if not all(report["isolation"].values()):
                raise RuntimeError("evaluation changed retrieval accounting or the frozen source")
            return report
        finally:
            store.close()
    finally:
        shutil.rmtree(Path(copy_path).parent, ignore_errors=True)
        if old_clock is None:
            os.environ.pop(PINNED_NOW_ENV, None)
        else:
            os.environ[PINNED_NOW_ENV] = old_clock


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("both", "tuning", "holdout"), default="both")
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(split=args.split, max_items=args.max_items,
                     max_chars=args.max_chars, limit=args.limit)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"CONTEXT DELIVERY BENCHMARK FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("Context delivery comparison (diagnostic only; not coding success)")
        print(f"  fixture SHA-256: {report['fixture_sha256']}")
        for split_name, arms in report["splits"].items():
            print(f"  {split_name}:")
            for name in ("legacy", "intelligence"):
                r = arms[name]
                print(
                    f"    {name:13} delivered@5={r['gold_delivered_at_5']:.4f} "
                    f"MRR={r['delivered_mrr']:.4f} "
                    f"mean chars={r['avg_context_chars']:.1f} "
                    f"median latency={r['latency_median_ms']:.2f}ms "
                    f"budget breaches={r['budget_breaches']} "
                    f"missed={r['missed_fact_ids']}"
                )
        print("  fixture and retrieval accounting unchanged:", all(report["isolation"].values()))
        print("  Limitations: one gold fact per query; this is NOT an agent-outcome evaluation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
