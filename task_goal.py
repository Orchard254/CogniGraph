"""Predeclared, evidence-bound task goals (never automatic user acceptance).

A goal contract is registered *before* its supporting evidence is collected.
No model answer, stored retrieval hit, or untrusted "all green" phrase satisfies
the contract. Check criteria target exact command hashes, source-read criteria
target a specific source path and current digest, and repair criteria require
an observed failing check followed by an edit and a passing check.

The result is at most 'evidence_ready_for_review'. An agent-proposed contract
cannot establish owner acceptance or broad task correctness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from pm_common import looks_like_secret
from task_checks import check_snapshot, classify_check
from task_evidence import (
    _connect, _database, _digest_file, _key, _safe_stored_path,
    safe_project_file,
)

_MAX_CRITERIA = 8
_MAX_CONTRACTS = 256
_GOAL_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_PROFILES = frozenset({"implementation", "repair", "inspection"})
_CRITERIA = frozenset({"source_read", "check", "regression", "manual_review"})
_ORIGINS = frozenset({"self_run", "ide_runtime"})


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS task_goal_contracts (
            session_key TEXT NOT NULL,
            task_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            profile TEXT NOT NULL,
            criteria_json TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            source_floor INTEGER NOT NULL,
            check_floor INTEGER NOT NULL,
            edit_floor INTEGER NOT NULL,
            recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(session_key,task_key)
        );
    """)


def _event_max(conn: sqlite3.Connection, table: str,
               session_key: str, task_key: str, kind: str | None = None) -> int:
    # Table names are controlled constants, never derived from user input.
    if table not in ("task_evidence_events", "task_check_events"):
        raise ValueError("invalid evidence table")
    try:
        where = "session_key=? AND task_key=?"
        args: tuple[Any, ...] = (session_key, task_key)
        if kind is not None:
            where += " AND kind=?"
            args += (kind,)
        row = conn.execute(
            "SELECT COALESCE(MAX(id),0) AS high_water FROM " + table
            + " WHERE " + where, args,
        ).fetchone()
        return int(row["high_water"])
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise


def _normalize(profile: str, criteria: list[dict[str, Any]]) -> list[dict[str, str]]:
    if profile not in _PROFILES:
        raise ValueError("invalid goal profile")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= _MAX_CRITERIA:
        raise ValueError("goal requires 1..8 explicit criteria")
    normalized: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_checks: set[tuple[str, str]] = set()
    seen_sources: set[str] = set()
    for item in criteria:
        if not isinstance(item, dict):
            raise ValueError("each criterion must be an object")
        ident, kind = item.get("id"), item.get("type")
        if (not isinstance(ident, str) or not _GOAL_ID.fullmatch(ident)
                or ident in seen_ids or kind not in _CRITERIA):
            raise ValueError("invalid or duplicated criterion")
        seen_ids.add(ident)
        if kind == "source_read":
            if set(item) != {"id", "type", "path"}:
                raise ValueError("source criteria need id, type and path")
            path = item["path"]
            if not _safe_stored_path(path) or path in seen_sources:
                raise ValueError("unsafe or duplicate source criterion")
            seen_sources.add(path)
            normalized.append({"id": ident, "type": kind, "path": path})
        elif kind in ("check", "regression"):
            if not set(item).issubset({"id", "type", "command", "origin"}):
                raise ValueError("unexpected check field")
            if not {"id", "type", "command"}.issubset(item):
                raise ValueError("check requires id, type and command")
            command = item["command"]
            if classify_check(command) is None or looks_like_secret(command):
                raise ValueError("unsupported or sensitive direct check")
            origin = item.get("origin", "self_run")
            if origin not in _ORIGINS:
                raise ValueError("invalid check evidence origin")
            command_hash = hashlib.sha256(command.strip().encode("utf-8")).hexdigest()
            fingerprint = (kind, command_hash)
            if fingerprint in seen_checks:
                raise ValueError("duplicate check")
            seen_checks.add(fingerprint)
            normalized.append({
                "id": ident, "type": kind,
                "command_hash": command_hash,
                "check_kind": classify_check(command) or "",
                "origin": origin,
            })
        else:
            if set(item) != {"id", "type"}:
                raise ValueError("manual_review accepts no self-attested result")
            normalized.append({"id": ident, "type": kind})
    kinds = {c["type"] for c in normalized}
    if profile in ("implementation", "repair") and not (
        "source_read" in kinds and ("check" in kinds or "regression" in kinds)
    ):
        raise ValueError("implementation requires source and executable check criteria")
    if profile == "repair" and "regression" not in kinds:
        raise ValueError("repair requires a red-then-green regression check")
    return normalized


def propose_goal(root: Path, session_id: str, task: str,
                 profile: str, criteria: list[dict[str, Any]]) -> dict[str, Any]:
    """Register a proposal and freeze the evidence baseline.

    Re-registering the identical contract is idempotent. Changing criteria
    increments revision and resets the evidence floor: previously gathered
    checks are not retroactively proof of a new goal.
    """
    root = root.resolve()
    if not root.is_dir() or looks_like_secret(task):
        raise ValueError("invalid or sensitive goal task")
    session_key, task_key = _key(session_id, task)
    clean = _normalize(profile, criteria)
    contents = json.dumps(clean, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256((profile + "\n" + contents).encode()).hexdigest()
    path = _database(root)
    path.parent.mkdir(exist_ok=True)
    conn = _connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            previous = conn.execute(
                "SELECT revision,contract_hash FROM task_goal_contracts "
                "WHERE session_key=? AND task_key=?",
                (session_key, task_key),
            ).fetchone()
            if previous and previous["contract_hash"] == digest:
                conn.commit()
                return {"status": "unchanged", "revision": int(previous["revision"])}
            source_floor = _event_max(
                conn, "task_evidence_events", session_key, task_key,
            )
            check_floor = _event_max(
                conn, "task_check_events", session_key, task_key,
            )
            edit_floor = _event_max(
                conn, "task_evidence_events", session_key, task_key, kind="edit",
            )
            revision = 1 if previous is None else int(previous["revision"]) + 1
            conn.execute(
                "INSERT INTO task_goal_contracts "
                "(session_key,task_key,revision,profile,criteria_json,contract_hash,"
                "source_floor,check_floor,edit_floor) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_key,task_key) DO UPDATE SET "
                "revision=excluded.revision,profile=excluded.profile,"
                "criteria_json=excluded.criteria_json,contract_hash=excluded.contract_hash,"
                "source_floor=excluded.source_floor,check_floor=excluded.check_floor,"
                "edit_floor=excluded.edit_floor,recorded_at=CURRENT_TIMESTAMP",
                (session_key, task_key, revision, profile, contents, digest,
                 source_floor, check_floor, edit_floor),
            )
            # Bound the number of distinct task goal contracts per project.
            conn.execute(
                "DELETE FROM task_goal_contracts WHERE rowid NOT IN "
                "(SELECT rowid FROM task_goal_contracts ORDER BY rowid DESC LIMIT ?)",
                (_MAX_CONTRACTS,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return {
        "status": "registered_proposal", "revision": revision,
        "criteria_count": len(clean), "accepted_by_owner": False,
    }


def _source_criterion(conn: sqlite3.Connection, root: Path,
                      session_key: str, task_key: str,
                      item: dict[str, str], read_floor: int) -> str:
    path = item["path"]
    try:
        row = conn.execute(
            "SELECT id,kind,source_hash FROM task_evidence_events "
            "WHERE session_key=? AND task_key=? AND file_path=? "
            "ORDER BY id DESC LIMIT 1",
            (session_key, task_key, path),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None or int(row["id"]) <= read_floor:
        return "needs_source_read"
    if row["kind"] != "read":
        return "needs_reread"
    # A stored path is not trusted merely because it matches a criterion.
    # Recheck symlinks and project bounds before opening its source bytes.
    actual = _digest_file(root, path) if safe_project_file(root, path) == path else None
    if not actual or row["source_hash"] != actual:
        return "stale_source"
    return "evidence_observed"


def _source_edit_between(
    conn: sqlite3.Connection, session_key: str, task_key: str,
    source_paths: tuple[str, ...], after_id: int,
    before_or_equal: int | None = None,
) -> bool:
    """Require an observed edit to one of the *contract's* source files."""
    if not source_paths:
        return False
    params: list[Any] = [session_key, task_key, after_id]
    sql = (
        "SELECT 1 FROM task_evidence_events "
        "WHERE session_key=? AND task_key=? AND kind='edit' AND id>? "
    )
    if before_or_equal is not None:
        sql += "AND id<=? "
        params.append(before_or_equal)
    sql += "AND file_path IN (" + ",".join("?" for _ in source_paths) + ") LIMIT 1"
    params.extend(source_paths)
    try:
        return conn.execute(sql, params).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _regression_red_exists(
    conn: sqlite3.Connection, session_key: str, task_key: str,
    item: dict[str, str], check_floor: int, green_watermark: int,
    origin: str, green_event_id: int,
    source_paths: tuple[str, ...],
) -> bool:
    if green_watermark <= 0:
        return False
    try:
        rows = conn.execute(
            "SELECT edit_watermark FROM task_check_events "
            "WHERE session_key=? AND task_key=? AND command_hash=? "
            "AND id>? AND exit_code!=0 AND tracked_count>0 "
            "AND edit_watermark<? AND origin=? AND id<? "
            "ORDER BY id DESC LIMIT 32",
            (session_key, task_key, item["command_hash"],
             check_floor, green_watermark, origin, green_event_id),
        ).fetchall()
    except sqlite3.OperationalError:
        return False
    return any(_source_edit_between(
        conn, session_key, task_key, source_paths,
        int(red["edit_watermark"]), green_watermark,
    ) for red in rows)


def goal_snapshot(root: Path, session_id: str, task: str) -> dict[str, Any]:
    """Judge the *proposal's evidence*, not successful implementation."""
    root = root.resolve()
    session_key, task_key = _key(session_id, task)
    unregistered = {
        "status": "unregistered", "criteria": [], "met": 0, "total": 0,
        "accepted_by_owner": False, "verified": False,
        "next_action": "Define explicit source and check criteria before claiming completion.",
    }
    db = _database(root)
    if not db.is_file():
        return unregistered
    conn = _connect(db)
    try:
        try:
            row = conn.execute(
                "SELECT profile,revision,criteria_json,contract_hash,source_floor,"
                "check_floor,edit_floor FROM task_goal_contracts "
                "WHERE session_key=? AND task_key=?",
                (session_key, task_key),
            ).fetchone()
        except sqlite3.DatabaseError:
            return unregistered
        if row is None or row["profile"] not in _PROFILES:
            return unregistered
        expected = hashlib.sha256(
            (row["profile"] + "\n" + row["criteria_json"]).encode("utf-8")
        ).hexdigest()
        if expected != row["contract_hash"]:
            return unregistered
        items = json.loads(row["criteria_json"])
        # A corrupted or externally modified database must not promote any
        # criterion into the model-visible text or report ready-for-review.
        if not isinstance(items, list) or len(items) > _MAX_CRITERIA:
            return unregistered
        source_floor = int(row["source_floor"])
        check_floor = int(row["check_floor"])
        edit_floor = int(row["edit_floor"])
        results: list[dict[str, str]] = []
        source_paths = tuple(item["path"] for item in items
                             if isinstance(item, dict) and item.get("type") == "source_read"
                             and _safe_stored_path(item.get("path")))
        for item in items:
            if not isinstance(item, dict) or not _GOAL_ID.fullmatch(str(item.get("id", ""))):
                return unregistered
            kind = item.get("type")
            if kind == "source_read":
                if not _safe_stored_path(item.get("path")):
                    return unregistered
                status = _source_criterion(
                    conn, root, session_key, task_key, item, source_floor,
                )
            elif kind in ("check", "regression"):
                command_hash = item.get("command_hash")
                origin = item.get("origin")
                if (not isinstance(command_hash, str)
                        or re.fullmatch("[0-9a-f]{64}", command_hash) is None
                        or origin not in _ORIGINS):
                    return unregistered
                check = check_snapshot(
                    root, session_id, task,
                    after_id=check_floor, command_hash=command_hash,
                )
                status = {
                    "not_observed": "needs_check",
                    "stale": "stale_check",
                    "unscoped_zero_exit": "unscoped_check",
                    "unscoped_nonzero_exit": "failed_check",
                    "observed_nonzero_exit": "failed_check",
                    "observed_zero_exit": "evidence_observed",
                }.get(check["status"], "needs_check")
                if (status == "evidence_observed"
                        and check.get("origin") != origin):
                    status = "wrong_evidence_origin"
                if (status == "evidence_observed" and kind == "regression"
                        and not _regression_red_exists(
                            conn, session_key, task_key, item, check_floor,
                            check["edit_watermark"], origin, check["event_id"],
                            source_paths,
                        )):
                    status = "needs_red_green_sequence"
            elif kind == "manual_review":
                status = "requires_external_review"
            else:
                return unregistered
            results.append({
                "id": item["id"], "type": kind, "status": status,
                "target": item.get("path", item.get("check_kind", "")),
            })
        # An implementation candidate requires an actual IDE edit after
        # proposal registration, not just a passing check against old code.
        if row["profile"] in ("implementation", "repair") and not (
            _source_edit_between(conn, session_key, task_key,
                                 source_paths, edit_floor)
        ):
            results.append({
                "id": "implementation_change", "type": "edit",
                "status": "needs_observed_edit", "target": "",
            })
        met = sum(item["status"] == "evidence_observed" for item in results)
        serious = ("failed_check", "stale_check", "stale_source")
        failed = next((item for item in results if item["status"] in serious), None)
        pending = next((item for item in results if item["status"] != "evidence_observed"), None)
        if failed:
            state = "blocked"
            action = {
                "failed_check": "Investigate the failing observed check for criterion ",
                "stale_check": "Rerun the stale check for criterion ",
                "stale_source": "Reinspect changed source for criterion ",
            }[failed["status"]] + failed["id"] + "."
        elif pending:
            state = "needs_evidence"
            action = {
                "needs_source_read": "Inspect current source for criterion ",
                "needs_reread": "Re-read edited source for criterion ",
                "needs_check": "Execute the registered check for criterion ",
                "unscoped_check": "Establish source scope, then rerun criterion ",
                "wrong_evidence_origin": "Run the independent verifier for criterion ",
                "needs_red_green_sequence": "Reproduce failure, edit, then rerun criterion ",
                "needs_observed_edit": "Make and observe a relevant source change for criterion ",
                "requires_external_review": "Request independent review for criterion ",
            }.get(pending["status"], "Investigate missing evidence for criterion ")
            action += pending["id"] + "."
        else:
            state = "evidence_ready_for_review"
            action = "Review the implementation against the original objective; no automatic acceptance."
        return {
            "status": state, "profile": row["profile"],
            "revision": int(row["revision"]), "criteria": results,
            "met": met, "total": len(results),
            "accepted_by_owner": False, "verified": False,
            "next_action": action,
        }
    except (ValueError, TypeError, KeyError, sqlite3.DatabaseError):
        return unregistered
    finally:
        conn.close()


def format_goal_snapshot(goal: dict[str, Any], max_chars: int = 340) -> str:
    """Compact, untrusted-data-safe context projection."""
    if goal.get("status") == "unregistered":
        text = ("### Goal gate: not registered\n"
                "Define acceptance checks before declaring the task complete.\n")
        return text if len(text) <= max_chars else ""
    header = (
        "### Goal gate (proposed criteria; not owner acceptance)\n"
        + "- " + goal["status"].replace("_", " ") + ": "
        + str(goal["met"]) + "/" + str(goal["total"]) + " observed\n"
    )
    action = "- Next: " + goal["next_action"] + "\n"
    if len(header) + len(action) > max_chars:
        return header if len(header) <= max_chars else ""
    lines = [header]
    for item in goal["criteria"]:
        if item["status"] == "evidence_observed":
            continue
        line = "- " + item["id"] + ": " + item["status"].replace("_", " ") + "\n"
        if sum(len(v) for v in lines) + len(line) + len(action) <= max_chars:
            lines.append(line)
    lines.append(action)
    return "".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Predeclare goal evidence criteria")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--profile", choices=sorted(_PROFILES), default="implementation")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.show:
            result = goal_snapshot(Path(args.project_root), args.session_id, args.task)
        else:
            payload = sys.stdin.read(16_385)
            if len(payload) > 16_384:
                raise ValueError("goal contract payload exceeds 16 KiB")
            criteria = json.loads(payload)
            result = propose_goal(
                Path(args.project_root), args.session_id, args.task,
                args.profile, criteria,
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, TypeError, json.JSONDecodeError, OSError, sqlite3.Error):
        print("invalid goal contract", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
