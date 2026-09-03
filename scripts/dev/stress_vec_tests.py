"""
Stress tests for sqlite-vec integration

1) Metric verification for three controlled cosine similarities
2) Check whether vec0 requires `k = ?` in WHERE for indexed KNN
3) Performance benchmarks at scales: 500, 2000, 5000, 20000
4) Side-by-side brute vs vec similarity check for same/related/unrelated

This script prints raw outputs for manual analysis.
"""
import sys
from pathlib import Path
# Ensure project root is importable when script is run from scripts/dev
proj_root = str(Path(__file__).resolve().parents[2])
if proj_root not in sys.path:
    sys.path.insert(0, proj_root)

import sqlite3
import time
import tempfile
import os
import math
import random
import struct
import statistics

import sqlite_vec
import numpy as np

# Import helpers from memory_store to ensure same binary format
import memory_store
from memory_store import MemoryStore, _vec_to_blob, _blob_to_vec, _encode

# Utility: create a random unit vector (float32)
def rand_unit(dim=384, seed=None):
    if seed is not None:
        rng = np.random.RandomState(seed)
    else:
        rng = np.random
    v = rng.normal(size=(dim,)).astype('float32')
    v /= np.linalg.norm(v)
    return v

# Utility: make vector b with desired cosine c relative to a
# b = c*a + sqrt(1-c^2)*u where u is unit orthogonal to a
def make_with_cosine(a, cosine_target):
    dim = a.shape[0]
    # random vector
    r = np.random.normal(size=(dim,)).astype('float32')
    # orthogonalize
    r = r - (r.dot(a)) * a
    norm_r = np.linalg.norm(r)
    if norm_r < 1e-6:
        r = rand_unit(dim)
        r = r - (r.dot(a)) * a
        norm_r = np.linalg.norm(r)
    u = r / norm_r
    c = float(cosine_target)
    s = math.sqrt(max(0.0, 1.0 - c * c))
    b = c * a + s * u
    b = b.astype('float32')
    b /= np.linalg.norm(b)
    return b

# Pack into project's blob format
def pack(v):
    return _vec_to_blob(v)

# Metric test for given cosine targets
def metric_test(conn, cosine_targets):
    cur = conn.cursor()
    cur.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_memory USING vec0(embedding float[384])")
    conn.commit()
    out_lines = []
    for i, c in enumerate(cosine_targets, start=1):
        a = rand_unit(384, seed=100+i)
        b = make_with_cosine(a, c)
        # compute numpy cosine
        np_cos = float(np.dot(a, b))
        # pack and insert
        blob_a = pack(a)
        blob_b = pack(b)
        # use rowid 1000+i and 2000+i to avoid collisions
        rid_a = 1000 + i
        rid_b = 2000 + i
        cur.execute('INSERT OR REPLACE INTO vec_memory(rowid, embedding) VALUES (?, ?)', (rid_a, blob_a))
        cur.execute('INSERT OR REPLACE INTO vec_memory(rowid, embedding) VALUES (?, ?)', (rid_b, blob_b))
        conn.commit()
        # query: MATCH b against the table, expect to find a
        try:
            res = cur.execute('SELECT rowid, distance FROM vec_memory WHERE embedding MATCH ? ORDER BY distance LIMIT 10', (blob_b,)).fetchall()
        except Exception as e:
            res = [('ERROR', str(e))]
        # find distance for rid_a in results
        found = None
        for row in res:
            if isinstance(row[0], int) and row[0] == rid_a:
                found = row
                break
        # If not found in top-10, fetch full scan  to find exact distance to rid_a
        if found is None:
            try:
                # query direct distance by matching b but ordering all
                res_all = cur.execute('SELECT rowid, distance FROM vec_memory WHERE embedding MATCH ? ORDER BY distance', (blob_b,)).fetchall()
                for row in res_all:
                    if isinstance(row[0], int) and row[0] == rid_a:
                        found = row
                        break
            except Exception:
                pass
        out_lines.append((i, c, np_cos, res, found))
    return out_lines

# Test whether `k = ?` in WHERE affects plan/perf. We'll create N rows and run both queries multiple times.
def k_constraint_test(conn, N=5000, repeat=3):
    cur = conn.cursor()
    cur.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_memory USING vec0(embedding float[384])")
    conn.commit()
    # populate with random vectors
    cur.execute('DELETE FROM vec_memory')
    blobs = []
    for i in range(N):
        v = rand_unit(384, seed=1000 + (i % 1000))
        blobs.append(pack(v))
    for i, b in enumerate(blobs, start=1):
        cur.execute('INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)', (i, b))
    conn.commit()
    # pick a query vector
    q = rand_unit(384, seed=42)
    q_blob = pack(q)
    timings = {'limit_only': [], 'k_and_limit': [], 'k_only': []}
    # run queries
    for r in range(repeat):
        t0 = time.perf_counter()
        cur.execute('SELECT rowid, distance FROM vec_memory WHERE embedding MATCH ? ORDER BY distance LIMIT 10', (q_blob,)).fetchall()
        t1 = time.perf_counter()
        timings['limit_only'].append((t1-t0))

        # try k = ? syntax if supported
        try:
            t0 = time.perf_counter()
            cur.execute('SELECT rowid, distance FROM vec_memory WHERE embedding MATCH ? AND k = ? ORDER BY distance LIMIT 10', (q_blob, 10)).fetchall()
            t1 = time.perf_counter()
            timings['k_and_limit'].append((t1-t0))
        except Exception as e:
            timings['k_and_limit'].append(('ERROR', str(e)))

    return timings

# Performance benchmark across scales
def perf_bench(db_dir, scales=(500,2000,5000,20000)):
    results = []
    for N in scales:
        db_path = os.path.join(db_dir, f'bench_{N}.db')
        if os.path.exists(db_path):
            os.remove(db_path)
        store = MemoryStore(db_path)
        conn = store.conn
        cur = conn.cursor()
        # clear any existing vec_memory and memory_embeddings
        try:
            cur.execute('DELETE FROM vec_memory')
        except Exception:
            pass
        cur.execute('DELETE FROM memory_embeddings')
        conn.commit()
        # Bulk insert N random embeddings directly into memory_embeddings and vec_memory
        blobs = []
        for i in range(N):
            v = rand_unit(384, seed=2000 + (i % 1000))
            blobs.append(pack(v))
        # Ensure memory rows exist for foreign key constraint (insert minimal required fields)
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        cur.executemany('INSERT OR REPLACE INTO memory (id, category, content, created_at, confidence) VALUES (?, ?, ?, ?, ?)',
                                ((i+1, 'component', f'auto {i+1}', now, 'assumed') for i in range(N)))
        conn.commit()
        # insert using executemany for memory_embeddings (fact_id 1..N)
        cur.executemany('INSERT OR REPLACE INTO memory_embeddings (fact_id, model, embedding) VALUES (?, ?, ?)',
                                ((i+1, memory_store._EMBED_MODEL_NAME, blobs[i]) for i in range(N)))
        conn.commit()
        # ensure vec_memory synced: if triggers not available, insert explicitly
        try:
            cur.executemany('INSERT OR REPLACE INTO vec_memory(rowid, embedding) VALUES (?, ?)', ((i+1, blobs[i]) for i in range(N)))
            conn.commit()
        except Exception:
            # ignore
            pass
            # diagnostic counts
            try:
                vc = cur.execute('SELECT COUNT(*) as c FROM vec_memory').fetchone()
                me = cur.execute('SELECT COUNT(*) as c FROM memory_embeddings').fetchone()
                print(f'Inserted N={N}: vec_memory_count={vc[0] if vc else vc}, memory_embeddings_count={me[0] if me else me}')
            except Exception:
                pass
            # choose a random query from inserted items
        q_idx = max(1, N//7)
        q_blob = blobs[q_idx]
        # instantiate store if not already
        # Measure brute (MemoryStore._semantic_scores)
        t0 = time.perf_counter()
        brute = store._semantic_scores(_blob_to_vec(q_blob))
        t1 = time.perf_counter()
        brute_ms = (t1 - t0) * 1000.0
        # Measure vec accelerated
        t2 = time.perf_counter()
        vec = store._semantic_scores_vec(_blob_to_vec(q_blob))
        t3 = time.perf_counter()
        vec_ms = (t3 - t2) * 1000.0
        results.append((N, brute_ms, vec_ms, len(brute), len(vec)))
        store.close()
    return results

# Side-by-side sanity check for same/related/unrelated
def side_by_side_sanity(db_dir):
    db_path = os.path.join(db_dir, 'sanity.db')
    if os.path.exists(db_path):
        os.remove(db_path)
    store = MemoryStore(db_path)
    conn = store.conn
    # three sentences
    a = "Refactor the database connection pooling for better throughput."
    b = "Improve database connection pool to increase throughput and reduce latency."
    c = "Today the park had many trees and the wind felt cool on my face."
    # Insert embeddings directly into memory_embeddings
    # Use deterministic seeds to produce repeatable vectors
    va = _encode(a)
    vb = _encode(b)
    vc = _encode(c)
    blobs = [pack(va), pack(vb), pack(vc)]
    cur = conn.cursor()
    for i, blob in enumerate(blobs, start=1):
        # ensure a memory row exists to satisfy FK
        cur.execute('INSERT OR REPLACE INTO memory (id, category, content, created_at, confidence) VALUES (?, ?, ?, ?, ?)', (i, 'component', f'auto {i}', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'assumed'))
        cur.execute('INSERT OR REPLACE INTO memory_embeddings(fact_id, model, embedding) VALUES (?, ?, ?)', (i, memory_store._EMBED_MODEL_NAME, blob))
    conn.commit()
    # Ensure vec_memory populated
    try:
        cur.executemany('INSERT OR REPLACE INTO vec_memory(rowid, embedding) VALUES (?, ?)', ((i+1, blobs[i]) for i in range(len(blobs))))
        conn.commit()
    except Exception:
        pass
    q = _encode(a)
    brute = store._semantic_scores(q)
    vec = store._semantic_scores_vec(q)
    store.close()
    return brute, vec

# Main execution
if __name__ == '__main__':
    out = []
    # 1) Metric tests
    print('=== METRIC TESTS ===')
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    except Exception as e:
        print('Failed to load sqlite_vec in metric test:', e)
    conn.enable_load_extension(False)
    cosine_targets = [0.98, 0.5, -0.2]
    metric_results = metric_test(conn, cosine_targets)
    for (i, target_c, np_cos, res, found) in metric_results:
        print(f'PAIR #{i} target_cosine={target_c}')
        print(' numpy_cosine:', np_cos)
        print(' raw vec0 query results (top rows):')
        for r in res[:10]:
            print('  ', r)
        if found:
            # compute reconstructed similarity
            dist = float(found[1])
            recon = 1.0 - (dist * dist) / 2.0
            print(' found row for a (if present):', found)
            print(' reconstructed_cosine_from_distance:', recon)
        else:
            print(' found row for a (if present):', found)
        print('---')
    conn.close()
    os.unlink(tmp.name)

    # 2) k constraint test
    print('\n=== K-CONSTRAINT TEST ===')
    tmp2 = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    tmp2.close()
    conn2 = sqlite3.connect(tmp2.name)
    conn2.enable_load_extension(True)
    try:
        sqlite_vec.load(conn2)
    except Exception as e:
        print('Failed to load sqlite_vec in k-constraint test:', e)
    conn2.enable_load_extension(False)
    timings = k_constraint_test(conn2, N=3000, repeat=3)
    print('timings for 3000 rows (seconds):')
    print(timings)
    # additionally test k-only syntax (no LIMIT)
    try:
        q = rand_unit(384, seed=999)
        q_blob = pack(q)
        t0 = time.perf_counter()
        res_k_only = conn2.execute('SELECT rowid, distance FROM vec_memory WHERE embedding MATCH ? AND k = ?', (q_blob, 10)).fetchall()
        t1 = time.perf_counter()
        print('k-only query succeeded, time:', t1-t0, 'rows returned:', len(res_k_only))
        print('sample rows:', res_k_only[:5])
    except Exception as e:
        print('k-only query error:', e)
    conn2.close()
    os.unlink(tmp2.name)

    # 3) Performance benchmark
    print('\n=== PERFORMANCE BENCHMARK ===')
    tmpdir = tempfile.mkdtemp()
    try:
        perf = perf_bench(tmpdir, scales=(500,2000,5000,8000,10000,20000))
        print('\nrows, brute_ms, vec_ms, brute_count, vec_count')
        for row in perf:
            print(row)
            # additional diagnostic: for the DB corresponding to this scale, open and run manual vec query
            N = row[0]
            db_path = os.path.join(tmpdir, f'bench_{N}.db')
            try:
                connd = sqlite3.connect(db_path)
                connd.enable_load_extension(True)
                try:
                    sqlite_vec.load(connd)
                except Exception:
                    pass
                connd.enable_load_extension(False)
                curd = connd.cursor()
                # pick one of the inserted vectors as query
                curd.execute('SELECT rowid, embedding FROM vec_memory LIMIT 1')
                first = curd.fetchone()
                if first:
                    q_blob = first[1]
                    try:
                        t0 = time.perf_counter()
                        rows_sql = curd.execute('SELECT rowid, distance FROM vec_memory WHERE embedding MATCH ? ORDER BY distance LIMIT 10', (q_blob,)).fetchall()
                        t1 = time.perf_counter()
                        print(f'DB {N}: manual vec query time {t1-t0}, rows: {len(rows_sql)}')
                        print(' sample rows:', rows_sql[:5])
                    except Exception as e:
                        print(f'DB {N}: manual vec query error:', e)
                connd.close()
            except Exception as e:
                print('Diagnostic manual query error for DB', db_path, e)
    finally:
        # keep DBs for inspection but print path
        print('Benchmark DBs stored in:', tmpdir)

    # 4) Side-by-side sanity check
    print('\n=== SIDE-BY-SIDE SANITY CHECK ===')
    brute_map, vec_map = side_by_side_sanity(tmpdir)
    print('Brute-force scores:')
    print(brute_map)
    print('Vec-accelerated scores:')
    print(vec_map)
    print('\nDONE')
