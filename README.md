# project-memory MCP Server

An enterprise-grade, state-of-the-art persistent project-memory system for AI coding agents (**Google Antigravity, Cursor, OpenCode, Claude Desktop**).

It provides cross-session, vendor-neutral memory for project components, architectural decisions, constraints, file dependency graph edges, code-anchored provenance, and state checkpoints.

---

## Key Capabilities

1. **Hybrid RRF Search Engine**: Combines SQLite FTS5 BM25 keyword matching with term-vector similarity via Reciprocal Rank Fusion ($RRF = \frac{1}{60 + rank_{bm25}} + \frac{1}{60 + rank_{vec}}$).
2. **Graph-Relational Knowledge Engine (`memory_edges`)**: Directed graph edges (`depends_on`, `implements`, `conflicts_with`, `relates_to`) traversed via SQLite Recursive Common Table Expressions (CTEs).
3. **Automatic Context Expansion (`memory_get_context`)**: Searching for a topic automatically expands connected graph relationship trees into a single context briefing.
4. **File Impact Analysis (`memory_get_file_context`)**: Pre-edit briefing for any source file listing its stored components, decisions, constraints, and all linked dependent files.
5. **Multi-File Refactor Safety Matrix (`memory_check_safety`)**: Scans batches of target files before refactoring to detect cross-file breaking risks.
6. **Code-Anchored Self-Healing Provenance (`memory_verify_provenance`)**: Anchors line numbers and SHA256 snippet hashes with neighborhood shift detection that self-heals line range shifts automatically.
7. **Swarm Concurrency & Advisory Locks**: Optimistic Concurrency Control (`version` checking) + advisory locks (`memory_locks`) preventing race conditions across multi-agent swarms.
8. **Interactive Visual Web UI (`memory_export_ui`)**: Standalone, self-contained HTML visualizer ([`memory_graph.html`](file:///.project-memory/memory_graph.html)) for exploring the knowledge graph in 2D.
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

# 2. Wire your project to your coding agent (run from anywhere)
.\venv\Scripts\python.exe memory_cli.py wire cursor --root C:\path\to\your\project
#    supported agents: opencode | cursor | claude | zed | generic

# 3. Ingest your project's context into memory
.\venv\Scripts\python.exe memory_cli.py bootstrap --root C:\path\to\your\project --apply
```

Setup detail: setup.ps1 creates venv/, installs requirements.txt, then runs the full suite (setup.ps1:16-29). Re-run manually with .\venv\Scripts\python.exe -m pytest -q --ignore=scratch. 14 test_*.py files are present at repo root (test_call_tool, test_env, test_env2, test_final, test_list, test_list2, test_llm_priming, test_memory_cli, test_methods, test_phase3_priming, test_server_tools, test_tools_call, test_watcher, test_wire_bootstrap); pytest --collect-only -q --ignore=scratch collected 188 tests as of 2026-09-10, contributed by test_llm_priming, test_phase3_priming, test_wire_bootstrap, test_memory_cli, test_watcher and test_server_tools (remaining test_*.py files are manual/helper scripts; scratch/ holds a live-watcher probe excluded from the suite).

Restart your coding agent. It now starts every session with full project context.

---

## 2. Agent Configuration

> **Recommended:** run `memory_cli.py wire <agent> --root <project>` — it generates the correct config automatically. The manual JSON below is the fallback.

Point your AI agent's MCP configuration at `server.py`. Prefer `memory_cli.py wire <agent> --root <project>` - it generates the config automatically and pins MEMORY_DB_PATH to <project-root>/.project-memory/memory.db (created in wire.py:142-149).

Standard agents (`mcpServers`):

```jsonc
{
  "mcpServers": {
    "project-memory": {
      "command": "C:/Ai/project-memory/venv/Scripts/python.exe",
      "args": ["C:/Ai/project-memory/server.py"],
      "env": {
        "MEMORY_DB_PATH": "C:/path/to/your/project/.project-memory/memory.db"
      }
    }
  }
}
```

Zed (`.zed/settings.json`, `context_servers` - `env` is a sibling of `command`/`args` inside the server entry, not nested under `settings`; `settings.env` never reaches the process - wire.py:180-185):

```jsonc
{
  "context_servers": {
    "project-memory": {
      "command": "C:/Ai/project-memory/venv/Scripts/python.exe",
      "args": ["C:/Ai/project-memory/server.py"],
      "env": {
        "MEMORY_DB_PATH": "C:/path/to/your/project/.project-memory/memory.db"
      }
    }
  }
}
```

### Agent Instruction Rules (`AGENTS.md` / `GEMINI.md` / `.cursor/rules/project-memory.mdc`)

```markdown
## Mandatory Project Memory Protocol
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

| Tool | Parameters | Description |
|---|---|---|
| `memory_write` | `category`, `content`, `rationale?`, `confidence?`, `file_path?`, `tags?`, `importance?` | Record a new fact with source provenance; tags are auto-derived from category, file anchor, and content when omitted |
| `memory_write_batch` | `facts: list` | Record multiple facts atomically in one transaction |
| `memory_query` | `text`, `category?`, `tag?`, `limit?` | FTS5 BM25 keyword search over active facts |
| `memory_query_hybrid` | `text`, `category?`, `tag?`, `limit?`, `expand_graph?`, `intent?`, `agent_type?` | Hybrid RRF lexical + vector search with graph expansion, intent-aware category boosting, per-agent lane weights, file-anchor boosting, and usage-pattern learning (every retrieval is logged and boosts frequently-needed facts on similar future queries; cold start seeds boosts from `access_count` until usage history accrues) |
| `memory_get_context` | `query`, `limit?`, `intent?` | High-precision Markdown context briefing for LLM priming; auto-detects task intent (fix/implement/refactor/...) to prioritize constraints vs decisions |
| `memory_get_context_optimized` | `query`, `agent_type?`, `limit?`, `intent?` | Agent-specific context formatting with per-agent token budgets and priority orders (cursor, claude, opencode, zed, default) |
| `memory_get_context_for_agent` | `query`, `agent_type?`, `max_tokens?` | Smart context generation that auto-selects the best format for the target agent |
| `memory_get_file_context` | `file_path` | Pre-edit file impact briefing listing components, constraints, linked files, code structure (functions/classes/imports), and call-graph edges; unmapped files are flagged, and flagged/unverified facts are stamped inline |
| `memory_check_safety` | `file_paths: list` | Multi-file refactor risk matrix across a batch of target files |
| `memory_get_dependency_map` | `file_paths: list` | Cross-file dependency map showing internal edges, external dependents, shared constraints, and high-risk files |
| `memory_usage_stats` | `top_facts?` | Usage-pattern insights: total retrievals, intent distribution, most-retrieved facts, recent queries |
| `memory_suggest_context` | `current_fact_ids: list`, `limit?` | Proactive context suggestions from co-retrieval patterns: predicts what facts you will need next based on what was retrieved together in the past |
| `memory_who_calls` | `symbol` | One-hop code graph query: definitions, all callers, and importing files for a symbol (bare name or Class.method), from tree-sitter AST ground truth |
| `memory_project_shape` | — | Project orientation: indexed file inventory, definition counts, and code-graph size |
| `memory_flag_fact` | `fact_id`, `note?`, `flag_type?` | Correction hook: flag a fact that misled you so future briefings stamp the warning inline |
| `memory_supersede` | `old_id`, `new_content`, `expected_version?` | Atomically replace an outdated fact with OCC version check |
| `memory_deprecate` | `id`, `reason?` | Mark a fact as obsolete without replacement |
| `memory_link_facts` | `source_id`, `target_id`, `relation` | Add a directed graph edge (`depends_on`, `implements`, etc.) |
| `memory_unlink_facts` | `source_id`, `target_id`, `relation` | Remove a directed graph edge |
| `memory_get_graph` | `root_id`, `max_depth?` | Traverse knowledge graph starting from root_id via SQLite CTEs |
| `memory_acquire_lock` | `resource`, `locked_by`, `ttl_seconds?` | Acquire advisory lock for multi-agent swarm operations |
| `memory_release_lock` | `resource`, `locked_by` | Release advisory lock |
| `memory_verify_provenance`| `fact_id`, `project_root?` | SHA256 code hash check with line-shift self-healing |
| `memory_consolidate` | `session_id?`, `category?` | Compress itemized micro-state facts into milestone summaries |
| `memory_audit` | `None` | Scan active memory for near-duplicates or conflicting facts |
| `memory_export_markdown` | `category?` | Export full active memory snapshot as structured Markdown |
| `memory_export_ui` | `out_path?` | Generate standalone interactive 2D HTML Knowledge Graph file |
| `memory_optimize_db` | `None` | Run SQLite maintenance (`PRAGMA optimize`, `VACUUM`) |
| `memory_stats` | `None` | Active/deprecated counts, edges, and database location |
| `memory_prune` | `days_inactive?`, `dry_run?` | Deprecate stale low-value facts not accessed recently (dry-run by default) |
| `memory_prepare_task` | `intent`, `file_paths?`, `limit?` | Pre-coding flight briefing: context, file impact, risks, latest baseline (single newest checkpoint, capped — file briefings carry knowledge, not checkpoint history) |
| `memory_checkpoint_task` | `summary`, `components?`, `decisions?`, `test_baseline?`, `failures?` | Post-coding checkpoint: writes learned facts + verified test baseline; `failures` stores failed approaches as negative-knowledge constraints |
| `memory_scan_provenance` | `project_root?`, `auto_deprecate?` | Bulk code-drift health check across all anchored facts |
| `memory_backfill_embeddings` | None | Generate missing semantic embeddings for historical facts |

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
- --root DB binding (memory_cli.py:163-174): without explicit --db, any command with --root binds to <root>/.project-memory/memory.db so facts never land in the CWD fallback store; wire never touches the DB (store is constructed lazily for other commands).
- Concurrency (memory_store.py:48,1095-1123; server.py:25-40): SQLite PRAGMA journal_mode=WAL + busy_timeout=5000; get_store() returns a per-thread MemoryStore (one connection per thread on the same WAL DB); each MCP tool call gets a fresh store that is always closed (no leaked fd/WAL handles).
- Git probes (bootstrap.py:309-327; wire.py:26-43; hooks/git_hook.py:29-30): every git/python child probe runs with stdin=subprocess.DEVNULL and a timeout (15 s) so an inherited stdio pipe under the MCP server cannot stall indefinitely.
- Input clamps (server.py:42-71; mirrored in memory_cli.py:218-234): limit 1..100 (context/prepare-task paths cap at 50, graph depth at 10, bootstrap max_facts at 1000), lock ttl_seconds 1..86400 (default 60), fact importance 1..5 (default 3); non-numeric input falls back to the default instead of raising. Secrets are refused (pm_common.py:104-117; memory_store.py:474,682).

## 5. Automated Git Commit Hook Integration

Auto-capture commit messages tagged with `[decision]`, `[constraint]`, `[component]`, `[arch]`, or `[state]` into `memory.db`:

```bash
python hooks/git_hook.py
```
