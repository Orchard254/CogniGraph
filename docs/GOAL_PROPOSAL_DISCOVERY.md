# V5 — Grounded goal-contract discovery (proposal, challenge, adoption)

## What changed

CogniGraph can now suggest an acceptance-evidence contract *before* the coding
agent changes the source. Unlike V4's manually authored contracts, V5 performs
a bounded, read-only scan of the actual project and surfaces plausible source
files and their existing related tests.

The system is deliberately three-stage:

1. **Propose:** inspect safe on-disk paths and familiar test conventions.
   Prefer current IDE file focus, then grounded existing inspection leads,
   then uniquely best filename matches. Do not invent tests or paths.
2. **Challenge:** separately evaluate missing source ownership, ambiguous
   matches, missing checks, project-wide checks and unproven behavioral
   relevance. All candidate checks carry explicit limitations.
3. **Adopt:** an explicit user or coding agent action registers the reviewed
   candidate through V4's existing task_goal CLI. Until that happens, there
   is no goal contract and no evidence-floor reset.

This is not a second model or an assertion that a file or test is relevant.
The challenger is deterministic and conservative. All output is a *candidate*
which requires review before adoption, and no candidate is human-approved.

## Grounding and matching

The scan visits at most 1,200 directory entries and descends at most five
levels without following symlinks. It skips hidden directories, dependencies,
generated build artifacts and unsafe file paths. The result is ordered
deterministically and never changes the memory database.

Source paths originate from one of:

- completed structured IDE focus (validated file existence);
- validated inspection leads already returned by the context controller;
- explicit task paths;
- unambiguous source filenames matching task words.

For memory-derived fallback proposals, the bridge reserves a small character
budget **before** retrieval so a later proposed contract cannot silently
push out facts already attributed as delivered. A memory lead is a path to
inspect, never a claim of correct code or confirmed test behavior. Smaller
context budgets prioritize prior observations over speculative proposals.

For Python, related existing files such as tests/test_login.py can yield
a direct candidate like python -m pytest -q tests/test_login.py. For plain
JavaScript, a matching existing test may yield node --test <path>. A safe
recognized package.json test script (for example, vitest run) may yield
npm test for TypeScript, explicitly labeled project-wide. Unsupported,
masked-failure or missing checks are not manufactured.

The source and test *names* are only leads. The challenger warns that the
tests may not assert the behavior, and that a red-green repair candidate
has not yet reproduced the reported failure. If multiple source files have
equal filename relevance, no arbitrary owner is chosen. A missing test is
a blocker to registering an implementation/repair candidate.

## OpenCode context

On orient/inspect/verify bridge requests for an unregistered task, and only
when the character budget allows, CogniGraph provides:

- a bounded "Suggested goal contract" preview;
- a structured task_goal_proposal response with exact source/test evidence;
- a compact JSON criterion set for V4 adoption, if the challenger has no
  blockers;
- an explicit note that registration requires reviewing the proposal.

The existing opaque Goal ref in the same context identifies the active task.
Agents can submit the reviewed JSON to:

    python -m task_goal --project-root . \
      --ref <goal-ref-from-context> --profile repair

It accepts the JSON on stdin. This freezes evidence floors at registration.
No candidate is silently accepted, and source reads/checks collected before
registration do not become acceptance proof after adoption. When file focus
changes, the OpenCode plugin removes the stale base proposal and delivers
the new focused proposal rather than presenting contradictory plans.

## Controlled pilot and measurements

test_task_proposals.py includes a local fixture pilot:

- Discover source and actual related pytest file for a broken login function.
- Register the suggested repair contract.
- Run the **real failing pytest subprocess** through the V3 independent
  runner, with its measured nonzero exit and no shell.
- Apply an actual source change and record an IDE-style edit/re-read event.
- Rerun the exact check and confirm measured exit zero.
- Verify that V4 returns only evidence_ready_for_review; neither verified
  nor accepted_by_owner becomes true.

Adversarial tests cover ambiguous code ownership, absent tests, simulated
relevant focus, symlinks, unsupported scripts, masked npm failures,
independent check scoping, unchanged ledgers before adoption, and context
changes after registration.

This pilot is not an installed production OpenCode session. It validates the
Python bridge and simulated OpenCode behavior and exposes workflow failure
modes, but **does not establish improved real-world coding success**.
Future evaluation must compare tasks with versus without proposals using
independently judged outcomes, unnecessary inspections, false-confidence
rate, token cost, CPU and end-to-end latency.

## Boundaries and future work

- No LLM-driven acceptance-criterion interpretation yet; no user-authorized
  acceptance or final task completion.
- Filename matching cannot establish semantic coverage, regression
  reproduction, cross-file effects, or test completeness.
- Large trees and nonstandard tests may require explicit focus or manual
  criteria. The controller does not broaden to shell command execution or
  arbitrary scripts during discovery.
- Proposals remain separate from project memories and ranking feedback.

The next meaningful V6 milestone is explicit task-specific behavioral
acceptance against observed function/output changes and a proper live
OpenCode pilot. That must be assessed causally, not inferred from green CI.
