#!/usr/bin/env python
"""Read-only diagnostic: compare what legacy vs intelligent briefings deliver.

The frozen golden fixture labels a SINGLE expected fact per query. That gives
an honest delivery-recall proxy, not verified task success or a comprehensive
measure of misleading context. This script is intentionally report-only;
before adding a merge-blocking threshold, independently validate the labels
and collect real coding-task outcome evidence.

Run: python scripts/benchmark_context_delivery.py --split holdout
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import eval_golden  # noqa: E402
from pm_search import PINNED_NOW_ENV  # noqa: E402

FIXTURE = ROOT / "eval" / "fixtures" / "golden_fixture.db"
EXPECTATIONS = ROOT / "eval" / "fixtures" / "golden_expectations.json"
SPLITS = {
    "tuning": ROOT / "eval" / "golden_tuning.json",
    "holdout": ROOT / "eval" / "golden_holdout.json",
}
# Match ONLY numbered top-level facts, never incoming/graph neighbors; that is
# comparable to "selected", which also lists only primary delivered facts.
LEGACY_FACT_RE = re.compile(
    r"^\d+\.\s+\*\*\[([^\]]+)\]\*\*", re.MULTILINE
)
FACT_ID_RE = re.compile(r"#([1-9][0-9]*)\b")


def legacy_refs(markdown: str) -> list[str]:
    """Extract source-qualified top-level rendered refs, not graph mentions."""
    result = []
    for match in LEGACY_FACT_RE.finditer(markdown):
        label = match.group(1)
        ident = FACT_ID_RE.search(label)
        if not ident:
            continue
        source = "episodic" if label.startswith("EPISODIC/") else "memory"
        result.append(f"{source}:{ident.group(1)}")
    return result


def assess_cases(rows: list[dict], limit: int) -> dict:
    """Score a single gold target's position in the *delivered* briefing."""
    ranks = []
    lengths = []
    durations = []
    overflow = 0
    for row in rows:
        ref = f"memory:{row['expected_fact_id']}"
        shown = row["refs"][:limit]
        ranks.append(shown.index(ref) + 1 if ref in shown else None)
        lengths.append(row["chars"])
        durations.append(row["elapsed_ms"])
        overflow += int(row["chars"] > row["budget_chars"])
    n = len(rows)
    return {
        "n": n,
        "target_recall_at_k": round(sum(r is not None for r in ranks) / n, 4) if n else None,
        "target_mrr": round(sum(1 / r if r else 0 for r in ranks) / n, 4) if n else None,
        "mean_delivered_chars": round(statistics.mean(lengths), 1) if n else None,
        "median_latency_ms": round(statistics.median(durations), 2) if n else None,
        "budget_violations": overflow,
        "mean_primary_facts": round(statistics.mean(len(r["refs"]) for r in rows), 2) if n else None,
        "p95_latency_ms": round(sorted(durations)[max(0, (95 * n + 99) // 100 - 1)], 2) if n else None,
    }


def _run_variant(store, items: list[dict], variant: str, budget: int, limit: int) -> list[dict]:
    # Candidate discovery should be side-effect-free for evaluation. Existing
    # legacy get_context() has no log_usage parameter, so bind its hybrid
    # search to log_usage=False rather than modifying the production API.
    original_hybrid = store.query_hybrid

    def read_only_hybrid(*args, **kwargs):
        kwargs["log_usage"] = False
        return original_hybrid(*args, **kwargs)

    store.query_hybrid = read_only_hybrid
    query_embeddings = eval_golden.load_query_embeddings(store)
    if query_embeddings is None:
        raise RuntimeError("Frozen fixture is missing precomputed query embeddings")

    # The query embedding map has exactly the labeled queries. There are no
    # embedding downloads or alternate model results for this experiment.
    def frozen_encode(text: str):
        if text not in query_embeddings:
            raise RuntimeError(f"Fixture lacks embedding for golden query: {text!r}")
        return query_embeddings[text]

    store._query_encode = frozen_encode
    records = []
    for item in items:
        query = item["query"]
        started = time.perf_counter()
        if variant == "legacy":
            text = store.get_context(
                query, limit=limit, max_chars=budget, agent_type="opencode",
            )
            refs = legacy_refs(text)
        elif variant == "intelligent":
            output = store.get_intelligent_context(
                query, phase="orient", limit=limit, max_chars=budget,
                agent_type="opencode", log_usage=False,
            )
            text = output["context_markdown"]
            refs = [selected["ref"] for selected in output["selected"]]
            # All listed selected facts must appear in the delivered briefing.
            # Otherwise receipts/candidate attribution are not trustworthy.
            if any(f"[{ref}]" not in text for ref in refs):
                raise RuntimeError("Intelligent context selected a fact not shown in Markdown")
        else:
            raise ValueError(f"Unknown variant: {variant}")
        elapsed_ms = (time.perf_counter() - started) * 1000
        records.append({
            "expected_fact_id": item["fact_id"],
            "query": query,
            "refs": refs,
            "chars": len(text),
            "budget_chars": budget,
            "elapsed_ms": round(elapsed_ms, 3),
        })
    return records


def run_benchmark(split: str = "holdout", budget: int = 2900, limit: int = 5) -> dict:
    if split not in SPLITS and split != "both":
        raise ValueError("split must be tuning, holdout, or both")
    if not 512 <= budget <= 12000:
        raise ValueError("budget must be in the range 512..12000")
    if not 1 <= limit <= 12:
        raise ValueError("limit must be in the range 1..12")
    if not FIXTURE.exists() or not EXPECTATIONS.exists():
        raise FileNotFoundError("The committed frozen evaluation fixture is required")

    metadata = json.loads(EXPECTATIONS.read_text(encoding="utf-8"))
    source_sha = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    if source_sha != metadata["fixture"]["sha256"]:
        raise ValueError("Frozen fixture SHA-256 mismatch; do not benchmark altered data")

    # Restore the caller's clock even if the benchmark fails halfway through.
    old_clock = os.environ.get(PINNED_NOW_ENV)
    os.environ[PINNED_NOW_ENV] = metadata["pinned_now"]
    try:
        partitions = tuple(SPLITS) if split == "both" else (split,)
        reports = {}
        for partition in partitions:
            gold = eval_golden.load_golden(SPLITS[partition])
            by_variant = {}
            for variant in ("legacy", "intelligent"):
                path = eval_golden.snapshot_db(str(FIXTURE))
                try:
                    from memory_store import MemoryStore

                    store = MemoryStore(path)
                    try:
                        before_usage = eval_golden.usage_log_count(path)
                        before_access = store.conn.execute(
                            "SELECT COALESCE(SUM(access_count), 0) FROM memory"
                        ).fetchone()[0]
                        observed = _run_variant(store, gold, variant, budget, limit)
                        after_usage = eval_golden.usage_log_count(path)
                        after_access = store.conn.execute(
                            "SELECT COALESCE(SUM(access_count), 0) FROM memory"
                        ).fetchone()[0]
                        if (before_usage, before_access) != (after_usage, after_access):
                            raise RuntimeError(
                                "Context evaluation unexpectedly modified retrieval accounting"
                            )
                    finally:
                        store.close()
                finally:
                    shutil.rmtree(Path(path).parent, ignore_errors=True)
                by_variant[variant] = {
                    "metrics": assess_cases(observed, limit),
                    "misses": [
                        {"fact_id": row["expected_fact_id"], "query": row["query"]}
                        for row in observed
                        if f"memory:{row['expected_fact_id']}" not in row["refs"][:limit]
                    ],
                    "accounting_unchanged": True,
                }
            reports[partition] = by_variant

        if hashlib.sha256(FIXTURE.read_bytes()).hexdigest() != source_sha:
            raise RuntimeError("Benchmark changed the committed fixture")
        return {
            "dataset": "frozen single-target golden queries",
            "scope": "rendered primary-fact delivery; NOT coding outcome quality",
            "clock": metadata["pinned_now"],
            "fixture_sha256": source_sha,
            "source_fixture_unchanged": True,
            "limit": limit,
            "budget_chars": budget,
            "splits": reports,
            "comparison_note": (
                "A delivered target fact is not proof the agent used it correctly. "
                "This dataset does not label irrelevant/false facts or measure task completion. "
                "No performance threshold is set from this result."
            ),
        }
    finally:
        if old_clock is None:
            os.environ.pop(PINNED_NOW_ENV, None)
        else:
            os.environ[PINNED_NOW_ENV] = old_clock


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=(*SPLITS, "both"), default="holdout")
    parser.add_argument("--budget-chars", type=int, default=2900)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_benchmark(args.split, args.budget_chars, args.limit)
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"CONTEXT BENCHMARK ERROR: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Context delivery diagnostic (budget={args.budget_chars} chars, "
              f"top-{args.limit}, clock={report['clock']})")
        for split, variants in report["splits"].items():
            print(f"  {split}:")
            for name, data in variants.items():
                scores = data["metrics"]
                print(f"    {name:12} recall@k={scores['target_recall_at_k']:<7} "
                      f"mrr={scores['target_mrr']:<7} "
                      f"chars={scores['mean_delivered_chars']:<7} "
                      f"median_ms={scores['median_latency_ms']:<7} "
                      f"budget_violations={scores['budget_violations']} "
              f"p95_ms={scores['p95_latency_ms']} "
              f"primary_facts={scores['mean_primary_facts']}")
        print("  NOTE:", report["comparison_note"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
