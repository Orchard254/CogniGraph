"""
Project bootstrap / onboarding pipeline for project-memory.

Scans a target repository and ingests structural + documentary context into the
memory database, installs the git capture hook, and drops the agent protocol file —
so any coding agent starting on the project inherits full context from the first action.

Idempotent: re-running is safe — the auto-dedup guard skips facts that already exist.
"""
import os
import re
import json
import subprocess
import logging
import hashlib
from pathlib import Path
from typing import Dict, Any, List, Tuple

from memory_store import MemoryStore
from structural_extract import extract_structures

logger = logging.getLogger(__name__)

IGNORE_DIRS = {
    ".git", "venv", ".venv", "node_modules", "__pycache__", ".project-memory",
    "dist", "build", "target", ".next", ".cache", ".idea", ".vscode",
    "site-packages", ".pytest_cache", ".mypy_cache", "coverage", ".opencode",
}
IGNORE_FILE_SUFFIXES = {
    ".pyc", ".pyo", ".lock", ".sum", ".min.js", ".min.css", ".map",
    ".whl", ".egg", ".sqlite", ".db", ".log", ".png", ".jpg", ".jpeg",
    ".gif", ".ico", ".woff", ".woff2", ".ttf", ".pdf", ".zip",
}
CODE_LANGS = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".rs": "rust", ".go": "go", ".java": "java",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".sh": "shell", ".ps1": "powershell",
}
ENTRYPOINT_NAMES = {
    "main.py", "app.py", "server.py", "manage.py", "cli.py", "__main__.py",
    "index.ts", "index.js", "main.ts", "main.js", "main.rs", "main.go",
}
MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "setup.py", "requirements.txt",
    "cargo.toml", "go.mod", "pom.xml", "composer.json",
}
DOC_NAMES = {
    "readme.md", "readme.rst", "agents.md", "claude.md",
    "contributing.md", "architecture.md", "knowledge.md", "decisions.md",
}
TAG_MAP = {"decision": "decision", "constraint": "constraint",
           "component": "component", "arch": "component", "state": "state"}


def _iter_files(root: Path):
    """Walk the repo with directory pruning (never descends into IGNORE_DIRS)."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in IGNORE_DIRS and not entry.name.endswith(".egg-info"):
                    stack.append(entry)
            elif entry.is_file() and entry.suffix.lower() not in IGNORE_FILE_SUFFIXES:
                yield entry.relative_to(root), entry


def scan_file_tree(root: Path, full: bool = False, store: MemoryStore = None, dry_run: bool = False) -> Tuple[List[Dict[str, Any]], int]:
    """Directory summary facts + key-file facts. `full=True` adds per-file facts.
    
    If store is provided and not dry_run, also extracts and syncs structural nodes
    (functions, classes, methods, imports) per file.
    """
    dir_stats: Dict[str, Dict[str, int]] = {}
    facts: List[Dict[str, Any]] = []
    total_files = 0
    for rel, p in _iter_files(root):
        total_files += 1
        top = rel.parts[0] if len(rel.parts) > 1 else "(root)"
        st = dir_stats.setdefault(top, {"files": 0, "lines": 0})
        st["files"] += 1
        if p.suffix.lower() in CODE_LANGS:
            try:
                if p.stat().st_size < 1_000_000:
                    with p.open(encoding="utf-8", errors="replace") as fh:
                        st["lines"] += sum(1 for _ in fh)
            except OSError:
                pass
        name = p.name.lower()
        if name in ENTRYPOINT_NAMES or name in MANIFEST_NAMES or name in DOC_NAMES:
            try:
                if p.stat().st_size < 1_000_000:
                    data = p.read_bytes()
                    h = hashlib.sha256(data).hexdigest()
                    facts.append({
                        "category": "component",
                        "content": f"Key file: {str(rel).replace(os.sep, '/')} (hash {h[:8]})",
                        "tags": "bootstrap, key-file",
                        "importance": 3,
                        "file_path": str(rel).replace(os.sep, "/"),
                        "code_hash": h,
                    })
                else:
                    facts.append({
                        "category": "component",
                        "content": f"Key file: {str(rel).replace(os.sep, '/')}",
                        "tags": "bootstrap, key-file",
                        "importance": 3,
                        "file_path": str(rel).replace(os.sep, "/"),
                    })
            except OSError:
                facts.append({
                    "category": "component",
                    "content": f"Key file: {str(rel).replace(os.sep, '/')}",
                    "tags": "bootstrap, key-file",
                    "importance": 3,
                    "file_path": str(rel).replace(os.sep, "/"),
                })
        if full and p.suffix.lower() in CODE_LANGS:
            # Generate distinctive content with hash + line count to avoid audit false positives and enable provenance
            try:
                if p.stat().st_size < 1_000_000:
                    data = p.read_bytes()
                    h = hashlib.sha256(data).hexdigest()
                    lines = data.decode('utf-8', errors='replace').count('\n') + 1
                    facts.append({
                        "category": "component",
                        "content": f"Source file: {str(rel).replace(os.sep, '/')} ({CODE_LANGS[p.suffix.lower()]}, {lines} lines, hash {h[:8]})",
                        "tags": "bootstrap, file",
                        "importance": 2,
                        "file_path": str(rel).replace(os.sep, "/"),
                        "line_start": 1,
                        "line_end": lines,
                        "code_hash": h,
                    })
                else:
                    facts.append({
                        "category": "component",
                        "content": f"Source file: {str(rel).replace(os.sep, '/')} ({CODE_LANGS[p.suffix.lower()]}, large file)",
                        "tags": "bootstrap, file",
                        "importance": 2,
                        "file_path": str(rel).replace(os.sep, "/"),
                    })
            except OSError:
                facts.append({
                    "category": "component",
                    "content": f"Source file: {str(rel).replace(os.sep, '/')} ({CODE_LANGS[p.suffix.lower()]})",
                    "tags": "bootstrap, file",
                    "importance": 2,
                    "file_path": str(rel).replace(os.sep, "/"),
                })
        
        # Function-level provenance: when full, create per-function/class/method facts with line ranges
        if full and p.suffix.lower() in CODE_LANGS and p.stat().st_size < 1_000_000:
            try:
                nodes = extract_structures(p)
            except NotImplementedError:
                nodes = []
            # Limit to top 5 nodes per file to avoid explosion (max 5*327=1635)
            try:
                for n in nodes[:5]:
                    if n["type"] in ("function", "method", "class"):
                        # Compute snippet hash like sync_structural_nodes
                        try:
                            data = p.read_bytes()
                            text = data.decode('utf-8', errors='replace')
                            lines = text.splitlines(keepends=True)
                            l_start = max(0, n["line_start"]-1)
                            l_end = min(len(lines), n["line_end"])
                            snippet = "".join(lines[l_start:l_end])
                            h = hashlib.sha256(snippet.encode('utf-8')).hexdigest()
                            cname = f"{n.get('class_name','')}.{n['name']}" if n.get('class_name') else n['name']
                            facts.append({
                                "category": "component",
                                "content": f"{n['type'].title()} {cname} in {str(rel).replace(os.sep, '/')} (lines {n['line_start']}-{n['line_end']}, hash {h[:8]})",
                                "tags": "bootstrap, structural, auto",
                                "importance": 2,
                                "file_path": str(rel).replace(os.sep, "/"),
                                "line_start": n["line_start"],
                                "line_end": n["line_end"],
                                "code_hash": h,
                            })
                        except OSError:
                            pass
            except Exception as e:
                logger.debug("Function-level provenance failed for %s: %r", rel, e)

        # Structural extraction: extract and sync functions/classes/methods/imports
        if store and not dry_run and p.suffix.lower() in CODE_LANGS:
            try:
                if p.stat().st_size < 1_000_000:  # same size limit
                    try:
                        nodes = extract_structures(p)
                    except NotImplementedError:
                        # Unsupported extension — skip silently
                        nodes = []
                    # Call sync unconditionally (even with empty nodes) so stale-pruning runs
                    store.sync_structural_nodes(str(rel).replace(os.sep, "/"), nodes, project_root=str(root))
            except Exception as e:
                # Log warning but don't fail bootstrap
                logger.warning("Structural extraction/sync failed for %s: %s", rel, e)
    
    for top, st in sorted(dir_stats.items()):
        facts.append({
            "category": "component",
            "content": f"Directory '{top}/': {st['files']} files, ~{st['lines']} code lines",
            "tags": "bootstrap, structure",
            "importance": 2,
        })
    return facts, total_files


def parse_manifests(root: Path) -> List[Dict[str, Any]]:
    """Extract dependency/script facts from project manifests."""
    facts: List[Dict[str, Any]] = []
    pj = root / "package.json"
    if pj.exists():
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
            deps = list((data.get("dependencies") or {}).keys())
            scripts = list((data.get("scripts") or {}).keys())
            facts.append({
                "category": "component",
                "content": (f"package.json: '{data.get('name', '?')}' v{data.get('version', '?')}. "
                            f"deps({len(deps)}): {', '.join(deps[:15])}. scripts: {', '.join(scripts[:10])}"),
                "tags": "bootstrap, manifest, npm", "importance": 4, "file_path": "package.json",
            })
        except Exception:
            pass
    rt = root / "requirements.txt"
    if rt.exists():
        try:
            lines = [l.strip() for l in rt.read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.strip().startswith("#")]
            facts.append({
                "category": "component",
                "content": f"requirements.txt: {len(lines)} deps: {', '.join(lines[:20])}",
                "tags": "bootstrap, manifest, pip", "importance": 4, "file_path": "requirements.txt",
            })
        except Exception:
            pass
    pp = root / "pyproject.toml"
    if pp.exists():
        try:
            import tomllib
            data = tomllib.loads(pp.read_text(encoding="utf-8"))
            proj = data.get("project", {})
            facts.append({
                "category": "component",
                "content": (f"pyproject.toml: '{proj.get('name', '?')}' — {proj.get('description', '')[:120]}. "
                            f"deps: {', '.join(proj.get('dependencies', [])[:15])}"),
                "tags": "bootstrap, manifest, python", "importance": 4, "file_path": "pyproject.toml",
            })
        except Exception:
            pass
    return facts


def extract_docs(root: Path, max_chars: int = 900) -> List[Dict[str, Any]]:
    """Split markdown docs (repo root + docs/) into heading-based facts."""
    facts: List[Dict[str, Any]] = []
    scan_dirs = [root]
    docs_dir = root / "docs"
    if docs_dir.is_dir():
        scan_dirs.append(docs_dir)
    for d in scan_dirs:
        for p in sorted(d.iterdir()):
            if not (p.is_file() and p.name.lower() in DOC_NAMES):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for sec in re.split(r"\n(?=#{1,3} )", text):
                sec = sec.strip()
                if not sec:
                    continue
                head = sec.splitlines()[0].lstrip("# ").strip() or p.name
                body = sec if len(sec) <= max_chars else sec[:max_chars] + " …(truncated)"
                if re.search(r"(?i)rule|constraint|must|never|forbidden|protocol", head):
                    category = "constraint"
                elif re.search(r"(?i)decision|architecture|design|why|rationale", head):
                    category = "decision"
                else:
                    category = "other"
                facts.append({
                    "category": category,
                    "content": f"{p.name} § {head}: {body}",
                    "tags": f"bootstrap, docs, {p.stem.lower()}",
                    "importance": 3,
                    "file_path": str(p.relative_to(root)).replace(os.sep, "/"),
                })
    return facts


def scan_git(root: Path, commit_limit: int = 30) -> Dict[str, Any]:
    """Branch + recent commits + tagged commits from git history."""
    if not (root / ".git").exists():
        return {"available": False, "branch": "", "recent": [], "tagged": []}
    out: Dict[str, Any] = {"available": True, "branch": "", "recent": [], "tagged": []}
    try:
        out["branch"] = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root, text=True, encoding="utf-8", stderr=subprocess.DEVNULL).strip()
        log = subprocess.check_output(
            ["git", "log", f"-{commit_limit}", "--pretty=%H|%s"],
            cwd=root, text=True, encoding="utf-8", stderr=subprocess.DEVNULL)
        tag_re = re.compile(r"\[(decision|constraint|component|arch|state)\]\s*(.*)", re.IGNORECASE)
        for line in log.splitlines():
            sha, _, subject = line.partition("|")
            out["recent"].append({"sha": sha[:7], "subject": subject})
            m = tag_re.search(subject)
            if m:
                out["tagged"].append({"sha": sha, "tag": m.group(1).lower(),
                                      "content": m.group(2).strip()})
    except Exception:
        pass
    return out


def install_git_hook(root: Path) -> str:
    hooks_dir = root / ".git" / "hooks"
    if not hooks_dir.exists():
        return "skipped_no_git"
    target = hooks_dir / "post-commit"
    if target.exists():
        return "skipped_hook_exists"
    src = Path(__file__).resolve().parent / "hooks" / "git_hook.py"
    if not src.exists():
        return "skipped_hook_source_missing"
    target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return "installed"


def drop_agents_md(root: Path) -> str:
    target = root / "AGENTS.md"
    if target.exists():
        return "skipped_exists"
    src = Path(__file__).resolve().parent / "AGENTS.md"
    if not src.exists():
        return "skipped_no_template"
    target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return "created"


def bootstrap_project(root: Any, store: MemoryStore = None, dry_run: bool = True,
                      full: bool = False, max_facts: int = 150,
                      install_hook: bool = True, drop_protocol: bool = True) -> Dict[str, Any]:
    """
    Onboard a repository into project memory.
    dry_run=True (default) previews without writing. Apply writes facts,
    installs the git hook, and drops AGENTS.md if absent. Idempotent.
    
    Also extracts and syncs structural nodes (functions/classes/methods/imports)
    per file, then links existing episodic facts to those structural nodes.
    """
    root_path = Path(root or Path.cwd()).resolve()
    if not root_path.is_dir():
        raise ValueError(f"project root does not exist: {root_path}")
    own_store = store is None
    store = store or MemoryStore()
    try:
        # Apply-path side effects run BEFORE the scan so a freshly dropped AGENTS.md
        # is ingested by this same run — re-runs then write nothing new (idempotent).
        # Dry-run never touches the target repo.
        if not dry_run:
            hook_status = install_git_hook(root_path) if install_hook else "skipped_disabled"
            agents_status = drop_agents_md(root_path) if drop_protocol else "skipped_disabled"

        planned: List[Dict[str, Any]] = [{
            "category": "component",
            "content": f"Project root: {root_path.name} at {root_path}",
            "tags": "bootstrap, identity", "importance": 4,
        }]
        # Pass store and dry_run so structural extraction runs during the scan
        struct_facts, total_files = scan_file_tree(root_path, full=full, store=store, dry_run=dry_run)
        planned.extend(struct_facts)
        planned.extend(parse_manifests(root_path))
        planned.extend(extract_docs(root_path))
        git_info = scan_git(root_path)
        if git_info["available"]:
            latest = git_info["recent"][0] if git_info["recent"] else None
            planned.append({
                "category": "state",
                "content": (f"Git branch '{git_info['branch']}'"
                            + (f", last commit {latest['sha']}: {latest['subject'][:120]}" if latest else "")),
                "tags": "bootstrap, git", "importance": 3,
            })
            for t in git_info["tagged"]:
                planned.append({
                    "category": TAG_MAP.get(t["tag"], "other"),
                    "content": f"Git commit ({t['sha'][:7]}): {t['content']}",
                    "rationale": "Tagged commit captured during bootstrap",
                    "confidence": "confirmed",
                    "tags": "bootstrap, git, auto-capture",
                    "git_commit_sha": t["sha"], "importance": 3,
                })
        planned = planned[:max_facts]

        if dry_run:
            return {
                "status": "DRY_RUN",
                "root": str(root_path),
                "planned_facts": len(planned),
                "total_files_seen": total_files,
                "git_available": git_info["available"],
                "preview": [f"[{p['category']}] {p['content'][:100]}" for p in planned[:20]],
            }

        written: List[int] = []
        dups = refused = 0
        # Batch write without per-fact embedding to avoid 120s timeout on large full bootstrap;
        # embeddings are backfilled in a single batched pass afterwards.
        for f in planned:
            try:
                fid = store.write(**f, embed=False)
                if fid < 0:
                    dups += 1
                else:
                    written.append(fid)
            except ValueError:
                refused += 1
        # Batched embedding backfill for newly written facts (single model load, batched encode)
        if written:
            try:
                # Use the store's batched semantic path if available
                from memory_store import _get_embed_model, _vec_to_blob, _EMBED_MODEL_NAME
                model = _get_embed_model()
                if model is not None:
                    # Fetch texts for written ids in one query
                    placeholders = ",".join("?" * len(written))
                    rows = store.conn.execute(f"SELECT id, content, rationale FROM memory WHERE id IN ({placeholders})", written).fetchall()
                    id_to_text = {r["id"]: f"{r['content']} {r['rationale'] or ''}" for r in rows}
                    texts = [id_to_text[fid] for fid in written if fid in id_to_text]
                    ids_in_order = [fid for fid in written if fid in id_to_text]
                    # Batch encode (fastembed supports batch)
                    if texts:
                        vecs = list(model.embed(texts))
                        for fid, vec in zip(ids_in_order, vecs):
                            try:
                                import numpy as np
                                vec_arr = np.asarray(vec, dtype="float32")
                                norm = np.linalg.norm(vec_arr)
                                if norm > 0:
                                    vec_arr = vec_arr / norm
                                blob = vec_arr.astype("float32").tobytes()
                                store.conn.execute("INSERT OR REPLACE INTO memory_embeddings (fact_id, model, embedding) VALUES (?, ?, ?)", (fid, _EMBED_MODEL_NAME, blob))
                                # Keep vec_memory in sync if triggers unavailable
                                if getattr(store, '_vec_available', False) and not getattr(store, '_vec_triggers', False):
                                    try:
                                        store.conn.execute("INSERT OR REPLACE INTO vec_memory(rowid, embedding) VALUES (?, ?)", (fid, blob))
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        store.conn.commit()
                else:
                    # Fallback to per-fact _store_embedding if model not available
                    for fid in written:
                        row = store.conn.execute("SELECT content, rationale FROM memory WHERE id=?", (fid,)).fetchone()
                        if row:
                            try:
                                store._store_embedding(fid, f"{row['content']} {row['rationale'] or ''}")
                            except Exception:
                                pass
            except Exception as e:
                logger.warning("bootstrap batched embedding backfill failed: %r", e)
        
        # After writing episodic facts, link them to structural nodes (full relink)
        # NOTE: On a fresh project, most episodic facts are written during actual coding
        # sessions after bootstrap, not during bootstrap itself. So this initial link
        # pass may find little. Incremental re-linking as new facts get written is
        # a follow-up, not part of this task.
        if not dry_run:
            store.link_facts_to_structural_nodes()

        return {
            "status": "BOOTSTRAPPED",
            "root": str(root_path),
            "facts_written": len(written),
            "duplicates_skipped": dups,
            "refused": refused,
            "total_files_seen": total_files,
            "git_hook": hook_status,
            "agents_md": agents_status,
            "fact_ids": written,
        }
    finally:
        if own_store:
            store.close()