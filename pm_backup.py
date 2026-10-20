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


def _readonly_sqlite_uri(path: str | Path) -> str:
    """Build a read-only SQLite URI that safely encodes filesystem metacharacters."""
    return f"{Path(path).resolve().as_uri()}?mode=ro"


def _reserve_backup_path(directory: Path, db_name: str, tag: str, timestamp: str) -> Path:
    """Atomically reserve a unique backup filename, including under concurrent calls."""
    stem = f"{db_name}.backup-{tag}-{timestamp}"
    suffix = 0
    while True:
        candidate_name = f"{stem}{f'-{suffix}' if suffix else ''}.bak"
        candidate = directory / candidate_name
        try:
            # Exclusive creation avoids a check-then-create race between two
            # maintenance/migration callers snapshotting the same DB together.
            with candidate.open("xb"):
                pass
            return candidate
        except FileExistsError:
            suffix += 1


def timestamped_backup(db_path: str | Path, tag: str = "",
                       keep: int = DEFAULT_KEEP) -> Dict[str, Any]:
    """Snapshot db_path to a unique, timestamped <dir>/<db>.backup-<tag>-<ts>.bak.

    The SQLite backup API supplies a WAL-safe snapshot. A read-only URI avoids
    accidentally opening a different file when the path contains URI-reserved
    characters. The destination name includes microseconds and is reserved
    exclusively, so concurrent or repeated same-tag calls cannot overwrite an
    earlier recovery point. The copy is integrity-checked and old snapshots
    beyond `keep` are pruned.

    Returns {"backup_path", "bytes", "kept", "pruned"}. Raises ValueError if
    the source is missing/empty; a failed snapshot or integrity check removes
    only the newly reserved destination before raising.
    """
    src = Path(db_path)
    if not src.is_file() or src.stat().st_size == 0:
        raise ValueError(f"nothing to back up: {src} does not exist or is empty")
    tag_clean = _TAG_RE.sub("-", (tag or "routine").strip("-")) or "routine"
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    src_conn = sqlite3.connect(_readonly_sqlite_uri(src), uri=True)
    backup_path = None
    try:
        backup_path = _reserve_backup_path(src.parent, src.name, tag_clean, ts)
        dst = sqlite3.connect(str(backup_path))
        try:
            src_conn.backup(dst)
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            dst.close()

        check = sqlite3.connect(_readonly_sqlite_uri(backup_path), uri=True)
        try:
            row = check.execute("PRAGMA integrity_check").fetchone()
        finally:
            check.close()
        if not row or str(row[0]).lower() != "ok":
            raise RuntimeError(f"backup integrity check failed for {backup_path}")
    except Exception:
        if backup_path is not None:
            backup_path.unlink(missing_ok=True)
        raise
    finally:
        src_conn.close()

    # The snapshot is self-contained (copied via the backup API,
    # checkpointed, integrity-checked); drop any vestigial sidecars
    # SQLite left beside it so kept backups stay sidecar-free.
    for suffix in _SIDECAR_SUFFIXES:
        (backup_path.parent / (backup_path.name + suffix)).unlink(missing_ok=True)

    kept, pruned = _prune_backups(src.parent, src.name, keep=keep)
    return {"backup_path": str(backup_path), "bytes": backup_path.stat().st_size,
            "kept": len(kept), "pruned": [str(p) for p in pruned]}


_SIDECAR_SUFFIXES = ("-shm", "-wal", "-journal")


def _unlink_with_sidecars(backup: Path) -> list:
    """Unlink a `.bak` snapshot plus any SQLite sidecar siblings.

    Returns the list of paths actually removed (the `.bak` itself plus
    any `<name>.bak-shm/-wal/-journal` present). Missing files and
    unlink errors are skipped silently — retention pruning must never
    fail the backup that triggered it.
    """
    removed = []
    candidates = [backup] + [backup.parent / (backup.name + s)
                             for s in _SIDECAR_SUFFIXES]
    for p in candidates:
        try:
            if p.is_file():
                p.unlink()
                removed.append(p)
        except OSError:
            pass
    return removed


def _prune_backups(directory: Path, db_name: str, keep: int = DEFAULT_KEEP):
    """Keep the newest `keep` timestamped backups; delete the rest.

    Only touches files matching memory.db.backup-<tag>-<ts>.bak in the
    same directory — historical ad-hoc *.bak files are never pruned.
    Only real `.bak` snapshots compete for keep slots (sidecars used to
    match the glob and steal slots independently); each pruned snapshot
    takes its `-shm`/`-wal`/`-journal` siblings with it, and a final
    sweep removes dangling sidecars whose parent `.bak` is already gone
    (legacy orphans from before sibling-aware pruning).
    Returns (kept_paths_newest_first, pruned_paths_oldest_first).
    """
    # Match names literally rather than via glob syntax: database filenames may
    # legally contain wildcard characters such as '?' or '[' on POSIX.
    prefix = f"{db_name}.backup-"
    candidates = (p for p in directory.iterdir()
                  if p.name.startswith(prefix) and p.name.endswith(".bak"))
    backups = sorted(candidates, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    keepers = backups[:max(1, keep)]
    removed = []
    for old in backups[max(1, keep):]:
        removed.extend(_unlink_with_sidecars(old))
    for stray in directory.iterdir():
        if stray.name.startswith(prefix):
            for suffix in _SIDECAR_SUFFIXES:
                if not stray.name.endswith(".bak" + suffix):
                    continue
                parent = directory / stray.name[: -len(suffix)]
                if not parent.exists():
                    try:
                        stray.unlink()
                        removed.append(stray)
                    except OSError:
                        pass
                break
    return keepers, removed