# Real tree-sitter extraction (not just "parses")
# Uses modern constructor API: Parser(Language(pkg.language()))
from tree_sitter import Parser, Language
import tree_sitter_python as tspython
import tree_sitter_javascript as tsjs

def extract_python(file_path):
    parser = Parser(Language(tspython.language()))
    with open(file_path, "rb") as f:
        source = f.read()
    tree = parser.parse(source)
    root = tree.root_node
    print(f"Python file: {file_path}")
    print(f"Root node type: {root.type!r}  start_point={root.start_point} end_point={root.end_point}")
    print()

    # Top-level functions
    print("=== TOP-LEVEL FUNCTIONS ===")
    for child in root.children:
        if child.type == "function_definition":
            name_node = child.child_by_field_name("name")
            name = name_node.text.decode() if name_node else "?"
            print(f"  type={child.type!r}  name={name!r}  "
                  f"start={child.start_point[0]+1}:{child.start_point[1]} end={child.end_point[0]+1}:{child.end_point[1]}  "
                  f"(0-indexed line {child.start_point[0]} -> {child.end_point[0]}, +1 => 1-indexed)")

    # Classes + methods
    print()
    print("=== CLASSES ===")
    for child in root.children:
        if child.type == "class_definition":
            name_node = child.child_by_field_name("name")
            name = name_node.text.decode() if name_node else "?"
            print(f"  type={child.type!r}  name={name!r}  "
                  f"start={child.start_point[0]+1} end={child.end_point[0]+1}  "
                  f"(0-indexed line {child.start_point[0]} -> {child.end_point[0]})")
            # Methods inside
            body = None
            for c2 in child.children:
                if c2.type == "block":
                    body = c2
            if body:
                for m in body.children:
                    if m.type == "function_definition":
                        mname_node = m.child_by_field_name("name")
                        mname = mname_node.text.decode() if mname_node else "?"
                        print(f"      -> method type={m.type!r} name={mname!r}  "
                              f"start={m.start_point[0]+1} end={m.end_point[0]+1}")

    # Imports at top level
    print()
    print("=== IMPORT STATEMENTS ===")
    for child in root.children:
        if child.type in ("import_statement", "import_from_statement"):
            line_0 = child.start_point[0] + 1
            print(f"  type={child.type!r}  line (1-indexed)={line_0}  0-indexed={child.start_point[0]}  raw={child}")


def extract_js(file_path):
    parser = Parser(Language(tsjs.language()))
    with open(file_path, "rb") as f:
        source = f.read()
    tree = parser.parse(source)
    root = tree.root_node
    print(f"JS file: {file_path}")
    print(f"Root node type: {root.type!r}  start_point={root.start_point} end_point={root.end_point}")
    print()
    def walk(node, depth=0):
        indent = "    " * depth
        if node.type in ("function_declaration", "class_declaration", "method_definition",
                         "lexical_declaration", "variable_declaration", "expression_statement"):
            line_0 = node.start_point[0]
            line_1 = node.end_point[0]
            line_0_1idx = line_0 + 1
            line_1_1idx = line_1 + 1
            # Extract identifier/name where present
            extra = ""
            for c in node.children:
                if c.type == "identifier" and extra == "":
                    extra = c.text.decode()
            print(f"{indent}type={node.type!r}  line(1-indexed)={line_0_1idx}-{line_1_1idx}  "
                  f"0-indexed={line_0}-{line_1}  name/extra={extra!r}")
        for c in node.children:
            if c.type in ("function_declaration", "class_declaration", "method_definition",
                          "lexical_declaration", "variable_declaration", "expression_statement",
                          "statement_block", "program"):
                walk(c, depth + (1 if node.type == "program" or node.type == "statement_block" else 0))
    walk(root)

if __name__ == "__main__":
    print("=" * 70)
    print("TREE-SITTER REAL EXTRACTION (Python grammar)")
    print("Using: Parser(Language(tree_sitter_python.language()))  [modern constructor API]")
    print("=" * 70)
    extract_python("memory_store.py")

    # Small JS sample
    js_sample = b"function foo() { return 1; }\nclass Bar { baz() { return 2; } }\nconst x = require('y');\n"
    with open("test_sample.js", "wb") as f:
        f.write(js_sample)

    print()
    print("=" * 70)
    print("TREE-SITTER REAL EXTRACTION (JavaScript grammar)")
    print("=" * 70)
    extract_js("test_sample.js")
