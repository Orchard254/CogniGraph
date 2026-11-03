"""Bounded, project-scoped CogniGraph context bridge for IDE plugins.

Protocol: one JSON object on stdin with `query`; one JSON object on stdout.
No external model or MCP server is required. Retrieval usage counters may update.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_QUERY_CHARS = 4000
MAX_CONTEXT_CHARS = 6000


# Limit automatic file context to source files; never infer secrets from tool output.
_SOURCE_SUFFIXES = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".cs",
    ".c", ".h", ".hpp", ".cpp", ".rb", ".php", ".vue", ".svelte", ".sql",
})


def _safe_project_file(root: Path, candidate: str) -> str | None:
    """Return a relative project path, never a path outside this workspace."""
    if not isinstance(candidate, str) or not candidate or len(candidate) > 512:
        return None
    try:
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        relative = resolved.relative_to(root)
        if relative.suffix.lower() not in _SOURCE_SUFFIXES:
            return None
        if any(part.startswith(".") or part.lower() in ("node_modules", "vendor")
               for part in relative.parts):
            return None
        return relative.as_posix()
    except (ValueError, OSError, RuntimeError):
        return None


def get_plugin_context(
    project_root: str, query: str, max_chars: int = 4500,
    file_paths: list[str] | None = None,
) -> dict:
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Project root is not a directory")
    task = str(query or "").strip()[:MAX_QUERY_CHARS]
    if file_paths is not None and not isinstance(file_paths, list):
        raise ValueError("file_paths must be a list")
    requested_files = file_paths or []
    if not task and not requested_files:
        return {"context_markdown": "", "status": "empty_query"}

    # Never search an adjacent repository or the CogniGraph installation
    # directory as a fallback.
    canonical = root / ".cognigraph" / "memory.db"
    legacy = root / ".project-memory" / "memory.db"
    db = canonical if canonical.is_file() else legacy
    if not db.is_file():
        return {"context_markdown": "", "status": "no_project_memory"}

    from pm_common import looks_like_secret
    if task and looks_like_secret(task):
        # Automatic prompt handling must not persist credentials in usage logs.
        return {"context_markdown": "", "status": "sensitive_query"}

    from memory_store import MemoryStore

    cap = max(256, min(int(max_chars), MAX_CONTEXT_CHARS))
    store = MemoryStore(str(db))
    try:
        parts = []
        retrieval_id = None
        if task:
            markdown = store.get_context(
                task, limit=5, max_chars=cap, agent_type="opencode"
            )
            retrieval_id = getattr(store, "_last_retrieval_id", None)
            if "*No matching facts found in memory.*" not in markdown:
                parts.append(markdown[:cap])

        # File focus comes only from structured successful IDE tool calls,
        # not arbitrary text scraped from shell output. Re-validate here.
        visited = set()
        for candidate in reversed(requested_files[-2:]):
            relative = _safe_project_file(root, candidate)
            if not relative or relative in visited:
                continue
            visited.add(relative)
            brief = store.get_file_context(relative)
            if "UNMAPPED in memory" in brief:
                continue
            # Prioritize the most recently used file, fit within one cap.
            remaining = cap - sum(len(p) for p in parts) - len(parts) * 2
            if remaining < 160:
                break
            parts.append(brief[:remaining])

    finally:
        store.close()
    text = "\n\n".join(parts)[:cap]
    if not text:
        return {"context_markdown": "", "status": "no_matches"}
    return {"context_markdown": text, "status": "ok",
            "retrieval_id": retrieval_id}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="IDE context retrieval via stdin JSON")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--max-chars", type=int, default=4500)
    args = parser.parse_args(argv)
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict) or not isinstance(request.get("query"), str):
            raise ValueError("Input must be a JSON object with a string query")
        response = get_plugin_context(
            args.project_root, request["query"], args.max_chars,
            file_paths=request.get("file_paths"),
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
