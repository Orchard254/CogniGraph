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
3. On tool-driven continuations, the plugin injects the same cached briefing
   into the new outgoing request without re-querying SQLite.
4. A new user task invalidates that session's cache. Sessions are isolated by ID.
5. Successful retrievals include a receipt ID that agents may use to report
   attributable feedback through the existing MCP interface. Repeated exposure
   is never treated as evidence of correctness.
6. If memory is unavailable or retrieval times out, no context is injected;
   coding continues normally and the original MCP tools still work.

The bridge retrieves from the target project's existing memory database (and may
update retrieval-usage counters). Neither
an unrelated project nor the CogniGraph checkout can be used as a fallback.
A bounded context payload is marked as **reference data, not instructions**;
the agent should verify facts against current source code before modifying it.

Current limitations: this adapter is for **OpenCode V2**; V1 uses a different
hook API. The retrieval cache refreshes on each submitted task, not on every
file change. It does not yet run a full live OpenCode integration test; unit
tests validate the installer and Python bridge.

## Tests

```bash
python -m unittest -v test_opencode_plugin.py
```

Official API reference: https://opencode.ai/v2/docs/build/plugins/server
