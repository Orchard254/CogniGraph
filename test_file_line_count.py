"""Regression tests for physical file line counting in bootstrap/watcher source stubs."""
import bootstrap
import watcher


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, existing=()):
        self.existing = list(existing)
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((sql, tuple(params)))
        if sql.startswith("SELECT id, code_hash"):
            return FakeResult(self.existing)
        return FakeResult([])


class FakeStore:
    def __init__(self, db_path=":fake:"):
        self.db_path = db_path
        self.conn = FakeConn()
        self.writes = []
        self.synced_nodes = []
        self.re_embedded = []
        self.linked = []
        self.deleted = []
        self.closed = False

    def sync_structural_nodes(self, rel, nodes, project_root=None):
        self.synced_nodes.append((rel, nodes))

    def write(self, **kwargs):
        self.writes.append(kwargs)
        return 99

    def link_facts_to_structural_nodes(self, rel=None):
        self.linked.append(rel)

    def _store_embedding(self, fid, content):
        self.re_embedded.append((fid, content))
        return True

    def close(self):
        self.closed = True


def test_bootstrap_two_line_file(tmp_path):
    (tmp_path / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    facts, _ = bootstrap.scan_file_tree(tmp_path, full=True, dry_run=True)
    src = [f for f in facts if str(f.get("content", "")).startswith("Source file: a.py")]
    assert len(src) == 1
    f = src[0]
    assert "2 lines" in f["content"]
    assert f["line_start"] == 1
    assert f["line_end"] == 2


def test_watcher_two_line_file(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "MemoryStore", None)
    monkeypatch.setattr(watcher, "extract_structures", lambda p: [])
    pyfile = tmp_path / "svc.py"
    pyfile.write_text("def helper():\n    return 1\n", encoding="utf-8")
    store = FakeStore()
    watcher._sync_file(pyfile, tmp_path, store)
    assert len(store.writes) == 1
    w = store.writes[0]
    assert "2 lines" in w["content"]
    assert w["line_end"] == 2


def test_bootstrap_empty_file(tmp_path):
    (tmp_path / "empty.py").write_text("", encoding="utf-8")
    facts, _ = bootstrap.scan_file_tree(tmp_path, full=True, dry_run=True)
    src = [f for f in facts if str(f.get("content", "")).startswith("Source file: empty.py")]
    assert len(src) == 1
    f = src[0]
    assert "0 lines" in f["content"]
    assert f["line_end"] == 0
    assert f["line_end"] != 1