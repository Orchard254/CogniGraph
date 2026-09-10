#!/usr/bin/env python3
"""
Extract structural nodes (imports, functions, classes, methods) from Python files
and write them as facts into the MemoryStore. This is a safe, idempotent batch
operation: it uses MemoryStore.write() which applies the project's auto-dedup guard.

Usage:
  python scripts/dev/extract_structures_and_write.py --root . [--apply]

By default runs in dry-run mode and prints planned facts. With --apply it writes.
"""
import argparse
import ast
from pathlib import Path
import os
import sys

# Ensure project root is on PYTHONPATH
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from memory_store import MemoryStore

CODE_SUFFIX = '.py'


def extract_structures_from_source(source_text: str):
    tree = ast.parse(source_text)
    results = []
    # Imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            line = node.lineno
            names = [alias.name + (f" as {alias.asname}" if alias.asname else "") for alias in node.names]
            content = "import " + ", ".join(names)
            results.append({
                'sub_type': 'import', 'content': content, 'line_start': line, 'line_end': line
            })
        elif isinstance(node, ast.ImportFrom):
            line = node.lineno
            module = node.module or ''
            names = [alias.name + (f" as {alias.asname}" if alias.asname else "") for alias in node.names]
            prefix = f"from {'.'*node.level + module if node.level else module} import " if module or node.level else "from ? import "
            content = prefix + ", ".join(names)
            results.append({
                'sub_type': 'import', 'content': content, 'line_start': line, 'line_end': line
            })
    # Top-level functions and classes
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            ls = node.lineno
            le = node.end_lineno or ls
            name = node.name
            content = f"function {name}"
            results.append({'sub_type': 'function', 'content': content, 'line_start': ls, 'line_end': le})
        elif isinstance(node, ast.ClassDef):
            cls_ls = node.lineno
            cls_le = node.end_lineno or cls_ls
            cls_name = node.name
            content = f"class {cls_name}"
            results.append({'sub_type': 'class', 'content': content, 'line_start': cls_ls, 'line_end': cls_le})
            # methods
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    m_ls = item.lineno
                    m_le = item.end_lineno or m_ls
                    m_name = item.name
                    m_content = f"method {cls_name}.{m_name}"
                    results.append({'sub_type': 'method', 'content': m_content, 'line_start': m_ls, 'line_end': m_le})
    return results


def iter_py_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        # prune common ignores similar to bootstrap
        dirnames[:] = [d for d in dirnames if d not in {
            '.git', 'venv', '.venv', 'node_modules', '__pycache__', '.project-memory', '.cognigraph', 'dist', 'build'
        }]
        for fn in filenames:
            if fn.lower().endswith(CODE_SUFFIX) and not fn.lower().endswith('.pyc'):
                yield Path(dirpath) / fn


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', default='.', help='Project root')
    p.add_argument('--apply', action='store_true', help='Write facts (default: dry-run)')
    p.add_argument('--max', type=int, default=10000, help='Max files to process')
    p.add_argument('--db', default=None, help='Target DB for --apply (required with --apply; never defaults to the live store)')
    args = p.parse_args()

    root = Path(args.root).resolve()
    print(f"Root: {root}")
    # NOTE: dev-only script. --apply requires an explicit --db so spike runs
    # can never silently write into the live store.
    store = MemoryStore(args.db) if args.db else None
    planned = []
    count = 0
    for fp in iter_py_files(root):
        rel = fp.relative_to(root)
        try:
            text = fp.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        entries = extract_structures_from_source(text)
        for e in entries:
            fact = {
                'category': 'component',
                'content': e['content'],
                'file_path': str(rel).replace('\\', '/'),
                'tags': 'phase-b,ast-extracted,python',
                'importance': 2,
                'line_start': e['line_start'],
                'line_end': e['line_end'],
                'rationale': f"extracted {e['sub_type']} via ast",
            }
            planned.append(fact)
        count += 1
        if count >= args.max:
            break

    print(f"Planned facts: {len(planned)} from {count} files")
    if not args.apply:
        for i, f in enumerate(planned[:200], 1):
            print(f"{i:4}: {f['file_path']}:{f['line_start']}-{f['line_end']} {f['content']}")
        print('Dry-run complete. Use --apply to write facts.')
    else:
        if not args.db:
            print('Refusing --apply without --db: pass an explicit target DB.')
            return
        ids = store.write_batch(planned, force=False)
        print(f"Wrote {len(ids)} results (some may be negative sentinels for duplicates)")
        try:
            store.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
