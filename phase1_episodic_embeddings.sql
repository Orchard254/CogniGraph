-- Phase 1 fix: separate embeddings for episodic_memories, mirroring memory_embeddings/vec_memory exactly
-- Colliding ID spaces (memory.id vs episodic_memories.memory_id) are now isolated.
-- New tables are keyed to episodic_memories(memory_id), not memory(id).

CREATE TABLE IF NOT EXISTS episodic_embeddings (
    memory_id INTEGER PRIMARY KEY REFERENCES episodic_memories(memory_id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    embedding BLOB NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_episodic_memory USING vec0(embedding float[384]);

-- Mirrors vec_ei / vec_ed / vec_eu exactly, but for episodic_embeddings -> vec_episodic_memory
CREATE TRIGGER IF NOT EXISTS vec_episodic_ei AFTER INSERT ON episodic_embeddings BEGIN
    INSERT OR REPLACE INTO vec_episodic_memory(rowid, embedding) VALUES (new.memory_id, new.embedding);
END;

CREATE TRIGGER IF NOT EXISTS vec_episodic_ed AFTER DELETE ON episodic_embeddings BEGIN
    DELETE FROM vec_episodic_memory WHERE rowid = old.memory_id;
END;

CREATE TRIGGER IF NOT EXISTS vec_episodic_eu AFTER UPDATE ON episodic_embeddings BEGIN
    INSERT OR REPLACE INTO vec_episodic_memory(rowid, embedding) VALUES (new.memory_id, new.embedding);
END;
