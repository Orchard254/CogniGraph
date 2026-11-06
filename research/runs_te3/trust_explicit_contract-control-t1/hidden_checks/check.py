import sys
from pathlib import Path
root = Path.cwd()
sys.path.insert(0, str(root))
from greeter import greet
assert greet('ada') == 'Hello, ada!', greet('ada')
print('CHECK PASS')
