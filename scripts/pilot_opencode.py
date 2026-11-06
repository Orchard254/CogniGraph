"""Live installed-OpenCode delivery pilot for the CogniGraph V2 plugin.

This is the "Live OpenCode pilot" stage of the validation ladder in
docs/PRODUCT_THESIS_AND_VALIDATION.md: it runs the REAL installed OpenCode
CLI in an isolated sandbox project and checks whether the CogniGraph plugin
actually injects context into the model request.

Two arms, same prompt, same model, fresh sandboxes:
  treated : plugin installed via install_opencode.py + fixture memory DB
  control : no plugin (same DB files present, never loaded)

The prompt asks the agent to write the CogniGraph retrieval ID from its
system context (or NO_CONTEXT) into answer.txt. Delivery is confirmed only
when the treated arm reports a numeric retrieval ID and the control arm
reports NO_CONTEXT. Anything else is "inconclusive" with reasons, never a
silent pass.

Isolation rules:
- Sandboxes live in a temp workdir; the live repository database is never
  opened or copied from. The sandbox DB is a byte copy of the frozen golden
  fixture (eval/fixtures/golden_fixture.db). No SQLite ATTACH is used.
- The workdir must not be the repository root.
- Model runs are the only network/provider use; everything else is local.

Usage:
  python scripts/pilot_opencode.py [--model opencode/crouton-alpha-free]
      [--workdir DIR] [--report PATH] [--timeout SECONDS] [--keep]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURE_DB = REPO_ROOT / "eval" / "fixtures" / "golden_fixture.db"
DELIVERY_PROMPT = (
    'If your system context contains a line starting with "CogniGraph retrieval ID:", '
    "write only that number to a new file named answer.txt in the project root. "
    "If no such line exists, write exactly NO_CONTEXT to answer.txt. "
    "Do not read any other files."
)
DEFAULT_MODEL = "opencode/crouton-alpha-free"

_DESKTOP_CANDIDATES = (
    r"%LOCALAPPDATA%\Programs\ai.opencode.desktop\resources\opencode-cli.exe",
    r"%APPDATA%\ai.opencode.desktop\cli\*\opencode-cli.exe",
)


def find_cli_candidates() -> list[str]:
    """All locations a real installed OpenCode CLI might live, in order."""
    candidates: list[str] = []
    env_cli = os.environ.get("OPENCODE_CLI", "").strip()
    if env_cli:
        candidates.append(env_cli)
    on_path = shutil.which("opencode")
    if on_path:
        candidates.append(on_path)
    for pattern in _DESKTOP_CANDIDATES:
        expanded = os.path.expandvars(pattern)
        candidates.extend(sorted(Path(os.path.dirname(expanded)).glob(
            os.path.basename(expanded))) if "*" in expanded else [expanded])
    return candidates


def find_opencode_cli() -> str | None:
    """Return the path of an installed OpenCode CLI, or None."""
    for candidate in find_cli_candidates():
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return None


def cli_version(cli: str) -> str:
    try:
        proc = subprocess.run(
            [cli, "--version"], capture_output=True, text=True,
            timeout=30, stdin=subprocess.DEVNULL, shell=False,
        )
        return (proc.stdout or proc.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def extract_retrieval_id(answer_text: str) -> int | str | None:
    """Numeric retrieval ID, the literal NO_CONTEXT, or None if unclear."""
    text = (answer_text or "").strip()
    if text == "NO_CONTEXT":
        return "NO_CONTEXT"
    if re.fullmatch(r"\d{1,12}", text):
        return int(text)
    return None


def parse_run_events(stdout_text: str) -> dict:
    """Summarize `opencode run --format json` event lines.

    Returns session id, summed input/output tokens and cost across
    step_finish parts, and the concatenated assistant text parts.
    """
    tokens_in = 0
    tokens_out = 0
    cost = 0.0
    session_id = None
    texts: list[str] = []
    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part") or {}
        if event.get("sessionID"):
            session_id = event["sessionID"]
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        if event.get("type") == "step_finish" and isinstance(part, dict):
            step_tokens = part.get("tokens") or {}
            tokens_in += int(step_tokens.get("input") or 0)
            tokens_out += int(step_tokens.get("output") or 0)
            cost += float(part.get("cost") or 0.0)
    return {
        "session_id": session_id,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost": cost,
        "assistant_text": "\n".join(texts).strip(),
    }


def run_opencode(cli: str, cwd: Path, prompt: str, model: str,
                 timeout: int) -> dict:
    """Run one headless `opencode run` in cwd. Never uses a shell."""
    started = time.monotonic()
    record: dict = {
        "model": model, "exit_code": None, "seconds": None,
        "timed_out": False, "session_id": None, "tokens_in": 0,
        "tokens_out": 0, "cost": 0.0, "assistant_text": "",
        "plugin_loaded": False, "stdout_tail": "", "stderr_tail": "",
    }
    try:
        proc = subprocess.run(
            [cli, "--standalone", "run", "--format", "json", "--auto",
             "-m", model, prompt],
            cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL, shell=False,
        )
        record["exit_code"] = proc.returncode
        summary = parse_run_events(proc.stdout)
        record.update({k: summary[k] for k in (
            "session_id", "tokens_in", "tokens_out", "cost",
            "assistant_text")})
        record["stdout_tail"] = (proc.stdout or "")[-2000:]
        record["stderr_tail"] = (proc.stderr or "")[-2000:]
        record["plugin_loaded"] = "loading plugin" in (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        record["timed_out"] = True
        record["stdout_tail"] = (exc.stdout or b"").decode(
            "utf-8", "replace")[-2000:] if isinstance(exc.stdout, bytes) else ""
        record["stderr_tail"] = (exc.stderr or b"").decode(
            "utf-8", "replace")[-2000:] if isinstance(exc.stderr, bytes) else ""
    except OSError as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["seconds"] = round(time.monotonic() - started, 2)
    return record


def build_delivery_sandbox(base: Path, name: str, *, with_plugin: bool,
                           fixture_db: Path = FIXTURE_DB) -> Path:
    """Create an isolated sandbox project with a copied fixture DB."""
    if not fixture_db.is_file():
        raise FileNotFoundError(f"Frozen fixture DB missing: {fixture_db}")
    sandbox = Path(base) / name
    (sandbox / ".cognigraph").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture_db, sandbox / ".cognigraph" / "memory.db")
    (sandbox / "app.py").write_text("def hello():\n    return 42\n",
                                    encoding="utf-8")
    (sandbox / "README.md").write_text("pilot sandbox\n", encoding="utf-8")
    if with_plugin:
        from install_opencode import install
        install(str(sandbox), python_path=sys.executable)
    # The server writes logs next to the project when --print-logs is used;
    # run_opencode captures stderr itself, so no log file is needed here.
    return sandbox


def run_delivery_arm(cli: str, sandbox: Path, model: str,
                     timeout: int) -> dict:
    record = run_opencode(cli, sandbox, DELIVERY_PROMPT, model, timeout)
    answer_path = sandbox / "answer.txt"
    answer = answer_path.read_text(encoding="utf-8").strip() \
        if answer_path.is_file() else ""
    record["answer"] = answer[:200]
    record["retrieval_id"] = extract_retrieval_id(answer)
    return record


def verdict_from_arms(treated: dict, control: dict) -> tuple[str, str]:
    """Delivery verdict from the two arms. Returns (verdict, reason)."""
    treated_id = treated.get("retrieval_id")
    control_id = control.get("retrieval_id")
    if isinstance(treated_id, int) and control_id == "NO_CONTEXT":
        return ("delivery_confirmed",
                f"treated reported retrieval {treated_id}, "
                "control reported NO_CONTEXT")
    reasons = []
    if not isinstance(treated_id, int):
        reasons.append(f"treated answer was {treated.get('answer')!r}, "
                       "not a numeric retrieval ID")
    if control_id != "NO_CONTEXT":
        reasons.append(f"control answer was {control.get('answer')!r}, "
                       "not NO_CONTEXT (control may have hallucinated)")
    if treated.get("timed_out") or control.get("timed_out"):
        reasons.append("an arm timed out")
    if treated.get("error") or control.get("error"):
        reasons.append(f"run error: {treated.get('error') or control.get('error')}")
    return ("inconclusive", "; ".join(reasons) or "no signal")


def run_pilot(cli: str, workdir: Path, model: str, timeout: int) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    treated_sandbox = build_delivery_sandbox(
        workdir, "treated", with_plugin=True)
    control_sandbox = build_delivery_sandbox(
        workdir, "control", with_plugin=False)
    treated = run_delivery_arm(cli, treated_sandbox, model, timeout)
    control = run_delivery_arm(cli, control_sandbox, model, timeout)
    verdict, reason = verdict_from_arms(treated, control)
    return {
        "tool": "pilot_opencode",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_head": _git_head(),
        "cli": str(cli),
        "cli_version": cli_version(cli),
        "model": model,
        "fixture_db": str(FIXTURE_DB),
        "prompt": DELIVERY_PROMPT,
        "arms": {"treated": treated, "control": control},
        "verdict": verdict,
        "verdict_reason": reason,
    }


def _git_head() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL, shell=False,
        )
        return proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _guard_workdir(workdir: Path) -> Path:
    resolved = Path(workdir).expanduser().resolve()
    if resolved == REPO_ROOT.resolve():
        raise ValueError("Pilot workdir must not be the repository root: "
                         "the live database must never be touched.")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live installed-OpenCode CogniGraph delivery pilot")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--workdir", default=None,
                        help="Sandbox parent dir (default: fresh temp dir)")
    parser.add_argument("--report", default=None,
                        help="Report JSON path (default: <workdir>/report.json)")
    parser.add_argument("--timeout", type=int, default=420,
                        help="Per-arm timeout in seconds")
    parser.add_argument("--keep", action="store_true",
                        help="Keep sandboxes (default: keep, they are in temp)")
    args = parser.parse_args(argv)

    cli = find_opencode_cli()
    if not cli:
        print("No installed OpenCode CLI found. Set OPENCODE_CLI or install "
              "OpenCode first.", file=sys.stderr)
        return 2
    workdir = Path(args.workdir) if args.workdir else Path(
        tempfile.mkdtemp(prefix="cognigraph-pilot-"))
    try:
        workdir = _guard_workdir(workdir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"CLI: {cli} ({cli_version(cli)})")
    print(f"Model: {args.model}  Workdir: {workdir}")
    report = run_pilot(cli, workdir, args.model, args.timeout)
    report_path = Path(args.report) if args.report else workdir / "report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for name in ("treated", "control"):
        arm = report["arms"][name]
        print(f"  {name:8s} answer={arm['answer']!r} "
              f"retrieval={arm['retrieval_id']!r} "
              f"tokens={arm['tokens_in']}+{arm['tokens_out']} "
              f"cost={arm['cost']} seconds={arm['seconds']}")
    print(f"Verdict: {report['verdict']} ({report['verdict_reason']})")
    print(f"Report: {report_path}")
    return 0 if report["verdict"] == "delivery_confirmed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
