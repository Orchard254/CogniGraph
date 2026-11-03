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


def get_plugin_context(project_root: str, query: str, max_chars: int = 4500) -> dict:
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Project root is not a directory")
    task = str(query or "").strip()[:MAX_QUERY_CHARS]
    if not task:
        return {"context_markdown": "", "status": "empty_query"}

    # Never search the CogniGraph installation folder or a parent project as a
    # fallback: the active workspace is the only valid memory source.
    canonical = root / ".cognigraph" / "memory.db"
    legacy = root / ".project-memory" / "memory.db"
    db = canonical if canonical.is_file() else legacy
    if not db.is_file():
        return {"context_markdown": "", "status": "no_project_memory"}

    from pm_common import looks_like_secret
    if looks_like_secret(task):
        # Automatic prompt handling must not persist credentials in usage logs.
        return {"context_markdown": "", "status": "sensitive_query"}

    from memory_store import MemoryStore

    cap = max(256, min(int(max_chars), MAX_CONTEXT_CHARS))
    store = MemoryStore(str(db))
    try:
        markdown = store.get_context(
            task, limit=5, max_chars=cap, agent_type="opencode"
        )
    finally:
        store.close()
    if "*No matching facts found in memory.*" in markdown:
        return {"context_markdown": "", "status": "no_matches"}
    return {"context_markdown": markdown[:cap], "status": "ok"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="IDE context retrieval via stdin JSON")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--max-chars", type=int, default=4500)
    args = parser.parse_args(argv)
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict) or not isinstance(request.get("query"), str):
            raise ValueError("Input must be a JSON object with a string query")
        response = get_plugin_context(args.project_root, request["query"], args.max_chars)
        sys.stdout.write(json.dumps(response) + "\n")
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, ImportError) as exc:
        # Errors go to stderr, not a model-visible result. Do not echo query.
        print(f"CogniGraph context unavailable: {type(exc).__name__}", file=sys.stderr)
        sys.stdout.write(json.dumps({"context_markdown": "", "status": "error"}) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
