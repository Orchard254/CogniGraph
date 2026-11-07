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

    def test_bridge_prefers_context_intelligence_when_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".cognigraph/memory.db"
            db.parent.mkdir()
            db.touch()
            seen = []
            class FakeStore:
                def __init__(self, path):
                    seen.append(("init", path))
                def get_intelligent_context(self, task, **kwargs):
                    seen.append(("intelligent", task, kwargs))
                    return {
                        "status": "ok",
                        "context_markdown": "### CogniGraph context: selected knowledge",
                        "retrieval_id": 117,
                        "selected": [{"ref": "memory:2", "fact_id": 2}],
                    }
                def get_context(self, *args, **kwargs):
                    raise AssertionError("legacy composer used unexpectedly")
                def close(self):
                    seen.append(("closed",))
            with patch.dict(sys.modules, {
                "memory_store": types.SimpleNamespace(MemoryStore=FakeStore),
                "pm_common": types.SimpleNamespace(looks_like_secret=lambda _: False),
            }):
                result = get_plugin_context(directory, "fix session", max_chars=1200)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["retrieval_id"], 117)
            self.assertIn("selected knowledge", result["context_markdown"])
            self.assertEqual(seen[1][0], "intelligent")
            self.assertEqual(seen[1][2]["max_chars"], 1200)
            self.assertEqual(seen[1][2]["phase"], "orient")
            self.assertEqual(seen[-1], ("closed",))

    def test_bridge_ide_focus_composes_phase_without_new_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".cognigraph/memory.db"
            db.parent.mkdir()
            db.touch()
            src = root / "src/auth.py"
            src.parent.mkdir()
            src.write_text("def login(): return True\n")
            calls = []

            class FakeStore:
                def __init__(self, path):
                    calls.append(("opened", path))

                def get_intelligent_context(self, task, **kwargs):
                    calls.append(("composed", task, kwargs))
                    return {
                        "status": "ok", "context_markdown": "Evidence to inspect before verification",
                        "retrieval_id": 999,
                    }

                def get_file_context(self, path):
                    calls.append(("file", path))
                    return "Structural dependencies for " + path

                def get_context(self, *args, **kwargs):
                    raise AssertionError("legacy context should not be called")

                def close(self):
                    calls.append(("closed",))

            with patch.dict(sys.modules, {
                "memory_store": types.SimpleNamespace(MemoryStore=FakeStore),
                "pm_common": types.SimpleNamespace(looks_like_secret=lambda _: False),
            }):
                result = get_plugin_context(
                    directory, "fix login", max_chars=1500,
                    file_paths=["../unsafe.py", "src/auth.py"], phase="verify",
                )
            self.assertEqual(result["status"], "ok")
            self.assertIsNone(result["retrieval_id"])
            self.assertIn("Evidence to inspect", result["context_markdown"])
            self.assertIn("Structural dependencies", result["context_markdown"])
            self.assertLessEqual(len(result["context_markdown"]), 1500)
            _, task, options = next(x for x in calls if x[0] == "composed")
            self.assertEqual(task, "fix login")
            self.assertEqual(options["phase"], "verify")
            self.assertEqual(options["focus_files"], ["src/auth.py"])
            self.assertEqual(options["limit"], 3)
            self.assertEqual(options["max_chars"], 900)
            self.assertFalse(options["log_usage"])
            self.assertEqual([x for x in calls if x[0] == "file"],
                             [("file", "src/auth.py")])
            self.assertEqual(calls[-1], ("closed",))

    def test_refresh_rechecks_task_without_new_attribution_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / ".cognigraph/memory.db"
            db.parent.mkdir()
            db.touch()
            seen = []

            class FakeStore:
                def __init__(self, path):
                    pass

                def get_intelligent_context(self, task, **kwargs):
                    seen.append((task, kwargs))
                    return {
                        "status": "ok", "context_markdown": "Source rechecked",
                        "retrieval_id": 100,
                    }

                def get_context(self, *args, **kwargs):
                    raise AssertionError("refresh must not use legacy logging")

                def close(self):
                    pass

            with patch.dict(sys.modules, {
                "memory_store": types.SimpleNamespace(MemoryStore=FakeStore),
                "pm_common": types.SimpleNamespace(looks_like_secret=lambda _: False),
            }):
                result = get_plugin_context(directory, "fix login", refresh=True)
            self.assertEqual(result["status"], "ok")
            self.assertIsNone(result["retrieval_id"])
            self.assertFalse(seen[0][1]["log_usage"])
            self.assertEqual(seen[0][1]["phase"], "orient")

    def test_ide_focus_rejects_missing_and_outside_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            db = root / ".cognigraph/memory.db"
            db.parent.mkdir()
            db.touch()
            external = Path(directory) / "outside.py"
            external.write_text("TOP_SECRET = 1\n")
            observed = []

            class FakeStore:
                def __init__(self, path):
                    observed.append("opened")

                def get_intelligent_context(self, *args, **kwargs):
                    raise AssertionError("no untrusted file should reach composer")

                def close(self):
                    observed.append("closed")

            with patch.dict(sys.modules, {
                "memory_store": types.SimpleNamespace(MemoryStore=FakeStore),
                "pm_common": types.SimpleNamespace(looks_like_secret=lambda _: False),
            }):
                result = get_plugin_context(
                    str(root), "fix login", file_paths=["../outside.py"],
                    phase="inspect",
                )
            self.assertEqual(result["status"], "no_matches")
            self.assertEqual(observed, ["opened", "closed"])
            self.assertNotIn("outside", result["context_markdown"])

    def test_real_source_edit_invalidates_cached_freshness_without_new_receipt(self):
        import hashlib
        from memory_store import MemoryStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".cognigraph" / "memory.db"
            db.parent.mkdir()
            src = root / "src/auth.py"
            src.parent.mkdir()
            original = b"def login():\n    return True\n"
            src.write_bytes(original)
            store = MemoryStore(str(db))
            try:
                store.write(
                    "component", "Session login authentication is restored here",
                    file_path="src/auth.py", line_start=1, line_end=2,
                    code_hash=hashlib.sha256(original).hexdigest(),
                    confidence="confirmed", force=True, embed=False,
                )
                store.conn.commit()
            finally:
                store.close()

            class NoVectorStore(MemoryStore):
                def _query_encode(self, text):
                    return None

            with patch.dict(sys.modules, {
                "memory_store": types.SimpleNamespace(MemoryStore=NoVectorStore),
            }):
                before = get_plugin_context(
                    directory, "session login authentication", max_chars=1800,
                )
                self.assertEqual(before["status"], "ok")
                self.assertIsNotNone(before["retrieval_id"])
                self.assertIn("source_freshness=current", before["context_markdown"])

                src.write_bytes(b"def login():\n    return False\n")
                after = get_plugin_context(
                    directory, "session login authentication", max_chars=1800,
                    refresh=True,
                )

            self.assertEqual(after["status"], "ok")
            self.assertIsNone(after["retrieval_id"])
            self.assertIn("source_freshness=possibly_stale", after["context_markdown"])
            self.assertIn("source changed", after["context_markdown"])
            self.assertNotIn("source_freshness=current", after["context_markdown"])

            store = MemoryStore(str(db))
            try:
                self.assertEqual(
                    store.conn.execute(
                        "SELECT COUNT(*) FROM memory_usage_log"
                    ).fetchone()[0], 1,
                )
            finally:
                store.close()

    def test_unsupported_phase_and_refresh_type_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                get_plugin_context(directory, "fix login", phase="invent")
            with self.assertRaises(ValueError):
                get_plugin_context(directory, "fix login", refresh="true")

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


    def test_max_chars_validation_rejects_malformed_and_accepts_numeric_string(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".cognigraph" / "memory.db"
            db.parent.mkdir()
            db.touch()
            seen = []

            class FakeStore:
                def __init__(self, path):
                    seen.append(("init", path))

                def get_intelligent_context(self, task, **kwargs):
                    seen.append(("intelligent", task, kwargs))
                    return {
                        "status": "ok",
                        "context_markdown": "bounded " + ("x" * 100),
                        "retrieval_id": 7,
                    }

                def close(self):
                    seen.append(("closed",))

            with patch.dict(sys.modules, {
                "memory_store": types.SimpleNamespace(MemoryStore=FakeStore),
                "pm_common": types.SimpleNamespace(looks_like_secret=lambda _: False),
            }):
                with self.assertRaises(ValueError):
                    get_plugin_context(str(root), "fix session", max_chars="not-a-number")
                with self.assertRaises(ValueError):
                    get_plugin_context(str(root), "fix session", max_chars=1.5)
                result = get_plugin_context(str(root), "fix session", max_chars="1200")
            self.assertEqual(result["status"], "ok")
            self.assertLessEqual(len(result["context_markdown"]), 1200)
            self.assertEqual(seen[1][2]["max_chars"], 1200)
            self.assertEqual(seen[-1], ("closed",))

if __name__ == "__main__":
    unittest.main()
