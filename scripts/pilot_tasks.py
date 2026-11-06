"""Task definitions for the controlled coding-task comparison harness.

Each task is a tiny coding job whose CORRECT implementation depends on one
project convention that exists ONLY as a seeded CogniGraph fact — it is not
in the starter files, so a control agent (no injected memory) has to guess
it, while a treated agent should receive it through the plugin bridge.

Scoring is deterministic: after the agent run, the runner writes the hidden
check files into the sandbox and executes them. No LLM judge is involved.
Check files are written only AFTER the agent finishes, so the agent cannot
read the answers from the sandbox.

To add a task: add one dict to TASKS with a unique id, a prompt that does
not state the convention, seed_facts the bridge must deliver for that
prompt (lexical overlap with the prompt matters — seeding uses embed=False
in the harness for speed, so the fact must be findable by the lexical
lanes), starter files, and a check command.
"""

TASKS: list[dict] = [
    {
        "id": "future_import",
        "title": "New Python module must follow project conventions",
        "prompt": (
            "Create a new module config_loader.py in the project root with a "
            "function load_config(path) that reads a JSON file at path and "
            "returns the parsed dict. Keep the module consistent with this "
            "project's module conventions. Create the file; do not run "
            "anything."
        ),
        "seed_facts": [
            {
                "category": "constraint",
                "content": (
                    "Every Python module in this project must begin with "
                    "`from __future__ import annotations` as its first line "
                    "and keep every line at or under 88 characters. Use "
                    "double quotes for strings."
                ),
                "importance": 5,
                "tags": "conventions,python,style",
            }
        ],
        "starter_files": {
            "README.md": (
                "demo project\nThis project follows strict module "
                "conventions.\n"
            ),
        },
        "check_files": {
            "hidden_checks/check.py": (
                "import json, sys, tempfile\n"
                "from pathlib import Path\n"
                "root = Path.cwd()\n"
                "mod = root / 'config_loader.py'\n"
                "assert mod.is_file(), 'config_loader.py missing'\n"
                "lines = mod.read_text(encoding='utf-8').splitlines()\n"
                "first = next((l for l in lines if l.strip()), '')\n"
                "assert first == 'from __future__ import annotations', \\\n"
                "    f'first line is {first!r}'\n"
                "long = [l for l in lines if len(l) > 88]\n"
                "assert not long, f'lines over 88 chars: {long!r}'\n"
                "sys.path.insert(0, str(root))\n"
                "from config_loader import load_config\n"
                "with tempfile.NamedTemporaryFile('w', suffix='.json', "\
                "delete=False) as fh:\n"
                "    json.dump({'a': 1, 'b': [2, 3]}, fh)\n"
                "    name = fh.name\n"
                "assert load_config(name) == {'a': 1, 'b': [2, 3]}\n"
                "print('CHECK PASS')\n"
            ),
        },
        "check_cmd": ["{python}", "hidden_checks/check.py"],
    },
    {
        "id": "debounce_this",
        "title": "debounce uses this project's leading-edge behavior",
        "prompt": (
            "Implement a debounce function in debounce.js and export it via "
            "module.exports. debounce(fn, wait) should invoke fn, passing "
            "through the arguments of the call. "
            "Follow this project's implementation conventions for debounce. "
            "Create the file; do not run anything."
        ),
        "seed_facts": [
            {
                "category": "constraint",
                "content": (
                    "In this codebase debounce is leading-edge: it invokes "
                    "fn immediately on the first call, then suppresses "
                    "further calls until wait milliseconds pass with no new "
                    "call. It is declared with the `function` keyword (never "
                    "an arrow function) so the caller's `this` binding is "
                    "preserved."
                ),
                "importance": 5,
                "tags": "js,debounce,leading-edge,this",
            }
        ],
        "starter_files": {
            "package.json": '{"name": "pilot-debounce", "private": true}\n',
            "README.md": "demo project\n",
        },
        "check_files": {
            "hidden_checks/check.js": (
                "const assert = require('node:assert');\n"
                "const debounce = require('../debounce.js');\n"
                "assert.strictEqual(typeof debounce, 'function');\n"
                "const calls = [];\n"
                "const obj = {\n"
                "  name: 'ctx',\n"
                "  handler: debounce(function (x) { calls.push([this.name, x]); }, 40)\n"
                "};\n"
                "obj.handler(1);\n"
                "assert.deepStrictEqual(calls, [['ctx', 1]],\n"
                "  'leading edge: first call invokes immediately');\n"
                "obj.handler(2); obj.handler(3);\n"
                "setTimeout(() => {\n"
                "  assert.deepStrictEqual(calls, [['ctx', 1]],\n"
                "    'calls within wait are suppressed');\n"
                "  obj.handler(4);\n"
                "  assert.deepStrictEqual(calls, [['ctx', 1], ['ctx', 4]],\n"
                "    'after quiet wait, next call invokes immediately');\n"
                "  console.log('CHECK PASS');\n"
                "}, 90);\n"
            ),
        },
        "check_cmd": ["node", "hidden_checks/check.js"],
    },
    {
        "id": "retry_backoff",
        "title": "retry uses this project's exponential backoff policy",
        "prompt": (
            "Implement a retry helper in retry.js and export it via "
            "module.exports. retry(fn) should call fn, and if fn throws, "
            "call it again until it succeeds or the project's retry limits "
            "are reached, then rethrow the last error. Follow this "
            "project's retry conventions. Create the file; do not run "
            "anything."
        ),
        "seed_facts": [
            {
                "category": "constraint",
                "content": (
                    "In this codebase retry uses exponential backoff: the "
                    "first retry waits 100 milliseconds, the wait doubles "
                    "each attempt, and it never exceeds 2000 milliseconds. "
                    "Retry gives up after 5 attempts total and rethrows the "
                    "last error."
                ),
                "importance": 5,
                "tags": "js,retry,backoff,exponential",
            }
        ],
        "starter_files": {
            "package.json": '{"name": "pilot-retry", "private": true}\n',
            "README.md": "demo project\n",
        },
        "check_files": {
            "hidden_checks/check.js": (
                "const assert = require('node:assert');\n"
                "const retry = require('../retry.js');\n"
                "assert.strictEqual(typeof retry, 'function');\n"
                "(async () => {\n"
                "  const stamps = [];\n"
                "  let attempts = 0;\n"
                "  await assert.rejects(async () => {\n"
                "    await retry(() => { attempts++; stamps.push(Date.now());"
                " throw new Error('boom'); });\n"
                "  }, /boom/);\n"
                "  assert.strictEqual(attempts, 5, 'exactly 5 attempts');\n"
                "  const gaps = [];\n"
                "  for (let i = 1; i < stamps.length; i++) gaps.push(stamps[i] - stamps[i-1]);\n"
                "  assert.ok(gaps[0] >= 80 && gaps[0] <= 250, 'first wait ~100ms, got ' + gaps[0]);\n"
                "  assert.ok(gaps[1] > gaps[0], 'backoff grows: ' + gaps);\n"
                "  let okAttempts = 0;\n"
                "  const value = await retry(() => { okAttempts++;"
                " if (okAttempts < 3) throw new Error('x'); return 42; });\n"
                "  assert.strictEqual(value, 42);\n"
                "  assert.strictEqual(okAttempts, 3);\n"
                "  console.log('CHECK PASS');\n"
                "})().catch((e) => { console.error(e); process.exit(1); });\n"
            ),
        },
        "check_cmd": ["node", "hidden_checks/check.js"],
    },
    {
        "id": "sql_qmark_params",
        "title": "SQL uses qmark placeholders in this (legacy) project",
        "prompt": (
            "Create a module user_store.py in the project root with a "
            "function get_user(db, user_id) that returns the row from the "
            "users table with the given id, or None if there is no such "
            "row. db is a sqlite3 connection. Follow this project's "
            "database conventions. Create the file; do not run anything."
        ),
        "seed_facts": [
            {
                "category": "constraint",
                "content": (
                    "All SQL in this project uses qmark placeholders (?) "
                    "with a tuple binding. This codebase predates named "
                    "parameters: never use named parameters, percent "
                    "formatting, or f-strings to build SQL queries."
                ),
                "importance": 5,
                "tags": "python,sql,qmark,legacy",
            }
        ],
        "starter_files": {
            "README.md": "demo project\nThis project has strict database conventions.\n",
        },
        "check_files": {
            "hidden_checks/check.py": (
                "import sqlite3, sys\n"
                "from pathlib import Path\n"
                "root = Path.cwd()\n"
                "mod = root / 'user_store.py'\n"
                "assert mod.is_file(), 'user_store.py missing'\n"
                "src = mod.read_text(encoding='utf-8')\n"
                "assert '?' in src, 'qmark placeholder ? required'\n"
                "assert ':user_id' not in src and ':uid' not in src, 'named parameters are banned'\n"
                "assert '%s' not in src and '%(' not in src, 'percent formatting is banned'\n"
                "sys.path.insert(0, str(root))\n"
                "from user_store import get_user\n"
                "db = sqlite3.connect(':memory:')\n"
                "db.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)')\n"
                "db.execute(\"INSERT INTO users (id, name) VALUES (1, 'ada'), (2, 'grace')\")\n"
                "row = get_user(db, 2)\n"
                "assert row is not None and 'grace' in str(row), row\n"
                "assert get_user(db, 999) is None\n"
                "print('CHECK PASS')\n"
            ),
        },
        "check_cmd": ["{python}", "hidden_checks/check.py"],
    },
    {
        "id": "explicit_wins",
        "title": "Explicit prompt contract wins over memory (no hijack)",
        "prompt": (
            "Create a module greeter.py in the project root with a "
            "function greet(name) that returns the string "
            "`Hello, <name>!` with a trailing exclamation mark. The "
            "exclamation mark is required by this task's explicit "
            "contract. Create the file; do not run anything."
        ),
        "seed_facts": [
            {
                "category": "constraint",
                "content": (
                    "In this codebase greet(name) returns `Hi, <name>` "
                    "with no trailing punctuation. All greeting functions "
                    "follow this punctuation convention."
                ),
                "importance": 5,
                "tags": "python,greeting,punctuation",
            }
        ],
        "starter_files": {
            "README.md": "demo project\n",
        },
        "check_files": {
            "hidden_checks/check.py": (
                "import sys\n"
                "from pathlib import Path\n"
                "root = Path.cwd()\n"
                "sys.path.insert(0, str(root))\n"
                "from greeter import greet\n"
                "assert greet('ada') == 'Hello, ada!', greet('ada')\n"
                "print('CHECK PASS')\n"
            ),
        },
        "check_cmd": ["{python}", "hidden_checks/check.py"],
    },
]
