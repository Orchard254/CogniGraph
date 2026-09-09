import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_common import default_db_path


def main():
    p = argparse.ArgumentParser(description="Inspect a project-memory SQLite DB (read-only).")
    p.add_argument("--db", default=None,
                   help="DB path (default: MEMORY_DB_PATH env or project default; never hardcoded)")
    args = p.parse_args()
    db = args.db or os.environ.get("MEMORY_DB_PATH") or default_db_path()
    print('db:', db)
    print('db exists:', os.path.exists(db))
    if not os.path.exists(db):
        raise SystemExit(0)
    st = os.stat(db)
    print('size_bytes:', st.st_size)
    print('mtime:', time.ctime(st.st_mtime))
    conn = sqlite3.connect(db)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' OR type='view' ORDER BY name")
        print('tables:')
        for r in cur.fetchall():
            print(' ', r[0])
        # memory count
        try:
            cur.execute('SELECT count(*) FROM memory')
            print('memory rows:', cur.fetchone()[0])
        except Exception as e:
            print('memory count error:', e)
        # memory_embeddings count
        try:
            cur.execute('SELECT count(*) FROM memory_embeddings')
            print('memory_embeddings rows:', cur.fetchone()[0])
        except Exception as e:
            print('memory_embeddings count error:', e)
        # recent rows
        try:
            cur.execute('PRAGMA table_info(memory)')
            cols = [c[1] for c in cur.fetchall()]
            print('memory cols:', cols)
            if 'created_at' in cols:
                cur.execute('SELECT id, category, created_at FROM memory ORDER BY id DESC LIMIT 5')
                for r in cur.fetchall():
                    print(' recent:', r)
        except Exception as e:
            print('recent rows error:', e)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
