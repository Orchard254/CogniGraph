"""
MCP server exposing an enterprise project memory store as tools a coding agent can call.

Run via stdio - point opencode's, Cursor's, or Antigravity's mcpServers config at this script.
"""
import logging
import threading
from contextlib import contextmanager
from typing import List, Dict, Any, Optional, Iterator
from mcp.server.fastmcp import FastMCP
from memory_store import MemoryStore

logger = logging.getLogger(__name__)

mcp = FastMCP("project-memory")
# Create a function to get a store instance (not a global singleton)
# Global flag to track if first-time setup has been done
_first_time_setup_done = False
_setup_lock = threading.Lock()

def _get_store():
    from memory_store import MemoryStore
    return MemoryStore()

@contextmanager
def _store() -> Iterator["MemoryStore"]:
    """Yield a fresh store and always close it (no leaked fd/WAL handles).

    Each MCP tool call gets its own connection, so no SQLite connection is
    ever shared across threads; the finally-close keeps handle counts flat
    under load.
    """
    store = _get_store()
    try:
        yield store
    finally:
        try:
            store.close()
        except Exception:
            pass

def _clamp_limit(limit: int, default: int = 10, maximum: int = 100) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, limit))

def _clamp_chars(max_chars: int, default: int = 8000, maximum: int = 128000) -> int:
    """Clamp a character budget (chars, not tokens — never mix the units)."""
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        return default
    return max(256, min(maximum, max_chars))

def _clamp_ttl(ttl_seconds: int, default: int = 60, maximum: int = 86400) -> int:
    """Clamp a lock TTL: non-numeric falls back, negatives become 1s."""
    try:
        ttl_seconds = int(ttl_seconds)
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, ttl_seconds))

def _clamp_importance(importance: int, default: int = 3) -> int:
    """Clamp fact importance into 1..5; garbage defaults instead of raising."""
    try:
        importance = int(importance)
    except (TypeError, ValueError):
        return default
    return max(1, min(5, importance))

def _ensure_first_time_setup():
    """Run automatic first-time setup (bootstrap) on first tool call."""
    global _first_time_setup_done
    if _first_time_setup_done:
        return
    
    with _setup_lock:
        # Double-check after acquiring lock
        if _first_time_setup_done:
            return

        store = None
        try:
            from bootstrap import bootstrap_project
            from pathlib import Path
            store = _get_store()
            root = Path(store.db_path).parent.parent
            
            # Check if database is empty (new database)
            import sqlite3
            conn = sqlite3.connect(store.db_path, timeout=5.0)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM memory")
                count = cursor.fetchone()[0]
            finally:
                conn.close()
            
            if count == 0:
                logger.info("New database detected, running automatic bootstrap...")
                try:
                    bootstrap_project(root, store=store, dry_run=False, full=False, max_facts=20, install_hook=False, drop_protocol=False)
                    logger.info("Automatic bootstrap completed for %s", root)
                except Exception as e:
                    logger.warning("Automatic bootstrap failed: %r", e)
            else:
                logger.info("Database already contains %d facts, skipping bootstrap", count)
                
            _first_time_setup_done = True
            
        except Exception as e:
            logger.warning("First-time setup failed: %r", e)
            # Continue anyway - the system should still work
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass

def _auto_maintenance():
    """Background self-heal: bootstrap 20 + scan auto_heal + backfill + optimize - keeps 99% without manual loop."""
    store = _get_store()
    try:
        from bootstrap import bootstrap_project
        from pathlib import Path
        root = Path(store.db_path).parent.parent
        # 1. Incremental bootstrap (top 20, no hook/doc drop - just context)
        try:
            bootstrap_project(root, store=store, dry_run=False, full=False, max_facts=20, install_hook=False, drop_protocol=False)
        except Exception as e:
            logger.debug("auto bootstrap failed: %r", e)
        # 2. Scan with auto-heal for low-importance churn
        try:
            store.scan_provenance(str(root), auto_heal=True)
        except Exception as e:
            logger.debug("auto scan failed: %r", e)
        # 3. Backfill any missing embeddings (batched)
        try:
            store.backfill_embeddings()
        except Exception as e:
            logger.debug("auto backfill failed: %r", e)
        # 4. Optimize
        try:
            store.optimize_db()
        except Exception as e:
            logger.debug("auto optimize failed: %r", e)
        logger.info("auto_maintenance completed for %s", root)
    except Exception as e:
        logger.warning("auto_maintenance failed: %r", e)
    finally:
        try:
            store.close()
        except Exception:
            pass

def _start_background_services():
    """Start auto-maintenance and file watcher threads. Called from main block."""
    # Run once on startup in background thread (non-blocking, best-effort)
    try:
        import threading
        threading.Thread(target=_auto_maintenance, daemon=True).start()
    except Exception:
        pass

    # Start real-time file watcher (watchdog or polling) for C:/Ai and other roots
    try:
        from watcher import start_watcher
        from pathlib import Path
        store = _get_store()
        _watch_root = Path(store.db_path).parent.parent
        start_watcher(project_root=_watch_root, store=store)
        logger.info("file watcher started for %s", _watch_root)
    except Exception as e:
        logger.debug("file watcher failed to start: %r", e)


@mcp.tool()
def memory_write(category: str, content: str, rationale: str = "",
                 confidence: str = "assumed", session_id: str = "",
                 file_path: str = "", tags: str = "", importance: int = 3,
                 line_start: int = 0, line_end: int = 0, code_hash: str = "",
                 git_commit_sha: str = "") -> dict:
    """
    Record a new fact in project memory.

    category: component | decision | constraint | state | other
    content: one atomic claim sentence
    rationale: why this is true or why decision was made
    confidence: confirmed | assumed | uncertain
    session_id: optional session identifier
    file_path: optional source file relative/absolute path
    tags: optional comma-separated tags
    importance: 1 (low) to 5 (critical)
    """
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    with _store() as _s:
        new_id = _s.write(
            category=category,
            content=content,
            rationale=rationale or None,
            confidence=confidence,
            session_id=session_id or None,
            file_path=file_path or None,
            tags=tags or None,
            importance=_clamp_importance(importance),
            line_start=line_start or None,
            line_end=line_end or None,
            code_hash=code_hash or None,
            git_commit_sha=git_commit_sha or None,
        )
    if new_id < 0:
        # Auto-dedup guard triggered: near-duplicate already exists
        existing_id = -new_id
        return {
            "id": existing_id,
            "status": "DUPLICATE_DETECTED",
            "message": (
                f"A near-duplicate fact already exists (id={existing_id}). "
                "Use memory_supersede to update it instead of writing a new one."
            ),
        }
    return {"id": new_id, "status": "written"}


@mcp.tool()
def memory_write_batch(facts: List[Dict[str, Any]]) -> dict:
    """
    Record multiple new facts atomically in a single transaction.
    """
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    unembedded: List[int] = []
    with _store() as _s:
        new_ids = _s.write_batch(facts if isinstance(facts, list) else [], embed_errors=unembedded)
    return {
        "count": len(new_ids),
        "written": sum(1 for i in new_ids if i > 0),
        "duplicates_detected": sum(1 for i in new_ids if i < 0),
        "duplicate_of_existing_ids": [-i for i in new_ids if i < 0],
        "ids": new_ids,
        "status": "written_batch",
        # Facts committed but missing semantic coverage (heal via memory_backfill_embeddings)
        "unembedded_ids": unembedded,
    }


@mcp.tool()
def memory_query(text: str, category: str = "", tag: str = "", limit: int = 10, session_id: str = "") -> dict:
    """
    Search project memory by keyword/tag (FTS5 lexical).
    """
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    limit = _clamp_limit(limit, default=10)
    with _store() as _s:
        results = _s.query(text, category=category or None, tag=tag or None, limit=limit, session_id=session_id or None)
    return {"count": len(results), "results": results}


@mcp.tool()
def memory_query_hybrid(text: str, category: str = "", tag: str = "", limit: int = 10, expand_graph: bool = True, session_id: str = "", repo_id: Optional[int] = None, intent: str = "", agent_type: str = "") -> dict:
    """Hybrid RRF search combining lexical BM25 matching and term vector similarity, with intent-aware ranking and usage-pattern learning. Automatically includes graph-linked dependency facts when expand_graph=True. repo_id: optional - if omitted, defaults to the store's own repo (first repo) for episodic filtering. intent: optional task intent ('fix', 'implement', 'refactor', etc.) that boosts category-relevant facts in ranking; auto-detected from text if omitted. agent_type: optional ('cursor', 'claude', 'opencode', 'zed', 'default') to apply agent-specific retrieval-lane weights. Every retrieval is logged for usage-pattern learning."""
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    limit = _clamp_limit(limit, default=10)
    # Derive repo_id for episodic filtering if not provided (single-repo deployment)
    with _store() as _s:
        if repo_id is None:
            repo_id = _s._derive_default_repo_id()
        results = _s.query_hybrid(text, category=category or None, tag=tag or None, limit=limit, expand_graph=expand_graph, session_id=session_id or None, repo_id=repo_id, intent=intent, agent_type=agent_type)
    return {"count": len(results), "results": results}


@mcp.tool()
def memory_get_context(query: str, limit: int = 5, repo_id: Optional[int] = None, max_chars: int = 8000, intent: str = "") -> dict:
    """High-precision context retriever with intent-based expansion and LLM optimization.
    
    Runs hybrid search, expands graph relationships, and returns a clean, fully-formatted Markdown Context block 
    optimized for AI coding agents. Automatically detects intent from query or accepts explicit intent parameter.
    
    Args:
        query: The search query or task description
        limit: Maximum number of facts to include (default: 5)
        repo_id: Optional repository ID for episodic filtering
        max_chars: Maximum character budget to prevent unbounded injection (default: 8000)
        intent: Optional explicit intent (overrides automatic detection): 'implement', 'fix', 'refactor', 'add', 'remove', 'optimize', 'test', 'document', 'debug', 'review'
    
    Returns:
        Dictionary with 'query' and 'context_markdown' keys containing the enhanced context.
    """
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    limit = _clamp_limit(limit, default=5, maximum=50)
    max_chars = _clamp_chars(max_chars)
    with _store() as _s:
        if repo_id is None:
            repo_id = _s._derive_default_repo_id()
        ctx_md = _s.get_context(query, limit=limit, repo_id=repo_id, max_chars=max_chars, intent=intent)
        detected = intent or _s._detect_intent(query)
    return {"query": query, "detected_intent": detected, "context_markdown": ctx_md}


@mcp.tool()
def memory_get_context_optimized(query: str, agent_type: str = "default", limit: int = 5, max_chars: int = 8000, intent: str = "") -> dict:
    """Agent-specific context generation with model-optimized formatting.
    
    Generates context specifically formatted for different LLM architectures and IDEs.
    Automatically detects intent and applies agent-specific formatting rules.
    
    Args:
        query: The search query or task description
        agent_type: Target agent/IDE type: 'cursor', 'claude', 'opencode', 'zed', 'default'
        limit: Maximum number of facts to include (default: 5)
        max_chars: Maximum character budget (default: 8000)
        intent: Optional explicit intent (overrides automatic detection)
    
    Returns:
        Dictionary with 'query', 'agent_type', 'detected_intent', 'format_info', and 'context_markdown' keys.
    
    Example Usage:
        cursor: Optimized for Cursor IDE's context window
        claude: Optimized for Claude Desktop's large context window
        opencode: Optimized for OpenCode's structured output
        zed: Optimized for Zed editor's AI features
    """
    _ensure_first_time_setup()  # Auto-bootstrap on first use

    # Agent-specific formatting configuration (caps below are char budgets,
    # kept in the same units as max_chars so the two never mix).
    AGENT_FORMATS = {
        'cursor': {
            'max_tokens': 8000,
            'format': 'markdown_with_sections',
            'priority_order': ['constraint', 'decision', 'component'],
            'importance_boost': 1.2,
            'instruction_block': 'Follow Cursor IDE best practices for code generation.',
            'output_format': 'Rich Markdown with emoji indicators'
        },
        'claude': {
            'max_tokens': 128000,
            'format': 'concise_json_like',
            'priority_order': ['component', 'decision', 'constraint'],
            'importance_boost': 1.0,
            'instruction_block': 'Provide clear, concise responses suitable for Claude Desktop.',
            'output_format': 'Structured JSON-like format'
        },
        'opencode': {
            'max_tokens': 16000,
            'format': 'structured_sections',
            'priority_order': ['decision', 'constraint', 'component'],
            'importance_boost': 1.1,
            'instruction_block': 'Generate code with OpenCodes preferred patterns.',
            'output_format': 'Sectioned Markdown with clear headings'
        },
        'zed': {
            'max_tokens': 12000,
            'format': 'detailed_with_references',
            'priority_order': ['constraint', 'decision', 'component'],
            'importance_boost': 1.15,
            'instruction_block': 'Provide detailed context for Zed editors AI assistant.',
            'output_format': 'Detailed Markdown with cross-references'
        },
        'default': {
            'max_tokens': 8000,
            'format': 'markdown_with_sections',
            'priority_order': ['constraint', 'decision', 'component'],
            'importance_boost': 1.0,
            'instruction_block': 'Standard LLM context format.',
            'output_format': 'Rich Markdown with emoji indicators'
        }
    }

    limit = _clamp_limit(limit, default=5, maximum=50)
    max_chars = _clamp_chars(max_chars)
    agent_type = agent_type if agent_type in AGENT_FORMATS else "default"

    with _store() as store:
        detected_intent = intent or store._detect_intent(query)

        format_config = AGENT_FORMATS.get(agent_type, AGENT_FORMATS['default'])

        # Generate context with agent-specific parameters (agent lane weights applied at retrieval).
        # NOTE: max_chars is a character budget; format_config caps below are
        # also chars (not tokens) so the units never mix.
        char_cap = min(max_chars, format_config['max_tokens'])
        ctx_md = store.get_context(
            query=query,
            limit=limit,
            repo_id=None,
            max_chars=char_cap,
            intent=detected_intent,
            agent_type=agent_type
        )

        return {
            "query": query,
            "agent_type": agent_type,
            "detected_intent": detected_intent,
            "format_info": dict(format_config),
            "context_markdown": ctx_md
        }


@mcp.tool()
def memory_get_context_for_agent(query: str, agent_type: str = "default", max_tokens: int = 8000) -> dict:
    """Smart context generation that automatically selects the best format for the agent.
    
    This tool intelligently chooses between different context formats based on:
    - Agent type and capabilities
    - Query complexity
    - Available context window size
    
    Args:
        query: The search query or task description
        agent_type: Target agent/IDE type
        max_tokens: Maximum token budget for the output
    
    Returns:
        Optimized context with format selection and usage tips.
    """
    _ensure_first_time_setup()  # Auto-bootstrap on first use

    max_chars = _clamp_chars(max_tokens)
    with _store() as _s:
        detected_intent = _s._detect_intent(query)

    def _tag(result: dict, reason: str) -> dict:
        # Tolerate shape drift from the inner call instead of KeyError.
        if not isinstance(result, dict):
            return {"query": query, "error": "context generation failed", "reason": reason}
        info = result.get("format_info")
        if not isinstance(info, dict):
            result["format_info"] = {}
            info = result["format_info"]
        info["auto_selected"] = True
        info["reason"] = reason
        return result

    # Auto-select best format based on agent and query
    if agent_type == 'claude' or max_chars > 10000:
        # Use concise format for large context windows
        return _tag(memory_get_context_optimized(query, 'claude', limit=10, max_chars=max_chars, intent=detected_intent),
                    'Large context window detected, using concise format')
    elif agent_type == 'cursor':
        # Use structured format for Cursor
        return _tag(memory_get_context_optimized(query, 'cursor', limit=8, max_chars=max_chars, intent=detected_intent),
                    'Cursor IDE detected, using structured format')
    elif agent_type == 'opencode':
        # Use structured sections for OpenCode
        return _tag(memory_get_context_optimized(query, 'opencode', limit=10, max_chars=max_chars, intent=detected_intent),
                    'OpenCode detected, using sectioned format')
    else:
        # Default smart selection
        if max_chars > 8000:
            return _tag(memory_get_context_optimized(query, 'claude', limit=10, max_chars=max_chars, intent=detected_intent),
                        'Large token budget, using concise format')
        else:
            return _tag(memory_get_context_optimized(query, 'default', limit=5, max_chars=max_chars, intent=detected_intent),
                        'Standard token budget, using default format')


@mcp.tool()
def memory_get_file_context(file_path: str) -> dict:
    """Get file context before editing."""
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    with _store() as _s:
        briefing_md = _s.get_file_context(file_path)
    return {"file_path": file_path, "briefing_markdown": briefing_md}


@mcp.tool()
def memory_check_safety(file_paths: List[str]) -> dict:
    """Multi-File Refactor Safety Matrix. Call this BEFORE modifying a batch of multiple files! Analyzes cross-file dependencies, shared constraints, and breaking change risks across all target files."""
    with _store() as _s:
        return _s.check_safety(file_paths if isinstance(file_paths, list) else [])


@mcp.tool()
def memory_get_dependency_map(file_paths: List[str]) -> dict:
    """Cross-File Dependency Map for LLM priming.

    Given a list of files, returns an LLM-readable Markdown map showing:
    - Which of the target files depend on each other
    - Which external files depend on them (and vice versa)
    - Shared constraints/decisions spanning multiple target files
    - High-risk files with 2+ external dependents

    Call this before multi-file refactors to give the LLM a clear picture of
    how the target files relate to each other and the rest of the codebase.
    """
    map_md = None
    with _store() as _s:
        map_md = _s.get_dependency_map(file_paths if isinstance(file_paths, list) else [])
    return {"file_paths": file_paths, "dependency_map_markdown": map_md}


@mcp.tool()
def memory_usage_stats(top_facts: int = 20) -> dict:
    """USAGE-PATTERN INSIGHTS - Summarizes how this project's memory is being used.

    Reports total retrievals, the distribution of detected intents, the most
    frequently retrieved facts (from the last 500 retrievals), and recent
    queries. Call this to understand which facts are load-bearing for agents
    and which areas of the project attract the most attention.
    """
    with _store() as _s:
        return _s.usage_stats(top_facts=_clamp_limit(top_facts, default=20, maximum=100))


@mcp.tool()
def memory_suggest_context(current_fact_ids: List[int], limit: int = 5) -> dict:
    """PROACTIVE CONTEXT SUGGESTIONS - Predicts what context you may need next.

    Based on co-retrieval patterns in the usage log: if facts A and B were
    often retrieved together and A is in your current context, B is suggested.
    Call this after receiving initial context (pass the IDs of facts you
    currently hold) to preemptively pull in related facts and reduce later
    round-trips.
    """
    limit = _clamp_limit(limit, default=5)
    with _store() as _s:
        md = _s.suggest_proactive_context(current_fact_ids if isinstance(current_fact_ids, list) else [], limit=limit)
    return {"current_fact_ids": current_fact_ids, "suggestions_markdown": md}


@mcp.tool()
def memory_optimize_db() -> dict:
    """
    Run SQLite database maintenance: PRAGMA optimize, VACUUM, and check integrity.
    """
    with _store() as _s:
        return _s.optimize_db()


@mcp.tool()
def memory_auto_maintenance(project_root: str = "") -> dict:
    """
    AUTOMATIC MAINTENANCE - self-heals 99%: incremental bootstrap (20) + scan auto_heal (importance=1) + backfill + optimize.
    Call this weekly or on startup instead of manual loop. Also runs automatically in background on server startup.
    """
    from pathlib import Path
    _ensure_first_time_setup()
    with _store() as store:
        root = Path(project_root) if project_root else Path(store.db_path).parent.parent
        # 1. Incremental bootstrap (best-effort)
        try:
            from bootstrap import bootstrap_project
            boot = bootstrap_project(str(root), store=store, dry_run=False, full=False, max_facts=20, install_hook=False, drop_protocol=False)
        except Exception as e:
            logger.warning("maintenance bootstrap failed: %r", e)
            boot = {"error": str(e)}
        # 2. Scan with auto-heal (best-effort)
        try:
            scan = store.scan_provenance(str(root), auto_heal=True) or {}
        except Exception as e:
            logger.warning("maintenance scan failed: %r", e)
            scan = {"error": str(e)}
        # 3. Backfill (best-effort)
        try:
            bf = store.backfill_embeddings()
        except Exception as e:
            logger.warning("maintenance backfill failed: %r", e)
            bf = {"error": str(e)}
        # 4. Optimize (best-effort)
        try:
            opt = store.optimize_db()
        except Exception as e:
            logger.warning("maintenance optimize failed: %r", e)
            opt = {"error": str(e)}
        # 5. Audit for report (best-effort, tolerant keys)
        try:
            audit = store.audit() or {}
        except Exception as e:
            logger.warning("maintenance audit failed: %r", e)
            audit = {}
        scan = scan if isinstance(scan, dict) else {}
        return {
            "bootstrap": boot,
            "scan": {
                "ok": len(scan.get("ok", []) or []),
                "drifted": len(scan.get("drifted", []) or []),
                "healed": len(scan.get("healed", []) or []),
                "missing": len(scan.get("missing", []) or []),
            },
            "backfill": bf,
            "optimize": opt,
            "audit_dupes": audit.get("potential_duplicates_count", 0),
        }


@mcp.tool()
def memory_export_ui(out_path: str = "") -> dict:
    """
    Generate an interactive HTML Knowledge Graph visualizer file for browsing memory.
    """
    with _store() as _s:
        html_file = _s.export_interactive_html(out_path or None)
    return {"status": "generated", "file_path": html_file}


@mcp.tool()
def memory_supersede(old_id: int, new_content: str, rationale: str = "",
                      confidence: str = "assumed", session_id: str = "",
                      file_path: str = "", tags: str = "", expected_version: int = 0) -> dict:
    """
    Replace an outdated fact with a corrected or updated one.
    Use expected_version > 0 for optimistic concurrency control.
    """
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    with _store() as _s:
        new_id = _s.supersede(
            old_id=old_id,
            new_content=new_content,
            rationale=rationale or None,
            confidence=confidence,
            session_id=session_id or None,
            file_path=file_path or None,
            tags=tags or None,
            expected_version=expected_version if expected_version > 0 else None,
        )
    return {"new_id": new_id, "old_id": old_id, "status": "superseded"}


@mcp.tool()
def memory_deprecate(id: int, reason: str = "") -> dict:
    """
    Mark a fact as no longer valid without replacing it.
    """
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    with _store() as _s:
        _s.deprecate(id, reason or None)
    return {"id": id, "status": "deprecated"}


@mcp.tool()
def memory_get_state(category: str = "", session_id: str = "") -> dict:
    """
    Get full snapshot of active project memory.
    """
    with _store() as _s:
        results = _s.get_state(category=category or None, session_id=session_id or None)
    return {"count": len(results), "facts": results}


@mcp.tool()
def memory_link_facts(source_id: int, target_id: int, relation: str) -> dict:
    """
    Create a directed graph edge between two facts.
    relation: depends_on | implements | conflicts_with | relates_to
    """
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    with _store() as _s:
        edge_id = _s.link_facts(source_id, target_id, relation)
    return {"edge_id": edge_id, "source_id": source_id, "target_id": target_id, "relation": relation, "status": "linked"}


@mcp.tool()
def memory_unlink_facts(source_id: int, target_id: int, relation: str) -> dict:
    """
    Remove a directed graph edge between two facts.
    """
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    with _store() as _s:
        _s.unlink_facts(source_id, target_id, relation)
    return {"source_id": source_id, "target_id": target_id, "relation": relation, "status": "unlinked"}


@mcp.tool()
def memory_get_graph(root_id: int, max_depth: int = 3) -> dict:
    """
    Traverse relationship edges starting from root_id up to max_depth.
    Returns DAG of related components, decisions, and constraints.
    """
    max_depth = _clamp_limit(max_depth, default=3, maximum=10)
    with _store() as _s:
        graph = _s.get_graph(root_id, max_depth)
    return {"count": len(graph), "root_id": root_id, "graph": graph}


@mcp.tool()
def memory_acquire_lock(resource: str, locked_by: str, ttl_seconds: int = 60) -> dict:
    """
    Acquire an advisory lock on a named resource for multi-turn multi-agent operations.
    """
    with _store() as _s:
        success = _s.acquire_lock(resource, locked_by, _clamp_ttl(ttl_seconds))
    return {"resource": resource, "locked_by": locked_by, "acquired": success}


@mcp.tool()
def memory_release_lock(resource: str, locked_by: str) -> dict:
    """
    Release an advisory lock on a named resource.
    """
    with _store() as _s:
        released = _s.release_lock(resource, locked_by)
    return {"resource": resource, "locked_by": locked_by, "released": released}


@mcp.tool()
def memory_verify_provenance(fact_id: int, project_root: str = "") -> dict:
    """
    Verify code snippet SHA256 hash at file_path. Self-heals line range shifts
    or flags code drift / deletion.
    """
    with _store() as _s:
        res = _s.verify_provenance(fact_id, project_root or None)
    return res


@mcp.tool()
def memory_consolidate(session_id: str = "", category: str = "state", allow_all: bool = False) -> dict:
    """
    Compress itemized micro-state facts into milestone summary facts.
    Requires session_id to scope the operation. Pass allow_all=True explicitly
    to consolidate ALL state facts (destructive - collapses entire state history).
    """
    with _store() as _s:
        res = _s.consolidate_state_facts(session_id or None, category, allow_all=allow_all)
    return res


@mcp.tool()
def memory_prune(days_inactive: int = 90, dry_run: bool = True) -> dict:
    """
    PRUNE STALE FACTS - Deprecate low-value state/other facts not accessed in
    `days_inactive` days. Safety rails: never touches importance >= 4 facts,
    facts with graph edges, or the last 10 active facts.
    ALWAYS run with dry_run=True first to preview candidates.
    """
    with _store() as _s:
        return _s.prune_stale(days_inactive=max(1, days_inactive), dry_run=dry_run)


@mcp.tool()
def memory_audit() -> dict:
    """
    Scan active memory for potential near-duplicates or conflicting facts.
    """
    with _store() as _s:
        return _s.audit()


@mcp.tool()
def memory_export_markdown(category: str = "") -> dict:
    """
    Export active memory as formatted markdown for LLM context prompts.
    """
    with _store() as _s:
        md = _s.export_markdown(category=category or None)
    return {"markdown": md}


@mcp.tool()
def memory_stats() -> dict:
    """
    Overview of memory database statistics, categories, edges, and location.
    """
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    with _store() as _s:
        return _s.stats()


@mcp.tool()
def memory_prepare_task(intent: str, file_paths: List[str] | None = None, limit: int = 5) -> dict:
    """PRE-CODING FLIGHT BRIEFING - Call this at the START of every coding task.
    
    Returns a complete Markdown briefing containing:
    1. Architectural context and prior decisions relevant to your intent
    2. File impact analysis for each file you plan to edit
    3. Cross-file dependency warnings and breaking change risks
    4. Latest verified test baseline and state checkpoint

    intent: Short description of what you are about to implement or fix
    file_paths: List of files you plan to edit (optional but recommended)
    """
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    limit = _clamp_limit(limit, default=5, maximum=50)
    with _store() as _s:
        briefing = _s.prepare_task(intent, file_paths=file_paths or None, limit=limit)
    return {"intent": intent, "briefing_markdown": briefing}


@mcp.tool()
def memory_checkpoint_task(
    summary: str,
    components: List[Dict[str, Any]] | None = None,
    decisions: List[Dict[str, Any]] | None = None,
    test_baseline: str = "",
    failures: List[Dict[str, Any]] | None = None,
) -> dict:
    """
    POST-CODING CHECKPOINT - Call this at the END of every coding task.

    Atomically writes all newly learned components, design decisions, and the
    verified test baseline as a state checkpoint into project memory.

    summary: One-paragraph summary of what was implemented and why
    components: List of dicts with keys: content, file_path, line_start, line_end, importance
    decisions: List of dicts with keys: content, rationale
    test_baseline: Output from pytest/vitest e.g. '701 passed, 32 skipped'
    failures: NEGATIVE KNOWLEDGE - approaches tried that did NOT work, each a
        dict with content (what was tried) and rationale (why it failed).
        Stored as constraints so future agents do not re-derive the dead end.
    """
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    with _store() as _s:
        result = _s.checkpoint_task(
            summary=summary,
            components=components or [],
            decisions=decisions or [],
            test_baseline=test_baseline or None,
            failures=failures or [],
        )
    # Translate negative dedup sentinels to positive IDs, matching memory_write/memory_write_batch
    fact_ids = result.get("fact_ids", [])
    if any(isinstance(i, int) and i < 0 for i in fact_ids) or result.get("state_id", 0) < 0:
        raw_ids = list(fact_ids)
        result["fact_ids"] = [-i if isinstance(i, int) and i < 0 else i for i in raw_ids]
        if "duplicates_of_existing_ids" not in result:
            result["duplicates_of_existing_ids"] = [-i for i in raw_ids if isinstance(i, int) and i < 0]
        sid = result.get("state_id")
        if isinstance(sid, int) and sid < 0:
            result["state_id"] = -sid
            result["state_was_duplicate"] = True
    else:
        result.setdefault("duplicates_of_existing_ids", result.get("duplicates_of_existing_ids", []))
        result.setdefault("state_was_duplicate", False)
    return result


@mcp.tool()
def memory_scan_provenance(project_root: str = "", auto_deprecate: bool = False, auto_heal: bool = False) -> dict:
    """
    BULK PROVENANCE HEALTH CHECK - Scan all active facts for code drift.

    Checks every fact that has a file_path + code_hash recorded and reports:
    - ok: facts whose code snippet hash still matches the file on disk
    - drifted: facts whose code has changed since the fact was recorded
    - missing: facts whose files have been deleted
    - healed: auto-healed low-importance drifted facts (when auto_heal=True)

    project_root: Root directory to resolve relative file paths (defaults to CWD)
    auto_deprecate: If True, automatically deprecate facts for deleted files
    auto_heal: If True, auto-update code_hash for low-importance (1) file-coverage drifted facts
    """
    with _store() as _s:
        return _s.scan_provenance(project_root or None, auto_deprecate=auto_deprecate, auto_heal=auto_heal)


@mcp.tool()
def memory_backfill_embeddings() -> dict:
    """
    SEMANTIC INDEX BACKFILL - Generate embeddings for all existing facts that lack them.

    Run once after upgrading the memory system to populate the semantic search index
    for existing historical facts. Safe to call multiple times (skips already-embedded facts).
    Uses the all-MiniLM-L6-v2 model (384-dim, approx 80MB, CPU-friendly).
    """
    with _store() as _s:
        return _s.backfill_embeddings()


@mcp.tool()
def memory_promote_branch(source_branch_id: int, convergence_policy: str = "KEEP_BOTH") -> dict:
    """
    Promote an experimental branch's episodic memories into its canonical root (KEEP_BOTH).

    Reads branch-local ACTIVE episodic_memories (branch_id=source), classifies each as
    ADDITION vs CONVERGENT vs UNCHANGED against current canonical state, and for
    ADDITION/CONVERGENT inserts new canonical rows under the target root (new memory_id,
    same revision) - KEEP_BOTH keeps both rows for CONVERGENT, never merges. Inserts
    branch_promotions ledger, memory_lineage_events PROMOTED, and marks source branch
    PROMOTED atomically in one RevisionTransaction.

    source_branch_id: experimental branch to promote (must be ACTIVE, non-root)
    convergence_policy: only KEEP_BOTH is implemented in Phase 0
    """
    from branch_promotion import promote_branch

    # Use the module-level store's DB path so the MCP tool operates on the same DB
    # the rest of the tools use (respects MEMORY_DB_PATH env).
    with _store() as _s:
        db_path = _s.db_path
    result = promote_branch(db_path, source_branch_id, convergence_policy=convergence_policy)
    return {"status": "promoted", "source_branch_id": source_branch_id, **result}


@mcp.tool()
def memory_create_branch(repo_id: Optional[int] = None) -> dict:
    """
    Create a new experimental branch under the canonical root.

    Per Section 18, base_revision is the canonical root's current_revision at fork time,
    read inside the same RevisionTransaction that performs the INSERT so it cannot race
    with a concurrent canonical write. Per Section 16 the new branch is always a direct
    child of the root (single-level); trg_branches_single_level enforces this.

    repo_id: if provided, the new branch belongs to that repo (validated to exist);
             if None, derives single-repo stopgap via first repo in DB (same as query_hybrid).
    No name/label column exists (8-column memory_branches per live schema and Sections 16-19);
    branches are addressable by branch_id.

    Returns dict with branch_id, repo_id, parent_branch_id, base_revision, base_timestamp.
    """
    from branch_promotion import create_branch

    with _store() as _s:
        db_path = _s.db_path
    result = create_branch(db_path, repo_id=repo_id)
    return {"status": "created", **result}


@mcp.tool()
def memory_discard_branch(source_branch_id: int, reason: str = "") -> dict:
    """
    Discard an experimental branch (terminal alternative to promotion).

    Inserts branch_tombstones + DISCARDED lineage and transitions status to
    DISCARDED atomically in one RevisionTransaction. Only ACTIVE non-root
    branches can be discarded; root / PROMOTED / DISCARDED are rejected.
    Mirrors promote_branch in discipline: same revision stamping, same
    trigger enforcement, same atomic rollback guarantee.

    source_branch_id: experimental branch to discard (must be ACTIVE, non-root)
    reason: optional human-readable rationale stored in branch_tombstones.reason
    """
    from branch_promotion import discard_branch

    with _store() as _s:
        db_path = _s.db_path
    result = discard_branch(db_path, source_branch_id, reason=reason or None)
    return {"status": "discarded", "source_branch_id": source_branch_id, **result}


@mcp.tool()
def memory_list_branches(repo_id: Optional[int] = None, status: str = "") -> dict:
    """
    List branches for a repo (read-only).

    Returns branch_id, status, base_revision, created_at, parent info.
    Repo derivation mirrors create_branch/query_hybrid: if repo_id is None,
    derives the single-repo default via first repo in DB. Status filter is
    optional (ACTIVE/PROMOTED/DISCARDED); when omitted all are returned.

    repo_id: optional - if omitted, defaults to the store's own repo.
    status: optional status filter - ACTIVE | PROMOTED | DISCARDED
    """
    from branch_promotion import list_branches

    with _store() as _s:
        db_path = _s.db_path
    result = list_branches(db_path, repo_id=repo_id, status=status or None)
    return {"status": "listed", **result}


@mcp.tool()
def memory_bootstrap_project(project_root: str = "", dry_run: bool = True,
                             full: bool = False, max_facts: int = 150,
                             install_hook: bool = True, drop_protocol: bool = True) -> dict:
    """
    PROJECT ONBOARDING - Scan a repository and ingest its structural + documentary
    context into memory: directory summaries, key files, manifests/dependencies,
    README/docs sections, AGENTS.md conventions, git branch + tagged commits.
    Idempotent: re-running skips duplicates via the dedup guard. When applied,
    installs the git post-commit hook and drops AGENTS.md if absent
    (disable with install_hook=False / drop_protocol=False).
    ALWAYS run with dry_run=True first to preview.
    """
    from bootstrap import bootstrap_project
    max_facts = _clamp_limit(max_facts, default=150, maximum=1000)
    with _store() as _s:
        return bootstrap_project(project_root or None, store=_s, dry_run=dry_run,
                                 full=full, max_facts=max_facts,
                                 install_hook=install_hook, drop_protocol=drop_protocol)


@mcp.tool()
def memory_who_calls(symbol: str) -> dict:
    """ONE-HOP CODE GRAPH QUERY - ask this mid-reasoning instead of re-reading files.

    Returns where a symbol is defined, every caller of it (name, class, file,
    line ranges), and which files import its file. Accepts a bare name
    ('query_hybrid') or Class.method ('MemoryStore.write'). Every edge is
    ground truth extracted from the tree-sitter AST, not LLM output.
    """
    with _store() as _s:
        return _s.who_calls(symbol)


@mcp.tool()
def memory_project_shape() -> dict:
    """PROJECT ORIENTATION - indexed file inventory and code-graph size.

    Call once early in a session to ground yourself: how many files are
    indexed, which modules carry the most definitions, and how large the
    call/import graph is.
    """
    with _store() as _s:
        return {"markdown": _s.project_shape()}


@mcp.tool()
def memory_flag_fact(fact_id: int, note: str = "", flag_type: str = "misleading") -> dict:
    """CORRECTION HOOK - fire this the moment a served fact turns out wrong.

    Records a flag that future briefings print next to the fact, so the same
    bad context is never silently consumed again. Prefer memory_supersede when
    you know the corrected content; use this when you only know it misled you.
    """
    with _store() as _s:
        return _s.flag_fact(fact_id, note=note, flag_type=flag_type)


if __name__ == "__main__":
    _start_background_services()
    mcp.run()
