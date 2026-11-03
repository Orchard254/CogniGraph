# CogniGraph: native OpenCode V2 context delivery

OpenCode V2 can inject project memory *before* each agent-model request through its
native `session.hook("context", ...)` hook. This integration supplements the existing
MCP server; it does not remove or change MCP tools.

## Install into a project

Use the Python interpreter where CogniGraph's dependencies are installed:

```bash
python install_opencode.py --root /absolute/path/to/your/project
```

If your CogniGraph environment uses a different Python, run this command from
that interpreter or pass `--python /path/to/venv/bin/python`.

This creates `.opencode/plugins/cognigraph/index.ts` and adds `@opencode/plugin`
to `.opencode/package.json` without deleting existing dependencies. OpenCode
loads local plugins at startup; restart OpenCode after installation. OpenCode
may install the plugin dependency with Bun at startup. The plugin calls the
CogniGraph Python bridge by absolute path, so **keep the CogniGraph checkout
at its installed location** (reinstall if it moves).

To populate memory the first time, follow the existing bootstrap workflow:

```bash
python memory_cli.py bootstrap --root /absolute/path/to/your/project --apply
```

If `.cognigraph/memory.db` does not exist, the bridge returns no context and
**does not** silently create a second database. It accepts legacy
`.project-memory/memory.db` when that is the only project database. Run the
normal migration command when convenient.

## How delivery works

1. `prompt` hook records the submitted task without modifying the user's text.
2. `context` hook reads the task, invokes `opencode_context.py` in a bounded
   subprocess and appends the returned Markdown as a system text part.
3. The OpenCode V2 `tool.execute.after` hook observes successful structured
   `read`, `edit`, `write`, and `multiedit` calls. When the actively worked-on
   source file changes (or is edited), CogniGraph refreshes a bounded,
   file-specific briefing at the next model request. Failed tools and arbitrary
   shell output are never used to infer a file path.
4. The task-wide briefing remains cached. File context is independently cached
   until focus changes or the editor reports an edit. The most recent two
   observed file paths are eligible for refresh.
5. A new user task resets both caches. Sessions are isolated by ID.
6. Successful task-wide retrievals include a receipt ID that agents may use
   for attributable feedback through MCP. Mere exposure isn't evidence.
7. If memory is unavailable or retrieval times out, no context is injected;
   coding continues normally and existing MCP tools still work.

On each refreshed file briefing, source-anchored facts with hashes receive
read-only freshness checks. When the anchored code changed or disappeared,
the injected context includes a warning for the agent to verify it. These
warnings do not independently prove a fact false or modify stored memory.
See [Source Freshness](docs/SOURCE_FRESHNESS.md).

The bridge retrieves from the target project's existing memory database (and may
update retrieval-usage counters). Neither
an unrelated project nor the CogniGraph checkout can be used as a fallback.
A bounded context payload is marked as **reference data, not instructions**;
the agent should verify facts against current source code before modifying it.

Current limitations: this adapter targets **OpenCode V2**, not V1. Only
structured source-file paths from the supported editor tools trigger file
refreshes; a shell command, file watcher, or external Git change does not.
File memory is not independently verified against the latest source; the
briefing explicitly instructs the agent to check the code. Database-backed
context retrieval does not run a filesystem indexing pass. The integration
still needs a live installed-OpenCode end-to-end test; hook simulations and
Python bridge tests do not establish full product compatibility.

The bridge only permits existing source files inside the active project,
rejects files outside the project (including symlink escapes), and excludes
hidden paths and non-source extensions. Automatic context is deliberately
small: 2,900 characters for task memory plus up to 1,500 for focused files.
Neither the tool's output nor its source-code contents are sent to CogniGraph
for learning.

## Tests

```bash
python -m unittest -v test_opencode_plugin.py
```

Official API reference: https://opencode.ai/v2/docs/build/plugins/server

After updating CogniGraph, run the installer again and restart OpenCode to
replace the previously generated plugin file. The installer refuses to
overwrite a manually customized plugin.
