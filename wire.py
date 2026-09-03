"""
Agent wiring module for project-memory.

Generates the correct MCP server configuration for a given coding agent,
pinned to the target project's own .project-memory/memory.db, and drops the
AGENTS.md protocol file. Existing agent configs are merged (never clobbered)
and backed up first.

Usage:
    python memory_cli.py wire cursor --root C:\path\to\project
"""
import os
import json
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional

SUPPORTED_AGENTS = ("opencode", "cursor", "claude", "zed", "generic")
ENTRY_NAME = "project-memory"


def _python_exe(pm_root: Path) -> str:
    pm_root = pm_root.resolve()
    for candidate in (pm_root / "venv" / "Scripts" / "python.exe",
                      pm_root / "venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate.resolve()).replace("\\", "/")
    return "python"


def _backup(path: Path) -> Optional[str]:
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
        return str(backup)
    return None


def _merge_json(path: Path, top_key: str, entry: Dict[str, Any]) -> None:
    """Merge one MCP entry into a JSON config file, preserving existing content.
    Removes deprecated keys (e.g., 'mcp' when writing 'mcpServers')."""
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    
    # Remove deprecated keys for this agent
    if top_key == "mcpServers":
        data.pop("mcp", None)
    elif top_key == "mcp":
        data.pop("mcpServers", None)
    elif top_key == "context_servers":
        data.pop("mcpServers", None)
        data.pop("mcp", None)
    
    data.setdefault(top_key, {})
    if not isinstance(data[top_key], dict):
        data[top_key] = {}
    data[top_key][ENTRY_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def wire_agent(agent: str, project_root: Optional[str] = None,
               project_memory_root: Optional[str] = None,
               drop_protocol: bool = True) -> Dict[str, Any]:
    """
    Wire a target project to project-memory for the given coding agent.
    Returns a report with config file paths, backups, and exact next-step commands.
    """
    agent = agent.lower().strip()
    if agent not in SUPPORTED_AGENTS:
        raise ValueError(f"agent must be one of {SUPPORTED_AGENTS}, got {agent!r}")

    pm_root = Path(project_memory_root or Path(__file__).resolve().parent)
    proj_root = Path(project_root or Path.cwd()).resolve()
    if not proj_root.is_dir():
        raise ValueError(f"project root does not exist: {proj_root}")

    db_dir = proj_root / ".project-memory"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(db_dir / "memory.db").replace("\\", "/")

    py = _python_exe(pm_root)
    server = str((pm_root / "server.py").resolve()).replace("\\", "/")
    cli = str((pm_root / "memory_cli.py").resolve()).replace("\\", "/")
    env = {"MEMORY_DB_PATH": db_path}

    written: List[str] = []
    backups: List[str] = []

    if agent == "opencode":
        cfg = proj_root / "opencode.json"
        b = _backup(cfg)
        if b: backups.append(b)
        _merge_json(cfg, "mcpServers", {"command": py, "args": [server], "env": env})
        written.append(str(cfg))
    elif agent == "cursor":
        cfg = proj_root / ".cursor" / "mcp.json"
        b = _backup(cfg)
        if b: backups.append(b)
        _merge_json(cfg, "mcpServers", {"command": py, "args": [server], "env": env})
        written.append(str(cfg))
    elif agent == "claude":
        appdata = os.environ.get("APPDATA")
        if appdata:
            cfg = Path(appdata) / "Claude" / "claude_desktop_config.json"
        else:
            cfg = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
        b = _backup(cfg)
        if b: backups.append(b)
        _merge_json(cfg, "mcpServers", {"command": py, "args": [server], "env": env})
        written.append(str(cfg))
    elif agent == "zed":
        cfg = proj_root / ".zed" / "settings.json"
        b = _backup(cfg)
        if b: backups.append(b)
        _merge_json(cfg, "context_servers", {"command": py, "args": [server], "settings": {"env": env}})
        written.append(str(cfg))
    else:  # generic — print standard MCP snippet, write nothing
        written.append("(none — paste the snippet below into your agent's MCP config)")

    agents_md_status = "skipped_disabled"
    if drop_protocol:
        target = proj_root / "AGENTS.md"
        if target.exists():
            agents_md_status = "skipped_exists"
        else:
            src = pm_root / "AGENTS.md"
            if src.exists():
                target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                agents_md_status = "created"
            else:
                agents_md_status = "skipped_no_template"

    snippet = {
        "mcpServers": {
            ENTRY_NAME: {"command": py, "args": [server], "env": env}
        }
    }
    return {
        "status": "WIRED",
        "agent": agent,
        "project_root": str(proj_root),
        "db_path": db_path,
        "config_files": written,
        "backups": backups,
        "agents_md": agents_md_status,
        "mcp_snippet": snippet,
        "next_steps": [
            f'{py} "{cli}" bootstrap --root "{proj_root}"   # dry-run preview',
            f'{py} "{cli}" bootstrap --root "{proj_root}" --apply   # ingest into memory',
            "Restart your coding agent so it loads the new MCP configuration.",
        ],
    }