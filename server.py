"""
MCP server exposing an enterprise project memory store as tools a coding agent can call.

Run via stdio - point opencode's `mcp` config (others use `mcpServers`) at this script.
"""
import logging
import threading
from contextlib import contextmanager
from typing import List, Dict, Any, Optional, Iterator
from mcp.server.fastmcp import FastMCP
from memory_store import MemoryStore

logger = logging.getLogger(__name__)

mcp = FastMCP("CogniGraph")
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
        except Exception as e:
            logger.warning("store close failed: %r", e)

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
        # 0. Timestamped full-DB snapshot before any maintenance mutation
        try:
            from pm_backup import timestamped_backup
            timestamped_backup(store.db_path, tag="routine")
        except Exception as e:
            logger.debug("auto backup failed: %r", e)
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
    """Record a durable fact this session learned that future sessions will need: decisions, constraints, gotchas, where components live. Secrets are refused."""
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
    """Atomic batch form of memory_write for several related facts."""
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
        # Facts committed but missing semantic coverage (heal via `backfill` CLI / store.backfill_embeddings)
        "unembedded_ids": unembedded,
    }


@mcp.tool()
def memory_query(text: str, category: str = "", tag: str = "", limit: int = 10, expand_graph: bool = True, session_id: str = "", repo_id: Optional[int] = None, intent: str = "", agent_type: str = "") -> dict:
    """Search project memory — decisions, constraints, components, past failures — via hybrid lexical + semantic + code-structure search. Use when you need project knowledge you don't have."""
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    limit = _clamp_limit(limit, default=10)
    # Derive repo_id for episodic filtering if not provided (single-repo deployment)
    with _store() as _s:
        if repo_id is None:
            repo_id = _s._derive_default_repo_id()
        results = _s.query_hybrid(text, category=category or None, tag=tag or None, limit=limit, expand_graph=expand_graph, session_id=session_id or None, repo_id=repo_id, intent=intent, agent_type=agent_type)
    return {"count": len(results), "results": results}


@mcp.tool()
def memory_get_context(query: str, agent_type: str = "default", max_tokens: int = 2000, limit: int = 5, intent: str = "") -> dict:
    """Token-budgeted Markdown briefing on a topic, formatted for your agent type and token ceiling. Use before working in an unfamiliar area."""
    _ensure_first_time_setup()  # Auto-bootstrap on first use

    # Agent-specific formatting configuration (caps below are char budgets,
    # kept in the same units as max_chars so the two never mix).
    # Absorbed from memory_get_context_optimized / memory_get_context_for_agent.
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
    # Token budget converted to chars via the absorbed for_agent logic
    # (_clamp_chars keeps units consistent with the per-agent char caps).
    max_chars = _clamp_chars(max_tokens)
    agent_type = agent_type if agent_type in AGENT_FORMATS else "default"

    with _store() as store:
        detected_intent = intent or store._detect_intent(query)
        format_config = AGENT_FORMATS.get(agent_type, AGENT_FORMATS['default'])
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
def memory_get_file_context(file_path: str) -> dict:
    """Call while planning an approach to a file — and before editing it: its symbols, anchored facts, callers, and call-graph edges."""
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    with _store() as _s:
        briefing_md = _s.get_file_context(file_path)
    return {"file_path": file_path, "briefing_markdown": briefing_md}


@mcp.tool()
def memory_check_safety(file_paths: List[str]) -> dict:
    """Call before a multi-file refactor: dependency risk matrix across the files you're about to change."""
    with _store() as _s:
        return _s.check_safety(file_paths if isinstance(file_paths, list) else [])


@mcp.tool()
def memory_supersede(old_id: int, new_content: str, rationale: str = "",
                      confidence: str = "assumed", session_id: str = "",
                      file_path: str = "", tags: str = "", expected_version: int = 0) -> dict:
    """Replace an outdated fact with corrected content. Use when you discover memory that is wrong."""
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
    """Mark a fact obsolete without replacement."""
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    with _store() as _s:
        _s.deprecate(id, reason or None)
    return {"id": id, "status": "deprecated"}


@mcp.tool()
def memory_stats() -> dict:
    """Memory health: active/deprecated counts, category breakdown, edges. Call at session start per AGENTS.md."""
    _ensure_first_time_setup()  # Auto-bootstrap on first use
    with _store() as _s:
        return _s.stats()


@mcp.tool()
def memory_prepare_task(intent: str, file_paths: List[str] | None = None, limit: int = 5) -> dict:
    """REQUIRED before writing or modifying any code in this repository. Returns the task briefing: relevant decisions, constraints, component facts, file-level impacts, cross-file dependency risks, and the current verified test baseline."""
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
    """Call immediately after a task is verified complete (tests green). Records new components, decisions, the test baseline, and failed approaches as durable memory for future sessions."""
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
def memory_who_calls(symbol: str) -> dict:
    """Where a symbol is defined, every call site, and importing files. Ask mid-reasoning while planning an approach — and before renaming or changing a signature."""
    with _store() as _s:
        return _s.who_calls(symbol)


@mcp.tool()
def memory_blast_radius(symbol: str, class_name: str = "", file_path: str = "",
                        max_depth: int = 5, direction: str = "callers",
                        file_detail: str = "", max_symbols_per_file: int = 20) -> dict:
    """Transitive blast radius: who depends on this symbol, transitively. Call while planning an approach or estimating effort, before making changes — not just as a pre-edit gate."""
    with _store() as _s:
        return _s.blast_radius(symbol, class_name=class_name or "",
                               file_path=file_path or "",
                               max_depth=max_depth, direction=direction or "callers",
                               file_detail=file_detail or "",
                               max_symbols_per_file=max_symbols_per_file)


@mcp.tool()
def memory_project_shape() -> dict:
    """Lay of the land: indexed files, top modules by definition density, call/import edge totals."""
    with _store() as _s:
        return {"markdown": _s.project_shape()}


@mcp.tool()
def memory_flag_fact(fact_id: int, note: str = "", flag_type: str = "misleading") -> dict:
    """Flag a fact as misleading or unverified so future briefings carry a warning. Use when memory looks wrong but you lack the correction."""
    with _store() as _s:
        return _s.flag_fact(fact_id, note=note, flag_type=flag_type)


if __name__ == "__main__":
    # One-shot legacy (.project-memory -> .cognigraph) data-dir migration.
    # Runs ONCE here, before any store is opened or the watcher thread starts
    # (os.replace on an open SQLite file fails on Windows) — never per tool call.
    try:
        from pm_common import ensure_startup_migration
        ensure_startup_migration()
    except Exception as e:
        logger.warning("startup migration check failed: %r", e)
    _start_background_services()
    mcp.run()
