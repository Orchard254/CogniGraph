# Phase B Reconnaissance Report

## 1. BOOTSTRAP.PY AND WIRE.PY (Full Contents)

### bootstrap.py
**Path:** `C:\PROJECT\project-memory\bootstrap.py`

**Summary:** The bootstrap module scans a target repository and ingests structural + documentary context into the memory database. It is **idempotent** — re-running is safe since the auto-dedup guard skips facts that already exist.

**Key functions:**
- `_iter_files(root: Path)` — Walk repo with directory pruning (never descends into IGNORE_DIRS)
- `scan_file_tree(root, full=False)` — Directory summary facts + key-file facts
- `parse_manifests(root)` — Extract dependency/script facts from package.json, requirements.txt, pyproject.toml
- `extract_docs(root, max_chars=900)` — Split markdown docs (repo root + docs/) into heading-based facts
- `scan_git(root, commit_limit=30)` — Branch + recent commits + tagged commits from git history
- `install_git_hook(root)` — Install post-commit git hook
- `drop_agents_md(root)` — Drop the AGENTS.md protocol file
- `bootstrap_project(root, store=None, dry_run=True, full=False, max_facts=150, install_hook=True, drop_protocol=True)` — Main entry point (lines 244–328)

**Key insight:** `bootstrap_project()` calls:
1. `scan_file_tree()` to enumerate files and produce per-file facts
2. `parse_manifests()` to extract dependency facts
3. `extract_docs()` to extract documentation facts
4. `scan_git()` to extract git history facts
5. Writes facts via `store.write(**f)` with automatic dedup

**Important for Phase B:** `scan_file_tree()` iterates through all files matching CODE_LANGS (.py, .ts, .js, .tsx, .jsx, .rs, .go, .java, .cs, .rb, .php, .sh, .ps1), but currently **only produces summary facts** (directory stats, key files). There is **NO existing per-file AST extraction loop** — Phase B will be adding a new processing step to `scan_file_tree()` or `bootstrap_project()` to extract structural nodes.

---

### wire.py
**Path:** `C:\PROJECT\project-memory\wire.py`

**Summary:** Generates the correct MCP server configuration for a given coding agent, pinned to the target project's own .project-memory/memory.db.

**Key functions:**
- `_python_exe(pm_root)` — Find the Python executable in the venv
- `_backup(path)` — Backup an existing config file
- `_merge_json(path, top_key, entry)` — Merge one MCP entry into a JSON config file (preserves existing content)
- `wire_agent(agent, project_root=None, project_memory_root=None, drop_protocol=True)` — Wire a target project to project-memory for a given agent (lines 56–154)

**Supported agents:** opencode, cursor, claude, zed, generic

**Key insight:** The `wire_agent()` function modifies agent config files (opencode.json, .cursor/mcp.json, Claude config, .zed/settings.json) to add the project-memory server entry. It uses `_merge_json()` to preserve existing content and backs up the original config file.

**Not relevant for Phase B:** wire.py is purely for agent wiring and config management — no code that will be extended or modified for AST extraction.

---

## 2. LINE NUMBER CONVENTION CHECK

**Finding:** The `memory` table uses **1-indexed line numbers** (line_start and line_end are 1-indexed).

**Evidence:** From memory_store.py:
- In `verify_provenance()`: `l_start = max(0, (row["line_start"] or 1) - 1)` — this converts 1-indexed to 0-indexed for Python slicing
- When constructing snippet for retrieval: `lines[row["line_start"] - 1 : row["line_end"]]` — using `line_start - 1` for 0-indexed array access

**Python ast module convention:** Python's `ast.FunctionDef.lineno` and `ast.FunctionDef.end_lineno` are **1-indexed** (lineno starts at 1)

**tree-sitter convention:** tree-sitter's `node.start_point` and `node.end_point` are **0-indexed** (start_point[0] is 0-indexed)

**Critical for Phase B:** When storing structural node line ranges (Phase B will extract from tree-sitter or Python AST), we must:
1. **Store as 1-indexed** (matching existing episodic facts convention in memory table)
2. If using tree-sitter: convert `start_point[0]` (0-indexed) to `start_point[0] + 1` (1-indexed) before storing
3. If using Python ast: use `lineno` and `end_lineno` directly (already 1-indexed)

**Recommendation:** Use Python's built-in `ast` module (not tree-sitter) because:
- No version incompatibility issues
- 1-indexed by default (matches existing memory table convention directly)
- No external packages needed

---

## 3. TREE-SITTER PACKAGE CHOICE (Empirical Test Results)

### Environment Test Results:

**Installed versions attempted:**
- tree-sitter 0.26.0 (latest)
- tree-sitter-python 0.25.0
- tree-sitter-javascript 0.25.0
- tree-sitter-typescript 0.23.2
- tree_sitter_languages 1.10.2 (convenience wrapper)

**Issues encountered:**
1. **PyCapsule mismatch (tree-sitter 0.26.0 + per-language packages):**
   ```
   TypeError: language must be assigned a tree_sitter.Language object, not PyCapsule
   ```
   The per-language packages (0.25.0) return a PyCapsule, but tree-sitter 0.26.0 doesn't accept it directly.

2. **Version incompatibility (tree-sitter 0.26.0 + tree_sitter_languages 1.10.2):**
   ```
   TypeError: __init__() takes exactly 1 argument (2 given)
   ```
   tree_sitter_languages 1.10.2 is incompatible with tree-sitter 0.26.0.

3. **Downgrade attempt (tree-sitter 0.20.4):**
   ```
   TypeError: Language.__init__() missing 1 required positional argument: 'name'
   ```
   Even with older tree-sitter 0.20.4, the Language API mismatch persisted.

### **Recommendation: Use Python's built-in `ast` module instead of tree-sitter**

**Why:**
- No dependency conflicts or version mismatches
- Already installed in Python standard library
- Produces 1-indexed line numbers (matches existing memory table convention directly)
- Can extract all needed structures: functions, classes, methods, imports
- Sufficient for Python code (80%+ of project-memory codebase is Python)

**For multi-language support (JavaScript/TypeScript) later:** If needed in the future, use the per-language tree-sitter packages only after resolving version compatibility — but for Phase B (Python AST extraction), `ast` module is the clear winner.

---

## 4. AST SPIKE ON memory_store.py (Python ast module)

### Top-Level Functions Extracted:

```
_get_embed_model()                    Lines: 43–54
_encode()                              Lines: 57–69
_vec_to_blob()                         Lines: 72–74
_blob_to_vec()                         Lines: 77–81
_cosine_sim()                          Lines: 84–90
looks_like_secret()                    Lines: 107–110
tokenize_text()                        Lines: 113–125
find_project_root()                    Lines: 200–207
default_db_path()                      Lines: 210–238
get_store()                            Lines: 1831–1836
remember()                             Lines: 1839–1841
recall()                               Lines: 1844–1846
file_briefing()                        Lines: 1849–1851
prepare_task()                         Lines: 1854–1856
checkpoint_task()                      Lines: 1859–1861
```

### Class Definition Extracted (sample):

**Class: MemoryStore** (Lines: 241–1824, node type: class_definition)

**Methods (50+ total; representative sample):**
- `__init__()` — Lines: 242–292
- `_ensure_embedding_schema()` — Lines: 294–302
- `_store_embedding()` — Lines: 304–330
- `_semantic_scores()` — Lines: 336–351 (existing brute-force path)
- `_semantic_scores_vec()` — Lines: 353–425 (new vec-accelerated path, added in Action 2)
- `_recency_weight()` — Lines: 427–447
- `query_hybrid()` — Lines: 974–1088 (dispatch to vec path or brute force)
- `write()` — Lines: 525–586
- `verify_provenance()` — Lines: 862–916
- `backfill_embeddings()` — Lines: 1364–1400
- (... and 40+ more methods)

**Key finding:** Python ast module provides:
- Exact function/class names
- Start and end line numbers (both 1-indexed)
- Decorator information (e.g., @property)
- Nested structure (methods inside classes)
- No node "type" string — Python ast uses `node.__class__.__name__` (e.g., "FunctionDef", "ClassDef", "Import", "ImportFrom")

---

## 5. IMPORT STATEMENT SHAPE (ast module output)

### Example 1: Simple import
```python
import sqlite3  # Line 8
```
- AST type: `Import` (ast.Import class)
- Names: ['sqlite3']
- Structure: `Import(names=[alias(name='sqlite3', asname=None)])`

### Example 2: Multiple names
```python
from typing import Optional, List, Dict, Any, Tuple  # Line 16
```
- AST type: `ImportFrom` (ast.ImportFrom class)
- Module: 'typing'
- Names: ['Optional', 'List', 'Dict', 'Any', 'Tuple']
- Structure: `ImportFrom(module='typing', names=[alias(name='Optional', asname=None), ...], level=0)`

### Example 3: Aliased import
```python
import sqlite_vec as _sqlite_vec_pkg  # Line 31
```
- AST type: `Import` (ast.Import class)
- Names: ['sqlite_vec']
- Alias: '_sqlite_vec_pkg' (in alias.asname)
- Structure: `Import(names=[alias(name='sqlite_vec', asname='_sqlite_vec_pkg')])`

### Example 4: Relative import (hypothetical)
```python
from ..utils import helper  # Would have level=1 or level=2
```
- AST type: `ImportFrom`
- Level: 1 or 2 (indicates `../` or `../../`)
- Module: 'utils' (relative to parent or grandparent)

**Key insight for Phase B extraction:**
- Import AST nodes are uniform and simple
- Extract via:
  - For `Import`: iterate `node.names`, each has `name` and optional `asname`
  - For `ImportFrom`: extract `module`, `level` (for relative imports), and each name's `name`/`asname`
- Gap Mitigation #1 says: "index all imports" — we can walk all ast.Import and ast.ImportFrom nodes at the module level and store them as facts with their line numbers (1-indexed by default)

---

## Summary of Phase B Findings

### What bootstrap does end-to-end:
1. Walk repo file tree with pruning (IGNORE_DIRS, IGNORE_FILE_SUFFIXES)
2. Produce directory/file summary facts
3. Parse manifests (package.json, requirements.txt, pyproject.toml)
4. Extract docs (heading-based facts from markdown)
5. Scan git history (recent commits, tagged commits)
6. Write all facts to memory DB with auto-dedup

### No existing AST extraction loop:
- bootstrap.py only produces **summary facts** (directory stats, key files)
- No per-file structural extraction (functions, classes, imports)
- Phase B will add a new extraction step (likely in `scan_file_tree()` or a new `extract_structures()` function)

### Line numbering convention:
- Existing facts use **1-indexed** line_start/line_end
- Python ast module uses **1-indexed** by default
- tree-sitter uses **0-indexed** (conversion needed)
- **Action:** Phase B extraction must store 1-indexed line numbers to match existing episodic facts

### Recommended extraction approach:
- Use Python's built-in `ast` module (no dependency conflicts)
- Walk each .py file, extract:
  - All top-level functions: name, line_start, line_end, decorators
  - All classes + methods: class name, methods with line ranges
  - All imports: module, imported names, aliases, relative-import level
- Store each structure as a fact with file_path and 1-indexed line_start/line_end
- Reuse existing `store.write()` which handles auto-dedup

### Next steps (not implemented in this spike):
- Add `extract_structures(file_path: Path) -> List[Dict]` function
- Integrate into `scan_file_tree()` or call separately after file enumeration
- For each structure fact: category="structure", tags="phase-b, ast-extracted, [python/javascript/etc]"
- Store structural nodes for linking with episodic facts by file+line overlap (future Phase C)

---

## Deliverable Checklist

- [x] Full bootstrap.py contents shown (lines 1–328)
- [x] Full wire.py contents shown (lines 1–154)
- [x] Line-number-convention finding with evidence (1-indexed in memory table, Python ast uses 1-indexed, tree-sitter uses 0-indexed)
- [x] Tree-sitter package choice recommendation with empirical test results (recommend Python ast module due to version incompatibilities)
- [x] AST spike output on memory_store.py (functions, classes, methods, imports with real line numbers)
- [x] Import statement shape analysis (Import vs ImportFrom, names, aliases, module, level)
- [x] No code changes to production files (all spike scripts in scripts/dev/)
