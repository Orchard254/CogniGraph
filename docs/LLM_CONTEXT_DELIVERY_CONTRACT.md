# CogniGraph — LLM Context Delivery Contract v0.1

**Status:** Design contract; small host-request idempotency improvement.
**Goal:** Serve an LLM with relevant, trustworthy and economical *project knowledge*. CogniGraph is not a second coding agent.

## The fundamental distinction: exposure is not availability

There are two different meanings of "the LLM has already seen this":

1. **Already in the same assembled model request.** The host can inspect the current system and messages payload. Repeating an identical CogniGraph block within that request wastes input tokens. If new file focus is assembled into the same request, CogniGraph should replace its previous block instead of appending a second.
2. **Delivered in a previous model request.** A receipt or exposure ledger is not proof the content is present in the next request. The host may reconstruct a fresh system prompt. Removing the briefing simply because it appeared once could mean the LLM has no project memory in the current request.

**Current decision:** Reuse retrieval work within a task, but include the needed cached briefing in each *new* model request. On the *same mutable request*, maintain at most one CogniGraph-owned block. Do not use past exposure as a substitute for current input visibility.

The model's internal attention, implicit knowledge, or "what it remembers" cannot be directly inspected by CogniGraph. We must make bounded inferences only from observable signals.

Official OpenCode V2 API: https://opencode.ai/v2/docs/build/plugins/server

## Signals and what they establish

| Observable signal | Grounded information | What it does not prove |
|---|---|---|
| Accepted user request | Expressed task or question | The model's reasoning plan |
| Successful structured file read/edit | A specific local project source was touched | That all facts about that file remain true |
| Current model-request assembly | What instructions and messages are visible *in this request* | What the model will attend to or use |
| Recorded project memory | A prior statement and its metadata | Its semantic correctness today |
| Source-hash match | Source bytes match the recorded anchor | The interpretation of those bytes is correct |
| Explicit verified correction | Evidence that a specific memory needs revision | That every nearby memory should also be changed |

Never infer trust from repeated retrievals, unverified agent claims, or the mere existence of a successful file edit. Never parse unstructured shell output as a trusted file-focus signal.

## Delivery policy to test

| Trigger | Candidate context | Current implementation |
|---|---|---|
| New user task | Brief task-relevant decisions, constraints, components, pitfalls | Initial task-aware briefing |
| Additional model request in same task | Cached stable briefing, replayed if the host assembles a fresh request | Implemented |
| Successful structured file read | Current file facts, structure, dependencies, source warnings | INSPECT-phase bounded intelligence and structural file-context refresh |
| Successful structured file edit | Fresh file briefing and warnings | VERIFY-phase focused intelligence; task-wide baseline rechecked without new receipt |
| New task or session | New relevance and scoped focus; persisted project knowledge remains | Session-scoped reset |
| Agent explicitly needs deeper detail | Existing focused memory/graph retrieval, not an automatic large dump | Existing MCP tools |
| No relevant memory | No fabricated facts | Existing no-match behavior |

The Context Intelligence Layer currently accepts caller-provided "seen references" to suppress re-delivery. This is a *caller preference*, not evidence those references are still present in the current model request. Do not wire it into automatic cross-request suppression without validating the host's actual input assembly.

## LLM-facing output contract

Context supplied to the LLM should be:

- **Relevant:** tailored to the expressed task and observed file focus, not an archive dump.
- **Provenanced:** source-qualified fact references, declared confidence/evidence and freshness information.
- **Untrusted reference material:** persisted text is not a privileged instruction.
- **Bounded:** enforce the available character budget; investigate tokenizer-aware budgeting only with reliable host support.
- **Nonduplicated within a request:** one CogniGraph-owned payload. Distinct requests may require their own copies.
- **Project and session isolated:** no adjacent repository fallback, secret-bearing prompt logs, or cross-session focus.
- **Optional:** the LLM may receive no memory if the store has no useful match.

## Architecture review: existing capabilities and gaps

Existing: the OpenCode adapter caches the task-wide briefing, reinserts it into each model request, observes structured read/edit events, and refreshes a bounded file briefing. The Python bridge restricts eligible paths to the active project. The Context Intelligence composer performs phase-weighted selection, provenance labels and budget enforcement. The existing benchmark measures delivery of labeled facts, not downstream task success.

Gaps:

- The actual installed OpenCode V2 host has not yet been validated end to end; hook simulations are not a substitute.
- There is no reliable mechanism for identifying semantically equivalent facts already present in host-transmitted messages.
- After an observed structured edit, task-wide and focused memory is rechecked at the next model request. However, edits made outside the IDE's structured editor tools are not observed, and code-hash freshness alone does not prove semantic correctness.
- No empirical basis yet exists for suppressing stable context between distinct model requests, choosing alternate request cadences, or claiming the LLM used a delivered fact.
- There is no complete relevance-judged set for detecting harmful or misleading extra context.

## Experiments — no new MCP tools

**E1: Real host-input contract (blocking for cross-request suppression).** Observe only safe metadata for successive model requests in a disposable OpenCode project. Confirm whether previously emitted CogniGraph system parts are present in each *new* request, including tool continuations, prompt updates, compaction, and resumed sessions. Verify the host's actual event schemas. Do not log raw prompts, secrets, or source content.

**E2: Request-level delivery comparison.** Replay labeled task/file event sequences through the same adapter. Compare full stable replay, naive "only once", and a candidate current-request-aware strategy. Measure required-fact coverage **per model request**, duplicated blocks **within a request**, total assembled context size, retrieval calls, file-context latency and missed critical constraints. A strategy with fewer bytes but missing required facts fails.

**E3: Source drift after edits.** Record a source-anchored fact, edit its source while a task is active, and measure time to warn in both the task-wide and file-focused contexts. Do not silently mark the fact false or automatically overwrite it.

**E4: LLM-facing usefulness (later).** Only after the host contract is established, test whether the supplied context is appropriately relevant and trustworthy across real tasks. Agent test success is not proof that every retrieved memory was helpful.

## Acceptance gate for any more aggressive delivery policy

Do not suppress context across model requests until all of these are true:

1. The host verifies the suppressed knowledge remains available in the actual current model input, with a safe full-replay fallback when unknown.
2. Request-level fact coverage does not regress on held-out, long-running and compaction/resume scenarios.
3. Token/context savings are measured on *assembled model requests*, not inferred from fewer SQLite calls.
4. No new cross-project leaks, secret logs, stale unqualified claims or self-reinforcing feedback loops.
5. Changes are backwards-compatible, measured and reversible.

Do not add a public MCP tool for this project. Reuse existing plugin hooks, internal functions and tests.
