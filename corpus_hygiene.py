"""Corpus hygiene migration: de-pollute template stubs from boosted categories.

Census on 2026-09-10 (312 active facts) found 138 template-derived facts
("Source file: ...", "Key file: ...", "Directory '...'") sitting in
retrieval-boosted categories: 131 component, 6 decision, 1 constraint.
Intent multipliers rank those categories above 'other', so template noise
was outranking curated knowledge (the Phase-3 ablation confound).

The 6 decision + 1 constraint rows are worse than misfiled: their curated
content was destroyed by watcher._sync_file's pre-guard UPDATE, which
rewrote ANY active fact anchored to a changed file with the stub template.
Their original content is unrecoverable (checked every .bak in
.cognigraph/), so this migration deprecates them with an explicit reason
instead of pretending they still carry knowledge.

This migration:
  A. Re-categorizes ACTIVE template stubs in boosted categories
     (component/decision/constraint) to 'other' — retrieval-neutral, still
     served for file inventory.
  B. Deprecates the clobbered curated rows (stub content under
     decision/constraint) with reason 'corpus_hygiene_clobbered_curated'.

Idempotent: a second run finds no candidates and changes nothing.
--apply snapshots the DB first (WAL-safe, integrity-checked, pruned to
the newest 8) via pm_backup.timestamped_backup; the script refuses to
run without --apply.

Usage:
    python corpus_hygiene.py            # dry-run report on the live DB
    python corpus_hygiene.py --apply    # run the migration
    python corpus_hygiene.py --db PATH  # target a specific DB (tests/copies)
"""
import argparse
import sqlite3
import datetime
import sys
from pathlib import Path
from typing import Dict, Any

BOOSTED_CATEGORIES = ("component", "decision", "constraint")
STUB_PREFIXES = ("Source file:", "Key file:", "Directory '")
CLOBBER_REASON = "corpus_hygiene_clobbered_curated"


def _stub_where() -> str:
    """Bound-parameter form of the stub predicate. OR, never AND."""
    return " OR ".join("content LIKE ?" for _ in STUB_PREFIXES)


def _stub_params() -> tuple:
    return tuple(f"{p}%" for p in STUB_PREFIXES)


def _stub_where_inline() -> str:
    """Literal form of the same predicate, for DDL.

    SQLite refuses bound parameters in a partial index WHERE clause
    ("parameters prohibited in partial index WHERE clauses"), so a unique
    index over the stub prefixes has to inline the patterns. STUB_PREFIXES
    are module constants rather than user input, and every single quote is
    doubled, so this is safe -- "Directory '" becomes "Directory ''%".

    Kept beside _stub_where() so there is still exactly one definition of the
    predicate's shape, rather than a second hand-rolled copy that can drift.
    test_stub_dedup.py asserts the two forms agree.
    """
    return " OR ".join(
        "content LIKE '" + p.replace("'", "''") + "%'" for p in STUB_PREFIXES)


def corpus_hygiene(db_path: str | Path, apply: bool = False) -> Dict[str, Any]:
    """Run (or preview) the hygiene migration. Returns before/after stats."""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")

    def counts() -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        out["active_total"] = conn.execute(
            "SELECT COUNT(*) c FROM memory WHERE deprecated_at IS NULL").fetchone()["c"]
        by_cat = {}
        for r in conn.execute(
                "SELECT category, COUNT(*) c FROM memory WHERE deprecated_at IS NULL GROUP BY category"):
            by_cat[r["category"]] = r["c"]
        out["active_by_category"] = by_cat
        out["active_stubs_boosted"] = conn.execute(
            f"SELECT COUNT(*) c FROM memory WHERE deprecated_at IS NULL AND category IN "
            f"({','.join('?' * len(BOOSTED_CATEGORIES))}) AND ({_stub_where()})",
            BOOSTED_CATEGORIES + _stub_params()).fetchone()["c"]
        out["clobbered_curated"] = conn.execute(
            f"SELECT COUNT(*) c FROM memory WHERE deprecated_at IS NULL AND category IN "
            f"('decision','constraint') AND ({_stub_where()})",
            _stub_params()).fetchone()["c"]
        return out

    before = counts()

    # Clobbered curated rows: stub content under decision/constraint. Their
    # original knowledge is gone; deprecate rather than re-categorize so the
    # corpus stops serving destroyed content as if it were a decision.
    clobber_rows = conn.execute(
        f"SELECT id, category, file_path FROM memory WHERE deprecated_at IS NULL AND "
        f"category IN ('decision','constraint') AND ({_stub_where()})",
        _stub_params()).fetchall()

    # Template stubs in component (decision/constraint handled above).
    stub_rows = conn.execute(
        f"SELECT id FROM memory WHERE deprecated_at IS NULL AND category = 'component' "
        f"AND ({_stub_where()})",
        _stub_params()).fetchall()

    report: Dict[str, Any] = {
        "db_path": str(db_path),
        "dry_run": not apply,
        "before": before,
        "recategorize_ids": [r["id"] for r in stub_rows],
        "deprecate": [
            {"id": r["id"], "category": r["category"], "file_path": r["file_path"]}
            for r in clobber_rows
        ],
    }

    if apply and (stub_rows or clobber_rows):
        from pm_backup import timestamped_backup
        report["backup"] = timestamped_backup(db_path, tag="pre-corpus-hygiene")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for r in stub_rows:
                conn.execute("UPDATE memory SET category='other' WHERE id=?", (r["id"],))
            for r in clobber_rows:
                conn.execute(
                    "UPDATE memory SET deprecated_at=?, deprecated_reason=? WHERE id=?",
                    (now, CLOBBER_REASON, r["id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    report["after"] = counts()
    conn.close()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Corpus hygiene migration (stub de-pollution)")
    parser.add_argument("--apply", action="store_true", help="execute (default: dry-run report)")
    parser.add_argument("--db", default=None, help="path to memory.db (default: auto-detected)")
    args = parser.parse_args(argv)

    if args.db:
        db_path = args.db
    else:
        from pm_common import default_db_path
        db_path = default_db_path()

    report = corpus_hygiene(db_path, apply=args.apply)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] corpus hygiene on {report['db_path']}")
    for label, snap in (("before", report["before"]), ("after", report["after"])):
        print(f"  {label}: active={snap['active_total']} "
              f"stubs_in_boosted={snap['active_stubs_boosted']} "
              f"clobbered_curated={snap['clobbered_curated']} "
              f"by_category={snap['active_by_category']}")
    print(f"  recategorize component->other: {len(report['recategorize_ids'])} facts")
    for item in report["deprecate"]:
        print(f"  deprecate #{item['id']} [{item['category']}] {item['file_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())