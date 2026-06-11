import os
import sys

# Make `backend` importable when pytest runs from the repo root or the worktree.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
