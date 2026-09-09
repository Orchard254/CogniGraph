#!/usr/bin/env python3
"""
Extract AST structures from memory_store.py using Python's ast module.
Report top-level functions/classes, methods, and imports.
"""

import ast
import inspect
from pathlib import Path

# Resolve against the repo root so the script works from any CWD.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Read the target file
with open(REPO_ROOT / 'memory_store.py', 'r') as f:
    source_code = f.read()
    lines = source_code.split('\n')

# Parse into AST
tree = ast.parse(source_code)

print("=" * 80)
print("AST EXTRACTION: memory_store.py")
print("=" * 80)

# IMPORTS
print("\n" + "=" * 80)
print("IMPORT STATEMENTS")
print("=" * 80)
for node in ast.walk(tree):
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        line_no = node.lineno
        print(f"\nLine {line_no}: node type = '{node.__class__.__name__}'")
        if isinstance(node, ast.Import):
            names = ', '.join([alias.name for alias in node.names])
            print(f"  Type: import")
            print(f"  Imports: {names}")
            # Show source line
            if line_no - 1 < len(lines):
                print(f"  Source: {lines[line_no - 1]}")
        else:  # ImportFrom
            module = node.module or '(none)'
            names = ', '.join([alias.asname or alias.name for alias in node.names])
            print(f"  Type: from...import")
            print(f"  Module: {module}")
            print(f"  Imports: {names}")
            # Show source line
            if line_no - 1 < len(lines):
                print(f"  Source: {lines[line_no - 1]}")

# TOP-LEVEL FUNCTIONS
print("\n" + "=" * 80)
print("TOP-LEVEL FUNCTIONS")
print("=" * 80)
for node in tree.body:
    if isinstance(node, ast.FunctionDef):
        line_start = node.lineno
        line_end = node.end_lineno or line_start
        print(f"\nFunction: {node.name}")
        print(f"  Lines: {line_start}-{line_end}")
        print(f"  AST node type: function_definition")
        # Show decorator if any
        if node.decorator_list:
            for dec in node.decorator_list:
                if isinstance(dec, ast.Name):
                    print(f"  Decorator: @{dec.id}")

# CLASS DEFINITIONS (with methods)
print("\n" + "=" * 80)
print("CLASS DEFINITIONS (with methods)")
print("=" * 80)
for node in tree.body:
    if isinstance(node, ast.ClassDef):
        line_start = node.lineno
        line_end = node.end_lineno or line_start
        print(f"\nClass: {node.name}")
        print(f"  Lines: {line_start}-{line_end}")
        print(f"  AST node type: class_definition")
        
        # Extract methods
        methods = []
        for item in node.body:
            if isinstance(item, ast.FunctionDef):
                methods.append({
                    'name': item.name,
                    'line_start': item.lineno,
                    'line_end': item.end_lineno or item.lineno,
                    'is_property': any(
                        isinstance(d, ast.Name) and d.id == 'property'
                        for d in item.decorator_list
                    )
                })
        
        if methods:
            for method in methods:
                prop_str = " [@property]" if method['is_property'] else ""
                print(f"    Method: {method['name']}{prop_str}")
                print(f"      Lines: {method['line_start']}-{method['line_end']}")

# LINE NUMBER CONVENTION CHECK
print("\n" + "=" * 80)
print("LINE NUMBER CONVENTION IN memory_store.py")
print("=" * 80)
print("\nPython's ast module uses 1-indexed line numbers (lineno starts at 1)")
print("tree-sitter uses 0-indexed points (start_point[0] is 0-indexed)")
print("\nWhen memory_store.py stores line_start/line_end in the memory table,")
print("it should use 1-indexed (matching Python's ast module convention).")

# Show example: first function's line numbers
for node in tree.body:
    if isinstance(node, ast.FunctionDef):
        print(f"\nExample: First function '{node.name}'")
        print(f"  ast.FunctionDef.lineno = {node.lineno} (1-indexed)")
        print(f"  ast.FunctionDef.end_lineno = {node.end_lineno} (1-indexed)")
        print(f"  Actual code from file:")
        for i in range(node.lineno - 1, min(node.end_lineno, node.lineno + 3)):
            print(f"    Line {i+1}: {lines[i][:80]}")
        break

# IMPORT NODE STRUCTURE
print("\n" + "=" * 80)
print("IMPORT STATEMENT DETAILS")
print("=" * 80)
import_stmts = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
print(f"\nTotal imports found: {len(import_stmts)}")

# Show structure of a few examples
print("\nExample import structures (first 5):")
for i, imp in enumerate(import_stmts[:5]):
    print(f"\n[Import {i+1}] Line {imp.lineno}")
    if isinstance(imp, ast.Import):
        print(f"  Type: Import (import ...)")
        print(f"  Names: {[alias.name for alias in imp.names]}")
    else:
        print(f"  Type: ImportFrom (from ... import)")
        print(f"  Module: {imp.module}")
        print(f"  Names: {[alias.name for alias in imp.names]}")
        print(f"  Level: {imp.level} (0=absolute, >0=relative)")
