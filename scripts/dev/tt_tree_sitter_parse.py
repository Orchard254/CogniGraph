from tree_sitter import Language, Parser
import tree_sitter_python

PY_LANGUAGE = Language(tree_sitter_python.language())
parser = Parser(PY_LANGUAGE)

with open('memory_store.py','rb') as f:
    src = f.read()

tree = parser.parse(src)
root = tree.root_node

print('root type:', root.type)

# collect imports
imports = []
for node in root.walk():
    pass

# Use simple traversal: iterate children recursively
def walk(node):
    res = []
    if node.type in ('import_statement', 'import_from_statement'):
        res.append(('import', node.start_point, node.end_point, node.type, node.text.decode('utf-8')))
    if node.type == 'function_definition':
        name = None
        for c in node.children:
            if c.type == 'identifier':
                name = c.text.decode('utf-8')
                break
        res.append(('function', name, node.start_point, node.end_point))
    if node.type == 'class_definition':
        name = None
        for c in node.children:
            if c.type == 'identifier':
                name = c.text.decode('utf-8')
                break
        res.append(('class', name, node.start_point, node.end_point))
    for ch in node.children:
        res.extend(walk(ch))
    return res

items = walk(root)
print('found items:', len(items))
# print first 40
for it in items[:40]:
    print(it)
