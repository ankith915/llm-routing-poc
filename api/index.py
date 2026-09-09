"""Vercel entrypoint.

Vercel's Python runtime serves the module-level ASGI `app`. Everything else
lives in backend/, which vercel.json ships alongside this file.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.api.main import app  # noqa: E402

__all__ = ["app"]
