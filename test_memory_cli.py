"""Hermetic tests for memory_cli.py dispatch.

Every command is exercised through main() with a stubbed sys.argv and a
FakeCLIStore standing in for the real MemoryStore, so no database is ever
created or touched. Assertions target the wiring: the right store method is
called with clamped/normalized arguments, output is printed, and the store
is always closed — including the `wire` command, which must never construct
a store at all.
"""
import json
import sys

import pytest

import memory_cli


class FakeCLIStore:
    instances = []

    def __init__(self, db_path=None):
        self.db_path = db_path
        self.calls = []
        self.closed = False
        FakeCLIStore.instances.append(self)

    def _rec(self, name, *a, **k):
        self.calls.append((name, a, k))

    # -- canned store surface used by the CLI --
    def stats(self):
        self._rec("stats")
        return {"db_path": self.db_path, "total_active": 2, "total_deprecated": 0,
                "total_edges": 1, "active_by_category": {"decision": 2}}

    def audit(self):
        self._rec("audit")
        return {"total_active_checked": 2, "potential_duplicates_count": 0,
                "potential_duplicates": []}

    def export_markdown(self, category=None):
        self._rec("export_markdown", category=category)
        return "# Memory\n"

    def query(self, text, category=None, tag=None, limit=10):
        self._rec("query", text, category=category, tag=tag, limit=limit)
        return [{"id": 1, "category": "decision", "content": "hit", "file_path": "a.py"}]

    def query_hybrid(self, text, category=None, tag=None, limit=10, explain=False,
                     fields=None):
        self._rec("query_hybrid", text, category=category, tag=tag, limit=limit,
                  explain=explain, fields=fields)
        from pm_common import ProjectFieldError, project_fields
        if fields:
            # Mirror the real store so a bad field list fails the way it does
            # in production rather than being swallowed by the fake.
            project_fields([{"id": 1, "category": "decision", "content": "hit"}],
                           fields)
        return []

    def link_facts(self, s, t, r):
        self._rec("link_facts", s, t, r)
        return 7

    def get_graph(self, root_id, max_depth=3):
        self._rec("get_graph", root_id, max_depth=max_depth)
        return [{"depth": 1, "relation": "relates_to", "target_id": 2,
                 "target_category": "other", "target_content": "x"}]

    def verify_provenance(self, fid, project_root=None):
        self._rec("verify_provenance", fid, project_root=project_root)
        return {"status": "ok", "fact_id": fid}

    def consolidate_state_facts(self, session_id=None, category="state", allow_all=False):
        self._rec("consolidate_state_facts", session_id=session_id,
                  category=category, allow_all=allow_all)
        return {"consolidated": 0}

    def get_context(self, query, limit=5):
        self._rec("get_context", query, limit=limit)
        return "CTX"

    def get_file_context(self, file_path):
        self._rec("get_file_context", file_path)
        return "FILECTX"

    def check_safety(self, files):
        self._rec("check_safety", files)
        return {"safe": True}

    def optimize_db(self):
        self._rec("optimize_db")
        return {"vacuum": True}

    def export_interactive_html(self, out=None):
        self._rec("export_interactive_html", out=out)
        return out or "graph.html"

    def get_state(self, category=None, limit=None):
        # limit=None is passed by the `state` command so the printed
        # "Active State (N facts)" count is the true active total.
        self._rec("get_state", category=category, limit=limit)
        return [{"id": 1, "category": "state", "confidence": "confirmed", "content": "s"}]

    def scan_provenance(self, project_root=None, auto_deprecate=False, auto_heal=False):
        self._rec("scan_provenance", project_root=project_root,
                  auto_deprecate=auto_deprecate, auto_heal=auto_heal)
        return {"ok": []}

    def backfill_embeddings(self):
        self._rec("backfill_embeddings")
        return {"embedded": 0}

    def backup_db(self, tag="manual", keep=8):
        self._rec("backup_db", tag=tag, keep=keep)
        return {"backup_path": "memory.db.backup-manual-20260101-000000.bak",
                "bytes": 1024, "kept": 1, "pruned": []}

    def prune_stale(self, days_inactive=90, dry_run=True):
        self._rec("prune_stale", days_inactive=days_inactive, dry_run=dry_run)
        return {"pruned": []}

    def prepare_task(self, intent, file_paths=None, limit=5):
        self._rec("prepare_task", intent, file_paths=file_paths, limit=limit)
        return "BRIEFING"

    def close(self):
        self.closed = True


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """Patch argv/store; return (run, stores) helper."""
    FakeCLIStore.instances.clear()
    monkeypatch.setattr(memory_cli, "MemoryStore", FakeCLIStore)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["memory_cli.py", *argv])
        memory_cli.main()
        return FakeCLIStore.instances[-1] if FakeCLIStore.instances else None

    return run


def _call(store, name):
    return [c for c in store.calls if c[0] == name]


# --------------------------------------------------------------------------
# db routing + lifecycle
# --------------------------------------------------------------------------

def test_explicit_db_routing(cli, tmp_path):
    db = str(tmp_path / "custom.db")
    store = cli("stats", "--db", db)
    assert store.db_path == db
    assert store.closed is True


def test_root_binds_to_project_db(cli, tmp_path):
    # Only --root-accepting commands (verify/scan-provenance/bootstrap) bind;
    # `stats` has no --root flag, so exercise the binding via scan-provenance.
    store = cli("scan-provenance", "--root", str(tmp_path))
    assert store.db_path.replace("\\", "/").endswith(".cognigraph/memory.db")
    assert ".cognigraph" in store.db_path
    # No stray DB file created by a stubbed store; real binding only.
    assert not (tmp_path / ".cognigraph" / "memory.db").exists()


def test_default_db_fallback(cli, monkeypatch):
    monkeypatch.setattr(memory_cli, "default_db_path", lambda: "SENTINEL.db")
    store = cli("stats")
    assert store.db_path == "SENTINEL.db"
    assert store.closed is True


def test_no_command_exits(cli):
    with pytest.raises(SystemExit):
        cli()


def test_wire_never_constructs_store(cli, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("store must not be constructed for wire")
    monkeypatch.setattr(memory_cli, "MemoryStore", boom)
    seen = {}
    monkeypatch.setattr("wire.wire_agent",
                        lambda agent, project_root=None, drop_protocol=True,
                        global_config=False, force_protocol=False:
                        seen.update(agent=agent, root=project_root,
                                    drop=drop_protocol,
                                    global_config=global_config) or {"status": "WIRED"})
    monkeypatch.setattr(sys, "argv", ["memory_cli.py", "wire", "zed",
                                      "--root", str(tmp_path), "--no-protocol"])
    memory_cli.main()  # would raise if a store were constructed
    assert seen == {"agent": "zed", "root": str(tmp_path), "drop": False,
                    "global_config": False}


def test_wire_global_flag_passthrough(cli, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr("wire.wire_agent",
                        lambda agent, project_root=None, drop_protocol=True,
                        global_config=False, force_protocol=False:
                        seen.update(agent=agent, global_config=global_config)
                        or {"status": "WIRED"})
    monkeypatch.setattr(sys, "argv", ["memory_cli.py", "wire", "antigravity",
                                      "--root", str(tmp_path), "--global"])
    memory_cli.main()
    assert seen == {"agent": "antigravity", "global_config": True}


def test_wire_force_protocol_passthrough(cli, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr("wire.wire_agent",
                        lambda agent, project_root=None, drop_protocol=True,
                        global_config=False, force_protocol=False:
                        seen.update(agent=agent, force=force_protocol)
                        or {"status": "WIRED"})
    monkeypatch.setattr(sys, "argv", ["memory_cli.py", "wire", "opencode",
                                      "--root", str(tmp_path), "--force-protocol"])
    memory_cli.main()
    assert seen == {"agent": "opencode", "force": True}


def test_wire_auto_detect_dispatched(cli, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr("wire.wire_agent",
                        lambda agent, project_root=None, drop_protocol=True,
                        global_config=False, force_protocol=False:
                        seen.update(agent=agent, root=project_root,
                                    global_config=global_config) or {"status": "WIRED"})
    monkeypatch.setattr("wire.detect_agent", lambda proj: "antigravity")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys, "argv", ["memory_cli.py", "wire", "--root", str(tmp_path)])
    memory_cli.main()
    assert seen["agent"] == "antigravity"


def test_wire_invalid_agent_exits(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["memory_cli.py", "wire", "not-an-agent", "--root", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        memory_cli.main()
    assert exc.value.code == 1


# --------------------------------------------------------------------------
# read commands
# --------------------------------------------------------------------------

def test_stats_output(cli, capsys):
    cli("stats")
    out = capsys.readouterr().out
    assert "=== CogniGraph Statistics ===" in out
    assert "Total Active" in out


def test_audit_empty_and_duplicates(cli, capsys, monkeypatch):
    cli("audit")
    assert "No duplicate active facts detected." in capsys.readouterr().out
    store = FakeCLIStore.instances[-1]
    monkeypatch.setattr(FakeCLIStore, "audit", lambda self: {
        "total_active_checked": 2, "potential_duplicates_count": 1,
        "potential_duplicates": [{"similarity": 0.9, "category": "decision",
                                  "fact1_id": 1, "fact1_content": "a",
                                  "fact2_id": 2, "fact2_content": "b"}]})
    cli("audit")
    assert "Potential Duplicates : 1" in capsys.readouterr().out


def test_audit_threshold_flags_forward_to_store(cli, capsys, monkeypatch):
    """--semantic-threshold/--lexical-threshold reach store.audit() as kwargs."""
    seen = {}

    def _audit(self, **kwargs):
        seen.update(kwargs)
        return {"total_active_checked": 2, "potential_duplicates_count": 1,
                "potential_duplicates": [{
                    "similarity": 0.91, "category": "decision", "method": "semantic",
                    "cross_category": True,
                    "fact1_id": 1, "fact1_content": "a", "fact1_category": "state",
                    "fact1_file_path": "README.md",
                    "fact2_id": 2, "fact2_content": "b", "fact2_category": "decision",
                    "fact2_file_path": None}],
                "method": "semantic", "embedded_checked": 2,
                "thresholds": {"semantic": 0.9, "lexical": 0.5}}

    monkeypatch.setattr(FakeCLIStore, "audit", _audit)
    cli("audit", "--semantic-threshold", "0.9", "--lexical-threshold", "0.5")
    assert seen == {"semantic_threshold": 0.9, "lexical_threshold": 0.5}
    out = capsys.readouterr().out
    assert "semantic>=0.9" in out
    assert "read-only report" in out
    assert "CROSS-CATEGORY" in out
    assert "via semantic" in out
    assert "README.md" in out


def test_audit_without_flags_calls_store_with_no_kwargs(cli, monkeypatch):
    """argparse.SUPPRESS keeps the no-flag call at audit(), so a stubbed
    audit(self) signature keeps working."""
    calls = []

    def _audit(self):
        calls.append(1)
        return {"total_active_checked": 0, "potential_duplicates_count": 0,
                "potential_duplicates": []}

    monkeypatch.setattr(FakeCLIStore, "audit", _audit)
    cli("audit")
    assert calls == [1]


def test_export_stdout_and_file(cli, tmp_path, capsys):
    cli("export", "--category", "decision")
    store = FakeCLIStore.instances[-1]
    assert _call(store, "export_markdown")[0][2] == {"category": "decision"}
    assert "# Memory" in capsys.readouterr().out
    out = tmp_path / "sub" / "mem.md"
    cli("export", "--out", str(out))
    assert out.read_text(encoding="utf-8") == "# Memory\n"


def test_query_plain_hybrid_and_limit_clamps(cli):
    s = cli("query", "hello")
    assert _call(s, "query")[0][1] == ("hello",)
    s = cli("query", "hello", "--hybrid", "--limit", "500", "--tag", "t")
    assert _call(s, "query_hybrid")[0][2]["limit"] == 100  # clamped
    assert _call(s, "query_hybrid")[0][2]["tag"] == "t"
    s = cli("query", "hello", "--limit", "0")
    assert _call(s, "query")[0][2]["limit"] == 10  # falsy -> default


def test_query_explain_flag(cli):
    s = cli("query", "hello", "--hybrid")
    assert _call(s, "query_hybrid")[0][2]["explain"] is False
    s = cli("query", "hello", "--hybrid", "--explain")
    assert _call(s, "query_hybrid")[0][2]["explain"] is True


def test_query_fields_flag(cli, capsys):
    s = cli("query", "hello", "--hybrid")
    assert _call(s, "query_hybrid")[0][2]["fields"] is None
    s = cli("query", "hello", "--hybrid", "--fields", "id,content")
    assert _call(s, "query_hybrid")[0][2]["fields"] == "id,content"
    out = capsys.readouterr().out
    # The projection must be announced, and absent keys must not print blank.
    assert "projected onto: id, content" in out
    assert "—" in out


def test_query_bad_field_exits_nonzero(cli, capsys):
    with pytest.raises(SystemExit) as e:
        cli("query", "hello", "--hybrid", "--fields", "id,file_pth")
    assert e.value.code == 2
    assert "unknown result field" in capsys.readouterr().err


def test_link_and_graph(cli, capsys):
    s = cli("link", "1", "2", "depends_on")
    assert _call(s, "link_facts")[0][1] == (1, 2, "depends_on")
    assert "edge ID: 7" in capsys.readouterr().out
    s = cli("graph", "1", "--depth", "99")
    assert _call(s, "get_graph")[0][2] == {"max_depth": 10}  # clamped
    s = cli("graph", "1", "--depth", "0")
    assert _call(s, "get_graph")[0][2] == {"max_depth": 3}  # falsy -> default


def test_verify_state_and_context_family(cli, capsys, tmp_path):
    s = cli("verify", "3", "--root", str(tmp_path))
    assert _call(s, "verify_provenance")[0][1:] == ((3,), {"project_root": str(tmp_path)})
    assert "Provenance status for fact 3: ok" in capsys.readouterr().out
    s = cli("state", "--json")
    # limit=None: the command prints "Active State (N facts)", so N must be the
    # true active count rather than the 200-row default.
    assert _call(s, "get_state")[0][2] == {"category": None, "limit": None}
    assert json.loads(capsys.readouterr().out)[0]["id"] == 1
    s = cli("context", "how does X work?", "--limit", "200")
    assert _call(s, "get_context")[0][2] == {"limit": 50}
    s = cli("file-context", "a.py")
    assert _call(s, "get_file_context")[0][1] == ("a.py",)
    s = cli("check-safety", "a.py", "b.py")
    assert _call(s, "check_safety")[0][1] == (["a.py", "b.py"],)


def test_optimize_export_ui_scan_backfill(cli):
    assert _call(cli("optimize"), "optimize_db")
    s = cli("export-ui", "--out", "ui.html")
    assert _call(s, "export_interactive_html")[0][2] == {"out": "ui.html"}
    s = cli("scan-provenance", "--root", "R", "--auto-deprecate")
    assert _call(s, "scan_provenance")[0][2] == {"project_root": "R",
                                                "auto_deprecate": True, "auto_heal": False}
    assert _call(cli("backfill"), "backfill_embeddings")


def test_backup_dispatch_clamps(cli, capsys):
    s = cli("backup", "--tag", "weekly", "--keep", "3")
    assert _call(s, "backup_db")[0][2] == {"tag": "weekly", "keep": 3}
    assert "backup-manual" in capsys.readouterr().out
    s = cli("backup", "--keep", "0")
    assert _call(s, "backup_db")[0][2] == {"tag": "manual", "keep": 8}  # falsy -> default


def test_consolidate_and_prune_flags(cli):
    s = cli("consolidate", "--session", "S9", "--all")
    assert _call(s, "consolidate_state_facts")[0][2] == {"session_id": "S9",
                                                        "category": "state", "allow_all": True}
    s = cli("prune")
    assert _call(s, "prune_stale")[0][2] == {"days_inactive": 90, "dry_run": True}
    s = cli("prune", "--days", "7", "--apply")
    assert _call(s, "prune_stale")[0][2] == {"days_inactive": 7, "dry_run": False}
    s = cli("prepare-task", "fix X", "--files", "a.py", "b.py")
    assert _call(s, "prepare_task")[0][2] == {"file_paths": ["a.py", "b.py"], "limit": 5}


# --------------------------------------------------------------------------
# commands delegating to other modules
# --------------------------------------------------------------------------

def test_promote_and_branch_dispatch(cli, monkeypatch):
    monkeypatch.setattr("branch_promotion.promote_branch",
                        lambda db, src, convergence_policy=None: {"ok": src, "db": db})
    monkeypatch.setattr("branch_promotion.create_branch",
                        lambda db, repo_id=None: {"created": True})
    monkeypatch.setattr("branch_promotion.discard_branch",
                        lambda db, src, reason=None: {"discarded": src, "reason": reason})
    monkeypatch.setattr("branch_promotion.list_branches",
                        lambda db, repo_id=None, status=None: {"branches": []})
    s = cli("promote", "4", "--db", "X.db")
    assert s.db_path == "X.db"  # promote uses db_path for the module call
    s = cli("branch", "create")
    s = cli("branch", "discard", "4", "--reason", "oops")
    s = cli("branch", "list", "--status", "ACTIVE")


def test_bootstrap_dispatch_maps_flags(cli, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr("bootstrap.bootstrap_project",
                        lambda root, store=None, dry_run=True, full=False,
                        max_facts=150, install_hook=True, drop_protocol=True,
                        force_protocol=False:
                        seen.update(root=root, dry=dry_run, full=full, max=max_facts,
                                    hook=install_hook, proto=drop_protocol) or {"status": "x"})
    cli("bootstrap", "--root", str(tmp_path), "--apply", "--full",
        "--max-facts", "5000", "--no-hook")
    assert seen == {"root": str(tmp_path), "dry": False, "full": True,
                    "max": 1000, "hook": False, "proto": True}  # max-facts clamped


def test_bootstrap_force_protocol_passthrough(cli, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr("bootstrap.bootstrap_project",
                        lambda root, store=None, dry_run=True, full=False,
                        max_facts=150, install_hook=True, drop_protocol=True,
                        force_protocol=False:
                        seen.update(force=force_protocol) or {"status": "x"})
    cli("bootstrap", "--root", str(tmp_path), "--apply", "--force-protocol")
    assert seen == {"force": True}
