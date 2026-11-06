import sys
from pathlib import Path
root = Path.cwd()
sys.path.insert(0, str(root))
from invoice import total
from pricing import apply_tax
items = [10, 20, 30]
expected = apply_tax(sum(items))
assert total(items) == expected, (total(items), expected)
assert apply_tax(100) == 120.0
src = (root / 'invoice.py').read_text(encoding='utf-8')
assert 'pricing' in src and 'apply_tax' in src
print('CHECK PASS')
