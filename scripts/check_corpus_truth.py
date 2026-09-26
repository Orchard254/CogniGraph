"""Machine-checkable truth invariants over the live corpus.

Every expensive defect found in the 2026-09-27 audit session was *silent*:
a deprecated fact still ranking in search, a write landing in the wrong
project's database, a fact instructing agents to call tools that do not exist,
and a fact asserting "The server registers 14 tools" when 18 were registered.
None of them raised anything.

The test suite and the eval gate both guard CODE correctness. The eval gate
runs against a frozen fixture, so by design it can never see corpus rot. This
module closes that gap for the subset of claims that can be checked against
machine truth rather than human judgment.

The distinction that matters: `audit()` finds facts that are SIMILAR and needs
a person to decide which survives; `check_truth()` finds facts that are
factually WRONG relative to the running system, so it can block.

Scope is deliberately narrow and evidence-driven. Only invariants that have
already caught a real bug are implemented -- an invented check nobody has
needed yet is just maintenance burden. Extend TOOL_COUNT_CHECKS-style entries
when a new class of contradiction actually appears.

Not a CI job: `.cognigraph/` is gitignored, so CI has no live corpus to check,
and the frozen fixture's facts are historical by design (see eval/GATE.md,
"Fixture vs. live drift"). This runs against a real database, by hand or from
the maintenance suite.

Usage:
    python scripts/check_corpus_truth.py            # live DB
    python scripts/check_corpus_truth.py --db PATH  # a specific database
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Claims of the form "N tools" / "N-tool catalog" / "N MCP tools".
_TOOL_COUNT_RE = re.compile(
    r"\b(\d{1,3})\s*(?:-\s*tool|MCP\s+tools|tools\b)", re.IGNORECASE)
# Counts that are obviously not a live registry size, so historical
# "41-tool catalog" style records are not flagged as contradictions.
_MIN_PLAUSIBLE_TOOL_COUNT = 5


def live_tool_count() -> int:
    """Number of tools the server actually registers right now."""
    import server  # verified side-effect free by scripts/gen_tool_table.py
    return len(server.mcp._tool_manager._tools)


def _claimed_counts(text):
    for m in _TOOL_COUNT_RE.finditer(text or ""):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if n >= _MIN_PLAUSIBLE_TOOL_COUNT:
            yield n


def check_tool_count(db_path, actual=None):
    """Find active facts claiming a tool count the live registry contradicts.

    A fact may legitimately mention a historical count ("regenerated to 15
    tools at commit ba517be") as long as it also records the current value, or
    is explicitly marked resolved/historical. Flagging every historical mention
    would produce noise nobody acts on, so a fact is only a violation when it
    asserts a count and does not acknowledge the live one.
    """
    if actual is None:
        actual = live_tool_count()
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, category, content FROM memory "
            "WHERE deprecated_at IS NULL AND content IS NOT NULL").fetchall()
    finally:
        conn.close()

    violations = []
    for r in rows:
        text = r["content"]
        claims = sorted(set(_claimed_counts(text)))
        if not claims:
            continue
        if actual in claims:
            continue  # acknowledges the live value
        lowered = text.lower()
        # An explicitly historical/resolved record is not asserting the present.
        if any(marker in lowered for marker in
               ("resolved", "historical", "superseded", "at commit", "last commit",
                "was regenerated", "formerly", "previously", "stale", "drift",
                "at the time", "at that point", "as of")):
            continue
        violations.append({
            "id": r["id"],
            "category": r["category"],
            "claimed": claims,
            "actual": actual,
            "content": text[:200],
        })
    return violations


def check_all(db_path, actual=None):
    if actual is None:
        actual = live_tool_count()
    return {"tool_count": actual, "violations": check_tool_count(db_path, actual)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the live corpus against machine-checkable truth")
    parser.add_argument("--db", default=None, help="database (default: live)")
    args = parser.parse_args(argv)

    if args.db:
        db_path = args.db
    else:
        from pm_common import default_db_path
        db_path = default_db_path()

    report = check_all(db_path)
    v = report["violations"]
    print("=== Corpus Truth Check ===")
    print(f"Database            : {db_path}")
    print(f"Live tool registry  : {report['tool_count']}")
    print(f"Violations          : {len(v)}")
    if v:
        print("\n  These active facts contradict the running system. Each is a")
        print("  fact being SERVED to agents that is factually wrong. Fix with")
        print("  memory_supersede (preferred) or memory_deprecate.\n")
        for item in v:
            print(f"  #{item['id']} [{item['category']}] claims {item['claimed']}, "
                  f"live is {item['actual']}")
            print(f"    {item['content']}")
        return 1
    print("No machine-checkable contradictions found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
