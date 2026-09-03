# Quick Start Guide

## Installation

No installation required! The universal npm package handles everything automatically.

## Setup (30 seconds)

### 1. Add to your IDE's MCP configuration

**Trae IDE, Windsurf, Cursor, Claude Desktop:**
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

**Zed:**
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

### 2. Restart your IDE

### 3. Start coding!

The system will automatically:
- Create `.project-memory/memory.db` in your project
- Learn your project structure 
- Bootstrap with key information
- Begin persistent memory tracking

## Verification

After restarting your IDE, ask your coding agent:

```
"Call memory_stats to show me the project memory status"
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
2. **Customize**: Add `.project-memory/config.json` for project-specific settings
3. **Explore**: Use `memory_prepare_task` before complex tasks
4. **Monitor**: Check `memory_stats` periodically to see what's been learned

## Troubleshooting

**Issue**: "npx command not found"
- **Solution**: Install Node.js from nodejs.org

**Issue**: "Server starts but no tools available"  
- **Solution**: Restart your IDE and check MCP config JSON validity

**Issue**: "Database creation failed"
- **Solution**: Check write permissions or set custom MEMORY_DB_PATH

## Support

For issues or questions, please refer to the main README_NPM.md or the core project-memory documentation.
