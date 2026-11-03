"""Unit tests for the OpenCode native integration and its Python bridge."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from install_opencode import install
from opencode_context import get_plugin_context, record_plugin_outcome


class OpenCodePluginTests(unittest.TestCase):
    def test_install_is_idempotent_and_preserves_existing_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / ".opencode" / "package.json"
            cfg.parent.mkdir()
            cfg.write_text(json.dumps({"name": "project", "dependencies": {"existing": "1.0.0"}}))
            first = install(d)
            second = install(d)
            self.assertEqual(first, second)
            self.assertIn("@opencode/plugin", json.loads(cfg.read_text())["dependencies"])
            self.assertEqual(json.loads(cfg.read_text())["dependencies"]["existing"], "1.0.0")
            code = Path(first["plugin"]).read_text()
            self.assertIn('ctx.session.hook("context"', code)
            self.assertIn('ctx.session.hook("prompt"', code)
            self.assertNotIn("__COGNIGRAPH_BRIDGE_JSON__", code)

    def test_file_context_scoped_to_existing_project_source(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db = root / ".cognigraph/memory.db"
            db.parent.mkdir()
            db.touch()
            source = root / "src/main.py"
            source.parent.mkdir()
            source.write_text("x = 1\n")
            (root / ".env").write_text("secret=example")
            seen = []
            class FakeStore:
                def __init__(self, path):
                    seen.append(path)
                def get_file_context(self, name):
                    seen.append(name)
                    return "file briefing for " + name
                def close(self):
                    seen.append("closed")
            with patch.dict(sys.modules, {
                "memory_store": types.SimpleNamespace(MemoryStore=FakeStore),
                "pm_common": types.SimpleNamespace(looks_like_secret=lambda _: False)
            }):
                result = get_plugin_context(
                    d, "", file_paths=[
                        "../escape.py", "/not/the/project.py", ".env",
                        "src/main.py", "src/main.py",
                    ], max_chars=1200)
            self.assertEqual(result["status"], "ok")
            self.assertIn("file briefing for src/main.py", result["context_markdown"])
            self.assertEqual(seen[1:-1], ["src/main.py"])
            self.assertEqual(seen[-1], "closed")

    def test_external_existing_file_is_not_used_as_context(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "project"
            root.mkdir()
            db = root / ".cognigraph/memory.db"
            db.parent.mkdir()
            db.touch()
            outside = parent / "outside.py"
            outside.write_text("private_setting = 3\n")
            seen = []
            class FakeStore:
                def __init__(self, path):
                    seen.append("opened")
                def get_file_context(self, name):
                    seen.append(name)
                    return "not allowed"
                def close(self):
                    seen.append("closed")
            with patch.dict(sys.modules, {
                "memory_store": types.SimpleNamespace(MemoryStore=FakeStore),
                "pm_common": types.SimpleNamespace(looks_like_secret=lambda _: False)
            }):
                result = get_plugin_context(
                    str(root), "", file_paths=["../outside.py"], max_chars=1200)
            self.assertEqual(result["status"], "no_matches")
            self.assertEqual(seen, ["opened", "closed"])

    def test_latest_file_gets_first_place_in_small_context_budget(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db = root / ".cognigraph/memory.db"
            db.parent.mkdir()
            db.touch()
            for name in ("a.py", "b.py"):
                (root / name).write_text("pass\n")
            class FakeStore:
                def __init__(self, path):
                    pass
                def get_file_context(self, name):
                    return name + " " + ("memory " * 200)
                def close(self):
                    pass
            with patch.dict(sys.modules, {
                "memory_store": types.SimpleNamespace(MemoryStore=FakeStore),
                "pm_common": types.SimpleNamespace(looks_like_secret=lambda _: False)
            }):
                result = get_plugin_context(
                    d, "", file_paths=["a.py", "b.py"], max_chars=256)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["context_markdown"].startswith("b.py "))
            self.assertLessEqual(len(result["context_markdown"]), 256)

    def test_automatic_outcome_bridge_restricts_project_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            db = root / ".cognigraph" / "memory.db"
            db.parent.mkdir()
            db.touch()
            source = root / "src/auth.py"
            source.parent.mkdir()
            source.write_text("def auth(): pass\\n")
            (root / ".env").write_text("DO_NOT_READ=example")
            outside = Path(directory) / "outside.py"
            outside.write_text("private=True\\n")
            seen = []
            class FakeStore:
                def __init__(self, path):
                    seen.append(("db", path))
                def record_task_outcome(self, receipt, paths):
                    seen.append(("outcome", receipt, paths))
                    return {"ranking_updated": False, "review_candidate_fact_ids": [2]}
                def close(self):
                    seen.append(("closed",))
            with patch.dict(sys.modules, {
                "memory_store": types.SimpleNamespace(MemoryStore=FakeStore),
            }):
                result = record_plugin_outcome(
                    str(root), 91,
                    ["src/auth.py", str(source), ".env", "../outside.py", str(outside)],
                )
            self.assertEqual(result["status"], "recorded")
            self.assertEqual(seen[1], ("outcome", 91, ["src/auth.py"]))
            self.assertFalse(result["ranking_updated"])
            self.assertEqual(seen[-1], ("closed",))

    def test_automatic_outcome_bridge_does_not_create_memory(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "app.py"
            source.write_text("x = 1\\n")
            self.assertEqual(
                record_plugin_outcome(d, 1, ["app.py"])["status"], "no_project_memory",
            )
            self.assertFalse((Path(d) / ".cognigraph").exists())
            self.assertEqual(
                record_plugin_outcome(d, 1, ["missing.py"])["status"],
                "no_eligible_source_files",
            )
            with self.assertRaises(ValueError):
                record_plugin_outcome(d, True, ["app.py"])
            with self.assertRaises(ValueError):
                record_plugin_outcome(d, 1, ["app.py"] * 21)

    def test_automatic_outcome_bridge_updates_review_queue_not_ranking(self):
        from memory_store import MemoryStore
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db = root / ".cognigraph" / "memory.db"
            db.parent.mkdir()
            source = root / "src/auth.py"
            source.parent.mkdir()
            source.write_text("def auth(): return True\\n")
            store = MemoryStore(str(db))
            try:
                fact = store.write(
                    "component", "Session login authentication routes through auth.py",
                    file_path="src/auth.py", force=True, embed=False,
                )
                receipt = store.conn.execute(
                    """INSERT INTO memory_usage_log
                    (query_text, retrieved_fact_ids, result_count, logged_at)
                    VALUES (?, ?, ?, ?)""",
                    ("fix session login authentication", str(fact), 1, "2026-11-03T00:00:00+00:00"),
                ).lastrowid
            finally:
                store.close()
            result = record_plugin_outcome(d, receipt, ["src/auth.py"])
            self.assertEqual(result["status"], "recorded")
            self.assertEqual(result["review_candidate_fact_ids"], [fact])
            self.assertFalse(result["ranking_updated"])
            store = MemoryStore(str(db))
            try:
                self.assertEqual(
                    store.task_outcome_review_queue()["outcomes"][0]["test_status"],
                    "no_test_report",
                )
                self.assertEqual(store._feedback_boost("fix session login authentication"), {})
            finally:
                store.close()

    def test_custom_plugin_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            custom = Path(d) / ".opencode/plugins/cognigraph/index.ts"
            custom.parent.mkdir(parents=True)
            custom.write_text("// my plugin\n")
            with self.assertRaises(FileExistsError):
                install(d)
            self.assertEqual(custom.read_text(), "// my plugin\n")

    def test_missing_project_memory_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as d:
            result = get_plugin_context(d, "fix authentication")
            self.assertEqual(result["status"], "no_project_memory")
            self.assertFalse((Path(d) / ".cognigraph").exists())

    def test_sensitive_query_is_not_logged(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / ".cognigraph/memory.db"
            db.parent.mkdir()
            db.touch()
            fake_pm = types.SimpleNamespace(looks_like_secret=lambda text: "token=" in text)
            with patch.dict(sys.modules, {"pm_common": fake_pm}):
                result = get_plugin_context(d, "token=abcdefghijklmnopqr123456")
            self.assertEqual(result["status"], "sensitive_query")

    def test_new_bridge_uses_context_intelligence_with_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".cognigraph" / "memory.db"
            db.parent.mkdir()
            db.touch()
            received = []
            class FakeStore:
                def __init__(self, path):
                    received.append(("opened", path))
                def get_intelligent_context(self, query, **kwargs):
                    received.append(("briefing", query, kwargs))
                    return {
                        "context_markdown": "### CogniGraph | Task orientation\\n- [MEM #17] Auth decision",
                        "retrieval_id": 48, "status": "ok", "fact_ids": [17],
                    }
                def close(self):
                    received.append(("closed",))
            with patch.dict(sys.modules, {
                "memory_store": types.SimpleNamespace(MemoryStore=FakeStore),
                "pm_common": types.SimpleNamespace(looks_like_secret=lambda _: False),
            }):
                result = get_plugin_context(directory, "fix login refresh", max_chars=1400)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["retrieval_id"], 48)
            self.assertIn("MEM #17", result["context_markdown"])
            self.assertEqual(received[1][2]["stage"], "task")
            self.assertEqual(received[1][2]["agent_type"], "opencode")
            self.assertEqual(received[1][2]["max_chars"], 1400)
            self.assertEqual(received[-1], ("closed",))

    def test_bridge_passes_project_path_budget_and_agent(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / ".cognigraph/memory.db"
            db.parent.mkdir()
            db.touch()
            calls = []
            class FakeStore:
                def __init__(self, path):
                    calls.append(path)
                def get_context(self, query, **kw):
                    calls.append((query, kw))
                    return "### memory\nimportant fact"
                def close(self):
                    calls.append("closed")
            with patch.dict(sys.modules, {"memory_store": types.SimpleNamespace(MemoryStore=FakeStore),
                                          "pm_common": types.SimpleNamespace(looks_like_secret=lambda text: False)}):
                result = get_plugin_context(d, "fix authentication", max_chars=1200)
            self.assertEqual(result["status"], "ok")
            # Windows may return the same path in short (8.3) or long form.
            self.assertTrue(Path(calls[0]).samefile(db))
            self.assertEqual(calls[1][1]["agent_type"], "opencode")
            self.assertEqual(calls[1][1]["max_chars"], 1200)
            self.assertEqual(calls[-1], "closed")


if __name__ == "__main__":
    unittest.main()
