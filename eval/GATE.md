# Retrieval-quality gate (`eval-gate`)

The `eval-gate` CI job (ubuntu-only, in `.github/workflows/ci.yml`) makes
retrieval quality merge-blocking: every PR runs the golden-set eval against
the frozen fixture and fails if quality drops. The gate failing IS the
process working — it means a change altered what agents retrieve, and that
must be a conscious, reviewed decision rather than an accident.

## What the gate checks

`scripts/eval_gate.py` runs `eval_golden` twice per split (tuning: 36
pairs, holdout: 16 pairs) against `eval/fixtures/golden_fixture.db` and
enforces, for BOTH splits:

1. **No drop in recall@5 or MRR** below the values committed in
   `eval/fixtures/golden_expectations.json`. There is no tolerance band:
   the evaluation is deterministic (pinned clock, frozen fixture,
   precomputed vectors), so the expected behavior is exact reproduction.
   Any measured drop is a regression by definition.
2. **Determinism**: the two consecutive runs must produce identical
   per-item ranks.
3. **Fixture integrity**: the fixture's SHA-256 must match the committed
   hash in the expectations file. Fixture and expectations always change
   together.

## Why it cannot rot

- **Pinned clock.** `_recency_weight` ages facts against a reference
  instant. Against the wall clock, golden ranks drift as facts age and a
  committed baseline rots within weeks. The gate sets `PROJECT_MEMORY_NOW`
  to the instant the fixture was frozen (`pinned_now` in the expectations
  file), so the numbers reproduce exactly whenever CI runs.
- **Frozen fixture.** The gate never evaluates the live database; it
  evaluates a committed snapshot, so corpus growth cannot fail a PR.
- **Precomputed embeddings.** Fact and golden-query embeddings are stored
  inside the fixture (`eval_query_embeddings`), so the gate is fully
  offline: fastembed is installed as a library, but the 80 MB model is
  never loaded or downloaded.

## Intentional retrieval changes (fixture refresh process)

If a PR deliberately changes retrieval (ranking lanes, RRF k, boosts,
schema affecting search), the gate will fail until the baseline is moved.
That is the point: in the SAME PR,

1. run `python scripts/freeze_eval_fixture.py` (add `--now <ISO date>`
   only when retiring the old pinned clock),
2. commit the regenerated `eval/fixtures/golden_fixture.db` and
   `eval/fixtures/golden_expectations.json`,
3. justify the new numbers in the PR description (what changed, why the
   metrics moved, and that holdout split was not peeked at during tuning).

A PR that changes retrieval without refreshing the fixture cannot merge —
either the change is a regression, or the author is silently moving the
baseline. Both are exactly what the gate exists to catch.

## Current committed baseline (k=5, pinned 2026-09-11)

| split   | pairs | recall@5 | recall@10 | MRR    |
|---------|-------|----------|-----------|--------|
| tuning  | 36    | 0.8333   | 0.9444    | 0.5672 |
| holdout | 16    | 0.7500   | 1.0000    | 0.5298 |

These measure the frozen artifact (precomputed query vectors) rather than
the live model's query encoding, so they differ from the live-DB numbers
recorded in the Phase 5 decision; the gate compares only against its own
committed baseline.