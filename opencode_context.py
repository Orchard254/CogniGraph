"""Bounded, project-scoped CogniGraph context bridge for IDE plugins.

Protocol: one JSON object on stdin with `query`; one JSON object on stdout.
No external model or MCP server is required. Retrieval usage counters may update.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from task_evidence import (
    safe_project_file as _safe_project_file,
    record_observation, task_snapshot, format_task_snapshot,
)

MAX_QUERY_CHARS = 4000
MAX_CONTEXT_CHARS = 6000


def _is_deliverable_briefing(briefing: dict) -> bool:
    """Allow explicit no-fact investigation guidance, but never safety rejections."""
    status = briefing.get("status")
    if status == "ok":
        return True
    if status != "no_new_context":
        return False
    return bool(briefing.get("inspection_targets") or briefing.get("knowledge_gaps"))


def get_plugin_context(
    project_root: str, query: str, max_chars: int = 4500,
    file_paths: list[str] | None = None,
    *, phase: str = "orient", refresh: bool = False,
    session_id: str | None = None,
) -> dict:
    """Compose automatic initial or IDE-focused context.

    Phase is derived from structured IDE observations, NOT an agent-reported
    plan. A refresh may re-check sources, but does not create a new receipt
    that could incorrectly attribute earlier edits to newly delivered facts.
    """
    if phase not in ("orient", "inspect", "verify"):
        raise ValueError("unsupported IDE context phase")
    if type(refresh) is not bool:
        raise ValueError("refresh must be a boolean")
    # Validate before conversion so malformed values fail fast as a clear
    # ValueError instead of an unhandled int() error.
    try:
        _max_chars = int(max_chars)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("max_chars must be an integer-like number")
    if not isinstance(max_chars, str) and max_chars != _max_chars:
        raise ValueError("max_chars must be an integer-like number")
    max_chars = _max_chars
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Project root is not a directory")
    task = str(query or "").strip()[:MAX_QUERY_CHARS]
    if file_paths is not None and not isinstance(file_paths, list):
        raise ValueError("file_paths must be a list")
    requested_files = file_paths or []
    if not task and not requested_files:
        return {"context_markdown": "", "status": "empty_query"}

    from pm_common import looks_like_secret
    if task and looks_like_secret(task):
        # No task evidence lookup/recording for potentially sensitive queries.
        return {"context_markdown": "", "status": "sensitive_query"}

    cap = max(256, min(max_chars, MAX_CONTEXT_CHARS))
    observation = None
    observation_text = ""
    if session_id is not None and task:
        observation = task_snapshot(root, session_id, task)
        observation_text = format_task_snapshot(observation, max_chars=min(380, cap // 4))
    content_cap = cap - len(observation_text) - 2 if observation_text else cap
    if content_cap < 512:
        observation_text = ""
        content_cap = cap

    # Never search an adjacent repository or the CogniGraph installation
    # directory as a fallback. Observations remain usable if the memory
    # corpus is not initialized; they do not create facts.
    canonical = root / ".cognigraph" / "memory.db"
    legacy = root / ".project-memory" / "memory.db"
    db = canonical if canonical.is_file() else legacy
    if not db.is_file():
        if observation_text:
            return {"context_markdown": observation_text, "status": "ok",
                    "retrieval_id": None, "task_evidence": observation}
        return {"context_markdown": "", "status": "no_project_memory"}

    from memory_store import MemoryStore
    store = MemoryStore(str(db))
    try:
        parts = []
        retrieval_id = None
        valid_focus = []
        for candidate in reversed(requested_files[-2:]):
            relative = _safe_project_file(root, candidate)
            if relative and relative not in valid_focus:
                valid_focus.append(relative)

        focused_mode = phase in ("inspect", "verify")
        if focused_mode and not valid_focus:
            return {"context_markdown": "", "status": "no_matches"}
        composer = getattr(store, "get_intelligent_context", None)

        if focused_mode and callable(composer) and content_cap >= 512:
            # Preserve room for the established structural file briefing.
            # The phase composer contributes only already-grounded facts and
            # inspection leads; it does not log another attribution receipt.
            attention_budget = min(900, max(512, content_cap - 500))
            briefing = composer(
                task or " ".join(valid_focus),
                phase=phase, focus_files=valid_focus,
                max_chars=attention_budget, limit=3,
                agent_type="opencode", log_usage=False,
                observed_files=(
                    [p for p in valid_focus if p in observation["observed_files"]]
                    if observation is not None else
                    (valid_focus if phase == "inspect" else None)
                ),
            )
            if _is_deliverable_briefing(briefing):
                parts.append(briefing["context_markdown"][:attention_budget])

        if task and not focused_mode:
            # Prefer the bounded Context Intelligence Layer for the initial
            # task briefing. Keep legacy compatibility for prior installations,
            # constrained budgets, and isolated bridge mocks.
            if callable(composer) and content_cap >= 512:
                briefing = composer(
                    task, phase="orient", max_chars=content_cap, limit=6,
                    agent_type="opencode", log_usage=not refresh,
                )
                if not refresh:
                    retrieval_id = briefing.get("retrieval_id")
                if _is_deliverable_briefing(briefing):
                    parts.append(briefing["context_markdown"][:cap])
            elif not refresh:
                # An older composer may not support read-only rechecks;
                # never create a second possibly misleading usage receipt.
                markdown = store.get_context(
                    task, limit=5, max_chars=content_cap, agent_type="opencode"
                )
                retrieval_id = getattr(store, "_last_retrieval_id", None)
                if "*No matching facts found in memory.*" not in markdown:
                    parts.append(markdown[:cap])

        # File focus comes only from structured successful IDE tool calls,
        # not arbitrary text scraped from shell output. Re-validate here.
        for relative in valid_focus:
            brief = store.get_file_context(relative)
            if "UNMAPPED in memory" in brief:
                continue
            # Prioritize the most recently used file, fit within one cap.
            remaining = content_cap - sum(len(p) for p in parts) - len(parts) * 2
            if remaining < 160:
                break
            parts.append(brief[:remaining])

    finally:
        store.close()
    if observation_text:
        parts.append(observation_text.rstrip())
    text = "\n\n".join(parts)[:cap]
    if not text:
        return {"context_markdown": "", "status": "no_matches"}
    response = {"context_markdown": text, "status": "ok",
                "retrieval_id": retrieval_id}
    if observation is not None:
        response["task_evidence"] = observation
    return response



def record_plugin_evidence(
    project_root: str, session_id: str, query: str,
    kind: str, file_path: str,
) -> dict:
    """Accept only observed structured read/edit events from the IDE bridge."""
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Project root is not a directory")
    from pm_common import looks_like_secret
    if looks_like_secret(query):
        return {"status": "sensitive_query", "recorded": False}
    return record_observation(root, session_id, query, kind, file_path)


def record_plugin_outcome(
    project_root: str, retrieval_id: int, changed_files: list[str],
) -> dict:
    """Record observed editor changes for later review; never train rankings.

    All paths must resolve to existing source files inside the active
    project. No source content or tool output is stored.
    """
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Project root is not a directory")
    if type(retrieval_id) is not int or retrieval_id <= 0:
        raise ValueError("retrieval_id must be a positive integer")
    if not isinstance(changed_files, list) or len(changed_files) > 20:
        raise ValueError("changed_files must be a list of at most 20 paths")
    valid = []
    for file in changed_files:
        relative = _safe_project_file(root, file)
        if relative and relative not in valid:
            valid.append(relative)
    if not valid:
        return {"status": "no_eligible_source_files", "ranking_updated": False}

    canonical = root / ".cognigraph" / "memory.db"
    legacy = root / ".project-memory" / "memory.db"
    db = canonical if canonical.is_file() else legacy
    if not db.is_file():
        return {"status": "no_project_memory", "ranking_updated": False}

    from memory_store import MemoryStore
    store = MemoryStore(str(db))
    try:
        report = store.record_task_outcome(retrieval_id, valid)
    finally:
        store.close()
    return {"status": "recorded", **report}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="IDE context retrieval via stdin JSON")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--max-chars", type=int, default=4500)
    parser.add_argument("--mode", choices=("context", "outcome", "evidence"), default="context")
    args = parser.parse_args(argv)
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("Input must be a JSON object")
        if args.mode == "evidence":
            response = record_plugin_evidence(
                args.project_root, request.get("session_id"), request.get("query"),
                request.get("kind"), request.get("file_path"),
            )
        elif args.mode == "outcome":
            response = record_plugin_outcome(
                args.project_root,
                request.get("retrieval_id"),
                request.get("changed_files"),
            )
        else:
            if not isinstance(request.get("query"), str):
                raise ValueError("Input must include a string query")
            response = get_plugin_context(
                args.project_root, request["query"], args.max_chars,
                file_paths=request.get("file_paths"),
                phase=request.get("phase", "orient"),
                refresh=request.get("refresh", False),
                session_id=request.get("session_id"),
            )
        sys.stdout.write(json.dumps(response) + "\n")
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, ImportError) as exc:
        # Errors go to stderr, not a model-visible result. Do not echo query.
        print(f"CogniGraph context unavailable: {type(exc).__name__}", file=sys.stderr)
        sys.stdout.write(json.dumps({"context_markdown": "", "status": "error"}) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
