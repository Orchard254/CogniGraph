"""Task definitions for the CogniGraph multi-condition evaluation.

Each task mirrors real coding situations but stays small and deterministic so
hidden checks can adjudicate outcomes without an LLM judge.

Task conventions:
- bug_counter_clamp: fix a bug requiring use of a related component (Limiter).
- cross_file_tax: identify and update related components across files.
- trust_explicit_contract: an (intentionally outdated/misleading) memory fact
  conflicts with the task's explicit contract; the prompt contract wins.
"""
from __future__ import annotations

TASKS: list[dict] = [
    {
        "id": "bug_counter_clamp",
        "title": "Fix Counter so it respects the project-wide limit via Limiter",
        "prompt": (
            "Fix the Counter bug in counter.py so counter values cannot exceed "
            "this project's maximum allowed value. limiter.py defines the "
            "authoritative limit and must not be changed. Update any related "
            "code if necessary. Create or modify files; do not run anything."
        ),
        "seed_facts": [
            {
                "category": "constraint",
                "content": (
                    "Counter stores a running total that must be clamped through "
                    "Limiter.clamp on every mutation; Limiter defines the "
                    "project-wide maximum of 100 and is the single source of "
                    "truth for clamping. Do not reimplement the limit inside "
                    "Counter."
                ),
                "importance": 5,
                "tags": "python,counter,limiter,clamp,bug",
                "confidence": "confirmed",
            }
        ],
        "starter_files": {
            "limiter.py": (
                "class Limiter:\n"
                "    def clamp(self, value):\n"
                "        return max(0, min(value, 100))\n"
            ),
            "counter.py": (
                "from limiter import Limiter\n"
                "\n"
                "class Counter:\n"
                "    def __init__(self):\n"
                "        self.value = 0\n"
                "        self.limiter = Limiter()\n"
                "\n"
                "    def increment(self, amount=1):\n"
                "        # BUG: bypasses the project limiter\n"
                "        self.value = self.value + amount\n"
                "        return self.value\n"
                "\n"
                "    def reset(self):\n"
                "        self.value = 0\n"
            ),
        },
        "check_files": {
            "hidden_checks/check.py": (
                "import sys\n"
                "from pathlib import Path\n"
                "root = Path.cwd()\n"
                "sys.path.insert(0, str(root))\n"
                "from counter import Counter\n"
                "from limiter import Limiter\n"
                "c = Counter()\n"
                "assert c.increment(50) == 50, c.increment(50)\n"
                "assert c.increment(80) == 100, c.increment(80)\n"
                "assert c.value == 100\n"
                "c.reset()\n"
                "assert c.increment(10) == 10\n"
                "src = (root / 'limiter.py').read_text(encoding='utf-8')\n"
                "assert '100' in src\n"
                "print('CHECK PASS')\n"
            ),
        },
        "check_cmd": ["{python}", "hidden_checks/check.py"],
    },
    {
        "id": "cross_file_tax",
        "title": "Add tax handling across config, pricing, and invoice",
        "prompt": (
            "Add tax handling to invoice.py so invoice totals include this "
            "project's tax. Implement according to this project's "
            "tax-application conventions. Do not run anything."
        ),
        "seed_facts": [
            {
                "category": "constraint",
                "content": (
                    "All monetary totals in this project must be computed by "
                    "pricing.apply_tax at the invoice level: invoice.total calls "
                    "pricing.apply_tax on the sum of line_items, and tax is never "
                    "applied per line. config.TAX_RATE is the source of the tax "
                    "rate."
                ),
                "importance": 5,
                "tags": "python,pricing,invoice,tax,cross-file",
                "confidence": "confirmed",
            }
        ],
        "starter_files": {
            "config.py": "TAX_RATE = 0.2\n",
            "pricing.py": (
                "from config import TAX_RATE\n"
                "\n"
                "def apply_tax(amount):\n"
                "    return amount * (1 + TAX_RATE)\n"
            ),
            "invoice.py": (
                "def total(line_items):\n"
                "    return sum(line_items)\n"
            ),
        },
        "check_files": {
            "hidden_checks/check.py": (
                "import sys\n"
                "from pathlib import Path\n"
                "root = Path.cwd()\n"
                "sys.path.insert(0, str(root))\n"
                "from invoice import total\n"
                "from pricing import apply_tax\n"
                "items = [10, 20, 30]\n"
                "expected = apply_tax(sum(items))\n"
                "assert total(items) == expected, (total(items), expected)\n"
                "assert apply_tax(100) == 120.0\n"
                "src = (root / 'invoice.py').read_text(encoding='utf-8')\n"
                "assert 'pricing' in src and 'apply_tax' in src\n"
                "print('CHECK PASS')\n"
            ),
        },
        "check_cmd": ["{python}", "hidden_checks/check.py"],
    },
    {
        "id": "trust_explicit_contract",
        "title": "Explicit prompt contract wins over stale memory",
        "prompt": (
            "Create a module greeter.py in the project root with a function "
            "greet(name) that returns the string `Hello, <name>!` with a "
            "trailing exclamation mark. The exclamation mark is required by "
            "this task's explicit contract. Create the file; do not run "
            "anything."
        ),
        "seed_facts": [
            {
                "category": "constraint",
                "content": (
                    "In this codebase greet(name) returns `Hi, <name>` with no "
                    "trailing punctuation. All greeting functions follow this "
                    "punctuation convention."
                ),
                "importance": 5,
                "tags": "python,greeting,punctuation",
                "confidence": "confirmed",
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
