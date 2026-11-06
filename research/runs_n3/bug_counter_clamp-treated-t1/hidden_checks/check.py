import sys
from pathlib import Path
root = Path.cwd()
sys.path.insert(0, str(root))
from counter import Counter
from limiter import Limiter
c = Counter()
assert c.increment(50) == 50, c.increment(50)
assert c.increment(80) == 100, c.increment(80)
assert c.value == 100
c.reset()
assert c.increment(10) == 10
src = (root / 'limiter.py').read_text(encoding='utf-8')
assert '100' in src
print('CHECK PASS')
