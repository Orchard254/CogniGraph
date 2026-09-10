#!/usr/bin/env python
"""Regenerate the MCP tool table in README.md from the live server registry.

Introspects server.mcp._tool_manager._tools for the canonical tool list so
the table cannot drift from what the server actually registers.

Usage:
    python scripts/gen_tool_table.py

Idempotent: a second run produces no diff. CI enforces freshness by running
this script and failing when README.md changes.
"""

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402 -- import is verified side-effect free

START_MARKER = "<!-- tool-table:start auto-generated -->"
END_MARKER = "<!-- tool-table:end -->"
HEADING = "## 3. Exposed MCP Tools"


def _first_line(text):
    """First non-empty line of a tool docstring, escaped for a table cell."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line.replace("|", "\\|")
    return ""


def _format_params(fn):
    sig = inspect.signature(fn)
    parts = []
    for name, param in sig.parameters.items():
        optional = param.default is not inspect.Parameter.empty
        parts.append(f"`{name}{'?' if optional else ''}`")
    return ", ".join(parts) if parts else "—"


def build_block():
    tools = server.mcp._tool_manager._tools
    rows = ["| Tool | Parameters | Description |", "|---|---|---|"]
    for name, tool in tools.items():
        rows.append(
            f"| `{name}` | {_format_params(tool.fn)} | {_first_line(tool.description)} |"
        )
    block = "\n".join(
        [
            START_MARKER,
            f"The server registers {len(tools)} tools.",
            "",
            *rows,
            END_MARKER,
        ]
    )
    return len(tools), block


def main():
    readme_path = ROOT / "README.md"
    raw = readme_path.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    readme = raw.replace("\r\n", "\n")

    count, block = build_block()

    start = readme.find(HEADING)
    if start == -1:
        sys.exit(f"error: heading '{HEADING}' not found in README.md")
    # The section body runs from the heading to the next '---' divider.
    divider = readme.find("\n---\n", start)
    if divider == -1:
        sys.exit("error: section divider after the tool table not found")

    updated = readme[:start] + HEADING + "\n\n" + block + "\n" + readme[divider:]

    if updated == readme:
        print(f"README.md already up to date ({count} tools)")
        return

    if crlf:
        updated = updated.replace("\n", "\r\n")
    readme_path.write_bytes(updated.encode("utf-8"))
    print(f"README.md updated ({count} tools)")


if __name__ == "__main__":
    main()