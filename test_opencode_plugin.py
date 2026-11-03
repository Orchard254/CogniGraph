"""Unit tests for the OpenCode native integration and its Python bridge."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from install_opencode import install
from opencode_context import get_plugin_context


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
