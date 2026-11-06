# Context Intelligence Layer v1

CogniGraph now offers a small, task-aware briefing designed to answer:
**what does this coding agent need to know for its next action?**

This layer operates on the existing hybrid retrieval engine. It does **not**
train a model, infer proof from a completed task, rewrite project facts, or
promote retrieved memories simply because they were seen.

## Usage

The MCP tool `memory_get_intelligent_context` accepts:

```json
{
  "task": "Fix login session restoration after page refresh",
  "phase": "debug",
  "focus_files": ["src/auth/session.py"],
  "seen_refs": ["memory:42"],
  "max_tokens": 800,
  "limit": 8,
  "agent_type": "opencode"
}
```

Supported phases: `orient`, `inspect`, `edit`, `debug`, `verify`,
and `handoff`. The aliases `plan`, `read`, `implement`, `fix`,
`test`, and `complete` are accepted.

The result includes a `context_markdown` briefing and structured metadata:
`selected` (source-qualified fact IDs and confidence), `warnings`,
`phase`, `retrieval_id`, `budget_used_chars`, and `omitted_seen`.
Source-qualified refs (`memory:42` versus `episodic:42`) prevent
collisions between the two historical ID spaces.

For OpenCode V2, the initial task briefing now uses this composer
automatically. File-specific briefings and edit-triggered refreshes continue
to use the existing context system, avoiding changes to the structural
call-graph explanation. The new MCP tool supports explicit phase changes.
Automatic phase detection from every IDE tool step is **not yet implemented**.

## How context is selected

1. Ask the existing seven-lane hybrid retriever for candidates, without
   logging a retrieval or changing access counters during candidate discovery.
2. Add a bounded number of active curated facts anchored exactly to the
   requested files. No general filesystem scan is performed.
3. Apply transparent, deterministic phase-category preferences, a boost for
   exact file focus, and small provenance/confidence adjustments.
4. Deprioritize facts carrying correction flags or source-hash drift warnings;
   if selected, label them as requiring review.
5. Omit repeated source-qualified references in `seen_refs`, skip duplicate
   content and suspected secrets, and fit whole fact entries within the
   requested strict character ceiling.
6. **Only after composing the briefing**, create one usage receipt containing
   only the selected live `memory` IDs. Historical episodic refs are displayed
   with their source label but are not mistaken for live fact IDs.

No-feedback behavior of the underlying hybrid retriever is unchanged.
The new composer has its own output selection policy, which must be evaluated
separately against task-level relevance judgments.

## Proactive inspection leads (first increment)

The existing task briefing now proactively identifies **where to inspect next**,
without asking the LLM to discover or invoke a memory tool:

- From facts **actually included** in the briefing, it proposes up to three
  existing, project-local source anchors or one-hop linked files. Links are
  explicitly described as *stored relationships*, not verified code dependencies.
- With a known IDE file focus, it flags when no **displayed** fact is directly
  anchored to that file. This is a gap in the **delivered briefing**, never a
  claim that the project has no relevant knowledge.
- In edit/debug phases, it can flag when no constraint fact was delivered.
  It does not infer that no constraints exist in source code or archived memory.
- Symlink escapes, missing files, hidden paths, dependency directories and
  suspicious paths are excluded from proactive inspection targets.
- Suggestions and gaps appear only when their **entire line fits** the same
  character budget; \`inspection_targets\` and \`knowledge_gaps\` report only
  rendered entries. The \`selected\` list and retrieval receipts still count
  only displayed memory facts, never neighboring graph facts.
- No new tool, model, table, writable state, ranking weight or retrieval
  candidate set is introduced. The OpenCode initial task briefing already
  uses this composer, so this first increment reaches the LLM automatically.

This is *bounded evidence-guided discovery*, not a complete project-wide
planner. It cannot prove which files a task needs, reason about unseen
dependencies, or infer source truth from an old graph edge. The next phase
should validate relevance on labeled tasks and only then consider improved
IDE event-phase delivery and incremental gap resolution.

## Evidence and trust

For each selected memory the briefing labels category, source, declared
confidence, recorded evidence type, source-freshness status, and any associated
path. A matching SHA-256 means the *source bytes* still match; it does not
independently prove that the memory statement is correct.

A flagged or outdated memory is never silently fixed, marked true, or
automatically removed. Missing hashes remain unverified. The agent should
inspect current source and tests before relying on an important claim.

The briefing is marked as **reference data, not instructions**. Memory
contents and paths are locally sanitized for formatting, but developers
should continue to treat stored text as untrusted data.

## Resource controls and limitations

- `max_tokens` accepts 128–3,000 and is converted to an approximate
  four-character-per-token ceiling. The composer enforces 512–12,000
  characters exactly; it does not guarantee a tokenizer-exact token count.
- At most 12 selected facts, 40 hybrid candidates, four file-focus paths,
  100 already-seen source-qualified refs, and eight direct facts per file.
- Source-hash checks inherit the bounded, project-local implementation in
  `pm_freshness.py`; raw source content is not copied into the briefing.
- Relative file paths are validated before use. The initial OpenCode bridge
  also checks source paths against the active project's real filesystem.
- There is no persistent automatic "what the agent already saw" tracker yet.
  A caller can provide `seen_refs`; the OpenCode adapter's existing
  per-task caching avoids repeated base briefings.
- The output currently focuses on facts, file anchors, and risk labels.
  Full dependency matrices, project-wide code impact analysis, and
  test-specific provenance still require the existing specialized tools.
- Continuous outcome attribution and live installed-OpenCode compatibility
  need separate end-to-end validation.

## Evaluation requirement

Before broad rollout, compare Context Intelligence vs. the legacy
`memory_get_context` on curated coding-task scenarios. Measure
relevant fact recall@5, false-positive context, flagged/stale fact handling,
context characters, latency, and repeated content. Do not equate a smaller
prompt with a better answer without actual task-level evidence.
