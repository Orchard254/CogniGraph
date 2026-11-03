"""Task-outcome observation ledger with explicit fact-level review candidates.

Observed file changes and caller-reported tests are NOT correctness evidence.
No ranking updates occur here: only explicit fact feedback can affect ranking.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from pm_common import looks_like_secret

_MAX_FILES = 20
_MAX_FACTS = 30
_SHA = re.compile(r"^[0-9a-f]{40}$", re.I)


def _relative_source_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 400:
        raise ValueError("each changed file must be a non-empty project-relative path")
    raw = value.strip().replace("\\", "/")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (raw.startswith("/") or posix.is_absolute() or windows.is_absolute() or
            windows.drive or any(p in ("..", "") for p in raw.split("/")) or
            any(ord(c) < 32 for c in raw) or ":" in raw):
        raise ValueError("changed file must be project-relative without traversal")
    relative = posix.as_posix()
    if (relative in ("", ".") or
            any(p.startswith(".") or p.lower() in ("node_modules", "vendor", "venv")
                for p in relative.split("/"))):
        raise ValueError("hidden, dependency, and non-project paths are not allowed")
    return relative


class TaskOutcomeMixin:
    def _ensure_task_outcome_schema(self) -> None:
        self.conn.executescript("""
          CREATE TABLE IF NOT EXISTS task_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            retrieval_id INTEGER NOT NULL UNIQUE
              REFERENCES memory_usage_log(id) ON DELETE CASCADE,
            changed_files_json TEXT NOT NULL,
            test_status TEXT NOT NULL,
            test_command_digest TEXT,
            test_commit_sha TEXT,
            recorded_at TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS task_outcome_candidates (
            outcome_id INTEGER NOT NULL REFERENCES task_outcomes(id) ON DELETE CASCADE,
            fact_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
            file_path TEXT NOT NULL,
            PRIMARY KEY (outcome_id, fact_id)
          );
          CREATE INDEX IF NOT EXISTS idx_task_outcomes_at ON task_outcomes(recorded_at);
        """)

    def record_task_outcome(
        self, retrieval_id: int, changed_files: list[str],
        test_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a task observation and review candidates; never train from it."""
        if type(retrieval_id) is not int or retrieval_id < 1:
            raise ValueError("retrieval_id must be a positive integer")
        if not isinstance(changed_files, list) or len(changed_files) > _MAX_FILES:
            raise ValueError("changed_files must be an array of at most 20 paths")
        paths = list(dict.fromkeys(_relative_source_path(p) for p in changed_files))
        retrieval = self.conn.execute(
            "SELECT query_text, retrieved_fact_ids FROM memory_usage_log WHERE id=?",
            (retrieval_id,),
        ).fetchone()
        if retrieval is None or looks_like_secret(retrieval["query_text"]):
            raise ValueError("unknown retrieval or sensitive query")

        evidence = test_evidence or {}
        if not isinstance(evidence, dict):
            raise ValueError("test_evidence must be an object")
        test_status = "no_test_report"
        command_digest = None
        matched_commit = None
        if evidence:
            command = evidence.get("command")
            exit_code = evidence.get("exit_code")
            commit_sha = evidence.get("commit_sha")
            if (not isinstance(command, str) or not command.strip()
                    or len(command) > 256 or looks_like_secret(command)
                    or type(exit_code) is not int or abs(exit_code) > 32767):
                raise ValueError("invalid test command or exit code")
            command_digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]
            current_head = self._current_git_commit()
            if (isinstance(commit_sha, str) and _SHA.fullmatch(commit_sha)
                    and current_head and commit_sha.lower() == current_head.lower()):
                matched_commit = commit_sha.lower()
                test_status = "reported_pass_at_head" if exit_code == 0 else "reported_failure_at_head"
            else:
                test_status = "reported_unmatched_revision"

        ids = [int(x) for x in (retrieval["retrieved_fact_ids"] or "").split(",")
               if x.isdigit()][:_MAX_FACTS]
        candidates = []
        if ids and paths:
            query = ",".join("?" for _ in ids)
            rows = self.conn.execute(
                f"SELECT id, file_path FROM memory WHERE id IN ({query}) AND deprecated_at IS NULL",
                tuple(ids),
            ).fetchall()
            for fact in rows:
                try:
                    anchor = _relative_source_path(fact["file_path"])
                except ValueError:
                    continue
                if anchor in paths:
                    candidates.append((int(fact["id"]), anchor))
            candidates.sort()

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        files_json = json.dumps(paths, ensure_ascii=False)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """INSERT INTO task_outcomes
                   (retrieval_id, changed_files_json, test_status,
                    test_command_digest, test_commit_sha, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(retrieval_id) DO UPDATE SET
                     changed_files_json=excluded.changed_files_json,
                     test_status=excluded.test_status,
                     test_command_digest=excluded.test_command_digest,
                     test_commit_sha=excluded.test_commit_sha,
                     recorded_at=excluded.recorded_at""",
                (retrieval_id, files_json, test_status, command_digest, matched_commit, now),
            )
            row = self.conn.execute(
                "SELECT id FROM task_outcomes WHERE retrieval_id=?", (retrieval_id,)
            ).fetchone()
            outcome_id = row["id"]
            self.conn.execute(
                "DELETE FROM task_outcome_candidates WHERE outcome_id=?", (outcome_id,)
            )
            for fact_id, anchor in candidates:
                self.conn.execute(
                    "INSERT INTO task_outcome_candidates (outcome_id, fact_id, file_path) VALUES (?, ?, ?)",
                    (outcome_id, fact_id, anchor),
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "outcome_id": outcome_id,
            "retrieval_id": retrieval_id,
            "changed_files": paths,
            "test_status": test_status,
            "review_candidate_fact_ids": [fid for fid, _ in candidates],
            "ranking_updated": False,
            "note": "Files and test metadata are caller-reported. The agent must seek fact-specific evidence or user confirmation before rating memory.",
        }

    def task_outcome_review_queue(self, limit: int = 10) -> dict[str, Any]:
        """Return recent candidate memories still awaiting attributable feedback."""
        if type(limit) is not int:
            raise ValueError("limit must be an integer")
        limit = max(1, min(limit, 30))
        outcomes = self.conn.execute(
            """SELECT id, retrieval_id, changed_files_json, test_status, recorded_at
               FROM task_outcomes ORDER BY id DESC LIMIT ?""", (limit,),
        ).fetchall()
        result = []
        for out in outcomes:
            rows = self.conn.execute(
                """SELECT c.fact_id, c.file_path, f.rating, f.evidence_type
                   FROM task_outcome_candidates c
                   LEFT JOIN memory_relevance_feedback f
                      ON f.retrieval_id=? AND f.fact_id=c.fact_id
                   JOIN memory m ON m.id=c.fact_id AND m.deprecated_at IS NULL
                   WHERE c.outcome_id=? ORDER BY c.fact_id""",
                (out["retrieval_id"], out["id"]),
            ).fetchall()
            candidates = [
                {
                    "fact_id": r["fact_id"],
                    "file_path": r["file_path"],
                    "review_status": (
                        "rated" if r["evidence_type"] in ("user", "test")
                        else "pending"),
                    "rating": r["rating"] if r["evidence_type"] in ("user", "test") else None,
                }
                for r in rows
            ]
            result.append({
                "outcome_id": out["id"],
                "retrieval_id": out["retrieval_id"],
                "changed_files": json.loads(out["changed_files_json"]),
                "test_status": out["test_status"],
                "recorded_at": out["recorded_at"],
                "candidates": candidates,
            })
        return {
            "count": len(result),
            "outcomes": result,
            "ranking_updated": False,
            "note": "No automatic positive rating is generated from test pass/failure or file edits.",
        }
