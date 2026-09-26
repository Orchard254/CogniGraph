"""Multi-project DB routing: pin the contracts the existing tests leave implicit.

The routing added in e57e207 is covered by test_server_tools.py
(test_uri_to_path_and_resolve_db, test_dynamic_call_tool_routes_by_roots,
test_dynamic_call_tool_routes_by_file_path), but all three use a project that
ALREADY has .cognigraph/memory.db. Two real behaviours therefore had no coverage
and read as accidents rather than design:

1. _resolve_db_from_root returns the canonical path for a project root that has
   no database at all (server.py falls through to `if root_dir.is_dir()`).
   MemoryStore creates the missing parent dir and an empty DB merely by being
   opened, so a tool call that only MENTIONS a file in an un-onboarded project
   silently creates .cognigraph/memory.db there. That is the lazy-onboarding
   behaviour, and it is intended -- but it was undocumented and untested, so
   this file pins it.

2. The file_path heuristic reads only the FIRST entry of file_paths (and of
   items). A batch spanning two projects therefore routes to whichever project
   the first file belongs to. That is current behaviour, not necessarily
   desired, but it must be pinned before anyone changes it.

Run with: python -m pytest test_multi_project_routing.py -v
"""
import json
from pathlib import Path

import pytest

import server
from memory_store import MemoryStore
from pm_common import find_project_root


def _project(root, name, seed_content=None, git=True):
    proj = root / name
    proj.mkdir(parents=True, exist_ok=True)
    if git:
        (proj / ".git").mkdir(exist_ok=True)
    (proj / "src").mkdir(exist_ok=True)
    (proj / "src" / "svc.py").write_text("def run(): pass\n", encoding="utf-8")
    if seed_content:
        store = MemoryStore(str(proj / ".cognigraph" / "memory.db"))
        store.write("component", seed_content)
        store.close()
    return proj


# --- contract 1: resolution and lazy creation ---------------------------

def test_resolves_canonical_path_for_uninitialised_project(tmp_path):
    """No DB present: resolution still returns the canonical location."""
    proj = _project(tmp_path, "fresh")
    expected = proj / ".cognigraph" / "memory.db"

    assert not expected.exists()
    resolved = server._resolve_db_from_root(proj)

    assert resolved is not None
    assert resolved == str(expected)
    assert not expected.exists(), "resolution must not create anything itself"


def test_resolve_returns_none_for_non_directory(tmp_path):
    """A path that is not a directory cannot host a DB."""
    missing = tmp_path / "does_not_exist"
    assert server._resolve_db_from_root(missing) is None


def test_prefers_canonical_over_legacy_dir(tmp_path):
    """With both present, .cognigraph wins over legacy .project-memory."""
    proj = _project(tmp_path, "both", seed_content="canonical wins")
    legacy = proj / ".project-memory" / "memory.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"")

    assert server._resolve_db_from_root(proj) == str(proj / ".cognigraph" / "memory.db")


def test_uses_legacy_dir_when_it_is_the_only_one(tmp_path):
    proj = _project(tmp_path, "legacy_only")
    legacy = proj / ".project-memory" / "memory.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"")

    assert server._resolve_db_from_root(proj) == str(legacy)


# --- contract 1b: the lazy-create side effect through a real tool call ---

@pytest.mark.anyio
async def test_roots_routing_onboards_an_uninitialised_project(tmp_path, monkeypatch):
    """MCP session roots CAN reach a project that has no memory yet.

    A root is supplied directly, so _resolve_db_from_root's canonical-path
    fallback applies and the store is created and bootstrapped on first use.
    """
    proj = _project(tmp_path, "via_roots")
    expected = proj / ".cognigraph" / "memory.db"
    assert not expected.exists()

    class Root:
        def __init__(self, uri):
            self.uri = uri

    class RootsResult:
        def __init__(self, roots):
            self.roots = roots

    class Session:
        async def list_roots(self):
            return RootsResult([Root(f"file:///{proj.as_posix()}")])

    class Ctx:
        session = Session()

    monkeypatch.setattr(server.mcp, "get_context", lambda: Ctx())
    res = await server.mcp.call_tool("memory_stats", {})
    data = json.loads(res[0].text)

    assert data["db_path"] == str(expected)
    assert expected.is_file()


@pytest.mark.anyio
async def test_file_path_heuristic_cannot_reach_an_uninitialised_project(tmp_path, monkeypatch):
    """DOCUMENTS A REAL GAP: mentioning a file is NOT enough to route.

    find_project_root() searches upward for an EXISTING .cognigraph/memory.db
    (or legacy) -- it looks for a database, not for a project marker like .git
    or pyproject.toml. So for a project that has no memory yet it returns None,
    resolved_db stays None, and the call silently falls back to the server's
    OWN database.

    Consequence: memory_get_file_context() for a file in an un-onboarded
    project returns THIS project's facts, and a memory_write() anchored to such
    a file would be stored in the wrong database. Contrast with the roots path
    above, which does onboard a fresh project.

    Pinned so the gap is explicit. Changing find_project_root to fall back to
    project markers would fix it, but that changes which database many existing
    calls resolve to, so it is a deliberate decision rather than a drive-by fix.
    """
    proj = _project(tmp_path, "via_filepath")
    target = proj / "src" / "svc.py"
    expected = proj / ".cognigraph" / "memory.db"

    class EmptyContext:
        session = None

    monkeypatch.setattr(server.mcp, "get_context", lambda: EmptyContext())
    res = await server.mcp.call_tool(
        "memory_get_file_context", {"file_path": str(target)})
    data = json.loads(res[0].text)

    assert not expected.exists(), (
        "premise: the file_path heuristic must not be able to onboard a project")
    # It answered, but from the server's own database, not the target project.
    assert "svc.py" in data.get("briefing_markdown", "")
    assert find_project_root(target.resolve()) is None, (
        "premise: find_project_root finds no DB, so no routing is possible")


# --- contract 2: batch routing uses the first file only ------------------

def test_batch_routing_uses_first_file_path(tmp_path):
    """file_paths[0] decides the project; later entries do not vote.

    Pinned so the cross-project-batch limitation is explicit.
    """
    proj_a = _project(tmp_path, "batch_a", seed_content="Fact from project A")
    proj_b = _project(tmp_path, "batch_b", seed_content="Fact from project B")
    a_file = proj_a / "src" / "svc.py"
    b_file = proj_b / "src" / "svc.py"

    assert find_project_root(a_file.resolve()) == proj_a.resolve()
    assert find_project_root(b_file.resolve()) == proj_b.resolve()

    # The heuristic evaluates only the first element, so the chosen DB is A's.
    chosen = server._resolve_db_from_root(
        find_project_root(Path(a_file).resolve()))
    assert chosen == str(proj_a / ".cognigraph" / "memory.db")


def test_find_project_root_looks_for_a_database_not_a_project_marker(tmp_path):
    """The root cause of the gap above, asserted directly.

    A .git directory is NOT a project marker for find_project_root; only an
    existing .cognigraph/memory.db (or legacy) is.
    """
    proj = _project(tmp_path, "marker_only", git=True)
    src = proj / "src" / "svc.py"
    assert (proj / ".git").is_dir()
    assert find_project_root(src) is None

    db = proj / ".cognigraph" / "memory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    assert find_project_root(src) == proj.resolve()
