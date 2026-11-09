# V3 — Observed checks and independent verification

## Goal

CogniGraph must stop mistaking **a claim that tests passed** for evidence
that a check actually ran and exited successfully. It must also stop treating
a successful check as current after subsequent code edits or changed source.

This is a deliberately narrow verification layer: it verifies the **process
exit of an individual check**, not the correctness of an entire engineering
task, user acceptance, test coverage, or absence of undiscovered failures.

## Two observation paths

1. **Self-run verifier (stronger evidence):** The explicit CLI command

       python -m task_checks --project-root . --session-id my-task \
           --task "fix login" -- python -m pytest -q

   executes the requested check itself, as a subprocess with shell=False and
   a bounded timeout (default 120 seconds, maximum 600). The actual return
   code comes from subprocess.run, not user or model narration. Test output is
   shown to the caller but never stored by CogniGraph.

   This runner is opt-in. It does not automatically launch tests after edits.

2. **OpenCode runtime check observation (conditional):** A *completed*
   structured bash tool event can be recorded **only when**
   result.metadata.exitCode or result.metadata.exit_code is a numeric
   integer. A "completed" status alone, stdout containing "passed", or
   agent-generated commentary does not qualify. Some OpenCode releases
   may not expose exit metadata in this shape; such events remain unknown.
   If the explicit runner is called via the IDE shell, the plugin refreshes
   context to read the runner's own ledger result.

Both paths use the same constrained command classifier. Direct invocations
such as python -m pytest, pytest, npm test, node --test, cargo test, go test,
python -m unittest and ruff check are supported. Compound shell commands,
pipes, semicolons, command substitutions and redirections are **rejected**:
a command such as "pytest || true" could hide failed tests behind exit code 0.
We store a hash of the command, not its text, stdout, or stderr.

## Source-aware freshness

A bounded task_check_events table lives in the existing per-project journal
at .cognigraph/task_evidence.sqlite3. It records:

- Hashed task and session identity, command hash, check kind, origin and
  numeric return code.
- The latest known IDE edit-event watermark.
- A fingerprint of files already tracked by the task evidence ledger,
  including current source hashes and unavailable files.

On context reconstruction, CogniGraph independently recalculates this
fingerprint. A later edit (even if reverted), or a changed/deleted tracked
source file, makes the prior check **stale**. Results without tracked source
files remain explicitly **unscoped**.

The journal retains up to 32 check events per task and 512 per project,
independently of fact retrieval receipts and feedback rankings.

## Information returned to coding agents

The next context includes a bounded "Check observation" section and the
structured task_check result. The evidence plan's verification requirement
can display:

- exit_zero_observed — one current, source-scoped check exited 0
- check_failed_observed — one current check exited nonzero
- stale_check — source or subsequent edits invalidated a previous check
- unscoped_check — exit known but no tracked source footprint
- missing — no qualifying observation

**evidence_plan.verified remains false**. A passing check is not an
independent proof that the complete objective is satisfied, nor proof of
comprehensive test coverage. Failed or stale checks cause a specific
investigation/re-run suggestion.

A persisted check is not a positive training signal for retrieved facts.

## Boundaries

- Numeric metadata is an observation by the IDE, not a cryptographic
  attestation. Another plugin could falsify it; the explicit CogniGraph runner
  offers the stronger independent exit code path.
- The scope covers **tracked source paths**, not every repository file, system
  dependency, environment variable, generated artifact or remote service.
  File hashes detect state changes but not semantic correctness.
- Dynamic shell wrappers and unsupported runners may remain unclassified;
  this is preferable to manufacturing confidence.
- No automatic test run, universal task-completion gate, forced regression
  block, or LLM-generated verification claim is added.
- Future work: explicit acceptance criteria and independent task-level
  completion adjudication based on multiple checks and observed behavior.

## Tests

test_task_checks.py exercises successful and failing **real subprocesses**,
runtime-reported exits, deceptive shell commands, task isolation, source
changes, edit watermark invalidation, unscoped checks, bounded retention,
private command storage, bridge rendering and nonpromotion to task success.
The OpenCode hook test covers metadata-gated observations and context refresh.

A real installed OpenCode pilot is still needed to validate the actual
metadata contract on supported client versions.
