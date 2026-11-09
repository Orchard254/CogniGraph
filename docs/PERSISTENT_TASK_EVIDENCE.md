# Persistent task-evidence ledger — first implementation

## Why this exists

A coding agent loses its immediate working state when its context is
reassembled, the plugin restarts, or a long conversation is compacted.
CogniGraph previously recomputed task requirements but did not persist which
source files the IDE had **actually** read or edited. As a result it could
repeat an already-completed inspection or trust a read made before an edit.

The new ledger addresses that narrow missing capability without pretending to
understand code semantics.

## Architecture

A per-project, bounded SQLite event journal lives at
.cognigraph/task_evidence.sqlite3, separate from the canonical fact store.
It does **not** create, promote, or modify project memory facts.

- Index by SHA-256 of OpenCode session ID + task text (stored independently);
  neither the raw session ID nor the prompt is written to the ledger.
- Record only successfully completed structured **read** or **edit/write**
  events, not shell output, chat narration, or agent declarations.
- Record a bounded SHA-256 file digest at a read event and recheck it before
  treating the observation as current. External edits or deletions mark a
  read as stale. An edit explicitly invalidates earlier reads until the file
  is inspected again.
- Reject source files outside the project, hidden paths, dependency directories,
  symlink escapes, oversized files, and untrusted file names.
- Retain at most 96 recent events per task and 4096 per project, preserving
  the latest event for each recently accessed file within those bounds.
- Read the journal without changing retrieval receipts, ranking or training.
- If the primary memory DB is unavailable, already-recorded task evidence may
  still be delivered, but it cannot substitute for source discovery.
- A failed/unavailable observation bridge never blocks the IDE; it simply
  cannot claim the unrecorded event occurred.

### Direct integration

The native OpenCode plugin records an event before the next context hook,
including when a file is re-read without changing the current focus.

The Python bridge accepts a new internal --mode evidence and a session_id
on context retrieval. There is no additional public MCP tool. A bounded
summary is delivered as **task evidence**, not a memory fact; the existing
context composer uses only source-read observations that still match the
current digest.

When source has been edited but not re-read, the briefing says so rather
than reporting source inspection as complete. Previously generated
task-level next-action guidance is suppressed when a fresher file-focused
briefing supersedes it. The plugin remains fail-open.

### Status semantics

- observed_files: IDE completed a read of this path and the post-read
  source digest still matches. **Not** an assertion of complete understanding
  or even that every source line was in the tool's output.
- edited_files: latest event is an editor change; prior reads are invalid.
- stale_files: file changed, disappeared, or became unsafe since a read.
- verified: always false; no test runner is wired into this ledger.

## Deliberate non-features

The current ledger does not claim successful test execution, reproducible
failures, verified architecture, or completed tasks. It also does not infer
an explicit user acceptance decision from model output.

It survives plugin restarts **within the same IDE session/task identity**.
Cross-session task matching, durable constraints/decisions and verification
checkpoints are separate follow-on problems. Files older than the bounded
journal retention may drop from the active snapshot.

## How to evaluate it

Use real OpenCode sessions to compare the evidence-first controller with and
without the ledger. Track repeated source reads, stale-file mistakes,
unnecessary model context, task completion quality, latency, and number of
unjustified assumptions. Unit tests exercise persistence, file drift,
event invalidation, isolation, payload bounds, and bridge integration.

Improving the number of green tests is not a substitute for demonstrating
better coding decisions. All claims should be backed by actual observed runs.
