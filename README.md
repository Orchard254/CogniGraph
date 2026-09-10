# CogniGraph MCP Server

An enterprise-grade, state-of-the-art persistent memory system for AI coding agents (**Google Antigravity, Cursor, OpenCode, Claude Desktop**) — CogniGraph.

It provides cross-session, vendor-neutral memory for project components, architectural decisions, constraints, file dependency graph edges, code-anchored provenance, and state checkpoints.

For the full agent-orientation deep dive — tech stack, annotated file tree, complete 41-tool catalog, conventions, and current state — see [project-brief.md](project-brief.md).

---

## Key Capabilities

1. **Hybrid RRF Search Engine**: Combines SQLite FTS5 BM25 keyword matching with term-vector similarity via Reciprocal Rank Fusion ($RRF = \frac{1}{60 + rank_{bm25}} + \frac{1}{60 + rank_{vec}}$).
2. **Graph-Relational Knowledge Engine (`memory_edges`)**: Directed graph edges (`depends_on`, `implements`, `conflicts_with`, `relates_to`) traversed via SQLite Recursive Common Table Expressions (CTEs).
3. **Automatic Context Expansion (`memory_get_context`)**: Searching for a topic automatically expands connected graph relationship trees into a single context briefing.
4. **File Impact Analysis (`memory_get_file_context`)**: Pre-edit briefing for any source file listing its stored components, decisions, constraints, and all linked dependent files.
5. **Multi-File Refactor Safety Matrix (`memory_check_safety`)**: Scans batches of target files before refactoring to detect cross-file breaking risks.
6. **Code-Anchored Self-Healing Provenance (`memory_verify_provenance`)**: Anchors line numbers and SHA256 snippet hashes with neighborhood shift detection that self-heals line range shifts automatically.
7. **Swarm Concurrency & Advisory Locks**: Optimistic Concurrency Control (`version` checking) + advisory locks (`memory_locks`) preventing race conditions across multi-agent swarms.
8. **Interactive Visual Web UI (`memory_export_ui`)**: Standalone, self-contained HTML visualizer ([`memory_graph.html`](file:///.cognigraph/memory_graph.html)) for exploring the knowledge graph in 2D.
9. **Priming Eval Harness (`eval_priming.py`)**: Scores retrieval quality for realistic coding tasks — whether the facts an LLM needs (by content or file anchor) land in the top-K of `query_hybrid`. Run `python eval_priming.py` to regress-check priming quality after ranking changes.
10. **Code Graph & Trust Layer (`structural_edges`)**: AST-ground-truth call and import edges across the codebase, rebuilt on every structural sync. One-hop mid-reasoning queries via `memory_who_calls` ("who calls X? who imports Y?"), project orientation via `memory_project_shape`, and a trust layer: unmapped files are flagged explicitly, flagged/unverified facts are stamped inline at priming time (`memory_flag_fact` correction hook), and `memory_checkpoint_task` captures failed approaches as negative-knowledge constraints.

---

## Architecture: the two-table intake contract

The database has two fact stores with distinct roles:

- **`memory`** — the live intake table. Every `memory_write` / `memory_write_batch` / `memory_checkpoint_task` lands here, along with all lifecycle operations (supersede, deprecate, consolidate, prune, provenance verification, graph edges).
- **`episodic_memories`** — the historical archive created by the genesis migration on 2026-08-24 (108 legacy facts), plus the branch/revision bookkeeping used by `memory_create_branch` / `memory_promote_branch` / `memory_discard_branch`. It is read-only for intake.

Retrieval (`memory_query_hybrid`, `memory_get_context`) fuses both stores through 7-lane RRF, so agents see one unified memory. New facts are intentionally memory-bound by contract; episodic_memories is not an intake target.

## Code module structure

The `MemoryStore` class is composed from focused mixins, one per architectural layer:

- **`pm_common.py`** — constants, schema, embedding helpers (`_encode`, `_vec_to_blob`, `_cosine_sim`), secret detection, and DB-path resolution.
- **`pm_search.py`** (`SearchMixin`) — the retrieval brain: semantic scoring lanes, 7-lane hybrid RRF `query`/`query_hybrid` with file-anchor boosting (facts anchored to a file named in the query get a 1.6x rank boost), intent detection, and usage-pattern learning (`_log_usage`, `_usage_boost`, `usage_stats`).
- **`pm_context.py`** (`ContextMixin`) — LLM priming output: `get_context` (with long-fact truncation and doc-noise suppression for coding intents), `get_file_context`, `_structural_briefing_lines`, `get_dependency_map`, and proactive suggestions.
- **`pm_lifecycle.py`** (`LifecycleMixin`) — agent workflow hooks and maintenance: `prepare_task`, `checkpoint_task`, provenance verification/scanning, consolidation, pruning, auditing, export, and stats.
- **`pm_structural.py`** (`StructuralMixin`) — structural index sync (`sync_structural_nodes`) and fact-to-node linking.
- **`memory_store.py`** — the slimmed storage core (schema setup, fact CRUD, dedup, graph edges, locks) composing the mixins. It re-exports every public name, so existing imports like `from memory_store import MemoryStore, default_db_path` keep working unchanged.

---

## 1. Quick Start (3 commands)

```powershell
# 1. One-time setup: venv + dependencies + test suite
.\setup.ps1

# 2. Wire your project to your coding agent (interactive menu if agent is omitted)
.\venv\Scripts\python.exe memory_cli.py wire --root C:\path\to\your\project
#    or specify directly: antigravity | cursor | windsurf | claude | zed | roo | cline | opencode | generic

# 3. Ingest your project's context into memory
.\venv\Scripts\python.exe memory_cli.py bootstrap --root C:\path\to\your\project --apply
```

Setup detail: setup.ps1 creates venv/, installs requirements.txt, then runs the full suite (setup.ps1:16-29). Re-run manually with .\venv\Scripts\python.exe -m pytest -q --ignore=scratch. 14 test_*.py files are present at repo root (test_call_tool, test_env, test_env2, test_final, test_list, test_list2, test_llm_priming, test_memory_cli, test_methods, test_phase3_priming, test_server_tools, test_tools_call, test_watcher, test_wire_bootstrap); pytest --collect-only -q --ignore=scratch collected 205 tests as of 2026-09-10, contributed by test_llm_priming, test_phase3_priming, test_wire_bootstrap, test_memory_cli, test_watcher and test_server_tools (remaining test_*.py files are manual/helper scripts; scratch/ holds a live-watcher probe excluded from the suite).

Restart your coding agent. It now starts every session with full project context.

---

## 2. Agent Configuration

> **Recommended:** run `memory_cli.py wire <agent> --root <project>` — it generates the correct config automatically. The manual JSON below is the fallback.

Point your AI agent's MCP configuration at `server.py`. Prefer `memory_cli.py wire <agent> --root <project>` - it generates the config automatically and pins MEMORY_DB_PATH to <project-root>/.cognigraph/memory.db (created in wire.py:142-149).

Standard agents (`mcpServers`):

```jsonc
{
  "mcpServers": {
    "cognigraph": {
      "command": "C:/Ai/cognigraph/venv/Scripts/python.exe",
      "args": ["C:/Ai/cognigraph/server.py"],
      "env": {
        "MEMORY_DB_PATH": "C:/path/to/your/project/.cognigraph/memory.db"
      }
    }
  }
}
```

Zed (`.zed/settings.json`, `context_servers` - `env` is a sibling of `command`/`args` inside the server entry, not nested under `settings`; `settings.env` never reaches the process - wire.py:180-185):

```jsonc
{
  "context_servers": {
    "cognigraph": {
      "command": "C:/Ai/cognigraph/venv/Scripts/python.exe",
      "args": ["C:/Ai/cognigraph/server.py"],
      "env": {
        "MEMORY_DB_PATH": "C:/path/to/your/project/.cognigraph/memory.db"
      }
    }
  }
}
```

### Agent Instruction Rules (`AGENTS.md` / `GEMINI.md` / `.cursor/rules/cognigraph.mdc`)

```markdown
## Mandatory CogniGraph Protocol
At the start of every session:
1. Call `memory_get_state` or `memory_stats` to load existing project context.
2. Call `memory_get_context(query)` before asking broad architecture questions.
3. Call `memory_get_file_context(file_path)` BEFORE editing any source file.
4. Call `memory_write` or `memory_write_batch` when learning new facts (include `file_path` and `line_start`/`line_end` for provenance).
5. Call `memory_supersede` (not a new `memory_write`) when correcting an outdated fact.
6. Call `memory_link_facts` to record architectural dependencies between facts.
```

---

## 3. Exposed MCP Tools

<!-- tool-table:start auto-generated -->
The server registers 41 tools.

| Tool | Parameters | Description |
|---|---|---|
| `memory_write` | `category`, `content`, `rationale?`, `confidence?`, `session_id?`, `file_path?`, `tags?`, `importance?`, `line_start?`, `line_end?`, `code_hash?`, `git_commit_sha?` | Record a new fact in project memory. |
| `memory_write_batch` | `facts` | Record multiple new facts atomically in a single transaction. |
| `memory_query` | `text`, `category?`, `tag?`, `limit?`, `session_id?` | Search project memory by keyword/tag (FTS5 lexical). |
| `memory_query_hybrid` | `text`, `category?`, `tag?`, `limit?`, `expand_graph?`, `session_id?`, `repo_id?`, `intent?`, `agent_type?` | Hybrid RRF search combining lexical BM25 matching and term vector similarity, with intent-aware ranking and usage-pattern learning. Automatically includes graph-linked dependency facts when expand_graph=True. repo_id: optional - if omitted, defaults to the store's own repo (first repo) for episodic filtering. intent: optional task intent ('fix', 'implement', 'refactor', etc.) that boosts category-relevant facts in ranking; auto-detected from text if omitted. agent_type: optional ('cursor', 'claude', 'opencode', 'zed', 'default') to apply agent-specific retrieval-lane weights. Every retrieval is logged for usage-pattern learning. |
| `memory_get_context` | `query`, `limit?`, `repo_id?`, `max_chars?`, `intent?` | High-precision context retriever with intent-based expansion and LLM optimization. |
| `memory_get_context_optimized` | `query`, `agent_type?`, `limit?`, `max_chars?`, `intent?` | Agent-specific context generation with model-optimized formatting. |
| `memory_get_context_for_agent` | `query`, `agent_type?`, `max_tokens?` | Smart context generation that automatically selects the best format for the agent. |
| `memory_get_file_context` | `file_path` | Get file context before editing. |
| `memory_check_safety` | `file_paths` | Multi-File Refactor Safety Matrix. Call this BEFORE modifying a batch of multiple files! Analyzes cross-file dependencies, shared constraints, and breaking change risks across all target files. |
| `memory_get_dependency_map` | `file_paths` | Cross-File Dependency Map for LLM priming. |
| `memory_usage_stats` | `top_facts?` | USAGE-PATTERN INSIGHTS - Summarizes how this project's memory is being used. |
| `memory_suggest_context` | `current_fact_ids`, `limit?` | PROACTIVE CONTEXT SUGGESTIONS - Predicts what context you may need next. |
| `memory_optimize_db` | — | Run SQLite database maintenance: PRAGMA optimize, VACUUM, and check integrity. |
| `memory_auto_maintenance` | `project_root?` | AUTOMATIC MAINTENANCE - self-heals 99%: incremental bootstrap (20) + scan auto_heal (importance=1) + backfill + optimize. |
| `memory_export_ui` | `out_path?` | Generate an interactive HTML Knowledge Graph visualizer file for browsing memory. |
| `memory_supersede` | `old_id`, `new_content`, `rationale?`, `confidence?`, `session_id?`, `file_path?`, `tags?`, `expected_version?` | Replace an outdated fact with a corrected or updated one. |
| `memory_deprecate` | `id`, `reason?` | Mark a fact as no longer valid without replacing it. |
| `memory_get_state` | `category?`, `session_id?` | Get full snapshot of active project memory. |
| `memory_link_facts` | `source_id`, `target_id`, `relation` | Create a directed graph edge between two facts. |
| `memory_unlink_facts` | `source_id`, `target_id`, `relation` | Remove a directed graph edge between two facts. |
| `memory_get_graph` | `root_id`, `max_depth?` | Traverse relationship edges starting from root_id up to max_depth. |
| `memory_acquire_lock` | `resource`, `locked_by`, `ttl_seconds?` | Acquire an advisory lock on a named resource for multi-turn multi-agent operations. |
| `memory_release_lock` | `resource`, `locked_by` | Release an advisory lock on a named resource. |
| `memory_verify_provenance` | `fact_id`, `project_root?` | Verify code snippet SHA256 hash at file_path. Self-heals line range shifts |
| `memory_consolidate` | `session_id?`, `category?`, `allow_all?` | Compress itemized micro-state facts into milestone summary facts. |
| `memory_prune` | `days_inactive?`, `dry_run?` | PRUNE STALE FACTS - Deprecate low-value state/other facts not accessed in |
| `memory_audit` | — | Scan active memory for potential near-duplicates or conflicting facts. |
| `memory_export_markdown` | `category?` | Export active memory as formatted markdown for LLM context prompts. |
| `memory_stats` | — | Overview of memory database statistics, categories, edges, and location. |
| `memory_prepare_task` | `intent`, `file_paths?`, `limit?` | PRE-CODING FLIGHT BRIEFING - Call this at the START of every coding task. |
| `memory_checkpoint_task` | `summary`, `components?`, `decisions?`, `test_baseline?`, `failures?` | POST-CODING CHECKPOINT - Call this at the END of every coding task. |
| `memory_scan_provenance` | `project_root?`, `auto_deprecate?`, `auto_heal?` | BULK PROVENANCE HEALTH CHECK - Scan all active facts for code drift. |
| `memory_backfill_embeddings` | — | SEMANTIC INDEX BACKFILL - Generate embeddings for all existing facts that lack them. |
| `memory_promote_branch` | `source_branch_id`, `convergence_policy?` | Promote an experimental branch's episodic memories into its canonical root (KEEP_BOTH). |
| `memory_create_branch` | `repo_id?` | Create a new experimental branch under the canonical root. |
| `memory_discard_branch` | `source_branch_id`, `reason?` | Discard an experimental branch (terminal alternative to promotion). |
| `memory_list_branches` | `repo_id?`, `status?` | List branches for a repo (read-only). |
| `memory_bootstrap_project` | `project_root?`, `dry_run?`, `full?`, `max_facts?`, `install_hook?`, `drop_protocol?` | PROJECT ONBOARDING - Scan a repository and ingest its structural + documentary |
| `memory_who_calls` | `symbol` | ONE-HOP CODE GRAPH QUERY - ask this mid-reasoning instead of re-reading files. |
| `memory_project_shape` | — | PROJECT ORIENTATION - indexed file inventory and code-graph size. |
| `memory_flag_fact` | `fact_id`, `note?`, `flag_type?` | CORRECTION HOOK - fire this the moment a served fact turns out wrong. |
<!-- tool-table:end -->

---

## 4. Command-Line Utility (`memory_cli.py`)

Run offline tasks directly from the shell:

```powershell
# Show DB statistics & counts
python memory_cli.py stats

# Run duplicate & conflict audit
python memory_cli.py audit

# Pre-edit file impact briefing
python memory_cli.py file-context backend/core/config.py

# Multi-file refactor safety check
python memory_cli.py check-safety backend/main.py backend/core/config.py

# Traverse graph dependencies
python memory_cli.py graph 254 --depth 3

# Export Markdown context snapshot
python memory_cli.py export --out MEMORY_SNAPSHOT.md

# Generate Interactive 2D HTML Knowledge Graph
python memory_cli.py export-ui --out memory_graph.html

# Bulk provenance health check (read-only unless --auto-deprecate)
python memory_cli.py scan-provenance --root C:\path\to\your\repo

# Backfill semantic embeddings for historical facts (batched single-model-load pass; bootstrap itself writes with embed=False then backfills in one batch - bootstrap.py:432-487)
python memory_cli.py backfill

# Preview stale-fact pruning (add --apply to execute)
python memory_cli.py prune --days 90

# Pre-coding flight briefing from the shell
python memory_cli.py prepare-task "implement caching layer" --files cache.py
```

---

## 4b. Reliability notes (verified against current code)

- wire report schema (wire.py:207-222): status is WIRED, or WIRED_WITH_ERRORS when config_errors is non-empty; report carries config_files, backups (rotating .bak/.bak.1/.bak.2), config_errors, agents_md (created/skipped_exists/...), mcp_snippet, and next_steps (bootstrap dry-run, --apply, restart, plus Fix config errors first lines when errors exist). On WIRED_WITH_ERRORS, fix config_errors first - the listed config was not written.
- Corrupt-config refusal (wire.py:64-90): an existing config that is not valid JSON is preserved as <name>.corrupt-<timestamp>.bak and reported in config_errors; it is never silently overwritten (atomic tmp + fsync + os.replace).
- --root DB binding (memory_cli.py:163-174): without explicit --db, any command with --root binds to <root>/.cognigraph/memory.db so facts never land in the CWD fallback store; wire never touches the DB (store is constructed lazily for other commands).
- Concurrency (memory_store.py:48,1095-1123; server.py:25-40): SQLite PRAGMA journal_mode=WAL + busy_timeout=5000; get_store() returns a per-thread MemoryStore (one connection per thread on the same WAL DB); each MCP tool call gets a fresh store that is always closed (no leaked fd/WAL handles).
- Git probes (bootstrap.py:309-327; wire.py:26-43; hooks/git_hook.py:29-30): every git/python child probe runs with stdin=subprocess.DEVNULL and a timeout (15 s) so an inherited stdio pipe under the MCP server cannot stall indefinitely.
- Input clamps (server.py:42-71; mirrored in memory_cli.py:218-234): limit 1..100 (context/prepare-task paths cap at 50, graph depth at 10, bootstrap max_facts at 1000), lock ttl_seconds 1..86400 (default 60), fact importance 1..5 (default 3); non-numeric input falls back to the default instead of raising. Secrets are refused (pm_common.py:104-117; memory_store.py:474,682).

## 5. Automated Git Commit Hook Integration

Auto-capture commit messages tagged with `[decision]`, `[constraint]`, `[component]`, `[arch]`, or `[state]` into `memory.db`:

```bash
python hooks/git_hook.py
```
