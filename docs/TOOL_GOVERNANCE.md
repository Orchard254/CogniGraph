# Public MCP Tool Governance

## Decision

**Do not add another agent-visible MCP tool by default.** CogniGraph's primary
goal is better coding-agent context and more accurate retained knowledge,
not a larger tool catalog. Put new intelligence inside an existing retrieval,
file-analysis, outcome or maintenance workflow whenever practical.

As audited on 2026-11-04, `server.py` registers **31 public MCP tools**.
`test_mcp_tool_surface.py` makes that set an explicit review baseline.
It is a change-control tripwire, **not** a permanent numerical cap and not
proof that the current list is optimal.

## How a coding agent should use the existing interface

| Need | Preferred starting point | Use alternatives only when needed |
|---|---|---|
| Get context for an actual coding task | `memory_get_intelligent_context` | `memory_prepare_task` for the established detailed preflight; `memory_get_context` for legacy consumers |
| Find a particular stored fact | `memory_query` | Avoid querying again if the briefing already included it |
| Read focused code context | `memory_get_file_context` | `memory_read_code` for explicit scoped source lines, not as another mandatory briefing |
| Understand change impact | `memory_check_safety` | `memory_validate_constraints` for constraint-specific validation; `memory_check_security` for heuristic security scanning |
| Learn from a completed task | `memory_checkpoint_task` for reusable decisions/failures | `memory_record_task_outcome` records changed files and review candidates; it does not verify correctness |
| Correct a memory | `memory_supersede` for a known correction | `memory_flag_fact` for uncertainty, `memory_deprecate` when truly obsolete |
| Investigate code relationships | `memory_blast_radius` for transitive impact | `memory_who_calls`, `memory_who_inherits`, and `memory_search_symbols` for narrower structural questions |
| Inspect or administer the store | `memory_stats` | Feedback stats, disagreement audits, locks and review queues are specialist operations, not required for routine coding |

Not all these calls are required every task. The OpenCode plugin can inject
initial and file-specific context without the agent calling MCP. Avoid
repeating those results solely to satisfy a rote checklist.

## Current overlap assessment

**Highest consolidation candidate: four context entrypoints.**
`memory_query` searches facts; `memory_get_context` formats older
briefings; `memory_get_intelligent_context` selects phase-aware briefings;
`memory_prepare_task` is an extensive preflight. These have distinct
implementations, but an agent can struggle to choose. Keep backward
compatibility while testing a single recommended default and routing other
capabilities through the same underlying intelligence layer.

**Related, not proven redundant: three pre-edit checks.**
`memory_check_safety` analyzes dependencies,
`memory_validate_constraints` checks stored constraints, and
`memory_check_security` applies security heuristics. Before consolidation,
measure whether a unified internal preflight improves coverage and latency
without hiding distinct warnings.

**Related, not interchangeable: three outcome paths.**
`memory_checkpoint_task` persists knowledge,
`memory_record_task_outcome` logs observed edits, and
`memory_rate_context` records attributable ratings. Do not combine these
semantics into an automatic "task passed, therefore fact was helpful" loop.

**Advanced utilities:** detailed graph, locks, audits, and diagnostics may
belong in a selectively exposed expert profile. Do not remove them or break
existing clients merely to achieve a smaller count.

## Mandatory proposal for any public tool addition

A PR must explain:

1. **Unique agent decision:** What action would a coding agent be unable to
   complete correctly with the current tools?
2. **Reuse alternatives:** Why not extend an existing tool, add an optional
   parameter, improve the Context Intelligence composer, use the OpenCode
   hook, or implement an internal method/CLI command?
3. **Workflow impact:** How will an agent know *when* to use this tool,
   and which current tools will it replace or render unnecessary?
4. **Evidence:** Add a representative failing scenario and a before/after
   test. Measure relevance or correctness, unnecessary calls, latency, and
   output budget rather than treating a new tool as success.
5. **Compatibility:** Describe schema, permissions, storage, security,
   portability, documentation, and migration implications.
6. **Net surface change:** State `+N / -N` exposed tools and why the extra
   selection burden is worth it.

Any exposed change must update the reviewed name set in
`test_mcp_tool_surface.py`, the generated README registry and the relevant
installation/agent guidance. The gate intentionally fails until this review
is performed. Modifying the baseline is **not authorization on its own**.

## Next audit experiments (no new MCP endpoints)

- Compare the four context entrypoints on identical coding tasks; measure
  which facts were useful, missed, stale, or duplicated.
- Test whether the existing `memory_get_intelligent_context` can support
  inspect/edit/verify steps without requiring more agent-selected tools.
- Measure the number of tool selection errors and redundant calls per task.
- Document a stable small "common workflow" surface and a clearly optional
  specialist surface; decide on compatibility-preserving consolidation only
  after evidence is collected.
