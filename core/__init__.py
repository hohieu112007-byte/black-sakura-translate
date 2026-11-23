"""Core services for Black Sakura Translate."""

from __future__ import annotations

import sys
from pathlib import Path


def get_app_dir(anchor_file: str) -> Path:
    """Return persistent writable app directory (beside executable when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(anchor_file).resolve().parent.parent


def get_bundle_dir(anchor_file: str) -> Path:
    """Return read-only bundled assets directory (_MEIPASS when frozen)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return get_app_dir(anchor_file)
