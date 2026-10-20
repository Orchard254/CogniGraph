-- Phase 0 foundational layer. Loaded by MemoryStore's canonical-schema
-- initializer for fresh databases and to repair incomplete prior initialization.
-- Scope: repositories, revision protocol, entities, anchors (single generation),
-- and observations. This file defines the root-branch substrate; episodic memory
-- and dependency tables are added by Phase 1, branch administration by Phase 2.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS repositories (
    repo_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path       TEXT NOT NULL UNIQUE,
    current_revision INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

-- The revision cursor is a monotonic sequence, not a user-settable label.
-- Every legitimate RevisionTransaction (and genesis migration) advances exactly
-- one step. Reject rollback, no-op writes, and unaccounted multi-step jumps.
CREATE TRIGGER IF NOT EXISTS trg_repositories_revision_monotonic
BEFORE UPDATE OF current_revision ON repositories
WHEN NEW.current_revision != OLD.current_revision + 1
BEGIN
    SELECT RAISE(ABORT, 'revision protocol violation: current_revision must advance by exactly one');
END;

-- Minimal branch table: only the auto-created canonical root branch exists
-- in this increment. Full experimental-branch workflow (Sections 16-25) is
-- deferred, but observations need a branch_id to point at per Section 10.

CREATE TABLE IF NOT EXISTS memory_branches (
    branch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id          INTEGER NOT NULL REFERENCES repositories(repo_id),
    parent_branch_id INTEGER REFERENCES memory_branches(branch_id),
    is_root          INTEGER NOT NULL DEFAULT 0 CHECK(is_root IN (0,1)),
    base_revision    INTEGER NOT NULL,
    base_timestamp   TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','PROMOTED','DISCARDED')),
    created_at       TEXT NOT NULL
);

-- Exactly one root branch per repository (Section 7).
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_root_branch_per_repo
    ON memory_branches(repo_id) WHERE is_root = 1;

CREATE TABLE IF NOT EXISTS entities (
    entity_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id        INTEGER NOT NULL REFERENCES repositories(repo_id),
    entity_type    TEXT NOT NULL CHECK(entity_type IN ('FUNCTION','METHOD','CLASS','MODULE')),
    file_path      TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    class_name     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    UNIQUE(repo_id, file_path, entity_type, qualified_name, class_name)
    -- DECISION (superseding the earlier UNIQUE(repo_id, entity_type,
    -- qualified_name) draft): a true globally-resolved qualified_name is not
    -- computable yet -- the codebase deliberately never built cross-file
    -- symbol/scope resolution (see original Phase B checkpoint's rationale
    -- for deferring structural_edges/call-graph work). So entity identity is
    -- kept file-scoped, mirroring structural_nodes' proven
    -- (file_path, node_type, name, class_name) shape. class_name defaults to
    -- '' rather than NULL for the same reason structural_nodes does --
    -- SQLite treats NULLs as distinct in UNIQUE constraints, which would
    -- silently break dedup.
    -- 'import' has no entity_type here deliberately: an import is a
    -- reference to an entity, not an entity itself.
);

-- Section 9 (revised v2.3.0): Phase 0 anchors are single-generation only.
-- No supersedes_anchor_id / supersession_confidence exists yet.
CREATE TABLE IF NOT EXISTS anchors (
    anchor_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id  INTEGER NOT NULL REFERENCES entities(entity_id),
    repo_id    INTEGER NOT NULL REFERENCES repositories(repo_id),
    created_at TEXT NOT NULL
);

-- A cross-repository entity must not be smuggled into another repository
-- by declaring a different repo_id on its otherwise-valid anchor.
CREATE TRIGGER IF NOT EXISTS trg_anchors_repo_isolation
BEFORE INSERT ON anchors
BEGIN
    SELECT CASE
        WHEN (SELECT repo_id FROM entities WHERE entity_id = NEW.entity_id) IS NOT NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: anchor entity belongs to a different repository')
    END;
END;

-- Anchors are immutable (Section 9). Enforced at the DB level, not just by
-- application convention, per the "correctness must hold even if the
-- application wrapper is bypassed" principle (Section 29).
CREATE TRIGGER IF NOT EXISTS trg_anchors_immutable
BEFORE UPDATE ON anchors
BEGIN
    SELECT RAISE(ABORT, 'anchors are immutable: no UPDATE is ever permitted');
END;

CREATE TRIGGER IF NOT EXISTS trg_anchors_immutable_delete
BEFORE DELETE ON anchors
BEGIN
    SELECT RAISE(ABORT, 'anchors are immutable: deletion is never permitted');
END;

CREATE TABLE IF NOT EXISTS observations (
    observation_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    anchor_id        INTEGER NOT NULL REFERENCES anchors(anchor_id),
    branch_id        INTEGER NOT NULL REFERENCES memory_branches(branch_id),
    repo_id          INTEGER NOT NULL REFERENCES repositories(repo_id),
    file_path        TEXT NOT NULL,
    ast_symbol       TEXT,
    content_hash     TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    valid_from       TEXT NOT NULL,
    valid_until      TEXT,
    created_revision INTEGER NOT NULL,
    closed_revision  INTEGER,
    CHECK (valid_until IS NULL OR valid_until > valid_from)
);

-- ============================================================
-- Revision protocol enforcement (Sections 28-31)
-- ============================================================

-- INSERT: created_revision must equal the repo's current_revision at time
-- of insert, and closed_revision must be NULL. Catches unstamped (NULL),
-- stale (< current), and future (> current) inserts with one invariant.
CREATE TRIGGER IF NOT EXISTS trg_observations_insert_revision
BEFORE INSERT ON observations
BEGIN
    SELECT CASE
        WHEN NEW.created_revision IS NULL THEN
            RAISE(ABORT, 'unstamped insert: created_revision is NULL')
        WHEN NEW.created_revision != (SELECT current_revision FROM repositories WHERE repo_id = NEW.repo_id) THEN
            RAISE(ABORT, 'revision mismatch: created_revision does not equal current_revision')
        WHEN NEW.closed_revision IS NOT NULL THEN
            RAISE(ABORT, 'invalid insert: closed_revision must be NULL on insert')
    END;
END;

-- UPDATE: the only legal transition is closing an open record (closed_revision
-- NULL -> current_revision). All identity/provenance fields must be unchanged.
-- Once closed, closed_revision itself becomes immutable.
CREATE TRIGGER IF NOT EXISTS trg_observations_update_revision
BEFORE UPDATE ON observations
BEGIN
    SELECT CASE
        WHEN OLD.closed_revision IS NOT NULL THEN
            RAISE(ABORT, 'immutable: record already closed, closed_revision cannot change')
        WHEN NEW.anchor_id IS NOT OLD.anchor_id
          OR NEW.branch_id IS NOT OLD.branch_id
          OR NEW.repo_id IS NOT OLD.repo_id
          OR NEW.file_path IS NOT OLD.file_path
          OR NEW.ast_symbol IS NOT OLD.ast_symbol
          OR NEW.content_hash IS NOT OLD.content_hash
          OR NEW.recorded_at IS NOT OLD.recorded_at
          OR NEW.valid_from IS NOT OLD.valid_from
          OR NEW.created_revision IS NOT OLD.created_revision THEN
            RAISE(ABORT, 'immutable-field mutation: identity/provenance fields cannot change')
        WHEN NEW.closed_revision IS NULL THEN
            RAISE(ABORT, 'invalid update: only a closure transition (NULL -> current_revision) is permitted')
        WHEN NEW.closed_revision != (SELECT current_revision FROM repositories WHERE repo_id = NEW.repo_id) THEN
            RAISE(ABORT, 'invalid closure: closed_revision must equal current_revision at time of closure')
        WHEN NEW.closed_revision <= NEW.created_revision THEN
            RAISE(ABORT, 'invalid closure: closed_revision must exceed created_revision')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_observations_immutable_delete
BEFORE DELETE ON observations
BEGIN
    SELECT RAISE(ABORT, 'observation history is immutable: deletion is never permitted');
END;

-- Repository isolation (Section 27): anchor and branch referenced by an
-- observation must belong to the same repository as the observation itself.
CREATE TRIGGER IF NOT EXISTS trg_observations_repo_isolation
BEFORE INSERT ON observations
BEGIN
    SELECT CASE
        WHEN (SELECT repo_id FROM anchors WHERE anchor_id = NEW.anchor_id) != NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: anchor belongs to a different repository')
        WHEN (SELECT repo_id FROM memory_branches WHERE branch_id = NEW.branch_id) != NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: branch belongs to a different repository')
    END;
END;
