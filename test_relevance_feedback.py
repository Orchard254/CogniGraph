"""Tests for evidence-aware relevance learning (uses a small SQLite fixture)."""
import datetime
import sqlite3
import unittest

from pm_feedback import FeedbackMixin


class _Store(FeedbackMixin):
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
            CREATE TABLE memory (id INTEGER PRIMARY KEY, deprecated_at TEXT);
            CREATE TABLE memory_usage_log (
                id INTEGER PRIMARY KEY, query_text TEXT, retrieved_fact_ids TEXT
            );
        """)
        self.conn.execute("INSERT INTO memory(id) VALUES (1), (2), (3)")
        self.conn.execute("""INSERT INTO memory_usage_log
            (id, query_text, retrieved_fact_ids) VALUES
            (10, 'fix oauth session login refresh', '1,2'),
            (11, 'fix oauth session refresh bug', '1,2'),
            (12, 'optimize database index plans', '1,2')""")
        self._ensure_relevance_feedback_schema()

    def _current_git_commit(self):
        return "commit123"


class RelevanceFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()

    def tearDown(self):
        self.store.conn.close()

    def test_only_evidenced_outcomes_change_rank(self):
        s = self.store
        task = "fix oauth session login refresh"
        self.assertEqual(s._feedback_boost(task), {})
        s.record_relevance_feedback(10, [{"fact_id": 1, "rating": "helpful"}])
        self.assertEqual(s._feedback_boost(task), {}, "Agent guesses cannot train ranking")
        s.record_relevance_feedback(10, [{"fact_id": 1, "rating": "helpful"}], evidence_type="user")
        s.record_relevance_feedback(11, [{"fact_id": 2, "rating": "incorrect"}], evidence_type="user")
        boost = s._feedback_boost(task)
        self.assertGreater(boost[1], 1.0)
        self.assertLess(boost[2], 1.0)
        self.assertEqual(s._feedback_boost("optimize database index plans"), {})

    def test_only_exposed_active_memory_facts_are_ratable(self):
        with self.assertRaises(ValueError):
            self.store.record_relevance_feedback(
                10, [{"fact_id": 3, "rating": "helpful"}], "user")
        self.assertEqual(self.store.relevance_feedback_stats()["total"], 0)
        self.store.conn.execute("UPDATE memory SET deprecated_at='2026-01-01' WHERE id=1")
        with self.assertRaises(ValueError):
            self.store.record_relevance_feedback(
                10, [{"fact_id": 1, "rating": "helpful"}], "user")

    def test_verified_test_metadata_must_match_head(self):
        rating = [{"fact_id": 1, "rating": "helpful"}]
        with self.assertRaises(ValueError):
            self.store.record_relevance_feedback(
                10, rating, evidence_type="test",
                verification_evidence={
                    "command": "pytest", "exit_code": 0, "commit_sha": "wrong"})
        self.assertTrue(self.store.record_relevance_feedback(
            10, rating, evidence_type="test",
            verification_evidence={
                "command": "pytest", "exit_code": 0, "commit_sha": "commit123"}
        )["ranking_effect"])

    def test_upsert_does_not_double_count_or_preserve_old_rating(self):
        s = self.store
        s.record_relevance_feedback(10, [{"fact_id": 1, "rating": "helpful"}], "user")
        s.record_relevance_feedback(10, [{"fact_id": 1, "rating": "irrelevant"}], "user")
        self.assertEqual(s.relevance_feedback_stats()["total"], 1)
        self.assertLess(s._feedback_boost("fix oauth session login refresh")[1], 1.0)

    def test_old_feedback_expires(self):
        s = self.store
        s.record_relevance_feedback(10, [{"fact_id": 1, "rating": "helpful"}], "user")
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=100)).isoformat()
        s.conn.execute("UPDATE memory_relevance_feedback SET recorded_at=?", (old,))
        self.assertEqual(s._feedback_boost("fix oauth session login refresh"), {})

    def test_invalid_feedback_does_not_partially_write(self):
        s = self.store
        with self.assertRaises(ValueError):
            s.record_relevance_feedback(10, [
                {"fact_id": 1, "rating": "helpful"},
                {"fact_id": 3, "rating": "helpful"},
            ], "user")
        self.assertEqual(s.relevance_feedback_stats()["total"], 0)

    def test_feedback_boost_is_bounded(self):
        s = self.store
        for i in range(100, 400):
            s.conn.execute("INSERT INTO memory_usage_log VALUES (?, ?, ?)",
                           (i, "fix oauth session login refresh", "1,2"))
            s.record_relevance_feedback(i, [{"fact_id": 1, "rating": "helpful"}], "user")
            s.record_relevance_feedback(i, [{"fact_id": 2, "rating": "incorrect"}], "user")
        boost = s._feedback_boost("fix oauth session login refresh")
        self.assertLessEqual(boost[1], 1.2)
        self.assertGreaterEqual(boost[2], 0.72)


if __name__ == "__main__":
    unittest.main()
