#!/usr/bin/env python3
"""
Microbenchmark suite for CogniGraph key operations.
Measures and saves JSON baselines. No behavior changes — just measurement.
"""
import gc
import json
import os
import platform
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from memory_store import MemoryStore
from structural_extract import extract_structures
from bootstrap import bootstrap_project


ITERATIONS = 50
REPO_ROOT = Path(__file__).parent.parent


def median(values: List[float]) -> float:
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n % 2 == 0:
        return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
    return sorted_vals[n // 2]


def p95(values: List[float]) -> float:
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * 0.95)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def mean(values: List[float]) -> float:
    return sum(values) / len(values)


def stats(values: List[float]) -> Dict[str, float]:
    return {
        "median_ms": round(median(values) * 1000, 3),
        "p95_ms": round(p95(values) * 1000, 3),
        "mean_ms": round(mean(values) * 1000, 3),
        "min_ms": round(min(values) * 1000, 3),
        "max_ms": round(max(values) * 1000, 3),
    }


def run_benchmark(name: str, fn, iterations: int = ITERATIONS, warmup: int = 5) -> Dict[str, Any]:
    """Run a benchmark with GC disabled, return stats dict."""
    # Warmup
    for _ in range(warmup):
        fn()

    # Measured runs
    gc.disable()
    try:
        times = []
        for _ in range(iterations):
            start = time.perf_counter()
            fn()
            end = time.perf_counter()
            times.append(end - start)
    finally:
        gc.enable()

    return {
        "name": name,
        "iterations": iterations,
        "stats": stats(times),
    }


def create_warm_db_with_facts(num_facts: int = 200) -> tuple[MemoryStore, str]:
    """Create a temp DB and seed it with ~200 facts (no embeddings for speed)."""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    store = MemoryStore(db_path=temp_db.name)
    # Disable git commit lookup for temp DBs (outside repo)
    store._current_git_commit = lambda: None

    # Seed facts using the same pattern as test_llm_priming.py seeded_store fixture
    categories = ['component', 'decision', 'constraint', 'state', 'other']
    for i in range(num_facts):
        cat = categories[i % len(categories)]
        store.write(
            category=cat,
            content=f'Benchmark fact {i}: {cat} content for testing retrieval performance',
            importance=(i % 5) + 1,
            tags=f'benchmark,{cat},test',
            file_path=f'bench/file_{i % 20}.py',
            line_start=1,
            line_end=10,
            embed=False,
            force=True,
        )
    return store, temp_db.name


def find_500_line_python_file() -> Path:
    """Find a real ~500-line Python file in the repo."""
    for py_file in REPO_ROOT.glob("*.py"):
        try:
            lines = py_file.read_text(encoding="utf-8", errors="replace").count('\n') + 1
            if 400 <= lines <= 600:
                return py_file
        except Exception:
            pass
    # Fallback: use memory_store.py (largest) and just take first 500 lines worth of nodes
    return REPO_ROOT / "memory_store.py"


def benchmark_init_cold() -> float:
    """MemoryStore() init with no existing DB file."""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    store = MemoryStore(db_path=temp_db.name)
    store.close()
    os.unlink(temp_db.name)


def benchmark_init_warm() -> float:
    """MemoryStore() init with existing DB with ~200 facts."""
    store, db_path = create_warm_db_with_facts(200)
    store.close()
    # Re-open to measure init on existing DB
    store2 = MemoryStore(db_path=db_path)
    store2.close()
    os.unlink(db_path)


def benchmark_write_with_embed() -> float:
    """write() with embed=True on fresh DB."""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    store = MemoryStore(db_path=temp_db.name)
    store._current_git_commit = lambda: None
    store.write(
        category='component',
        content='Benchmark write with embed test content for measuring embedding overhead',
        file_path='bench/test.py',
        line_start=1,
        line_end=10,
        embed=True,
    )
    store.close()
    os.unlink(temp_db.name)


def benchmark_write_without_embed() -> float:
    """write() with embed=False on fresh DB."""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    store = MemoryStore(db_path=temp_db.name)
    store._current_git_commit = lambda: None
    store.write(
        category='component',
        content='Benchmark write without embed test content for measuring base write overhead',
        file_path='bench/test.py',
        line_start=1,
        line_end=10,
        embed=False,
    )
    store.close()
    os.unlink(temp_db.name)


def benchmark_query_hybrid_warm() -> float:
    """query_hybrid() on warm DB with 200 facts."""
    store, db_path = create_warm_db_with_facts(200)
    store.query_hybrid('test query', limit=10)
    store.close()
    os.unlink(db_path)


def benchmark_get_context_warm() -> float:
    """get_context() on warm DB with 200 facts."""
    store, db_path = create_warm_db_with_facts(200)
    store.get_context('test query', limit=5)
    store.close()
    os.unlink(db_path)


def benchmark_current_git_commit() -> float:
    """_current_git_commit() on non-git path (temp DB)."""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    store = MemoryStore(db_path=temp_db.name)
    store._current_git_commit()
    store.close()
    os.unlink(temp_db.name)


def benchmark_sync_structural_nodes() -> float:
    """sync_structural_nodes() on a real 500-line python file."""
    py_file = find_500_line_python_file()
    nodes = extract_structures(py_file)
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    store = MemoryStore(db_path=temp_db.name)
    store.sync_structural_nodes(str(py_file.relative_to(REPO_ROOT)), nodes, project_root=str(REPO_ROOT))
    store.close()
    os.unlink(temp_db.name)


def benchmark_bootstrap_dry_run_full_false() -> float:
    """bootstrap_project(root, dry_run=True, full=False) on this repo."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Use a temp DB in the temp dir
        db_path = Path(tmpdir) / "memory.db"
        store = MemoryStore(db_path=str(db_path))
        bootstrap_project(REPO_ROOT, store=store, dry_run=True, full=False, max_facts=150)
        store.close()


def benchmark_bootstrap_dry_run_full_true() -> float:
    """bootstrap_project(root, dry_run=True, full=True) on this repo."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "memory.db"
        store = MemoryStore(db_path=str(db_path))
        bootstrap_project(REPO_ROOT, store=store, dry_run=True, full=True, max_facts=150)
        store.close()


def main():
    print("=" * 60)
    print("CogniGraph Microbenchmark Suite")
    print("=" * 60)
    print(f"Repo: {REPO_ROOT}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Platform: {platform.platform()}")
    print(f"Machine: {platform.machine()}")
    print(f"Iterations per benchmark: {ITERATIONS}")
    print()

    benchmarks = []

    # 1. MemoryStore() init cold
    print("[1/8] MemoryStore() init (cold: no DB file)...")
    cold = run_benchmark("MemoryStore init (cold)", benchmark_init_cold)
    benchmarks.append({"name": cold["name"], "cold_ms": cold["stats"], "warm_ms": None})

    # 2. MemoryStore() init warm
    print("[2/8] MemoryStore() init (warm: existing DB with ~200 facts)...")
    warm = run_benchmark("MemoryStore init (warm)", benchmark_init_warm)
    benchmarks[-1]["warm_ms"] = warm["stats"]

    # 3. write() with embed=True
    print("[3/8] write(category='component', ..., embed=True)...")
    write_embed = run_benchmark("write (embed=True)", benchmark_write_with_embed)
    benchmarks.append({"name": write_embed["name"], "cold_ms": write_embed["stats"], "warm_ms": None})

    # 4. write() with embed=False
    print("[4/8] write(category='component', ..., embed=False)...")
    write_no_embed = run_benchmark("write (embed=False)", benchmark_write_without_embed)
    benchmarks.append({"name": write_no_embed["name"], "cold_ms": write_no_embed["stats"], "warm_ms": None})

    # 5. query_hybrid on warm DB
    print("[5/8] query_hybrid('test query', limit=10) on warm DB...")
    query_hybrid = run_benchmark("query_hybrid (warm)", benchmark_query_hybrid_warm)
    benchmarks.append({"name": query_hybrid["name"], "cold_ms": None, "warm_ms": query_hybrid["stats"]})

    # 6. get_context on warm DB
    print("[6/8] get_context('test query', limit=5) on warm DB...")
    get_ctx = run_benchmark("get_context (warm)", benchmark_get_context_warm)
    benchmarks.append({"name": get_ctx["name"], "cold_ms": None, "warm_ms": get_ctx["stats"]})

    # 7. _current_git_commit
    print("[7/8] _current_git_commit() (non-git path)...")
    git_commit = run_benchmark("_current_git_commit", benchmark_current_git_commit)
    benchmarks.append({"name": git_commit["name"], "cold_ms": git_commit["stats"], "warm_ms": None})

    # 8. sync_structural_nodes on real 500-line file
    print("[8/8] sync_structural_nodes() on real 500-line Python file...")
    sync_struct = run_benchmark("sync_structural_nodes", benchmark_sync_structural_nodes)
    benchmarks.append({"name": sync_struct["name"], "cold_ms": sync_struct["stats"], "warm_ms": None})

    # 9. bootstrap dry_run=True, full=False
    print("[9/9] bootstrap_project(dry_run=True, full=False)...")
    boot_false = run_benchmark("bootstrap_project (dry_run, full=False)", benchmark_bootstrap_dry_run_full_false)
    benchmarks.append({"name": boot_false["name"], "cold_ms": boot_false["stats"], "warm_ms": None})

    # 10. bootstrap dry_run=True, full=True
    print("[10/10] bootstrap_project(dry_run=True, full=True)...")
    boot_true = run_benchmark("bootstrap_project (dry_run, full=True)", benchmark_bootstrap_dry_run_full_true)
    benchmarks.append({"name": boot_true["name"], "cold_ms": boot_true["stats"], "warm_ms": None})

    # Build output JSON
    output = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "python": sys.version.split()[0],
        "machine": platform.machine(),
        "platform": platform.platform(),
        "iterations": ITERATIONS,
        "benchmarks": benchmarks,
    }

    # Save to file
    date_str = datetime.now().strftime("%Y%m%d")
    out_path = Path(__file__).parent / f"baseline_{date_str}.json"
    out_path.write_text(json.dumps(output, indent=2))

    print()
    print("=" * 60)
    print(f"Baseline saved to: {out_path}")
    print("=" * 60)
    print()

    # Print key numbers
    print("Key Results (median_ms):")
    for b in benchmarks:
        cold_stats = b.get("cold_ms") or {}
        warm_stats = b.get("warm_ms") or {}
        cold = cold_stats.get("median_ms", "N/A")
        warm = warm_stats.get("median_ms", "N/A")
        if warm != "N/A":
            print(f"  {b['name']}: cold={cold}ms, warm={warm}ms")
        else:
            print(f"  {b['name']}: {cold}ms")

    return str(out_path)


if __name__ == "__main__":
    main()