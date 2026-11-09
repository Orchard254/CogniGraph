# Evidence-First Context Controller (experimental)

## Problem

CogniGraph currently finds relevant facts and only then announces generic
knowledge gaps. Retrieval similarity is not the same as deciding what a coding
task requires. A coding agent needs **evidence for its next decision**, not
the largest pile of similar project memories.

## Design decision

Replace retrieval-first behavior with a requirements-first, evidence-guided
controller inside the existing context delivery path.

No new MCP tool, SQLite table, model dependency, embedding index, or memory
intake route. The controller reuses the current hybrid retriever and
structural index but changes how the selected results are organized.

The actual cycle is:

1. **Interpret the task** using an explicit small evidence-requirements
   contract; a fix needs failure evidence, an edit needs constraints and
   impact investigation, all edits need test verification. IDE-observed phases
   override generic task-word classification.
2. **Retrieve candidates** through the existing hybrid engine; no retrieval
   receipt yet.
3. **Select for marginal coverage**, boosting the first source-anchored
   behavioral fact and the first applicable constraint/decision, rather than
   repeatedly rewarding similar candidates. A fact is counted as evidence
   only when its *full line is actually displayed*.
4. **Discover dependencies** from the indexed calls/imports graph adjacent
   to a focused file, plus already delivered fact relationships. Only
   existing, project-local files are offered as inspection leads; graph
   results are unverified observations.
5. **Evaluate unresolved needs** as a structured evidence plan containing
   requirement IDs, statuses, delivered fact references, file leads and one
   next action. Show a bounded subset within the existing context budget.
6. **React to an actual IDE source read**: the OpenCode bridge passes the
   validated file path after the successful structured read hook. This moves
   the source requirement to "source_read_observed", without claiming code
   semantics are true or tests have passed.
7. **Attribute usage** only to the displayed live-memory fact IDs. Leads,
   gaps and plans are not facts or automatic positive feedback.

## Contract

The existing get_intelligent_context response additionally includes:

- evidence_plan.requirements: ordered, machine-readable evidence needs.
  Each includes id, label, status, refs and paths.
- evidence_plan.next_action: one suggested immediate investigation action.
- evidence_plan.verified: always false until an independent execution/outcome
  mechanism is added; a retrieved memory is never sufficient for proof.
- evidence_plan.unresolved_count: count of missing evidence categories.

Status values: missing, inspection_lead, reference_available,
source_read_observed.

The legacy status, context_markdown, selected, retrieval_id,
inspection_targets, knowledge_gaps and exact budget/receipt rules remain.

The controller intentionally avoids declaring a task safe to edit, certain to
be correct, or complete on the strength of a retrieved fact. Missing evidence
is an *investigation request*, not a forced stop.

## Scope, limitations and things to retire

- **Keep** persistent canonical facts, historical snapshots, provenance,
  project isolation, structural code indexing, IDE event integration and
  attribution/feedback safeguards.
- **Demote** search ranking from decision maker to candidate generator.
- **Retire** the AGENTS.md mandatory three-call ritual as an orchestrator
  policy; OpenCode-injected evidence or direct source inspection may suffice.
- **Do not prematurely delete** seven-lane retrieval: whether lanes can be
  removed must be decided by measured task success and latency, not taste.

Task understanding currently uses a deterministic, intentionally narrow
requirements vocabulary; unknown tasks can still need evidence types that
have not been modeled. The structural graph may omit dynamic or unresolved
calls. No tests are executed by the controller; it cannot know that a user
report was reproduced. Structured IDE source read/edit observations now persist in a bounded
project-local journal (see PERSISTENT_TASK_EVIDENCE.md). This allows file
inspection state to survive context reassembly and plugin restarts for the
same task/session identity. A complete task-state planner, verified test
ledger, cross-session objective matching, and LLM-in-the-controller loop
remain future work.

The next experiment should compare this controller against the previous
composer on **real coding decisions**: whether necessary files were found,
whether relevant constraints were obeyed, false-confidence rate, successful
fixes, unnecessary reads, context length and time. Do not replace this with
retrieval recall alone.

## Test intent

test_evidence_controller.py verifies task intent, marginal coverage,
absence of fabricated verification, zero-memory actionability, code-graph
dependency discovery, path filtering and progress after a structured read.
Existing context-delivery tests remain the compatibility baseline.
