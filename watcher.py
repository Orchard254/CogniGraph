"""
File watcher for automatic memory sync — real-time file change detection.

Watches the project root (e.g., C:/Ai) for .py/.ts/.js/.rs changes and incrementally
updates memory: re-extracts structural nodes, re-hashes, and upserts file-level facts.

Fallback: polling every 30s if watchdog not available. Non-blocking, daemon thread.
"""
import time
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False
    Observer = None
    FileSystemEventHandler = object

try:
    from memory_store import MemoryStore, bump_background_failure
except ImportError:
    MemoryStore = None

    def bump_background_failure(kind):
        pass

try:
    from structural_extract import extract_structures
except ImportError:
    extract_structures = None

WATCHED_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java"}
IGNORE_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__", ".project-memory", ".cognigraph", "dist", "build", "target", ".next", ".cache", ".idea", ".vscode", "site-packages", ".pytest_cache", ".mypy_cache", "coverage", ".opencode", "scratch", "fixtures"}

def _should_watch(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root) if path.is_absolute() else path
    except ValueError:
        return False
    for part in rel.parts:
        if part in IGNORE_DIRS or part.endswith(".egg-info"):
            return False
    if path.suffix.lower() not in WATCHED_EXTS:
        return False
    try:
        if path.stat().st_size > 1_000_000:
            return False
    except (OSError, FileNotFoundError):
        # File deleted or unreadable between event delivery and handler.
        return False
    return True

_thread_local = threading.local()

def _thread_store(db_path: str) -> MemoryStore:
    """Per-thread MemoryStore for watcher callbacks.

    sqlite3 connections are bound to the thread that created them, but every
    watcher callback (watchdog observer or polling loop) runs on a background
    thread — reusing the main-thread store raises ProgrammingError, which the
    sync error handling used to swallow silently.
    """
    cache = getattr(_thread_local, "stores", None)
    if cache is None:
        cache = {}
        _thread_local.stores = cache
    store = cache.get(db_path)
    if store is None:
        store = MemoryStore(db_path)
        cache[db_path] = store
    return store

def close_thread_stores(db_path: Optional[str] = None) -> None:
    """Close per-thread cached stores so watcher threads don't leak handles.

    Call on watcher shutdown. If db_path is given, only that entry is closed.
    """
    cache = getattr(_thread_local, "stores", None)
    if not cache:
        return
    targets = [db_path] if db_path else list(cache.keys())
    for key in targets:
        store = cache.pop(key, None)
        if store is not None:
            try:
                store.close()
            except Exception:
                pass

def _prune_deleted_file(rel: str, store: MemoryStore) -> None:
    """Remove structural rows for a deleted file. Never raises.

    The deletion complement to _sync_file: sync only revisits files that
    still exist, so without this, deleted files leave permanent orphan
    rows. Child tables (structural_edges, call_sites, fact_node_links)
    clean up automatically via ON DELETE CASCADE.
    """
    try:
        if MemoryStore is not None and store is not None:
            store = _thread_store(store.db_path)
        store.delete_structural_file(rel)
    except Exception as e:
        logger.warning("watcher delete prune failed %s: %r", rel, e)
        bump_background_failure("structural_sync")


def _sync_file(file_path: Path, root: Path, store: MemoryStore):
    """Incrementally sync a single file: structural nodes + file fact."""
    if MemoryStore is not None and store is not None:
        store = _thread_store(store.db_path)
    try:
        rel = file_path.relative_to(root).as_posix()
        if not file_path.is_file():
            # Deleted between event delivery and handler (or moved away):
            # prune structural rows; there is no fact left to upsert for
            # a path with nothing on disk.
            _prune_deleted_file(rel, store)
            return
        # 1. Structural nodes
        if extract_structures:
            try:
                nodes = extract_structures(file_path)
                store.sync_structural_nodes(rel, nodes, project_root=str(root))
            except Exception as e:
                logger.warning("watcher structural sync failed %s: %r", rel, e)
                bump_background_failure("structural_sync")
        # 2. File fact upsert (hash + lines)
        try:
            data = file_path.read_bytes()
            h = hashlib.sha256(data).hexdigest()
            lines = data.decode('utf-8', errors='replace').count('\n') + 1
            content = f"Source file: {rel} (python, {lines} lines, hash {h[:8]})" if file_path.suffix.lower()==".py" else f"Source file: {rel} ({lines} lines, hash {h[:8]})"
            # Only rows whose content is already a stub template are upserted:
            # a curated fact anchored to this file must never be clobbered by
            # the file-sync rewrite (that destroyed 7 curated facts historically).
            existing = store.conn.execute(
                "SELECT id, code_hash FROM memory WHERE file_path=? AND deprecated_at IS NULL AND content LIKE 'Source file:%'",
                (rel,)).fetchall()
            if existing:
                # Update hash directly if changed (avoid supersede dance which creates duplicate with old hash).
                # Wrapped in one transaction so concurrent syncs can't interleave;
                # re-embed stays outside (slow model call, must not hold the lock).
                changed = [(row["id"],) for row in existing if row["code_hash"] != h]
                if changed:
                    store.conn.execute("BEGIN IMMEDIATE")
                    try:
                        for (fid,) in changed:
                            store.conn.execute(
                                "UPDATE memory SET code_hash=?, line_start=1, line_end=?, content=? WHERE id=?",
                                (h, lines, content, fid)
                            )
                        store.conn.execute("COMMIT")
                    except Exception:
                        try:
                            store.conn.execute("ROLLBACK")
                        except Exception:
                            pass
                        raise
                    for (fid,) in changed:
                        # Re-embed so semantic lane reflects the updated content
                        try:
                            store._store_embedding(fid, content)
                        except Exception as e:
                            logger.warning("watcher re-embed failed for fact %s: %r", fid, e)
                            bump_background_failure("re_embed")
            else:
                # New file — "other" like bootstrap's template facts, so stubs
                # never ride intent boosts meant for curated knowledge.
                store.write(category="other", content=content, file_path=rel, tags="bootstrap, file, auto-watched", importance=2, line_start=1, line_end=lines, code_hash=h, force=True, embed=True)
            # Link to structural nodes
            try:
                store.link_facts_to_structural_nodes(rel)
            except Exception as e:
                logger.warning("watcher fact linking failed %s: %r", rel, e)
                bump_background_failure("fact_linking")
        except Exception as e:
            logger.debug("watcher file fact sync failed %s: %r", file_path, e)
    except Exception as e:
        # Warning, not debug: a swallowed sync failure means the memory
        # silently drifts from the code, which is the bug this watcher exists
        # to prevent.
        logger.warning("watcher _sync_file failed %s: %r", file_path, e)

class _EventShim:
    """Minimal watchdog-event stand-in for internal re-dispatch.

    on_moved re-syncs the destination through on_modified, which only
    reads src_path / is_directory — no watchdog import needed.
    """
    def __init__(self, src_path, is_directory=False):
        self.src_path = src_path
        self.is_directory = is_directory


class _Handler(FileSystemEventHandler):
    def __init__(self, root: Path, store: MemoryStore):
        self.root = root
        self.store = store
        self._last_sync = {}
        self._pending = {}
        self._lock = threading.Lock()

    def on_modified(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if not _should_watch(p, self.root):
            return
        now = time.time()
        key = str(p)
        with self._lock:
            elapsed = now - self._last_sync.get(key, 0)
            if elapsed < 2.0:
                # Inside the debounce window this is either the tail of the same
                # save burst or a real follow-up edit. Schedule one trailing sync
                # just after the window closes so a genuine quick second edit is
                # never dropped — a missed sync is silent memory drift.
                if not self._pending.get(key):
                    self._pending[key] = True
                    t = threading.Timer(2.0 - elapsed + 0.1, self._trailing_sync, args=(p, key))
                    t.daemon = True
                    t.start()
                return
            self._last_sync[key] = now
        try:
            rel = p.relative_to(self.root)
        except ValueError:
            return
        logger.info("watcher detected change: %s", rel)
        _sync_file(p, self.root, self.store)

    def _trailing_sync(self, p: Path, key: str):
        with self._lock:
            self._pending.pop(key, None)
            self._last_sync[key] = time.time()
        try:
            rel = p.relative_to(self.root)
        except ValueError:
            return
        logger.info("watcher trailing sync: %s", rel)
        _sync_file(p, self.root, self.store)

    def on_created(self, event):
        self.on_modified(event)

    def _prune_event_path(self, src_path):
        """Normalize a deleted/moved-away path; None when out of scope.

        No _should_watch gate: the file is gone, so stat() fails and only
        the extension + ignore-dir parts can apply.
        """
        p = Path(src_path)
        try:
            rel = p.relative_to(self.root).as_posix()
        except ValueError:
            return None
        if p.suffix.lower() not in WATCHED_EXTS:
            return None
        if any(part in IGNORE_DIRS or part.endswith(".egg-info")
               for part in Path(rel).parts):
            return None
        return rel

    def on_deleted(self, event):
        if event.is_directory:
            return
        rel = self._prune_event_path(event.src_path)
        if rel is None:
            return
        logger.info("watcher detected deletion: %s", rel)
        _prune_deleted_file(rel, self.store)

    def on_moved(self, event):
        if event.is_directory:
            return
        # Old path: prune rows exactly like a deletion.
        rel = self._prune_event_path(event.src_path)
        if rel is not None:
            logger.info("watcher detected move-away: %s", rel)
            _prune_deleted_file(rel, self.store)
        # New path: sync as if newly created. Watchdog delivers a rename
        # as a single moved event (no separate created event follows), so
        # without this the destination would never be indexed. Re-dispatched
        # through on_modified so the full watch gate and debounce apply.
        dest = getattr(event, "dest_path", "") or ""
        if dest:
            self.on_modified(_EventShim(dest))

def start_watcher(project_root: Optional[Path] = None, store: Optional[MemoryStore] = None) -> Optional[object]:
    """Start daemon watcher thread. Returns Observer or polling thread, or None if failed."""
    try:
        root = Path(project_root) if project_root else Path(store.db_path).parent.parent if store and hasattr(store, 'db_path') else Path.cwd()
        root = root.resolve()
        if not root.is_dir():
            logger.warning("watcher: root does not exist %s", root)
            return None
        store = store or MemoryStore()
        if _WATCHDOG_AVAILABLE:
            handler = _Handler(root, store)
            observer = Observer()
            observer.schedule(handler, str(root), recursive=True)
            observer.daemon = True
            observer.start()
            logger.info("watcher started (watchdog) for %s", root)
            return observer
        else:
            # Polling fallback every 30s
            import threading
            def poll_loop():
                seen = {}
                first_scan = True
                while True:
                    try:
                        for ext in WATCHED_EXTS:
                            try:
                                candidates = list(root.rglob(f"*{ext}"))
                            except OSError as e:
                                logger.debug("watcher poll rglob failed %s: %r", ext, e)
                                continue
                            for p in candidates:
                                try:
                                    if not _should_watch(p, root):
                                        if not p.exists():
                                            seen.pop(str(p), None)
                                        continue
                                    mtime = p.stat().st_mtime
                                except (OSError, FileNotFoundError) as e:
                                    logger.debug("watcher poll stat failed %s: %r", p, e)
                                    seen.pop(str(p), None)
                                    continue
                                key = str(p)
                                if key not in seen or seen[key] != mtime:
                                    if not first_scan:
                                        _sync_file(p, root, store)
                                    seen[key] = mtime
                        # Evict deleted paths so `seen` stays bounded, and prune
                        # their structural rows (same as on_deleted: sync never
                        # revisits a path with nothing on disk).
                        for key in list(seen.keys()):
                            try:
                                if not Path(key).exists():
                                    seen.pop(key, None)
                                    try:
                                        rel = Path(key).relative_to(root).as_posix()
                                    except ValueError:
                                        continue
                                    _prune_deleted_file(rel, store)
                            except OSError:
                                seen.pop(key, None)
                        first_scan = False
                        time.sleep(30)
                    except Exception as e:
                        logger.debug("watcher poll error: %r", e)
                        time.sleep(30)
            t = threading.Thread(target=poll_loop, daemon=True)
            t.start()
            logger.info("watcher started (polling) for %s", root)
            return t
    except Exception as e:
        logger.warning("watcher failed to start: %r", e)
        return None


def stop_watcher(handle: Optional[object] = None, db_path: Optional[str] = None) -> None:
    """Stop a watcher started by start_watcher and release its DB handles.

    Observer handles are stopped/joined; polling threads are daemons and
    need no join. Per-thread cached stores are always closed so fd/WAL
    handles don't leak across restarts (e.g. tests creating many watchers).
    """
    if handle is not None:
        try:
            stop = getattr(handle, "stop", None)
            if callable(stop):
                stop()
            join = getattr(handle, "join", None)
            if callable(join):
                join(timeout=5)
        except Exception as e:
            logger.debug("watcher stop failed: %r", e)
    close_thread_stores(db_path)
