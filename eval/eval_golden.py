"""Golden-set retrieval evaluation on an isolated copy of the live DB.

Runs the query/fact pairs in eval/golden_set.json through query_hybrid and
scores recall@5, recall@10, MRR, and per-category recall. The live database
is only ever opened read-only here: it is snapshotted to a temp file with the
SQLite backup API (WAL-safe), and every query passes log_usage=False, so the
eval cannot write memory_usage_log rows or bump access_count on the store it
is trying to measure.

Usage:
    python eval/eval_golden.py             # two runs + determinism gate
    python eval/eval_golden.py --runs 1    # single run (ablation sweeps)
    python eval/eval_golden.py --json      # machine-readable report
    python eval/eval_golden.py --now 2026-09-11   # pin the recency clock (CI)

The --now flag (or the PROJECT_MEMORY_NOW env var) pins the clock the
recency lane ages facts against; without it, golden ranks drift day to
day as facts age and committed baseline numbers rot.
"""
import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from memory_store import MemoryStore  # noqa: E402
from pm_common import default_db_path, _blob_to_vec  # noqa: E402
from pm_search import PINNED_NOW_ENV, _parse_iso_instant  # noqa: E402

GOLDEN_PATH = Path(__file__).resolve().parent / "golden_set.json"
TOP_K = 10


def load_golden(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    data = json.loads((path or GOLDEN_PATH).read_text(encoding="utf-8"))
    return data["items"]


def snapshot_db(src_path: str) -> str:
    """Copy the DB into a temp dir via the SQLite backup API (WAL-safe).

    The source is opened read-only, so snapshotting can never mutate the live
    DB (not even a WAL checkpoint).
    """
    tmp_dir = tempfile.mkdtemp(prefix="cognigraph_eval_")
    dst_path = str(Path(tmp_dir) / "memory.db")
    src_uri = "file:" + Path(src_path).as_posix() + "?mode=ro"
    src = sqlite3.connect(src_uri, uri=True)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dst_path


def usage_log_count(db_path: str) -> int:
    conn = sqlite3.connect("file:" + Path(db_path).as_posix() + "?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM memory_usage_log").fetchone()[0]
    finally:
        conn.close()


def stale_facts(db_path: str, items: List[Dict[str, Any]]) -> List[int]:
    """Expected facts that are missing or deprecated in the snapshot."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        stale = []
        for item in items:
            row = conn.execute(
                "SELECT deprecated_at FROM memory WHERE id = ?", (item["fact_id"],)
            ).fetchone()
            if row is None or row["deprecated_at"] is not None:
                stale.append(item["fact_id"])
        return stale
    finally:
        conn.close()


def load_query_embeddings(store: MemoryStore) -> Optional[Dict[str, Any]]:
    """Precomputed golden-query vectors frozen into the fixture, or None.

    Only fixture DBs (scripts/freeze_eval_fixture.py) carry the
    eval_query_embeddings table. Swapping _query_encode for a lookup keeps
    fixture evals fully offline — CI never downloads the fastembed model —
    and pins the semantic lane's input, so runs reproduce exactly.
    """
    try:
        rows = store.conn.execute(
            "SELECT query_text, embedding FROM eval_query_embeddings"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    return {r["query_text"]: _blob_to_vec(r["embedding"]) for r in rows}


def run_once(copy_path: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # The frozen fixture predates the ux_stub_once fix (#530), so opening any
    # copy of it logs "run the #530 backfill, then reopen" -- advice that must
    # NOT be followed here, because this database exists to stay byte-frozen
    # and its sha256 is checked. The live corpus has the index and zero
    # duplicate cohorts, so the warning is true of the fixture and only of the
    # fixture. Muted narrowly, by message substring, rather than by lowering a
    # logger level: every OTHER warning from this constructor is still real and
    # still wanted.
    class _MuteFrozenFixture(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "ux_stub_once deferred" not in record.getMessage()

    # The warning is emitted by pm_common's module logger, which memory_store
    # imports -- filter that logger, not "memory_store".
    _ms_logger = logging.getLogger("pm_common")
    _flt = _MuteFrozenFixture()
    _ms_logger.addFilter(_flt)
    try:
        store = MemoryStore(copy_path)
    finally:
        _ms_logger.removeFilter(_flt)
    query_vecs = load_query_embeddings(store)
    if query_vecs is not None:
        def _frozen_lookup(text: str):
            if text not in query_vecs:
                raise RuntimeError(
                    f"fixture lacks a precomputed query embedding for: {text!r}; "
                    "re-run scripts/freeze_eval_fixture.py")
            return query_vecs[text]
        store._query_encode = _frozen_lookup
    per_item: List[Dict[str, Any]] = []
    try:
        for item in items:
            query = item["query"]
            results = store.query_hybrid(
                query, limit=TOP_K, log_usage=False, expand_graph=False
            )
            rank: Optional[int] = None
            for i, r in enumerate(results, start=1):
                if r.get("source") == "memory" and r.get("id") == item["fact_id"]:
                    rank = i
                    break
            per_item.append({
                "fact_id": item["fact_id"],
                "category": item["category"],
                "query": query,
                "detected_intent": store._detect_intent(query),
                "rank": rank,
                "top_id": results[0].get("id") if results else None,
                "top_result": (results[0].get("content") or "")[:90] if results else None,
            })
    finally:
        store.close()
    return per_item


def aggregate(per_item: List[Dict[str, Any]]) -> Dict[str, Any]:
    def recall(k: int, sub: List[Dict[str, Any]]) -> float:
        return round(sum(1 for p in sub if p["rank"] is not None and p["rank"] <= k) / len(sub), 4)

    def mrr(sub: List[Dict[str, Any]]) -> float:
        return round(sum(1.0 / p["rank"] if p["rank"] else 0.0 for p in sub) / len(sub), 4)

    per_category: Dict[str, Any] = {}
    for cat in sorted({p["category"] for p in per_item}):
        sub = [p for p in per_item if p["category"] == cat]
        per_category[cat] = {
            "n": len(sub),
            "recall@5": recall(5, sub),
            "recall@10": recall(10, sub),
            "mrr": mrr(sub),
        }
    return {
        "n": len(per_item),
        "recall@5": recall(5, per_item),
        "recall@10": recall(10, per_item),
        "mrr": mrr(per_item),
        "per_category": per_category,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Golden-set retrieval evaluation")
    parser.add_argument("--db", default=None, help="source memory.db (default: live DB)")
    parser.add_argument("--golden", default=None,
                        help="golden set file (default: eval/golden_set.json; "
                             "use golden_tuning.json / golden_holdout.json for the split)")
    parser.add_argument("--runs", type=int, default=2,
                        help="consecutive runs for the determinism gate (default 2)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable report")
    parser.add_argument("--now", default=None,
                        help="pin the recency clock (ISO date/datetime, e.g. 2026-09-11) so "
                             "golden ranks do not drift as facts age; required for CI")
    args = parser.parse_args(argv)

    if args.now is not None:
        if _parse_iso_instant(args.now) is None:
            print(f"ERROR: --now is not a valid ISO date/datetime: {args.now}", file=sys.stderr)
            return 1
        os.environ[PINNED_NOW_ENV] = args.now

    src_db = args.db or default_db_path()
    golden_path = Path(args.golden) if args.golden else GOLDEN_PATH
    items = load_golden(golden_path)

    usage_before = usage_log_count(src_db)

    runs: List[List[Dict[str, Any]]] = []
    stale: List[int] = []
    tmp_dirs: List[str] = []
    try:
        for i in range(max(1, args.runs)):
            copy_path = snapshot_db(src_db)
            tmp_dirs.append(str(Path(copy_path).parent))
            if i == 0:
                stale = stale_facts(copy_path, items)
            runs.append(run_once(copy_path, items))
    finally:
        for d in tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)

    usage_after = usage_log_count(src_db)

    deterministic = all(
        [p["rank"] for p in runs[0]] == [p["rank"] for p in run] for run in runs[1:]
    )
    agg = aggregate(runs[-1])
    usage_clean = usage_before == usage_after

    pinned = os.environ.get(PINNED_NOW_ENV)
    report = {
        "source_db": src_db,
        "pinned_now": pinned,
        "golden_pairs": len(items),
        "runs": len(runs),
        "deterministic": deterministic,
        "stale_fact_ids": stale,
        "usage_log_before": usage_before,
        "usage_log_after": usage_after,
        "usage_log_untouched": usage_clean,
        "aggregate": agg,
        "items": runs[-1],
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"Golden eval: {len(items)} pairs, {len(runs)} run(s), source {src_db}, "
              f"golden {golden_path.name}, "
              f"clock {'pinned to ' + pinned if pinned else 'wall time'}")
        print(f"  recall@5  = {agg['recall@5']}")
        print(f"  recall@10 = {agg['recall@10']}")
        print(f"  MRR       = {agg['mrr']}")
        print("  per-category:")
        for cat, c in agg["per_category"].items():
            print(f"    {cat:<11} n={c['n']:<3} recall@5={c['recall@5']:<7}"
                  f" recall@10={c['recall@10']:<7} mrr={c['mrr']}")
        print("  per-item:")
        for p in runs[-1]:
            mark = f"rank {p['rank']:<2}" if p["rank"] else "MISS  "
            print(f"    [{mark}] #{p['fact_id']:<4} {p['category']:<11}"
                  f" intent={p['detected_intent']:<10} {p['query']}")
            if p["rank"] is None:
                print(f"             top was #{p['top_id']}: {p['top_result']}")
        print(f"  determinism: {'IDENTICAL across runs' if deterministic else 'DRIFT between runs'}")
        print(f"  usage log: {usage_before} -> {usage_after}"
              + ("" if usage_clean else "  WARNING: live usage log changed (concurrent server?)"))
        if stale:
            print(f"  STALE golden pairs (fact superseded/deprecated): {stale}")

    if stale:
        print("ERROR: golden set is stale; update eval/golden_set.json", file=sys.stderr)
        return 1
    if not deterministic:
        print("ERROR: non-deterministic results across consecutive runs", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())