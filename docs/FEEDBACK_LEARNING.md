# CogniGraph: learning context relevance from evidence

## The principle

More retrievals do not mean greater intelligence. CogniGraph should learn
**which facts helped a particular kind of task**, and separately learn
which facts were irrelevant or incorrect. The memory store remains the
source of project facts; feedback changes retrieval ordering, not truth.

## What is implemented in this phase

1. Each logged `memory_query` or `memory_get_context` result includes
   a `retrieval_id`; the OpenCode V2 plugin also embeds a receipt ID
   alongside its context briefing.
2. `memory_rate_context(retrieval_id, ratings, evidence_type, ...)`
   accepts one to 20 entries, each `{"fact_id": 42, "rating": "helpful"}`,
   `irrelevant`, or `incorrect`. The referenced facts must actually
   have appeared in that retrieval, still be active, and belong to the
   primary memory store (not the episodic historical archive).
3. Only `evidence_type="user"` (an explicitly reported user judgement)
   or `evidence_type="test"` influence ranking. Test metadata requires
   a command, a successful exit code, and a commit SHA matching current
   git HEAD. **This only validates caller-supplied metadata; CogniGraph
   does not execute the test or independently confirm the outcome.**
   The agent must not claim a specific memory helped unless the
   evidence genuinely supports that conclusion.
4. `evidence_type="agent"` is diagnostic only and **does not train**.
   Mere retrievals, repeated tool calls, or unverified task completion
   never count as positive feedback.
5. The retrieval engine applies small, capped multipliers **only for
   similar topics**, using token overlap and a four-observation prior.
   Positive ratings promote, negative ratings demote; the influence
   decays over 90 days and expires.
6. Duplicate ratings for the same retrieval/fact pair update that
   feedback event rather than increasing its weight. Invalid batches
   are rejected rather than partially written.
7. `memory_relevance_feedback_stats()` reports outcome counts without
   exposing stored task text.

### Example

First call `memory_query(text="fix oauth session refresh", agent_type="opencode")`,
then after a user explicitly identifies facts as useful/incorrect:

```json
{
  "retrieval_id": 73,
  "ratings": [
    {"fact_id": 12, "rating": "helpful"},
    {"fact_id": 34, "rating": "incorrect"}
  ],
  "evidence_type": "user"
}
```

For `test` evidence, include `verification_evidence` with
`command`, `exit_code`, and `commit_sha` matching the current
checkout. If the evidence does not establish that a particular fact
helped, do **not** rate it as helpful.

### Safeguards and limitations

- Feedback never modifies a fact or its confidence; use
  `memory_supersede`, `memory_deprecate`, or `memory_flag_fact`
  for truth maintenance.
- User reports are attributed as such but not cryptographically
  authenticated. A calling agent can supply a false attribution,
  so an independent UI confirmation step should be added later.
- Context is local to the project's existing SQLite database.
- Existing raw usage logs contain query text; this phase skips
  logging new prompts matching known credential patterns, but it
  is **not** a complete privacy sanitization or retention policy.
- A successful test does not prove that every retrieved fact helped.
- No LLM trains the ranking. This is an evidence-informed,
  bounded retrieval heuristic, **not** self-training model weights.
- A native IDE hook currently injects context at task time; it
  does not autonomously verify final outcomes or refresh after
  every file change.

## Next milestones

**Phase A — closed-loop attribution.** Add explicit in-IDE feedback UI,
track which citations were actually referenced in an edit, and associate
tests with those files and facts. Require a user confirmation or a
machine-checkable proof before raising a fact's credibility.

**Phase B — progressive context (partially implemented).** The OpenCode V2
plugin now refreshes file-specific memory after successful read/edit/write tool
calls, while keeping the task briefing cached. Next priorities are external file
changes, dependency-aware pre-edit validation, and source-fact validity checks.

**Phase C — proactive relationships.** Use co-use and verified edit paths
to discover candidate dependency links, retain evidence separately,
and require checks before making links authoritative.

**Phase D — continuous evaluation.** Maintain task-level datasets with
human-rated relevant fact IDs, compare adaptive vs. non-adaptive
recall@5/MRR and error rates, inspect regressions by task/IDE, and
monitor context latency and prompt size. A false-positive context
rate is just as important as increasing recall.

Do not ship an automatic closed-loop ranking boost based solely on an
agent saying that its own answer was good.
