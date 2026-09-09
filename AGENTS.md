# Mandatory Project Memory Protocol — Self-Prompting for Human Language

0. Self-prompt (on any human message like "hey fix planner"):
   intent = lower(strip filler "hey/please/can you/fix/add") + keywords
   file_paths = user list or top 3 file_path from memory_query_hybrid(intent, 5) if none given
   Then STOP — no read/TodoWrite before memory_stats → memory_query_hybrid(intent) → memory_prepare_task(intent, file_paths)

At the start of EVERY session:
1. Call `memory_stats` to confirm the database location and counts.
2. Call `memory_query_hybrid` BEFORE reading or editing any file. If memory has the answer, trust it — do not re-explore code you already know. Pass `intent` (fix/implement/refactor/optimize/...) so ranking boosts the right categories; it is auto-detected from the text if omitted.
3. Call `memory_prepare_task(intent, file_paths)` before writing any code.
4. Call `memory_get_file_context(file_path)` before modifying any specific file — it now includes the file's code structure (functions/classes/imports) from the structural index.

LLM priming (context retrieval):
- Prefer `memory_get_context(query, intent=...)` for a single Markdown briefing; it auto-detects intent, expands related constraints/decisions, and prioritizes critical facts.
- Use `memory_get_context_optimized(query, agent_type=...)` when the consuming model is known (cursor | claude | opencode | zed | default) for agent-tuned token budgets and priority orders.
- Before touching 2+ files together, call `memory_get_dependency_map(file_paths)` to see internal edges, external dependents, shared constraints, and high-risk files.

Phase 3 learning (usage patterns & prediction):
- Every `memory_query_hybrid` retrieval is logged. Facts retrieved repeatedly for similar queries are automatically boosted in future rankings (caps at 1.15x), so the memory gets better at priming the more it is used.
- Pass `agent_type` ('cursor' | 'claude' | 'opencode' | 'zed' | 'default') to `memory_query_hybrid` / `memory_get_context_optimized` so retrieval-lane weights match the consuming model.
- After receiving initial context, call `memory_suggest_context(current_fact_ids)` to preemptively pull in co-retrieved facts and cut later round-trips.
- Call `memory_usage_stats` to see which facts are load-bearing and which intents dominate.

While working:
5. Write new facts IMMEDIATELY after verifying them (`memory_write` with `file_path`, `line_start`, `line_end`, and `code_hash` for provenance).
6. Correct outdated facts with `memory_supersede` — never write duplicates. If `memory_write` returns `DUPLICATE_DETECTED`, supersede the reported ID instead.
7. Record architectural relationships with `memory_link_facts` (`depends_on`, `implements`, `conflicts_with`, `relates_to`).
8. Never store secrets, tokens, or credentials. The store refuses them automatically.

At the end of every session:
9. Call `memory_checkpoint_task(summary, components, decisions, test_baseline)` with the verified test output.
10. If more than 5 micro-state facts accumulated: call `memory_consolidate`.

Maintenance (weekly or when drift is suspected):
- `memory_scan_provenance(project_root)` — detect code drift across anchored facts.
- `memory_audit` — detect near-duplicate facts.
- `memory_prune(dry_run=True)` — preview stale facts, then apply only if the list is sane.
- `memory_optimize_db` — SQLite maintenance.

Structural layer (tree-sitter, Python/JS/TS/TSX):
- `structural_extract.py:extract_structures(file_path)` — parse file into function/class/method/import nodes
- `memory_store.sync_structural_nodes(file_path, nodes)` — upsert nodes, prune stale, returns upserted/pruned counts
- `memory_store.link_facts_to_structural_nodes(file_path)` — link facts to nodes via line-range overlap
- Run `bootstrap.scan` or `sync_structural_nodes` after editing source files to keep structural index current; `memory_query_hybrid` automatically uses the structural lane (no extra call needed) but requires up-to-date nodes.

Code graph (one-hop queries — ask these mid-reasoning instead of re-reading files):
- `memory_who_calls(symbol)` — definitions, callers, and importing files for a symbol, from AST ground truth. Accepts `query_hybrid` or `MemoryStore.write`.
- `memory_project_shape()` — indexed file inventory and code-graph size; call once early for orientation.
- `memory_flag_fact(fact_id, note)` — fire the moment a served fact misleads you; future briefings stamp the warning inline. Prefer `memory_supersede` when you know the corrected content.
- `memory_checkpoint_task` accepts `failures`: approaches tried that did NOT work. They are stored as negative-knowledge constraints so no future agent re-derives the dead end.
- If `memory_cli.py wire` reports `WIRED_WITH_ERRORS`, fix `config_errors` first - the listed config was not written (wire.py:207-222).
