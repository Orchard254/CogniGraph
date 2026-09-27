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
5. Write new facts IMMEDIATELY after verifying them (`memory_write` with `file_path`, `line_start`, `line_end`, and `code_hash` for provenance; `memory_write_batch` for several related facts — its item schema lists every accepted key, `content` required).
   - Get `line_start`/`line_end`/`code_hash` from `memory_read_code` rather than computing them: each result carries `provenance_lines` and a `code_hash` over exactly that span, which is what `memory_cli.py verify` will later re-hash. Recomputing by hand is easy to get wrong (trailing newlines are part of the digest).
6. Correct outdated facts with `memory_supersede` — never write duplicates. If `memory_write` returns `DUPLICATE_DETECTED`, supersede the reported ID instead. Use `memory_deprecate` to mark a fact obsolete without replacement; it also accepts a list of ids for batch cleanups and reports each outcome, so one unknown id does not abandon the rest.
   - `memory_supersede` INHERITS `file_path`/`line_start`/`line_end`/`code_hash` from the fact it replaces. That is right when restating the same knowledge and wrong when the file was deleted or the code moved — you would keep a dead anchor plus a stale line range and hash that `memory_cli.py verify` re-hashes against the wrong span. Pass `inherit_provenance=false` to supply them yourself (blank clears), or `file_path=""` on its own to clear just the anchor.
   - Use `memory_cli.py locate <id>` when you need to know where an id lives: it reports whether it is in `memory`, in `episodic_memories`, or both, and flags an episodic row that is ACTIVE while its `memory` row is deprecated (which means NOT served — `memory.deprecated_at` is the liveness authority, episodic `status` is branch state).
7. Never store secrets, tokens, or credentials. The store refuses them automatically.

At the end of every session:
8. Call `memory_checkpoint_task` with the verified test output: `summary` (string), `test_baseline` (string), plus optional `components` and `decisions` (each a list of dicts with a required `content` key and optional `rationale`, `confidence`, `file_path`, `line_start`, `line_end`, `code_hash`, `tags`, `importance`), and optional `failures` (list of dicts with a required `content` key and optional `rationale`) for approaches that did not work.

Maintenance (weekly or when drift is suspected) runs via CLI/background, not MCP:
- `backup`, `scan-provenance`, `audit`, `pollution`, `prune`, `optimize`, `backfill`, `export`, `bootstrap`, `branch` via `memory_cli.py`; auto-maintenance also runs on server startup and takes a timestamped full-DB backup first (keep 8). Migration scripts snapshot automatically before mutating.
- `python scripts/check_corpus_truth.py` — exits non-zero when an active fact contradicts the running system. Two checks: `tool_count` (a fact claims an MCP tool count the live registry disagrees with) and `tool_reference` (a fact *prescribes calling* a `memory_*` identifier that is not a tool, CLI command, module, or table). Run it after any change to the tool surface, and before concluding a corpus cleanup is finished. `.cognigraph/` is gitignored so this cannot be a CI job; run it locally.
- `python memory_cli.py audit --disagreements-only` — near-duplicate pairs whose facts *contradict* each other, skipping the benign co-topic ones. Plain `audit` classifies every pair `agree` / `disagree` / `unclear`; "unclear" means no load-bearing assertion could be compared, not that the pair is fine.

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
- `memory_query(..., explain=True)` (or `memory_cli.py query --hybrid --explain`) — attaches a per-result `lanes` trace (which of the 7 lanes found it, at what rank and weight) and `fused_score`. Opt-in: the fused number is otherwise not decomposable across lane weights, intent boosts, importance, recency and file-anchor boosts. Use it to verify a ranking change directly instead of inferring the cause from eval deltas.
- `memory_flag_fact(fact_id, note)` — fire the moment a served fact misleads you; future briefings stamp the warning inline. Prefer `memory_supersede` when you know the corrected content.
- `memory_checkpoint_task` accepts `failures`: approaches tried that did NOT work. They are stored as negative-knowledge constraints so no future agent re-derives the dead end.
- If `memory_cli.py wire` reports `WIRED_WITH_ERRORS`, fix `config_errors` first - the listed config was not written (wire.py:207-222).
<!-- cognigraph-template sha256:0c43220201f693281c4882481a93cdca88a47bbc1caf5c898522e5ed55f2824d -->
