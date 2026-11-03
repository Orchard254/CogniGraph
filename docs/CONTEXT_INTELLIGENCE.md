# CogniGraph Context Intelligence Layer

## Purpose

Turn project memory into the **next useful, evidence-labeled briefing** for a
coding agent. Avoid returning every stored fact or allowing frequent exposure
to make a claim authoritative. This is a bounded retrieval composition layer,
**not** an LLM trained inside CogniGraph.

The existing hybrid retrieval engine remains responsible for finding
candidates. The new `memory_intelligent_context` tool and native OpenCode
task briefings use those candidates to create a shorter context answer.

## Context stages

- `task`: project decisions, constraints, and task-relevant components
- `inspect`: file/module responsibility and related decisions
- `edit`: critical constraints, related components, and design decisions
- `verify`: existing constraints and facts to cross-check against tests
- `resume`: state and decisions relevant to continued work

All these stages apply to **the same underlying stored facts**; they do not
pretend to know the current test result or infer new relationships.

The API accepts an optional list of up to five relative paths to emphasize,
up to 100 `seen_fact_ids` to avoid repeated live-memory facts, 1-12 returned
facts, and a strict 256-6,000 character budget (default 3,600). Stage choice is
explicit and should reflect what the agent is actually doing. These are
**character limits, not token measurements**.

## Example MCP request

```json
{
  "query": "fix authentication refresh after page reload",
  "stage": "edit",
  "file_paths": ["src/auth/session.py"],
  "max_chars": 1800,
  "max_facts": 5,
  "seen_fact_ids": [12, 18],
  "agent_type": "opencode"
}
```

A response returns `context_markdown`, `retrieval_id`,
`fact_ids`, `source_refs`, `stage`, and `character_count` when
facts are found. The source labels `MEM #42` and `EPISODIC #42` are
intentionally distinct namespaces. Only rendered **live** memory facts are
recorded on the retrieval receipt. A candidate filtered out by the budget
cannot later be rated against this receipt. This prevents falsely learning
from facts the model never saw.

## Evidence discipline

- Include source file and recorded line range when available.
- `source_bytes=current` proves a stored source hash still matches the
  current bytes, **not** that the higher-level claim is true.
- `source=possibly_stale` and `source=source_missing` identify
  knowledge requiring review; a warning never silently edits memory.
- Show confidence and evidence type as **metadata**, not authority.
- Mark correction flags. Flagged/stale facts are lightly demoted but
  remain eligible and visibly warned about.
- Never label all retrieved memories helpful following a successful test.
  The existing `memory_rate_context` and task review workflow still
  require fact-specific evidence or an explicit user report.
- Reject credential-bearing task text before saving a retrieval receipt.
  This is best-effort secret recognition, **not** comprehensive DLP.

## Compatibility and limits

- Existing `memory_query`, `memory_get_context`, file briefings and
  the frozen retrieval-quality gate retain their original interfaces.
- New OpenCode installations and updated opt-in installations use the
  intelligence layer for their initial task briefing. Its existing focused
  file refresh mechanism remains available for live file-specific context.
- Explicit stage selection and already-seen fact IDs are exposed through
  MCP; OpenCode does not yet automatically infer every coding stage.
- Graph dependency data is available through existing `memory_prepare_task`,
  `memory_get_file_context`, and `memory_check_safety`. This first layer
  does not automatically invent a dependency based on shared keywords.
- Context budget is enforced by skipping entire fact lines; no half-cut fact
  is presented as if complete.
- No automatic knowledge-writing or self-scoring is introduced.
- Live OpenCode V2 host end-to-end verification is still necessary.

## Tests

```bash
python -m unittest -v test_context_intelligence.py
```

Development gates: frozen retrieval-quality evaluation, Python/Node matrix,
docs/tool-table drift check and packager checks.

Next stage: gather live before/after measurements across real coding tasks,
assess precision/recall and misleading-context rate, and add trustworthy
per-stage signals only after establishing improved task outcomes.
