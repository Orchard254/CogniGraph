"""Controlled coding-task comparison: with vs. without CogniGraph context.

Same tasks (scripts/pilot_tasks.py), same model, same prompt, two arms:
  treated : CogniGraph plugin installed; seeded facts are injected by the
            real host through the context hook before model requests.
  control : no plugin and no memory database. The agent only sees starter
            files. (In the first live run the control sandbox also contained
            a seeded DB and the control agents read it directly with
            SQLite — that run is invalid as a comparison and is recorded as
            such in docs/PILOT_OPENCODE.md.)

Scoring is deterministic (hidden check scripts, exit code), not an LLM
judge. Each arm also records tokens, cost, wall time, the bridge delivery
status for its prompt, and which files the agent created/changed.

Before any live run, the runner verifies through the real bridge
(opencode_context.get_plugin_context) that the treated sandbox's prompt
actually delivers the seeded fact (status ok and fact keyword present in
the briefing). If delivery fails, the arm is marked bridge_unavailable
instead of burning a model run on a broken setup.

--dry-run builds all sandboxes and performs the bridge check only: no CLI,
no model calls. Use it to validate tasks before a live comparison.

Usage:
  python scripts/pilot_compare.py --dry-run [--tasks future_import]
  python scripts/pilot_compare.py [--tasks future_import,debounce_this]
      [--model opencode/crouton-alpha-free] [--workdir DIR]
      [--report PATH] [--timeout SECONDS]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pilot_opencode import (  # noqa: E402
    DEFAULT_MODEL, _guard_workdir, find_opencode_cli, run_opencode,
)
from pilot_tasks import TASKS  # noqa: E402


def task_by_id(task_id: str, tasks: list[dict] | None = None) -> dict:
    for task in TASKS if tasks is None else tasks:
        if task["id"] == task_id:
            return task
    raise KeyError(f"unknown task id: {task_id}")


def seed_task_db(db_path: Path, seed_facts: list[dict]) -> int:
    """Create an isolated memory DB seeded with the task's facts.

    Returns the number of facts written. Embeddings are disabled for speed;
    task prompts are written to share lexical overlap with their seed facts
    so the lexical lanes deliver them (the bridge check verifies this).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    from memory_store import MemoryStore
    store = MemoryStore(str(db_path))
    try:
        # Temp sandboxes are outside any git repo; skip commit probing.
        store._current_git_commit = lambda: None  # type: ignore[method-assign]
        written = 0
        for fact in seed_facts:
            result = store.write(
                category=fact["category"], content=fact["content"],
                importance=fact.get("importance", 4),
                confidence=fact.get("confidence", "confirmed"),
                tags=fact.get("tags", ""), embed=False, force=True,
            )
            if result != "DUPLICATE_DETECTED":
                written += 1
        return written
    finally:
        store.close()


def build_task_sandbox(base: Path, task: dict, arm: str,
                       trial: int = 1) -> Path:
    """Create a sandbox for one task/arm/trial. arm is 'treated'/'control'."""
    sandbox = Path(base) / f"{task['id']}-{arm}-t{trial}"
    sandbox.mkdir(parents=True, exist_ok=True)
    for relpath, content in task["starter_files"].items():
        target = sandbox / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if arm == "treated":
        seed_task_db(sandbox / ".cognigraph" / "memory.db", task["seed_facts"])
        from install_opencode import install
        install(str(sandbox), python_path=sys.executable)
    # The control arm must not contain a readable memory database: a
    # capable agent can otherwise bypass the missing plugin by inspecting
    # .cognigraph/memory.db directly, contaminating the comparison (this
    # exact failure was observed in the first live run).
    return sandbox


def bridge_check(sandbox: Path, prompt: str) -> dict:
    """Ask the real bridge what the treated agent would be injected."""
    from opencode_context import get_plugin_context
    try:
        result = get_plugin_context(str(sandbox), prompt)
    except (ValueError, OSError, RuntimeError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}",
                "delivered_chars": 0, "retrieval_id": None}
    markdown = result.get("context_markdown") or ""
    return {
        "status": result.get("status"),
        "delivered_chars": len(markdown),
        "retrieval_id": result.get("retrieval_id"),
        "markdown": markdown[:4000],
    }


def list_agent_files(sandbox: Path, task: dict) -> dict:
    """Files the agent created or actually modified, excluding harness
    internals. A starter file counts as modified only if its content
    differs from the starter content recorded in the task definition."""
    created, modified = [], []
    starter = task["starter_files"]
    skip_dirs = {".cognigraph", ".opencode", ".git", "hidden_checks",
                 "node_modules", "__pycache__"}
    for path in sorted(sandbox.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(sandbox).as_posix()
        if any(part in skip_dirs for part in path.relative_to(sandbox).parts):
            continue
        if rel in {"run-stderr.log"}:
            continue
        if rel not in starter:
            created.append(rel)
        elif path.read_text(encoding="utf-8", errors="replace") != starter[rel]:
            modified.append(rel)
    return {"created": created, "modified": modified}


def run_hidden_check(sandbox: Path, task: dict) -> dict:
    """Write check files (post-run only) and execute the check command."""
    for relpath, content in task["check_files"].items():
        target = sandbox / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    cmd = [part.replace("{python}", sys.executable)
           for part in task["check_cmd"]]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=str(sandbox), capture_output=True, text=True,
            timeout=120, stdin=subprocess.DEVNULL, shell=False,
        )
        return {
            "passed": proc.returncode == 0 and "CHECK PASS" in proc.stdout,
            "exit_code": proc.returncode,
            "seconds": round(time.monotonic() - started, 2),
            "output_tail": ((proc.stdout or "") + (proc.stderr or ""))[-1000:],
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"passed": False, "exit_code": None,
                "seconds": round(time.monotonic() - started, 2),
                "output_tail": f"{type(exc).__name__}: {exc}"}


def run_task_arm(cli: str | None, sandbox: Path, task: dict, arm: str,
                 model: str, timeout: int, *, dry_run: bool) -> dict:
    record: dict = {
        "task_id": task["id"], "arm": arm, "model": model,
        "sandbox": str(sandbox),
    }
    if arm == "treated":
        check = bridge_check(sandbox, task["prompt"])
        record["bridge"] = {k: check[k] for k in (
            "status", "delivered_chars", "retrieval_id")}
        record["bridge_markdown"] = check["markdown"]
        # Briefings may strip backticks; check a punctuation-stable head.
        seed_text = task["seed_facts"][0]["content"][:25]
        record["bridge_delivered_seed"] = seed_text in check["markdown"]
        if check["status"] != "ok" or not record["bridge_delivered_seed"]:
            record["outcome"] = "bridge_unavailable"
            record["reason"] = ("bridge did not deliver the seed fact; "
                                "task setup is broken, not the model")
            return record
    else:
        record["bridge"] = None

    if dry_run:
        record["outcome"] = "dry_run"
        return record

    assert cli is not None
    run = run_opencode(cli, sandbox, task["prompt"], model, timeout)
    record["run"] = run
    if run.get("timed_out") or run.get("error") or \
            run.get("exit_code") not in (0, None):
        # Infrastructure failures (provider rate limits, CLI errors) must
        # never be scored as model failures.
        record["outcome"] = "run_failed"
        record["reason"] = run.get("error") or (
            f"opencode exited {run.get('exit_code')} with "
            f"{run.get('tokens_in', 0)} input tokens")
        return record
    record["files"] = list_agent_files(sandbox, task)
    record["check"] = run_hidden_check(sandbox, task)
    record["outcome"] = "passed" if record["check"]["passed"] else "failed"
    return record


def run_compare(cli: str | None, workdir: Path, tasks: list[dict],
                model: str, timeout: int, *, dry_run: bool,
                trials: int = 1) -> dict:
    if trials < 1:
        raise ValueError("trials must be >= 1")
    workdir.mkdir(parents=True, exist_ok=True)
    results = []
    for task in tasks:
        for arm in ("treated", "control"):
            for trial in range(1, trials + 1):
                sandbox = build_task_sandbox(workdir, task, arm, trial)
                record = run_task_arm(
                    cli, sandbox, task, arm, model, timeout,
                    dry_run=dry_run)
                record["trial"] = trial
                results.append(record)
    summary: dict[str, dict[str, int]] = {}
    for task in tasks:
        summary[task["id"]] = {}
        for arm in ("treated", "control"):
            outcomes = [r["outcome"] for r in results
                        if r["task_id"] == task["id"] and r["arm"] == arm]
            summary[task["id"]][arm] = sum(
                1 for o in outcomes if o == "passed")
    return {
        "tool": "pilot_compare",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "model": model,
        "trials": trials,
        "tasks": [t["id"] for t in tasks],
        "results": results,
        "passed_summary": summary,
        "note": (f"n={trials} per task/arm: signal only, not statistics. "
                 "Scoring is deterministic hidden checks; no LLM judge."),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Controlled coding-task comparison (CogniGraph on/off)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--tasks", default=None,
                        help="Comma-separated task ids (default: all)")
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--report", default=None)
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--trials", type=int, default=1,
                        help="Repeated trials per task/arm (default: 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build sandboxes + bridge check only; no model")
    args = parser.parse_args(argv)

    tasks = TASKS
    if args.tasks:
        wanted = [t.strip() for t in args.tasks.split(",") if t.strip()]
        missing = [t for t in wanted if t not in {x["id"] for x in TASKS}]
        if missing:
            print(f"Unknown task ids: {missing}", file=sys.stderr)
            return 2
        tasks = [task_by_id(t) for t in wanted]

    cli = None if args.dry_run else find_opencode_cli()
    if not args.dry_run and not cli:
        print("No installed OpenCode CLI found. Set OPENCODE_CLI or "
              "install OpenCode first.", file=sys.stderr)
        return 2
    workdir = Path(args.workdir) if args.workdir else Path(
        tempfile.mkdtemp(prefix="cognigraph-compare-"))
    try:
        workdir = _guard_workdir(workdir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"Model: {args.model}  Workdir: {workdir}  "
          f"trials={args.trials}  dry_run={args.dry_run}")
    report = run_compare(cli, workdir, tasks, args.model, args.timeout,
                         dry_run=args.dry_run, trials=args.trials)
    report_path = Path(args.report) if args.report else workdir / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for result in report["results"]:
        extra = ""
        if "check" in result:
            extra = f" check_passed={result['check']['passed']}"
        elif "bridge" in result and result["bridge"]:
            extra = (f" bridge_status={result['bridge']['status']} "
                     f"delivered_seed={result.get('bridge_delivered_seed')}")
        print(f"  {result['task_id']:16s} {result['arm']:8s} "
              f"t{result.get('trial', 1)} {result['outcome']}{extra}")
    print(f"Passed summary: {report['passed_summary']}")
    print(f"Report: {report_path}")
    if args.dry_run:
        return 0
    unavailable = [r for r in report["results"]
                   if r["outcome"] == "bridge_unavailable"]
    return 1 if unavailable else 0


if __name__ == "__main__":
    raise SystemExit(main())
