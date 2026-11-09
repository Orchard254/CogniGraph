"""Persistent, project-scoped observations of coding-task activity.

This is intentionally a different truth domain from project memories:
completed IDE read/edit tool calls are observations, not semantic claims. No
LLM-produced text, command output, credentials, or source content is persisted.

One bounded SQLite event log per project survives plugin restarts and context
compaction. Task/session identities are stored as hashes. A source read remains
current only while its recorded digest matches; edits invalidate earlier reads.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

_EXTENSIONS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".cs",
    ".c", ".h", ".hpp", ".cpp", ".rb", ".php", ".vue", ".svelte", ".sql",
})
_EXCLUDED = frozenset({"node_modules", "vendor", "venv", "__pycache__"})
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_EVENTS_PER_TASK = 96
_MAX_EVENTS_PER_PROJECT = 4096


def safe_project_file(root: Path, raw: str) -> str | None:
    """Validate an existing source file, rejecting escapes and hidden dirs."""
    if (not isinstance(raw, str) or not raw.strip() or len(raw) > 512
            or any(ord(ch) < 32 or ch in "`<>" for ch in raw)):
        return None
    try:
        root = root.resolve()
        resolved = (root / raw).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        relative = resolved.relative_to(root)
        if relative.suffix.lower() not in _EXTENSIONS:
            return None
        if any(part.startswith(".") or part.lower() in _EXCLUDED
               for part in relative.parts):
            return None
        return relative.as_posix()
    except (OSError, RuntimeError, ValueError):
        return None


def _safe_stored_path(raw: str) -> bool:
    """Even local SQLite rows are untrusted input to model-visible context."""
    if (not isinstance(raw, str) or not raw or len(raw) > 512
            or any(ord(ch) < 32 or ch in "`<>" for ch in raw)):
        return False
    if "\\" in raw:
        return False
    parts = raw.split("/")
    return (not raw.startswith("/") and not any(
        part in ("", ".", "..") or part.startswith(".") or part.lower() in _EXCLUDED
        for part in parts
    ) and Path(raw).suffix.lower() in _EXTENSIONS)


def _digest_file(root: Path, relative: str) -> str | None:
    """Bound source hashing; never retain source bytes or read large files."""
    path = root / relative
    try:
        if path.stat().st_size > _MAX_SOURCE_BYTES:
            return None
        h = hashlib.sha256()
        with path.open("rb") as stream:
            total = 0
            for block in iter(lambda: stream.read(65536), b""):
                total += len(block)
                if total > _MAX_SOURCE_BYTES:
                    return None
                h.update(block)
        return h.hexdigest()
    except (OSError, RuntimeError, ValueError):
        return None


def _key(session_id: str, task: str) -> tuple[str, str]:
    if not isinstance(session_id, str) or not (1 <= len(session_id) <= 256):
        raise ValueError("invalid session identity")
    if not isinstance(task, str) or not (1 <= len(task.strip()) <= 4000):
        raise ValueError("invalid task identity")
    return (hashlib.sha256(session_id.encode()).hexdigest(),
            hashlib.sha256(task.strip().encode()).hexdigest())


def _database(root: Path) -> Path:
    directory = root / ".cognigraph"
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError("unsafe project data directory")
    return directory / "task_evidence.sqlite3"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=3.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def record_observation(
    root: Path, session_id: str, task: str, kind: str, file_path: str,
) -> dict[str, Any]:
    """Record a successful structured IDE read/edit without inferring truth.

    The caller attests successful tool completion. Path and source digest are
    validated independently. A tool event cannot prove how much was read.
    """
    if kind not in ("read", "edit"):
        raise ValueError("only structured read and edit events are accepted")
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("invalid project root")
    session_key, task_key = _key(session_id, task)
    path = safe_project_file(root, file_path)
    if path is None:
        return {"status": "invalid_project_file", "recorded": False}
    digest = _digest_file(root, path) if kind == "read" else None
    if kind == "read" and digest is None:
        return {"status": "source_unavailable", "recorded": False}

    db_path = _database(root)
    db_path.parent.mkdir(exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_evidence_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                task_key TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('read','edit')),
                file_path TEXT NOT NULL,
                source_hash TEXT,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS task_evidence_recent
                ON task_evidence_events(session_key,task_key,id DESC);
        """)
        with conn:
            conn.execute(
                "INSERT INTO task_evidence_events "
                "(session_key,task_key,kind,file_path,source_hash) "
                "VALUES(?,?,?,?,?)",
                (session_key, task_key, kind, path, digest),
            )
            conn.execute(
                "DELETE FROM task_evidence_events WHERE session_key=? AND task_key=? "
                "AND id NOT IN (SELECT id FROM task_evidence_events "
                "WHERE session_key=? AND task_key=? ORDER BY id DESC LIMIT ?)",
                (session_key, task_key, session_key, task_key, _MAX_EVENTS_PER_TASK),
            )
            conn.execute(
                "DELETE FROM task_evidence_events WHERE id NOT IN "
                "(SELECT id FROM task_evidence_events ORDER BY id DESC LIMIT ?)",
                (_MAX_EVENTS_PER_PROJECT,),
            )
    finally:
        conn.close()
    return {"status": "recorded", "recorded": True, "kind": kind, "file_path": path}


def task_snapshot(root: Path, session_id: str, task: str) -> dict[str, Any]:
    """Reconstruct evidence from the latest event per file, read-only."""
    root = root.resolve()
    session_key, task_key = _key(session_id, task)
    empty: dict[str, Any] = {
        "observed_files": [], "edited_files": [], "stale_files": [],
        "event_count": 0, "verified": False,
    }
    path = _database(root)
    if not path.is_file():
        return empty
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT kind,file_path,source_hash FROM task_evidence_events "
            "WHERE session_key=? AND task_key=? ORDER BY id DESC LIMIT ?",
            (session_key, task_key, _MAX_EVENTS_PER_TASK),
        ).fetchall()
    except sqlite3.DatabaseError:
        return empty
    finally:
        conn.close()
    current = set()
    edited = set()
    stale = set()
    seen = set()
    for row in rows:
        candidate = row["file_path"]
        if not _safe_stored_path(candidate) or candidate in seen:
            continue
        seen.add(candidate)
        safe = safe_project_file(root, candidate)
        if not safe:
            stale.add(candidate)
        elif row["kind"] == "edit":
            edited.add(candidate)
        elif row["source_hash"] and _digest_file(root, safe) == row["source_hash"]:
            current.add(candidate)
        else:
            stale.add(candidate)
    return {
        "observed_files": sorted(current)[:20],
        "edited_files": sorted(edited)[:20],
        "stale_files": sorted(stale)[:20],
        "event_count": len(rows),
        "verified": False,
    }


def format_task_snapshot(snapshot: dict[str, Any], max_chars: int = 380) -> str:
    """Tiny, strictly observation-only context summary, no partial paths."""
    if not snapshot.get("event_count"):
        return ""
    lines = ["### Persistent task evidence (IDE observations, not proof)"]
    footer = "Observations do not establish code correctness or passing tests."
    if len(lines[0]) + len(footer) + 2 > max_chars:
        return ""
    for label, key in (
        ("Read and unchanged", "observed_files"),
        ("Edited; source re-read needed", "edited_files"),
        ("Changed/missing since read", "stale_files"),
    ):
        paths = snapshot.get(key) or []
        if paths:
            line = f"- {label}: " + ", ".join(chr(96) + p + chr(96) for p in paths[:3])
            if sum(len(x) + 1 for x in lines) + len(line) + len(footer) + 2 <= max_chars:
                lines.append(line)
    lines.append(footer)
    return "\n".join(lines) + "\n"
