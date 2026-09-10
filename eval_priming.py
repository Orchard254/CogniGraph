"""Priming evaluation harness.

Scores retrieval quality for realistic coding tasks against the live database:
whether the facts an LLM actually needs (expected facts + their files) land in
the top-K results of query_hybrid / get_context.

Usage:
    python eval_priming.py              # run all cases, print report
    python eval_priming.py --json       # machine-readable report

Each case declares a natural-language task and the IDs and/or file paths of the
facts it should retrieve. A case passes when at least one expected fact (by ID)
or one fact anchored to an expected file appears in the top-K.
"""
import argparse
import json
import sys
from typing import Dict, Any, List, Optional

from memory_store import MemoryStore

TOP_K = 5

# Expected facts are referenced by stable content substrings so the harness
# keeps working as fact IDs shift through supersession.
CASES: List[Dict[str, Any]] = [
    {
        "task": "fix the watcher so it re-embeds facts after file change",
        "intent": "fix",
        "expected_files": ["watcher.py"],
        "expected_content": ["watcher"],
    },
    {
        "task": "implement usage-pattern learning for retrieval ranking",
        "intent": "implement",
        "expected_files": ["pm_search.py", "memory_store.py"],
        "expected_content": ["usage", "query_hybrid"],
    },
    {
        "task": "refactor memory_store.py into focused modules",
        "intent": "refactor",
        "expected_files": ["memory_store.py", "pm_search.py", "pm_context.py"],
        "expected_content": ["mixin", "split", "module"],
    },
    {
        "task": "how does the bootstrap onboarding ingest a repository",
        "intent": "implement",
        "expected_files": ["bootstrap.py"],
        "expected_content": ["bootstrap"],
    },
]


def run_case(store: MemoryStore, case: Dict[str, Any], top_k: int) -> Dict[str, Any]:
    results = store.query_hybrid(case["task"], intent=case.get("intent", ""), limit=top_k,
                                 log_usage=False)
    retrieved_files = {
        (r.get("file_path") or "").replace("\\", "/").lower()
        for r in results if r.get("file_path")
    }
    expected_files = {f.lower() for f in case.get("expected_files", [])}

    file_hit = bool(retrieved_files & expected_files)
    id_hit = False
    content_hit = False
    for r in results:
        content = (r.get("content") or "").lower()
        if any(sub.lower() in content for sub in case.get("expected_content", [])):
            content_hit = True
            break

    passed = file_hit or content_hit or id_hit
    return {
        "task": case["task"],
        "intent": case.get("intent", ""),
        "passed": passed,
        "file_hit": file_hit,
        "content_hit": content_hit,
        "top_result": (results[0].get("content") or "")[:100] if results else None,
        "top_result_file": results[0].get("file_path") if results else None,
        "retrieved_files": sorted(retrieved_files),
    }


def run_graph_checks(store: MemoryStore) -> Dict[str, Any]:
    """One-hop code graph sanity: the graph must answer real symbol queries.

    Regression gate for the structural_edges layer — if edge extraction or
    resolution breaks, who_calls stops finding callers before any LLM does.
    """
    r = store.who_calls("query_hybrid")
    passed = bool(r.get("found")) and len(r.get("callers", [])) >= 1
    defs = r.get("definitions") or []
    return {
        "task": "code graph one-hop: who calls query_hybrid",
        "intent": "graph",
        "passed": passed,
        "file_hit": False,
        "content_hit": passed,
        "top_result": f"{len(r.get('callers', []))} callers found" if r.get("found") else "symbol not found",
        "top_result_file": defs[0].get("file_path") if defs else None,
        "retrieved_files": [],
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Priming retrieval quality evaluation")
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--json", action="store_true", help="emit machine-readable report")
    parser.add_argument("--db", default=None, help="path to memory.db (default: auto-detected)")
    args = parser.parse_args(argv)

    store = MemoryStore(args.db) if args.db else MemoryStore()
    report = [run_case(store, case, args.top_k) for case in CASES]
    report.append(run_graph_checks(store))

    passed = sum(1 for r in report if r["passed"])
    summary = {
        "cases": len(report),
        "passed": passed,
        "failed": len(report) - passed,
        "score": round(passed / len(report), 3) if report else 0.0,
        "results": report,
    }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(f"Priming eval: {passed}/{len(report)} cases passed (score {summary['score']})")
        for r in report:
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"  [{mark}] {r['task']}")
            print(f"         top result: {r['top_result']}")
            if r["top_result_file"]:
                print(f"         top file:   {r['top_result_file']}")

    store.close()
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

