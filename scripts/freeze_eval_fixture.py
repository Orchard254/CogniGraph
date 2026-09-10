"""Freeze the live memory DB into the committed golden eval fixture.

Snapshots the live database into eval/fixtures/golden_fixture.db via the
SQLite backup API (source opened read-only, WAL-safe — the same method
eval_golden uses), verifies that every fact the golden set needs already
has a stored embedding so CI never downloads the fastembed model, then
runs the golden eval against a throwaway copy with the recency clock
pinned and writes the expected metrics to golden_expectations.json.

The pinned clock is load-bearing: _recency_weight ages facts against
"now", so without a fixed reference instant the golden ranks drift as
facts age and the CI gate (see .github/workflows/ci.yml eval-gate job)
would rot. The fixture is always created and evaluated with the same
pinned instant recorded in the expectations file.

Refresh process: a PR that intentionally changes retrieval MUST re-run
this script and commit the regenerated fixture + expectations together,
otherwise the eval-gate job fails. The gate failing IS the process
working.

Usage:
    python scripts/freeze_eval_fixture.py                 # default live DB
    python scripts/freeze_eval_fixture.py --db <path>     # explicit source
    python scripts/freeze_eval_fixture.py --now 2026-09-11
"""
import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "eval"))

import eval_golden  # noqa: E402
from pm_common import default_db_path  # noqa: E402
from pm_search import PINNED_NOW_ENV, _parse_iso_instant  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "eval" / "fixtures"
FIXTURE_PATH = FIXTURE_DIR / "golden_fixture.db"
EXPECT_PATH = FIXTURE_DIR / "golden_expectations.json"
SPLITS = {"tuning": "golden_tuning.json", "holdout": "golden_holdout.json"}
# RRF fusion constant committed in pm_search.py query_hybrid.
RRF_K = 5
DEFAULT_PINNED_NOW = "2026-09-11T00:00:00+00:00"


def snapshot_fixture(src_path: str, dst_path: Path) -> None:
    """Copy the live DB to dst_path via the SQLite backup API (WAL-safe).

    The source is opened read-only, so freezing can never mutate the live
    DB. The destination is integrity-checked; a failed check removes the
    partial file and raises.
    """
    dst_path.unlink(missing_ok=True)
    src = sqlite3.connect("file:" + Path(src_path).as_posix() + "?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            src.backup(dst)
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            dst.close()
    except Exception:
        dst_path.unlink(missing_ok=True)
        raise
    finally:
        src.close()

    check = sqlite3.connect("file:" + dst_path.as_posix() + "?mode=ro", uri=True)
    try:
        row = check.execute("PRAGMA integrity_check").fetchone()
    finally:
        check.close()
    if not row or str(row[0]).lower() != "ok":
        dst_path.unlink(missing_ok=True)
        raise RuntimeError(f"fixture integrity check failed for {dst_path}")


def verify_embedding_coverage(fixture: Path) -> dict:
    """Every active fact (and every golden fact id) must already carry an
    embedding, so CI retrieval uses stored vectors and never downloads the
    fastembed model. Returns coverage counts for the report."""
    conn = sqlite3.connect("file:" + fixture.as_posix() + "?mode=ro", uri=True)
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE deprecated_at IS NULL").fetchone()[0]
        missing = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE deprecated_at IS NULL "
            "AND id NOT IN (SELECT fact_id FROM memory_embeddings)").fetchone()[0]
        golden_missing = []
        for split_file in SPLITS.values():
            items = eval_golden.load_golden(REPO_ROOT / "eval" / split_file)
            ids = [i["fact_id"] for i in items]
            ph = ",".join("?" * len(ids))
            stale = conn.execute(
                f"SELECT id FROM memory WHERE id IN ({ph}) "
                "AND (deprecated_at IS NOT NULL "
                "OR id NOT IN (SELECT fact_id FROM memory_embeddings))",
                ids,
            ).fetchall()
            golden_missing.extend(r[0] for r in stale)
    finally:
        conn.close()
    if missing:
        raise RuntimeError(
            f"{missing} active facts lack embeddings; run memory_cli.py backfill "
            "before freezing the fixture")
    if golden_missing:
        raise RuntimeError(
            f"golden fact ids missing/deprecated/unembedded: {sorted(set(golden_missing))}; "
            "update the golden set before freezing")
    return {"active_facts": active, "golden_fact_ids_ok": True}


def embed_golden_queries(fixture: Path) -> int:
    """Precompute an embedding for every golden query into the fixture.

    Fact embeddings travel in the snapshot, but the semantic lane also
    encodes the query at search time; without this table the gate would
    load (and on a fresh runner, download) the fastembed model. Freeze
    runs on a dev machine that has the model; CI only reads the blobs.
    """
    from pm_common import _encode, _vec_to_blob, _EMBED_MODEL_NAME
    queries = set()
    for fname in ("golden_set.json", *SPLITS.values()):
        for item in eval_golden.load_golden(REPO_ROOT / "eval" / fname):
            queries.add(item["query"])
    conn = sqlite3.connect(str(fixture))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS eval_query_embeddings ("
            "query_text TEXT PRIMARY KEY, model TEXT NOT NULL, embedding BLOB NOT NULL)")
        for q in sorted(queries):
            vec = _encode(q)
            if vec is None:
                raise RuntimeError(
                    "embedding model unavailable; freezing requires fastembed locally")
            conn.execute(
                "INSERT OR REPLACE INTO eval_query_embeddings VALUES (?, ?, ?)",
                (q, _EMBED_MODEL_NAME, _vec_to_blob(vec)))
        conn.commit()
        return len(queries)
    finally:
        conn.close()


def eval_split(fixture: Path, split_file: str, runs: int = 2) -> dict:
    """Run the golden eval for one split on throwaway copies of the fixture.

    Runs against copies (never the committed fixture itself) so MemoryStore
    startup migrations/WAL cannot dirty the artifact; consecutive runs must
    agree or the split is not gate-worthy.
    """
    items = eval_golden.load_golden(REPO_ROOT / "eval" / split_file)
    per_runs = []
    tmp_dirs = []
    try:
        for _ in range(runs):
            copy_path = eval_golden.snapshot_db(str(fixture))
            tmp_dirs.append(str(Path(copy_path).parent))
            per_runs.append(eval_golden.run_once(copy_path, items))
    finally:
        for d in tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)
    if any([p["rank"] for p in per_runs[0]] != [p["rank"] for p in r]
           for r in per_runs[1:]):
        raise RuntimeError(f"non-deterministic eval on {split_file}")
    agg = eval_golden.aggregate(per_runs[-1])
    stale = [p["fact_id"] for p in per_runs[-1] if p["rank"] is None]
    return {"n": agg["n"], "recall@5": agg["recall@5"],
            "recall@10": agg["recall@10"], "mrr": agg["mrr"],
            "missed_fact_ids": stale}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Freeze the golden eval fixture")
    parser.add_argument("--db", default=None, help="source memory.db (default: live DB)")
    parser.add_argument("--now", default=DEFAULT_PINNED_NOW,
                        help="pinned recency clock (ISO date/datetime); recorded in "
                             "the expectations and used by the CI gate")
    args = parser.parse_args(argv)

    if _parse_iso_instant(args.now) is None:
        print(f"ERROR: --now is not a valid ISO date/datetime: {args.now}", file=sys.stderr)
        return 1
    # Pin the clock BEFORE any MemoryStore is opened by the eval runs.
    import os
    os.environ[PINNED_NOW_ENV] = args.now

    src_db = args.db or default_db_path()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_fixture(src_db, FIXTURE_PATH)
    coverage = verify_embedding_coverage(FIXTURE_PATH)
    n_query_vecs = embed_golden_queries(FIXTURE_PATH)

    splits = {}
    for name, split_file in SPLITS.items():
        splits[name] = eval_split(FIXTURE_PATH, split_file)

    fixture_bytes = FIXTURE_PATH.stat().st_size
    fixture_sha256 = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    try:
        git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
            text=True, check=True).stdout.strip()
    except Exception:
        git_sha = None

    expectations = {
        "description": "Committed eval-gate baseline for golden_fixture.db. "
                       "Regenerate both files together via scripts/freeze_eval_fixture.py.",
        "pinned_now": args.now,
        "rrf_k": RRF_K,
        "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "git_commit_sha": git_sha,
        "fixture": {
            "path": "eval/fixtures/golden_fixture.db",
            "source_db": str(src_db),
            "bytes": fixture_bytes,
            "sha256": fixture_sha256,
            "active_facts": coverage["active_facts"],
            "embeddings_precomputed": True,
            "golden_query_embeddings": n_query_vecs,
        },
        "gate_policy": "FAIL if measured recall@5 or mrr for either split is below "
                       "the committed value. Deterministic pinned clock: expected "
                       "behavior is exact reproduction; any drop is a regression.",
        "splits": splits,
    }
    EXPECT_PATH.write_text(json.dumps(expectations, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")

    print(f"Fixture frozen: {FIXTURE_PATH}")
    print(f"  bytes: {fixture_bytes} ({fixture_bytes / (1024 * 1024):.2f} MiB)")
    print(f"  sha256: {fixture_sha256}")
    print(f"  pinned clock: {args.now}  rrf k: {RRF_K}  git: {git_sha}")
    print(f"  precomputed golden query embeddings: {n_query_vecs}")
    for name, s in splits.items():
        print(f"  {name}: n={s['n']} recall@5={s['recall@5']} "
              f"recall@10={s['recall@10']} mrr={s['mrr']}"
              + (f"  missed={s['missed_fact_ids']}" if s["missed_fact_ids"] else ""))
    print(f"Expectations written: {EXPECT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())