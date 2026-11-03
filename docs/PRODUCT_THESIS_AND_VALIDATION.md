# CogniGraph: product thesis and validation gates

## North star

**An agent using CogniGraph should make more correct project-specific coding
decisions than the same agent without it, at acceptable context and latency
cost, and that advantage should improve as trustworthy project evidence
accumulates.**

This is a *hypothesis*, not a claim that has been demonstrated. Better
retrieval recall or more saved memories is not sufficient evidence.

CogniGraph is a **project intelligence service**, not an autonomous coding
agent or general chatbot. It should help an external coding agent remember,
interpret, and verify project-specific constraints, decisions, relationships
and historical failures.

## The intended end-to-end loop

1. **Observe (without equating activity with correctness).** Collect bounded,
   authorized project structure, source relationships, decisions, and coding
   outcomes. IDE events indicate observations, not verified success.
2. **Represent.** Preserve concise claims linked to code, time, scope,
   evidence type, trust and uncertainty. Distinguish historical state from
   currently valid facts.
3. **Evaluate.** Check whether evidence still supports a claim. Source hashes,
   successful tests and user feedback each answer different questions.
   Neither a hash match nor a task pass proves every retrieved fact true.
4. **Deliver.** Determine the agent's immediate decision, retrieve relevant
   facts, prioritize stable constraints and risks, mark uncertainty, and
   respect a measurable budget. Avoid duplicate tool calls and filler.
5. **Learn only from attributable outcomes.** Add, correct, retire or
   reweight knowledge only when the supporting evidence warrants it.
   Popularity alone is not evidence.
6. **Measure.** Re-run grounded task tests after each material change. If the
   change does not improve useful outcomes, simplify or revert it.

## The five questions each design review must answer

- What real coding decision becomes more correct or less risky?
- What is the smallest internal change that can accomplish it with
  the **existing** API?
- What evidence distinguishes a valid memory from an appealing but false one?
- What is the expected cost: memory, CPU, context tokens, latency, and
  more agent-selected calls?
- What benchmark could falsify the claimed benefit?

Without persuasive answers, do not implement the feature.

## Validation ladder

| Stage | Evidence required | What it does **not** prove |
|---|---|---|
| Unit tests | Behaviors, permissions, bounds, protocol and isolation work on fixtures | Improved coding decisions |
| Frozen retrieval gate | Labeled fact can be found with stable search behavior | Labeled fact reached the model |
| **Rendered delivery comparison** | Whether a labeled fact appears in the actual briefing, at what rank, character cost and latency | Whether the model uses the fact correctly |
| Context trust review | Curated cases with known misleading/stale memories and outcome labels | Generalization to new repos |
| Live OpenCode pilot | The plugin delivers and refreshes the intended context under real host events | Better task success across projects |
| Controlled coding-task comparison | Same tasks and agent configuration with vs. without CogniGraph; blinded adjudication, repeated trials | Infinite scalability or universal benefit |
| Longitudinal experience test | Does exposure to **verified** earlier tasks reduce repeated mistakes on later held-out tasks? | Automatic truth from frequent usage |

Each stage can block promotion to the next. Do not jump directly from
passing CI to announcing the engine is learning.

## Initial diagnostic: context delivery

Run:

```bash
python scripts/benchmark_context_delivery.py --split holdout
python scripts/benchmark_context_delivery.py --split both --json
```

This uses the **existing**, hash-checked frozen retrieval fixture and pinned
clock, never the developer's live memory database. It compares:
`MemoryStore.get_context` (legacy rendered context) and
`MemoryStore.get_intelligent_context` (Context Intelligence Layer v1).
Both receive an equal 2,900-character target budget and a limit of five
primary facts; legacy's truncation behavior may exceed that budget and is
recorded explicitly.

The golden set contains just **one expected fact per query**. We measure
*delivered target recall@5*, *target MRR*, average output characters,
median latency, and budget violations. We extract only the primary
numbered facts from legacy markdown, not graph-neighbor mentions.

The existing retrieval quality gate remains unchanged and merge-blocking.
This diagnostic is report-only until independent task labels and a meaningful
performance threshold exist. A lower number is an investigation trigger;
do not silently reset the benchmark or bias holdout labels to improve it.

Important limitations:
- These old golden queries concern the CogniGraph repository, **not**
  representative edit/verify situations across diverse projects.
- A target fact appearing in text proves delivery only. It does not prove
  correctness, usefulness, reduced hallucination, or task success.
- The frozen database snapshot is isolated from its original source files.
  Source-freshness statuses cannot be interpreted as live source validation.
- Context latency from a CI runner is a rough comparative measure, not a
  production performance guarantee.
- The existing fixture is historically selected; the tuning/holdout split
  does not substitute for new, independently judged task-level labels.

## Next study, before new feature proposals

Collect approximately 20–30 real coding tasks from at least two codebases,
with an honest record of what the agent needed to decide, the before/after
code, held-out acceptance tests, and independently adjudicated relevant and
misleading memory claims. Compare **no memory**, **legacy context**, and
**Context Intelligence** in randomized order with the same coding agent,
model, prompts, token limits, environment and allowed tool access. Include
negative and stale-memory cases; record calls, tokens, latency and memory
mutations. Hold out half the tasks from any tuning.

Only then propose a narrowly scoped fix to whichever failure dominates.
Do not add a new MCP tool simply because an evaluator identified a gap.
