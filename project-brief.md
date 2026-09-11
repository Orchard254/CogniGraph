# Project Brief: CogniGraph

---

## 1. ONE-PARAGRAPH SUMMARY

**CogniGraph** is an enterprise-grade, persistent, vendor-neutral memory system and Model Context Protocol (MCP) server designed for AI coding agents (including Google Antigravity, Cursor, Windsurf, OpenCode, Claude Desktop, Zed, Roo Code, and Cline). Its primary goal is to eliminate agent "context thrashing" and multi-session amnesia by maintaining an authoritative, cross-session knowledge base of project components, architectural decisions, operational constraints, abstract syntax tree (AST) call/import graphs, code-anchored provenance hashes, and verified test checkpoints. In terms of maturity, CogniGraph is a **working, production-ready Phase 0/Phase 3 substrate** with 205 passing Python unit tests, 26 passing Node.js tests, an active multi-platform CI matrix, robust concurrency controls (SQLite WAL mode + per-call connections), self-healing AST indexing via Tree-sitter, and zero-config multi-IDE automated wiring.

---

## 2. TECH STACK

- **Languages & Versions:**
  - **Python:** Requires `>=3.12` (enforced in [pyproject.toml:11](file:///C:/PROJECT/project-memory/pyproject.toml#L11), [setup.ps1:9-13](file:///C:/PROJECT/project-memory/setup.ps1#L9-L13), and [lib.js:29-37](file:///C:/PROJECT/project-memory/lib.js#L29-L37)). Actively tested up to Python 3.13 in [.github/workflows/ci.yml:18-29](file:///C:/PROJECT/project-memory/.github/workflows/ci.yml#L18-L29).
  - **JavaScript / Node.js:** Node.js `>=18.0.0` (specified in [package.json:32](file:///C:/PROJECT/project-memory/package.json#L32)). Tested against Node 18, 20, and 22 in CI.
  - **SQL:** SQLite 3.35+ dialect with FTS5 and vector extension support.

- **Core Frameworks & SDKs:**
  - **FastMCP / MCP SDK:** `mcp==1.9.4` ([requirements.txt:1](file:///C:/PROJECT/project-memory/requirements.txt#L1), [pyproject.toml:29](file:///C:/PROJECT/project-memory/pyproject.toml#L29)).
  - **Embeddings:** `fastembed==0.8.0` ([requirements.txt:2](file:///C:/PROJECT/project-memory/requirements.txt#L2)), running the CPU-friendly `sentence-transformers/all-MiniLM-L6-v2` (384-dimensional dense vector space; [pm_common.py:46-52](file:///C:/PROJECT/project-memory/pm_common.py#L46-L52)).
  - **Vector Similarity:** `sqlite-vec==0.1.9` ([requirements.txt:5](file:///C:/PROJECT/project-memory/requirements.txt#L5)), with fallback to NumPy cosine similarity ([pm_common.py:59-67](file:///C:/PROJECT/project-memory/pm_common.py#L59-L67), [pm_search.py:21-46](file:///C:/PROJECT/project-memory/pm_search.py#L21-L46)).
  - **AST Parsing:** Modern constructor-based Tree-sitter 0.26+ ecosystem ([requirements.txt:10-13](file:///C:/PROJECT/project-memory/requirements.txt#L10-L13)):
    - `tree-sitter==0.26.0`
    - `tree-sitter-python==0.25.0`
    - `tree-sitter-javascript==0.25.0`
    - `tree-sitter-typescript==0.23.2`
  - **Numerical & ML Utilities:** `scikit-learn==1.9.0` ([requirements.txt:3](file:///C:/PROJECT/project-memory/requirements.txt#L3)), `numpy==2.5.3` ([requirements.txt:4](file:///C:/PROJECT/project-memory/requirements.txt#L4)).
  - **File System Observer:** `watchdog>=3.0.0` (optional extra in [pyproject.toml:39](file:///C:/PROJECT/project-memory/pyproject.toml#L39); fallback is a non-blocking 30-second polling thread in [watcher.py:7-8,19-27](file:///C:/PROJECT/project-memory/watcher.py#L7-L27)).

- **Package Managers & Build Tools:**
  - Python: `setuptools>=61.0` and `wheel` build backend ([pyproject.toml:1-3](file:///C:/PROJECT/project-memory/pyproject.toml#L1-L3)), entry point console script `cognigraph = "memory_cli:main"` (legacy alias `memory_cli` kept; [pyproject.toml:41-42](file:///C:/PROJECT/project-memory/pyproject.toml#L41-L42)).
  - Node.js: `npm` package `cognigraph@1.0.0` ([package.json:2-3](file:///C:/PROJECT/project-memory/package.json#L2-L3)) providing binary `cognigraph` ([package.json:7](file:///C:/PROJECT/project-memory/package.json#L7)).

- **Runtime Requirements:**
  - Standard OS (Windows, Linux, macOS).
  - SQLite with compiled FTS5 extension and C dynamic extension loading enabled (for `sqlite-vec`).
  - Standard C/C++ runtime compatible with pre-built wheels of `tree-sitter` and `fastembed` ONNX Runtime dependencies.

- **Unusual Setup Characteristics:**
  - **Dual Packaging & Dual Distribution:** The system can be consumed either as a native Python package or zero-config via `npx -y cognigraph`. When invoked through Node.js, [index.js](file:///C:/PROJECT/project-memory/index.js) and [lib.js](file:///C:/PROJECT/project-memory/lib.js) automatically inspect the host system for Python `>=3.12`, establish an isolated `.venv` inside the package directory, execute pip install on missing requirements, perform a `py_compile` pre-spawn sanity check, and spawn `server.py` over stdio with inherited handles ([index.js:170-285](file:///C:/PROJECT/project-memory/index.js#L170-L285)).
  - **Tree-sitter 0.26.0 Workaround:** Tree-sitter 0.26.0 eliminated `Parser.set_language()` and deprecated `.captures(node)`. To avoid breaking changes across minor releases, [structural_extract.py:8-13](file:///C:/PROJECT/project-memory/structural_extract.py#L8-L13) uses `Parser(Language(pkg.language()))` combined with explicit recursive descent AST traversal instead of query pattern matching.
  - **Dual-Store Architecture (Two-Table Intake Contract):** Live writes strictly target the `memory` table, while historical migrations and branch operations reside in the `episodic_memories` table; retrieval fuses both stores across a 7-lane hybrid Reciprocal Rank Fusion (RRF) pipeline ([pm_search.py:513-528](file:///C:/PROJECT/project-memory/pm_search.py#L513-L528)).

---

## 3. ARCHITECTURE & STRUCTURE

### Annotated Directory Tree

```
C:\PROJECT\project-memory
├── .github/
│   └── workflows/
│       └── ci.yml                     # Multi-OS (Ubuntu, Win, Mac), multi-version (Py 3.12-3.13, Node 18-22) CI pipeline
├── bench/
│   ├── baseline_20260910.json         # Performance regression baseline metrics
│   └── bench_memory.py                # Microbenchmark suite for store init, writes, AST sync, and bootstrap
├── hooks/
│   └── git_hook.py                    # Git post-commit hook parsing [decision], [constraint], etc. into memory
├── test-js/
│   └── lib.test.js                    # Node.js built-in test runner suite (26 tests for JS wrapper logic)
├── __init__.py                        # Package init re-exporting MemoryStore and default_db_path
├── AGENTS.md                          # Mandatory operational protocol for AI coding agents connecting to memory
├── bootstrap.py                       # Project onboarding scanner (file tree, manifests, markdown docs, git history)
├── branch_promotion.py                # Experimental branch creation, discard, and KEEP_BOTH promotion engine
├── conftest.py                        # Pytest config: auto-caches git rev-parse subprocesses
├── episodic_fts.sql                   # SQL triggers and FTS5 table definitions for episodic memory search
├── eval_priming.py                    # Evaluation harness assessing top-K retrieval relevance for coding tasks
├── genesis_migrate.py                 # Historical migration script converting flat memory tables to episodic substrate
├── index.js                           # Node.js entry point: bootstraps Python venv, handles CLI arguments, spawns server.py
├── INSTALL.md                         # Zero-config setup instructions for IDEs and human operators
├── legacy_provenance.sql              # SQL migration helper for anchoring legacy facts
├── lib.js                             # Node.js utility library: Python version detection, venv resolution, spawn configuration
├── memory_cli.py                      # Comprehensive CLI utility (22 subcommands: stats, query, wire, bootstrap, etc.)
├── memory_store.py                    # Storage engine facade: DB initialization, CRUD, OCC, locks, and mixin aggregation
├── package.json                       # npm manifest for cognigraph
├── phase0_schema.sql                  # Canonical schema DDL: memory, memory_fts, memory_edges, memory_locks
├── phase1_episodic.sql                # Episodic schema DDL: revisions, branches, episodic_memories, triggers
├── phase1_episodic_embeddings.sql     # SQLite vector tables for episodic embedding storage
├── phase2_branch_admin.sql            # Branch administration triggers, single-level branch invariants, and lineage
├── pm_common.py                       # Shared utilities: secret masking, vector encoding/decoding, cosine math, logging
├── pm_context.py                      # ContextMixin: get_context, get_file_context, get_dependency_map, LLM formatting
├── pm_lifecycle.py                    # LifecycleMixin: prepare_task flight briefings, checkpoint_task, audit, consolidate
├── pm_search.py                       # SearchMixin: 7-lane hybrid RRF engine, intent detection, usage-pattern learning
├── pm_structural.py                   # StructuralMixin: AST symbol indexing, who_calls graph queries, project_shape
├── postinstall.js                     # npm postinstall hook attempting best-effort venv setup and dependency installation
├── cognigraph_master_engineering_blueprint_v2_3_0.md # Core architectural specification (Phase 0 frozen architecture)
├── pyproject.toml                     # PEP 518/621 project configuration, dependencies, and entry-points
├── pytest.ini                         # Pytest configuration registering custom markers (stress_heavy)
├── README.md                          # Primary human-readable documentation and architectural overview
├── README_NPM.md                      # npm package documentation and zero-config quickstart
├── requirements.txt                   # Production Python dependencies pinned to exact versions
├── server.py                          # Main MCP server exposing 14 FastMCP tools over stdio
├── setup.ps1                          # Automated Windows PowerShell environment bootstrapper
├── structural_extract.py              # Tree-sitter recursive-descent AST extractor (Python, JS, TS, TSX)
├── watcher.py                         # Background daemon thread monitoring project files for live AST re-indexing
├── wire.py                            # Automated agent configuration generator for 9 IDE agent targets
├── test_dedup_embed.py                # Unit tests for embedding deduplication and fallback logic
├── test_llm_priming.py                # Comprehensive unit tests for LLM context generation and AST edge extraction
├── test_memory_cli.py                 # Unit tests for memory_cli subcommands via stubbed argv
├── test_phase3_priming.py             # Unit tests for usage logging, frequency boosting, and agent lane weighting
├── test_server_e2e.py                 # End-to-end stdio framing smoke test communicating directly with server.py
├── test_server_tools.py               # Unit tests covering FastMCP tool parameter clamping, errors, and roundtrips
├── test_watcher.py                    # Unit tests for background file watcher filtering and debounce behavior
└── test_wire_bootstrap.py             # Unit tests for agent config merging, rotation, and bootstrap ingestion
```

### Component Interactions & Information Flow

```
+---------------------------------------------------------------------------------------+
|                                    AI Coding Agent                                    |
|              (Google Antigravity, Cursor, Windsurf, Claude Desktop, Zed, etc.)        |
+---------------------------------------------------------------------------------------+
                                           |  (MCP over stdio JSON-RPC)
                                           v
+---------------------------------------------------------------------------------------+
|                                      server.py                                        |
|                        FastMCP Server ("CogniGraph", 14 tools)                     |
+---------------------------------------------------------------------------------------+
        |                                  |                                 |
        | (per-call context manager)       | (background thread)             | (agent wiring)
        v                                  v                                 v
+----------------------+         +--------------------+            +--------------------+
|   memory_store.py    |         |    watcher.py      |            |      wire.py       |
|  MemoryStore Facade  |         | (Watchdog/Polling) |            | (Config generator) |
+----------------------+         +--------------------+            +--------------------+
        |                                  |
        +---------------+                  | (calls AST parser)
        | Composes      |                  v
        | Mixins:       |        +--------------------+
        |               |        |structural_extract  |
        |               |        | (Tree-sitter 0.26) |
        |               |        +--------------------+
        v               v
+---------------+ +------------------+ +-----------------+ +-------------------+
| pm_search.py  | |  pm_context.py   | | pm_lifecycle.py | | pm_structural.py  |
| (7-lane RRF,  | | (Context format, | | (prepare_task,  | | (AST nodes/calls, |
| usage boost,  | |  file briefings, | | checkpoint_task | | who_calls queries,|
| intent lanes) | |  doc-noise cut)  | | OCC, provenance)| | project_shape)   |
+---------------+ +------------------+ +-----------------+ +-------------------+
        |                  |                     |                   |
        +------------------+---------------------+-------------------+
                                   |
                                   v
+---------------------------------------------------------------------------------------+
|                                SQLite 3 Database                                      |
|                              (.cognigraph/memory.db)                              |
|  - memory (live intake, OCC, provenance)     - memory_fts (FTS5 BM25 index)           |
|  - episodic_memories (historical/branches)   - episodic_fts (Episodic FTS5 index)     |
|  - memory_edges (knowledge graph CTEs)       - vec_memory / sqlite-vec (dense vectors)|
|  - structural_nodes / calls / imports        - memory_usage_log (Phase 3 boost stats) |
+---------------------------------------------------------------------------------------+
```

### Execution Entry Points

1. **MCP Server Daemon:** `server.py` ([server.py:1015-1018](file:///C:/PROJECT/project-memory/server.py#L1015-L1018)). Invoked by the agent host. Starts background services (file watcher in [server.py:160-178](file:///C:/PROJECT/project-memory/server.py#L160-L178)) and runs `mcp.run()` over standard I/O.
2. **NPM Distribution Wrapper:** `index.js` ([index.js:275-285](file:///C:/PROJECT/project-memory/index.js#L275-L285)). Invoked when configured as `npx -y cognigraph`. Resolves or creates Python virtual environment, executes preflight compilation, and spawns `server.py`.
3. **CLI Management Interface:** `memory_cli.py:main` ([memory_cli.py:13-18](file:///C:/PROJECT/project-memory/memory_cli.py#L13-L18)). Direct terminal access for manual querying, database optimization, bulk provenance scanning, agent wiring, and project onboarding.
4. **Automated Git Hook:** `hooks/git_hook.py:main` ([hooks/git_hook.py:55-80](file:///C:/PROJECT/project-memory/hooks/git_hook.py#L55-L80)). Triggered post-commit by git to ingest bracket-tagged commit summaries.
5. **Evaluation & Benchmark Scripts:**
   - `eval_priming.py:main` ([eval_priming.py:118-144](file:///C:/PROJECT/project-memory/eval_priming.py#L118-L144)): Quantifies context recall and precision.
   - `bench/bench_memory.py:main` ([bench/bench_memory.py:160-195](file:///C:/PROJECT/project-memory/bench/bench_memory.py#L160-L195)): Assesses write, embedding, and query latency.

---

## 4. MCP IMPLEMENTATION

### SDK, Version, & Transport

- **SDK:** Official Python Model Context Protocol SDK `mcp==1.9.4` ([pyproject.toml:29](file:///C:/PROJECT/project-memory/pyproject.toml#L29)).
- **Server Implementation:** FastMCP instance `mcp = FastMCP("CogniGraph")` in [server.py:15](file:///C:/PROJECT/project-memory/server.py#L15).
- **Transport:** Standard I/O (`stdio`) via `mcp.run()` in [server.py:1017](file:///C:/PROJECT/project-memory/server.py#L1017). Stdio framing integrity is guarded by configuring Python logging to `stderr` ([server.py:6, 13](file:///C:/PROJECT/project-memory/server.py#L6-L13)) and avoiding raw `print()` statements in server operations.
- **Resource / Prompt Exposure:**
  - **Tools:** Exactly **14 tools** are exposed via `@mcp.tool()`.
  - **Resources:** **0** (no `@mcp.resource` defined; state is retrieved dynamically via tools).
  - **Prompts:** **0** (no `@mcp.prompt` defined; priming workflows are handled by structured tool calls).

### Complete Catalog of Exposed MCP Tools

| # | Tool Name | Description | Summary Input Schema (Parameters & Types) |
|---|---|---|---|
| 1 | `memory_prepare_task` | REQUIRED before writing or modifying any code in this repository. Returns the task briefing: relevant decisions, constraints, component facts, file-level impacts, cross-file dependency risks, and the current verified test baseline. | `intent: str`, `file_paths: Optional[List[str]] = None`, `limit: int = 5` |
| 2 | `memory_checkpoint_task` | Call immediately after a task is verified complete (tests green). Records new components, decisions, the test baseline, and failed approaches as durable memory for future sessions. | `summary: str`, `components: Optional[List[Dict[str, Any]]] = None`, `decisions: Optional[List[Dict[str, Any]]] = None`, `test_baseline: Optional[str] = None`, `failures: Optional[List[Dict[str, Any]]] = None` |
| 3 | `memory_write` | Record a durable fact this session learned that future sessions will need: decisions, constraints, gotchas, where components live. Secrets are refused. | `category: str`, `content: str`, `rationale: str = ""`, `confidence: str = "assumed"`, `session_id: str = ""`, `file_path: str = ""`, `tags: str = ""`, `importance: int = 3`, `line_start: int = 0`, `line_end: int = 0`, `code_hash: str = ""`, `git_commit_sha: str = ""` |
| 4 | `memory_write_batch` | Atomic batch form of memory_write for several related facts. | `facts: List[Dict[str, Any]]` |
| 5 | `memory_query` | Search project memory — decisions, constraints, components, past failures — via hybrid lexical + semantic + code-structure search. Use when you need project knowledge you don't have. | `text: str`, `category: str = ""`, `tag: str = ""`, `limit: int = 10`, `expand_graph: bool = True`, `session_id: str = ""`, `repo_id: Optional[int] = None`, `intent: str = ""`, `agent_type: str = ""` |
| 6 | `memory_get_context` | Token-budgeted Markdown briefing on a topic, formatted for your agent type and token ceiling. Use before working in an unfamiliar area. | `query: str`, `agent_type: str = "default"`, `max_tokens: int = 2000`, `limit: int = 5`, `intent: str = ""` |
| 7 | `memory_get_file_context` | Call BEFORE editing a file: its symbols, anchored facts, callers, and call-graph edges. | `file_path: str` |
| 8 | `memory_check_safety` | Call before a multi-file refactor: dependency risk matrix across the files you're about to change. | `file_paths: List[str]` |
| 9 | `memory_who_calls` | Where a symbol is defined, every call site, and importing files. Call before renaming or changing a signature. | `symbol: str` |
| 10 | `memory_project_shape` | Lay of the land: indexed files, top modules by definition density, call/import edge totals. | *(None)* |
| 11 | `memory_supersede` | Replace an outdated fact with corrected content. Use when you discover memory that is wrong. | `old_id: int`, `new_content: str`, `rationale: str = ""`, `confidence: str = "assumed"`, `session_id: str = ""`, `file_path: str = ""`, `tags: str = ""`, `expected_version: int = 0` |
| 12 | `memory_deprecate` | Mark a fact obsolete without replacement. | `id: int`, `reason: str = ""` |
| 13 | `memory_flag_fact` | Flag a fact as misleading or unverified so future briefings carry a warning. Use when memory looks wrong but you lack the correction. | `fact_id: int`, `note: str = ""`, `flag_type: str = "misleading"` |
| 14 | `memory_stats` | Memory health: active/deprecated counts, category breakdown, edges. Call at session start per AGENTS.md. | *(None)* |

### How Context is Gathered and Built

The context-building mechanism operates across five concrete stages:

1. **Static Ingestion & Heuristic Parsing ([bootstrap.py](file:///C:/PROJECT/project-memory/bootstrap.py)):**
   - **Directory Tree Walk ([bootstrap.py:55-70](file:///C:/PROJECT/project-memory/bootstrap.py#L55-L70)):** `_iter_files` traverses the project root while strictly pruning noise directories (`.git`, `node_modules`, `venv`, `dist`, `.cognigraph`, legacy `.project-memory`, etc.) and non-code binaries. Computes file counts and line metrics per directory.
   - **Dependency Manifest Parsing ([bootstrap.py:226-269](file:///C:/PROJECT/project-memory/bootstrap.py#L226-L269)):** `parse_manifests` parses `pyproject.toml`, `package.json`, `requirements.txt`, and `Cargo.toml`, extracting pinned dependencies and runtime scripts.
   - **Documentation Chunking ([bootstrap.py:272-306](file:///C:/PROJECT/project-memory/bootstrap.py#L272-L306)):** `extract_docs` reads markdown files (`README.md`, `AGENTS.md`, `CONTRIBUTING.md`), splitting them along markdown heading boundaries (`#`, `##`, `###`) into discrete facts capped at 900 characters, tagging them by topic (`decision`, `constraint`, `component`).
   - **Git History Ingestion ([bootstrap.py:309-338](file:///C:/PROJECT/project-memory/bootstrap.py#L309-L338)):** `scan_git` extracts current branch and recent commit messages, detecting bracket tags like `[decision]` or `[constraint]` to ingest structured commit rationale.

2. **Structural Code Indexing ([structural_extract.py](file:///C:/PROJECT/project-memory/structural_extract.py) & [pm_structural.py](file:///C:/PROJECT/project-memory/pm_structural.py)):**
   - For every `.py`, `.js`, `.jsx`, `.ts`, and `.tsx` file, `extract_structures` runs a Tree-sitter 0.26 recursive descent parse.
   - Extracts functions, classes, methods, direct function calls (`structural_calls`), and module import bindings (`structural_imports`) with 1-indexed line numbers.
   - Ingests these into `structural_nodes`, `structural_calls`, and `structural_imports`, linking them to stored facts via line-range overlap (`fact_node_links`).

3. **Continuous Background Watching ([watcher.py](file:///C:/PROJECT/project-memory/watcher.py)):**
   - When the MCP server launches, `_start_background_services()` starts a daemon file watcher.
   - If `watchdog` is installed, it attaches a native file system observer; otherwise, it runs a 30-second polling loop.
   - Debounces file modifications (1.5-second settling window), recalculates SHA256 hashes, re-runs Tree-sitter extraction, and updates structural tables dynamically.

4. **Multi-Lane Hybrid Search & Ranking ([pm_search.py:508-550](file:///C:/PROJECT/project-memory/pm_search.py#L508-L550)):**
   - When queried, `query_hybrid` executes 7 parallel retrieval lanes:
     1. **Memory FTS5 BM25:** Lexical matching over live facts in `memory_fts`.
     2. **Memory Token Jaccard:** Set-overlap similarity across tokenized facts.
     3. **Memory Vector Cosine:** 384-dimensional dense vector embeddings generated via FastEmbed and queried via `sqlite-vec` (or NumPy fallback).
     4. **Episodic FTS5 BM25:** Lexical matching over canonical episodic memories.
     5. **Episodic Token Jaccard:** Set-overlap similarity over episodic records.
     6. **Episodic Vector Cosine:** Vector similarity over episodic embeddings.
     7. **Structural AST Lane:** Direct symbol and identifier lookup in `structural_nodes`.
   - Fuses lane ranks using Reciprocal Rank Fusion ($k=5$).
   - Multiplies scores by importance weights (1..5) and recency decay.
   - Applies **intent-aware boosting** ([pm_search.py:465-495](file:///C:/PROJECT/project-memory/pm_search.py#L465-L495)): e.g., an intent containing "fix" boosts `constraint` facts by 1.8x, whereas "implement" boosts `component` and `decision` facts.
   - Applies **file-anchor boosting**: Facts anchored to a file mentioned in the query receive a 1.6x multiplier.
   - Applies **Phase 3 usage boosting**: Historical retrieval frequencies dynamically boost relevant facts up to a 1.15x ceiling.

5. **Synthesis & Priming Briefings ([pm_context.py](file:///C:/PROJECT/project-memory/pm_context.py) & [pm_lifecycle.py](file:///C:/PROJECT/project-memory/pm_lifecycle.py)):**
   - `prepare_task` gathers top-K intent facts, executes `check_safety` and `get_dependency_map` across target files, appends code structure (classes/methods/callers), attaches the latest verified test baseline, stamps misleading-fact warnings, and returns a single Markdown flight briefing.

---

## 5. COMMANDS

### Install & Setup

- **Python Virtual Environment & Dependencies:**
  ```powershell
  # Create virtual environment and install dependencies
  python -m venv venv
  .\venv\Scripts\pip install -r requirements.txt
  .\venv\Scripts\pip install -e .
  ```
  *(Automated Windows equivalent: `.\setup.ps1`)*

- **Node.js Package Installation:**
  ```bash
  npm install
  ```

### Build & Package

- **Python Wheel/SDist Build:**
  ```powershell
  .\venv\Scripts\pip install build
  .\venv\Scripts\python -m build
  ```
- **Node.js:** No compile/transpile step is required; `index.js` and `lib.js` are written in standard ECMAScript.

### Run Entry Points

- **Run MCP Server directly (stdio transport):**
  ```powershell
  .\venv\Scripts\python.exe server.py
  ```
- **Run via NPM distribution wrapper:**
  ```bash
  npx -y cognigraph
  # or locally:
  node index.js
  ```
- **Agent Configuration (Wire):**
  ```powershell
  # Interactive agent setup
  .\venv\Scripts\python.exe memory_cli.py wire --root C:\path\to\target\project

  # Direct agent specification
  .\venv\Scripts\python.exe memory_cli.py wire cursor --root C:\path\to\target\project
  ```
- **Project Onboarding (Bootstrap):**
  ```powershell
  # Preview dry-run
  .\venv\Scripts\python.exe memory_cli.py bootstrap --root C:\path\to\target\project

  # Ingest into memory
  .\venv\Scripts\python.exe memory_cli.py bootstrap --root C:\path\to\target\project --apply
  ```
- **Evaluation Harness:**
  ```powershell
  .\venv\Scripts\python.exe eval_priming.py
  ```
- **Performance Benchmarks:**
  ```powershell
  .\venv\Scripts\python.exe bench/bench_memory.py
  ```

### Test Suite

- **Run Python Unit Tests (Pytest):**
  ```powershell
  .\venv\Scripts\python.exe -m pytest -q --ignore=scratch
  ```
  *(Collects and runs 205 tests across 8 test suites in ~30 seconds).*

- **Run Python Tests with Coverage:**
  ```powershell
  .\venv\Scripts\pip install pytest-cov
  .\venv\Scripts\python.exe -m pytest --cov=memory_store --cov=bootstrap --cov=watcher --cov=wire --cov=pm_common --cov=pm_search --cov=pm_context --cov=pm_lifecycle --cov=pm_structural --cov-report=term-missing -q --ignore=scratch
  ```

- **Run Node.js Unit Tests:**
  ```bash
  npm test
  ```
  *(Executes `node --test "test-js/*.test.js"`, running 26 hermetic tests).*

- **Run E2E Stdio Smoke Test:**
  ```powershell
  .\venv\Scripts\python.exe test_server_e2e.py
  ```

### Lint & Quality Checks

- **Configured Tool:** [pyproject.toml:125-140](file:///C:/PROJECT/project-memory/pyproject.toml#L125-L140) specifies configuration for `ruff`:
  ```toml
  [tool.ruff]
  line-length = 100
  target-version = "py312"
  ```
- **To run Ruff:**
  ```powershell
  .\venv\Scripts\pip install ruff
  .\venv\Scripts\ruff check .
  ```

### Flagged Issues, Broken Commands, & Gaps

> [!WARNING]
> 1. **`npm ci` in CI Workflow is Broken:** [.github/workflows/ci.yml:100](file:///C:/PROJECT/project-memory/.github/workflows/ci.yml#L100) specifies `npm ci`, but **no `package-lock.json` exists in the repository root**. Running `npm ci` on a fresh runner without a lockfile causes `npm ERR! code ENOSELF / ENOKEY` and fails.
> 2. **Ruff Linter is Undocumented and Uninstalled:** While `[tool.ruff]` is configured in `pyproject.toml`, `ruff` is not included in `requirements.txt`, not installed by `setup.ps1`, not present in `package.json` scripts, and not executed in `.github/workflows/ci.yml`.
> 3. **Pytest Inconsistency in `pyproject.toml`:** [pyproject.toml:106](file:///C:/PROJECT/project-memory/pyproject.toml#L106) declares `testpaths = ["tests"]`, but all primary test files reside in the root directory (`test_*.py`), while `tests/` holds no test modules. Running `pytest` works solely because [pytest.ini](file:///C:/PROJECT/project-memory/pytest.ini) overrides `pyproject.toml` without specifying `testpaths`. If `pytest.ini` were deleted, `pytest` would collect zero tests.

---

## 6. CONVENTIONS

### Code Style & Formatting

- **Target Version & Line Length:** Formatted to Python 3.12 standards with a maximum line length of 100 characters ([pyproject.toml:126-127](file:///C:/PROJECT/project-memory/pyproject.toml#L126-L127)).
- **Type Annotations:** Extensive use of `typing` annotations (`Optional`, `List`, `Dict`, `Tuple`, `Any`) throughout function signatures.
- **Mix-in Architecture:** The monolithic `MemoryStore` is structured as a composition of specialized mixin classes (`SearchMixin`, `ContextMixin`, `LifecycleMixin`, `StructuralMixin`), with backward-compatible re-exports maintained in [memory_store.py:30-41](file:///C:/PROJECT/project-memory/memory_store.py#L30-L41).

### Error Handling & Defensive Programming Patterns

- **Resilient Input Clamping ([server.py:42-71](file:///C:/PROJECT/project-memory/server.py#L42-L71)):** MCP parameters pass through clamping functions (`_clamp_limit`, `_clamp_chars`, `_clamp_ttl`, `_clamp_importance`). Non-numeric or out-of-range arguments fall back to safe defaults instead of throwing unhandled exceptions.
- **Secrets Refusal ([pm_common.py:104-117](file:///C:/PROJECT/project-memory/pm_common.py#L104-L117)):** Prior to writing any fact, content is scanned with regexes detecting API keys, private keys, authorization tokens, and credentials. Detected secrets are rejected with `SECRETS_REFUSED`.
- **Atomic File Writing with Backups ([wire.py:72-108](file:///C:/PROJECT/project-memory/wire.py#L72-L108)):** Config mutations create rotating backups (`.bak`, `.bak.1`, `.bak.2`). Corrupt or non-JSON files are preserved as `.corrupt-<timestamp>.bak` and logged rather than clobbered. Writes write to temporary files, `fsync()`, and perform atomic `os.replace`.
- **Subprocess Safety:** All subprocess calls (git probes, python version checks) enforce `stdin=subprocess.DEVNULL`, `shell=False`, and a 15-second timeout to prevent hangs on inherited MCP stdio pipes ([wire.py:61-62](file:///C:/PROJECT/project-memory/wire.py#L61-L62), [bootstrap.py:317-318](file:///C:/PROJECT/project-memory/bootstrap.py#L317-L318)).
- **Graceful Feature Degradation:** Core operations run without optional packages. Missing `sqlite-vec` falls back to NumPy cosine distance; missing `watchdog` falls back to polling; missing Tree-sitter grammars skip structural indexing without breaking lexical or semantic storage.

### Testing Approach

- **Hermetic Isolation:** Every test suite provisions temporary databases inside pytest's `tmp_path` fixture. Tests never mutate the project's own `.cognigraph/memory.db`.
- **Subprocess Deduplication:** [conftest.py:26-51](file:///C:/PROJECT/project-memory/conftest.py#L26-L51) registers an autouse fixture monkeypatching `MemoryStore._current_git_commit` to cache results per repository root, reducing test suite execution time by over 50%.
- **Lightweight Native Node Tests:** The JavaScript test suite utilizes Node.js's built-in `node:test` runner and assertions ([test-js/lib.test.js](file:///C:/PROJECT/project-memory/test-js/lib.test.js)) without third-party frameworks like Jest or Mocha.

### Architectural Rules

- **Strict Stdio Purity:** In [server.py](file:///C:/PROJECT/project-memory/server.py), standard output (`stdout`) is reserved exclusively for MCP JSON-RPC protocol packets. All debugging and operational logs are routed to `logging.getLogger(__name__)` which streams to `stderr`.
- **Per-Call SQLite Connection Discipline ([server.py:25-40](file:///C:/PROJECT/project-memory/server.py#L25-L40)):** Tools obtain a database connection via the `_store()` context manager, which creates a fresh connection and closes it in a `finally` block. This prevents cross-thread SQLite concurrency exceptions and handle leaks.
- **Two-Table Separation of Concerns:** New facts are strictly written to the `memory` table. The `episodic_memories` table is reserved for branch management and historical migrations.
- **Optimistic Concurrency Control (OCC):** Fact updates via `memory_supersede` verify the `expected_version` column to prevent lost updates during concurrent agent editing sessions.

---

## 7. CURRENT STATE

### What's Implemented & Complete

- **Full MCP Server:** 14 tools fully registered and wired via FastMCP over stdio ([server.py:181-522](file:///C:/PROJECT/project-memory/server.py#L181-L522)).
- **Multi-Lane Hybrid RRF Search:** Complete 7-lane fusion engine with intent detection, agent lane weights, and usage-pattern frequency boosting ([pm_search.py:508-650](file:///C:/PROJECT/project-memory/pm_search.py#L508-L650)).
- **Tree-sitter AST Extraction:** Recursive descent AST parsing for Python, JS, and TS, populating structural nodes, call edges, and import dependencies ([structural_extract.py](file:///C:/PROJECT/project-memory/structural_extract.py)).
- **Background File Watcher:** Live file monitoring with debounce handling and automatic AST node re-indexing ([watcher.py](file:///C:/PROJECT/project-memory/watcher.py)).
- **Universal Multi-IDE Wiring:** Interactive CLI menu and automated configuration for 9 agent targets with rotating backups ([wire.py](file:///C:/PROJECT/project-memory/wire.py)).
- **Branch Management Substrate:** Branch creation, tombstones/discard, and promotion under the `KEEP_BOTH` convergence policy ([branch_promotion.py](file:///C:/PROJECT/project-memory/branch_promotion.py)).
- **Self-Healing Provenance:** SHA256 snippet hashing with neighborhood line-shift detection and automatic healing ([pm_lifecycle.py:34-118](file:///C:/PROJECT/project-memory/pm_lifecycle.py#L34-L118)).
- **Lifecycle Briefings & Negative Knowledge:** Pre-coding flight briefings (`prepare_task`) and post-coding checkpoints (`checkpoint_task`) with explicit capture of failed approaches (`failures`) stored as negative-knowledge constraints.

### What's Stubbed, Half-Implemented, or Deferred

- **`memory_dependencies` Table (Phase IV Deferred):** The episodic dependency schema (`memory_dependencies` with relation types `SUPPORTS`, `DERIVED_FROM`, `CONTRADICTS`, `REQUIRES`) and database triggers exist in [phase1_episodic.sql:98-112](file:///C:/PROJECT/project-memory/phase1_episodic.sql#L98-L112), but there are **zero writers or producers** in code. As recorded in architectural decision Fact #104, episodic relationship derivation is explicitly deferred to Phase IV (Epistemic Controller).
- **Branch Convergence Policies:** In [branch_promotion.py:17-21](file:///C:/PROJECT/project-memory/branch_promotion.py#L17-L21), only `convergence_policy="KEEP_BOTH"` is implemented. Alternative reconciliation strategies (e.g., `THREE_WAY_MERGE`, `OVERWRITE`) are stubbed/deferred.
- **Anchor-to-Anchor Lineage:** Anchor supersession (`supersedes_anchor_id`) is reclassified as Phase IV work in the master blueprint ([project-memory_master_engineering_blueprint_v2_3_0.md:16-21](file:///C:/PROJECT/project-memory/project-memory_master_engineering_blueprint_v2_3_0.md#L16-L21)). Phase 0 anchors are root-generation only.
- **Single-Level Branch Hierarchy:** Nested branch hierarchies are rejected by SQL triggers (`trg_branches_single_level` in [phase2_branch_admin.sql:40-48](file:///C:/PROJECT/project-memory/phase2_branch_admin.sql#L40-L48)). Multi-level branching is deferred.
- **Single-Repo Stopgap:** When `repo_id` is omitted in `query_hybrid` or `create_branch`, it derives the default repository by taking `SELECT id FROM repos ORDER BY id LIMIT 1` ([pm_search.py:530-534](file:///C:/PROJECT/project-memory/pm_search.py#L530-L534)). Multi-repo resolution is not yet implemented.

### Current Git Status & Commit History

- **Current Branch:** `main` (clean working tree, up to date with `origin/main`).
- **Last 12 Commits:**
  1. `31af859` - fix(wire): treat empty or whitespace-only existing config files as empty objects
  2. `633e583` - fix(cli): handle nonexistent project root gracefully in wire command
  3. `ae6ef1d` - feat(wire): add multi-IDE interactive setup, auto-detection, and support for Antigravity, Windsurf, Roo, and Cline
  4. `b5eb651` - Fix .gitignore patterns for coverage/debug artifacts
  5. `4619e98` - Ignore _pyc_inspect.py debug script
  6. `0ec6395` - Ignore coverage artifacts
  7. `4fd4eb1` - Add watchdog as optional to requirements.txt; weekly DB maintenance run (prune 0, audit stable, optimize) | Verified Baseline: 194 pytest + 26 node
  8. `18b3813` - CI matrix + coverage (63% total), packaging (pip install -e . + memory_cli entry), perf baselines (bench/bench_memory.py) | Verified Baseline: 194 pytest + 26 node
  9. `fba6178` - Fix dedup for embed=False facts: fall through to Jaccard when semantic misses (was early None) | 3 regression tests | Verified Baseline: 191 passed pytest + 26 node
  10. `aa65956` - Docs: test count 188->190 (README); add stdio e2e smoke test (test_server_e2e.py) | Verified Baseline: 191 passed
  11. `d809e74` - Cover watcher observer branch (2 tests, watchdog-gated) + fix polling test path | Verified Baseline: 190 passed
  12. `bbff37c` - Refresh docs: Zed env, wire schema, reliability notes, test invocation

---

## 8. DOCUMENTATION

Every documentation file present in the repository, its purpose, and any noted contradictions or stale content:

1. **[README.md](file:///C:/PROJECT/project-memory/README.md):**
   - *Description:* Primary project overview, architecture description, quickstart guide, tool reference table, CLI guide, and reliability notes.
   - *Status:* Accurate; Section 3 tool table is auto-generated from the live server registry (14 tools) and enforced by docs-drift CI.
2. **[README_NPM.md](file:///C:/PROJECT/project-memory/README_NPM.md):**
   - *Description:* npm package documentation explaining zero-config `cognigraph` setup, automatic environment provisioning, and IDE snippets.
   - *Status:* Accurate and aligned with `index.js` and `package.json`.
3. **[AGENTS.md](file:///C:/PROJECT/project-memory/AGENTS.md):**
   - *Description:* Mandatory operational protocol for AI coding agents detailing self-prompting rules, start-of-session checks (`memory_stats`, `memory_prepare_task`), in-flight memory recording, and end-of-session checkpoints.
   - *Status:* Authoritative and actively referenced by the system prompt and agent rules.
4. **[INSTALL.md](file:///C:/PROJECT/project-memory/INSTALL.md):**
   - *Description:* Concise installation guide for human operators and IDE configurations across Windsurf, Cursor, Claude, Trae, and Zed.
   - *Status:* Accurate; correctly documents the Zed `context_servers` top-level environment requirement.
5. **[cognigraph_master_engineering_blueprint_v2_3_0.md](file:///C:/PROJECT/project-memory/cognigraph_master_engineering_blueprint_v2_3_0.md):**
   - *Description:* Definitive Phase 0 architectural specification defining the canonical substrate, two-table model, revision invariants, and Phase IV deferrals.
   - *Status:* Accurate architectural baseline reconciling live Tree-sitter implementation with episodic storage.
