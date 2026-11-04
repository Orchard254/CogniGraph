# CogniGraph simplification audit — Phase 1

**Objective:** reduce work the coding agent must do, simplify retrieval and
storage where defensible, and establish whether complexity improves context
quality. No production behavior is changed in this phase.

## Evidence collected from current code

1. **Too many mandatory steps.** `AGENTS.md` opens with "STOP — no
   read/TodoWrite before ..." yet later says OpenCode's native briefing may
   satisfy initial retrieval. These conflicting instructions create avoidable
   agent overhead. Replace in a later PR with a short protocol rather than
   editing agent behavior before collecting a baseline.
2. **Four of seven lanes were inactive in a historical 12-query live-corpus
   audit.** See `test_lane_health.py`. `struct` is intentionally disabled
   for the default agent after prior quality measurements, and three episodic
   lanes were inactive because of source-of-truth deprecation guards. The
   current live corpus may differ; this is a hypothesis, not removal proof.
3. **The official frozen evaluation is only 52 labeled query/fact pairs.**
   Tuning has 36, holdout has 16, pinned to 2026-09-11. The baseline is
   high and *project-specific*, so changes must not be selected using only
   this corpus. The split is not an independent test of generality across
   different repositories or languages.
4. **The hybrid retrieval layer includes distinct lexical, token, semantic,
   episodic and structural paths.** Even if a path's fusion weight is zero,
   the implementation may still do candidate work. Therefore a ranking
   ablation does NOT automatically establish a runtime speedup.
5. **MCP is a 30-tool external interface.** We should measure agent tool
   selection overhead and consider an *optional* 4–6-tool facade before
   removing specialist APIs. MCP callers may depend on current tool names.
6. **The SQLite lifecycle includes substantial schema migrations and
   episodic branch history.** Deleting or consolidating historical tables
   would risk data loss; a read-only compatibility review and migration plan
   are required first.

## Reproducible offline retrieval ablation

```bash
python scripts/eval_gate.py
python scripts/benchmark_simplification.py
python scripts/benchmark_simplification.py --json
```

The benchmark uses `eval/fixtures/golden_fixture.db` (read-only source,
SQLite backup copy), its pinned clock and precomputed vectors, and both
tuning and holdout labels:

| Profile | Fusion lanes retained | Interpretation |
|---|---|---|
| `full` | Seven configured lanes | Current reference; default structural lane already has weight zero |
| `core` | Memory FTS + token matching + semantic | No episodic or structural fusion |
| `lexical` | Memory FTS + token matching | No embeddings or archive in *fusion* |
| `fts_only` | Memory FTS only | Lowest-complexity ranking reference |

**Important:** the benchmark disables lanes *at fusion* by temporarily
zeroing their weights. A fact returned only because a disabled lane
created a zero-score candidate does **not** count as a retrieved match.
Most disabled lanes still execute candidate generation; the structural scan
is already skipped when its fusion weight is zero. These comparisons test
relevance and ranking **only**. The recorded
full-profile query timings are diagnostics for this runner, not a prediction
of the speedups from removing lanes. A subsequent implementation benchmark
must actually skip disabled work before comparing latency, CPU and memory.

The tool restores the original weights and clock after each run and calls
`query_hybrid(log_usage=False, expand_graph=False, explain=True)`. It does not update
retrieval counters, source history, or the committed fixture. The existing
`eval-gate` check still protects the unchanged default ranking.

## Initial measured results (2026-11-03 CI, pinned frozen fixture)

Both the default agent and OpenCode were compared under their own configured
fusion weights. `core` keeps only memory FTS, token, and semantic signals.

| Agent / split | Profile | Recall@5 | MRR | Per-query rank changes vs full |
|---|---|---:|---:|---|
| Default / tuning (36) | full | 0.9722 | 0.7963 | reference |
| Default / tuning (36) | core | 0.9722 | 0.7963 | 0 changed |
| Default / holdout (16) | full | 0.9375 | 0.6760 | reference |
| Default / holdout (16) | core | 0.9375 | 0.6760 | 0 changed |
| OpenCode / tuning (36) | full | 0.9444 | 0.7083 | reference |
| OpenCode / tuning (36) | core | **0.9722** | **0.7963** | 8 better, 0 worse |
| OpenCode / holdout (16) | full | 0.8125 | 0.6271 | reference |
| OpenCode / holdout (16) | core | **0.9375** | **0.6760** | 4 better, 0 worse |

The `lexical` configuration had lower recall@5 (tuning 0.8611;
holdout 0.8125). After explicitly excluding candidates with zero
fusion scores, `fts_only` reached **0.0000 recall@5 on both splits**:
these natural-language queries did not yield positive FTS-only matches.
A first experimental run incorrectly counted zero-weight, zero-score
candidates in this profile; that artifact was corrected and regression
tested. This result does **not** mean FTS is useless as one signal in a
hybrid retriever. It shows that semantic and/or tokenized retrieval is
necessary for this benchmark's longer coding queries.

**Interpretation:** The extra episodic/structural fusion does not improve
this frozen evaluation, and for the OpenCode weights it hurts top-5 recall.
This is **one project-specific 52-pair fixture, not proof of generality**.
The ablation does not bypass candidate computation and cannot support
claims about speed or memory reductions. Do not use these findings alone
to delete episodic history, change defaults, or claim validated benefits
on users' projects.

## Simplification acceptance criteria

A simpler profile is *eligible for further trials*, not production adoption,
when:
- Holdout **recall@5 and MRR do not regress** relative to current baseline.
- Tuning quality remains stable; per-query rank changes are reviewed.
- The profile performs acceptably on a **new, independent task set from
  multiple real codebases**. The frozen golden split is too small and too
  specific for final architectural decisions.
- An actual optimized implementation, with disabled computation bypassed,
  lowers p50/p95 latency or cost on representative project sizes.
- Source-provenance, privacy, isolation, feedback and failure behavior are
  unchanged; existing database/API compatibility is maintained.

Do not silently regenerate the frozen fixture to hide a regression or
treat a single favorable recall result as sufficient evidence.

## Next controlled changes, in order

1. **Simplify AGENTS.md first**: keep auto-injected context, optional deep
   queries, verification before acting, and brief evidence-based reporting.
   Eliminate "STOP until 3+ tool calls" instructions.
2. **Make retrieval cost observable**: benchmark a truly optimized
   `core` path on a representative corpus, measure p50/p95, peak memory,
   and top-5 quality under comparable caches and hardware.
3. **Evaluate a smaller documented default workflow**, using the existing
   Context Intelligence entrypoint and OpenCode hooks. Avoid any new public
   facade until real routing/overhead evidence justifies it.
4. **Review episodic activity and memory history** with real active-user
   metrics. Preserve migrations and archived facts until an export and
   rollback plan is proven.
5. **Test OpenCode end-to-end in an actual host.** Unit-test hooks alone do
   not establish runtime correctness.

**Decision record:** This phase authorizes evaluation tooling and a
simplification proposal, not removal of any retrieval or history system.
