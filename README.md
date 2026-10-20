# CogniGraph MCP Server

An enterprise-grade, state-of-the-art persistent memory system for AI coding agents (**Google Antigravity, Cursor, OpenCode, Claude Desktop**) — CogniGraph.

It provides cross-session, vendor-neutral memory for project components, architectural decisions, constraints, file dependency graph edges, code-anchored provenance, and state checkpoints.

Coding conventions and the context-building pipeline are recorded in project memory — query `memory_get_context` for "engineering conventions" or "context building" rather than reading a static doc that would drift.

---

## Key Capabilities

1. **Hybrid RRF Search Engine**: Combines SQLite FTS5 BM25 keyword matching with term-vector similarity via Reciprocal Rank Fusion ($RRF = \frac{1}{5 + rank_{bm25}} + \frac{1}{5 + rank_{vec}}$).
2. **Graph-Relational Knowledge Engine (`memory_edges`)**: Directed graph edges (`depends_on`, `implements`, `conflicts_with`, `relates_to`) traversed via SQLite Recursive Common Table Expressions (CTEs).
3. **Automatic Context Expansion (`memory_get_context`)**: Searching for a topic automatically expands connected graph relationship trees into a single context briefing.
4. **File Impact Analysis (`memory_get_file_context`)**: Pre-edit briefing for any source file listing its stored components, decisions, constraints, and all linked dependent files.
5. **Multi-File Refactor Safety Matrix (`memory_check_safety`)**: Scans batches of target files before refactoring to detect cross-file breaking risks.
6. **Code-Anchored Self-Healing Provenance**: Anchors line numbers and SHA256 snippet hashes with neighborhood shift detection that self-heals line range shifts automatically. Exposed via the CLI (`memory_cli.py scan-provenance`, `memory_cli.py verify`), not as an MCP tool.
7. **Swarm Concurrency & Advisory Locks**: Optimistic Concurrency Control (`version` checking) + advisory locks (`memory_locks`) preventing race conditions across multi-agent swarms.
8. **Interactive Visual Web UI**: Standalone, self-contained HTML visualizer ([`memory_graph.html`](file:///.cognigraph/memory_graph.html)) for exploring the knowledge graph in 2D, generated via `memory_cli.py export-ui`.
9. **Priming Eval Harness (`eval_priming.py`)**: Scores retrieval quality for realistic coding tasks — whether the facts an LLM needs (by content or file anchor) land in the top-K of `query_hybrid`. Run `python eval_priming.py` to regress-check priming quality after ranking changes.
10. **Code Graph & Trust Layer (`structural_edges`)**: AST-ground-truth call and import edges across the codebase, rebuilt on every structural sync. One-hop mid-reasoning queries via `memory_who_calls` ("who calls X? who imports Y?"), project orientation via `memory_project_shape`, and a trust layer: unmapped files are flagged explicitly, flagged/unverified facts are stamped inline at priming time (`memory_flag_fact` correction hook), and `memory_checkpoint_task` captures failed approaches as negative-knowledge constraints.

---

## Architecture: the two-table intake contract

The database has two fact stores with distinct roles:

- **`memory`** — the live intake table. Every `memory_write` / `memory_write_batch` / `memory_checkpoint_task` lands here, along with all lifecycle operations (supersede, deprecate, consolidate, prune, provenance verification, graph edges).
- **`episodic_memories`** — the historical archive created by the genesis migration on 2026-08-24 (108 legacy facts), plus the branch/revision bookkeeping managed via `memory_cli.py branch create|discard|list` and `memory_cli.py promote` (branch_promotion.py). It is read-only for intake.

Retrieval (`memory_query`, `memory_get_context`) fuses both stores through 7-lane RRF, so agents see one unified memory. New facts are intentionally memory-bound by contract; episodic_memories is not an intake target.

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
#    or specify directly: antigravity | cursor | windsurf | claude | zed | roo | cline | opencode | kiro | vscode | zcode | generic

# 3. Ingest your project's context into memory
.\venv\Scripts\python.exe memory_cli.py bootstrap --root C:\path\to\your\project --apply
```

Setup detail: setup.ps1 creates venv/, installs requirements.txt, then runs the full suite (setup.ps1:16-29). Re-run manually with .\venv\Scripts\python.exe -m pytest -q --ignore=scratch. The collected test modules at repo root include test_llm_priming, test_memory_cli, test_phase3_priming, test_server_tools, test_watcher and test_wire_bootstrap; pytest --collect-only -q --ignore=scratch collected 205 tests as of 2026-09-10, contributed by test_llm_priming, test_phase3_priming, test_wire_bootstrap, test_memory_cli, test_watcher and test_server_tools (scratch/ holds a live-watcher probe excluded from the suite).

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
1. Call `memory_stats` to load existing project context.
2. Call `memory_get_context(query)` before asking broad architecture questions.
3. Call `memory_get_file_context(file_path)` BEFORE editing any source file.
4. Call `memory_write` or `memory_write_batch` when learning new facts (include `file_path` and `line_start`/`line_end` for provenance).
5. Call `memory_supersede` (not a new `memory_write`) when correcting an outdated fact.
6. Record architectural dependencies between facts via the CLI (`memory_cli.py link <from_id> <to_id> <edge_type>`); linking is not exposed as an MCP tool.
```

---

## 3. Exposed MCP Tools

<!-- tool-table:start auto-generated -->
The server registers 24 tools.

| Tool | Parameters | Description |
|---|---|---|
| `memory_write` | `category`, `content`, `rationale?`, `confidence?`, `session_id?`, `file_path?`, `tags?`, `importance?`, `line_start?`, `line_end?`, `code_hash?`, `git_commit_sha?`, `evidence_type?`, `source_ref?`, `created_by_agent?`, `branch_id?`, `trust_level?` | Record a durable fact this session learned that future sessions will need: decisions, constraints, gotchas, where components live. Secrets are refused. |
| `memory_write_batch` | `facts` | Atomic batch form of memory_write for several related facts. |
| `memory_query` | `text`, `category?`, `tag?`, `limit?`, `expand_graph?`, `session_id?`, `repo_id?`, `intent?`, `agent_type?`, `explain?`, `fields?` | Search project memory — decisions, constraints, components, past failures — via hybrid lexical + semantic + code-structure search. Use when you need project knowledge you don't have. |
| `memory_get_context` | `query`, `agent_type?`, `max_tokens?`, `limit?`, `intent?` | Token-budgeted Markdown briefing on a topic, formatted for your agent type and token ceiling. Use before working in an unfamiliar area. |
| `memory_get_file_context` | `file_path` | Call while planning an approach to a file — and before editing it: its symbols, anchored facts, callers, and call-graph edges. |
| `memory_check_safety` | `file_paths` | Call before a multi-file refactor: dependency risk matrix across the files you're about to change. |
| `memory_check_security` | `file_paths` | Heuristic security scan: flag sensitive/PII/dangerous patterns (password, secret, token, eval, exec, shell, SQL interpolation) with file:line, plus cross-file impact. |
| `memory_acquire_lock` | `resource`, `locked_by`, `ttl_seconds?` | Acquire a named lock on a resource for multi-agent coordination. Returns success boolean. |
| `memory_release_lock` | `resource`, `locked_by` | Release a named lock previously acquired by locked_by. |
| `memory_validate_constraints` | `file_paths` | Validate stored constraints against the files about to be edited; returns constraint_checks and verdict. |
| `memory_check_hygiene` | `project_root?`, `ci_status?` | Commit/push-discipline status for the working tree: cleanliness, ahead/behind, stale branches. Local git only, read-only; optional ci_status records external CI result. |
| `memory_supersede` | `old_id`, `new_content`, `rationale?`, `confidence?`, `session_id?`, `file_path?`, `tags?`, `expected_version?`, `inherit_provenance?`, `line_start?`, `line_end?`, `code_hash?`, `git_commit_sha?` | Replace an outdated fact with corrected content. Use when you discover memory that is wrong. |
| `memory_deprecate` | `id`, `reason?` | Mark one fact (or a batch of ids) obsolete without replacement. |
| `memory_resolve_conflict` | `keep_id`, `deprecate_id`, `rationale?` | Resolve a conflict between two facts: deprecate the losing fact and |
| `memory_stats` | — | Memory health: active/deprecated counts, category breakdown, edges. Call at session start per AGENTS.md. |
| `memory_prepare_task` | `intent`, `file_paths?`, `limit?` | REQUIRED before writing or modifying any code in this repository. Returns the task briefing: relevant decisions, constraints, component facts, file-level impacts, cross-file dependency risks, and the current verified test baseline. |
| `memory_checkpoint_task` | `summary`, `components?`, `decisions?`, `test_baseline?`, `failures?`, `unfinished?`, `blockers?`, `acceptance_criteria?`, `verified?`, `evidence_type?`, `source_ref?` | Call immediately after a task is verified complete (tests green). Records new components, decisions, the test baseline, and failed approaches as durable memory for future sessions. |
| `memory_who_calls` | `symbol` | Where a symbol is defined, every call site, and importing files. Ask mid-reasoning while planning an approach — and before renaming or changing a signature. |
| `memory_search_symbols` | `query`, `limit?` | Fuzzy/ambiguous symbol discovery: LIKE-search structural_nodes, returns file:line:name:type hits (limit capped). |
| `memory_who_inherits` | `symbol`, `max_depth?` | Which classes inherit from this base class (exact), transitively. No LIKE false positives. |
| `memory_blast_radius` | `symbol`, `class_name?`, `file_path?`, `max_depth?`, `direction?`, `file_detail?`, `max_symbols_per_file?` | Transitive blast radius: who depends on this symbol, transitively. Call while planning an approach or estimating effort, before making changes — not just as a pre-edit gate. |
| `memory_read_code` | `items`, `max_lines_per_item?`, `max_total_chars?`, `context?` | Read scoped source lines by symbol or explicit file range, batched in one call. Request order is preserved in results; batch budget cutoffs return per-item errors (not partial content) so re-request those items in a smaller batch. |
| `memory_project_shape` | — | Lay of the land: indexed files, top modules by definition density, call/import edge totals. |
| `memory_flag_fact` | `fact_id`, `note?`, `flag_type?` | Flag a fact as misleading or unverified so future briefings carry a warning. Use when memory looks wrong but you lack the correction. |
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

# Timestamped full-DB snapshot (WAL-safe, integrity-checked; prunes beyond --keep)
python memory_cli.py backup --tag manual --keep 8
```

---

## 4b. Reliability notes (verified against current code)

- wire report schema (wire.py:207-222): status is WIRED, or WIRED_WITH_ERRORS when config_errors is non-empty; report carries config_files, backups (rotating .bak/.bak.1/.bak.2), config_errors, agents_md (created/up_to_date/updated_clean/skipped_customized_update_available/...), mcp_snippet, and next_steps (bootstrap dry-run, --apply, restart, plus Fix config errors first lines when errors exist). On WIRED_WITH_ERRORS, fix config_errors first - the listed config was not written.
- Corrupt-config refusal (wire.py:64-90): an existing config that is not valid JSON is preserved as <name>.corrupt-<timestamp>.bak and reported in config_errors; it is never silently overwritten (atomic tmp + fsync + os.replace).
- --root DB binding (memory_cli.py:163-174): without explicit --db, any command with --root binds to <root>/.cognigraph/memory.db so facts never land in the CWD fallback store; wire never touches the DB (store is constructed lazily for other commands).
- Concurrency (memory_store.py:48,1095-1123; server.py:25-40): SQLite PRAGMA journal_mode=WAL + busy_timeout=5000; get_store() returns a per-thread MemoryStore (one connection per thread on the same WAL DB); each MCP tool call gets a fresh store that is always closed (no leaked fd/WAL handles).
- Git probes (bootstrap.py:309-327; wire.py:26-43; hooks/git_hook.py:29-30): every git/python child probe runs with stdin=subprocess.DEVNULL and a timeout (15 s) so an inherited stdio pipe under the MCP server cannot stall indefinitely.
- Input clamps (server.py:42-71; mirrored in memory_cli.py:218-234): limit 1..100 (context/prepare-task paths cap at 50, graph depth at 10, bootstrap max_facts at 1000), lock ttl_seconds 1..86400 (default 60), fact importance 1..5 (default 3); non-numeric input falls back to the default instead of raising. Secrets are refused (pm_common.py:104-117; memory_store.py:474,682).
- Timestamped DB backups (pm_backup.py): weekly auto-maintenance takes a WAL-safe snapshot via the SQLite backup API before any mutation, and corpus_hygiene --apply / genesis_migrate snapshot first; backups named memory.db.backup-<tag>-<ts>.bak are integrity-checked and pruned to the newest 8 (keep configurable), historical ad-hoc *.bak files untouched (test_backup.py).

## 5. Automated Git Commit Hook Integration

Auto-capture commit messages tagged with `[decision]`, `[constraint]`, `[component]`, `[arch]`, or `[state]` into `memory.db`:

```bash
python hooks/git_hook.py
```
