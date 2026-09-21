"""Float — a working companion for schoolteachers.

A small, self-contained assistant: one SQLite file, one stdlib HTTP server,
no build step. Runs on the teacher's own laptop, on their own API key.
"""

from .config import APP_NAME, APP_TAGLINE, VERSION

__all__ = ["APP_NAME", "APP_TAGLINE", "VERSION"]
