# Mirror of memory_store.py pattern: top-level and nested imports inside try/except
import os
import sys

try:
    import numpy as np
    from fastembed import TextEmbedding
except ImportError:
    pass

def helper():
    try:
        import sqlite_vec
    except Exception:
        pass
    return 42
