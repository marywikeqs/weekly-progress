"""Vercel entry point: the same server, one request per call (see server.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import Handler  # noqa: E402


class handler(Handler):
    pass
