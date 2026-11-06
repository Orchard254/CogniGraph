"""Zero-hit proactive context: structural leads, unknowns, safety, and IDE delivery."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_store import MemoryStore
from opencode_context import get_plugin_context, _is_deliverable_briefing


class UnknownFirstDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / ".cognigraph").mkdir()
        (self.root / "src").mkdir()
        self.db = self.root / ".cognigraph" / "memory.db"
        self.source = self.root / "src" / "session_handler.py"
        self.source.write_text("def restore_session():\n    return True\n")
        self.store = MemoryStore(str(self.db))
        self.original_hybrid = self.store.query_hybrid
        self.store.query_hybrid = lambda *args, **kwargs: []

    def tearDown(self):
        self.store.query_hybrid = self.original_hybrid
        self.store.close()
        self.temp.cleanup()

    def index(self, name, file_path, node_type="function"):
        now = "2026-11-06T00:00:00+00:00"
        self.store.conn.execute(
            """INSERT INTO structural_nodes
               (node_type, name, class_name, file_path, line_start,
                line_end, created_at, last_seen_at)
               VALUES (?, ?, '', ?, 1, 2, ?, ?)""",
            (node_type, name, file_path, now, now),
        )

    def test_zero_hit_task_recommends_existing_indexed_symbol_without_facts(self):
        self.index("restore_session", "src/session_handler.py")
        report = self.store.get_intelligent_context(
            "repair restore_session after a login refresh",
            max_chars=1800, limit=2, log_usage=True,
        )
        self.assertEqual(report["status"], "no_new_context")
        self.assertEqual(report["selected"], [])
        self.assertIsNone(report["retrieval_id"])
        self.assertEqual(report["budget_used_chars"], len(report["context_markdown"]))
        self.assertEqual(
            [t["file_path"] for t in report["inspection_targets"]],
            ["src/session_handler.py"],
        )
        self.assertEqual(report["inspection_targets"][0]["kind"], "structural_lead")
        self.assertIn("structural index (unverified)", report["context_markdown"])
        self.assertIn("No new relevant fact was delivered", report["context_markdown"])
        self.assertIn("`src/session_handler.py`", report["context_markdown"])
        self.assertEqual(
            self.store.conn.execute("SELECT count(*) FROM memory_usage_log").fetchone()[0],
            0,
        )

    def test_zero_hit_without_structural_matches_still_explains_missing_evidence(self):
        report = self.store.get_intelligent_context(
            "fix authentication", max_chars=512, limit=3, log_usage=True,
        )
        self.assertEqual(report["status"], "no_new_context")
        self.assertEqual(report["inspection_targets"], [])
        self.assertIn("no_delivered_fact", {g["kind"] for g in report["knowledge_gaps"]})
        self.assertIn("current source and tests", report["context_markdown"])
        self.assertLessEqual(len(report["context_markdown"]), 512)
        self.assertIsNone(report["retrieval_id"])

    def test_untrusted_missing_or_out_of_project_index_entries_are_not_leads(self):
        outside = Path(self.temp.name) / "outside.py"
        outside.write_text("token = 'secret'\n")
        self.index("restore_session_outside", "../outside.py")
        self.index("restore_session_missing", "src/missing.py")
        self.index("restore_session_import", "src/session_handler.py", node_type="import")
        self.index("restore_session", "src/session_handler.py")
        report = self.store.get_intelligent_context(
            "restore_session", max_chars=1500, limit=2, log_usage=False,
        )
        paths = [x["file_path"] for x in report["inspection_targets"]]
        self.assertEqual(paths, ["src/session_handler.py"])
        self.assertNotIn("outside.py", report["context_markdown"])
        self.assertNotIn("src/missing.py", report["context_markdown"])

    def test_stale_structural_index_cannot_imply_source_still_exists(self):
        self.index("restore_session", "src/session_handler.py")
        self.source.unlink()
        report = self.store.get_intelligent_context(
            "restore_session", max_chars=1500, log_usage=False,
        )
        self.assertEqual(report["inspection_targets"], [])
        self.assertIn("no_delivered_fact", {g["kind"] for g in report["knowledge_gaps"]})
        self.assertNotIn("`src/session_handler.py`", report["context_markdown"])

    def test_focus_gap_is_preserved_when_no_facts_are_selected(self):
        report = self.store.get_intelligent_context(
            "restore session", phase="verify",
            focus_files=["src/session_handler.py"], max_chars=1200,
            log_usage=False,
        )
        self.assertEqual(report["selected"], [])
        kinds = {g["kind"] for g in report["knowledge_gaps"]}
        self.assertIn("missing_focused_evidence", kinds)
        self.assertIn("no_delivered_fact", kinds)
        self.assertIn("`src/session_handler.py`", report["context_markdown"])

    def test_sensitive_prompt_is_never_used_to_generate_unknown_guidance(self):
        self.index("restore_session", "src/session_handler.py")
        report = self.store.get_intelligent_context(
            "api_key=abcdefghijklmnopqrstuvwxyz123456",
        )
        self.assertEqual(report["status"], "sensitive_query")
        self.assertEqual(report["context_markdown"], "")
        self.assertIsNone(report["retrieval_id"])

    def test_bridge_delivers_no_fact_guidance_without_usage_or_fake_receipt(self):
        self.index("restore_session", "src/session_handler.py")
        self.store.conn.commit()
        # This is the real bridge, not a fake composer. Embeddings are disabled
        # so fixture execution stays offline and deterministic.
        with patch.object(MemoryStore, "_query_encode", lambda *_args: None):
            result = get_plugin_context(
                str(self.root), "repair restore_session", max_chars=1500,
            )
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["retrieval_id"])
        self.assertIn("structural index (unverified)", result["context_markdown"])
        self.assertIn("`src/session_handler.py`", result["context_markdown"])
        self.assertEqual(
            self.store.conn.execute("SELECT count(*) FROM memory_usage_log").fetchone()[0],
            0,
        )

    def test_bridge_rejects_safety_statuses_and_unqualified_empty_results(self):
        self.assertTrue(_is_deliverable_briefing({
            "status": "no_new_context", "knowledge_gaps": [{"kind": "no_delivered_fact"}],
        }))
        for result in (
            {"status": "sensitive_query", "knowledge_gaps": [{"kind": "not_allowed"}]},
            {"status": "no_new_context", "knowledge_gaps": [], "inspection_targets": []},
            {"status": "empty_query", "inspection_targets": [{"file_path": "a.py"}]},
        ):
            self.assertFalse(_is_deliverable_briefing(result))


if __name__ == "__main__":
    unittest.main()
