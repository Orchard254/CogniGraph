# Context-delivery benchmark: proof before new features

## Question

Does CogniGraph's phase-aware Context Intelligence Layer put the right
*previously labeled fact* in the actual briefing delivered to a coding agent,
with fewer context characters and acceptable latency, compared with the
existing `get_context` briefing?

This is deliberately narrower than asking whether the agent solves a
programming task. Passing this diagnostic **does not prove intelligence,
successful learning, or useful agent behavior**.

## Frozen fixture and fairness

Use the existing immutable fixture `eval/fixtures/golden_fixture.db`,
the 36 tuning and 16 holdout query/fact pairs, and the existing
`golden_expectations.json` fixture hash and pinned recency clock.

Run both composers against the same temporary SQLite snapshot:
- Legacy: `MemoryStore.get_context(query, limit=5, max_chars=2900)`
- Context Intelligence: `MemoryStore.get_intelligent_context(query,
  phase="orient", limit=5, max_chars=2900, log_usage=False)`

This compares initial *OpenCode-style* briefings: the new plugin uses
`orient` at task admission. A precomputed vector lookup from the fixture
avoids model downloads. Legacy's search calls are wrapped to disable
usage/access counters without changing ranking. Results alternate execution
order to reduce simple cache/order bias.

The benchmark checks that usage logs and access counters remain unchanged,
that the source fixture's SHA-256 still matches its committed hash, and that
both composers read from the same fixture. The real `.cognigraph`
database is never opened.

## Metrics

- **Gold delivered@5**: fraction of queries where the single labeled
  fact is actually present as one of the first five *primary displayed*
  entries (not just a graph-neighbor reference).
- **Delivered MRR**: reciprocal rank of the labeled fact among those
  primary entries, averaged over queries.
- **Briefing size**: mean character count and budget violations.
- **Latency**: median and p95 per-call milliseconds; diagnostic only,
  not a reproducible hardware benchmark.
- **Selected fact count**: average number of primary entries.

The new composer reports its selected IDs directly. The legacy formatter
has no structured selected list, so a conservative parser extracts numbered
top-level entries from the *actual returned Markdown*. This does not count
incidental mentions in expanded graph neighbors.

**Do not label other retrieved facts as false positives.** Each golden
query names one expected fact, not a complete judged set of all relevant
facts. The benchmark cannot establish precision, context harmfulness,
task success, or learning improvements. No false-positive rate is claimed.

**Do not conflate source hashes with semantic correctness.** This fixture
does not include the current working tree corresponding to each recorded
source path, so the diagnostic does not score freshness or correctness.
Separate source-drift tests cover only the implemented warning mechanism.

## Run

```bash
python scripts/eval_context_delivery.py
python scripts/eval_context_delivery.py --json
python scripts/eval_context_delivery.py --split tuning --max-items 8
python scripts/eval_context_delivery.py --split holdout --max-chars 2900
```

The existing CI retrieval-quality job runs the full comparison and prints
results. No delivery-quality threshold is set: the metrics are diagnostic,
while fixture validation, read-only accounting, and script correctness
are enforced. Do not change the freeze or golden split to get a better score.

## Evidence needed before a new learning feature

A second, *task-outcome* experiment is required before claiming CogniGraph
has become more useful through experience:

1. Select real, runnable coding tasks with fixed repositories, base commits,
   expected behavioral tests, and known constraints. Include bug repair,
   interface-preserving refactors, and regression prevention.
2. Define three conditions with the **same coding agent, model, permissions,
   tool exposure, time budget, and task order balancing**: no CogniGraph,
   the current legacy context, and Context Intelligence.
3. Verify task success independently through repeatable tests and review.
   Measure incorrect/misleading context, unnecessary code edits, wall time,
   agent/tool calls, and context token usage.
4. For *learning* claims, repeat related tasks after **verified experience**
   is added, comparing the same-memory and no-new-memory conditions against
   genuinely unseen tasks. Do not automatically reward a retrieved fact
   because a task passed.
5. Use a held-out evaluation set and record failures. Require improvement
   across varied tasks without a meaningful rise in misleading context,
   overhead, or regression rate.

Do not add another public MCP tool to run these experiments. Prefer an
offline harness and existing code paths. Keep the tool-governance baseline.
