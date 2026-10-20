"""Deleting a source file must retire its generated stub but preserve curated facts."""
import watcher
from memory_store import MemoryStore


def test_deleted_file_deprecates_only_generated_stub_and_prunes_structure(tmp_path, monkeypatch):
    project = tmp_path / "project"
    source = project / "src" / "svc.py"
    source.parent.mkdir(parents=True)
    source.write_text("def serve():\n    return 'ok'\n", encoding="utf-8")

    store = MemoryStore(db_path=str(tmp_path / ".cognigraph" / "memory.db"))
    # Keep the supplied connection for this deterministic same-thread test.
    monkeypatch.setattr(watcher, "MemoryStore", None)
    try:
        structural = store.sync_structural_nodes(
            "src/svc.py",
            [{"type": "function", "name": "serve", "line_start": 1, "line_end": 2}],
            project_root=str(project),
        )
        assert structural["upserted"] == 1

        stub_id = store.write(
            category="other",
            content="Source file: src/svc.py (python, 2 lines, hash 12345678)",
            file_path="src/svc.py",
            tags="bootstrap, file, auto-watched",
            importance=2,
            line_start=1,
            line_end=2,
            code_hash="12345678",
            force=True,
            embed=False,
        )
        curated_id = store.write(
            category="component",
            content="The service endpoint validates its request before dispatch.",
            file_path="src/svc.py",
            tags="architecture",
            force=True,
            embed=False,
        )

        source.unlink()
        watcher._prune_deleted_file("src/svc.py", store)

        rows = {
            row["id"]: row
            for row in store.conn.execute(
                "SELECT id, deprecated_at, deprecated_reason FROM memory WHERE id IN (?, ?)",
                (stub_id, curated_id),
            ).fetchall()
        }
        assert rows[stub_id]["deprecated_at"] is not None
        assert rows[stub_id]["deprecated_reason"] == (
            "source file removed from workspace: src/svc.py"
        )
        assert rows[curated_id]["deprecated_at"] is None
        assert store.conn.execute(
            "SELECT COUNT(*) FROM structural_nodes WHERE file_path = ?",
            ("src/svc.py",),
        ).fetchone()[0] == 0
    finally:
        store.close()
