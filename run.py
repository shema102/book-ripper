"""Launch from source, preferring this folder's local virtual environment."""
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
environment = root / '.venv'
python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
if python.exists() and Path(sys.prefix).resolve() != environment.resolve():
    os.execv(str(python), [str(python), str(root/'run.py'), *sys.argv[1:]])

from opticalbook.__main__ import main
main()
