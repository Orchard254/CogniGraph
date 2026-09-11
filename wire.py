"""
Agent wiring module for CogniGraph.

Generates the correct MCP server configuration for a given coding agent,
pinned to the target project's own .cognigraph/memory.db (legacy
.project-memory/ migrated on first use), and drops the
AGENTS.md protocol file. Existing agent configs are merged (never clobbered)
and backed up first.

Usage:
    python memory_cli.py wire cursor --root C:/path/to/project
"""
import os
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

SUPPORTED_AGENTS = (
    "antigravity",
    "cursor",
    "windsurf",
    "claude",
    "zed",
    "roo",
    "cline",
    "opencode",
    "kiro",
    "vscode",
    "zcode",
    "generic",
)
AGENT_ALIASES = {
    "gemini": "antigravity",
    "vs-code": "vscode",
    "roo-code": "roo",
    "roocode": "roo",
}
AGENT_DISPLAY_NAMES = {
    "antigravity": "Google Antigravity (.agents/mcp_config.json)",
    "cursor": "Cursor (.cursor/mcp.json)",
    "windsurf": "Windsurf (~/.codeium/windsurf/mcp_config.json)",
    "claude": "Claude Desktop (global config)",
    "zed": "Zed (.zed/settings.json)",
    "roo": "Roo Code (.roo/mcp.json)",
    "cline": "Cline (.cline/mcp.json)",
    "opencode": "OpenCode (opencode.json)",
    "kiro": "Kiro (.kiro/settings/mcp.json)",
    "vscode": "VS Code (.vscode/mcp.json)",
    "zcode": "ZCode (.zcode/config.json)",
    "generic": "Generic (display JSON snippet only)",
}
ENTRY_NAME = "cognigraph"
# Phase 1.9 rebrand: entries matching any of these are legacy orphans and get
# replaced (not duplicated) when merging existing IDE configs.
LEGACY_ENTRY_NAMES = ("project-memory",)
LEGACY_NPX_PACKAGES = ("@project-memory/mcp-server",)
LEGACY_DATA_DIR_FRAGMENT = ".project-memory"


def _python_exe(pm_root: Path) -> str:
    pm_root = pm_root.resolve()
    for candidate in (pm_root / "venv" / "Scripts" / "python.exe",
                      pm_root / "venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate.resolve()).replace("\\", "/")
    # No venv yet: prefer a version-checked interpreter over bare "python".
    for cmd in ("python3", "python"):
        try:
            out = subprocess.check_output([cmd, "--version"], text=True, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL, timeout=15)
            import re
            m = re.search(r"Python\s+(\d+)\.(\d+)", out or "")
            if m and (int(m.group(1)) > 3 or (int(m.group(1)) == 3 and int(m.group(2)) >= 8)):
                return cmd
        except Exception:
            continue
    return sys.executable.replace("\\", "/")


def _backup(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    # Rotating backups: .bak, .bak.1, .bak.2 — never silently lose the original.
    candidates = [path.with_name(path.name + ".bak")] + [
        path.with_name(f"{path.name}.bak.{i}") for i in (1, 2)
    ]
    oldest = candidates[-1]
    if oldest.exists():
        oldest.unlink()
    for newer, older in zip(reversed(candidates[1:]), reversed(candidates[:-1])):
        if older.exists():
            shutil.move(str(older), str(newer))
    newest = candidates[0]
    shutil.copy2(path, newest)
    return str(newest)


def _is_legacy_entry(name: str, value: Any) -> bool:
    """True when a sibling MCP entry is a pre-rebrand CogniGraph orphan.

    Legacy signatures: the old server name, the old npx package command, or a
    MEMORY_DB_PATH still pointing inside legacy `.project-memory/`.
    """
    if name in LEGACY_ENTRY_NAMES and name != ENTRY_NAME:
        return True
    if not isinstance(value, dict):
        return False
    if value.get("command") == "npx":
        args = value.get("args") or []
        try:
            if any(any(pkg in str(a) for pkg in LEGACY_NPX_PACKAGES) for a in args):
                return True
        except Exception:
            pass
    try:
        env = value.get("env") or {}
        if LEGACY_DATA_DIR_FRAGMENT in str(env.get("MEMORY_DB_PATH") or ""):
            return True
    except Exception:
        pass
    return False


def _merge_json(path: Path, top_key: str, entry: Dict[str, Any]) -> None:
    """Merge one MCP entry into a JSON config file, preserving existing content.
    Removes deprecated keys (e.g., 'mcp' when writing 'mcpServers').

    top_key may be dotted for nested configs (e.g., ZCode's "mcp.servers"):
    intermediate objects are created as needed and the entry lands in the
    leaf mapping under ENTRY_NAME.

    A corrupt (unparseable) existing file is NEVER silently clobbered: it is
    preserved as <name>.corrupt-<timestamp>.bak and reported, then rebuilt.
    Writes are atomic (tmp + fsync + os.replace) so a mid-write crash cannot
    leave a half-written config.
    """
    data: Dict[str, Any] = {}
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            try:
                data = json.loads(raw)
            except Exception as e:
                corrupt_backup = path.with_name(
                    f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.bak"
                )
                try:
                    shutil.copy2(path, corrupt_backup)
                except Exception:
                    corrupt_backup = None
                raise ValueError(
                    f"existing config {path} is not valid JSON ({e}); "
                    f"preserved as {corrupt_backup}, refusing to overwrite"
                    if corrupt_backup is not None
                    else f"existing config {path} is not valid JSON ({e}); refusing to overwrite"
                )
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
    elif top_key == "servers":
        # VS Code uses `servers` (not `mcpServers`); drop the other shapes so
        # a stale key cannot linger and shadow the live entry.
        data.pop("mcpServers", None)
        data.pop("mcp", None)
        data.pop("context_servers", None)
    elif top_key == "mcp.servers":
        # ZCode native shape is {"mcp": {"servers": {...}}}; drop the
        # industry-standard compat key at top level so the two cannot disagree.
        data.pop("mcpServers", None)
        data.pop("context_servers", None)
        data.pop("servers", None)

    parts = top_key.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    leaf_key = parts[-1]
    node.setdefault(leaf_key, {})
    if not isinstance(node[leaf_key], dict):
        node[leaf_key] = {}
    # CogniGraph rebrand: replace legacy orphans instead of leaving duplicates.
    for legacy_key in LEGACY_ENTRY_NAMES:
        if legacy_key != ENTRY_NAME:
            node[leaf_key].pop(legacy_key, None)
    for name in [n for n, v in node[leaf_key].items()
                 if n != ENTRY_NAME and _is_legacy_entry(n, v)]:
        del node[leaf_key][name]
    node[leaf_key][ENTRY_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass
        raise


def wire_agent(agent: str, project_root: Optional[str] = None,
               project_memory_root: Optional[str] = None,
               drop_protocol: bool = True,
               global_config: bool = False) -> Dict[str, Any]:
    """
    Wire a target project to CogniGraph for the given coding agent.
    Returns a report with config file paths, backups, and exact next-step commands.

    global_config only affects agents with a global config location
    (currently antigravity): when False (default) only the project-local
    workspace file is written so one project's DB path cannot bleed into
    other projects; pass True to also update the user-global file.
    """
    if not isinstance(agent, str):
        raise ValueError(f"agent must be one of {SUPPORTED_AGENTS}, got {type(agent).__name__}")
    agent = agent.lower().strip()
    agent = AGENT_ALIASES.get(agent, agent)
    if agent not in SUPPORTED_AGENTS:
        raise ValueError(f"agent must be one of {SUPPORTED_AGENTS}, got {agent!r}")

    pm_root = Path(project_memory_root or Path(__file__).resolve().parent)
    proj_root = Path(project_root or Path.cwd()).resolve()
    if not proj_root.is_dir():
        raise ValueError(f"project root does not exist: {proj_root}")

    db_dir = None
    try:
        from pm_common import maybe_migrate_legacy_data_dir
        db_dir = maybe_migrate_legacy_data_dir(proj_root)
    except Exception:
        from pm_common import DATA_DIR_NAME
        db_dir = proj_root / DATA_DIR_NAME
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(db_dir / "memory.db").replace("\\", "/")

    py = _python_exe(pm_root)
    server = str((pm_root / "server.py").resolve()).replace("\\", "/")
    cli = str((pm_root / "memory_cli.py").resolve()).replace("\\", "/")
    env = {"MEMORY_DB_PATH": db_path}

    written: List[str] = []
    backups: List[str] = []
    config_errors: List[str] = []
    warnings: List[str] = []

    def _safe_merge(cfg: Path, top_key: str, entry: Dict[str, Any]) -> None:
        # A corrupt/unwritable config must surface as a report entry, never
        # as a traceback: the refusal backup already preserved the evidence.
        try:
            b = _backup(cfg)
            if b:
                backups.append(b)
            _merge_json(cfg, top_key, entry)
            written.append(str(cfg))
        except Exception as e:
            config_errors.append(f"{cfg}: {e}")

    if agent == "opencode":
        # OpenCode checks both ./opencode.json and ./.opencode/opencode.json;
        # write both so either lookup finds the wiring. Schema unchanged.
        oc_entry = {"type": "local", "command": [py, server],
                    "environment": env, "enabled": True}
        cfg = proj_root / "opencode.json"
        _safe_merge(cfg, "mcp", oc_entry)
        alias = proj_root / ".opencode" / "opencode.json"
        _safe_merge(alias, "mcp", oc_entry)
    elif agent == "cursor":
        cfg = proj_root / ".cursor" / "mcp.json"
        _safe_merge(cfg, "mcpServers", {"command": py, "args": [server], "env": env})
    elif agent == "antigravity":
        # Primary: workspace .agents/mcp_config.json (project-local, STATIC).
        # The old global-only write (~/.gemini/config/mcp_config.json) meant
        # the last-wired project's DB path bled into every other workspace,
        # so global is now opt-in via global_config=True, never the default.
        ws_cfg = proj_root / ".agents" / "mcp_config.json"
        _safe_merge(ws_cfg, "mcpServers", {"command": py, "args": [server], "env": env})
        if global_config:
            gbl_cfg = Path.home() / ".gemini" / "config" / "mcp_config.json"
            _safe_merge(gbl_cfg, "mcpServers",
                        {"command": py, "args": [server], "env": env})
    elif agent == "windsurf":
        cfg = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
        _safe_merge(cfg, "mcpServers", {"command": py, "args": [server], "env": env})
    elif agent == "roo":
        cfg = proj_root / ".roo" / "mcp.json"
        _safe_merge(cfg, "mcpServers", {"command": py, "args": [server], "env": env})
    elif agent == "cline":
        cfg = proj_root / ".cline" / "mcp.json"
        _safe_merge(cfg, "mcpServers", {"command": py, "args": [server], "env": env})
    elif agent == "kiro":
        # Kiro: workspace .kiro/settings/mcp.json (project-local, STATIC bake).
        # Global ~/.kiro/settings/mcp.json is intentionally NOT written: it
        # would bleed one project's DB path into every other workspace.
        # Kiro supports ${VAR} env expansion but no ${workspaceFolder}, so a
        # resolved absolute MEMORY_DB_PATH is the only reliable wiring.
        cfg = proj_root / ".kiro" / "settings" / "mcp.json"
        _safe_merge(cfg, "mcpServers", {"command": py, "args": [server], "env": env})
    elif agent == "vscode":
        # VS Code: DYNAMIC workspace variable (the only IDE in scope with a
        # confirmed ${workspaceFolder} expansion for command/args/env).
        # .vscode/mcp.json uses the `servers` key with a required per-entry
        # `type: "stdio"` — NOT `mcpServers`.
        dyn_env = {"MEMORY_DB_PATH": "${workspaceFolder}/.cognigraph/memory.db"}
        cfg = proj_root / ".vscode" / "mcp.json"
        _safe_merge(cfg, "servers", {"type": "stdio", "command": py,
                                     "args": [server], "cwd": "${workspaceFolder}",
                                     "env": dyn_env})
        # Portable fallback: VS Code's Agent Host does not read
        # .vscode/mcp.json directly (it receives a forwarded copy, and
        # ${input:...} servers are excluded). A root-level .mcp.json with the
        # STATIC resolved absolute path is read natively by Agent Host /
        # Copilot tooling (~/.copilot/mcp-config.json user equivalent), so
        # both files are written: DYNAMIC for the editor, STATIC for portability.
        portable = proj_root / ".mcp.json"
        _safe_merge(portable, "mcpServers",
                    {"command": py, "args": [server], "env": env})
    elif agent == "zcode":
        # ZCode: workspace .zcode/config.json with the native
        # {"mcp": {"servers": {...}}} shape (STATIC bake). Deliberately NOT
        # .agents/mcp.json: ZCode skips that fallback entirely once a .zcode
        # config exists in the same scope, so writing there would go dead.
        # CRITICAL load rule: on a same-name conflict the USER scope always
        # wins over the workspace, so a user-global `cognigraph` entry would
        # silently shadow this per-project wiring. Check first and warn loud.
        try:
            user_cfg = Path.home() / ".zcode" / "cli" / "config.json"
            if user_cfg.is_file():
                user_data = json.loads(user_cfg.read_text(encoding="utf-8"))
                if (isinstance(user_data, dict)
                        and isinstance(user_data.get("mcp"), dict)
                        and isinstance(user_data["mcp"].get("servers"), dict)
                        and ENTRY_NAME in user_data["mcp"]["servers"]):
                    warnings.append(
                        f"SHADOWING RISK: user-global {user_cfg} already defines "
                        f"'mcp.servers.{ENTRY_NAME}' — ZCode always prefers the "
                        "user scope over the workspace on same-name conflicts, "
                        "so the workspace entry below will be IGNORED until the "
                        "user-scope entry is renamed or removed. Per-project DB "
                        "isolation will NOT work until then.")
        except Exception as e:
            warnings.append(f"Could not inspect user-scope ZCode config: {e}")
        cfg = proj_root / ".zcode" / "config.json"
        _safe_merge(cfg, "mcp.servers",
                    {"command": py, "args": [server], "env": env})
    elif agent == "claude":
        appdata = os.environ.get("APPDATA")
        if appdata:
            cfg = Path(appdata) / "Claude" / "claude_desktop_config.json"
        elif sys.platform == "darwin":
            cfg = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        else:
            cfg = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
        _safe_merge(cfg, "mcpServers", {"command": py, "args": [server], "env": env})
    elif agent == "zed":
        cfg = proj_root / ".zed" / "settings.json"
        # Zed custom servers take env vars TOP-LEVEL ("env": {...}); `settings`
        # holds server init options, not process env. Verified against
        # https://zed.dev/docs/context-servers (2026-09-09).
        _safe_merge(cfg, "context_servers", {"command": py, "args": [server], "env": env})
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

    if agent == "opencode":
        snippet = {
            "mcp": {
                ENTRY_NAME: {"type": "local", "command": [py, server],
                             "environment": env, "enabled": True}
            }
        }
    elif agent == "vscode":
        snippet = {
            "servers": {
                ENTRY_NAME: {"type": "stdio", "command": py, "args": [server],
                             "cwd": "${workspaceFolder}",
                             "env": {"MEMORY_DB_PATH":
                                     "${workspaceFolder}/.cognigraph/memory.db"}}
            }
        }
    elif agent == "zcode":
        snippet = {
            "mcp": {
                "servers": {
                    ENTRY_NAME: {"command": py, "args": [server], "env": env}
                }
            }
        }
    else:
        snippet = {
            "mcpServers": {
                ENTRY_NAME: {"command": py, "args": [server], "env": env}
            }
        }
    next_steps = [
        f'"{py}" "{cli}" bootstrap --root "{proj_root}"   # dry-run preview',
        f'"{py}" "{cli}" bootstrap --root "{proj_root}" --apply   # ingest into memory',
        "Restart your coding agent so it loads the new MCP configuration.",
    ] + ([f"Fix config errors first: {e}" for e in config_errors] if config_errors else [])
    if agent == "zed":
        # User-facing (not just a code comment): project-scope
        # context_servers triggers a one-time worktree-trust prompt on first
        # open. Accept it — the wiring itself is complete at this point.
        next_steps.append(
            "Note: Zed will show a one-time trust confirmation for this "
            "project's context_servers on first open — this is expected, "
            "not a wire failure.")
        # User-facing: .zed/settings.json is gitignored (absolute local
        # paths) yet shares one file with general editor settings — share
        # those separately if the team wants them tracked.
        next_steps.append(
            "Note: .zed/settings.json is gitignored because the merged "
            "context_servers entry contains absolute local paths — general "
            "editor settings in the same file will not be committed, so "
            "share them separately (e.g. a .example/template file) if the "
            "team wants them tracked.")
    if agent == "zcode":
        # User-facing: .zcode/config.json is gitignored (absolute local
        # paths) yet shares one file with general agent settings — share
        # those separately if the team wants them tracked.
        next_steps.append(
            "Note: .zcode/config.json is gitignored because the merged "
            "mcp.servers entry contains absolute local paths — general "
            "agent settings in the same file will not be committed, so "
            "share them separately (e.g. a .example/template file) if the "
            "team wants them tracked.")
    if warnings:
        # Warnings are impossible-to-miss: first in next_steps (the printed
        # user output) as well as a dedicated report field.
        next_steps = [f"WARNING: {w}" for w in warnings] + next_steps
    return {
        "status": "WIRED_WITH_ERRORS" if config_errors else "WIRED",
        "agent": agent,
        "project_root": str(proj_root),
        "db_path": db_path,
        "config_files": written,
        "backups": backups,
        "config_errors": config_errors,
        "warnings": warnings,
        "agents_md": agents_md_status,
        "mcp_snippet": snippet,
        "next_steps": next_steps,
    }


def detect_agent(project_root: Optional[Path] = None,
                 env: Optional[Dict[str, str]] = None,
                 home: Optional[Path] = None) -> Optional[str]:
    """Auto-detect the developer's IDE / coding agent from environment and filesystem."""
    proj = Path(project_root or Path.cwd()).resolve()
    environ = os.environ if env is None else env
    user_home = Path.home() if home is None else home

    # 1. Project-level indicators (highest specificity for target repo)
    if (proj / ".cursor").is_dir():
        return "cursor"
    if (proj / ".roo").is_dir():
        return "roo"
    if (proj / ".cline").is_dir():
        return "cline"
    if (proj / ".zed").is_dir():
        return "zed"
    if (proj / ".kiro").is_dir():
        return "kiro"
    if (proj / ".vscode").is_dir():
        return "vscode"
    if (proj / ".zcode").is_dir():
        return "zcode"
    if (proj / "opencode.json").is_file():
        return "opencode"
    if (proj / ".opencode" / "opencode.json").is_file():
        return "opencode"
    if (proj / ".agents").is_dir():
        return "antigravity"

    # 2. Active environment variables
    if any(k in environ for k in ("ANTIGRAVITY_PID", "GEMINI_CLI", "ANTIGRAVITY_AGENT")):
        return "antigravity"
    if any(k in environ for k in ("CURSOR_PROJECT_DIR", "CURSOR_AGENT", "CURSOR_TRACE_ID")):
        return "cursor"
    if any(k in environ for k in ("WINDSURF_PID", "CODEIUM_API_KEY", "WINDSURF_AGENT")):
        return "windsurf"
    if any(k in environ for k in ("ZED_TERM", "ZED_SERVER_URL")):
        return "zed"
    if any(k in environ for k in ("CLAUDE_PROJECT_DIR", "CLAUDE_DESKTOP")):
        return "claude"
    if any(k in environ for k in ("OPENCODE", "OPENCODE_PID")):
        return "opencode"

    # 3. System / Home directory installations (lower confidence fallback)
    if (user_home / ".gemini").is_dir():
        return "antigravity"
    if (user_home / ".codeium" / "windsurf").is_dir():
        return "windsurf"

    return None


def prompt_agent_choice(detected: Optional[str] = None,
                        input_fn=input,
                        print_fn=print) -> str:
    """Prompt the user interactively to select their IDE / coding agent."""
    options = list(AGENT_DISPLAY_NAMES.keys())
    print_fn("\nSelect your IDE / Coding Agent for MCP server setup:\n")
    default_idx = None
    for idx, key in enumerate(options, 1):
        label = AGENT_DISPLAY_NAMES[key]
        is_default = (key == detected)
        tag = " (detected)" if is_default else ""
        if is_default:
            default_idx = idx
        print_fn(f"  [{idx}] {label}{tag}")

    prompt = f"\nChoice [1-{len(options)}]"
    if default_idx:
        prompt += f" (default: [{default_idx}] {detected}): "
    else:
        prompt += ": "

    while True:
        try:
            choice = input_fn(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print_fn()
            raise SystemExit(1)
        if not choice and default_idx:
            return options[default_idx - 1]
        if choice.isdigit():
            val = int(choice)
            if 1 <= val <= len(options):
                return options[val - 1]
        choice_lower = choice.lower()
        choice_lower = AGENT_ALIASES.get(choice_lower, choice_lower)
        if choice_lower in options:
            return choice_lower
        print_fn(f"Please enter a number between 1 and {len(options)}, or an agent name.")
