"""
MCP server exposing an enterprise project memory store as tools a coding agent can call.

Run via stdio - point opencode's `mcp` config (others use `mcpServers`) at this script.
"""
import contextvars
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import List, Dict, Any, Optional, Iterator
from urllib.parse import urlparse, unquote
from mcp.server.fastmcp import FastMCP
from memory_store import MemoryStore
from pm_common import (
    DATA_DIR_NAME,
    LEGACY_DATA_DIR_NAME,
    default_db_path,
    find_project_root,
)

logger = logging.getLogger(__name__)

mcp = FastMCP("CogniGraph")
# Create a function to get a store instance (not a global singleton)
# Global flag to track if first-time setup has been done
_first_time_setup_done = False
_first_time_setup_dbs: set = set()
_setup_lock = threading.Lock()
_active_db_path: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_active_db_path", default=None)


def _uri_to_path(uri: str) -> Optional[Path]:
    """Convert a file:// URI or filesystem path string to a Path object."""
    try:
        parsed = urlparse(str(uri))
        if parsed.scheme != "file":
            return Path(uri)
        path = unquote(parsed.path)
        if path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        return Path(path)
    except Exception:
        return None


def _resolve_db_from_root(root_dir: Path) -> Optional[str]:
    """Given a project root directory, locate existing or canonical memory.db."""
    try:
        root_dir = Path(root_dir).resolve()
        if (root_dir / DATA_DIR_NAME / "memory.db").is_file():
            return str(root_dir / DATA_DIR_NAME / "memory.db")
        if (root_dir / LEGACY_DATA_DIR_NAME / "memory.db").is_file():
            return str(root_dir / LEGACY_DATA_DIR_NAME / "memory.db")
        if root_dir.is_dir():
            return str(root_dir / DATA_DIR_NAME / "memory.db")
    except Exception:
        pass
    return None


def _get_store(*args, **kwargs):
    from memory_store import MemoryStore
    db_path = kwargs.get("db_path_override") or _active_db_path.get() or default_db_path()
    return MemoryStore(db_path)


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

def _ensure_first_time_setup(db_path: Optional[str] = None):
    """Run automatic first-time setup (bootstrap) on first tool call."""
    global _first_time_setup_done
    target_db = db_path or _active_db_path.get() or default_db_path()
    if target_db in _first_time_setup_dbs:
        return
    
    with _setup_lock:
        # Double-check after acquiring lock
        if target_db in _first_time_setup_dbs:
            return

        store = None
        try:
            from bootstrap import bootstrap_project
            from pathlib import Path
            store = _get_store(target_db)
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
                logger.info("New database detected, running automatic bootstrap for %s...", root)
                try:
                    bootstrap_project(root, store=store, dry_run=False, full=False, max_facts=20, install_hook=False, drop_protocol=False)
                    logger.info("Automatic bootstrap completed for %s", root)
                except Exception as e:
                    logger.warning("Automatic bootstrap failed: %r", e)
            else:
                logger.info("Database %s already contains %d facts, skipping bootstrap", store.db_path, count)
                
            _first_time_setup_done = True
            _first_time_setup_dbs.add(target_db)
            
        except Exception as e:
            logger.warning("First-time setup failed for %s: %r", target_db, e)
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
def memory_check_hygiene(project_root: str = "") -> dict:
    """Commit/push-discipline status for the working tree: cleanliness, ahead/behind, stale branches. Local git only, read-only; no CI correlation in v1."""
    # Deliberately no _ensure_first_time_setup: a read-only status probe must
    # never trigger bootstrap writes (AGENTS.md / hook install).
    with _store() as _s:
        return _s.check_hygiene(project_root or None)


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
def memory_who_inherits(symbol: str, max_depth: int = 5) -> dict:
    """Which classes inherit from this base class (exact), transitively. No LIKE false positives."""
    with _store() as _s:
        return _s.who_inherits(symbol, max_depth=max_depth)


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
def memory_read_code(items: List[Dict[str, Any]], max_lines_per_item: int = 200,
                     max_total_chars: int = 24000, context: int = 0) -> dict:
    """Read scoped source lines by symbol or explicit file range, batched in one call. Request order is preserved in results; batch budget cutoffs return per-item errors (not partial content) so re-request those items in a smaller batch."""
    with _store() as _s:
        return _s.read_code(items=items if isinstance(items, list) else [],
                            max_lines_per_item=max_lines_per_item,
                            max_total_chars=max_total_chars,
                            context=context)


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


_orig_call_tool = mcp.call_tool


async def _dynamic_call_tool(name: str, arguments: dict[str, Any]) -> Any:
    """Dynamically route tool invocations to the active workspace's database."""
    resolved_db = None

    # 1. Explicit project_root argument in tool call (if present)
    if arguments and isinstance(arguments, dict):
        explicit_root = arguments.get("project_root")
        if explicit_root:
            cand = _resolve_db_from_root(Path(explicit_root))
            if cand:
                resolved_db = cand

    # 2. Attempt to resolve active workspace root from MCP session roots (e.g. Antigravity)
    if not resolved_db:
        try:
            context = mcp.get_context()
            if context and hasattr(context, "session") and context.session:
                roots_result = await context.session.list_roots()
                if roots_result and hasattr(roots_result, "roots") and roots_result.roots:
                    for r in roots_result.roots:
                        uri = getattr(r, "uri", None) or (r.get("uri") if isinstance(r, dict) else None)
                        if uri:
                            root_path = _uri_to_path(str(uri))
                            if root_path:
                                candidate_db = _resolve_db_from_root(root_path)
                                if candidate_db:
                                    resolved_db = candidate_db
                                    break
        except Exception as e:
            logger.debug("dynamic roots resolution skipped: %r", e)

    # 3. Fallback: inspect arguments for file_path / file_paths / items
    if not resolved_db and arguments and isinstance(arguments, dict):
        file_arg = arguments.get("file_path")
        if not file_arg and isinstance(arguments.get("file_paths"), list) and arguments["file_paths"]:
            file_arg = arguments["file_paths"][0]
        if not file_arg and isinstance(arguments.get("items"), list) and arguments["items"]:
            first_item = arguments["items"][0]
            if isinstance(first_item, dict):
                file_arg = first_item.get("file_path") or first_item.get("file")
        if file_arg and isinstance(file_arg, str):
            try:
                cand_root = find_project_root(Path(file_arg).resolve())
                if cand_root:
                    resolved_db = _resolve_db_from_root(cand_root)
            except Exception:
                pass

    if resolved_db:
        token = _active_db_path.set(resolved_db)
        try:
            return await _orig_call_tool(name, arguments)
        finally:
            _active_db_path.reset(token)
    else:
        return await _orig_call_tool(name, arguments)


mcp.call_tool = _dynamic_call_tool
mcp._mcp_server.call_tool()(_dynamic_call_tool)


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
