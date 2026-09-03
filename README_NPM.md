# @project-memory/mcp-server

Universal MCP server for persistent project memory across all AI coding IDEs.

## 🚀 Zero-Configuration Setup

The easiest way to add persistent memory to any AI coding agent:

```json
{
  "mcpServers": {
    "project-memory": {
      "command": "npx",
      "args": ["-y", "@project-memory/mcp-server"]
    }
  }
}
```

That's it! No manual setup, no database creation, no bootstrapping required.

## ✨ Features

- **Universal Compatibility**: Works with Trae, Windsurf, Cursor, Claude Desktop, Zed, and any MCP-compatible IDE
- **Zero Configuration**: Auto-detects project structure and creates databases automatically
- **Auto-Bootstrap**: Intelligently learns your project on first use
- **Persistent Memory**: Maintains context across coding sessions
- **Smart Context Retrieval**: Hybrid search with graph-based dependency tracking
- **Self-Healing**: Automatic provenance checking and maintenance

## 🎯 How It Works

1. **First Use**: When an agent first calls any memory tool, the system:
   - Auto-detects your project root
   - Creates `.project-memory/memory.db` if needed
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
    "project-memory": {
      "command": "npx",
      "args": ["-y", "@project-memory/mcp-server"]
    }
  }
}
```

### Windsurf IDE
```json
{
  "mcpServers": {
    "project-memory": {
      "command": "npx",
      "args": ["-y", "@project-memory/mcp-server"]
    }
  }
}
```

### Cursor
```json
{
  "mcpServers": {
    "project-memory": {
      "command": "npx",
      "args": ["-y", "@project-memory/mcp-server"]
    }
  }
}
```

### Claude Desktop
```json
{
  "mcpServers": {
    "project-memory": {
      "command": "npx",
      "args": ["-y", "@project-memory/mcp-server"]
    }
  }
}
```

### Zed
```json
{
  "context_servers": {
    "project-memory": {
      "command": "npx",
      "args": ["-y", "@project-memory/mcp-server"]
    }
  }
}
```

## 🔧 Advanced Configuration

### Custom Database Location
Set the `MEMORY_DB_PATH` environment variable:

```json
{
  "mcpServers": {
    "project-memory": {
      "command": "npx",
      "args": ["-y", "@project-memory/mcp-server"],
      "env": {
        "MEMORY_DB_PATH": "/custom/path/to/memory.db"
      }
    }
  }
}
```

### Project-Specific Configuration
Create `.project-memory/config.json` in your project root:

```json
{
  "bootstrap": {
    "max_facts": 50,
    "full": false
  },
  "auto_maintenance": true
}
```

## 🛠️ Available MCP Tools

### Core Memory Operations
- `memory_write` - Record new facts with provenance
- `memory_query` - Search by keywords/tags
- `memory_query_hybrid` - Advanced semantic + keyword search
- `memory_get_context` - Get formatted context for coding questions
- `memory_stats` - View database statistics

### File Operations
- `memory_get_file_context` - Pre-edit file impact analysis
- `memory_check_safety` - Multi-file refactor safety check
- `memory_verify_provenance` - Code drift detection

### Advanced Features
- `memory_prepare_task` - Pre-coding flight briefing
- `memory_checkpoint_task` - Post-coding task checkpoint
- `memory_link_facts` - Record architectural relationships
- `memory_consolidate` - Compress micro-state facts

### Maintenance
- `memory_auto_maintenance` - Self-healing maintenance
- `memory_audit` - Duplicate/conflict detection
- `memory_prune` - Remove stale facts
- `memory_optimize_db` - Database optimization

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
→ Agent calls memory_prepare_task("add caching layer", ["api.py"])
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
- Ensure Node.js 14+ is installed
- Check that `npx` is available in your PATH
- Try running `npx -y @project-memory/mcp-server` manually

### Database not created
- Check write permissions in your project directory
- Verify the `.project-memory` directory can be created
- Try setting a custom `MEMORY_DB_PATH` environment variable

### Tools not showing up
- Restart your IDE after adding the MCP config
- Check the MCP server is running (look for process in task manager)
- Verify the MCP config JSON is valid

### Poor search results
- Give it time - the system learns more with each session
- Try the `memory_bootstrap_project` tool with `full=true`
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

This is the universal npm wrapper for the core project-memory system. For core development, see the main project-memory repository.

## 📄 License

MIT License - see LICENSE file for details

## 🙏 Acknowledgments

Built on top of the excellent project-memory core system, designed for enterprise-grade persistent memory for AI coding agents.

---

**Zero configuration. Universal compatibility. Persistent memory. Happy coding! 🚀**
