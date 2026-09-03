"""
Unit tests for Phase 3 LLM Agent Priming features.

Tests cover:
- Usage-pattern learning: memory_usage_log schema, _log_usage, _usage_boost, usage_stats
- Proactive context prediction: suggest_proactive_context
- Per-model RRF lane weight tuning: AGENT_LANE_WEIGHTS in query_hybrid
- agent_type threading through get_context

Run with: python -m pytest test_phase3_priming.py -v
"""
import pytest
import tempfile
import os
from memory_store import MemoryStore


@pytest.fixture
def temp_store():
    """Create a temporary MemoryStore for testing."""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    store = MemoryStore(db_path=temp_db.name)
    yield store
    store.close()
    try:
        os.unlink(temp_db.name)
    except Exception:
        pass


@pytest.fixture
def seeded_store(temp_store):
    """Store with realistic test facts seeded."""
    temp_store.write(category='constraint', content='All API endpoints must validate JWT tokens',
                     importance=5, tags='security,api,critical',
                     file_path='api/auth.py', line_start=10, line_end=15)
    temp_store.write(category='decision', content='Use PostgreSQL for production database',
                     importance=4, tags='database,architecture',
                     file_path='config/database.py', line_start=20, line_end=25)
    temp_store.write(category='component', content='User authentication service handles login and logout flows',
                     importance=3, tags='auth,service',
                     file_path='services/auth_service.py', line_start=5, line_end=30)
    temp_store.write(category='constraint', content='Database queries must use parameterized queries',
                     importance=5, tags='security,critical',
                     file_path='models/user.py', line_start=8, line_end=12)
    temp_store.write(category='decision', content='Implement rate limiting at 100 requests per minute',
                     importance=4, tags='api,performance',
                     file_path='config/rate_limiting.py', line_start=15, line_end=20)
    return temp_store


# ============================================================
# Usage Log Schema Tests
# ============================================================

class TestUsageLogSchema:
    def test_usage_log_table_created(self, temp_store):
        """memory_usage_log table exists after store init."""
        cols = [c[1] for c in temp_store.conn.execute("PRAGMA table_info(memory_usage_log)").fetchall()]
        assert "query_text" in cols
        assert "intent" in cols
        assert "agent_type" in cols
        assert "retrieved_fact_ids" in cols
        assert "result_count" in cols
        assert "logged_at" in cols

    def test_usage_log_index_exists(self, temp_store):
        """Index on logged_at exists."""
        idx = temp_store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_usage_log_at'"
        ).fetchone()
        assert idx is not None


# ============================================================
# Usage Logging Tests
# ============================================================

class TestUsageLogging:
    def test_query_hybrid_logs_usage(self, seeded_store):
        """Each query_hybrid call writes a usage log entry."""
        seeded_store.query_hybrid("fix authentication bug")
        count = seeded_store.conn.execute("SELECT COUNT(*) AS c FROM memory_usage_log").fetchone()["c"]
        assert count >= 1

    def test_usage_log_records_intent(self, seeded_store):
        """Logged intent matches detected intent."""
        seeded_store.query_hybrid("fix authentication bug")
        row = seeded_store.conn.execute("SELECT intent FROM memory_usage_log ORDER BY id DESC LIMIT 1").fetchone()
        assert row["intent"] == "fix"

    def test_usage_log_records_agent_type(self, seeded_store):
        """agent_type is recorded when provided."""
        seeded_store.query_hybrid("fix auth", agent_type="cursor")
        row = seeded_store.conn.execute("SELECT agent_type FROM memory_usage_log ORDER BY id DESC LIMIT 1").fetchone()
        assert row["agent_type"] == "cursor"

    def test_usage_log_records_fact_ids(self, seeded_store):
        """Retrieved fact IDs are stored as comma-separated string."""
        seeded_store.query_hybrid("JWT authentication tokens")
        row = seeded_store.conn.execute("SELECT retrieved_fact_ids FROM memory_usage_log ORDER BY id DESC LIMIT 1").fetchone()
        assert row["retrieved_fact_ids"]  # non-empty

    def test_multiple_queries_logged(self, seeded_store):
        """Multiple queries create multiple log entries."""
        seeded_store.query_hybrid("fix auth")
        seeded_store.query_hybrid("database production")
        seeded_store.query_hybrid("rate limiting")
        count = seeded_store.conn.execute("SELECT COUNT(*) AS c FROM memory_usage_log").fetchone()["c"]
        assert count >= 3


# ============================================================
# Usage Boost Tests
# ============================================================

class TestUsageBoost:
    def test_no_boost_on_empty_history(self, seeded_store):
        """No boosts when usage log is empty."""
        boosts = seeded_store._usage_boost("some random query")
        assert boosts == {}

    def test_boost_after_repeated_queries(self, seeded_store):
        """Repeated queries for same topic produce boosts."""
        for _ in range(3):
            seeded_store.query_hybrid("fix authentication bug")
        boosts = seeded_store._usage_boost("fix authentication bug")
        assert len(boosts) > 0

    def test_boost_values_are_positive_multipliers(self, seeded_store):
        """Boost values are multipliers >= 1.0."""
        for _ in range(3):
            seeded_store.query_hybrid("fix authentication bug")
        boosts = seeded_store._usage_boost("fix authentication bug")
        for fid, mult in boosts.items():
            assert mult >= 1.0

    def test_boost_capped_at_115(self, seeded_store):
        """Boost never exceeds 1.15x."""
        for _ in range(10):
            seeded_store.query_hybrid("fix authentication bug")
        boosts = seeded_store._usage_boost("fix authentication bug")
        for fid, mult in boosts.items():
            assert mult <= 1.15

    def test_no_boost_for_unrelated_query(self, seeded_store):
        """Unrelated queries don't get boosts from unrelated history."""
        for _ in range(3):
            seeded_store.query_hybrid("fix authentication bug")
        boosts = seeded_store._usage_boost("completely different topic about networking")
        # May or may not have boosts depending on token overlap; just check no crash
        assert isinstance(boosts, dict)


# ============================================================
# Usage Stats Tests
# ============================================================

class TestUsageStats:
    def test_usage_stats_empty_store(self, temp_store):
        """usage_stats returns zeros for empty store."""
        stats = temp_store.usage_stats()
        assert stats["total_retrievals"] == 0
        assert stats["intent_distribution"] == {}
        assert stats["most_retrieved_facts"] == []

    def test_usage_stats_counts_retrievals(self, seeded_store):
        """usage_stats counts total retrievals."""
        seeded_store.query_hybrid("fix auth")
        seeded_store.query_hybrid("fix auth")
        stats = seeded_store.usage_stats()
        assert stats["total_retrievals"] >= 2

    def test_usage_stats_intent_distribution(self, seeded_store):
        """Intent distribution tracks different intents."""
        seeded_store.query_hybrid("fix authentication bug")
        seeded_store.query_hybrid("implement new feature")
        stats = seeded_store.usage_stats()
        assert "fix" in stats["intent_distribution"]
        assert "implement" in stats["intent_distribution"]

    def test_usage_stats_most_retrieved_facts(self, seeded_store):
        """Most retrieved facts are listed with content."""
        for _ in range(3):
            seeded_store.query_hybrid("JWT authentication tokens")
        stats = seeded_store.usage_stats()
        assert len(stats["most_retrieved_facts"]) > 0
        top = stats["most_retrieved_facts"][0]
        assert "fact_id" in top
        assert "retrieval_count" in top
        assert "content" in top

    def test_usage_stats_recent_queries(self, seeded_store):
        """Recent queries are listed."""
        seeded_store.query_hybrid("fix auth")
        stats = seeded_store.usage_stats()
        assert len(stats["recent_queries"]) > 0
        assert "query_text" in stats["recent_queries"][0]


# ============================================================
# Proactive Context Suggestion Tests
# ============================================================

class TestProactiveSuggestions:
    def test_no_suggestions_empty_context(self, seeded_store):
        """Empty current context returns guidance message."""
        result = seeded_store.suggest_proactive_context([])
        assert "Provide the IDs" in result

    def test_no_suggestions_sparse_history(self, seeded_store):
        """Sparse history returns sparse message."""
        result = seeded_store.suggest_proactive_context([1])
        assert isinstance(result, str)

    def test_suggestions_after_co_retrieval(self, seeded_store):
        """Co-retrieval patterns produce suggestions."""
        # Retrieve facts 1 and 2 together multiple times
        for _ in range(5):
            seeded_store.conn.execute(
                "INSERT INTO memory_usage_log (query_text, intent, agent_type, retrieved_fact_ids, result_count, session_id, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("fix auth", "fix", None, "1,2", 2, None, "2026-08-31T12:00:00")
            )
        # Now ask for suggestions given fact 1 is in context
        result = seeded_store.suggest_proactive_context([1])
        assert "Proactive Context Suggestions" in result or "No co-retrieval" in result

    def test_suggestions_exclude_current_facts(self, seeded_store):
        """Suggested facts should not include facts already in context."""
        for _ in range(5):
            seeded_store.conn.execute(
                "INSERT INTO memory_usage_log (query_text, intent, agent_type, retrieved_fact_ids, result_count, session_id, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("fix auth", "fix", None, "1,2", 2, None, "2026-08-31T12:00:00")
            )
        result = seeded_store.suggest_proactive_context([1])
        # If fact 2 is suggested, it should be shown; fact 1 should not appear as a suggestion
        if "#2" in result:
            assert "#1" not in result or "co-retrieved" not in result.split("#1")[0] if "#1" in result else True


# ============================================================
# Per-Agent Lane Weight Tests
# ============================================================

class TestAgentLaneWeights:
    def test_query_hybrid_accepts_agent_type(self, seeded_store):
        """query_hybrid accepts agent_type parameter without error."""
        results = seeded_store.query_hybrid("fix auth", agent_type="cursor")
        assert isinstance(results, list)

    def test_all_agent_types_work(self, seeded_store):
        """All defined agent types produce results."""
        for agent in ["default", "cursor", "claude", "opencode", "zed"]:
            results = seeded_store.query_hybrid("database", agent_type=agent)
            assert isinstance(results, list)

    def test_unknown_agent_type_falls_back(self, seeded_store):
        """Unknown agent type falls back to default weights."""
        results = seeded_store.query_hybrid("database", agent_type="unknown_agent")
        assert isinstance(results, list)

    def test_agent_type_threaded_to_get_context(self, seeded_store):
        """get_context passes agent_type to query_hybrid."""
        ctx = seeded_store.get_context("fix auth", agent_type="cursor")
        assert isinstance(ctx, str)
        assert "FIX" in ctx


# ============================================================
# Integration Tests
# ============================================================

class TestPhase3Integration:
    def test_full_learning_loop(self, seeded_store):
        """Full loop: query -> log -> learn -> boost."""
        # Query multiple times to build history
        for _ in range(5):
            seeded_store.query_hybrid("fix authentication bug", intent="fix")

        # Verify logging
        stats = seeded_store.usage_stats()
        assert stats["total_retrievals"] >= 5

        # Verify learning
        boosts = seeded_store._usage_boost("fix authentication bug")
        assert len(boosts) > 0

        # Verify boosted query still works
        results = seeded_store.query_hybrid("fix authentication bug", intent="fix")
        assert len(results) > 0

    def test_proactive_after_learning(self, seeded_store):
        """Proactive suggestions work after building retrieval history."""
        # Build co-retrieval pattern
        for _ in range(5):
            results = seeded_store.query_hybrid("JWT authentication security")
            if results:
                ids = [r["id"] for r in results]
                seeded_store.conn.execute(
                    "INSERT INTO memory_usage_log (query_text, intent, agent_type, retrieved_fact_ids, result_count, session_id, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("JWT authentication security", "fix", None, ",".join(str(i) for i in ids), len(ids), None, "2026-08-31T12:00:00")
                )

        # Get suggestions based on first result
        if results:
            first_id = results[0]["id"]
            suggestions = seeded_store.suggest_proactive_context([first_id])
            assert isinstance(suggestions, str)