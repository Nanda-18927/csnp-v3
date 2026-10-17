"""WSGI entry point for real servers (PythonAnywhere, gunicorn, waitress).

python backend/app.py is for local development -- it calls app.run().
A WSGI server instead imports this module and looks for `application`,
so the database is built here at import time by backend/app.py.

PythonAnywhere: point the "Source code" directory at the project root and
leave this file as the WSGI configuration file's import target.
"""

import sys
from pathlib import Path

# Make the repository root importable no matter where the server starts.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import app  # noqa: E402,F401  (importing builds the schema)

application = app