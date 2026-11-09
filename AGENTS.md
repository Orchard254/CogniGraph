# CogniGraph Evidence-First Agent Protocol

For each task, identify the immediate decision, necessary evidence, observed
facts and remaining uncertainty. An OpenCode V2 native context injection may
already provide an evidence plan and safe source leads. Use it before adding
retrieval calls; do not block a legitimate source read or edit on three ritual
MCP calls.

When information is missing, inspect current source, callers and tests using
the smallest useful tool action. Do not treat a retrieved fact or an indexed
call edge as proof. A successful read is evidence of observation, not of
correct interpretation; a stored test-result fact is not an executed test.
Record actual outcomes and revise the plan as new evidence arrives.

The remaining tool reference below is for selective deeper investigations,
not a mandatory checklist for every prompt.

At the start of a task:
0. Prefer the injected evidence plan, or inspect current source directly. Use
   memory tools only when they answer a concrete open question.
1. Find the implementation and constraints that matter for the next action.
   Evidence retrieval is optional when direct source inspection is sufficient.
2. Before a risky change, investigate affected callers and available tests.
   If the graph lacks evidence, acknowledge the gap and inspect source.
3. After a structured IDE file read, revise the immediate next action;
   do not keep re-requesting the same information.
4. Verify with observed tool outputs, not stored claims of past test success.

LLM priming (context retrieval):
- Prefer `memory_get_context(query, agent_type=..., max_tokens=..., intent=...)` for a single Markdown briefing; it auto-detects intent, expands related constraints/decisions, and prioritizes critical facts.
- Before touching 2+ files together, call `memory_check_safety(file_paths)` for the dependency risk matrix. `memory_prepare_task` already embeds file impacts, dependency risks, and the test baseline.

Learning (task outcomes):
- Retrieval tools return a `retrieval_id`. Use `memory_rate_context(retrieval_id, ratings)` only when the evidence is attributable to specific returned fact IDs. Ratings: `helpful`, `irrelevant`, `incorrect`.
- Never mark facts helpful merely because they were retrieved, seen, or used in generated code. For a user report use `evidence_type='user'` only when the user explicitly supplied that judgement; for `evidence_type='test'` include the command, exit code 0, and current HEAD commit SHA, and make sure the result actually supports the rated fact.
- `evidence_type='agent'` is available for diagnostic annotations but **does not train ranking**. Report corrections through `memory_supersede` or `memory_flag_fact` when appropriate, even without a rating.

Learning (usage patterns):
- Every `memory_query` retrieval is logged. Facts retrieved repeatedly for similar queries are automatically boosted in future rankings (caps at 1.15x).
- Pass `agent_type` ('cursor' | 'claude' | 'opencode' | 'zed' | 'default') to `memory_query` / `memory_get_context` so retrieval-lane weights match the consuming model.

While working:
5. Write new facts IMMEDIATELY after verifying them (`memory_write` with `file_path`, `line_start`, `line_end`, and `code_hash` for provenance; `memory_write_batch` for several related facts — its item schema lists every accepted key, `content` required).
   - **Do not put a bare corpus count in a fact.** "409 active facts", "137 have file_path" — these rot the moment anything is written or deprecated, and they read as present-tense long after they stop being true. Measured 2026-09-28: four such facts had already drifted, including one written hours earlier. State the *conclusion*; if a number is the evidence, date it ("as measured on 2026-09-14 against 409 active non-stub facts") so it reads as a snapshot. Current numbers are one `memory_stats` call away.
   - Get `line_start`/`line_end`/`code_hash` from `memory_read_code` rather than computing them: each result carries `provenance_lines` and a `code_hash` over exactly that span, which is what `memory_cli.py verify` will later re-hash. Recomputing by hand is easy to get wrong (trailing newlines are part of the digest).
6. Correct outdated facts with `memory_supersede` — never write duplicates. If `memory_write` returns `DUPLICATE_DETECTED`, supersede the reported ID instead. Use `memory_deprecate` to mark a fact obsolete without replacement; it also accepts a list of ids for batch cleanups and reports each outcome, so one unknown id does not abandon the rest.
   - `memory_supersede` INHERITS `file_path`/`line_start`/`line_end`/`code_hash` from the fact it replaces. That is right when restating the same knowledge and wrong when the file was deleted or the code moved — you would keep a dead anchor plus a stale line range and hash that `memory_cli.py verify` re-hashes against the wrong span. Pass `inherit_provenance=false` to supply them yourself (blank clears), or `file_path=""` on its own to clear just the anchor.
   - Use `memory_cli.py locate <id>` when you need to know where an id lives: it reports whether it is in `memory`, in `episodic_memories`, or both, and flags an episodic row that is ACTIVE while its `memory` row is deprecated (which means NOT served — `memory.deprecated_at` is the liveness authority, episodic `status` is branch state).
7. Never store secrets, tokens, or credentials. The store refuses them automatically.

At the end of every session:
8. Call `memory_checkpoint_task` with the verified test output: `summary` (string), `test_baseline` (string), plus optional `components` and `decisions` (each a list of dicts with a required `content` key and optional `rationale`, `confidence`, `file_path`, `line_start`, `line_end`, `code_hash`, `tags`, `importance`), and optional `failures` (list of dicts with a required `content` key and optional `rationale`) for approaches that did not work.


9. If the coding task changed files and you have a prior CogniGraph `retrieval_id`, call `memory_record_task_outcome(retrieval_id, changed_files, test_evidence?)` after completing the task. Only include files actually changed; only report test commands, exit codes, and commit SHAs you observed. This creates candidate memories for review and does **not** train ranking.
10. Use `memory_task_outcome_review_queue` when useful to identify facts needing explicit review. Do not rate a memory as helpful solely because the task tests passed or the agent used it; use `memory_rate_context` only with attributable, fact-specific evidence or explicit user feedback.

Maintenance (weekly or when drift is suspected) runs via CLI/background, not MCP:
- `python memory_cli.py prune-stubs [--prune-root DIR] [--apply]` — deprecates whole-file `Source file:` stubs whose file no longer exists. Dry-run by default. Stubs are *inventory*, so a deleted file makes them false; curated facts with a dangling anchor are a **different** problem — null the `file_path` and keep the fact. The scope flag is `--prune-root`, not `--root`, because `--root` selects the database and would bootstrap a stray project for a typo.
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
- `memory_query(..., fields="id,category,content")` — narrows each result to the named keys, measured at 54-62% less payload on a routine query. `id` is always included, an unknown field returns an error with the valid list rather than a silently short result, a narrowed response reports `projected_fields`, and `explain=True` survives the narrowing. Reach for it on routine lookups; drop it when you need provenance fields like `file_path` or `code_hash`.
- `memory_flag_fact(fact_id, note)` — fire the moment a served fact misleads you; future briefings stamp the warning inline. Prefer `memory_supersede` when you know the corrected content.
- `memory_checkpoint_task` accepts `failures`: approaches tried that did NOT work. They are stored as negative-knowledge constraints so no future agent re-derives the dead end.
- If `memory_cli.py wire` reports `WIRED_WITH_ERRORS`, fix `config_errors` first - the listed config was not written (wire.py:207-222).
<!-- cognigraph-template sha256:dfb65579e55a6931cbc192b1389931d38ba4615d2de71544a6d2f4e23ed84a54 -->
