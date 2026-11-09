# V4 — Goal evidence gate

## The architectural change

CogniGraph no longer needs to infer that a coding objective is complete from
a successful command or from the agent's explanation. It can instead maintain
an **explicit, predeclared goal-evidence contract** and compare independently
observed source reads, edits, and check exits against each requirement.

The outcome is **at most** "evidence_ready_for_review". Owner acceptance and
whole-task correctness are outside the evidence gate.

A contract is a *workflow proposal*, not a human-authenticated instruction.
Agents must not represent a proposed contract as the user's approved intent.

## Contract lifecycle

1. Define a small criterion set for a **specific task and IDE session**.
2. Freeze the current source-event and check-event high-water marks.
3. Observe actual IDE reads, changes, and direct command check exits.
4. Re-evaluate the same contract on the next context request. Detect edits,
   stale files, failures, missing evidence, and checks run on wrong commands.
5. If the evidence matches every criterion, request independent review against
   the original user objective; do not automatically declare success.

Re-registering an identical contract is idempotent. Changing the contract
increments its revision and resets evidence floors, so old checks cannot be
retroactively used to justify weaker or changed acceptance criteria.

Only a hash of the original task/session, criterion IDs, safe source paths,
check *kind*, command hash, and required evidence provenance are retained.
The raw task prompt, command and test output are not stored.

## Profiles and criterion kinds

Profiles:

- **implementation** — requires an observed post-registration edit to a
  source file explicitly named in the contract, at least one source-read
  criterion, and one exact check criterion.
- **repair** — same, but requires a regression criterion proving the
  failure-then-edit-then-success check chronology.
- **inspection** — can require only source-reading or external review;
  still cannot claim task acceptance.

Criteria:

- source_read: an exact project-local source file must be read after
  contract registration, remain safe and unchanged, and have no later edit.
- check: a specific direct test/lint command must have a current, source-scoped
  exit code 0 after registration and match the required origin.
- regression: the **same** check command must fail after registration, then
  an observed edit to a source file named in the contract must occur,
  then the same check must exit 0. Editing an unrelated file, running a
  different passing check, or repeating a test without editing does
  not satisfy it.
- manual_review: deliberately remains open until a future independently
  verifiable review source is integrated. Model-written "approved" does not
  close it.

By default, check criteria require origin self_run (the explicit CogniGraph
subprocess verifier); a workflow can explicitly require IDE numeric runtime
observations instead. Neither origin proves the entire requested outcome.

## Example

In a project directory, define an objective contract before executing its
checks. The command reads a JSON array from stdin; it is intentionally an
explicit local operation, not a side effect of normal memory retrieval.

The **preferred OpenCode path** is to copy the opaque Goal ref displayed in
CogniGraph context (86 URL-safe characters). No raw user prompt or internal
OpenCode session ID needs to appear in the registration command:

    python -m task_goal \
      --project-root . \
      --ref "<Goal ref from injected context>" \
      --profile repair

Submit this JSON on stdin:

    [
      {"id":"auth_source","type":"source_read","path":"src/auth.py"},
      {"id":"auth_red_green","type":"regression",
       "command":"python -m pytest -q test_auth.py"}
    ]

After the failing check is observed, an edit occurs, the source is re-read,
and the same check succeeds against current code, inspect the gate:

    python -m task_goal \
      --project-root . \
      --session-id "<your OpenCode session ID>" \
      --task "fix login" --show

A direct check with independently measured exit status can be run with:

    python -m task_checks \
      --project-root . \
      --session-id "<your OpenCode session ID>" \
      --task "fix login" -- python -m pytest -q test_auth.py

The opaque Goal ref identifies the already active task/session in the
project-local journal; it is **not an authorization credential** and does not
indicate user approval. The raw session/task CLI form is retained for non-IDE
use, including `--show` to query a known task. The OpenCode plugin does not
yet offer an end-user form for authoring the contract. It detects an explicit
task_goal CLI command and refreshes the task briefing so newly registered
requirements are visible in the next model request.

## Output and semantics

task_goal.goal_snapshot returns:

- status: unregistered | needs_evidence | blocked | evidence_ready_for_review
- revision, profile, criterion-by-criterion statuses, met and total counts
- next_action: one concrete immediate investigation step
- accepted_by_owner: **always false**
- verified: **always false**

A code edit invalidates preceding check results through the existing source
fingerprint and edit watermark. A direct test without a tracked source scope
does not satisfy the criteria. A failing registered check blocks the goal
even if a separate lint/test check passes.

The goal summary appears in OpenCode context **before** retrieved facts.
The existing task planner uses the goal's unresolved criterion to decide
what action to suggest, replacing generic search-derived advice where relevant.

## Why this is more than a checklist

The contract is predeclared and its *measured evidence* is evaluated
repeatedly. It can reject a deceptive false-positive sequence: test passes
before an edit; failure is not actually reproduced; unrelated test succeeds;
source has changed; goalposts are changed after observation; or an agent
simply claims a task is complete.

This is still not general artificial intelligence. A meaningful acceptance
contract must be chosen by a capable agent or human before work. Future work
should infer **candidate contracts** from task semantics and project tests
without allowing the agent to self-certify success.

## Verification and limitations

The engine is intentionally read-only when evaluating, and bounded to a few
criteria per task. It does not write to memory ranking, reward models, or
historical knowledge.

Limitations: only explicitly tracked source files are covered, the source
fingerprint is not an exhaustive build graph, and local observation origins
are operational trust labels, not cryptographic attestation. Long-running
multi-session objectives and independent human acceptance still need design
work. Live installed OpenCode verification remains necessary.

See test_task_goal.py for pre-registration baselines, independent commands,
exact-origin checks, red-green ordering, contract revisions, symlink safety,
bridge context reconstruction, and non-promotion to completed tasks.
