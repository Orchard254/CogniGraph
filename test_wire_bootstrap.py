"""Hermetic tests for wire.py, bootstrap.py (dry/apply paths), hooks/git_hook.py.

Collected by pytest (unlike the standalone test_*.py e2e scripts excluded in
conftest.py). No embeddings, no real git, no writes outside tmp_path: the
claude-agent test redirects APPDATA, and bootstrap apply-path tests use a
FakeStore whose writes all report duplicates so the embedding backfill is
never reached.
"""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

import bootstrap
import wire
from wire import (_backup, _merge_json, _python_exe, wire_agent, detect_agent,
                  prompt_agent_choice, sync_agents_md, classify_agents_md)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _make_pm_root(tmp_path: Path) -> Path:
    """Fake CogniGraph install root with an AGENTS.md template."""
    pm = tmp_path / "pm"
    pm.mkdir()
    (pm / "AGENTS.md").write_text("# Protocol\n", encoding="utf-8")
    return pm


def _make_proj(tmp_path: Path, name: str = "proj") -> Path:
    proj = tmp_path / name
    proj.mkdir()
    return proj


def _demo_tree(root: Path) -> None:
    (root / "package.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0",
                    "dependencies": {"a": "^1.0"}, "scripts": {"test": "pytest"}}),
        encoding="utf-8")
    (root / "requirements.txt").write_text("requests==2.31\n# comment\n\nurllib3\n",
                                            encoding="utf-8")
    (root / "README.md").write_text(
        "# Demo\n\n## Decision log\nWe chose X.\n\n## Rules\nNever do Y.\n\n## Misc\nPlain text.\n",
        encoding="utf-8")
    src = root / "src"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    nm = root / "node_modules"
    nm.mkdir()
    (nm / "evil.js").write_text("x=1", encoding="utf-8")
    (root / "stray.pyc").write_bytes(b"\x00\x01")


class FakeStore:
    """Apply-path stand-in: every write reports a duplicate (-1)."""

    def __init__(self):
        self.writes = []
        self.linked = False

    def write(self, **kwargs):
        self.writes.append(kwargs)
        return -1  # duplicate: keeps the embedding backfill out of the test

    def link_facts_to_structural_nodes(self, *a, **k):
        self.linked = True

    def close(self):
        pass


# --------------------------------------------------------------------------
# wire._backup
# --------------------------------------------------------------------------

def test_backup_missing_returns_none(tmp_path):
    assert _backup(tmp_path / "nope.json") is None


def test_backup_copy_and_rotation(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text("v0", encoding="utf-8")
    b0 = _backup(cfg)
    assert b0 is not None and Path(b0).read_text(encoding="utf-8") == "v0"
    # Rotate: each new backup pushes the older one down; .bak.2 is the oldest kept.
    cfg.write_text("v1", encoding="utf-8")
    _backup(cfg)
    cfg.write_text("v2", encoding="utf-8")
    _backup(cfg)
    cfg.write_text("v3", encoding="utf-8")
    _backup(cfg)
    assert (tmp_path / "mcp.json.bak").read_text(encoding="utf-8") == "v3"
    assert (tmp_path / "mcp.json.bak.1").read_text(encoding="utf-8") == "v2"
    assert (tmp_path / "mcp.json.bak.2").read_text(encoding="utf-8") == "v1"


# --------------------------------------------------------------------------
# wire._merge_json
# --------------------------------------------------------------------------

def test_merge_json_fresh_write(tmp_path):
    cfg = tmp_path / "sub" / "mcp.json"
    _merge_json(cfg, "mcpServers", {"command": "py", "args": [], "env": {}})
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data == {"mcpServers": {"cognigraph": {"command": "py", "args": [], "env": {}}}}
    assert not list(tmp_path.rglob("*.tmp"))  # no atomic-write leftovers


def test_merge_json_preserves_and_removes_deprecated(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcp": {"old": True}, "other": 1,
                               "mcpServers": {"prior": {"x": 1}}}), encoding="utf-8")
    _merge_json(cfg, "mcpServers", {"command": "py"})
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "mcp" not in data  # deprecated key removed
    assert data["other"] == 1  # unrelated content preserved
    assert data["mcpServers"]["prior"] == {"x": 1}  # sibling entries preserved
    assert data["mcpServers"]["cognigraph"] == {"command": "py"}


def test_merge_json_corrupt_preserved_not_clobbered(tmp_path):
    cfg = tmp_path / "mcp.json"
    raw = "{not valid json"
    cfg.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        _merge_json(cfg, "mcpServers", {"command": "py"})
    assert cfg.read_text(encoding="utf-8") == raw  # original untouched
    corrupt = list(tmp_path.glob("mcp.json.corrupt-*.bak"))
    assert len(corrupt) == 1 and corrupt[0].read_text(encoding="utf-8") == raw


def test_merge_json_nondict_top_key_reset(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": ["not", "a", "dict"]}), encoding="utf-8")
    _merge_json(cfg, "mcpServers", {"command": "py"})
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["cognigraph"] == {"command": "py"}


def test_merge_json_empty_or_whitespace_file(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text("   \n\t  ", encoding="utf-8")
    _merge_json(cfg, "mcpServers", {"command": "py"})
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["cognigraph"] == {"command": "py"}


# --------------------------------------------------------------------------
# wire._python_exe
# --------------------------------------------------------------------------

def test_python_exe_prefers_venv(tmp_path):
    pm = tmp_path / "pm"
    exe = pm / "venv" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("fake", encoding="utf-8")
    got = _python_exe(pm)
    assert got.replace("\\", "/").endswith("venv/Scripts/python.exe")


def test_python_exe_fallback_is_usable():
    got = _python_exe(Path("C:/nonexistent-pm-root"))
    assert isinstance(got, str) and got  # version-checked cmd or sys.executable


# --------------------------------------------------------------------------
# wire.wire_agent
# --------------------------------------------------------------------------

def test_wire_bad_agent_and_missing_root(tmp_path):
    with pytest.raises(ValueError, match="agent must be one of"):
        wire_agent("not-an-agent", project_root=str(tmp_path))
    with pytest.raises(ValueError, match="agent must be one of"):
        wire_agent(123, project_root=str(tmp_path))
    with pytest.raises(ValueError, match="project root does not exist"):
        wire_agent("opencode", project_root=str(tmp_path / "missing"))


def test_wire_opencode_entry(tmp_path):
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    res = wire_agent("opencode", project_root=str(proj), project_memory_root=str(pm))
    assert res["status"] == "WIRED"
    assert res["agents_md"] == "created"
    content = (proj / "AGENTS.md").read_text(encoding="utf-8")
    body, marker = wire._split_agents_marker(content)
    assert body == "# Protocol\n"
    assert marker == wire._agents_body_hash(body)
    cfg = json.loads((proj / "opencode.json").read_text(encoding="utf-8"))
    assert "mcpServers" not in cfg  # opencode uses `mcp`, never `mcpServers`
    entry = cfg["mcp"]["cognigraph"]
    assert entry["type"] == "local"
    assert entry["command"][0] and entry["command"][1].endswith("server.py")
    assert entry["environment"]["MEMORY_DB_PATH"].endswith(".cognigraph/memory.db")
    assert entry["enabled"] is True
    assert (proj / ".cognigraph").is_dir()


def test_wire_opencode_cleans_legacy_mcpservers(tmp_path):
    """Old (buggy) opencode.json with `mcpServers` gets migrated to `mcp`."""
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    (proj / "opencode.json").write_text(json.dumps({
        "mcpServers": {"cognigraph": {"command": "py", "args": ["server.py"],
                                      "env": {"MEMORY_DB_PATH": "x"}}}}),
        encoding="utf-8")
    res = wire_agent("opencode", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    cfg = json.loads((proj / "opencode.json").read_text(encoding="utf-8"))
    assert "mcpServers" not in cfg
    assert cfg["mcp"]["cognigraph"]["type"] == "local"


def test_wire_opencode_writes_alias_path(tmp_path):
    """Both ./opencode.json and ./.opencode/opencode.json get the entry."""
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path, "oc_alias_proj")
    alias_dir = proj / ".opencode"
    alias_dir.mkdir()
    alias = alias_dir / "opencode.json"
    alias.write_text(json.dumps({"mcp": {"other": {"type": "local",
                                                   "command": ["x"],
                                                   "environment": {}}}}),
                     encoding="utf-8")
    res = wire_agent("opencode", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    for path in (proj / "opencode.json", alias):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "mcpServers" not in data
        entry = data["mcp"]["cognigraph"]
        assert entry["type"] == "local"
        assert isinstance(entry["command"], list)
        assert entry["environment"]["MEMORY_DB_PATH"].endswith(".cognigraph/memory.db")
    # Sibling in the alias file is preserved, not clobbered.
    assert json.loads(alias.read_text(encoding="utf-8"))["mcp"]["other"] == {
        "type": "local", "command": ["x"], "environment": {}}
    assert str(proj / "opencode.json") in res["config_files"]
    assert str(alias) in res["config_files"]


def test_wire_cursor_nested_and_existing_backup(tmp_path):
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    (proj / ".cursor").mkdir()
    existing = proj / ".cursor" / "mcp.json"
    existing.write_text(json.dumps({"mcpServers": {"other": {}}}), encoding="utf-8")
    res = wire_agent("cursor", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    assert len(res["backups"]) == 1
    data = json.loads(existing.read_text(encoding="utf-8"))
    assert "other" in data["mcpServers"] and "cognigraph" in data["mcpServers"]
    assert res["agents_md"] == "skipped_disabled"


def test_wire_legacy_server_name_replaced_not_duplicated(tmp_path):
    """A config containing the legacy 'project-memory' entry gets rewritten."""
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    (proj / ".cursor").mkdir()
    existing = proj / ".cursor" / "mcp.json"
    existing.write_text(json.dumps({"mcpServers": {
        "project-memory": {"command": "py", "args": ["server.py"],
                           "env": {"MEMORY_DB_PATH": "x/.project-memory/memory.db"}},
        "other": {"command": "other"},
    }}), encoding="utf-8")
    res = wire_agent("cursor", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    data = json.loads(existing.read_text(encoding="utf-8"))
    assert "project-memory" not in data["mcpServers"]  # orphan replaced, not duplicated
    assert "cognigraph" in data["mcpServers"]
    assert data["mcpServers"]["other"] == {"command": "other"}  # unrelated preserved
    assert data["mcpServers"]["cognigraph"]["env"]["MEMORY_DB_PATH"].endswith(
        ".cognigraph/memory.db")


def test_wire_legacy_npx_and_path_entries_replaced(tmp_path):
    """Legacy npx-command and legacy-path siblings are removed; others stay."""
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    (proj / ".cursor").mkdir()
    existing = proj / ".cursor" / "mcp.json"
    existing.write_text(json.dumps({"mcpServers": {
        "my-memory": {"command": "npx", "args": ["-y", "@project-memory/mcp-server"]},
        "stale-paths": {"command": "py", "args": ["s.py"],
                        "env": {"MEMORY_DB_PATH": "y/.project-memory/memory.db"}},
        "unrelated": {"command": "node", "args": ["server.js"]},
    }}), encoding="utf-8")
    res = wire_agent("cursor", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    data = json.loads(existing.read_text(encoding="utf-8"))
    assert "my-memory" not in data["mcpServers"]
    assert "stale-paths" not in data["mcpServers"]
    assert data["mcpServers"]["unrelated"] == {"command": "node", "args": ["server.js"]}
    assert "cognigraph" in data["mcpServers"]


def test_wire_zed_top_level_env_regression(tmp_path):
    """Zed reads env vars TOP-LEVEL; settings.env never reaches the process."""
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    res = wire_agent("zed", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    entry = json.loads((proj / ".zed" / "settings.json").read_text(encoding="utf-8"))
    pm_entry = entry["context_servers"]["cognigraph"]
    assert "MEMORY_DB_PATH" in pm_entry["env"]
    assert "settings" not in pm_entry


def test_wire_zed_trust_note_in_output(tmp_path):
    """Zed output tells the user the trust prompt is expected."""
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path, "zed_note_proj")
    res = wire_agent("zed", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    assert any("one-time trust" in s for s in res["next_steps"])
    # Non-zed agents must not carry the note.
    res2 = wire_agent("cursor", project_root=str(proj), project_memory_root=str(pm),
                      drop_protocol=False)
    assert not any("one-time trust" in s for s in res2["next_steps"])


def test_wire_generic_writes_nothing(tmp_path):
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    res = wire_agent("generic", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    assert res["config_files"] == ["(none — paste the snippet below into your agent's MCP config)"]
    assert "cognigraph" in res["mcp_snippet"]["mcpServers"]
    assert not (proj / "opencode.json").exists()


def test_wire_claude_redirected_appdata(tmp_path, monkeypatch):
    """Must never touch the real %APPDATA% user config."""
    fake_appdata = tmp_path / "appdata"
    fake_appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(fake_appdata))
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    res = wire_agent("claude", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    cfg_path = fake_appdata / "Claude" / "claude_desktop_config.json"
    assert cfg_path.exists()
    assert "cognigraph" in json.loads(cfg_path.read_text(encoding="utf-8"))["mcpServers"]


def test_wire_corrupt_config_reports_not_raises(tmp_path):
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    (proj / "opencode.json").write_text("{broken", encoding="utf-8")
    res = wire_agent("opencode", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED_WITH_ERRORS"
    assert len(res["config_errors"]) == 1
    assert any("Fix config errors first" in s for s in res["next_steps"])


def test_wire_protocol_missing_creates_stamped_copy(tmp_path):
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    res = wire_agent("opencode", project_root=str(proj), project_memory_root=str(pm))
    assert res["agents_md"] == "created"
    assert "agents_md_new_file" not in res
    content = (proj / "AGENTS.md").read_text(encoding="utf-8")
    body, marker = wire._split_agents_marker(content)
    assert body == "# Protocol\n"
    assert marker == wire._agents_body_hash(body)
    # Second run is a no-op with an explicit status.
    res2 = wire_agent("opencode", project_root=str(proj), project_memory_root=str(pm))
    assert res2["agents_md"] == "up_to_date"
    assert (proj / "AGENTS.md").read_text(encoding="utf-8") == content


def test_wire_protocol_customized_writes_new_sibling(tmp_path):
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    (proj / "AGENTS.md").write_text("# Protocol\n\nMy team edits.\n", encoding="utf-8")
    res = wire_agent("opencode", project_root=str(proj), project_memory_root=str(pm))
    assert res["agents_md"] == "skipped_customized_update_available"
    # Real file untouched; no backup taken since nothing was overwritten.
    assert (proj / "AGENTS.md").read_text(encoding="utf-8") == "# Protocol\n\nMy team edits.\n"
    assert not list(proj.glob("AGENTS.md.bak*"))
    new_file = Path(res["agents_md_new_file"])
    assert new_file == proj / "AGENTS.md.new"
    body, marker = wire._split_agents_marker(new_file.read_text(encoding="utf-8"))
    assert body == "# Protocol\n"
    assert marker == wire._agents_body_hash(body)
    assert any("AGENTS.md.new" in s for s in res["next_steps"])
    # No .new written -> the optional report field stays absent entirely.
    res_quiet = wire_agent("cursor", project_root=str(proj), project_memory_root=str(pm),
                           drop_protocol=False)
    assert "agents_md_new_file" not in res_quiet


def test_wire_protocol_force_overwrites_customized_with_backup(tmp_path):
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    (proj / "AGENTS.md").write_text("# Protocol\n\nMy team edits.\n", encoding="utf-8")
    (proj / "AGENTS.md.new").write_text("stale leftover\n", encoding="utf-8")
    res = wire_agent("opencode", project_root=str(proj), project_memory_root=str(pm),
                     force_protocol=True)
    assert res["agents_md"] == "updated_forced"
    assert "agents_md_new_file" not in res
    content = (proj / "AGENTS.md").read_text(encoding="utf-8")
    body, marker = wire._split_agents_marker(content)
    assert body == "# Protocol\n"
    assert marker == wire._agents_body_hash(body)
    assert (proj / "AGENTS.md.bak").read_text(encoding="utf-8") == "# Protocol\n\nMy team edits.\n"
    assert str(proj / "AGENTS.md.bak") in res["backups"]
    assert not (proj / "AGENTS.md.new").exists()
    assert any("force-protocol" in s for s in res["next_steps"])


def test_wire_protocol_force_is_opt_in(tmp_path):
    # Without the flag the safe default is unchanged: customized files are
    # staged as .new, never overwritten.
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    (proj / "AGENTS.md").write_text("mine\n", encoding="utf-8")
    res = wire_agent("opencode", project_root=str(proj), project_memory_root=str(pm))
    assert res["agents_md"] == "skipped_customized_update_available"
    assert (proj / "AGENTS.md").read_text(encoding="utf-8") == "mine\n"


def test_wire_protocol_stale_unmodified_overwrites_with_backup(tmp_path, monkeypatch):
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    old_body = "# Older protocol\n"
    old_hash = wire._agents_body_hash(old_body)
    monkeypatch.setattr(wire, "KNOWN_AGENTS_TEMPLATE_HASHES", frozenset({old_hash}))
    old_rendered = old_body + f"<!-- cognigraph-template sha256:{old_hash} -->\n"
    (proj / "AGENTS.md").write_text(old_rendered, encoding="utf-8")
    res = wire_agent("opencode", project_root=str(proj), project_memory_root=str(pm))
    assert res["agents_md"] == "updated_clean"
    assert "agents_md_new_file" not in res
    content = (proj / "AGENTS.md").read_text(encoding="utf-8")
    body, marker = wire._split_agents_marker(content)
    assert body == "# Protocol\n"  # current template installed
    assert marker == wire._agents_body_hash(body)
    assert (proj / "AGENTS.md.bak").read_text(encoding="utf-8") == old_rendered
    assert str(proj / "AGENTS.md.bak") in res["backups"]


def test_wire_protocol_unmarked_known_body_is_stale(tmp_path, monkeypatch):
    # Pre-versioning copy, byte-identical to a shipped revision: safe to stamp.
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    old_body = "# Older protocol\n"
    old_hash = wire._agents_body_hash(old_body)
    monkeypatch.setattr(wire, "KNOWN_AGENTS_TEMPLATE_HASHES", frozenset({old_hash}))
    (proj / "AGENTS.md").write_text(old_body, encoding="utf-8")  # no marker at all
    res = wire_agent("opencode", project_root=str(proj), project_memory_root=str(pm))
    assert res["agents_md"] == "updated_clean"
    body, marker = wire._split_agents_marker(
        (proj / "AGENTS.md").read_text(encoding="utf-8"))
    assert body == "# Protocol\n"
    assert marker == wire._agents_body_hash(body)


def test_wire_protocol_new_sibling_overwritten(tmp_path):
    # AGENTS.md.new is our own artifact: a stale one is replaced, and the real
    # file is still never touched or backed up.
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    (proj / "AGENTS.md").write_text("mine\n", encoding="utf-8")
    (proj / "AGENTS.md.new").write_text("stale leftover\n", encoding="utf-8")
    res = wire_agent("opencode", project_root=str(proj), project_memory_root=str(pm))
    assert res["agents_md"] == "skipped_customized_update_available"
    body, _ = wire._split_agents_marker(
        (proj / "AGENTS.md.new").read_text(encoding="utf-8"))
    assert body == "# Protocol\n"
    assert (proj / "AGENTS.md").read_text(encoding="utf-8") == "mine\n"
    assert not list(proj.glob("AGENTS.md.bak*"))


def test_classify_agents_md_four_outcomes():
    cur_body = "# Protocol\n"
    cur = wire._agents_body_hash(cur_body)
    old_body = "# Older protocol\n"
    old = wire._agents_body_hash(old_body)
    known = frozenset({old, cur})

    def stamped(body, h):
        return body + f"<!-- cognigraph-template sha256:{h} -->\n"

    assert classify_agents_md(stamped(cur_body, cur), cur, known) == "up_to_date"
    assert classify_agents_md(stamped(old_body, old), cur, known) == "stale_unmodified"
    assert classify_agents_md(old_body, cur, known) == "stale_unmodified"  # pre-versioning
    assert classify_agents_md(cur_body, cur, known) == "stale_unmodified"  # needs marker stamp
    assert classify_agents_md("# Protocol\n\nedited\n", cur, known) == "customized"
    # Current marker but edited body: the marker is stale evidence, not a pass.
    assert classify_agents_md(stamped("# Protocol\n\nedited\n", cur), cur, known) == "customized"
    # Marker/body mismatch and unknown markers are edit signals, not passes.
    assert classify_agents_md(stamped(cur_body, old), cur, known) == "customized"
    assert classify_agents_md(stamped(cur_body, "0" * 64), cur, known) == "customized"
    # CRLF checkouts normalize to the same verdict as LF.
    assert classify_agents_md(stamped(cur_body, cur).replace("\n", "\r\n"),
                              cur, known) == "up_to_date"


def test_known_hashes_cover_shipped_history():
    # A floor, not an equality: the set must only ever GROW. Every legitimate
    # AGENTS.md template change appends one hash, so pinning an exact count
    # would fail on every correct update and discourage them. What must never
    # happen is entries being removed, hence >= against the high-water mark.
    assert len(wire.KNOWN_AGENTS_TEMPLATE_HASHES) >= 10
    assert all(re.fullmatch(r"[0-9a-f]{64}", h)
               for h in wire.KNOWN_AGENTS_TEMPLATE_HASHES)
    real = Path(wire.__file__).resolve().parent / "AGENTS.md"
    body, marker = wire._split_agents_marker(real.read_text(encoding="utf-8"))
    assert marker == wire._agents_body_hash(body)  # shipped file stamps its own hash
    assert marker in wire.KNOWN_AGENTS_TEMPLATE_HASHES  # ... and it is a known revision


def test_wire_and_bootstrap_share_helper(tmp_path):
    # Both paths read the same canonical template: identical file, identical
    # verdict. Uses the real install root so both sides see the real template.
    real_pm = Path(wire.__file__).resolve().parent
    proj = _make_proj(tmp_path, "agree_fresh")
    assert sync_agents_md(proj / "AGENTS.md", real_pm)["status"] == "created"
    assert bootstrap.drop_agents_md(proj) == "up_to_date"
    proj2 = _make_proj(tmp_path, "agree_custom")
    (proj2 / "AGENTS.md").write_text("hand-edited\n", encoding="utf-8")
    assert (sync_agents_md(proj2 / "AGENTS.md", real_pm)["status"]
            == bootstrap.drop_agents_md(proj2)
            == "skipped_customized_update_available")


def test_wire_protocol_no_template(tmp_path):
    pm = tmp_path / "pm-bare"  # no AGENTS.md template
    pm.mkdir()
    proj = _make_proj(tmp_path)
    res = wire_agent("opencode", project_root=str(proj), project_memory_root=str(pm))
    assert res["agents_md"] == "skipped_no_template"


def test_wire_antigravity_workspace_primary(tmp_path, monkeypatch):
    """Default writes workspace plugin .agents/plugins/cognigraph/mcp_config.json, NOT global."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(wire.Path, "home", lambda: fake_home)
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path, "ag_proj")
    plugin_dir = proj / ".agents" / "plugins" / "cognigraph"
    plugin_dir.mkdir(parents=True)
    existing = plugin_dir / "mcp_config.json"
    existing.write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}}),
                        encoding="utf-8")
    legacy = proj / ".agents" / "mcp_config.json"
    legacy.write_text("legacy", encoding="utf-8")

    res = wire_agent("antigravity", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    assert not legacy.exists()  # legacy cleaned up
    assert (plugin_dir / "plugin.json").exists()
    data = json.loads(existing.read_text(encoding="utf-8"))
    assert data["mcpServers"]["other"] == {"command": "other"}  # sibling preserved
    assert data["mcpServers"]["cognigraph"]["env"]["MEMORY_DB_PATH"].endswith(
        ".cognigraph/memory.db")
    assert str(existing) in res["config_files"]
    # Global untouched by default — no cross-project bleed.
    assert not (fake_home / ".gemini" / "config" / "mcp_config.json").exists()


def test_wire_antigravity_global_opt_in(tmp_path, monkeypatch):
    """global_config=True additionally writes the user-global file without hardcoded MEMORY_DB_PATH."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(wire.Path, "home", lambda: fake_home)
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path, "ag_global_proj")
    res = wire_agent("antigravity", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False, global_config=True)
    assert res["status"] == "WIRED"
    assert (proj / ".agents" / "plugins" / "cognigraph" / "mcp_config.json").exists()
    gbl = fake_home / ".gemini" / "config" / "mcp_config.json"
    assert gbl.exists()
    gbl_data = json.loads(gbl.read_text(encoding="utf-8"))
    assert "cognigraph" in gbl_data["mcpServers"]
    assert "env" not in gbl_data["mcpServers"]["cognigraph"]


def test_wire_windsurf_home_config(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(wire.Path, "home", lambda: fake_home)
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    res = wire_agent("windsurf", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    cfg = fake_home / ".codeium" / "windsurf" / "mcp_config.json"
    assert cfg.exists()
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "cognigraph" in data["mcpServers"]


def test_wire_roo_and_cline_config(tmp_path):
    pm = _make_pm_root(tmp_path)
    proj_roo = _make_proj(tmp_path, "roo_proj")
    res_roo = wire_agent("roo", project_root=str(proj_roo), project_memory_root=str(pm),
                         drop_protocol=False)
    assert res_roo["status"] == "WIRED"
    assert (proj_roo / ".roo" / "mcp.json").exists()

    proj_cline = _make_proj(tmp_path, "cline_proj")
    res_cline = wire_agent("cline", project_root=str(proj_cline), project_memory_root=str(pm),
                           drop_protocol=False)
    assert res_cline["status"] == "WIRED"
    assert (proj_cline / ".cline" / "mcp.json").exists()


def test_wire_kiro_project_config(tmp_path):
    """Kiro writes project-local .kiro/settings/mcp.json, merging siblings."""
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path, "kiro_proj")
    existing_dir = proj / ".kiro" / "settings"
    existing_dir.mkdir(parents=True)
    existing = existing_dir / "mcp.json"
    existing.write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}}),
                        encoding="utf-8")
    res = wire_agent("kiro", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    assert len(res["backups"]) == 1
    data = json.loads(existing.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    assert data["mcpServers"]["other"] == {"command": "other"}  # sibling preserved
    entry = data["mcpServers"]["cognigraph"]
    assert entry["args"][0].endswith("server.py")
    assert entry["env"]["MEMORY_DB_PATH"].endswith(".cognigraph/memory.db")
    assert str(existing) in res["config_files"]


def test_detect_agent_kiro_marker(tmp_path):
    proj = _make_proj(tmp_path, "kiro_detect")
    empty_home = tmp_path / "kiro_empty_home"
    empty_home.mkdir(exist_ok=True)
    (proj / ".kiro").mkdir()
    assert detect_agent(proj, env={}, home=empty_home) == "kiro"


def test_wire_vscode_dynamic_and_portable(tmp_path):
    """VS Code: DYNAMIC .vscode/mcp.json (servers+type) + STATIC .mcp.json."""
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path, "vscode_proj")
    vsc = proj / ".vscode"
    vsc.mkdir()
    existing = vsc / "mcp.json"
    existing.write_text(json.dumps({"servers": {"other": {"type": "stdio",
                                                          "command": "other"}}}),
                        encoding="utf-8")
    res = wire_agent("vscode", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    data = json.loads(existing.read_text(encoding="utf-8"))
    assert "mcpServers" not in data  # VS Code uses `servers`, never `mcpServers`
    assert data["servers"]["other"] == {"type": "stdio", "command": "other"}
    entry = data["servers"]["cognigraph"]
    assert entry["type"] == "stdio"
    assert entry["cwd"] == "${workspaceFolder}"
    assert entry["env"]["MEMORY_DB_PATH"] == "${workspaceFolder}/.cognigraph/memory.db"
    assert entry["args"][0].endswith("server.py")
    # Portable Agent Host fallback carries the STATIC resolved absolute path.
    portable = json.loads((proj / ".mcp.json").read_text(encoding="utf-8"))
    p_entry = portable["mcpServers"]["cognigraph"]
    assert p_entry["env"]["MEMORY_DB_PATH"].endswith(".cognigraph/memory.db")
    assert "${workspaceFolder}" not in p_entry["env"]["MEMORY_DB_PATH"]
    assert str(existing) in res["config_files"]
    assert str(proj / ".mcp.json") in res["config_files"]


def test_detect_agent_vscode_marker(tmp_path):
    proj = _make_proj(tmp_path, "vscode_detect")
    empty_home = tmp_path / "vscode_empty_home"
    empty_home.mkdir(exist_ok=True)
    (proj / ".vscode").mkdir()
    assert detect_agent(proj, env={}, home=empty_home) == "vscode"


def test_merge_json_nested_dotted_key(tmp_path):
    """Dotted top_key merges into nested mappings (ZCode mcp.servers)."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"mcp": {"servers": {"other": {"command": "o"}},
                                       "other_top": 1},
                               "mcpServers": {"stale": True}}),
                   encoding="utf-8")
    _merge_json(cfg, "mcp.servers", {"command": "py"})
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "mcpServers" not in data  # compat key dropped, no disagreement
    assert data["mcp"]["servers"]["other"] == {"command": "o"}
    assert data["mcp"]["other_top"] == 1
    assert data["mcp"]["servers"]["cognigraph"] == {"command": "py"}


def test_wire_zcode_workspace_config(tmp_path, monkeypatch):
    """ZCode writes workspace .zcode/config.json, never .agents/mcp.json."""
    fake_home = tmp_path / "zhome"
    fake_home.mkdir()
    monkeypatch.setattr(wire.Path, "home", lambda: fake_home)
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path, "zcode_proj")
    zdir = proj / ".zcode"
    zdir.mkdir()
    existing = zdir / "config.json"
    existing.write_text(json.dumps({"mcp": {"servers": {"other": {"command": "o"}}}}),
                        encoding="utf-8")
    res = wire_agent("zcode", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    assert res["warnings"] == []
    data = json.loads(existing.read_text(encoding="utf-8"))
    assert data["mcp"]["servers"]["other"] == {"command": "o"}  # sibling kept
    entry = data["mcp"]["servers"]["cognigraph"]
    assert entry["args"][0].endswith("server.py")
    assert entry["env"]["MEMORY_DB_PATH"].endswith(".cognigraph/memory.db")
    assert not (proj / ".agents" / "mcp.json").exists()
    assert str(existing) in res["config_files"]
    assert list(res["mcp_snippet"].keys()) == ["mcp"]


def test_wire_zcode_user_shadow_warning(tmp_path, monkeypatch):
    """A user-global cognigraph entry triggers an unmissable warning."""
    fake_home = tmp_path / "zhome2"
    user_cli = fake_home / ".zcode" / "cli"
    user_cli.mkdir(parents=True)
    (user_cli / "config.json").write_text(json.dumps(
        {"mcp": {"servers": {"cognigraph": {"command": "stale"}}}}),
        encoding="utf-8")
    monkeypatch.setattr(wire.Path, "home", lambda: fake_home)
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path, "zcode_shadow_proj")
    res = wire_agent("zcode", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["status"] == "WIRED"
    assert len(res["warnings"]) == 1
    assert "SHADOWING RISK" in res["warnings"][0]
    assert any("SHADOWING RISK" in s for s in res["next_steps"])
    # Workspace write still lands (ready once the user cleans the global).
    data = json.loads((proj / ".zcode" / "config.json").read_text(encoding="utf-8"))
    assert "cognigraph" in data["mcp"]["servers"]


def test_detect_agent_zcode_marker(tmp_path):
    proj = _make_proj(tmp_path, "zcode_detect")
    empty_home = tmp_path / "zcode_empty_home"
    empty_home.mkdir(exist_ok=True)
    (proj / ".zcode").mkdir()
    assert detect_agent(proj, env={}, home=empty_home) == "zcode"


def test_wire_alias_normalization(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(wire.Path, "home", lambda: fake_home)
    pm = _make_pm_root(tmp_path)
    proj = _make_proj(tmp_path)
    res = wire_agent("gemini", project_root=str(proj), project_memory_root=str(pm),
                     drop_protocol=False)
    assert res["agent"] == "antigravity"
    assert (proj / ".agents" / "plugins" / "cognigraph" / "mcp_config.json").exists()
    assert not (fake_home / ".gemini" / "config" / "mcp_config.json").exists()


def test_detect_agent_from_env(tmp_path):
    proj = _make_proj(tmp_path)
    assert detect_agent(proj, env={"ANTIGRAVITY_PID": "1234"}, home=tmp_path / "empty_home") == "antigravity"
    assert detect_agent(proj, env={"CURSOR_PROJECT_DIR": str(proj)}, home=tmp_path / "empty_home") == "cursor"
    assert detect_agent(proj, env={"WINDSURF_PID": "5678"}, home=tmp_path / "empty_home") == "windsurf"
    assert detect_agent(proj, env={"ZED_TERM": "true"}, home=tmp_path / "empty_home") == "zed"
    assert detect_agent(proj, env={"CLAUDE_DESKTOP": "1"}, home=tmp_path / "empty_home") == "claude"
    assert detect_agent(proj, env={"OPENCODE": "1"}, home=tmp_path / "empty_home") == "opencode"


def test_detect_agent_from_project_dirs(tmp_path):
    proj = _make_proj(tmp_path)
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir(exist_ok=True)
    (proj / ".roo").mkdir()
    assert detect_agent(proj, env={}, home=empty_home) == "roo"

    shutil.rmtree(proj / ".roo")
    (proj / ".cursor").mkdir()
    assert detect_agent(proj, env={}, home=empty_home) == "cursor"

    shutil.rmtree(proj / ".cursor")
    (proj / ".cline").mkdir()
    assert detect_agent(proj, env={}, home=empty_home) == "cline"

    shutil.rmtree(proj / ".cline")
    (proj / ".zed").mkdir()
    assert detect_agent(proj, env={}, home=empty_home) == "zed"

    shutil.rmtree(proj / ".zed")
    (proj / "opencode.json").write_text("{}", encoding="utf-8")
    assert detect_agent(proj, env={}, home=empty_home) == "opencode"

    (proj / "opencode.json").unlink()
    (proj / ".agents").mkdir()
    assert detect_agent(proj, env={}, home=empty_home) == "antigravity"


def test_detect_agent_from_home_fallback(tmp_path):
    proj = _make_proj(tmp_path)
    fake_gemini_home = tmp_path / "gemini_home"
    (fake_gemini_home / ".gemini").mkdir(parents=True)
    assert detect_agent(proj, env={}, home=fake_gemini_home) == "antigravity"

    fake_windsurf_home = tmp_path / "windsurf_home"
    (fake_windsurf_home / ".codeium" / "windsurf").mkdir(parents=True)
    assert detect_agent(proj, env={}, home=fake_windsurf_home) == "windsurf"


def test_prompt_agent_choice():
    # 1. User accepts default detected choice by pressing Enter
    got = prompt_agent_choice(detected="antigravity", input_fn=lambda _: "", print_fn=lambda *a: None)
    assert got == "antigravity"

    # 2. User types a numeric choice
    got2 = prompt_agent_choice(detected=None, input_fn=lambda _: "2", print_fn=lambda *a: None)
    assert got2 == "cursor"

    # 3. User types an agent alias
    got3 = prompt_agent_choice(detected=None, input_fn=lambda _: "gemini", print_fn=lambda *a: None)
    assert got3 == "antigravity"


# --------------------------------------------------------------------------
# bootstrap pure scan helpers
# --------------------------------------------------------------------------

def test_iter_files_prunes_and_skips(tmp_path):
    _demo_tree(tmp_path)
    rels = {str(rel).replace("\\", "/") for rel, _ in bootstrap._iter_files(tmp_path)}
    assert "node_modules/evil.js" not in rels
    assert "stray.pyc" not in rels
    assert "package.json" in rels and "src/app.py" in rels


def test_small_enough_missing_is_false(tmp_path):
    assert bootstrap._small_enough(tmp_path / "gone.py") is False


def test_scan_file_tree_dry_run(tmp_path):
    _demo_tree(tmp_path)
    facts, total = bootstrap.scan_file_tree(tmp_path, dry_run=True)
    assert total >= 4
    key = [f for f in facts if "Key file" in f["content"]]
    paths = {f["file_path"] for f in key}
    assert "package.json" in paths and "README.md" in paths
    assert any(f["content"].startswith("Directory ") for f in facts)


def test_scan_file_tree_template_facts_are_not_boosted_categories(tmp_path):
    """Key-file, Source-file and Directory facts are template-derived and must
    not sit in component/decision/constraint, where intent boosts would rank
    them above curated knowledge (the Phase-3 corpus pollution)."""
    _demo_tree(tmp_path)
    facts, _ = bootstrap.scan_file_tree(tmp_path, full=True, dry_run=True)
    templated = [f for f in facts
                 if f["content"].startswith(("Key file:", "Source file:", "Directory "))]
    assert templated, "demo tree should produce template facts"
    assert all(f["category"] == "other" for f in templated)
    manifests = bootstrap.parse_manifests(tmp_path)
    assert all(f["category"] == "component" for f in manifests)  # curated: unchanged


def test_parse_manifests(tmp_path):
    _demo_tree(tmp_path)
    facts = bootstrap.parse_manifests(tmp_path)
    by_path = {f["file_path"]: f for f in facts}
    assert "'demo' v1.0.0" in by_path["package.json"]["content"]
    assert "2 deps" in by_path["requirements.txt"]["content"]  # comment/blank skipped
    assert bootstrap.parse_manifests(tmp_path / "missing") == []


def test_parse_manifests_corrupt_json_never_raises(tmp_path):
    (tmp_path / "package.json").write_text("{broken", encoding="utf-8")
    assert bootstrap.parse_manifests(tmp_path) == []


def test_extract_docs_categories_and_truncation(tmp_path):
    _demo_tree(tmp_path)
    facts = bootstrap.extract_docs(tmp_path)
    by_head = {}
    for f in facts:
        head = f["content"].split(":")[0]
        by_head[head] = f["category"]
    assert by_head["README.md § Decision log"] == "decision"
    assert by_head["README.md § Rules"] == "constraint"
    assert by_head["README.md § Misc"] == "other"
    short = bootstrap.extract_docs(tmp_path, max_chars=10)
    assert any("…(truncated)" in f["content"] for f in short)


def test_scan_git_no_git(tmp_path):
    out = bootstrap.scan_git(tmp_path)
    assert out == {"available": False, "branch": "", "recent": [], "tagged": []}


def test_scan_git_real_repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git binary unavailable")
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess
    for args in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["commit", "--allow-empty", "-m", "[decision] Choose X"]):
        subprocess.run(["git"] + args, cwd=repo, check=True, capture_output=True, timeout=30)
    out = bootstrap.scan_git(repo)
    assert out["available"] is True and out["recent"]
    assert any(t["tag"] == "decision" for t in out["tagged"])


def test_install_git_hook_states(tmp_path):
    assert bootstrap.install_git_hook(tmp_path) == "skipped_no_git"
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    assert bootstrap.install_git_hook(tmp_path) == "installed"
    target = hooks_dir / "post-commit"
    src = Path(bootstrap.__file__).resolve().parent / "hooks" / "git_hook.py"
    assert target.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")
    if sys.platform != "win32":
        # Git only fires hooks with the executable bit set (POSIX); Windows
        # has no exec bit, so this assertion is POSIX-only by design.
        assert target.stat().st_mode & 0o111, "installed hook must be executable"
    assert bootstrap.install_git_hook(tmp_path) == "skipped_hook_exists"


def test_drop_agents_md(tmp_path):
    assert bootstrap.drop_agents_md(tmp_path) == "created"
    assert (tmp_path / "AGENTS.md").exists()
    assert bootstrap.drop_agents_md(tmp_path) == "up_to_date"


def test_bootstrap_dry_run_previews_without_touching(tmp_path):
    _demo_tree(tmp_path)
    store = FakeStore()
    res = bootstrap.bootstrap_project(tmp_path, store=store, dry_run=True)
    assert res["status"] == "DRY_RUN"
    assert res["planned_facts"] > 0
    assert res["total_files_seen"] >= 4
    assert res["git_available"] is False
    assert res["preview"] and all(p.startswith("[") for p in res["preview"])
    assert store.writes == []  # dry-run writes nothing
    assert not (tmp_path / "AGENTS.md").exists()  # ... and touches nothing
    with pytest.raises(ValueError, match="project root does not exist"):
        bootstrap.bootstrap_project(tmp_path / "missing", store=FakeStore(), dry_run=True)


def test_bootstrap_apply_with_fake_store(tmp_path):
    _demo_tree(tmp_path)
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    store = FakeStore()
    res = bootstrap.bootstrap_project(tmp_path, store=store, dry_run=False,
                                      install_hook=True, drop_protocol=True)
    assert res["status"] == "BOOTSTRAPPED"
    assert res["facts_written"] == 0  # FakeStore reports every write as duplicate
    assert res["duplicates_skipped"] > 0
    assert res["refused"] == 0
    assert store.linked is True
    assert res["git_hook"] == "installed"
    assert res["agents_md"] == "created"
    # max_facts cap is honored
    res2 = bootstrap.bootstrap_project(tmp_path, store=FakeStore(), dry_run=True, max_facts=2)
    assert res2["planned_facts"] == 2


# --------------------------------------------------------------------------
# hooks/git_hook.py (MemoryStore + git metadata both stubbed)
# --------------------------------------------------------------------------

def _load_hook():
    import importlib
    return importlib.import_module("hooks.git_hook")


class FakeHookStore:
    instances = []

    def __init__(self, *a, **k):
        self.writes = []
        FakeHookStore.instances.append(self)

    def write(self, **kwargs):
        self.writes.append(kwargs)
        return len(self.writes)

    def close(self):
        pass


@pytest.fixture
def hook_env(monkeypatch):
    gh = _load_hook()
    FakeHookStore.instances.clear()
    monkeypatch.setattr("memory_store.MemoryStore", FakeHookStore)
    return gh


def test_hook_no_tags_writes_nothing(hook_env, monkeypatch):
    monkeypatch.setattr(hook_env, "get_latest_commit_info",
                        lambda: ("plain commit, no tags", "abc123"))
    assert hook_env.main() == 0
    assert FakeHookStore.instances == []  # store never even opened


def test_hook_multi_tag_captures_all(hook_env, monkeypatch):
    monkeypatch.setattr(hook_env, "get_latest_commit_info",
                        lambda: ("[decision] Use X\ncontinuation\n[arch] Core Y [state] Live",
                                 "deadbeefcafe"))
    assert hook_env.main() == 0
    writes = FakeHookStore.instances[0].writes
    assert [w["category"] for w in writes] == ["decision", "component", "state"]  # arch→component
    assert all(w["content"].startswith("Git commit (deadbee)") for w in writes)
    assert "continuation" in writes[0]["content"]  # body spans newlines
    assert all(w["git_commit_sha"] == "deadbeefcafe" for w in writes)


def test_hook_unknown_brackets_stay_text(hook_env, monkeypatch):
    monkeypatch.setattr(hook_env, "get_latest_commit_info",
                        lambda: ("[decision] See [docs] for why", "abc123"))
    assert hook_env.main() == 0
    writes = FakeHookStore.instances[0].writes
    assert len(writes) == 1 and "[docs]" in writes[0]["content"]


def test_hook_never_fails_commit(hook_env, monkeypatch):
    # DB unavailable → return 0, no raise.
    class DeadStore:
        def __init__(self, *a, **k):
            raise RuntimeError("locked")
    monkeypatch.setattr("memory_store.MemoryStore", DeadStore)
    monkeypatch.setattr(hook_env, "get_latest_commit_info",
                        lambda: ("[decision] X", "abc123"))
    assert hook_env.main() == 0
    # Git itself failing → return 0, no raise.
    def boom():
        raise RuntimeError("no git")
    monkeypatch.setattr(hook_env, "get_latest_commit_info", boom)
    assert hook_env.main() == 0


def test_hook_tag_map_covers_all_known_tags():
    gh = _load_hook()
    assert set(gh.TAG_MAP) == {"decision", "constraint", "component", "arch", "state"}

