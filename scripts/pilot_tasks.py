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
]
