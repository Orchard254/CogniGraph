# Source freshness warnings for CogniGraph

## Purpose

CogniGraph retains architectural decisions and coding facts across sessions,
but code evolves. A fact that was correct when recorded can become inaccurate
after a later edit. A source-content hash is evidence of whether the original
source snippet is still present; **a matching hash does not prove that the
higher-level statement is correct**.

In this phase, source freshness is checked **read-only while creating task and
file context briefings**. Only facts with an explicit `file_path` and
`code_hash` are eligible. The existing memory database must be in the
project's `.cognigraph/memory.db` or legacy `.project-memory/memory.db`
directory so the workspace root can be identified without guessing.

## User-visible results

| Source status | Model-visible behavior |
|---|---|
| `CURRENT` | No extra warning; the anchored bytes still match |
| `RELOCATED` | Reports that the snippet moved within 30 lines, without silently editing the stored range |
| `POSSIBLY_STALE` | Warns the agent to verify current code; content may have changed |
| `SOURCE_MISSING` | Warns that the source file is missing and the fact needs review |
| `UNVERIFIED` | Explains why the source provenance could not be checked |

Checks support a whole-file SHA-256 or a line-range SHA-256 with line endings
included, consistent with stored source spans. They cap file reads at 2 MiB
and reuse bytes for multiple facts from the same file during one briefing.

## Safety boundaries

- No automatic deprecation, confidence changes, hash rewrites, line-range
  relocation, source edits, or learning from these warnings.
- Relative paths must resolve inside the active project. Absolute paths,
  parent traversal, and symlinks escaping the project are rejected.
- Source bytes are used only locally for hashing. They are not printed into
  the warning.
- No source hash means no ability to establish freshness; the absence of a
  warning is **not** proof that every fact is current.
- A changed snippet means `POSSIBLY_STALE`, not "false". Decisions can remain
  true even when surrounding code changes.
- A snippet can match even when *other code elsewhere* has invalidated the
  decision. Semantic verification requires separate tests or human review.
- Files larger than 2 MiB and files with unrecognizable provenance are
  reported as unverified rather than guessed about.
- Freshness checking is limited to model-visible briefing construction, not
  every raw search result or background update.

## Validation

Run:

```bash
python -m unittest -v test_source_freshness.py
```

The tests cover matching and changed source, moved spans, missing files,
whole-file hashes, path traversal, symlink escapes, oversized source,
unknown anchors, and integration with live MemoryStore briefings.

## Next phase

Collect verification outcomes after coding tasks, associate source changes and
test results with the exact facts they support, and offer explicit review of
stale facts. Do not turn a successful test into automatic blanket approval of
all related memories.
