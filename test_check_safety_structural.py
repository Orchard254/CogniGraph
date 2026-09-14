"""check_safety structural union (#499): memory_edges + structural_edges.

Run with: python -m pytest test_check_safety_structural.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_store import MemoryStore


def _mk(tmp_path):
    db = str(tmp_path / "safety.db")
    return MemoryStore(db)


def _node(store, node_type, name, file_path, line=1, class_name=""):
    cur = store.conn.execute(
        "INSERT INTO structural_nodes (node_type, name, class_name, file_path, "
        "line_start, line_end, created_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        (node_type, name, class_name, file_path, line, line),
    )
    store.conn.commit()
    return cur.lastrowid


def _edge(store, caller_id, callee_id, edge_type="calls"):
    store.conn.execute(
        "INSERT OR IGNORE INTO structural_edges (caller_id, callee_id, edge_type, created_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        (caller_id, callee_id, edge_type),
    )
    store.conn.commit()


def test_structural_union_found(tmp_path):
    s = _mk(tmp_path)
    try:
        a = _node(s, "function", "alpha", "src/a.py", 10)
        b = _node(s, "function", "beta", "src/b.py", 20)
        _edge(s, a, b, "calls")
        out = s.check_safety(["src/a.py", "src/b.py"])
        assert out["status"] == "CHECKED"
        assert out["cross_dependencies"] == []  # no memory_edges
        assert out["structural_dependencies_count"] == 1
        dep = out["structural_dependencies"][0]
        assert dep["edge_type"] == "calls"
        assert dep["caller_file"] == "src/a.py"
        assert dep["callee_file"] == "src/b.py"
        assert dep["caller_symbol"] == "alpha"
        assert dep["callee_symbol"] == "beta"
    finally:
        s.close()


def test_structural_imports_and_same_file_excluded(tmp_path):
    s = _mk(tmp_path)
    try:
        a = _node(s, "function", "alpha", "src/a.py", 10)
        b = _node(s, "function", "beta", "src/b.py", 20)
        c = _node(s, "function", "gamma", "src/c.py", 30)
        _edge(s, a, b, "imports")
        _edge(s, a, c, "calls")  # c.py not in batch -> excluded
        out = s.check_safety(["src/a.py", "src/b.py"])
        assert out["structural_dependencies_count"] == 1
        assert out["structural_dependencies"][0]["edge_type"] == "imports"
    finally:
        s.close()


def test_empty_and_compat(tmp_path):
    s = _mk(tmp_path)
    try:
        out = s.check_safety(["src/a.py", "src/b.py"])
        assert out["status"] == "CHECKED"
        assert out["cross_dependencies_count"] == 0
        assert out["structural_dependencies_count"] == 0
        assert out["known_limitations"]
        empty = s.check_safety([])
        assert empty["status"] == "NO_FILES_PROVIDED"
    finally:
        s.close()
