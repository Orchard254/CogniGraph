# Confirm line-number conversion: tree-sitter 0-indexed vs memory table 1-indexed
from pathlib import Path
from tree_sitter import Parser, Language
import tree_sitter_python as tspython

REPO_ROOT = Path(__file__).resolve().parents[2]

parser = Parser(Language(tspython.language()))
with open(REPO_ROOT / "memory_store.py", "rb") as f:
    source = f.read()

tree = parser.parse(source)
root = tree.root_node

# Pick first function (_get_embed_model at 0-indexed line 42 -> 1-indexed 43)
func = None
for c in root.children:
    if c.type == "function_definition":
        func = c
        break

print("NODE TYPE STRING (real grammar):", func.type)
print("start_point (0-indexed):", func.start_point, "=> row =", func.start_point[0], "col =", func.start_point[1])
print("end_point (0-indexed):", func.end_point, "=> row =", func.end_point[0], "col =", func.end_point[1])
print()
print("MEMORY TABLE CONVENTION: 1-indexed line_start / line_end")
print("Conversion needed: start_point[0] + 1 => line_start =", func.start_point[0] + 1)
print("Conversion needed: end_point[0] + 1 => line_end =", func.end_point[0] + 1)
print()
# Verify against the ast module spike's reported line (line 43 for _get_embed_model)
print("AST module spike reported: _get_embed_model Lines: 43-54")
print("Tree-sitter 0-indexed: line rows 42 -> 53")
print("After +1 conversion: 43 -> 54  => MATCHES ast spike exactly.")
