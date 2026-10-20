"""PythonAnywhere WSGI entry point.

Set ``QRCODE_PROJECT_DIR`` in PythonAnywhere if your repo is not in ``~/QRCODE``.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

PROJECT_DIR = Path(os.environ.get("QRCODE_PROJECT_DIR", Path.home() / "QRCODE"))

# Keep secrets in an untracked .env file on PythonAnywhere, not in this WSGI
# file or the Git repository.  This must run before importing the Flask app.
load_dotenv(PROJECT_DIR / ".env")

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app import app as application
