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


def test_relative_vs_absolute_path_match(tmp_path):
    s = _mk(tmp_path)
    try:
        a = _node(s, "function", "alpha", str(tmp_path / "src" / "a.py"), 10)
        b = _node(s, "function", "beta", "src/b.py", 20)
        _edge(s, a, b, "calls")
        out = s.check_safety(["src/a.py", "src/b.py"])
        assert out["structural_dependencies_count"] == 1
    finally:
        s.close()


def test_absolute_batch_vs_relative_stored(tmp_path):
    s = _mk(tmp_path)
    try:
        a = _node(s, "function", "alpha", "src/a.py", 10)
        b = _node(s, "function", "beta", "src/b.py", 20)
        _edge(s, a, b, "calls")
        out = s.check_safety([str(tmp_path / "src" / "a.py").replace("\\\\", "/"),
                              str(tmp_path / "src" / "b.py").replace("\\\\", "/")])
        assert out["structural_dependencies_count"] == 1
    finally:
        s.close()


def test_trailing_slash_normalization(tmp_path):
    s = _mk(tmp_path)
    try:
        a = _node(s, "function", "alpha", "src/a.py", 10)
        b = _node(s, "function", "beta", "src/b.py", 20)
        _edge(s, a, b, "calls")
        out = s.check_safety(["src/a.py/", "src/b.py/"])
        assert out["structural_dependencies_count"] == 1
        # constraints apply too
        s.conn.execute(
            "INSERT INTO memory (category, content, file_path, created_at, deprecated_at) "
            "VALUES ('constraint', 'c', 'src/a.py/', datetime('now'), NULL)"
        )
        s.conn.commit()
        out2 = s.check_safety(["src/a.py", "src/b.py"])
        assert any(cc["file_path"] == "src/a.py/" for cc in out2["constraint_checks"])
    finally:
        s.close()


def test_case_normalization(tmp_path):
    s = _mk(tmp_path)
    try:
        a = _node(s, "function", "alpha", "SRC/A.PY", 10)
        b = _node(s, "function", "beta", "src/b.py", 20)
        _edge(s, a, b, "calls")
        out = s.check_safety(["src/a.py", "SRC/B.PY"])
        assert out["structural_dependencies_count"] == 1
    finally:
        s.close()


def test_no_false_match_a_py_vs_a_pyx(tmp_path):
    s = _mk(tmp_path)
    try:
        a = _node(s, "function", "alpha", "src/a.pyx", 10)
        b = _node(s, "function", "beta", "src/b.py", 20)
        _edge(s, a, b, "calls")
        out = s.check_safety(["src/a.py", "src/b.py"])
        assert out["structural_dependencies_count"] == 0
    finally:
        s.close()


def test_affected_direct_dependent(tmp_path):
    s = _mk(tmp_path)
    try:
        mod = _node(s, "function", "f", "src/mod.py", 1)
        test = _node(s, "function", "test_f", "tests/test_mod.py", 1)
        _edge(s, test, mod, "imports")
        out = s.check_safety(["src/mod.py"])
        affected = out["affected_tests"]
        paths = [a["test_file"] for a in affected]
        assert "tests/test_mod.py" in paths
        entry = next(a for a in affected if a["test_file"] == "tests/test_mod.py")
        assert entry["depth"] == 1
        assert entry["direct"] is True
    finally:
        s.close()


def test_affected_indirect_dependent(tmp_path):
    s = _mk(tmp_path)
    try:
        mod = _node(s, "function", "f", "src/mod.py", 1)
        mid = _node(s, "function", "mid", "src/mid.py", 1)
        test = _node(s, "function", "test_f", "tests/test_mod.py", 1)
        _edge(s, mid, mod, "calls")
        _edge(s, test, mid, "imports")
        out = s.check_safety(["src/mod.py"])
        affected = out["affected_tests"]
        paths = [a["test_file"] for a in affected]
        assert "tests/test_mod.py" in paths
        entry = next(a for a in affected if a["test_file"] == "tests/test_mod.py")
        assert entry["depth"] == 2
        assert entry["direct"] is False
    finally:
        s.close()


def test_affected_direct_batch_test(tmp_path):
    s = _mk(tmp_path)
    try:
        out = s.check_safety(["tests/test_mod.py"])
        affected = out["affected_tests"]
        entry = next(a for a in affected if a["test_file"] == "tests/test_mod.py")
        assert entry["depth"] == 0
        assert entry["direct"] is True
    finally:
        s.close()


def _chain(store, paths):
    """Build caller->callee edges paths[i] -> paths[i-1] (paths[0] is the seed).

    Reverse BFS from paths[0] then reaches paths[i] at depth i.
    """
    ids = [_node(store, "function", f"f{i}", p, 1) for i, p in enumerate(paths)]
    for callee_id, caller_id in zip(ids, ids[1:]):
        _edge(store, caller_id, callee_id, "calls")
    return ids


def test_affected_bfs_beyond_six_hops_truncated(tmp_path):
    s = _mk(tmp_path)
    try:
        # seed at depth 0; h6 is depth 6 (included); h7 depth 7 and the
        # test at depth 8 are beyond the bound and must be omitted.
        paths = ["src/mod.py"] + [f"src/h{i}.py" for i in range(1, 8)] + ["tests/test_deep.py"]
        ids = _chain(s, paths)
        # A second test exactly at depth 6 (caller of h5) must be included.
        at_limit = _node(s, "function", "test_at_limit", "tests/test_at_limit.py", 1)
        _edge(s, at_limit, ids[5], "calls")  # ids[5] == src/h5.py, caller depth 6
        out = s.check_safety(["src/mod.py"])
        assert out["status"] == "CHECKED"
        assert out["affected_tests_max_depth"] == 6
        assert out["affected_tests_truncated"] is True
        assert out["affected_tests_truncated_at_depth"] == 6
        assert out["affected_tests_beyond_limit_frontier"] == ["src/h7.py"]
        affected_paths = [a["test_file"] for a in out["affected_tests"]]
        assert "tests/test_at_limit.py" in affected_paths
        assert "tests/test_deep.py" not in affected_paths
        entry = next(a for a in out["affected_tests"] if a["test_file"] == "tests/test_at_limit.py")
        assert entry["depth"] == 6
        assert entry["direct"] is False
        assert "tests/test_deep.py" not in out["likely_relevant_tests"]
        assert any("6 hops" in lim for lim in out["known_limitations"])
    finally:
        s.close()


def test_affected_bfs_not_truncated_when_chain_ends_at_six(tmp_path):
    s = _mk(tmp_path)
    try:
        # Chain ending exactly at depth 6: bound is reached, nothing cut off.
        paths = ["src/mod.py"] + [f"src/h{i}.py" for i in range(1, 6)] + ["tests/test_at_six.py"]
        _chain(s, paths)
        out = s.check_safety(["src/mod.py"])
        entry = next(a for a in out["affected_tests"] if a["test_file"] == "tests/test_at_six.py")
        assert entry["depth"] == 6
        assert out["affected_tests_max_depth"] == 6
        assert out["affected_tests_truncated"] is False
        assert out["affected_tests_truncated_at_depth"] is None
        assert out["affected_tests_beyond_limit_frontier"] == []
        assert any("6 hops" in lim for lim in out["known_limitations"])
    finally:
        s.close()


def test_affected_bfs_short_chain_not_truncated(tmp_path):
    s = _mk(tmp_path)
    try:
        paths = ["src/mod.py", "src/mid.py", "tests/test_mod.py"]
        _chain(s, paths)
        out = s.check_safety(["src/mod.py"])
        assert out["affected_tests_truncated"] is False
        assert out["affected_tests_truncated_at_depth"] is None
        assert out["affected_tests_beyond_limit_frontier"] == []
        assert out["likely_relevant_tests"] == ["tests/test_mod.py"]
    finally:
        s.close()


def test_affected_cycle_terminates(tmp_path):
    s = _mk(tmp_path)
    try:
        a = _node(s, "function", "a", "src/a.py", 1)
        b = _node(s, "function", "b", "src/b.py", 1)
        _edge(s, a, b, "calls")
        _edge(s, b, a, "calls")
        out = s.check_safety(["src/a.py"])
        assert out["status"] == "CHECKED"
    finally:
        s.close()


# --- DB-derived project-root helper (consolidation) ---

def test_project_root_from_db_derives_grandparent(tmp_path):
    db = tmp_path / "a" / "memory.db"
    s = MemoryStore(str(db))
    try:
        assert s._project_root_from_db() == str(tmp_path.resolve())
    finally:
        s.close()


def test_project_root_from_db_missing_dir_returns_none(tmp_path):
    s = _mk(tmp_path)
    try:
        s.db_path = str(tmp_path / "missing" / "inner" / "memory.db")
        assert s._project_root_from_db() is None
        assert s._read_project_root(None) is None
    finally:
        s.close()


def test_read_project_root_prefers_explicit_and_falls_back(tmp_path):
    s = _mk(tmp_path)
    try:
        derived = s._read_project_root(None)
        assert derived is not None
        assert s._read_project_root("/explicit/root") == "/explicit/root"
        assert s._read_project_root("") == derived
    finally:
        s.close()


def test_blast_unresolved_derived_root_vs_explicit_empty(tmp_path):
    db = tmp_path / "a" / "memory.db"
    s = MemoryStore(str(db))
    try:
        est, skipped = s._blast_unresolved([], {}, None)
        assert est == 0
        assert skipped == []
        # Explicit empty string must not trigger derivation (historical quirk).
        est2, skipped2 = s._blast_unresolved([], {}, "")
        assert est2 == 0
        assert skipped2 == ["project root unavailable — estimate skipped"]
    finally:
        s.close()
