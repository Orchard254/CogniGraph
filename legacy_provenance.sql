-- Legacy provenance for genesis migration (Section 34)
-- One row per migrated memory.id, preserving original identity and file provenance
-- This table is migration-only, not queried by retrieval; it is the audit trail that
-- facts.id -> episodic_memories.memory_id mapping plus file_path/line/hash came from.

CREATE TABLE legacy_provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
    episodic_memory_id INTEGER NOT NULL REFERENCES episodic_memories(memory_id) ON DELETE CASCADE,
    legacy_memory_id INTEGER NOT NULL,
    legacy_table TEXT NOT NULL DEFAULT 'memory',
    file_path TEXT,
    line_start INTEGER,
    line_end INTEGER,
    code_hash TEXT,
    git_commit_sha TEXT,
    legacy_created_at TEXT,
    migrated_at TEXT NOT NULL
);

CREATE INDEX idx_legacy_provenance_legacy ON legacy_provenance(legacy_memory_id);
CREATE INDEX idx_legacy_provenance_episodic ON legacy_provenance(episodic_memory_id);
