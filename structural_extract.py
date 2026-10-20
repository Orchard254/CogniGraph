"""
Structural extraction from source files using tree-sitter 0.26.0.

API verification (core 0.26.0, empirically tested):
  - Parser(Language(pkg.language())) works (same modern constructor).
  - Query API: Query(language, pattern_string) creates compiled query.
  - QueryCursor(query) then .matches(node) returns capture results.
  - There is NO .captures(node) method in 0.26.0; the previous
    `language.query()` method also does not exist. Given this churn,
    manual recursive descent is used here for stability rather than
    relying on an API that changed twice (Parser constructor + Query
    captures interface) within recent releases.

Public interface:
    extract_structures(file_path: Path) -> list[dict]

Dispatch: .py -> python grammar; .js/.jsx -> javascript grammar;
.ts -> typescript grammar; .tsx -> tsx grammar;
anything else -> empty list with explicit skip note.
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Tree-sitter is optional; degrade gracefully if not installed.
# Import each grammar independently — missing TS should not disable Python/JS.
try:
    from tree_sitter import Parser, Language
except ImportError:  # pragma: no cover
    Parser = None  # type: ignore
    Language = None  # type: ignore

try:
    import tree_sitter_python as tspython
except ImportError:
    tspython = None  # type: ignore

try:
    import tree_sitter_javascript as tsjs
except ImportError:
    tsjs = None  # type: ignore

try:
    import tree_sitter_typescript as tsts
except ImportError:
    tsts = None  # type: ignore


def _python_parser():
    if Parser is None or tspython is None:
        raise ImportError("tree-sitter-python not available")
    return Parser(Language(tspython.language()))


def _js_parser():
    if Parser is None or tsjs is None:
        raise ImportError("tree-sitter-javascript not available")
    return Parser(Language(tsjs.language()))


def _ts_parser():
    if Parser is None or tsts is None:
        raise ImportError("tree-sitter-typescript not available")
    # tree-sitter-typescript exports both typescript and tsx languages
    return Parser(Language(tsts.language_typescript()))


def _tsx_parser():
    if Parser is None or tsts is None:
        raise ImportError("tree-sitter-typescript not available")
    return Parser(Language(tsts.language_tsx()))


def _line_1(node) -> int:
    """Convert 0-indexed line number to 1-indexed line number."""
    return node.start_point[0] + 1


def _line_end_1(node) -> int:
    return node.end_point[0] + 1


def _node_text(node) -> str:
    return node.text.decode("utf-8") if node.text else ""


def _callee_name(call_node):
    """Resolve the callee text of a call node: identifier or attribute tail.

    Returns 'self.write' for self.write(...), 'query_hybrid' for a bare call,
    None for indirect callees (subscripts, nested calls) which cannot be
    resolved statically without type inference.
    """
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "identifier":
        return _node_text(fn)
    if fn.type == "attribute":
        attr = fn.child_by_field_name("attribute")
        base = fn.child_by_field_name("object")
        if attr is None:
            return None
        if base is not None and base.type == "identifier":
            return f"{_node_text(base)}.{_node_text(attr)}"
        return _node_text(attr)
    return None


def _js_callee_name(call_node):
    """Resolve the callee text of a JS/TS call node: identifier or
    member-expression tail. JS/TS mirror of _callee_name.

    Returns 'helper' for helper(...), 'fs.readFile' for fs.readFile(...),
    None for computed/indirect callees which cannot be resolved statically.
    """
    fn = call_node.child_by_field_name("function")
    if fn is None:
        for c in call_node.children:
            if c.type in ("identifier", "member_expression"):
                fn = c
                break
    if fn is None:
        return None
    if fn.type == "identifier":
        return _node_text(fn)
    if fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        base = fn.child_by_field_name("object")
        if prop is None:
            return None
        if base is not None and base.type == "identifier":
            return f"{_node_text(base)}.{_node_text(prop)}"
        return _node_text(prop)
    return None


# Keywords that can appear as identifier leaves inside a heritage subtree
# (plain-JS `implements` parses as an ERROR-node identifier) — never bases.
_HERITAGE_KEYWORDS = frozenset({"extends", "implements"})


def _heritage_bases(node) -> list:
    """Collect base-class name tails from a class-heritage subtree.

    Returns bare identifier tails in declaration order (dotted bases collapse
    to their last segment, the same convention as _callee_name): 'pkg.Mod'
    contributes 'Mod'. Keyword leaves (extends/implements) and
    keyword-argument subtrees (e.g. metaclass=...) are skipped; subscripts
    (e.g. Generic[T]) contribute only their value tail.
    """
    bases = []

    def add(text: str):
        if text and text not in _HERITAGE_KEYWORDS and text not in bases:
            bases.append(text)

    def visit(n):
        if n.type == "attribute":
            attr = n.child_by_field_name("attribute")
            if attr is not None:
                add(_node_text(attr))
            return
        if n.type == "member_expression":
            prop = n.child_by_field_name("property")
            if prop is not None:
                add(_node_text(prop))
            return
        if n.type == "subscript":
            val = n.child_by_field_name("value")
            if val is not None:
                visit(val)
            return
        if n.type in ("keyword_argument",):
            return
        if n.type in ("identifier", "type_identifier", "property_identifier"):
            add(_node_text(n))
            return
        for ch in n.children:
            visit(ch)

    visit(node)
    return bases


def _python_bases(class_node) -> list:
    """Bases of a Python class_definition (superclasses field), or []."""
    sup = class_node.child_by_field_name("superclasses")
    if sup is None:
        return []
    return _heritage_bases(sup)


def _js_heritage_bases(class_node) -> list:
    """Bases of a JS/TS/TSX class_declaration (class_heritage child), or []."""
    for ch in class_node.children:
        if ch.type == "class_heritage":
            return _heritage_bases(ch)
    return []


# Annotations that never name a class (last-identifier extraction skips them).
_NON_CLASS_ANNOTATIONS = frozenset({
    "None", "Any", "bool", "int", "float", "str", "bytes", "list", "dict",
    "tuple", "set", "frozenset", "type", "object",
})


def _python_factories(root) -> dict:
    """Map module-level factory function name -> returned class name.

    Bounded local inference only (no cross-file lookup, no general type
    inference):
    (a) return-annotation tail — `def _store() -> Iterator["MemoryStore"]`
        yields `_store -> MemoryStore` (last identifier of the annotation);
    (b) constructor return — `return MemoryStore()` where MemoryStore is a
        class defined in the same file (covers unannotated `_get_store`).
    Anything else stays unknown and its aliases never rewrite (conservative:
    today's dropped-edge behavior).
    """
    classes = set()
    funcs = []
    for child in root.children:
        node = child
        # Unwrap decorated definitions (@contextmanager def _store ...).
        if node.type == "decorated_definition":
            for sub in node.children:
                if sub.type == "function_definition":
                    node = sub
                    break
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                classes.add(_node_text(name_node))
        elif node.type == "function_definition":
            funcs.append(node)
    out = {}
    for fn in funcs:
        name_node = fn.child_by_field_name("name")
        fname = _node_text(name_node) if name_node is not None else ""
        if not fname or fname in out:
            continue
        ret = fn.child_by_field_name("return_type")
        if ret is not None:
            idents = re.findall(r"[A-Za-z_]\w*", _node_text(ret))
            # Known limitation: list[Widget]-style tails can't fire on runnable code; left as-is.
            tail = idents[-1] if idents else ""
            if tail and tail not in _NON_CLASS_ANNOTATIONS:
                out[fname] = tail
                continue
        found = _find_returned_class(fn, classes)
        if found:
            out[fname] = found
    return out


def _find_returned_class(fn_node, classes: set) -> str | None:
    """Class (from the file's own class set) constructed in a `return` stmt."""
    result = None

    def visit(n):
        nonlocal result
        if result is not None:
            return
        if n.type == "return_statement":
            for ch in n.children:
                if ch.type == "call":
                    callee = ch.child_by_field_name("function")
                    if callee is not None and callee.type == "identifier":
                        cname = _node_text(callee)
                        if cname in classes:
                            result = cname
                            return
            return
        # Do not descend into nested definitions: their returns belong to
        # another scope.
        if n is not fn_node and n.type in ("function_definition", "lambda"):
            return
        for ch in n.children:
            visit(ch)

    visit(fn_node)
    return result


def _with_aliases(with_node, factories: dict) -> list:
    """(alias, class) pairs bound by `with FACTORY() as VAR` items in one
    with_statement. Only factories with an inferred class contribute."""
    pairs = []
    for clause in with_node.children:
        if clause.type != "with_clause":
            continue
        for item in clause.children:
            if item.type != "with_item":
                continue
            call_node, target = None, None
            for sub in item.children:
                if sub.type == "as_pattern":
                    for part in sub.children:
                        if part.type == "call" and call_node is None:
                            call_node = part
                        elif part.type == "as_pattern_target" and target is None:
                            target = part
            if call_node is None or target is None:
                continue
            callee = call_node.child_by_field_name("function")
            if callee is None or callee.type != "identifier":
                continue
            cls = factories.get(_node_text(callee))
            var_node = target.child_by_field_name("name")
            var = _node_text(var_node) if var_node is not None else _node_text(target)
            if cls and var:
                pairs.append((var, cls))
    return pairs


# ------------------------------------------------------------------
# Python recursive extraction (full tree walk, not just root children)
# ------------------------------------------------------------------

def _extract_python(file_path: Path):
    parser = _python_parser()
    source = file_path.read_bytes()
    tree = parser.parse(source)
    root = tree.root_node
    items = []
    factories = _python_factories(root)

    # Stack holds (node, current_class_name_or_None)
    def walk(node, current_class: str | None = None, scope_item: dict | None = None,
             aliases: tuple = (), decorators: tuple = ()):
        # Capture calls into the innermost enclosing definition so call edges
        # can be derived later without a second AST pass.
        if node.type == "call" and scope_item is not None:
            callee = _callee_name(node)
            if callee and "." in callee:
                # Bounded instance-alias rewrite: VAR.method inside the
                # `with FACTORY() as VAR:` body that binds VAR becomes
                # Class.method (innermost binding wins); anything outside the
                # block keeps the VAR.method form and still drops at resolve
                # time, exactly as before.
                base, _tail = callee.split(".", 1)
                for aname, acls in reversed(aliases):
                    if base == aname:
                        callee = f"{acls}.{_tail}"
                        break
            if callee:
                if callee not in scope_item.setdefault("calls", []):
                    scope_item["calls"].append(callee)
                # Per-site lines for the same callee (a caller often repeats
                # a callee across branches); recorded under the rewritten name.
                ln = _line_1(node)
                lines = scope_item.setdefault("call_sites", {}).setdefault(callee, [])
                if ln not in lines:
                    lines.append(ln)

        # --- Imports (must find nested ones inside try/except/if/etc) ---
        if node.type in ("import_statement", "import_from_statement"):
            name_text = _node_text(node)
            items.append({
                "type": "import",
                "name": name_text.splitlines()[0] if name_text else node.type,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            })

        # --- Decorated definitions: capture decorators, then handle body def ---
        elif node.type == "decorated_definition":
            decs = []
            for ch in node.children:
                if ch.type == "decorator":
                    decs.append(_node_text(ch))
            for ch in (c for c in node.children if c.type in ("function_definition", "class_definition")):
                walk(ch, current_class=current_class, scope_item=scope_item,
                     aliases=aliases, decorators=decs)
            return

        # --- Functions ---
        elif node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            name = _node_text(name_node) if name_node else "unknown>"
            item = {
                "type": "method" if current_class else "function",
                "name": name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            }
            if decorators:
                item["decorators"] = list(decorators)
            if current_class:
                item["class_name"] = current_class
            items.append(item)
            # Recurse into function body with current_class=None (closure/local)
            # and no aliases: bindings never cross function boundaries.
            for ch in node.children:
                walk(ch, current_class=None, scope_item=item, aliases=(), decorators=())
            return

        # --- With-blocks: extend aliases for the body only ---
        elif node.type == "with_statement":
            bound = tuple(_with_aliases(node, factories))
            for ch in node.children:
                if ch.type == "block":
                    walk(ch, current_class=current_class, scope_item=scope_item,
                         aliases=aliases + bound, decorators=decorators)
                else:
                    # with_clause (context expressions evaluate in the outer
                    # scope) and punctuation: outer aliases.
                    walk(ch, current_class=current_class, scope_item=scope_item,
                         aliases=aliases, decorators=decorators)
            return

        # --- Classes ---
        elif node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            class_name = _node_text(name_node) if name_node else "unknown>"
            citem = {
                "type": "class",
                "name": class_name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
                "bases": _python_bases(node),
            }
            if decorators:
                citem["decorators"] = list(decorators)
            items.append(citem)
            # Walk children with this class in scope; nested functions become methods
            for ch in node.children:
                walk(ch, current_class=class_name, scope_item=scope_item, aliases=aliases, decorators=())
            return  # children already walked with class scope; don't double-walk

        # --- General recursive descent for anything else ---
        for ch in node.children:
            walk(ch, current_class=current_class, scope_item=scope_item, aliases=aliases, decorators=decorators)

    walk(root, decorators=())
    return items


# ------------------------------------------------------------------
# JavaScript recursive extraction
# ------------------------------------------------------------------

def _is_require_call(node) -> tuple[bool, str | None]:
    """Detect a call_expression whose callee identifier is exactly 'require'
    AND has a string literal argument. Both conditions must hold."""
    if node.type != "call_expression":
        return False, None
    callee_name = None
    module_str = None
    for c in node.children:
        if c.type == "identifier":
            callee_name = _node_text(c)
        elif c.type == "arguments":
            for arg in c.children:
                if arg.type == "string":
                    module_str = _node_text(arg).strip("'\"`")
    if callee_name == "require" and module_str is not None:
        return True, module_str
    return False, None


def _collect_decorators(node) -> list:
    """Collect raw decorator texts attached to a JS/TS/TSX definition node.

    Decorators may be direct children (class_declaration) or consecutive
    preceding named siblings (method/function), including when a parser wraps
    a leading decorator in an ERROR node. Returns [] when none."""
    decs = [ch for ch in getattr(node, "children", []) if getattr(ch, "type", None) == "decorator"]
    if decs:
        return [_node_text(c) for c in decs]
    # Preceding siblings (possibly wrapped in an ERROR node).
    out = []
    cur = getattr(node, "prev_named_sibling", None)
    if cur is not None and cur.type == "ERROR":
        out.extend(_node_text(c) for c in cur.children if c.type == "decorator")
        if out:
            return out
    cur = getattr(node, "prev_named_sibling", None)
    while cur is not None and cur.type == "decorator":
        out.insert(0, _node_text(cur))
        cur = cur.prev_named_sibling
    return out


def _extract_js(file_path: Path):
    parser = _js_parser()
    source = file_path.read_bytes()
    tree = parser.parse(source)
    root = tree.root_node
    items = []

    def walk(node, current_class: str | None = None, scope_item: dict | None = None):
        # Capture calls into the innermost enclosing definition so call edges
        # can be derived later without a second AST pass.
        if node.type == "call_expression" and scope_item is not None:
            callee = _js_callee_name(node)
            if callee:
                if callee not in scope_item.setdefault("calls", []):
                    scope_item["calls"].append(callee)
                ln = _line_1(node)
                lines = scope_item.setdefault("call_sites", {}).setdefault(callee, [])
                if ln not in lines:
                    lines.append(ln)

        # --- ES import statements ---
        if node.type == "import_statement":
            text_line = _node_text(node).splitlines()[0] if _node_text(node) else "import"
            items.append({
                "type": "import",
                "name": text_line,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            })

        # --- Function declarations ---
        elif node.type == "function_declaration":
            name_node = None
            for c in node.children:
                if c.type == "identifier":
                    name_node = c
                    break
            name = _node_text(name_node) if name_node else "unknown>"
            item = {
                "type": "method" if current_class else "function",
                "name": name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            }
            decorators = _collect_decorators(node)
            if decorators:
                item["decorators"] = decorators
            if current_class:
                item["class_name"] = current_class
            items.append(item)
            # Recurse into function body with current_class=None (closure/local)
            for ch in node.children:
                walk(ch, current_class=None, scope_item=item)
            return

        # --- Arrow function assigned to variable (const foo = () => ...) ---
        elif node.type == "variable_declarator":
            for c in node.children:
                if c.type == "arrow_function":
                    name_node = None
                    for ch in node.children:
                        if ch.type == "identifier":
                            name_node = ch
                            break
                    name = _node_text(name_node) if name_node else "unknown>"
                    item = {
                        "type": "function",
                        "name": name,
                        "file_path": str(file_path),
                        "line_start": _line_1(node),
                        "line_end": _line_end_1(node),
                    }
                    decorators = _collect_decorators(node)
                    if decorators:
                        item["decorators"] = decorators
                    items.append(item)
                    for ch in c.children:
                        walk(ch, current_class=None, scope_item=item)
                    return

        # --- Method definitions inside class ---
        elif node.type == "method_definition":
            name_node = None
            for c in node.children:
                if c.type == "property_identifier" and name_node is None:
                    name_node = c
            # Some JS grammars name the method differently; fall back to identifier
            if name_node is None:
                for c in node.children:
                    if c.type == "identifier" and name_node is None:
                        name_node = c
            name = _node_text(name_node) if name_node else "unknown>"
            item = {
                "type": "method",
                "name": name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            }
            decorators = _collect_decorators(node)
            if decorators:
                item["decorators"] = decorators
            if current_class:
                item["class_name"] = current_class
            items.append(item)
            # Recurse into method body with current_class=None (nested functions are closures)
            for ch in node.children:
                walk(ch, current_class=None, scope_item=item)
            return

        # --- Class declarations ---
        elif node.type == "class_declaration":
            name_node = None
            for c in node.children:
                if c.type == "identifier":
                    name_node = c
                    break
            class_name = _node_text(name_node) if name_node else "unknown>"
            citem = {
                "type": "class",
                "name": class_name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
                "bases": _js_heritage_bases(node),
            }
            decorators = _collect_decorators(node)
            if decorators:
                citem["decorators"] = decorators
            items.append(citem)
            for ch in node.children:
                walk(ch, current_class=class_name, scope_item=scope_item)
            return

        # --- CommonJS require() inside lexical / variable declarations ---
        # Handle both top-level and nested (inside function bodies)
        elif node.type in ("lexical_declaration", "variable_declaration"):
            # Look for a variable_declarator that holds a call_expression
            for c in node.children:
                # Some nodes nest declarator directly; others through children
                sub = c
                if sub.type == "variable_declarator":
                    # Check if its value/init is a call_expression
                    for init in sub.children:
                        if init.type == "call_expression":
                            is_req, module_str = _is_require_call(init)
                            if is_req:
                                # The declaration line is the parent lexical_declaration line
                                items.append({
                                    "type": "import",
                                    "name": module_str or "require(...)",
                                    "file_path": str(file_path),
                                    "line_start": _line_1(node),
                                    "line_end": _line_end_1(node),
                                })

        # --- Also detect bare expression_statement with require() ---
        elif node.type == "expression_statement":
            for c in node.children:
                if c.type == "call_expression":
                    is_req, module_str = _is_require_call(c)
                    if is_req:
                        items.append({
                            "type": "import",
                            "name": module_str or "require(...)",
                            "file_path": str(file_path),
                            "line_start": _line_1(node),
                            "line_end": _line_end_1(node),
                        })

        # General recursive descent for nested structures (e.g. functions inside
        # try/catch, if blocks, or nested functions inside functions)
        for ch in node.children:
            walk(ch, current_class=current_class, scope_item=scope_item)

    walk(root)
    return items


# ------------------------------------------------------------------
# TypeScript/TSX recursive extraction (similar to JavaScript)
# ------------------------------------------------------------------

def _extract_ts(file_path: Path):
    parser = _ts_parser()
    source = file_path.read_bytes()
    tree = parser.parse(source)
    root = tree.root_node
    items = []

    def walk(node, current_class: str | None = None, scope_item: dict | None = None):
        # Capture calls into the innermost enclosing definition so call edges
        # can be derived later without a second AST pass.
        if node.type == "call_expression" and scope_item is not None:
            callee = _js_callee_name(node)
            if callee:
                if callee not in scope_item.setdefault("calls", []):
                    scope_item["calls"].append(callee)
                ln = _line_1(node)
                lines = scope_item.setdefault("call_sites", {}).setdefault(callee, [])
                if ln not in lines:
                    lines.append(ln)

        # --- ES import statements ---
        if node.type == "import_statement":
            text_line = _node_text(node).splitlines()[0] if _node_text(node) else "import"
            items.append({
                "type": "import",
                "name": text_line,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            })

        # --- Function declarations ---
        elif node.type == "function_declaration":
            name_node = None
            for c in node.children:
                if c.type == "identifier":
                    name_node = c
                    break
            name = _node_text(name_node) if name_node else "unknown>"
            item = {
                "type": "method" if current_class else "function",
                "name": name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            }
            decorators = _collect_decorators(node)
            if decorators:
                item["decorators"] = decorators
            if current_class:
                item["class_name"] = current_class
            items.append(item)
            # Recurse into function body with current_class=None (closure/local)
            for ch in node.children:
                walk(ch, current_class=None, scope_item=item)
            return

        # --- Arrow function components (TSX) ---
        elif node.type == "variable_declarator":
            # Check if this is a const/let/var with an arrow function
            for c in node.children:
                if c.type == "arrow_function":
                    # The name is the identifier in the parent variable_declarator
                    name_node = None
                    for ch in node.children:
                        if ch.type == "identifier":
                            name_node = ch
                            break
                    name = _node_text(name_node) if name_node else "unknown>"
                    item = {
                        "type": "function",
                        "name": name,
                        "file_path": str(file_path),
                        "line_start": _line_1(node),
                        "line_end": _line_end_1(node),
                    }
                    decorators = _collect_decorators(node)
                    if decorators:
                        item["decorators"] = decorators
                    items.append(item)
                    # Recurse into arrow function body
                    for ch in c.children:
                        walk(ch, current_class=None, scope_item=item)
                    return

        # --- Method definitions inside class ---
        elif node.type == "method_definition":
            name_node = None
            for c in node.children:
                if c.type == "property_identifier" and name_node is None:
                    name_node = c
            if name_node is None:
                for c in node.children:
                    if c.type == "identifier" and name_node is None:
                        name_node = c
            name = _node_text(name_node) if name_node else "unknown>"
            item = {
                "type": "method",
                "name": name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            }
            decorators = _collect_decorators(node)
            if decorators:
                item["decorators"] = decorators
            if current_class:
                item["class_name"] = current_class
            items.append(item)
            # Recurse into method body with current_class=None (nested functions are closures)
            for ch in node.children:
                walk(ch, current_class=None, scope_item=item)
            return

        # --- Class declarations ---
        elif node.type == "class_declaration":
            name_node = None
            for c in node.children:
                if c.type == "type_identifier":
                    name_node = c
                    break
            # Fallback to identifier for JS compatibility
            if name_node is None:
                for c in node.children:
                    if c.type == "identifier":
                        name_node = c
                        break
            class_name = _node_text(name_node) if name_node else "unknown>"
            citem = {
                "type": "class",
                "name": class_name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
                "bases": _js_heritage_bases(node),
            }
            decorators = _collect_decorators(node)
            if decorators:
                citem["decorators"] = decorators
            items.append(citem)
            for ch in node.children:
                walk(ch, current_class=class_name, scope_item=scope_item)
            return

        # --- TypeScript-specific: interface declarations ---
        elif node.type == "interface_declaration":
            name_node = None
            for c in node.children:
                if c.type == "type_identifier":
                    name_node = c
                    break
            iface_name = _node_text(name_node) if name_node else "unknown>"
            citem = {
                "type": "class",  # treat interfaces as classes for structural purposes
                "name": iface_name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            }
            decorators = _collect_decorators(node)
            if decorators:
                citem["decorators"] = decorators
            items.append(citem)
            for ch in node.children:
                walk(ch, current_class=None, scope_item=scope_item)
            return

        # --- TypeScript-specific: type alias declarations ---
        elif node.type == "type_alias_declaration":
            name_node = None
            for c in node.children:
                if c.type == "type_identifier":
                    name_node = c
                    break
            type_name = _node_text(name_node) if name_node else "unknown>"
            citem = {
                "type": "class",  # treat type aliases as classes for structural purposes
                "name": type_name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            }
            decorators = _collect_decorators(node)
            if decorators:
                citem["decorators"] = decorators
            items.append(citem)
            for ch in node.children:
                walk(ch, current_class=None, scope_item=scope_item)
            return

        # --- CommonJS require() inside lexical / variable declarations ---
        elif node.type in ("lexical_declaration", "variable_declaration"):
            for c in node.children:
                sub = c
                if sub.type == "variable_declarator":
                    for init in sub.children:
                        if init.type == "call_expression":
                            is_req, module_str = _is_require_call(init)
                            if is_req:
                                items.append({
                                    "type": "import",
                                    "name": module_str or "require(...)",
                                    "file_path": str(file_path),
                                    "line_start": _line_1(node),
                                    "line_end": _line_end_1(node),
                                })

        # --- Also detect bare expression_statement with require() ---
        elif node.type == "expression_statement":
            for c in node.children:
                if c.type == "call_expression":
                    is_req, module_str = _is_require_call(c)
                    if is_req:
                        items.append({
                            "type": "import",
                            "name": module_str or "require(...)",
                            "file_path": str(file_path),
                            "line_start": _line_1(node),
                            "line_end": _line_end_1(node),
                        })

        # General recursive descent for nested structures
        for ch in node.children:
            walk(ch, current_class=current_class, scope_item=scope_item)

    walk(root)
    return items


# ------------------------------------------------------------------
# TSX recursive extraction (uses TSX parser for JSX support)
# ------------------------------------------------------------------

def _extract_tsx(file_path: Path):
    parser = _tsx_parser()
    source = file_path.read_bytes()
    tree = parser.parse(source)
    root = tree.root_node
    items = []

    def walk(node, current_class: str | None = None, scope_item: dict | None = None):
        # Capture calls into the innermost enclosing definition so call edges
        # can be derived later without a second AST pass.
        if node.type == "call_expression" and scope_item is not None:
            callee = _js_callee_name(node)
            if callee:
                if callee not in scope_item.setdefault("calls", []):
                    scope_item["calls"].append(callee)
                ln = _line_1(node)
                lines = scope_item.setdefault("call_sites", {}).setdefault(callee, [])
                if ln not in lines:
                    lines.append(ln)

        # --- ES import statements ---
        if node.type == "import_statement":
            text_line = _node_text(node).splitlines()[0] if _node_text(node) else "import"
            items.append({
                "type": "import",
                "name": text_line,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            })

        # --- Function declarations ---
        elif node.type == "function_declaration":
            name_node = None
            for c in node.children:
                if c.type == "identifier":
                    name_node = c
                    break
            name = _node_text(name_node) if name_node else "unknown>"
            item = {
                "type": "method" if current_class else "function",
                "name": name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            }
            decorators = _collect_decorators(node)
            if decorators:
                item["decorators"] = decorators
            if current_class:
                item["class_name"] = current_class
            items.append(item)
            for ch in node.children:
                walk(ch, current_class=None, scope_item=item)
            return

        # --- Arrow function components (TSX) ---
        elif node.type == "variable_declarator":
            for c in node.children:
                if c.type == "arrow_function":
                    name_node = None
                    for ch in node.children:
                        if ch.type == "identifier":
                            name_node = ch
                            break
                    name = _node_text(name_node) if name_node else "unknown>"
                    item = {
                        "type": "function",
                        "name": name,
                        "file_path": str(file_path),
                        "line_start": _line_1(node),
                        "line_end": _line_end_1(node),
                    }
                    decorators = _collect_decorators(node)
                    if decorators:
                        item["decorators"] = decorators
                    items.append(item)
                    for ch in c.children:
                        walk(ch, current_class=None, scope_item=item)
                    return

        # --- Method definitions inside class ---
        elif node.type == "method_definition":
            name_node = None
            for c in node.children:
                if c.type == "property_identifier" and name_node is None:
                    name_node = c
            if name_node is None:
                for c in node.children:
                    if c.type == "identifier" and name_node is None:
                        name_node = c
            name = _node_text(name_node) if name_node else "unknown>"
            item = {
                "type": "method",
                "name": name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            }
            decorators = _collect_decorators(node)
            if decorators:
                item["decorators"] = decorators
            if current_class:
                item["class_name"] = current_class
            items.append(item)
            for ch in node.children:
                walk(ch, current_class=None, scope_item=item)
            return

        # --- Class declarations ---
        elif node.type == "class_declaration":
            name_node = None
            for c in node.children:
                if c.type == "type_identifier":
                    name_node = c
                    break
            if name_node is None:
                for c in node.children:
                    if c.type == "identifier":
                        name_node = c
                        break
            class_name = _node_text(name_node) if name_node else "unknown>"
            citem = {
                "type": "class",
                "name": class_name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
                "bases": _js_heritage_bases(node),
            }
            decorators = _collect_decorators(node)
            if decorators:
                citem["decorators"] = decorators
            items.append(citem)
            for ch in node.children:
                walk(ch, current_class=class_name, scope_item=scope_item)
            return

        # --- TypeScript-specific: interface declarations ---
        elif node.type == "interface_declaration":
            name_node = None
            for c in node.children:
                if c.type == "type_identifier":
                    name_node = c
                    break
            iface_name = _node_text(name_node) if name_node else "unknown>"
            items.append({
                "type": "class",
                "name": iface_name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            })
            for ch in node.children:
                walk(ch, current_class=None, scope_item=scope_item)
            return

        # --- TypeScript-specific: type alias declarations ---
        elif node.type == "type_alias_declaration":
            name_node = None
            for c in node.children:
                if c.type == "type_identifier":
                    name_node = c
                    break
            type_name = _node_text(name_node) if name_node else "unknown>"
            items.append({
                "type": "class",
                "name": type_name,
                "file_path": str(file_path),
                "line_start": _line_1(node),
                "line_end": _line_end_1(node),
            })
            for ch in node.children:
                walk(ch, current_class=None, scope_item=scope_item)
            return

        # --- CommonJS require() inside lexical / variable declarations ---
        elif node.type in ("lexical_declaration", "variable_declaration"):
            for c in node.children:
                sub = c
                if sub.type == "variable_declarator":
                    for init in sub.children:
                        if init.type == "call_expression":
                            is_req, module_str = _is_require_call(init)
                            if is_req:
                                items.append({
                                    "type": "import",
                                    "name": module_str or "require(...)",
                                    "file_path": str(file_path),
                                    "line_start": _line_1(node),
                                    "line_end": _line_end_1(node),
                                })

        # --- Also detect bare expression_statement with require() ---
        elif node.type == "expression_statement":
            for c in node.children:
                if c.type == "call_expression":
                    is_req, module_str = _is_require_call(c)
                    if is_req:
                        items.append({
                            "type": "import",
                            "name": module_str or "require(...)",
                            "file_path": str(file_path),
                            "line_start": _line_1(node),
                            "line_end": _line_end_1(node),
                        })

        # General recursive descent for nested structures
        for ch in node.children:
            walk(ch, current_class=current_class, scope_item=scope_item)

    walk(root)
    return items


# ------------------------------------------------------------------
# Public interface
# ------------------------------------------------------------------

def extract_structures(file_path: Path) -> list[dict]:
    """Extract structural nodes (function/class/method/import) from a file.

    Returns a list of dicts, each with at minimum:
        - type: 'function' | 'method' | 'class' | 'import'
        - name: str (for import: module line / module name)
        - file_path: str
        - line_start: int (1-indexed)
        - line_end: int (1-indexed)
        - class_name: str (only for methods, the parent class name)
        - bases: list[str] (only for classes, ordered base-class name tails;
          dotted bases collapse to the last segment; [] when no heritage)
        - calls: list[str] (callee texts seen inside the definition body,
          e.g. 'query_hybrid', 'self.write', or 'MemoryStore.write' for a
          `with`-block instance alias rewritten to its inferred class;
          definitions only)
        - call_sites: dict[str, list[int]] (1-indexed call-site lines per
          callee name in calls, same rewritten names; a repeated callee
          lists every distinct line, e.g. {'helper': [12, 27]}; callers
          built before this field existed simply omit it)

    For unsupported extensions returns [] and logs
    an explicit skip message so unsupported and "found nothing" are
    distinguishable.
    """
    file_path = Path(file_path)
    suffix = file_path.suffix.lower()

    if suffix == ".py":
        try:
            return _extract_python(file_path)
        except ImportError as e:
            logger.warning("structural extraction skipped for %s: %s", file_path, e)
            return []

    if suffix in (".js", ".jsx"):
        try:
            return _extract_js(file_path)
        except ImportError as e:
            logger.warning("structural extraction skipped for %s: %s", file_path, e)
            return []

    if suffix == ".ts":
        try:
            return _extract_ts(file_path)
        except ImportError as e:
            logger.warning("structural extraction skipped for %s: %s", file_path, e)
            return []

    if suffix == ".tsx":
        try:
            return _extract_tsx(file_path)
        except ImportError as e:
            logger.warning("structural extraction skipped for %s: %s", file_path, e)
            return []

    # Explicit unsupported skip — raise NotImplementedError so callers can distinguish
    # from "no structures found" (which returns empty list).
    raise NotImplementedError(f"unsupported extension for structural extraction: {suffix!r} (file: {file_path})")
