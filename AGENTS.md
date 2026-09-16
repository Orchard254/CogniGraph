# Mandatory CogniGraph Protocol — Self-Prompting for Human Language

0. Self-prompt (on any human message like "hey fix planner"):
   intent = lower(strip filler "hey/please/can you/fix/add") + keywords
   file_paths = user list or top 3 file_path from memory_query(intent, 5) if none given
   Then STOP — no read/TodoWrite before memory_stats → memory_query(intent) → memory_prepare_task(intent, file_paths)

At the start of EVERY session:
0. If cognigraph tools are not available at session start, STOP and tell the user the memory server is not connected. Do not proceed without memory.
1. Call `memory_stats` to confirm the database location and counts.
2. Call `memory_query` BEFORE reading or editing any file. If memory has the answer, trust it — do not re-explore code you already know. Pass `intent` (fix/implement/refactor/optimize/...) so ranking boosts the right categories; it is auto-detected from the text if omitted.
3. Call `memory_prepare_task(intent, file_paths)` before writing any code.
4. Call `memory_get_file_context(file_path)` while planning an approach to a file, and before modifying it — it now includes the file's code structure (functions/classes/imports) from the structural index.

LLM priming (context retrieval):
- Prefer `memory_get_context(query, agent_type=..., max_tokens=..., intent=...)` for a single Markdown briefing; it auto-detects intent, expands related constraints/decisions, and prioritizes critical facts.
- Before touching 2+ files together, call `memory_check_safety(file_paths)` for the dependency risk matrix. `memory_prepare_task` already embeds file impacts, dependency risks, and the test baseline.

Learning (usage patterns):
- Every `memory_query` retrieval is logged. Facts retrieved repeatedly for similar queries are automatically boosted in future rankings (caps at 1.15x).
- Pass `agent_type` ('cursor' | 'claude' | 'opencode' | 'zed' | 'default') to `memory_query` / `memory_get_context` so retrieval-lane weights match the consuming model.

While working:
5. Write new facts IMMEDIATELY after verifying them (`memory_write` with `file_path`, `line_start`, `line_end`, and `code_hash` for provenance; `memory_write_batch` for several related facts).
6. Correct outdated facts with `memory_supersede` — never write duplicates. If `memory_write` returns `DUPLICATE_DETECTED`, supersede the reported ID instead. Use `memory_deprecate` to mark a fact obsolete without replacement.
7. Never store secrets, tokens, or credentials. The store refuses them automatically.

At the end of every session:
8. Call `memory_checkpoint_task` with the verified test output: `summary` (string), `test_baseline` (string), plus optional `components` and `decisions` (each a list of dicts with a required `content` key and optional `rationale`, `confidence`, `file_path`, `line_start`, `line_end`, `code_hash`, `tags`, `importance`), and optional `failures` (list of dicts with a required `content` key and optional `rationale`) for approaches that did not work.

Maintenance (weekly or when drift is suspected) runs via CLI/background, not MCP:
- `backup`, `scan-provenance`, `audit`, `prune`, `optimize`, `backfill`, `export`, `bootstrap`, `branch` via `memory_cli.py`; auto-maintenance also runs on server startup and takes a timestamped full-DB backup first (keep 8). Migration scripts snapshot automatically before mutating.

Structural layer (tree-sitter, Python/JS/TS/TSX):
- `structural_extract.py:extract_structures(file_path)` — parse file into function/class/method/import nodes
- `memory_store.sync_structural_nodes(file_path, nodes)` — upsert nodes, prune stale, returns upserted/pruned counts
- `memory_store.link_facts_to_structural_nodes(file_path)` — link facts to nodes via line-range overlap
- Run `bootstrap.scan` or `sync_structural_nodes` after editing source files to keep structural index current; `memory_query` automatically uses the structural lane (no extra call needed) but requires up-to-date nodes.

Code graph (one-hop queries — ask these mid-reasoning instead of re-reading files):
- `memory_blast_radius(symbol)` — transitive blast radius ("what breaks if I change this"): call this while planning an approach or estimating effort, before making changes — not just as a pre-edit gate.
- `memory_who_calls(symbol)` — definitions, callers, and importing files for a symbol, from AST ground truth. Accepts `query` or `MemoryStore.write`. Ask mid-reasoning while planning, not just before a rename.
- `memory_who_inherits(symbol)` — which classes inherit from a base class, exact and transitive, from AST ground truth. Ask mid-reasoning while planning, not just before a hierarchy change.
- `memory_project_shape()` — indexed file inventory and code-graph size; call once early for orientation.
- `memory_flag_fact(fact_id, note)` — fire the moment a served fact misleads you; future briefings stamp the warning inline. Prefer `memory_supersede` when you know the corrected content.
- `memory_checkpoint_task` accepts `failures`: approaches tried that did NOT work. They are stored as negative-knowledge constraints so no future agent re-derives the dead end.
- If `memory_cli.py wire` reports `WIRED_WITH_ERRORS`, fix `config_errors` first - the listed config was not written (wire.py:207-222).
<!-- cognigraph-template sha256:42cc909e4c37db07a3a671bc48a40aefff2854d1b927ec99623a9c76abc24ab9 -->
