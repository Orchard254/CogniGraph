"""Local integration-test fixture: no host or model, real CogniGraph SQLite.

This file is invoked only by test-js/opencode-real-bridge.test.js.
It creates one controlled source tree and may read accounting afterwards.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from memory_store import MemoryStore


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[2] not in {"seed-fact", "seed-unknown", "stats"}:
        raise SystemExit("expected PROJECT_ROOT {seed-fact|seed-unknown|stats}")
    root = Path(sys.argv[1]).resolve()
    mode = sys.argv[2]
    db = root / ".cognigraph" / "memory.db"
    if mode != "stats":
        source = root / "src" / "session.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        original = b"def restore_session():\n    return True\n"
        source.write_bytes(original)

    store = MemoryStore(str(db))
    try:
        if mode == "seed-fact":
            ident = store.write(
                "component", "restore_session restores the user login session after reload",
                file_path="src/session.py", line_start=1, line_end=2,
                code_hash=hashlib.sha256(original).hexdigest(),
                confidence="confirmed", force=True, embed=False,
            )
            store.conn.commit()
            print(json.dumps({"fact_id": ident}))
        elif mode == "seed-unknown":
            stamp = "2026-11-06T00:00:00+00:00"
            store.conn.execute(
                """INSERT INTO structural_nodes
                   (node_type, name, class_name, file_path, line_start, line_end,
                    created_at, last_seen_at)
                   VALUES ('function', 'restore_session', '', 'src/session.py',
                           1, 2, ?, ?)""",
                (stamp, stamp),
            )
            store.conn.commit()
            print(json.dumps({"seeded": "index_only"}))
        else:
            receipts = store.conn.execute(
                "SELECT id, retrieved_fact_ids FROM memory_usage_log ORDER BY id"
            ).fetchall()
            print(json.dumps({
                "usage_count": len(receipts),
                "receipt_ids": [row["id"] for row in receipts],
                "facts_in_receipts": [row["retrieved_fact_ids"] for row in receipts],
            }))
    finally:
        store.close()


if __name__ == "__main__":
    main()
