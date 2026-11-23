"""Atomic File I/O & Crash-Safe Temp Cleanup Utilities."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def atomic_write_bytes(target_path: Path | str, data: bytes) -> None:
    """Write bytes atomically via same-directory .sakuga_tmp + fsync + os.replace."""
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{target.name}.sakuga_tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(target_path: Path | str, text: str, encoding: str = "utf-8") -> None:
    """Write UTF-8 text atomically via same-directory .sakuga_tmp + fsync + os.replace."""
    atomic_write_bytes(target_path, text.encode(encoding))


def cleanup_orphaned_temps(roots: Iterable[Path | str]) -> int:
    """Remove leftover *.sakuga_tmp files left behind if the app was force-killed mid-write."""
    removed = 0
    for r in roots:
        if not r:
            continue
        p = Path(r)
        if not p.exists() or not p.is_dir():
            continue
        try:
            for tmp in p.rglob("*.sakuga_tmp"):
                try:
                    tmp.unlink()
                    removed += 1
                except OSError:
                    pass
        except OSError:
            pass
    return removed
