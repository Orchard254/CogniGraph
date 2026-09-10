"""Timestamped full-DB backup with retention pruning.

The corpus-hygiene incident showed that the ad-hoc .bak files taken
before each historical phase all predated the destroyed content, so
recovery found nothing. A routine, timestamped full-DB snapshot is the
durable habit: weekly maintenance (server.py _auto_maintenance) takes
one automatically, and every migration script (corpus_hygiene,
genesis_migrate) snapshots before mutating the database.

The snapshot uses the SQLite backup API, which is WAL-safe — a live
server writing to the source cannot produce a torn copy. Each backup
is integrity-checked and older ones beyond the retention window are
pruned (default: keep 8).
"""
import datetime
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict

DEFAULT_KEEP = 8
_TAG_RE = re.compile(r"[^A-Za-z0-9_-]")


def timestamped_backup(db_path: str | Path, tag: str = "",
                       keep: int = DEFAULT_KEEP) -> Dict[str, Any]:
    """Snapshot db_path to <dir>/memory.db.backup-<tag>-<ts>.bak via the
    SQLite backup API; verify the copy; prune old backups beyond `keep`.

    Returns {"backup_path", "bytes", "kept", "pruned"}. Raises
    ValueError if the source is missing/empty, or RuntimeError if the
    backup or its integrity check fails (a failed snapshot is removed).
    """
    src = Path(db_path)
    if not src.is_file() or src.stat().st_size == 0:
        raise ValueError(f"nothing to back up: {src} does not exist or is empty")
    tag_clean = _TAG_RE.sub("-", (tag or "routine").strip("-")) or "routine"
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = src.parent / f"{src.name}.backup-{tag_clean}-{ts}.bak"

    src_conn = sqlite3.connect("file:" + src.as_posix() + "?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(backup_path))
        try:
            src_conn.backup(dst)
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            dst.close()
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    finally:
        src_conn.close()

    check = sqlite3.connect("file:" + backup_path.as_posix() + "?mode=ro", uri=True)
    try:
        row = check.execute("PRAGMA integrity_check").fetchone()
    finally:
        check.close()
    if not row or str(row[0]).lower() != "ok":
        backup_path.unlink(missing_ok=True)
        raise RuntimeError(f"backup integrity check failed for {backup_path}")

    kept, pruned = _prune_backups(src.parent, src.name, keep=keep)
    return {"backup_path": str(backup_path), "bytes": backup_path.stat().st_size,
            "kept": len(kept), "pruned": [str(p) for p in pruned]}


def _prune_backups(directory: Path, db_name: str, keep: int = DEFAULT_KEEP):
    """Keep the newest `keep` timestamped backups; delete the rest.

    Only touches files matching memory.db.backup-<tag>-<ts>.bak in the
    same directory — historical ad-hoc *.bak files are never pruned.
    Returns (kept_paths_newest_first, pruned_paths_oldest_first).
    """
    pattern = f"{db_name}.backup-*"
    backups = sorted(directory.glob(pattern),
                     key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    keepers = backups[:max(1, keep)]
    removed = []
    for old in backups[max(1, keep):]:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            pass
    return keepers, removed