#!/usr/bin/env python3
"""
Git post-commit hook for automated memory capture.
Extracts commit messages containing tags [decision], [constraint], [component], [arch], or [state]
and writes them directly into project memory.
"""
import sys
import re
import subprocess
from pathlib import Path

# Add parent directory to path so memory_store can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memory_store import MemoryStore


TAG_MAP = {
    "decision": "decision",
    "constraint": "constraint",
    "component": "component",
    "arch": "component",
    "state": "state",
}


def get_latest_commit_info():
    try:
        msg = subprocess.check_output(["git", "log", "-1", "--pretty=%B"], text=True, encoding="utf-8").strip()
        sha = subprocess.check_output(["git", "log", "-1", "--pretty=%H"], text=True, encoding="utf-8").strip()
        return msg, sha
    except Exception:
        return None, None


def main():
    msg, sha = get_latest_commit_info()
    if not msg:
        return

    # Check for [tag] pattern in commit message (non-greedy, single-line)
    match = re.search(r"\[(decision|constraint|component|arch|state)\]\s*([^\n]*)", msg, re.IGNORECASE)
    if not match:
        return

    raw_tag = match.group(1).lower()
    content = match.group(2).strip()
    category = TAG_MAP.get(raw_tag, "other")

    if not content:
        return

    store = MemoryStore()
    try:
        fact_id = store.write(
            category=category,
            content=f"Git commit ({sha[:7]}): {content}",
            rationale="Auto-captured via Git post-commit hook",
            confidence="confirmed",
            git_commit_sha=sha,
            tags=f"git, auto-capture, {raw_tag}",
        )
        print(f"[project-memory] Auto-captured Git commit fact #{fact_id} ({category})")
    finally:
        store.close()


if __name__ == "__main__":
    main()
