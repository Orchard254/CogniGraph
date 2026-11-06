# cognigraph

CogniGraph: universal MCP server for persistent project memory across all AI coding IDEs.

## Native OpenCode V2 context delivery

The optional OpenCode V2 plugin injects relevant memory into outgoing model
requests automatically. MCP remains available for on-demand queries.
See [OPENCODE_PLUGIN.md](OPENCODE_PLUGIN.md). In the installed CogniGraph package,
run `python install_opencode.py --root /path/to/project` using the bundled
Python environment. Existing project memory must be initialized first.

## 🚀 Zero-Configuration Setup

The easiest way to add persistent memory to any AI coding agent:

```json
{
  "mcpServers": {
    "cognigraph": {
      "command": "npx",
      "args": ["-y", "cognigraph"]
    }
  }
}
```

That's it! Prerequisites below apply: Python 3.12+ on PATH and Node.js >= 18 (package.json engines). On first run the wrapper creates venv/, installs requirements.txt (only mcp is hard-required; sqlite-vec/tree-sitter*/fastembed/numpy lanes degrade with a warning), and launches server.py (index.js:98-132,171-232), guarded by a pre-spawn py_compile check and a 5 s startup watchdog (index.js:245-285). postinstall.js is best-effort and leaves a .postinstall-incomplete marker re-verified on next startup (index.js:175-183).

## Context intelligence

Use `memory_get_intelligent_context` for phase-aware, bounded coding
briefings with source-qualified fact references and explicit provenance
warnings. The legacy retrieval tools remain available. See
[Context Intelligence](docs/CONTEXT_INTELLIGENCE.md).

## Task outcome review

After a coding task, use `memory_record_task_outcome` with a retrieval receipt
and the reported changed files, then consult `memory_task_outcome_review_queue`
for fact-specific review candidates. This is a review workflow, not automatic
reinforcement. See [Task Outcomes](docs/TASK_OUTCOMES.md).

## Relevance learning

Context retrieval now returns a `retrieval_id`. Agents can record
attributable `helpful`, `irrelevant`, and `incorrect` memory outcomes with
`memory_rate_context`; only explicit user reports and metadata-checked test
evidence influence future ranking. See
[docs/FEEDBACK_LEARNING.md](docs/FEEDBACK_LEARNING.md).

## ✨ Features

- **Universal Compatibility**: Works with Trae, Windsurf, Cursor, Claude Desktop, Zed, Kiro, VS Code, ZCode, and any MCP-compatible IDE
- **Zero Configuration**: Auto-detects project structure and creates databases automatically
- **Auto-Bootstrap**: Intelligently learns your project on first use
- **Persistent Memory**: Maintains context across coding sessions
- **Smart Context Retrieval**: Hybrid search with graph-based dependency tracking
- **Self-Healing**: Automatic provenance checking and maintenance

## 🎯 How It Works

1. **First Use**: When an agent first calls any memory tool, the system:
   - Auto-detects your project root
   - Creates `.cognigraph/memory.db` if needed
   - Runs lightweight bootstrap (top 20 facts)
   - Learns project structure, dependencies, and key files

2. **Ongoing Use**: The system continuously:
   - Records architectural decisions and constraints
   - Tracks component relationships
   - Maintains code-anchored provenance
   - Provides context-aware suggestions

3. **Cross-Session**: All context persists between sessions, so agents remember what they learned about your project.

## 📋 IDE-Specific Setup

### Trae IDE
```json
{
  "mcpServers": {
    "cognigraph": {
      "command": "npx",
      "args": ["-y", "cognigraph"]
    }
  }
}
```

### Windsurf IDE
```json
{
  "mcpServers": {
    "cognigraph": {
      "command": "npx",
      "args": ["-y", "cognigraph"]
    }
  }
}
```

### Cursor
```json
{
  "mcpServers": {
    "cognigraph": {
      "command": "npx",
      "args": ["-y", "cognigraph"]
    }
  }
}
```

### Claude Desktop
```json
{
  "mcpServers": {
    "cognigraph": {
      "command": "npx",
      "args": ["-y", "cognigraph"]
    }
  }
}
```

### Zed
```json
{
  "context_servers": {
    "cognigraph": {
      "command": "npx",
      "args": ["-y", "cognigraph"],
      "env": {
        "MEMORY_DB_PATH": "<your-project>/.cognigraph/memory.db"
      }
    }
  }
}
```

### Kiro
```json
{
  "mcpServers": {
    "cognigraph": {
      "command": "npx",
      "args": ["-y", "cognigraph"]
    }
  }
}
```

### VS Code
```json
{
  "mcpServers": {
    "cognigraph": {
      "command": "npx",
      "args": ["-y", "cognigraph"]
    }
  }
}
```

### ZCode
```json
{
  "mcpServers": {
    "cognigraph": {
      "command": "npx",
      "args": ["-y", "cognigraph"]
    }
  }
}
```

## 🔧 Advanced Configuration

### Wire report + safety notes (current code)

- wire pins MEMORY_DB_PATH to <project>/.cognigraph/memory.db (wire.py:142-149) and returns WIRED or WIRED_WITH_ERRORS with config_errors/backups/agents_md/next_steps (wire.py:207-222) - on WIRED_WITH_ERRORS, fix config_errors first.
- Corrupt existing configs are preserved as .corrupt-<timestamp>.bak, never overwritten (wire.py:64-90).
- Without --db, --root commands bind to <root>/.cognigraph/memory.db (memory_cli.py:163-174); limit/ttl/importance inputs are clamped (server.py:42-71). Zed env is a sibling of command/args inside the server entry, not nested under settings (settings.env never reaches the process - wire.py:180-185).

### Custom Database Location
Set the `MEMORY_DB_PATH` environment variable:

```json
{
  "mcpServers": {
    "cognigraph": {
      "command": "npx",
      "args": ["-y", "cognigraph"],
      "env": {
        "MEMORY_DB_PATH": "/custom/path/to/memory.db"
      }
    }
  }
}
```

### Routine Startup Snapshot (optional)
By default, CogniGraph takes one timestamped full-DB snapshot
(`memory.db.backup-routine-...bak`) during routine startup maintenance, before
maintenance mutations. To opt out of **only** that routine startup snapshot, set
the `MEMORY_AUTO_BACKUP` environment variable to a false value in your MCP
server config:

```json
{
  "mcpServers": {
    "cognigraph": {
      "command": "npx",
      "args": ["-y", "cognigraph"],
      "env": {
        "MEMORY_AUTO_BACKUP": "0"
      }
    }
  }
}
```

Accepted false values (trimmed, case-insensitive): `0`, `false`, `no`, `off`.
Any other value—including unset, blank, `1`, `true`, `yes`, `on`, `y`, `t`,
`n`, `f`, or unrecognized text—preserves the historical enabled default.
This setting applies only to the routine startup snapshot in
`_auto_maintenance`: explicit `memory_cli.py backup` snapshots, migration
preflight snapshots, and the rest of auto-maintenance
(bootstrap/scan/optimize, etc.) are unaffected. It is not a switch that
disables all automatic snapshots. The `.cognigraph/config.json`
`auto_maintenance` field documented below is not the runtime control for this
snapshot; use the `MEMORY_AUTO_BACKUP` environment variable.

### Project-Specific Configuration
Create `.cognigraph/config.json` in your project root:

```json
{
  "bootstrap": {
    "max_facts": 50,
    "full": false
  },
  "auto_maintenance": true
}
```

## 🛠️ Everyday MCP workflows

The server currently registers **31 MCP tools**. The names below are a **quick-start subset**, not the complete registry. See the [full interface](README.md#3-exposed-mcp-tools) and the [tool governance policy](docs/TOOL_GOVERNANCE.md).

### Core Memory Operations
- `memory_write` - Record a new fact with provenance (secrets are refused)
- `memory_write_batch` - Atomic batch form of `memory_write`
- `memory_query` - Hybrid lexical + semantic + code-structure search
- `memory_get_intelligent_context` - Preferred phase-aware coding briefing with relevance, evidence, and budget controls
- `memory_get_context` - Legacy Markdown briefing for compatibility
- `memory_stats` - Database health: counts, category breakdown, edges

### File Operations
- `memory_get_file_context` - Pre-edit file impact briefing
- `memory_check_safety` - Dependency risk matrix for multi-file refactors

### Lifecycle
- `memory_supersede` - Replace an outdated fact with corrected content
- `memory_deprecate` - Mark a fact obsolete without replacement
- `memory_prepare_task` - Pre-coding flight briefing
- `memory_checkpoint_task` - Post-task checkpoint (components, decisions, test baseline, failed approaches)

### Code Graph & Trust
- `memory_who_calls` - Where a symbol is defined, its call sites, and importing files
- `memory_project_shape` - Indexed file inventory and code-graph size
- `memory_flag_fact` - Flag a misleading/unverified fact so future briefings carry a warning

Maintenance tasks (audit, prune, backfill, optimize, export-ui, branch, promote, backup, scan-provenance) run via `memory_cli.py` subcommands, not MCP tools.

## 📊 What Gets Stored Automatically

On first use, the system automatically learns:

- **Project Structure**: Directory organization and key files
- **Dependencies**: Package.json, requirements.txt, go.mod, etc.
- **Documentation**: README sections, docs/, AGENTS.md conventions
- **Git Context**: Branches, tagged commits, recent history
- **Code Architecture**: Function/class structures and imports

Then it continuously records:
- **Architectural Decisions**: Why certain approaches were chosen
- **Constraints**: Technical limitations and requirements
- **Component Relationships**: Dependencies and implementations
- **Code Changes**: With line-number provenance for self-healing

## 🎨 Agent Integration

The system automatically integrates with your coding agent's workflow:

### Before Editing Files
```
Agent: "I need to modify auth.py"
→ Agent calls memory_get_file_context("auth.py")
→ System returns: components, decisions, constraints, dependent files
→ Agent makes informed changes without breaking things
```

### Before Starting Tasks
```
Agent: "Add caching layer to API"
→ Agent requests memory_get_intelligent_context(task="add caching layer", phase="orient", focus_files=["api.py"])
→ System returns: architectural context, prior decisions, risks
→ Agent plans implementation with full context
```

### During Development
```
Agent: "I decided to use Redis for caching"
→ Agent calls memory_write(decision, "Use Redis for caching", ...)
→ System records decision with provenance
→ Future sessions remember this decision
```

## 🔍 Architecture

### Universal Entry Point
- **Node.js Wrapper** (`index.js`): Handles cross-platform setup
- **Auto-Detection**: Finds project root by searching for indicators
- **Environment Setup**: Creates Python venv and installs dependencies
- **Process Management**: Launches Python MCP server with proper environment

### Core System
- **Python MCP Server** (`server.py`): Exposes memory tools via MCP
- **Memory Store** (`memory_store.py`): SQLite-backed storage with FTS5 search
- **Bootstrap System** (`bootstrap.py`): Automated project learning
- **Provenance Tracking**: Line-number anchoring with drift detection

### Smart Features
- **Hybrid Search**: BM25 keyword + semantic embeddings
- **Graph Relationships**: Dependency tracking via SQLite CTEs
- **Auto-Maintenance**: Background self-healing and optimization
- **Concurrency Control**: Multi-agent swarm support with locks

## 🚦 Getting Started

1. **Add to your IDE's MCP config** (see IDE-specific sections above)
2. **Restart your IDE** to load the new MCP server
3. **Start coding** - the system auto-initializes on first use
4. **Enjoy persistent memory** across all your coding sessions

## 🐛 Troubleshooting

### Server doesn't start
- Ensure Node.js 18+ is installed
- Check that `npx` is available in your PATH
- Try running `npx -y cognigraph` manually

### Database not created
- Check write permissions in your project directory
- Verify the `.cognigraph` directory can be created
- Try setting a custom `MEMORY_DB_PATH` environment variable

### Tools not showing up
- Restart your IDE after adding the MCP config
- Check the MCP server is running (look for process in task manager)
- Verify the MCP config JSON is valid

### Poor search results
- Give it time - the system learns more with each session
- Run `python memory_cli.py bootstrap --root <project> --apply` to ingest more project context
- Use `memory_prepare_task` before complex tasks

## 📈 Performance

- **Database**: SQLite with WAL mode for concurrent access
- **Search**: FTS5 full-text search + semantic embeddings
- **Memory**: ~1-2MB per 1000 facts (very lightweight)
- **Startup**: <2 seconds for typical projects
- **Bootstrap**: 5-30 seconds depending on project size

## 🔐 Security

- **No Cloud**: Everything runs locally on your machine
- **No Telemetry**: No data sent to external services
- **Secrets Protection**: Automatic refusal to store credentials
- **File Permissions**: Respects system file permissions

## 🤝 Contributing

This is the universal npm wrapper for the core CogniGraph system. For core development, see the main CogniGraph repository.

## 📄 License

MIT License - see LICENSE file for details

## 🙏 Acknowledgments

Built on top of the excellent CogniGraph core system, designed for enterprise-grade persistent memory for AI coding agents.

---

**Zero configuration. Universal compatibility. Persistent memory. Happy coding! 🚀**
