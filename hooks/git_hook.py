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

# Add parent directory to path so memory_store can be imported.
# NOTE: the import itself stays inside main()'s try/except — a hook must
# never fail a commit, whatever is broken (missing module, locked DB, ...).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
    try:
        from memory_store import MemoryStore
    except Exception as e:
        print(f"[project-memory] hook disabled (import failed): {e}", file=sys.stderr)
        return 0

    try:
        msg, sha = get_latest_commit_info()
    except Exception as e:
        print(f"[project-memory] hook skipped (git failed): {e}", file=sys.stderr)
        return 0
    if not msg:
        return 0

    # Capture every [tag] in the message, not just the first, so multi-tag
    # commits (e.g. "[decision] X [constraint] Y") keep all facts.
    matches = re.findall(
        r"\[(decision|constraint|component|arch|state)\]\s*([^\n\[]*)", msg, re.IGNORECASE
    )
    if not matches:
        return 0

    try:
        store = MemoryStore()
    except Exception as e:
        print(f"[project-memory] hook skipped (db unavailable): {e}", file=sys.stderr)
        return 0
    try:
        for raw_tag, content in matches:
            raw_tag = raw_tag.lower()
            content = content.strip()
            category = TAG_MAP.get(raw_tag, "other")
            if not content:
                continue
            try:
                fact_id = store.write(
                    category=category,
                    content=f"Git commit ({sha[:7]}): {content}" if sha else content,
                    rationale="Auto-captured via Git post-commit hook",
                    confidence="confirmed",
                    git_commit_sha=sha,
                    tags=f"git, auto-capture, {raw_tag}",
                )
                print(f"[project-memory] Auto-captured Git commit fact #{fact_id} ({category})")
            except Exception as e:
                # One bad tag must not drop the others — and never fail commit.
                print(f"[project-memory] hook skipped tag [{raw_tag}]: {e}", file=sys.stderr)
                continue
    finally:
        try:
            store.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    main()
