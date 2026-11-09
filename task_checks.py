"""Outcome-based task checks: real exit codes, versioned against source state.

No model judgement is accepted as verification. A direct runner can observe a
process exit itself; the OpenCode adapter can record a runtime-provided numeric
exit status. Neither output text nor an agent's narrative is interpreted.
A zero exit proves only that this particular command exited successfully, not
that the requested objective or all possible tests are satisfied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from task_evidence import _connect, _database, _digest_file, _key, task_snapshot

_MAX_CMD = 400
_MAX_TRACKED = 40
_MAX_CHECKS_PER_TASK = 32
_MAX_CHECKS_PER_PROJECT = 512

# Deliberately exclude compound shell programs and their deceptive exit codes:
# e.g. "pytest || true" must never turn a failed test into an observed pass.
_SHELL_OPERATORS = frozenset(";&|><$" + chr(96) + "\n\r\x00")
# Commands that intentionally do not execute checks cannot count as evidence.
# This list is conservative; unknown flags still require independent review.
_NON_EXECUTING_FLAGS = frozenset({
    "-h", "--help", "--version", "-V", "--collect-only", "--co",
    "--fixtures", "--fixtures-per-test", "--setup-plan", "--trace-config",
    "--list", "--list-tests",
})


def classify_check(command: str) -> str | None:
    """Recognize one direct test/lint invocation, not a shell pipeline."""
    if not isinstance(command, str) or not 1 <= len(command) <= _MAX_CMD:
        return None
    if any(ch in _SHELL_OPERATORS or ord(ch) < 32 for ch in command):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv or len(argv) > 48:
        return None
    exe = Path(argv[0].replace("\\", "/")).name.lower()
    args = argv[1:]
    if any(arg.split("=", 1)[0] in _NON_EXECUTING_FLAGS for arg in args):
        return None
    if exe in ("pytest", "py.test"):
        return "pytest"
    if re.fullmatch(r"python(?:3(?:\.\d{1,2})?)?(?:\.exe)?", exe):
        if len(args) >= 2 and args[0] == "-m":
            if args[1] in ("pytest", "unittest"):
                return args[1]
            if args[1] in ("ruff", "compileall") and (
                args[1] == "compileall" or (len(args) >= 3 and args[2] == "check")
            ):
                return args[1]
    if exe in ("npm", "npm.cmd") and args:
        if args[0] == "test":
            return "npm_test"
        if len(args) >= 2 and args[0] == "run" and args[1] in (
            "test", "lint", "typecheck", "check"
        ):
            return "npm_" + args[1]
    if exe == "node" and args and args[0] == "--test":
        return "node_test"
    if exe == "cargo" and args and args[0] in ("test", "check", "clippy"):
        return "cargo_" + args[0]
    if exe == "go" and args and args[0] in ("test", "vet"):
        return "go_" + args[0]
    if exe == "ruff" and args and args[0] == "check":
        return "ruff"
    return None


def _fingerprint(root: Path, evidence: dict[str, Any]) -> tuple[str | None, int]:
    paths = sorted(set(
        (evidence.get("observed_files") or [])
        + (evidence.get("edited_files") or [])
        + (evidence.get("stale_files") or [])
    ))[:_MAX_TRACKED]
    if not paths:
        return None, 0
    digest = hashlib.sha256()
    for relative in paths:
        # Paths come from task_snapshot, where persisted rows have been
        # revalidated. Missing/oversized files stay in the fingerprint so
        # they cannot silently turn into a verified healthy scope.
        source_hash = _digest_file(root, relative) or "unavailable"
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(source_hash.encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest(), len(paths)


def _edit_watermark(
    conn: sqlite3.Connection, session_key: str, task_key: str,
) -> int:
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(id),0) AS last_edit "
            "FROM task_evidence_events "
            "WHERE session_key=? AND task_key=? AND kind='edit'",
            (session_key, task_key),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # A project may run its first check before the IDE reads a file.
        if "no such table" in str(exc):
            return 0
        raise
    return int(row["last_edit"])


def _check_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS task_check_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_key TEXT NOT NULL,
            task_key TEXT NOT NULL,
            check_kind TEXT NOT NULL,
            command_hash TEXT NOT NULL,
            origin TEXT NOT NULL CHECK(origin IN ('self_run','ide_runtime')),
            exit_code INTEGER NOT NULL,
            input_fingerprint TEXT,
            tracked_count INTEGER NOT NULL,
            edit_watermark INTEGER NOT NULL,
            recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS task_check_recent
            ON task_check_events(session_key,task_key,id DESC);
    """)


def record_check(
    root: Path, session_id: str, task: str, command: str,
    exit_code: int, *, origin: str = "ide_runtime", cwd: str | None = None,
) -> dict[str, Any]:
    """Store only measured process status; never store command/output text."""
    if origin not in ("self_run", "ide_runtime"):
        raise ValueError("invalid evidence origin")
    if type(exit_code) is not int or not -128 <= exit_code <= 255:
        raise ValueError("numeric process exit code required")
    kind = classify_check(command)
    if kind is None:
        return {"status": "not_direct_check", "recorded": False}
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("invalid project root")
    if cwd is not None:
        if not isinstance(cwd, str) or len(cwd) > 512:
            raise ValueError("invalid working directory")
        try:
            workdir = (root / cwd).resolve()
            if not workdir.is_relative_to(root) or not workdir.is_dir():
                return {"status": "outside_project", "recorded": False}
        except (OSError, ValueError, RuntimeError):
            return {"status": "invalid_workdir", "recorded": False}

    session_key, task_key = _key(session_id, task)
    db = _database(root)
    db.parent.mkdir(exist_ok=True)
    conn = _connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _check_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            evidence = task_snapshot(root, session_id, task)
            fingerprint, count = _fingerprint(root, evidence)
            marker = _edit_watermark(conn, session_key, task_key)
            conn.execute(
                "INSERT INTO task_check_events "
                "(session_key,task_key,check_kind,command_hash,origin,exit_code,"
                "input_fingerprint,tracked_count,edit_watermark) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (session_key, task_key, kind, hashlib.sha256(
                    command.strip().encode("utf-8")).hexdigest(),
                 origin, exit_code, fingerprint, count, marker),
            )
            conn.execute(
                "DELETE FROM task_check_events WHERE session_key=? AND task_key=? "
                "AND id NOT IN (SELECT id FROM task_check_events "
                "WHERE session_key=? AND task_key=? ORDER BY id DESC LIMIT ?)",
                (session_key, task_key, session_key, task_key, _MAX_CHECKS_PER_TASK),
            )
            conn.execute(
                "DELETE FROM task_check_events WHERE id NOT IN "
                "(SELECT id FROM task_check_events ORDER BY id DESC LIMIT ?)",
                (_MAX_CHECKS_PER_PROJECT,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return {
        "status": "recorded", "recorded": True, "check_kind": kind,
        "exit_code": exit_code, "origin": origin,
        "scope": "tracked_source" if count else "unscoped",
    }


def check_snapshot(
    root: Path, session_id: str, task: str, command: str | None = None,
    *, after_id: int = 0, command_hash: str | None = None,
) -> dict[str, Any]:
    """Report the latest overall or exact-command check.

    Checking the exact command matters for multi-criterion goals: a passing
    lint process must not substitute for a requested unit test.
    """
    if command is not None and classify_check(command) is None:
        raise ValueError("invalid exact check command")
    if type(after_id) is not int or after_id < 0:
        raise ValueError("invalid check event lower bound")
    if command_hash is not None and (
        command is not None or not isinstance(command_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", command_hash) is None
    ):
        raise ValueError("invalid exact check hash")
    root = root.resolve()
    session_key, task_key = _key(session_id, task)
    blank = {"status": "not_observed", "verified": False, "check_kind": None}
    db = _database(root)
    if not db.is_file():
        return blank
    conn = _connect(db)
    try:
        sql = (
            "SELECT id,check_kind,origin,exit_code,input_fingerprint,"
            "tracked_count,edit_watermark FROM task_check_events "
            "WHERE session_key=? AND task_key=? AND id>?"
        )
        args: tuple[str | int, ...] = (session_key, task_key, after_id)
        if command is not None:
            sql += " AND command_hash=?"
            args += (hashlib.sha256(command.strip().encode("utf-8")).hexdigest(),)
        elif command_hash is not None:
            sql += " AND command_hash=?"
            args += (command_hash,)
        row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
        if row is None:
            return blank
        marker = _edit_watermark(conn, session_key, task_key)
    except sqlite3.DatabaseError:
        return blank
    finally:
        conn.close()
    evidence = task_snapshot(root, session_id, task)
    fingerprint, count = _fingerprint(root, evidence)
    if marker != int(row["edit_watermark"]) or (
        row["input_fingerprint"] != fingerprint or count != int(row["tracked_count"])
    ):
        status = "stale"
    elif count == 0:
        status = "unscoped_zero_exit" if row["exit_code"] == 0 else "unscoped_nonzero_exit"
    else:
        status = "observed_zero_exit" if row["exit_code"] == 0 else "observed_nonzero_exit"
    return {
        "status": status, "verified": False,
        "check_kind": str(row["check_kind"]),
        "origin": str(row["origin"]),
        "exit_code": int(row["exit_code"]),
        "tracked_count": count,
        "event_id": int(row["id"]),
        "edit_watermark": int(row["edit_watermark"]),
    }


def format_check_snapshot(check: dict[str, Any]) -> str:
    """A deliberately small assertion about a process, never about task success."""
    status = check.get("status")
    if status == "not_observed":
        return ""
    if status == "stale":
        return ("### Check observation\n- Prior check is stale after source "
                "or task edits; rerun it against current code.\n")
    kind = check["check_kind"]
    status_text = (
        "exited 0" if check["exit_code"] == 0
        else "exited nonzero (" + str(check["exit_code"]) + ")"
    )
    scope = ("tracked files unchanged" if status.startswith("observed_")
             else "no tracked source scope")
    return ("### Check observation (not task completion)\n"
            + "- " + kind + ": " + status_text + "; " + scope + ".\n"
            + "- Only this process exit was observed; broader correctness is unknown.\n")


def run_check(
    root: Path, session_id: str, task: str, argv: list[str],
    *, timeout: int = 120,
) -> dict[str, Any]:
    """Explicit opt-in, independent subprocess, never shell=True."""
    if type(timeout) is not int or not (1 <= timeout <= 600):
        raise ValueError("timeout must be 1..600 seconds")
    if not isinstance(argv, list) or not argv or any(
        not isinstance(arg, str) for arg in argv
    ):
        raise ValueError("invalid command argv")
    command = shlex.join(argv)
    if classify_check(command) is None:
        return {"status": "not_direct_check", "recorded": False}
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("invalid project root")
    try:
        completed = subprocess.run(
            argv, cwd=str(root), timeout=timeout, check=False,
            shell=False, stdin=subprocess.DEVNULL,
        )
        code = completed.returncode
    except subprocess.TimeoutExpired:
        code = 124
    except OSError:
        return {"status": "process_unavailable", "recorded": False}
    outcome = record_check(
        root, session_id, task, command, code,
        origin="self_run", cwd=str(root),
    )
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run an explicit test/check and record its real process exit."
    )
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    try:
        outcome = run_check(
            Path(args.project_root), args.session_id, args.task,
            command, timeout=args.timeout,
        )
        print(json.dumps(outcome, sort_keys=True))
        if not outcome.get("recorded"):
            return 2
        code = outcome["exit_code"]
        # Signals are negative subprocess return codes on POSIX. Never map
        # a signal-terminated verification process to shell exit 0.
        return min(255, 128 - code) if code < 0 else min(255, code)
    except (ValueError, sqlite3.Error) as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
