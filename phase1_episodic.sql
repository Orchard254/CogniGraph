-- Phase 1 slice: episodic_memories + memory_dependencies, layered on top of
-- the deployed Phase 0 foundational layer (repositories, memory_branches,
-- entities, anchors, observations).
--
-- COEXISTENCE: this DDL touches nothing that already exists -- no ALTER, no
-- DROP, memory_edges is left completely alone (relation vocabulary mapping
-- depends_on->REQUIRES, implements->DERIVED_FROM, conflicts_with->CONTRADICTS,
-- relates_to->SUPPORTS is established here for a LATER migration, no data
-- moves in this slice).
--
-- Section 5.2 logical identity (anchor_id, content) is deliberately NOT a
-- UNIQUE constraint: it is an application-level comparison used during branch
-- promotion (later slice). Two genuinely different memories may share an
-- anchor and similar content.

CREATE TABLE IF NOT EXISTS episodic_memories (
    memory_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id                INTEGER NOT NULL REFERENCES repositories(repo_id),
    branch_id              INTEGER NOT NULL REFERENCES memory_branches(branch_id),
    anchor_id              INTEGER REFERENCES anchors(anchor_id),
    content                TEXT NOT NULL,
    category               TEXT NOT NULL CHECK(category IN ('component','decision','constraint','state','other')),
    confidence             TEXT NOT NULL DEFAULT 'assumed' CHECK(confidence IN ('confirmed','assumed','uncertain')),
    status                 TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','SUPERSEDED','INVALIDATED','ORPHANED')),
    superseded_by_memory_id INTEGER REFERENCES episodic_memories(memory_id),
    created_revision       INTEGER NOT NULL,
    closed_revision        INTEGER,
    created_at             TEXT NOT NULL,
    importance             INTEGER CHECK(importance BETWEEN 1 AND 5),
    tags                   TEXT,
    CHECK (closed_revision IS NULL OR closed_revision > created_revision)
    -- ORPHANED is reserved for a future dependency-graph pass; it is never
    -- written at this layer (insert trigger requires ACTIVE, update trigger
    -- only permits ACTIVE -> SUPERSEDED/INVALIDATED).
);

CREATE INDEX IF NOT EXISTS idx_episodic_repo_status ON episodic_memories(repo_id, status);
CREATE INDEX IF NOT EXISTS idx_episodic_created_revision ON episodic_memories(created_revision);

-- INSERT: full revision-protocol stamp check, mirrors trg_observations_insert_revision.
CREATE TRIGGER IF NOT EXISTS trg_episodic_insert_revision
BEFORE INSERT ON episodic_memories
BEGIN
    SELECT CASE
        WHEN NEW.created_revision IS NULL THEN
            RAISE(ABORT, 'unstamped insert: created_revision is NULL')
        WHEN NEW.created_revision != (SELECT current_revision FROM repositories WHERE repo_id = NEW.repo_id) THEN
            RAISE(ABORT, 'revision mismatch: created_revision does not equal current_revision')
        WHEN NEW.closed_revision IS NOT NULL THEN
            RAISE(ABORT, 'invalid insert: closed_revision must be NULL on insert')
        WHEN NEW.status != 'ACTIVE' THEN
            RAISE(ABORT, 'invalid insert: new memories must enter as ACTIVE')
    END;
END;

-- Repository isolation: branch and (when present) anchor must belong to the
-- same repository as the memory itself.
CREATE TRIGGER IF NOT EXISTS trg_episodic_repo_isolation
BEFORE INSERT ON episodic_memories
BEGIN
    SELECT CASE
        WHEN (SELECT repo_id FROM memory_branches WHERE branch_id = NEW.branch_id) != NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: branch belongs to a different repository')
        WHEN NEW.anchor_id IS NOT NULL
         AND (SELECT repo_id FROM anchors WHERE anchor_id = NEW.anchor_id) != NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: anchor belongs to a different repository')
        WHEN NEW.superseded_by_memory_id IS NOT NULL
         AND (SELECT repo_id FROM episodic_memories WHERE memory_id = NEW.superseded_by_memory_id) != NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: successor memory belongs to a different repository')
    END;
END;

-- UPDATE: the ONLY legal transition is ACTIVE -> SUPERSEDED | INVALIDATED,
-- performed atomically with closure. All identity/provenance fields are
-- immutable. Once closed_revision is set the row is fully immutable.
CREATE TRIGGER IF NOT EXISTS trg_episodic_update_revision
BEFORE UPDATE ON episodic_memories
BEGIN
    SELECT CASE
        WHEN OLD.closed_revision IS NOT NULL THEN
            RAISE(ABORT, 'immutable: record already closed, no further updates permitted')
        WHEN NEW.repo_id != OLD.repo_id
          OR NEW.branch_id != OLD.branch_id
          OR NEW.anchor_id != OLD.anchor_id
          OR NEW.content != OLD.content
          OR NEW.category != OLD.category
          OR NEW.confidence != OLD.confidence
          OR NEW.created_revision != OLD.created_revision
          OR NEW.created_at != OLD.created_at THEN
            RAISE(ABORT, 'immutable-field mutation: identity/provenance fields cannot change')
        WHEN OLD.status != 'ACTIVE' THEN
            RAISE(ABORT, 'invalid update: only ACTIVE memories may transition')
        WHEN NEW.status NOT IN ('SUPERSEDED', 'INVALIDATED') THEN
            RAISE(ABORT, 'invalid update: only an ACTIVE -> SUPERSEDED/INVALIDATED transition is permitted')
        WHEN NEW.status = 'SUPERSEDED' AND NEW.superseded_by_memory_id IS NULL THEN
            RAISE(ABORT, 'invalid supersession: SUPERSEDED requires superseded_by_memory_id')
        WHEN NEW.status = 'INVALIDATED' AND NEW.superseded_by_memory_id IS NOT NULL THEN
            RAISE(ABORT, 'invalid invalidation: INVALIDATED must not set superseded_by_memory_id')
        WHEN NEW.closed_revision IS NULL THEN
            RAISE(ABORT, 'invalid update: transition must set closed_revision')
        WHEN NEW.closed_revision != (SELECT current_revision FROM repositories WHERE repo_id = OLD.repo_id) THEN
            RAISE(ABORT, 'invalid closure: closed_revision must equal current_revision at time of closure')
        WHEN NEW.closed_revision <= NEW.created_revision THEN
            RAISE(ABORT, 'invalid closure: closed_revision must exceed created_revision')
    END;
END;

-- Dependency graph between episodic memories. New vocabulary; memory_edges is
-- untouched (migration is a later slice).
CREATE TABLE IF NOT EXISTS memory_dependencies (
    dependency_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id           INTEGER NOT NULL REFERENCES repositories(repo_id),
    source_memory_id  INTEGER NOT NULL REFERENCES episodic_memories(memory_id),
    target_memory_id  INTEGER NOT NULL REFERENCES episodic_memories(memory_id),
    relation_type     TEXT NOT NULL CHECK(relation_type IN ('SUPPORTS','DERIVED_FROM','CONTRADICTS','REQUIRES')),
    created_revision  INTEGER NOT NULL,
    UNIQUE(source_memory_id, target_memory_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_dependencies_source ON memory_dependencies(source_memory_id);
CREATE INDEX IF NOT EXISTS idx_dependencies_target ON memory_dependencies(target_memory_id);

CREATE TRIGGER IF NOT EXISTS trg_dependencies_insert_revision
BEFORE INSERT ON memory_dependencies
BEGIN
    SELECT CASE
        WHEN NEW.created_revision IS NULL THEN
            RAISE(ABORT, 'unstamped insert: created_revision is NULL')
        WHEN NEW.created_revision != (SELECT current_revision FROM repositories WHERE repo_id = NEW.repo_id) THEN
            RAISE(ABORT, 'revision mismatch: created_revision does not equal current_revision')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_dependencies_repo_isolation
BEFORE INSERT ON memory_dependencies
BEGIN
    SELECT CASE
        WHEN (SELECT repo_id FROM episodic_memories WHERE memory_id = NEW.source_memory_id) != NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: source memory belongs to a different repository')
        WHEN (SELECT repo_id FROM episodic_memories WHERE memory_id = NEW.target_memory_id) != NEW.repo_id THEN
            RAISE(ABORT, 'repository isolation violation: target memory belongs to a different repository')
    END;
END;
