"""read_code must return the provenance code_hash for the span it resolved.

Why: memory_write accepts file_path + line_start/line_end + code_hash, and
verify_provenance recomputes the hash as

    sha256("".join(file_lines[line_start - 1 : line_end]).encode("utf-8"))

with splitlines(keepends=True), so trailing newlines are part of the digest.
An agent recomputing that by hand has to reproduce the line-ending convention
exactly, and nothing in the API helps it. That friction is why only 25 of 433
active facts are linked to code nodes: 100 anchored facts carry a file_path but
no line range, so the linker has nothing to match on.

An agent that reads the code it is about to write a fact about should get the
hash in the same call, guaranteed consistent with the text it was handed. This
adds it to memory_read_code rather than introducing a new tool.

The hash covers the FULL actual span, not the possibly-truncated returned text,
because that is the span verify_provenance will re-hash later.

Run with: python -m pytest test_read_code_hash.py -v
"""
import hashlib
import os
import tempfile
from pathlib import Path

import pytest

from memory_store import MemoryStore


@pytest.fixture
def project():
    root = Path(tempfile.mkdtemp())
    (root / ".git").mkdir()
    src = root / "svc.py"
    src.write_text(
        "import os\n"          # 1
        "\n"                   # 2
        "def alpha():\n"       # 3
        "    return 1\n"       # 4
        "\n"                   # 5
        "def beta():\n"        # 6
        "    return 2\n",      # 7
        encoding="utf-8")
    store = MemoryStore(db_path=str(root / ".cognigraph" / "memory.db"))
    store._current_git_commit = lambda: None
    # Symbol resolution reads the structural index, so index the project the
    # way bootstrap/watcher would. Without this every symbol lookup returns
    # symbol_not_found and the provenance path is untestable.
    from structural_extract import extract_structures
    nodes = extract_structures(str(root / "svc.py"))
    # Mirror watcher.py:130-131 exactly -- a RELATIVE file_path plus
    # project_root. extract_structures stamps absolute paths onto the nodes it
    # returns, and sync_structural_nodes re-keys them from its own file_path
    # argument, so passing an absolute path here stores absolute paths that
    # read_code (which resolves relative to project_root) can never match.
    store.sync_structural_nodes("svc.py", nodes, project_root=str(root))
    yield root, store
    try:
        store.close()
    except Exception:
        pass


def _expected(root, line_start, line_end, filename="svc.py"):
    """Recompute exactly the way verify_provenance does."""
    lines = (root / filename).read_text(encoding="utf-8").splitlines(keepends=True)
    snippet = "".join(lines[max(0, line_start - 1):min(len(lines), line_end)])
    return hashlib.sha256(snippet.encode("utf-8")).hexdigest()


def _one(store, **item):
    out = store.read_code(items=[item], project_root=str(
        Path(store.db_path).parent.parent))
    assert len(out["results"]) == 1
    return out["results"][0]


def test_explicit_range_returns_matching_code_hash(project):
    root, store = project
    r = _one(store, file_path="svc.py", line_start=3, line_end=4)
    assert r["ok"] is True
    assert r["code_hash"] == _expected(root, 3, 4)
    assert r["provenance_lines"] == [3, 4]


def test_symbol_resolution_returns_code_hash(project):
    root, store = project
    r = _one(store, symbol="alpha", file_path="svc.py")
    assert r["ok"] is True
    lo, hi = r["provenance_lines"]
    assert r["code_hash"] == _expected(root, lo, hi)


def test_code_hash_covers_full_span_when_text_is_truncated(project):
    """The agent records provenance_lines, so the hash must match THOSE lines."""
    root, store = project
    (root / "big.py").write_text("".join(f"line {i}\n" for i in range(1, 401)),
                                 encoding="utf-8")
    r = store.read_code(
        items=[{"file_path": "big.py", "line_start": 1, "line_end": 400}],
        max_lines_per_item=10,
        project_root=str(root))["results"][0]

    assert r["ok"] is True
    assert r["truncated"] is True
    lo, hi = r["provenance_lines"]
    assert [lo, hi] == [1, 400], "provenance must describe the full requested span"
    assert r["code_hash"] == _expected(root, lo, hi, "big.py"), (
        "hash must cover the full span, not the truncated text returned")


def test_code_hash_is_stable_across_calls(project):
    _, store = project
    a = _one(store, file_path="svc.py", line_start=6, line_end=7)["code_hash"]
    b = _one(store, file_path="svc.py", line_start=6, line_end=7)["code_hash"]
    assert a == b


def test_code_hash_changes_when_the_snippet_changes(project):
    root, store = project
    before = _one(store, file_path="svc.py", line_start=6, line_end=7)["code_hash"]
    (root / "svc.py").write_text(
        "import os\n\ndef alpha():\n    return 1\n\ndef beta():\n    return 99\n",
        encoding="utf-8")
    after = _one(store, file_path="svc.py", line_start=6, line_end=7)["code_hash"]
    assert before != after


def test_error_items_have_no_code_hash(project):
    _, store = project
    r = _one(store, file_path="does_not_exist.py", line_start=1, line_end=2)
    assert r["ok"] is False
    assert "code_hash" not in r


def test_hash_matches_what_verify_provenance_expects(project):
    """End-to-end: write the fact with the returned hash, then verify it.

    This is the whole point -- an agent must be able to produce a fact that
    verify_provenance confirms without recomputing anything itself.
    """
    root, store = project
    r = _one(store, symbol="alpha", file_path="svc.py")
    lo, hi = r["provenance_lines"]
    fid = store.write(
        category="component",
        content="alpha returns the constant 1",
        file_path="svc.py", line_start=lo, line_end=hi,
        code_hash=r["code_hash"], embed=False, force=True)
    assert fid > 0
    out = store.verify_provenance(fid, project_root=str(root))
    assert out["status"] == "VERIFIED", (
        f"provenance did not verify with the read_code-supplied hash: {out}")
    assert os.path.exists(str(root / "svc.py"))
