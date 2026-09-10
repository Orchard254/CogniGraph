"""Merge-blocking retrieval-quality gate (CI: eval-gate job).

Runs eval_golden against the frozen fixture eval/fixtures/golden_fixture.db
with the pinned clock recorded in golden_expectations.json, twice per split
for the determinism check, and fails the build if recall@5 or MRR for either
split drops below the committed baseline, if the runs are not identical, or
if the fixture on disk no longer matches the committed hash.

The evaluation is fully offline: the fixture carries precomputed fact and
query embeddings, so the embedding model is never loaded or downloaded.
See eval/GATE.md for the threshold policy and the fixture refresh process.

Usage:
    python scripts/eval_gate.py
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "eval"))

import eval_golden  # noqa: E402
from pm_search import PINNED_NOW_ENV  # noqa: E402

FIXTURE_PATH = REPO_ROOT / "eval" / "fixtures" / "golden_fixture.db"
EXPECT_PATH = REPO_ROOT / "eval" / "fixtures" / "golden_expectations.json"
SPLIT_FILES = {"tuning": "golden_tuning.json", "holdout": "golden_holdout.json"}
GATED_METRICS = ("recall@5", "mrr")
REFRESH_HINT = ("Fix the regression, or if the change is intentional, re-run "
                "scripts/freeze_eval_fixture.py and commit the regenerated "
                "fixture + expectations in the SAME PR (see eval/GATE.md).")


def main(argv=None) -> int:
    if not FIXTURE_PATH.is_file() or not EXPECT_PATH.is_file():
        print("ERROR: fixture or expectations missing — run scripts/freeze_eval_fixture.py",
              file=sys.stderr)
        return 1
    exp = json.loads(EXPECT_PATH.read_text(encoding="utf-8"))

    sha = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    if sha != exp["fixture"]["sha256"]:
        print("ERROR: golden_fixture.db does not match the committed hash — the fixture "
              "and expectations must change together. Re-run scripts/freeze_eval_fixture.py "
              "and commit both files.", file=sys.stderr)
        return 1

    # Pin the clock to the instant the fixture was frozen; without it the
    # recency lane ages facts day to day and the baseline rots.
    import os
    os.environ[PINNED_NOW_ENV] = exp["pinned_now"]

    print(f"eval-gate: fixture {FIXTURE_PATH.name} (sha256 ok), "
          f"pinned clock {exp['pinned_now']}, rrf k={exp['rrf_k']}")
    failures = []
    for split, split_file in SPLIT_FILES.items():
        items = eval_golden.load_golden(REPO_ROOT / "eval" / split_file)
        per_runs, tmp_dirs = [], []
        try:
            for _ in range(2):
                copy_path = eval_golden.snapshot_db(str(FIXTURE_PATH))
                tmp_dirs.append(str(Path(copy_path).parent))
                per_runs.append(eval_golden.run_once(copy_path, items))
        finally:
            for d in tmp_dirs:
                shutil.rmtree(d, ignore_errors=True)

        if [p["rank"] for p in per_runs[0]] != [p["rank"] for p in per_runs[1]]:
            failures.append(f"{split}: non-deterministic across consecutive runs")
            continue

        agg = eval_golden.aggregate(per_runs[-1])
        expected = exp["splits"][split]
        print(f"  {split}: recall@5={agg['recall@5']} mrr={agg['mrr']} "
              f"(baseline recall@5={expected['recall@5']} mrr={expected['mrr']})")
        for metric in GATED_METRICS:
            if agg[metric] < expected[metric]:
                failures.append(f"{split}: {metric} dropped {expected[metric]} -> {agg[metric]}")

    if failures:
        print("\nGATE FAILED — retrieval quality regressed against the committed baseline:",
              file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(REFRESH_HINT, file=sys.stderr)
        return 1
    print("GATE PASSED: retrieval quality meets the committed baseline on both splits.")
    return 0


if __name__ == "__main__":
    sys.exit(main())