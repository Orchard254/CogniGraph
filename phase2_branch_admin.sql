-- Phase 2 slice: branch / tombstone / promotion / lineage administrative layer
-- Layered on top of deployed Phase 0 + Phase 1 tables.
-- COEXISTENCE: no ALTER/DROP on existing tables; memory_branches extended
-- only via triggers (branch_tombstones is per-branch terminal record for a
-- discarded branch — distinct from blueprint Section 20's per-record target
-- tombstone; memory_lineage_events here tracks branch lifecycle
-- CREATED/PROMOTED/DISCARDED — distinct from blueprint Section 15's
-- memory-evolution SUPERSEDES/EVOLVES_FROM/CORRECTS; these refinements are
-- intentional single-level Phase 0 scope reductions — flagged in test/SPEC
-- preamble where v2.2.0 text differs).
-- No memory-copying logic in this slice; only the administrative state
-- machine that the copying algorithm will use next.

-- Branch tombstones: terminal record for a discarded experimental branch.
-- One tombstone per discarded branch (UNIQUE branch_id). The blueprint's
-- per-record (branch_id, target_record_type, target_record_id) tombstone
-- is deferred as a future per-record refinement.
CREATE TABLE IF NOT EXISTS branch_tombstones (
    tombstone_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id            INTEGER NOT NULL REFERENCES repositories(repo_id),
    branch_id          INTEGER NOT NULL REFERENCES memory_branches(branch_id),
    discarded_at       TEXT NOT NULL,
    discarded_revision INTEGER NOT NULL,
    reason             TEXT,
    UNIQUE(branch_id)
);

-- Promotion ledger: one row per successful promotion of an experimental
-- branch into its canonical root. New canonical records receive new IDs;
-- the experimental branch is not transferred (promotion-as-copy-with-new-IDs
-- per blueprint Section 21). Target is always the canonical root in Phase 0
-- (Section 21). Source may be promoted at most once (UNIQUE source).
CREATE TABLE IF NOT EXISTS branch_promotions (
    promotion_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id            INTEGER NOT NULL REFERENCES repositories(repo_id),
    source_branch_id   INTEGER NOT NULL REFERENCES memory_branches(branch_id),
    target_branch_id   INTEGER NOT NULL REFERENCES memory_branches(branch_id),
    convergence_policy TEXT NOT NULL CHECK(convergence_policy IN ('KEEP_BOTH')),
    promoted_at        TEXT NOT NULL,
    created_revision   INTEGER NOT NULL,
    UNIQUE(source_branch_id)
);

-- Branch lifecycle audit log. Append-only. Event types are branch-level
-- CREATED/PROMOTED/DISCARDED in this slice (not the memory-evolution
-- SUPERSEDES/EVOLVES_FROM/CORRECTS of Section 15 — that lineage is a later
-- slice). Related promotion/tombstone FKs are required per event_type.
CREATE TABLE IF NOT EXISTS memory_lineage_events (
    event_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id              INTEGER NOT NULL REFERENCES repositories(repo_id),
    branch_id            INTEGER NOT NULL REFERENCES memory_branches(branch_id),
    event_type           TEXT NOT NULL CHECK(event_type IN ('CREATED','PROMOTED','DISCARDED')),
    created_revision     INTEGER NOT NULL,
    occurred_at          TEXT NOT NULL,
    related_promotion_id INTEGER REFERENCES branch_promotions(promotion_id),
    related_tombstone_id INTEGER REFERENCES branch_tombstones(tombstone_id),
    CHECK (
        (event_type = 'CREATED'   AND related_promotion_id IS NULL AND related_tombstone_id IS NULL) OR
        (event_type = 'PROMOTED'  AND related_promotion_id IS NOT NULL AND related_tombstone_id IS NULL) OR
        (event_type = 'DISCARDED' AND related_tombstone_id IS NOT NULL AND related_promotion_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_tombstones_branch ON branch_tombstones(branch_id);
CREATE INDEX IF NOT EXISTS idx_promotions_source ON branch_promotions(source_branch_id);
CREATE INDEX IF NOT EXISTS idx_promotions_target ON branch_promotions(target_branch_id);
CREATE INDEX IF NOT EXISTS idx_lineage_branch ON memory_lineage_events(branch_id);
CREATE INDEX IF NOT EXISTS idx_lineage_promotion ON memory_lineage_events(related_promotion_id);
CREATE INDEX IF NOT EXISTS idx_lineage_tombstone ON memory_lineage_events(related_tombstone_id);

-- ============================================================
-- 1. Branch creation: single-level enforcement
-- Non-root branches must fork directly from a root branch in the same repo.
-- Root branches must not have a parent.
-- ============================================================
CREATE TRIGGER IF NOT EXISTS trg_branches_single_level
BEFORE INSERT ON memory_branches
BEGIN
    SELECT CASE
        WHEN NEW.is_root = 0 AND NEW.parent_branch_id IS NULL THEN
            RAISE(ABORT, 'branch hierarchy violation: non-root branch must have parent_branch_id')
        WHEN NEW.is_root = 0 AND NOT EXISTS (
            SELECT 1 FROM memory_branches
            WHERE branch_id = NEW.parent_branch_id
              AND is_root = 1
              AND repo_id = NEW.repo_id
        ) THEN
            RAISE(ABORT, 'branch hierarchy violation: non-root branch parent must be a root branch in the same repository')
        WHEN NEW.is_root = 1 AND NEW.parent_branch_id IS NOT NULL THEN
            RAISE(ABORT, 'branch hierarchy violation: root branch must not have parent_branch_id')
        WHEN NEW.is_root NOT IN (0,1) THEN
            RAISE(ABORT, 'branch hierarchy violation: is_root must be 0 or 1')
    END;
END;

-- ============================================================
-- 2. Branch creation: auto-insert single CREATED lineage event
-- Single-hop only; memory_lineage_events has no further INSERT triggers
-- that chain, so recursive_triggers=OFF is respected.
-- ============================================================
CREATE TRIGGER IF NOT EXISTS trg_branches_created_lineage
AFTER INSERT ON memory_branches
WHEN NEW.is_root = 0
BEGIN
    INSERT INTO memory_lineage_events (
        repo_id, branch_id, event_type, created_revision, occurred_at,
        related_promotion_id, related_tombstone_id
    ) VALUES (
        NEW.repo_id,
        NEW.branch_id,
        'CREATED',
        (SELECT current_revision FROM repositories WHERE repo_id = NEW.repo_id),
        NEW.created_at,
        NULL,
        NULL
    );
END;

-- ============================================================
-- 3. Root branch status is immutable (canonical root never transitions)
-- ============================================================
CREATE TRIGGER IF NOT EXISTS trg_branches_root_status_immutable
BEFORE UPDATE OF status ON memory_branches
WHEN OLD.is_root = 1 AND NEW.status != OLD.status
BEGIN
    SELECT RAISE(ABORT, 'root branch status is immutable: canonical root never transitions');
END;

-- Branch identity and fork snapshot are immutable after creation. Allow only
-- lifecycle status changes; otherwise a direct SQL UPDATE could move an
-- experimental branch to another repository/root or rewrite its base revision.
CREATE TRIGGER IF NOT EXISTS trg_branches_identity_immutable
BEFORE UPDATE OF repo_id, parent_branch_id, is_root, base_revision, base_timestamp, created_at
ON memory_branches
WHEN NEW.repo_id IS NOT OLD.repo_id
  OR NEW.parent_branch_id IS NOT OLD.parent_branch_id
  OR NEW.is_root IS NOT OLD.is_root
  OR NEW.base_revision IS NOT OLD.base_revision
  OR NEW.base_timestamp IS NOT OLD.base_timestamp
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'branch identity is immutable: repository, parent, root role, and fork snapshot cannot change');
END;

-- ============================================================
-- 4. Terminal branches are permanently immutable (any column)
-- Once PROMOTED or DISCARDED, no further UPDATE of any kind is permitted.
-- This covers re-promotion, discard-after-promotion, reactivation, and
-- any other field mutation on a terminal branch — single enforcement point.
-- ============================================================
CREATE TRIGGER IF NOT EXISTS trg_branches_terminal_immutable
BEFORE UPDATE ON memory_branches
WHEN OLD.status IN ('PROMOTED','DISCARDED')
BEGIN
    SELECT RAISE(ABORT, 'branch is terminal and immutable: cannot update a PROMOTED or DISCARDED branch');
END;

-- ============================================================
-- 5a. Promotion requires a ledger row at the same revision
-- ============================================================
CREATE TRIGGER IF NOT EXISTS trg_branches_promotion_requires_ledger
BEFORE UPDATE OF status ON memory_branches
WHEN NEW.status = 'PROMOTED' AND OLD.status = 'ACTIVE' AND OLD.is_root = 0
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM branch_promotions
            WHERE source_branch_id = OLD.branch_id
              AND repo_id = OLD.repo_id
        ) THEN
            RAISE(ABORT, 'promotion requires branch_promotions row to exist before status transition')
        WHEN (SELECT created_revision FROM branch_promotions
              WHERE source_branch_id = OLD.branch_id
              ORDER BY promotion_id DESC LIMIT 1)
             != (SELECT current_revision FROM repositories WHERE repo_id = OLD.repo_id)
        THEN
            RAISE(ABORT, 'promotion ledger revision must equal current_revision at time of status transition')
    END;
END;

-- 5b. Discard requires a tombstone row at the same revision
CREATE TRIGGER IF NOT EXISTS trg_branches_discard_requires_tombstone
BEFORE UPDATE OF status ON memory_branches
WHEN NEW.status = 'DISCARDED' AND OLD.status = 'ACTIVE' AND OLD.is_root = 0
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM branch_tombstones
            WHERE branch_id = OLD.branch_id
              AND repo_id = OLD.repo_id
        ) THEN
            RAISE(ABORT, 'discard requires branch_tombstones row to exist before status transition')
        WHEN (SELECT discarded_revision FROM branch_tombstones
              WHERE branch_id = OLD.branch_id)
             != (SELECT current_revision FROM repositories WHERE repo_id = OLD.repo_id)
        THEN
            RAISE(ABORT, 'tombstone revision must equal current_revision at time of status transition')
    END;
END;

-- ============================================================
-- 6. branch_promotions: role and repo isolation
-- target must be root, source must be non-root, both same repo as row
-- ============================================================
CREATE TRIGGER IF NOT EXISTS trg_promotions_branch_roles
BEFORE INSERT ON branch_promotions
BEGIN
    SELECT CASE
        WHEN (SELECT is_root FROM memory_branches WHERE branch_id = NEW.target_branch_id) != 1 THEN
            RAISE(ABORT, 'promotion target must be a root branch')
        WHEN (SELECT is_root FROM memory_branches WHERE branch_id = NEW.source_branch_id) != 0 THEN
            RAISE(ABORT, 'promotion source must be a non-root branch')
        WHEN (SELECT repo_id FROM memory_branches WHERE branch_id = NEW.source_branch_id) != NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: source branch belongs to different repository')
        WHEN (SELECT repo_id FROM memory_branches WHERE branch_id = NEW.target_branch_id) != NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: target branch belongs to different repository')
        WHEN NEW.source_branch_id = NEW.target_branch_id THEN
            RAISE(ABORT, 'promotion source and target must differ')
    END;
END;

-- ============================================================
-- 7. Revision-protocol INSERT stamp checks (mirror observations pattern)
-- branch_promotions.created_revision == current_revision
-- branch_tombstones.discarded_revision == current_revision
-- memory_lineage_events.created_revision == current_revision
-- ============================================================
CREATE TRIGGER IF NOT EXISTS trg_promotions_revision
BEFORE INSERT ON branch_promotions
BEGIN
    SELECT CASE
        WHEN NEW.created_revision IS NULL THEN
            RAISE(ABORT, 'unstamped insert: created_revision is NULL')
        WHEN NEW.created_revision != (SELECT current_revision FROM repositories WHERE repo_id = NEW.repo_id) THEN
            RAISE(ABORT, 'revision mismatch: created_revision does not equal current_revision')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_tombstones_revision
BEFORE INSERT ON branch_tombstones
BEGIN
    SELECT CASE
        WHEN NEW.discarded_revision IS NULL THEN
            RAISE(ABORT, 'unstamped insert: discarded_revision is NULL')
        WHEN NEW.discarded_revision != (SELECT current_revision FROM repositories WHERE repo_id = NEW.repo_id) THEN
            RAISE(ABORT, 'revision mismatch: discarded_revision does not equal current_revision')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_lineage_revision
BEFORE INSERT ON memory_lineage_events
BEGIN
    SELECT CASE
        WHEN NEW.created_revision IS NULL THEN
            RAISE(ABORT, 'unstamped insert: created_revision is NULL')
        WHEN NEW.created_revision != (SELECT current_revision FROM repositories WHERE repo_id = NEW.repo_id) THEN
            RAISE(ABORT, 'revision mismatch: created_revision does not equal current_revision')
        WHEN NEW.event_type = 'PROMOTED' AND NEW.related_promotion_id IS NULL THEN
            RAISE(ABORT, 'PROMOTED lineage requires related_promotion_id')
        WHEN NEW.event_type = 'DISCARDED' AND NEW.related_tombstone_id IS NULL THEN
            RAISE(ABORT, 'DISCARDED lineage requires related_tombstone_id')
        WHEN NEW.event_type = 'CREATED' AND (NEW.related_promotion_id IS NOT NULL OR NEW.related_tombstone_id IS NOT NULL) THEN
            RAISE(ABORT, 'CREATED lineage must not have related promotion or tombstone')
        WHEN NEW.event_type = 'PROMOTED' AND NEW.related_tombstone_id IS NOT NULL THEN
            RAISE(ABORT, 'PROMOTED lineage must not have related_tombstone_id')
        WHEN NEW.event_type = 'DISCARDED' AND NEW.related_promotion_id IS NOT NULL THEN
            RAISE(ABORT, 'DISCARDED lineage must not have related_promotion_id')
    END;
END;

-- ============================================================
-- 8. Repository isolation for tombstones and lineage events
-- (promotions isolation already covered in #6)
-- ============================================================
CREATE TRIGGER IF NOT EXISTS trg_tombstones_repo_isolation
BEFORE INSERT ON branch_tombstones
BEGIN
    SELECT CASE
        WHEN (SELECT repo_id FROM memory_branches WHERE branch_id = NEW.branch_id) != NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: tombstone branch belongs to different repository')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_lineage_repo_isolation
BEFORE INSERT ON memory_lineage_events
BEGIN
    SELECT CASE
        WHEN (SELECT repo_id FROM memory_branches WHERE branch_id = NEW.branch_id) != NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: lineage branch belongs to different repository')
        WHEN NEW.related_promotion_id IS NOT NULL
         AND (SELECT repo_id FROM branch_promotions WHERE promotion_id = NEW.related_promotion_id) != NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: promotion belongs to different repository')
        WHEN NEW.related_tombstone_id IS NOT NULL
         AND (SELECT repo_id FROM branch_tombstones WHERE tombstone_id = NEW.related_tombstone_id) != NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: tombstone belongs to different repository')
    END;
END;

-- The branch lifecycle tables are audit ledgers, not mutable current-state
-- records. Enforce append-only semantics even for direct SQL callers.
CREATE TRIGGER IF NOT EXISTS trg_tombstones_immutable_update
BEFORE UPDATE ON branch_tombstones
BEGIN
    SELECT RAISE(ABORT, 'append-only audit ledger: branch_tombstones cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS trg_tombstones_immutable_delete
BEFORE DELETE ON branch_tombstones
BEGIN
    SELECT RAISE(ABORT, 'append-only audit ledger: branch_tombstones cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_promotions_immutable_update
BEFORE UPDATE ON branch_promotions
BEGIN
    SELECT RAISE(ABORT, 'append-only audit ledger: branch_promotions cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS trg_promotions_immutable_delete
BEFORE DELETE ON branch_promotions
BEGIN
    SELECT RAISE(ABORT, 'append-only audit ledger: branch_promotions cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_lineage_immutable_update
BEFORE UPDATE ON memory_lineage_events
BEGIN
    SELECT RAISE(ABORT, 'append-only audit ledger: memory_lineage_events cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS trg_lineage_immutable_delete
BEFORE DELETE ON memory_lineage_events
BEGIN
    SELECT RAISE(ABORT, 'append-only audit ledger: memory_lineage_events cannot be deleted');
END;
