-- Episodic FTS5 mirror of memory_fts, for retrieval lane 1b
-- Mirrors memory_fts exactly: CREATE VIRTUAL TABLE memory_fts USING fts5(content, rationale, tags, file_path, content='memory', content_rowid='id')
-- For episodic, we index content and tags (episodic has no rationale/file_path), content_rowid is memory_id

CREATE VIRTUAL TABLE episodic_fts USING fts5(content, tags, content='episodic_memories', content_rowid='memory_id');

-- Mirrors memory_ai / memory_ad / memory_au exactly
CREATE TRIGGER episodic_ai AFTER INSERT ON episodic_memories BEGIN
    INSERT INTO episodic_fts(rowid, content, tags) VALUES (new.memory_id, new.content, new.tags);
END;

CREATE TRIGGER episodic_ad AFTER DELETE ON episodic_memories BEGIN
    INSERT INTO episodic_fts(episodic_fts, rowid, content, tags) VALUES('delete', old.memory_id, old.content, old.tags);
END;

CREATE TRIGGER episodic_au AFTER UPDATE ON episodic_memories BEGIN
    INSERT INTO episodic_fts(episodic_fts, rowid, content, tags) VALUES('delete', old.memory_id, old.content, old.tags);
    INSERT INTO episodic_fts(rowid, content, tags) VALUES (new.memory_id, new.content, new.tags);
END;
