"""Loads the dashboard HTML asset from the static directory (cached)."""

from __future__ import annotations

import os

from . import config

_DASHBOARD_PATH = os.path.join(config.STATIC_DIR, "dashboard.html")
_CACHE: str | None = None


def dashboard_html() -> str:
    """Return the dashboard page, reading from disk once and caching it."""
    global _CACHE
    if _CACHE is None:
        with open(_DASHBOARD_PATH, "r", encoding="utf-8") as handle:
            _CACHE = handle.read()
    return _CACHE
