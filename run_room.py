"""Hidden room launcher with local logs; does not change either assistant's login."""
import os
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parent
os.chdir(root)
sys.path.insert(0, str(root))
sys.stdout = open(root / 'server-runtime.log', 'a', encoding='utf-8', buffering=1)
sys.stderr = sys.stdout
runpy.run_path(str(root / 'server.py'), run_name='__main__')
