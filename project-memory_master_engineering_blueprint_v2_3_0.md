# Master Engineering Blueprint: Project-Memory

**Document Status:** FINAL PHASE 0 ARCHITECTURAL SPECIFICATION  
**Version:** 2.3.0  
**Date:** August 23, 2026  
**Status:** IMPLEMENTATION-READY — PHASE 0 ARCHITECTURE FROZEN  
**Primary Objective:** Define the canonical memory substrate, migration path, one-level branch model, temporal/revision invariants, and implementation gates for `project-memory`.

**Changelog from v2.2.0 → v2.3.0:**
- Reconciled against the live production system: the existing, tested, and
  deployed `structural_nodes` / `fact_node_links` layer (tree-sitter-based, 49
  passing tests, live 4th RRF lane verified against production data) is
  preserved as-is. It is NOT replaced by this document.
- The new anchor-based `structural_edges` model is reclassified from an
  implicit Phase 0 requirement to explicit Phase IV future work.
- Section 9 (Anchor Model) revised: Phase 0 anchors are single-generation only
  (root anchors). The `supersedes_anchor_id` / `supersession_confidence`
  mechanism is deferred to Phase IV, contingent on first defining a
  deterministic, testable identity-resolution strategy — it is explicitly not
  invented at this stage, since it would otherwise require the kind of fuzzy
  rename/continuity matching the existing system deliberately does not perform.
- Sections 3, 5.2, 6, 11, 27, 42, 46, 47 updated for consistency with the above.

---

# 1. Executive Summary

`project-memory` is not merely a RAG database. It is a **Memory Controller for AI coding agents**.

The central problem is **context thrashing**: agents consume their context window with large amounts of raw filesystem state, causing attention dilution, forgotten constraints, state amnesia, and loss of prior rationale.

The system therefore separates:

**Physical Reality → Evidence → Belief → Dependency → Branch**

while keeping canonical state independent from derived retrieval projections.

The architecture has four dimensions:

| Dimension | Question |
|---|---|
| **Temporal (T)** | When was this true? |
| **Epistemic (E)** | How certain / trustworthy is this? |
| **Causal (C)** | What beliefs depend on this? |
| **Counterfactual (B)** | What was believed under an alternative hypothesis? |

Phase 0 freezes the **canonical storage substrate and its invariants**. Retrieval policy, epistemic controller behavior, semantic reasoning, and advanced branching remain future-phase concerns.

---

# 2. Architectural North Star

> **Canonical state and derived indexes are separate layers.**

Canonical state is the durable source of truth.

Derived indexes are reconstructible projections.

Deleting an FTS index, vector index, cache, or centrality projection must never destroy canonical knowledge.

```text
Canonical State
      ↓
Derived Projections
      ↓
Retrieval / Ranking
```

The inverse must remain possible:

```text
Canonical State
   → rebuild FTS
   → rebuild vectors
   → recompute graph metrics
   → rebuild caches
```

---

# 3. Phase Boundary

## Phase 0 freezes

- canonical storage model
- repository/entity/anchor identity
- observations
- existing `structural_nodes` / `fact_node_links` layer preserved as-is
- episodic memories
- dependencies
- one-level experimental branches
- tombstones
- lineage events
- promotion ledger
- revision protocol
- migration baseline
- canonical invariants
- runtime verification gates

## Deferred to future phases

- nested branches
- recursive branch ancestry resolution
- advanced RRF weighting
- epistemic trust scoring
- falsification propagation engine
- semantic concept extraction
- autonomous hypothesis selection
- advanced graph hub dampening
- historical vector indexes
- advanced consolidation policies

These future systems are **architectural intent, not Phase 0 implementation contracts**.

---

# 4. Five Epistemic Primitives

## 4.1 Observation

Physical reality.

Phase 0 preserves the existing structural representation (`structural_nodes` / `fact_node_links`) as-is. The new anchor-based `structural_edges` model is introduced in Phase IV.

Primary Phase 0 table:

- `observations`

Examples:

- file contains a function
- symbol exists
- parser detects a call
- file content has a specific hash

---

## 4.2 Evidence

Where a belief came from.

Primary table:

- `memory_sources`

Evidence can originate from:

- `AGENT_DECISION`
- `TASK_CHECKPOINT`
- `USER_INSTRUCTION`
- `DETECTED_CHANGE`

---

## 4.3 Belief

A conclusion, decision, rationale, constraint, assumption, or context.

Primary table:

- `episodic_memories`

---

## 4.4 Dependency

Relationship between beliefs.

Primary table:

- `memory_dependencies`

Supported relations:

```text
SUPPORTS
DERIVED_FROM
CONTRADICTS
REQUIRES
```

---

## 4.5 Branch

A **one-level task-scoped experimental overlay** of canonical state.

Primary table:

- `memory_branches`

Phase 0 explicitly does **not** allow branch-of-branch nesting.

---

# 5. Identity Model

Four concepts must remain distinct.

## 5.1 Record Identity

The unique historical database record:

- `memory_id`
- `observation_id`
- `edge_id`
- `dependency_id`

Never reused.

## 5.2 Logical State Identity

Comparison identity used by overlay/diff logic.

Examples:

```text
Observation:
(anchor_id, content_hash)

Structural edge (Phase IV — not a Phase 0 concept):
(source_anchor_id, target_anchor_id, edge_type)

Episodic memory:
(anchor_id, content)

Dependency:
(source_memory_id, target_memory_id, relation_type)
```

Logical state identity is not unique.

## 5.3 Branch Assertion

The branch in which a record was authored:

```text
branch_id
```

## 5.4 Effective Visibility

Whether a record is visible after applying branch-local overrides and exclusions.

This is computed, not stored as permanent truth.

---

# 6. Canonical Storage Tables

| Table | Purpose |
|---|---|
| `repositories` | Repository isolation + revision counter |
| `entities` | Deterministic code entities |
| `anchors` | Structural generations |
| `observations` | Temporal physical reality |
| `structural_nodes` / `fact_node_links` | Existing deterministic structural representation, preserved during migration |
| `structural_edges` | New anchor-based deterministic relationships (Phase IV) |
| `episodic_memories` | Beliefs and rationale |
| `memory_dependencies` | Epistemic graph |
| `memory_branches` | One-level experimental overlays |
| `branch_tombstones` | Branch-local record deletion markers |
| `memory_lineage_events` | Branch-scoped memory evolution |
| `branch_promotions` | Promotion audit ledger |
| `memory_sources` | Evidence/provenance |
| `task_sessions` | Task boundaries |
| `task_events` | Immutable operational history |
| `memory_categories` | Memory classification |
| `memory_tags` | Search/filter metadata |
| `legacy_provenance` | Migration-only preservation |

---

# 7. Repository Model

Each repository has:

- stable `repo_id`
- unique normalized `root_path`
- `current_revision`
- exactly one canonical root branch

The root branch is automatically created.

Manual root insertion is prohibited.

---

# 8. Entity Model

Phase 0 deterministic entity types:

```text
FUNCTION
CLASS
MODULE
```

Semantic concepts are deferred.

No LLM inference is permitted for deterministic code graph construction.

---

# 9. Anchor Model (Phase 0 scope revised)

An anchor binds an entity to a particular structural generation.

**Phase 0 scope:** anchors are single-generation only. Phase 0 does not implement
anchor-to-anchor supersession, lineage, or continuity across structural generations.
Every Phase 0 anchor is a root anchor.

Rationale: the existing production system (`structural_nodes`) deliberately does
NOT perform fuzzy rename/continuity matching — renames are handled by pruning the
stale row and creating a new one under a new identity, by design (see Section 42).
Introducing an anchor-to-anchor supersession mechanism would require a
confidence-scoring method for "this is probably the same underlying entity as
that one," and no such deterministic, testable identity-resolution design has
been specified. Per explicit review decision, this capability is **not invented
at this stage**.

Phase 0 fields:

- `anchor_id`
- `entity_id`
- `created_at`

Rules:

- every Phase 0 anchor is a root anchor (no supersession relationship exists yet)
- anchors immutable

**Deferred to Phase IV:** anchor-to-anchor lineage (`supersedes_anchor_id`,
`supersession_confidence`), contingent on first defining a deterministic,
testable identity-resolution strategy — not merely adding the columns. Until
that strategy is specified and validated, non-root anchors must not be
introduced.

---

# 10. Observation Model

Observations represent physical reality.

Key fields:

```text
observation_id
anchor_id
branch_id
file_path
ast_symbol
content_hash
recorded_at
valid_from
valid_until
created_revision
closed_revision
```

Rules:

```text
valid_until IS NULL
OR valid_until > valid_from
```

Historical identity/provenance fields are immutable.

Only lifecycle closure is mutable.

Revision-bounded visibility:

```text
created_revision <= snapshot_revision
AND
(
    closed_revision IS NULL
    OR closed_revision > snapshot_revision
)
```

---

# 11. Structural Edges (Phase IV Target)

The new anchor-based structural edge model is **Phase IV work**. Phase 0 preserves the existing `structural_nodes` / `fact_node_links` representation without replacing it.

Initial future-phase edge types:

```text
CALLS
IMPORTS
INHERITS
INSTANTIATES
```

Structural edges are:

- branch-scoped
- repository-scoped
- temporally bounded
- immutable except closure

Active uniqueness:

```text
(branch_id, source_anchor_id, target_anchor_id, edge_type)
```

Graph centrality remains derived retrieval metadata.

---

# 12. Episodic Memories

Episodic memories contain:

- decisions
- rationale
- constraints
- assumptions
- contextual knowledge

States:

```text
ACTIVE
SUPERSEDED
ORPHANED
INVALIDATED
```

Multiple ACTIVE memories per anchor are explicitly allowed.

This is intentional: different beliefs can coexist.

Example:

```text
Decision: "Use JWT."
Constraint: "Tokens expire after 15 minutes."
Rationale: "Centralize validation."
Assumption: "Provider supports RS256."
```

---

# 13. Memory Dependencies

A dependency records an epistemic relation.

Key fields:

```text
dependency_id
branch_id
source_memory_id
target_memory_id
relation_type
created_revision
closed_revision
status
```

The dependency's `branch_id` is the **assertion branch**.

Endpoints may physically belong to another branch if they are effectively visible in the assertion branch.

Physical ownership and effective visibility are distinct.

---

# 14. Dependency Lifecycle

Valid states:

```text
ACTIVE
INVALIDATED
SUPERSEDED
```

History is never deleted.

Legacy unlink operations become dependency invalidation rather than deletion.

---

# 15. Memory Lineage Events

Memory evolution is distinct from epistemic dependency.

Table:

```text
memory_lineage_events
```

Supported relations:

```text
SUPERSEDES
EVOLVES_FROM
CORRECTS
```

Lineage events are:

- immutable
- branch-scoped
- revision-stamped

They do not mutate the source or successor memory.

---

# 16. Phase 0 Branch Model

Phase 0 supports exactly one branch level:

```text
Canonical Root
      ↓
Experimental Branch
```

Experimental branches:

- are direct children of the canonical root
- have no children
- have fixed `base_revision`
- contain local overrides/tombstones/lineage assertions
- can be promoted directly to canonical
- can be discarded

Nested branches are deferred.

This is a deliberate scope reduction.

---

# 17. Branch Workflow

Concrete Phase 0 workflow:

```text
Agent receives risky coding task
          ↓
Create experimental memory branch
          ↓
Record observations / decisions
          ↓
Run validation/tests
      ↙           ↘
 SUCCESS          FAILURE
    ↓                ↓
 PROMOTE          DISCARD
    ↓
Canonical state updated
```

The purpose of the branch is to prevent speculative task memory from contaminating canonical knowledge.

---

# 18. Branch Snapshot

Every experimental branch stores:

```text
base_revision
base_timestamp
```

`base_revision` is authoritative.

`base_timestamp` is retained for human-readable chronology.

The branch sees the canonical state **as of `base_revision`**, plus its own local records.

No parent mutation after the fork silently enters the branch.

---

# 19. One-Level Effective State

Because Phase 0 has no nested branches, effective state is:

```text
canonical state at base_revision
+
branch-local records
-
branch-local tombstones
-
branch-local lineage supersessions
```

No recursive branch resolver is required.

This removes the 3-level/5-level nested branch machinery from Phase 0.

---

# 20. Tombstones

Table:

```text
branch_tombstones
```

Identity:

```text
(branch_id, target_record_type, target_record_id)
```

A tombstone means:

> Hide this exact historical record from this branch.

It does not:

- delete the target
- hide sibling records
- hide future records with the same logical state
- mutate historical identity

A future branch-local record may reintroduce the same logical state under a new record identity.

---

# 21. Branch Promotion

Phase 0 promotion target is always the canonical root.

Promotion is:

```text
experimental snapshot
        ↓
compare with current canonical
        ↓
calculate delta
        ↓
apply atomic transformation
        ↓
record audit
        ↓
mark experiment PROMOTED
```

Experimental records are never transferred directly into canonical.

New canonical records receive new IDs.

---

# 22. Promotion Diff

Classifications:

```text
UNCHANGED
ADDITION
REMOVAL
MODIFICATION
CONVERGENT
```

### UNCHANGED

Same database identity inherited unchanged.

### ADDITION

Experimental state absent from canonical.

### REMOVAL

Canonical state absent/tombstoned in experimental.

### MODIFICATION

Logical state differs within the same semantic context.

### CONVERGENT

Different database IDs share the same logical state identity.

---

# 23. Convergent Policy

Phase 0 default:

```text
KEEP_BOTH
```

Independent provenance must not be destroyed merely because two memories contain the same logical state.

Future semantic layers may introduce category-specific consolidation policies.

---

# 24. Promotion Finality

Once promoted:

```text
memory_branches.status = PROMOTED
```

The source branch becomes an immutable historical ledger.

It cannot be:

- updated
- reactivated
- deleted
- promoted again

Promotion audit rows are also immutable and undeletable.

---

# 25. Promotion Authorization

The database must reject:

- cross-repository promotion
- discarded branch promotion
- canonical-root promotion
- duplicate promotion of the same source branch
- task authorization from another repository

`promoted_by_task_id = NULL` is permitted for explicit system/administrative paths.

---

# 26. Memory Source Cross-Branch Authorization

A `memory_sources` row may link to an observation when:

```text
memory.branch_id == observation.branch_id
```

or when:

```text
branch_promotions
source_branch_id = observation.branch_id
target_branch_id = memory.branch_id
```

No implicit global cross-branch evidence access exists.

---

# 27. Repository Isolation

The following must obey repository consistency:

- anchors
- observations
- memories
- structural edges (Phase IV anchor-based representation; the existing
  file-path-scoped `structural_nodes` / `fact_node_links` layer is already
  implicitly repository-scoped by virtue of being keyed to a single project's
  file paths, and requires no change here)
- branches
- branch promotions
- lineage

Cross-repository structural or lineage connections are prohibited.

---

# 28. Monotonic Revision Protocol

Each normal mutation transaction:

```text
BEGIN IMMEDIATE
    ↓
current_revision += 1
    ↓
R = current_revision
    ↓
perform all related mutations with R
    ↓
validate
    ↓
COMMIT
```

`RevisionTransaction` is the application convenience layer.

SQLite is the authoritative integrity layer.

---

# 29. Revision Invariants

INSERT:

```text
created_revision == current_revision
closed_revision IS NULL
```

Lifecycle closure:

```text
created_revision unchanged
closed_revision == current_revision
```

Closed records:

```text
closed_revision immutable
```

Core identity:

```text
immutable after insertion
```

Guarantee:

> **No mutation can produce a revision-inconsistent canonical state, even when the application wrapper is bypassed.**

---

# 30. Trigger Design Rule

Correctness must not depend on trigger creation order.

Never assume:

> trigger A fires before trigger B

as an architectural guarantee.

Lifecycle validation and immutability validation must be independently correct.

Preferred lifecycle mutation:

```text
valid_until + closed_revision
```

or:

```text
status
effective_until
superseded_by_memory_id
closed_revision
```

in one explicit transaction update.

Triggers validate the resulting transition.

If internal trigger updates are retained anywhere, recursive-trigger behavior must be explicitly tested on the target SQLite runtime.

---

# 31. Runtime Spike Requirement

Before Phase 0 completion is declared, prototype against the real target runtime:

- Python version
- SQLite version
- sqlite-vec version
- fastembed version
- OS

Required tests:

- recursive CTE behavior where used
- trigger composition
- lifecycle/immutability interaction
- recursive trigger behavior where applicable
- WAL concurrency
- `BEGIN IMMEDIATE`
- sqlite-vec creation/query
- bulk insertion

A logically valid DDL is not considered proven until the runtime spike succeeds.

---

# 32. Genesis Migration

Genesis migration is a one-time controlled transaction.

```text
BEGIN IMMEDIATE
    ↓
repositories.current_revision = 1
    ↓
migrate baseline records
    ↓
active records.created_revision = 1
closed records.created_revision = 1
closed records.closed_revision = 1
    ↓
preserve legacy provenance
    ↓
COMMIT
```

Normal revisions begin at `2`.

Genesis does not use normal `RevisionTransaction`.

---

# 33. Legacy Migration

Migration path:

```text
Legacy
  ↓
Canonical migration adapters
  ↓
Parity tests
  ↓
Compatibility layer
  ↓
Production validation
  ↓
Legacy retirement
```

No legacy data may be silently discarded.

---

# 34. Legacy Mapping

Conceptual mapping:

```text
facts.id
→ episodic_memories.memory_id

facts.content
→ episodic_memories.content

facts.file_path
→ observations.file_path

facts.content_hash
→ observations.content_hash

facts.status
→ episodic_memories.status

facts.superseded_by
→ episodic_memories.superseded_by_memory_id

facts.embedding
→ vec_memories

facts.category
→ memory_categories

facts.tags
→ memory_tags
```

Legacy line ranges and timestamps without canonical equivalents go into `legacy_provenance`.

---

# 35. Derived Indexes

Derived projections include:

- FTS5
- `vec_memories`
- centrality
- retrieval caches

All are rebuildable.

Canonical state does not depend on their continued existence.

---

# 36. Embedding Storage

Preferred vector projection:

```text
vec_memories
```

using sqlite-vec when available.

Vector storage is derived.

Semantic retrieval must degrade gracefully if the vector index is unavailable.

---

# 37. FTS5

FTS5 is a projection over canonical memory and metadata.

Potential indexed content:

- episodic memory content
- repository-relative file path
- tags
- categories
- selected structural metadata

It must be rebuildable.

---

# 38. Metadata

Categories:

```text
memory_categories
```

Tags:

```text
memory_tags
```

These preserve the practical utility of the legacy system without conflating metadata with epistemic provenance.

---

# 39. Graph Hub Dampening

Hub dampening is a retrieval concern.

It may eventually use:

- degree penalties
- PageRank
- relation weights
- structural distance
- lane-specific ranking

It is not stored as canonical truth in Phase 0.

---

# 40. Retrieval Architecture

Phase 0 specifies the existence of retrieval projections but does not freeze future scoring policy.

Future target:

```text
FTS5
Semantic
Graph
Structural
    ↓
fusion
    ↓
utility-per-token allocation
```

The exact RRF constants/weights remain future implementation decisions.

---

# 41. Future Epistemic Controller

The future controller may implement:

```text
observation changes
       ↓
evidence reevaluation
       ↓
dependency traversal
       ↓
affected beliefs
       ↓
verification / invalidation / revision
```

Future epistemic capabilities may include:

- confidence decay
- trust hierarchies
- contradiction detection
- falsification propagation
- belief revision

These are **not Phase 0 implementation requirements**.

---

# 42. Tree-sitter

The existing Tree-sitter-based structural parsing layer is already part of the living system and is **preserved during Phase 0 and Phase 0.5**. The existing `structural_nodes` / `fact_node_links` representation remains operational during migration.

Phase IV extends this existing capability into the new anchor-based structural model. When that work begins, Tree-sitter must:

- deterministically parse supported languages
- prioritize definitions/imports
- aggressively prune local syntax noise
- map structural definitions into `entities` / `anchors`
- produce the new `structural_edges` representation

No LLM inference is permitted for structural graph construction.

---

# 43. MCP Compatibility

The existing MCP interface is preserved during migration.

Requirements:

- existing parameters remain accepted
- new parameters are additive/optional
- existing return fields remain compatible
- existing error behavior remains compatible where practical
- source-code inspection is authoritative for exact mappings

No inferred tool mapping should override the actual implementation.

---

# 44. CLI Compatibility

Applicable commands must retain:

```text
--db <path>
```

Internal/hidden arguments use:

```python
argparse.SUPPRESS
```

Existing CLI workflows remain functional throughout migration.

---

# 45. Agent Workflow Compatibility

Existing agent integration mechanisms must remain compatible, including where implemented:

- memory-first instructions
- repository wiring
- task preparation/checkpointing
- Git hooks
- MCP configuration generation
- bootstrap workflows

The new architecture must be integrated without silently bypassing established enforcement mechanisms.

---

# 46. Phase Ordering

```text
Phase 0
Canonical Storage & Invariants
        ↓
Phase 0.5
Migration & Compatibility Foundation
        ↓
Phase I
Reliable Synchronization
        ↓
Phase II
Provenance & Memory Lifecycle
        ↓
Phase III
Retrieval & Context Engineering
        ↓
Phase IV
Structural Intelligence
        ↓
Phase V
Epistemic Controller
        ↓
Phase VI
Advanced Performance
```

The existing Tree-sitter / `structural_nodes` structural layer remains operational through Phase 0 and Phase 0.5. The new anchor-based `structural_edges` work remains Phase IV.

`fastembed` may proceed independently because it does not alter canonical ontology.

`sqlite-vec` should be integrated after canonical schema stability is established.

---

# 47. Phase 0 Completion Gate

Phase 0 is complete only when:

- canonical schema implemented
- repository isolation proven
- anchor lineage proven
- observation lifecycle proven
- existing `structural_nodes` / `fact_node_links` sync/prune lifecycle preserved and verified
- episodic memory lifecycle proven
- dependency lifecycle proven
- one-level branches proven
- tombstones proven
- lineage events proven
- promotion proven
- revision protocol proven
- runtime trigger spike passed
- migration genesis proven
- adversarial tests passed

---

# 48. Phase 0.5 Completion Gate

Phase 0.5 is complete only when:

- legacy schema frozen
- migration adapters idempotent
- all legacy fields accounted for
- compatibility layer validated
- MCP parity validated
- CLI parity validated
- derived indexes reconstruct successfully
- fresh install validated
- legacy retirement path proven

---

# 49. Adversarial Test Gate

The implementation must include tests for:

### Revision protocol

1. unstamped insert
2. stale insert
3. future insert
4. invalid closure
5. concurrent writers
6. multi-record same revision
7. lifecycle-field mutation
8. immutable-field mutation
9. genesis migration

### Branch

- fork isolation
- parent change after fork
- parent closure after fork
- tombstone propagation
- descendant reintroduction through a new record
- lineage visibility
- inherited dependency
- tombstoned dependency endpoint
- promotion
- convergent state
- promotion rollback
- cross-repository rejection
- discarded branch rejection
- canonical-root promotion rejection

### Runtime

- trigger composition
- lifecycle + immutability interaction
- recursive trigger behavior where applicable
- WAL concurrency
- sqlite-vec availability

---

# 50. Final Architectural Position

`project-memory` is:

> **A temporal, provenance-aware, branch-aware memory substrate for AI coding agents.**

It does not merely store facts.

It preserves:

- what physically existed
- what evidence supported a belief
- what was believed
- what depended on it
- when that state existed
- what experiment produced it
- how the belief evolved
- whether the state became canonical

The fundamental architecture is:

```text
Physical Reality
      ↓
Observation
      ↓
Evidence
      ↓
Belief
      ↓
Dependency
      ↓
Branch / Experiment
      ↓
Validated Promotion
      ↓
Canonical Memory
```

---

# 51. Final Implementation Directive

**Phase 0 canonical storage architecture is now frozen.**

The next step is not another conceptual expansion.

The next step is:

```text
SQLite runtime spike
        ↓
Implement
        ↓
Break it adversarially
        ↓
Repair
        ↓
Retest
        ↓
Genesis migration
        ↓
Parity validation
```

If implementation reveals a contradiction, executable behavior takes precedence over the document and the affected invariant is revised explicitly.

Otherwise:

> **Stop designing. Build it.**

---

*End of Master Engineering Blueprint v2.2.0.*
