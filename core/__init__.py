"""Core services for Black Sakura Translate."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


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


def hidden_subprocess_kwargs(detached_group: bool = False) -> Dict[str, Any]:
    """Return Windows subprocess kwargs that suppress any console/terminal window flash.

    Never combines DETACHED_PROCESS with CREATE_NO_WINDOW (conflicting Win32 flags).
    """
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
    si.wShowWindow = 0  # SW_HIDE
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if detached_group:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    return {"creationflags": flags, "startupinfo": si}
