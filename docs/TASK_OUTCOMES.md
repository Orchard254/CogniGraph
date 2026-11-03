# Task outcome review: learning what actually helped

CogniGraph already tracks retrieval receipts and accepts explicit fact-level
feedback. This feature closes a gap between editing files and knowing which
retrieved memories should be evaluated afterward.

## Workflow

1. Retrieve project memory using `memory_query` or `memory_get_context`;
   save its `retrieval_id`.
2. When a coding task is complete, call `memory_record_task_outcome` with
   the retrieval ID, changed repository-relative file paths, and optional
   caller-reported test details. This is compatible with the existing
   `memory_checkpoint_task` lifecycle.
3. CogniGraph intersects *retrieved fact IDs* with *facts anchored to the
   reported changed files*. This deliberately conservative intersection
   creates review candidates, not positive memory labels.
4. Call `memory_task_outcome_review_queue` to see pending candidate facts.
   Ask for specific evidence or user confirmation, then use the existing
   `memory_rate_context` tool to assign `helpful`, `irrelevant`, or
   `incorrect` to a particular retrieved fact.
5. Verified, attributable feedback affects future task-conditioned retrieval
   as described in [Feedback Learning](FEEDBACK_LEARNING.md).

### Example

Call `memory_record_task_outcome`:

```json
{
  "retrieval_id": 42,
  "changed_files": ["src/auth.py", "tests/test_auth.py"],
  "test_evidence": {
    "command": "python -m pytest tests/test_auth.py",
    "exit_code": 0,
    "commit_sha": "0123456789abcdef0123456789abcdef01234567"
  }
}
```

The response lists only candidate fact IDs that were retrieved in receipt 42
and anchored to the reported files. The review queue shows which candidates
have received evidence-backed feedback.

## Evidence and safety

- A passing test is **not** proof that any particular memory was helpful,
  and a failing test is **not** proof the memory was incorrect.
- Changed file paths, commands, exit codes, and commits are **reported by
  the calling agent**. CogniGraph does not execute the tests or inspect git
  diffs for this API. A matching current HEAD records
  `reported_pass_at_head`, not `verified`.
- The ledger stores file names, outcome status, the retrieval ID and a hash
  of the test command. It does not store raw shell commands, source contents,
  test output, or copies of user prompts.
- Only previously retrieved, still-active memories can become candidates.
  Hidden paths, absolute paths, parent traversal and dependency directories
  are rejected. No filesystem reads or writes happen in this tool.
- Re-reporting an outcome for the same retrieval replaces the report and
  candidate list, rather than increasing its weight.
- Neither recording an outcome nor viewing the queue affects retrieval ranking
  or memory confidence. Only `memory_rate_context` can record ratings.
- Even a user-tagged rating depends on the calling agent reporting the user
  accurately. A future in-IDE approval UI should make attribution verifiable.
- Retrieval log records are retained under existing project-local storage
  rules; this feature does not implement a retention/deletion policy.
- The OpenCode V2 plugin can now emit best-effort task-outcome reports
  automatically after observed successful file-edit tools when a session
  becomes idle, with the existing retrieval receipt and changed source paths.
  This does not automatically mark tests as passed and does not rate facts.
  MCP tools remain available for manual reports and review.

## Tests

```bash
python -m unittest -v test_task_outcomes.py
```

Longer-term work: collect edit/test observations directly from trusted IDE
events, attach explicit user approvals, validate provenance after edits, and
measure whether fact-level outcome feedback improves retrieval precision.
