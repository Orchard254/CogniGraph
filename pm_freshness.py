"""Read-only, bounded source-anchor freshness checks for injected memory.

A mismatch is a *review signal*, not proof the recorded knowledge is false.
Never modifies facts, confidence, line ranges or the source tree.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from pm_common import DATA_DIR_NAME, LEGACY_DATA_DIR_NAME

MAX_CHECK_BYTES = 2 * 1024 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def project_root_for_db(db_path: str | None) -> Path | None:
    """Trust only databases under the canonical or legacy project data dir."""
    if not db_path:
        return None
    try:
        db = Path(db_path).expanduser().resolve()
        if db.name != "memory.db" or db.parent.name not in (DATA_DIR_NAME, LEGACY_DATA_DIR_NAME):
            return None
        return db.parent.parent
    except (OSError, ValueError, RuntimeError):
        return None


def check_source_freshness(
    fact: dict[str, Any], project_root: Path, *,
    cache: dict[str, bytes | None] | None = None,
    max_bytes: int = MAX_CHECK_BYTES,
) -> dict[str, Any]:
    """Compare a stored source hash against the current workspace source.

    Status: CURRENT, RELOCATED, POSSIBLY_STALE, SOURCE_MISSING, or UNVERIFIED.
    This method does not silently repair, deprecate, or trust a drifted fact.
    """
    file_path = fact.get("file_path")
    code_hash = fact.get("code_hash")
    if not file_path or not code_hash:
        return {"status": "UNVERIFIED", "reason": "no_provenance_hash"}
    if not isinstance(file_path, str) or not isinstance(code_hash, str) or not _HASH_RE.fullmatch(code_hash):
        return {"status": "UNVERIFIED", "reason": "invalid_provenance"}
    try:
        normalized = file_path.replace("\\", "/")
        relative = Path(normalized)
        windows = PureWindowsPath(file_path)
        if (relative.is_absolute() or windows.is_absolute() or windows.drive
                or ".." in relative.parts or not relative.parts):
            return {"status": "UNVERIFIED", "reason": "outside_project"}
        root = Path(project_root).expanduser().resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            return {"status": "UNVERIFIED", "reason": "outside_project"}
        if not target.exists():
            return {"status": "SOURCE_MISSING"}
        if not target.is_file():
            return {"status": "UNVERIFIED", "reason": "not_a_file"}

        # Each source is read at most once per briefing. Oversized files
        # are skipped rather than hashing unbounded user/project data.
        key = str(target)
        if cache is not None and key in cache:
            content = cache[key]
        else:
            if target.stat().st_size > max_bytes:
                return {"status": "UNVERIFIED", "reason": "size_limit"}
            content = target.read_bytes()
            if len(content) > max_bytes:
                return {"status": "UNVERIFIED", "reason": "size_limit"}
            if cache is not None:
                cache[key] = content
        if content is None:
            return {"status": "UNVERIFIED", "reason": "size_limit"}

        start = fact.get("line_start")
        end = fact.get("line_end")
        if start is None and end is None:
            digest = hashlib.sha256(content).hexdigest()
            return {"status": "CURRENT" if digest == code_hash.lower() else "POSSIBLY_STALE"}

        if (type(start) is not int or type(end) is not int or start < 1
                or end < start or end - start > 1000):
            return {"status": "UNVERIFIED", "reason": "invalid_line_anchor"}
        try:
            lines = content.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            return {"status": "UNVERIFIED", "reason": "non_utf8_file"}
        if end <= len(lines):
            snippet = "".join(lines[start - 1:end])
            if hashlib.sha256(snippet.encode("utf-8")).hexdigest() == code_hash.lower():
                return {"status": "CURRENT"}
        # A nearby shift can preserve the exact source snippet while its line
        # numbers move. Do not update recorded provenance without review.
        span = end - start + 1
        left = max(0, start - 31)
        right = min(len(lines), end + 30)
        for pos in range(left, max(left, right - span + 1)):
            snippet = "".join(lines[pos:pos + span])
            if hashlib.sha256(snippet.encode("utf-8")).hexdigest() == code_hash.lower():
                return {"status": "RELOCATED", "current_lines": [pos + 1, pos + span]}
        return {"status": "POSSIBLY_STALE"}
    except (OSError, ValueError, RuntimeError):
        return {"status": "UNVERIFIED", "reason": "source_unavailable"}


def freshness_note(
    fact: dict[str, Any], db_path: str | None, *,
    cache: dict[str, bytes | None] | None = None,
) -> str:
    """A short, conservative warning for a model-visible briefing."""
    root = project_root_for_db(db_path)
    if root is None or not fact.get("file_path") or not fact.get("code_hash"):
        return ""
    result = check_source_freshness(fact, root, cache=cache)
    status = result["status"]
    if status == "POSSIBLY_STALE":
        return "⚠️ SOURCE MAY HAVE CHANGED; check current code before relying on this fact"
    if status == "SOURCE_MISSING":
        return "⚠️ SOURCE FILE MISSING; verify this fact before use"
    if status == "RELOCATED":
        return "source snippet moved; stored line numbers need review"
    if status == "UNVERIFIED":
        return "source provenance not verified (" + result.get("reason", "unknown") + ")"
    return ""
