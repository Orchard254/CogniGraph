"""
Unit tests for LLM Agent Priming features (Phase 1 & 2).

Tests cover:
- Intent detection (_detect_intent)
- Fact prioritization (_prioritize_facts_for_llm)
- Intent-based context expansion (_expand_context_for_intent)
- Intent-aware RRF ranking in query_hybrid()
- Structural briefing lines (_structural_briefing_lines)
- Cross-file dependency map (get_dependency_map)
- Enhanced get_context() with intent parameter

Run with: python -m pytest test_llm_priming.py -v
"""
import pytest
import tempfile
import os
from pathlib import Path
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
# Intent Detection Tests
# ============================================================

class TestIntentDetection:
    def test_detect_fix_intent(self, temp_store):
        assert temp_store._detect_intent('fix the login bug') == 'fix'
        assert temp_store._detect_intent('resolve the authentication error') == 'fix'
        assert temp_store._detect_intent('patch the security issue') == 'fix'

    def test_detect_implement_intent(self, temp_store):
        assert temp_store._detect_intent('implement new user registration') == 'implement'
        assert temp_store._detect_intent('create the payment API') == 'implement'
        assert temp_store._detect_intent('build the dashboard component') == 'implement'

    def test_detect_refactor_intent(self, temp_store):
        assert temp_store._detect_intent('refactor the database models') == 'refactor'
        assert temp_store._detect_intent('clean up the authentication code') == 'refactor'
        assert temp_store._detect_intent('simplify the middleware chain') == 'refactor'

    def test_detect_optimize_intent(self, temp_store):
        assert temp_store._detect_intent('optimize memory usage') == 'optimize'
        assert temp_store._detect_intent('improve performance of queries') == 'optimize'
        assert temp_store._detect_intent('cache API responses') == 'optimize'

    def test_detect_test_intent(self, temp_store):
        assert temp_store._detect_intent('write unit tests for auth') == 'test'
        assert temp_store._detect_intent('add integration test coverage') == 'test'

    def test_detect_default_intent(self, temp_store):
        # When no keywords match, defaults to 'implement'
        assert temp_store._detect_intent('look at the code') == 'implement'

    def test_intent_detection_case_insensitive(self, temp_store):
        assert temp_store._detect_intent('FIX THE BUG') == 'fix'
        assert temp_store._detect_intent('Implement New Feature') == 'implement'


# ============================================================
# Fact Prioritization Tests
# ============================================================

class TestFactPrioritization:
    def test_fix_intent_prioritizes_constraints(self, seeded_store):
        facts = seeded_store.get_state(limit=10)
        prioritized = seeded_store._prioritize_facts_for_llm(facts, 'fix')
        constraint_positions = [i for i, f in enumerate(prioritized) if f['category'] == 'constraint']
        decision_positions = [i for i, f in enumerate(prioritized) if f['category'] == 'decision']
        assert constraint_positions and decision_positions
        assert constraint_positions[0] < decision_positions[0], \
            "Constraints should come before decisions for fix intent"

    def test_implement_intent_prioritizes_decisions(self, seeded_store):
        facts = seeded_store.get_state(limit=10)
        prioritized = seeded_store._prioritize_facts_for_llm(facts, 'implement')
        # Critical facts (importance >= 5) always come first by design, so
        # compare category ordering among non-critical facts only.
        # The seeded constraints are both importance 5, so they sit in the
        # critical bucket ahead of everything; compare decisions against the
        # non-critical component facts instead.
        non_critical = [f for f in prioritized if f.get('importance', 3) < 5]
        decision_positions = [i for i, f in enumerate(non_critical) if f['category'] == 'decision']
        component_positions = [i for i, f in enumerate(non_critical) if f['category'] == 'component']
        assert decision_positions and component_positions
        assert decision_positions[0] < component_positions[0], \
            "Decisions should come before components for implement intent"

    def test_critical_facts_always_first(self, seeded_store):
        facts = seeded_store.get_state(limit=10)
        prioritized = seeded_store._prioritize_facts_for_llm(facts, 'implement')
        critical_facts = [f for f in prioritized if f.get('importance', 3) >= 5]
        if critical_facts:
            first_critical_pos = prioritized.index(critical_facts[0])
            assert first_critical_pos < 3, "Critical facts should be near the top"

    def test_prioritization_preserves_all_facts(self, seeded_store):
        facts = seeded_store.get_state(limit=10)
        prioritized = seeded_store._prioritize_facts_for_llm(facts, 'fix')
        assert len(prioritized) >= len(facts), "Prioritization should not lose facts"


# ============================================================
# Intent-Based Context Expansion Tests
# ============================================================

class TestContextExpansion:
    def test_expansion_returns_list(self, seeded_store):
        facts = seeded_store.get_state(limit=5)
        expanded = seeded_store._expand_context_for_intent(facts, 'fix')
        assert isinstance(expanded, list)

    def test_expansion_includes_original_facts(self, seeded_store):
        facts = seeded_store.get_state(limit=5)
        expanded = seeded_store._expand_context_for_intent(facts, 'fix')
        original_ids = {f['id'] for f in facts}
        expanded_ids = {f['id'] for f in expanded}
        assert original_ids.issubset(expanded_ids), "Expansion should include all original facts"

    def test_expansion_no_duplicates(self, seeded_store):
        facts = seeded_store.get_state(limit=5)
        expanded = seeded_store._expand_context_for_intent(facts, 'implement')
        ids = [f['id'] for f in expanded]
        assert len(ids) == len(set(ids)), "Expansion should not create duplicates"


# ============================================================
# Intent-Aware RRF Ranking Tests
# ============================================================

class TestIntentAwareRanking:
    def test_query_hybrid_accepts_intent_param(self, seeded_store):
        results = seeded_store.query_hybrid('fix authentication bug', intent='fix', limit=5)
        assert isinstance(results, list)

    def test_fix_intent_boosts_constraints(self, seeded_store):
        results_fix = seeded_store.query_hybrid('fix authentication security', intent='fix', limit=5)
        results_implement = seeded_store.query_hybrid('implement authentication security', intent='implement', limit=5)

        fix_categories = [r['category'] for r in results_fix]
        implement_categories = [r['category'] for r in results_implement]

        if 'constraint' in fix_categories and 'constraint' in implement_categories:
            fix_constraint_pos = fix_categories.index('constraint')
            impl_constraint_pos = implement_categories.index('constraint')
            assert fix_constraint_pos <= impl_constraint_pos, \
                "Fix intent should rank constraints at least as high as implement intent"

    def test_query_hybrid_auto_detects_intent(self, seeded_store):
        # Should not raise even without explicit intent
        results = seeded_store.query_hybrid('fix the login bug', limit=5)
        assert isinstance(results, list)


# ============================================================
# Structural Briefing Tests
# ============================================================

class TestStructuralBriefing:
    def test_structural_briefing_returns_list(self, temp_store):
        lines = temp_store._structural_briefing_lines('nonexistent.py')
        assert isinstance(lines, list)

    def test_structural_briefing_empty_for_no_nodes(self, temp_store):
        lines = temp_store._structural_briefing_lines('no_structure.py')
        assert lines == []

    def test_file_context_includes_structure_when_available(self, temp_store):
        test_file = Path('test_struct_temp.py')
        test_file.write_text(
            'def hello():\n'
            '    return "world"\n'
            '\n'
            'class Greeter:\n'
            '    def greet(self, name):\n'
            '        return f"Hello, {name}"\n'
        )
        try:
            from structural_extract import extract_structures
            nodes = extract_structures(test_file)
            temp_store.sync_structural_nodes('test_struct_temp.py', nodes, project_root='.')

            briefing = temp_store.get_file_context('test_struct_temp.py')
            assert 'Code Structure' in briefing, "File context should include structural section"
            assert 'hello()' in briefing, "Should list function names"
            assert 'Greeter' in briefing, "Should list class names"
        finally:
            try:
                test_file.unlink()
            except Exception:
                pass


# ============================================================
# Dependency Map Tests
# ============================================================

class TestDependencyMap:
    def test_dependency_map_returns_string(self, temp_store):
        result = temp_store.get_dependency_map(['file1.py', 'file2.py'])
        assert isinstance(result, str)
        assert 'Cross-File Dependency Map' in result

    def test_dependency_map_shows_internal_edges(self, temp_store):
        id1 = temp_store.write(category='component', content='Auth service',
                               file_path='services/auth.py', importance=3)
        id2 = temp_store.write(category='decision', content='Use JWT',
                               file_path='config/auth_config.py', importance=4)
        temp_store.link_facts(id1, id2, 'depends_on')

        dep_map = temp_store.get_dependency_map(['services/auth.py', 'config/auth_config.py'])
        assert 'depends_on' in dep_map, "Should show dependency edges"
        assert 'services/auth.py' in dep_map
        assert 'config/auth_config.py' in dep_map

    def test_dependency_map_excludes_same_file_edges(self, temp_store):
        id1 = temp_store.write(category='component', content='Rate limiter',
                               file_path='middleware/rate_limit.py', importance=3)
        id2 = temp_store.write(category='constraint', content='Rate limit 100/min',
                               file_path='middleware/rate_limit.py', importance=4)
        temp_store.link_facts(id1, id2, 'implements')

        dep_map = temp_store.get_dependency_map(['middleware/rate_limit.py'])
        assert 'depended on by: `middleware/rate_limit.py`' not in dep_map

    def test_dependency_map_handles_no_edges(self, temp_store):
        dep_map = temp_store.get_dependency_map(['isolated_file.py'])
        assert isinstance(dep_map, str)
        assert 'isolated_file.py' in dep_map


# ============================================================
# Enhanced get_context() Tests
# ============================================================

class TestEnhancedGetContext:
    def test_get_context_with_intent_param(self, seeded_store):
        ctx = seeded_store.get_context('fix authentication bug', intent='fix', limit=3)
        assert isinstance(ctx, str)
        assert 'Project Memory Context' in ctx

    def test_get_context_auto_detects_intent(self, seeded_store):
        ctx = seeded_store.get_context('fix authentication bug', limit=3)
        assert 'FIX' in ctx, "Should show detected intent in output"

    def test_get_context_shows_intent_header(self, seeded_store):
        ctx = seeded_store.get_context('implement new feature', limit=3)
        assert 'Intent detected' in ctx

    def test_get_context_no_stray_backticks(self, seeded_store):
        ctx = seeded_store.get_context('fix bug', limit=3)
        lines = ctx.split('\n')
        intent_lines = [l for l in lines if 'Intent detected' in l]
        for line in intent_lines:
            assert not line.rstrip().endswith('`'), f"Stray backtick found: {line}"

    def test_get_context_respects_max_chars(self, seeded_store):
        ctx = seeded_store.get_context('fix authentication bug', limit=10, max_chars=500)
        assert len(ctx) <= 650, "Context should respect max_chars budget (with margin for truncation notice)"

    def test_get_context_includes_emojis(self, seeded_store):
        ctx = seeded_store.get_context('fix authentication', limit=5)
        has_emoji = any(e in ctx for e in ['\U0001F4E6', '\U0001F4A1', '\u26A0\uFE0F', '\U0001F4CA', '\U0001F4DD'])
        assert has_emoji, "Context should include category emojis"

    def test_get_context_tip_for_fix_tasks(self, seeded_store):
        ctx = seeded_store.get_context('fix authentication bug', intent='fix', limit=3)
        assert 'Tip:' in ctx, "Fix tasks should include a usage tip"


# ============================================================
# Integration Tests
# ============================================================

class TestIntegration:
    def test_full_priming_workflow(self, seeded_store):
        """Test the complete LLM priming workflow end-to-end."""
        query = 'fix the authentication security vulnerability'

        intent = seeded_store._detect_intent(query)
        assert intent == 'fix'

        ctx = seeded_store.get_context(query, intent=intent, limit=5)
        assert len(ctx) > 0
        assert 'FIX' in ctx

        results = seeded_store.query_hybrid(query, intent='fix', limit=5)
        assert len(results) > 0

    def test_implement_workflow(self, seeded_store):
        """Test priming workflow for implementation tasks."""
        query = 'implement user registration API endpoint'

        intent = seeded_store._detect_intent(query)
        ctx = seeded_store.get_context(query, intent=intent, limit=5)

        assert 'IMPLEMENT' in ctx
        results = seeded_store.query_hybrid(query, intent='implement', limit=5)
        categories = [r['category'] for r in results]
        assert 'decision' in categories, "Implement tasks should surface decisions"


# ============================================================
# File-Anchor Boosting Tests
# ============================================================

class TestFileAnchorBoosting:
    def test_mentioned_files_extracts_paths(self, temp_store):
        mentions = temp_store._mentioned_files('fix the bug in services/auth.py and config/auth.json')
        assert 'services/auth.py' in mentions
        assert 'config/auth.json' in mentions

    def test_mentioned_files_none_for_plain_query(self, temp_store):
        assert temp_store._mentioned_files('improve performance') == []

    def test_file_anchored_fact_outranks_unrelated(self, temp_store):
        temp_store.write(category='component',
                         content='watcher re-embeds facts after file change',
                         file_path='watcher.py', importance=3)
        temp_store.write(category='component',
                         content='database connection pooling configuration',
                         file_path='db/pool.py', importance=3)

        results = temp_store.query_hybrid('fix watcher.py re-embedding', limit=2)
        assert results, "query should return results"
        assert results[0].get('file_path') == 'watcher.py', \
            "fact anchored to the mentioned file should rank first"

    def test_no_crash_when_no_candidates(self, temp_store):
        results = temp_store.query_hybrid('fix watcher.py bug', limit=3)
        assert isinstance(results, list)


# ============================================================
# Doc-Noise Control Tests
# ============================================================

class TestDocNoiseControl:
    def test_long_fact_content_capped(self, temp_store):
        temp_store.write(category='constraint', content='short rule', importance=5)
        temp_store.write(category='other', content='word ' * 200, importance=3)

        ctx = temp_store.get_context('short rule', limit=5)
        for line in ctx.split('\n'):
            if line.startswith(('1.', '2.', '3.')):
                assert len(line) < 900, f"fact line exceeds cap: {len(line)} chars"

    def test_doc_dump_demoted_for_coding_intent(self, temp_store):
        doc_dump = 'AGENTS.md section dump ' + 'protocol text ' * 60
        temp_store.write(category='other', content=doc_dump,
                         tags='bootstrap, docs, agents', importance=3,
                         file_path='AGENTS.md')
        temp_store.write(category='component', content='watcher syncs files',
                         file_path='watcher.py', importance=3)

        demoted = temp_store._suppress_doc_noise(
            [f for f in temp_store.get_state(limit=10)], 'fix')
        categories = [f['category'] for f in demoted]
        assert categories[-1] == 'other', "doc dump should be ranked last for coding intents"

    def test_doc_dump_kept_for_non_coding_intent(self, temp_store):
        doc_dump = 'readme section ' + 'text ' * 60
        fid = temp_store.write(category='other', content=doc_dump,
                               tags='bootstrap, docs', importance=3)
        facts = temp_store.get_state(limit=10)
        unchanged = temp_store._suppress_doc_noise(facts, 'review')
        assert [f['id'] for f in unchanged] == [f['id'] for f in facts], \
            "non-coding intents should not reorder facts"


# ============================================================
# Auto-Tagging Tests
# ============================================================

class TestAutoTagging:
    def test_explicit_tags_preserved(self, temp_store):
        fid = temp_store.write(category='component', content='auth service',
                               tags='auth,service')
        row = temp_store.conn.execute("SELECT tags FROM memory WHERE id = ?", (fid,)).fetchone()
        assert row['tags'] == 'auth,service'

    def test_auto_tags_from_category_and_file_path(self, temp_store):
        fid = temp_store.write(category='component',
                               content='background sync loop re-embeds changed facts',
                               file_path='src/services/watcher.py')
        row = temp_store.conn.execute("SELECT tags FROM memory WHERE id = ?", (fid,)).fetchone()
        assert row['tags'], "facts written without tags should be auto-tagged"
        tags = row['tags'].split(',')
        assert 'component' in tags, "category should become a tag"
        assert 'watcher' in tags, "filename stem should become a tag"
        assert 'services' in tags, "parent directory should become a tag"

    def test_auto_tags_indexed_in_fts(self, temp_store):
        fid = temp_store.write(category='component',
                               content='background sync loop re-embeds changed facts',
                               file_path='src/services/watcher.py')
        hits = temp_store.conn.execute(
            "SELECT rowid FROM memory_fts WHERE memory_fts MATCH 'services'"
        ).fetchall()
        assert any(h['rowid'] == fid for h in hits), \
            "auto-derived directory tag should be FTS-searchable"


# ============================================================
# Cold-Start Usage Boost Tests
# ============================================================

class TestColdStartBoost:
    def test_cold_start_boosts_frequently_accessed_facts(self, temp_store):
        fid = temp_store.write(category='component', content='cache invalidation layer')
        temp_store.conn.execute("UPDATE memory SET access_count = 5 WHERE id = ?", (fid,))
        boosts = temp_store._usage_boost('cache invalidation layer')
        assert fid in boosts, "accessed facts should get a cold-start boost"
        assert 1.0 < boosts[fid] <= 1.10, "cold-start boost should be capped at 1.10x"

    def test_cold_start_skipped_when_usage_log_mature(self, temp_store):
        fid = temp_store.write(category='component', content='cache invalidation layer')
        temp_store.conn.execute("UPDATE memory SET access_count = 5 WHERE id = ?", (fid,))
        for i in range(12):
            temp_store._log_usage(f'unrelated query number {i}', 'fix', 'default', [], 0)
        boosts = temp_store._usage_boost('cache invalidation layer')
        assert fid not in boosts, "mature usage log should disable access_count seeding"

    def test_log_and_seed_boosts_merge(self, temp_store):
        fid = temp_store.write(category='component', content='cache invalidation layer')
        temp_store.conn.execute("UPDATE memory SET access_count = 5 WHERE id = ?", (fid,))
        temp_store._log_usage('cache invalidation layer', 'fix', 'default', [fid], 1)
        boosts = temp_store._usage_boost('cache invalidation layer')
        assert boosts[fid] >= 1.05, "fact should carry at least the log-derived boost"


# ============================================================
# Proactive Suggestions in prepare_task Tests
# ============================================================

class TestPrepareTaskSuggestions:
    def test_prepare_task_includes_suggestions_when_patterns_exist(self, temp_store):
        f1 = temp_store.write(category='component', content='auth service handles user login')
        f3 = temp_store.write(category='constraint',
                              content='nightly database backup must complete before deploy')
        for i in range(3):
            temp_store._log_usage('auth service login', 'fix', 'default', [f1, f3], 2)
        briefing = temp_store.prepare_task('fix auth service login flow', limit=1)
        assert 'Proactive Context Suggestions' in briefing, \
            "co-retrieved facts should be surfaced inside the briefing"
        assert f'#{f3}' in briefing

    def test_prepare_task_omits_section_without_patterns(self, temp_store):
        temp_store.write(category='component', content='auth service handles user login')
        briefing = temp_store.prepare_task('fix auth service login flow', limit=1)
        assert 'Proactive Context Suggestions' not in briefing, \
            "no suggestion section when usage history is empty"


# ============================================================
# Usage-Learning Pollution Tests
# ============================================================

class TestUsageLearningPollution:
    def test_stopword_only_overlap_gives_no_boost(self, temp_store):
        fid = temp_store.write(category='component', content='bootstrap ingests repositories',
                               file_path='bootstrap.py')
        temp_store.query_hybrid('how does the bootstrap onboarding ingest a repository', limit=3)

        boosts = temp_store._usage_boost('fix the watcher so it re-embeds facts after file change')
        assert fid not in boosts, "queries sharing only stopwords must not transfer boosts"

    def test_content_overlap_still_boosts(self, temp_store):
        fid = temp_store.write(category='component', content='watcher re-embeds facts',
                               file_path='watcher.py')
        temp_store.query_hybrid('watcher re embed facts broken', limit=3)

        boosts = temp_store._usage_boost('fix watcher re embed bug')
        assert fid in boosts, "queries sharing 2+ content tokens should boost"

    def test_internal_queries_skip_side_effects(self, temp_store):
        temp_store.write(category='component', content='search ranking internals',
                         file_path='pm_search.py')
        before = temp_store.conn.execute(
            'SELECT COUNT(*) AS c FROM memory_usage_log').fetchone()['c']

        temp_store.query_hybrid('search ranking internals', limit=3, log_usage=False)

        after = temp_store.conn.execute(
            'SELECT COUNT(*) AS c FROM memory_usage_log').fetchone()['c']
        assert after == before, "log_usage=False must not record usage"
        row = temp_store.conn.execute(
            "SELECT access_count FROM memory WHERE content = 'search ranking internals'"
        ).fetchone()
        assert row['access_count'] == 0, "log_usage=False must not bump access_count"


# ============================================================
# Code Graph Tests (calls / imports edges, one-hop queries)
# ============================================================

class TestCodeGraph:
    @pytest.fixture
    def graph_store(self, temp_store, tmp_path):
        """Two-file mini repo: b.py imports helper from a.py and calls it."""
        (tmp_path / 'a.py').write_text(
            "def helper():\n    return 1\n")
        (tmp_path / 'b.py').write_text(
            "from a import helper\n\n\ndef main():\n    helper()\n")
        from structural_extract import extract_structures
        for rel in ('a.py', 'b.py'):
            nodes = extract_structures(tmp_path / rel)
            temp_store.sync_structural_nodes(rel, nodes, project_root=str(tmp_path))
        return temp_store

    def test_extract_captures_calls(self, tmp_path):
        (tmp_path / 'c.py').write_text(
            "def outer():\n    inner()\n\n\ndef inner():\n    pass\n")
        from structural_extract import extract_structures
        nodes = extract_structures(tmp_path / 'c.py')
        outer = next(n for n in nodes if n['name'] == 'outer')
        assert 'inner' in outer.get('calls', []), "call expressions must be captured"

    def test_call_and_import_edges_created(self, graph_store):
        types = [r['edge_type'] for r in graph_store.conn.execute(
            'SELECT edge_type FROM structural_edges').fetchall()]
        assert 'calls' in types, "main -> helper call edge expected"
        assert 'imports' in types, "b.py -> a.py import edge expected"

    def test_who_calls_one_hop(self, graph_store):
        result = graph_store.who_calls('helper')
        assert result['found'] is True
        assert any(c['name'] == 'main' and c['file_path'] == 'b.py'
                   for c in result['callers']), "main() in b.py must be listed as caller"
        assert result['importers'] == ['b.py']

    def test_who_calls_qualified_symbol(self, temp_store, tmp_path):
        (tmp_path / 'svc.py').write_text(
            "class Service:\n    def run(self):\n        self.step()\n\n"
            "    def step(self):\n        pass\n")
        from structural_extract import extract_structures
        temp_store.sync_structural_nodes(
            'svc.py', extract_structures(tmp_path / 'svc.py'), project_root=str(tmp_path))
        result = temp_store.who_calls('Service.step')
        assert result['found'] is True
        assert any(c['name'] == 'run' for c in result['callers']), \
            "self.step() inside run() must resolve to Service.step"

    def test_who_calls_unknown_symbol(self, graph_store):
        assert graph_store.who_calls('nonexistent_symbol_xyz')['found'] is False

    def test_project_shape_renders(self, graph_store):
        shape = graph_store.project_shape()
        assert 'Project Shape' in shape
        assert 'indexed files' in shape
        assert 'call edges' in shape

    def test_file_briefing_shows_call_graph(self, graph_store):
        briefing = graph_store.get_file_context('b.py')
        assert 'Code Graph' in briefing
        assert 'calls' in briefing


# ============================================================
# Trust Layer Tests (unmapped flag, correction flags, negative knowledge)
# ============================================================

class TestTrustLayer:
    def test_unmapped_file_warns(self, temp_store):
        briefing = temp_store.get_file_context('totally_unmapped_file_xyz.py')
        assert 'UNMAPPED' in briefing, "files with no facts or structure must be flagged"

    def test_flag_fact_stamped_in_briefing(self, temp_store):
        fid = temp_store.write(category='component', content='legacy auth handler',
                               file_path='auth/legacy.py')
        temp_store.flag_fact(fid, note='function renamed in refactor')
        briefing = temp_store.get_file_context('auth/legacy.py')
        assert 'FLAGGED MISLEADING' in briefing
        assert 'function renamed in refactor' in briefing

    def test_flag_unknown_fact_raises(self, temp_store):
        with pytest.raises(ValueError):
            temp_store.flag_fact(99999)

    def test_checkpoint_failures_become_constraints(self, temp_store):
        temp_store.checkpoint_task(
            summary='task with a dead end',
            failures=[{'content': 'decompyle3 cannot decompile Python 3.11 bytecode',
                       'rationale': 'unsupported version; use raw marshal inspection instead'}])
        rows = temp_store.conn.execute(
            "SELECT category, tags FROM memory WHERE content LIKE '%decompyle3%'"
        ).fetchall()
        assert rows, "failure must be persisted"
        assert rows[0]['category'] == 'constraint'
        assert 'negative-knowledge' in rows[0]['tags']

    def test_prepare_task_includes_project_shape(self, temp_store):
        temp_store.write(category='component', content='some module does things')
        briefing = temp_store.prepare_task('fix some module', limit=1)
        assert '### Project Shape' in briefing


# ============================================================
# Briefing Bloat Tests (capped baselines, state dedup, budget)
# ============================================================

class TestBriefingBloat:
    def test_baseline_section_shows_single_state(self, temp_store):
        temp_store.checkpoint_task(summary='first checkpoint done')
        temp_store.checkpoint_task(summary='second checkpoint done')
        briefing = temp_store.prepare_task('fix a bug', limit=1)
        assert briefing.count('[STATE #') == 1, \
            "baseline section must carry only the latest checkpoint"

    def test_baseline_state_capped(self, temp_store):
        temp_store.checkpoint_task(
            summary='word ' * 200, test_baseline='word ' * 200)
        briefing = temp_store.prepare_task('fix a bug', limit=1)
        state_lines = [l for l in briefing.split('\n') if l.startswith('- **[STATE #')]
        assert state_lines, "baseline section must exist"
        for line in state_lines:
            assert len(line) < 800, f"baseline state line uncapped: {len(line)} chars"

    def test_state_facts_excluded_from_file_briefing(self, temp_store):
        temp_store.checkpoint_task(
            summary='long checkpoint prose ' * 20,
            components=[{'content': 'watcher syncs files',
                         'file_path': 'watcher.py', 'importance': 3}])
        briefing = temp_store.get_file_context('watcher.py')
        assert '[STATE #' not in briefing, \
            "state facts are session history, not file knowledge"
        assert 'watcher syncs files' in briefing

    def test_file_briefing_fact_lines_capped(self, temp_store):
        temp_store.write(category='component', content='word ' * 200,
                         file_path='big_fact.py')
        briefing = temp_store.get_file_context('big_fact.py')
        fact_lines = [l for l in briefing.split('\n')
                      if l.startswith('- **[COMPONENT #')]
        assert fact_lines, "component fact must be listed"
        for line in fact_lines:
            assert len(line) < 650, f"fact line exceeds content cap: {len(line)}"

    def test_prepare_task_token_budget(self, temp_store):
        temp_store.write(category='component', content='auth service handles login',
                         file_path='auth.py', importance=4)
        temp_store.checkpoint_task(summary='checkpoint ' * 100)
        briefing = temp_store.prepare_task('fix auth login', file_paths=['auth.py'], limit=3)
        # A single-file briefing must stay small enough to be cheap context
        assert len(briefing) < 6000, f"briefing too large: {len(briefing)} chars"


# ============================================================
# JS/TS Call Capture Tests (stage-2 code graph parity)
# ============================================================

class TestJsTsCallCapture:
    def test_js_calls_captured(self, tmp_path):
        pytest.importorskip('tree_sitter_javascript')
        from structural_extract import extract_structures
        f = tmp_path / 'svc.js'
        f.write_text(
            "function helper(x) { return x; }\n"
            "function main() { return helper(1); }\n"
            "class Svc {\n"
            "  start() { this.stop(); helper(2); }\n"
            "  stop() {}\n"
            "}\n"
        )
        items = extract_structures(f)
        by_name = {i['name']: i for i in items}
        assert 'helper' in by_name['main'].get('calls', []), \
            "function body calls must be captured"
        start_calls = by_name['start'].get('calls', [])
        assert 'stop' in start_calls and 'helper' in start_calls, \
            "method body calls must be captured"

    def test_ts_calls_captured(self, tmp_path):
        pytest.importorskip('tree_sitter_typescript')
        from structural_extract import extract_structures
        f = tmp_path / 'svc.ts'
        f.write_text(
            "function compute(n: number): number { return n; }\n"
            "const doubled = (x: number) => compute(x);\n"
            "class Engine { run() { compute(1); } }\n"
        )
        items = extract_structures(f)
        by_name = {i['name']: i for i in items}
        assert 'compute' in by_name['doubled'].get('calls', []), \
            "arrow function body calls must be captured"
        assert 'compute' in by_name['run'].get('calls', []), \
            "TS method body calls must be captured"

    def test_repo_wide_unique_method_resolves_bare_caller(self, tmp_path, temp_store):
        """Chained callees like _get_store().checkpoint_task collapse to the
        bare name; a repo-wide unique method must still resolve its callers."""
        from structural_extract import extract_structures
        (tmp_path / 'store.py').write_text(
            "class Store:\n"
            "    def checkpoint_task(self):\n"
            "        pass\n"
        )
        (tmp_path / 'api.py').write_text(
            "def handler():\n"
            "    checkpoint_task()\n"
        )
        for name in ('store.py', 'api.py'):
            nodes = extract_structures(tmp_path / name)
            temp_store.sync_structural_nodes(name, nodes, project_root=str(tmp_path))
        r = temp_store.who_calls('checkpoint_task')
        assert r['found']
        assert any(c['name'] == 'handler' for c in r['callers']), \
            "repo-wide unique method should resolve bare-name callers"

    def test_file_briefing_excludes_doc_dumps(self, temp_store):
        temp_store.write(category='other', content='wire section ' + 'text ' * 60,
                         tags='bootstrap, docs', file_path='README.md')
        temp_store.write(category='component', content='wire agent config writer',
                         file_path='wire.py')
        briefing = temp_store.get_file_context('wire.py')
        assert '[OTHER #' not in briefing, \
            "doc-dump facts that merely mention a filename are not file knowledge"
        assert 'wire agent config writer' in briefing


# ============================================================
# Sync Exclusion Tests (provenance artifacts never indexed)
# ============================================================

class TestSyncExclusion:
    def test_scratch_files_never_indexed(self, temp_store, tmp_path):
        from structural_extract import extract_structures
        svc = tmp_path / 'scratch' / 'svc.py'
        svc.parent.mkdir(parents=True)
        svc.write_text('def helper():\n    return 1\n')
        nodes = extract_structures(svc)
        assert nodes, "fixture must extract"

        res = temp_store.sync_structural_nodes('scratch/svc.py', nodes,
                                               project_root=str(tmp_path))
        assert res['upserted'] == 0, "scratch/ paths must be excluded"
        count = temp_store.conn.execute(
            'SELECT count(*) AS c FROM structural_nodes').fetchone()['c']
        assert count == 0

    def test_artifact_filenames_never_indexed(self, temp_store, tmp_path):
        from structural_extract import extract_structures
        f = tmp_path / 'code.py'
        f.write_text('def helper():\n    return 1\n')
        nodes = extract_structures(f)

        res = temp_store.sync_structural_nodes('memory_store.PRISTINE.bak.py',
                                               nodes, project_root=str(tmp_path))
        assert res['upserted'] == 0, "recovery artifacts must be excluded"

    def test_normal_files_still_indexed(self, temp_store, tmp_path):
        from structural_extract import extract_structures
        f = tmp_path / 'svc.py'
        f.write_text('def helper():\n    return 1\n')
        nodes = extract_structures(f)

        res = temp_store.sync_structural_nodes('svc.py', nodes,
                                               project_root=str(tmp_path))
        assert res['upserted'] >= 1, "non-excluded files must still sync"


# ============================================================
# Watcher Thread-Safety Tests (background-thread sync)
# ============================================================

class TestWatcherThreadSafety:
    def test_sync_file_from_background_thread(self, temp_store, tmp_path):
        """sqlite3 connections are thread-bound: the watcher must not reuse
        the main-thread store from its observer/polling thread."""
        import threading
        import watcher
        f = tmp_path / 'svc.py'
        f.write_text('def helper():\n    return 1\n')

        errors = []
        def worker():
            try:
                watcher._sync_file(f, tmp_path, temp_store)
            except Exception as e:
                errors.append(e)
        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert not errors, f"watcher sync raised in background thread: {errors}"
        count = temp_store.conn.execute(
            "SELECT count(*) AS c FROM structural_nodes WHERE file_path='svc.py'"
        ).fetchone()['c']
        assert count >= 1, "background-thread sync must index nodes"


# ============================================================
# Git Commit Anchoring Tests (provenance time axis)
# ============================================================

class TestGitCommitAnchoring:
    @pytest.fixture
    def git_repo(self, tmp_path):
        import subprocess
        def run(*args):
            subprocess.run(["git", "-C", str(tmp_path), *args],
                           check=True, capture_output=True)
        run("init", "-b", "main")
        run("config", "user.email", "test@example.com")
        run("config", "user.name", "Test")
        (tmp_path / "svc.py").write_text("def helper():\n    return 1\n")
        run("add", "svc.py")
        run("commit", "-m", "seed")
        head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        return tmp_path, head

    def _store_in(self, repo_root):
        db = repo_root / ".project-memory" / "memory.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        return MemoryStore(str(db))

    def test_anchored_fact_gets_commit_sha(self, git_repo):
        repo_root, head = git_repo
        store = self._store_in(repo_root)
        fid = store.write(category='component', content='helper returns one',
                          file_path='svc.py', line_start=1, line_end=2)
        row = store.conn.execute(
            "SELECT git_commit_sha FROM memory WHERE id = ?", (fid,)).fetchone()
        assert row['git_commit_sha'] == head
        store.close()

    def test_unanchored_fact_gets_no_sha(self, git_repo):
        repo_root, head = git_repo
        store = self._store_in(repo_root)
        fid = store.write(category='decision', content='prefer clarity over cleverness')
        row = store.conn.execute(
            "SELECT git_commit_sha FROM memory WHERE id = ?", (fid,)).fetchone()
        assert row['git_commit_sha'] is None
        store.close()

    def test_explicit_sha_preserved(self, git_repo):
        repo_root, head = git_repo
        store = self._store_in(repo_root)
        fid = store.write(category='component', content='helper returns one',
                          file_path='svc.py', line_start=1, line_end=2,
                          git_commit_sha='explicit-sha')
        row = store.conn.execute(
            "SELECT git_commit_sha FROM memory WHERE id = ?", (fid,)).fetchone()
        assert row['git_commit_sha'] == 'explicit-sha'
        store.close()

    def test_no_repo_means_no_sha(self, temp_store):
        fid = temp_store.write(category='component', content='orphan anchored fact',
                               file_path='svc.py', line_start=1, line_end=2)
        row = temp_store.conn.execute(
            "SELECT git_commit_sha FROM memory WHERE id = ?", (fid,)).fetchone()
        assert row['git_commit_sha'] is None

