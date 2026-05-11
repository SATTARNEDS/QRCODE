"""PythonAnywhere WSGI entry point.

Set ``QRCODE_PROJECT_DIR`` in PythonAnywhere if your repo is not in ``~/QRCODE``.
"""

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("QRCODE_PROJECT_DIR", Path.home() / "QRCODE"))

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app import app as application
