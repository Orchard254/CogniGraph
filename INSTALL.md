# Quick Start Guide

## Installation

Prerequisites: Python 3.12+ on PATH (setup.ps1:9-13 enforces this; index.js/postinstall.js probe python3 -> python for 3.12+) and Node.js >= 14 for the npx wrapper (package.json engines). On first run the wrapper creates venv/ inside the package, runs pip install -r requirements.txt, and launches server.py (index.js:98-132,171-232). Only mcp is hard-required - missing sqlite-vec/tree-sitter*/fastembed/numpy lanes degrade with a warning, not a failure. A pre-spawn py_compile gate plus a 5 s startup watchdog fail fast on broken installs (index.js:245-285); postinstall.js is best-effort and leaves a .postinstall-incomplete marker that index.js:175-183 re-verifies on next startup.

## Setup (30 seconds)

### 1. Add to your IDE's MCP configuration

**Trae IDE, Windsurf, Cursor, Claude Desktop:**
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

**Zed:**
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

Zed note: env is a sibling of command/args inside the server entry, not nested under settings (settings.env never reaches the process - wire.py:180-185). wire pins MEMORY_DB_PATH to <project-root>/.cognigraph/memory.db (wire.py:142-149); or set MEMORY_DB_PATH yourself - index.js honors it as the single source of truth (index.js:31-33).

### 2. Restart your IDE

### 3. Start coding!

The system will automatically:
- Create `.cognigraph/memory.db` in your project
- Learn your project structure 
- Bootstrap with key information
- Begin persistent memory tracking

## Verification

After restarting your IDE, ask your coding agent:

```
"Call memory_stats to show me the CogniGraph status"
```

You should see database statistics indicating the system is working.

## What Happens Automatically

**First Use:**
- ✅ Project root detection
- ✅ Database creation  
- ✅ Dependency installation
- ✅ Project structure learning
- ✅ Initial bootstrap (top 20 facts)

**Ongoing:**
- ✅ Architectural decision recording
- ✅ Component relationship tracking
- ✅ Code change provenance
- ✅ Context-aware suggestions

## Next Steps

1. **Try it out**: Ask your agent to modify a file and watch it use memory tools
2. **Customize**: Add `.cognigraph/config.json` for project-specific settings
3. **Explore**: Use `memory_prepare_task` before complex tasks
4. **Monitor**: Check `memory_stats` periodically to see what's been learned

## Troubleshooting

**Issue**: "npx command not found"
- **Solution**: Install Node.js from nodejs.org

**Issue**: "Server starts but no tools available"  
- **Solution**: Restart your IDE and check MCP config JSON validity

**Issue**: "Database creation failed"
- **Solution**: Check write permissions or set custom MEMORY_DB_PATH

## Wire report + safety notes (current code)

- wire pins MEMORY_DB_PATH to <project>/.cognigraph/memory.db (wire.py:142-149) and returns WIRED or WIRED_WITH_ERRORS with config_errors/backups/agents_md/next_steps (wire.py:207-222) - on WIRED_WITH_ERRORS, fix config_errors first.
- Corrupt existing configs are preserved as .corrupt-<timestamp>.bak, never overwritten (wire.py:64-90).
- Without --db, --root commands bind to <root>/.cognigraph/memory.db (memory_cli.py:163-174); limit/ttl/importance inputs are clamped (server.py:42-71).

## Support

For issues or questions, please refer to the main README_NPM.md or the core CogniGraph documentation.
