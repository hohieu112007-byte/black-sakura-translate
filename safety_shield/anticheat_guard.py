"""Anti-Cheat Ban Protection (Layer 1): 3-level fast directory scan."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any, Dict, List, Set

# Exact & wildcard signatures per anti-cheat vendor
ANTICHEAT_RULES: List[Dict[str, Any]] = [
    {
        "vendor": "Easy Anti-Cheat (EAC)",
        "severity": "BLOCKED",
        "dir_names": {"easyanticheat", "easyanticheat_eos"},
        "exact_files": {
            "start_protected_game.exe",
            "easyanticheat_eos_setup.exe",
            "easyanticheat_setup.exe",
            "easyanticheat_x64.dll",
            "easyanticheat_x86.dll",
        },
        "globs": ["*.eac"],
    },
    {
        "vendor": "BattlEye (BE)",
        "severity": "BLOCKED",
        "dir_names": {"battleye"},
        "exact_files": {
            "beservice.exe",
            "beservice_x64.exe",
            "beclient_x64.dll",
            "beclient.dll",
        },
        "globs": ["*_be.exe"],
    },
    {
        "vendor": "EA Anticheat (Javelin)",
        "severity": "BLOCKED",
        "dir_names": {"eaanticheat"},
        "exact_files": {
            "eaanticheat.gameservicelauncher.exe",
            "eaanticheat.gameservicelauncher.dll",
            "eaanticheat.installer.exe",
        },
        "globs": [],
    },
    {
        "vendor": "Tencent Anti-Cheat Expert (ACE)",
        "severity": "BLOCKED",
        "dir_names": {"anticheatexpert"},
        "exact_files": {
            "ace-base.dll",
            "ace-base64.dll",
            "ace-ats64.dll",
            "sguard64.exe",
            "sguardsvc64.exe",
        },
        "globs": [],
    },
    {
        "vendor": "Riot Vanguard",
        "severity": "BLOCKED",
        "dir_names": {"riot vanguard"},
        "exact_files": {"vgk.sys", "vgc.exe"},
        "globs": [],
    },
    {
        "vendor": "nProtect GameGuard",
        "severity": "BLOCKED",
        "dir_names": {"gameguard"},
        "exact_files": {"gameguard.des", "gamemon.des", "gamemon64.des"},
        "globs": [],
    },
    {
        "vendor": "XIGNCODE3",
        "severity": "BLOCKED",
        "dir_names": {"xigncode"},
        "exact_files": {"x3.xem", "xxd-0.xem", "xhunter1.sys"},
        "globs": [],
    },
    {
        "vendor": "HoYoKProtect / mhyprot",
        "severity": "BLOCKED",
        "dir_names": set(),
        "exact_files": {"mhyprot2.sys", "mhyprot3.sys", "hoyokprotect.sys"},
        "globs": [],
    },
]

# Skip heavy content folders & media extensions while walking 3 levels
SKIP_MEDIA_EXTS = {
    ".mp4", ".bik", ".bk2", ".webm", ".mkv", ".avi", ".mov",
    ".wav", ".ogg", ".mp3", ".wem", ".bnk", ".pck",
    ".png", ".jpg", ".jpeg", ".tga", ".dds", ".uasset", ".umap",
}


def scan_anticheat(game_dir: str | Path, max_depth: int = 3) -> Dict[str, Any]:
    """Scan game_dir up to max_depth levels for Anti-Cheat signatures.

    Returns:
        {"risk_level": "BLOCKED" | "CAUTION" | "SAFE", "detected_ac": [...], "matched_files": [...], "message": str}
    """
    root = Path(game_dir)
    if not root.exists() or not root.is_dir():
        return {
            "risk_level": "SAFE",
            "detected_ac": [],
            "matched_files": [],
            "message": "Thư mục game không tồn tại để quét Anti-Cheat.",
        }

    detected_vendors: List[str] = []
    seen_vendors: Set[str] = set()
    matched_files: List[str] = []
    highest_severity = "SAFE"

    root_depth = len(root.parts)

    for dirpath, dirnames, filenames in os.walk(root):
        cur_path = Path(dirpath)
        depth = len(cur_path.parts) - root_depth

        # Check matching directory names at this level
        for dname in list(dirnames):
            d_lower = dname.lower()
            full_d = cur_path / dname
            for rule in ANTICHEAT_RULES:
                if d_lower in rule["dir_names"]:
                    if rule["vendor"] not in seen_vendors:
                        seen_vendors.add(rule["vendor"])
                        detected_vendors.append(rule["vendor"])
                    matched_files.append(str(full_d))
                    if rule["severity"] == "BLOCKED":
                        highest_severity = "BLOCKED"
                    elif highest_severity == "SAFE":
                        highest_severity = "CAUTION"

        # Prune walk beyond max_depth
        if depth >= max_depth:
            dirnames[:] = []

        # Check matching files
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in SKIP_MEDIA_EXTS:
                continue
            f_lower = fname.lower()
            full_f = cur_path / fname
            for rule in ANTICHEAT_RULES:
                hit = f_lower in rule["exact_files"]
                if not hit and rule["globs"]:
                    hit = any(fnmatch.fnmatch(f_lower, g) for g in rule["globs"])
                if hit:
                    if rule["vendor"] not in seen_vendors:
                        seen_vendors.add(rule["vendor"])
                        detected_vendors.append(rule["vendor"])
                    matched_files.append(str(full_f))
                    if rule["severity"] == "BLOCKED":
                        highest_severity = "BLOCKED"
                    elif highest_severity == "SAFE":
                        highest_severity = "CAUTION"

    if highest_severity == "BLOCKED":
        msg = (
            f"Phát hiện Anti-Cheat ({', '.join(detected_vendors)}). "
            "Khóa tính năng sửa file để bảo vệ tài khoản khỏi bị Ban (chỉ mở khóa khi bật Offline Override)."
        )
    elif highest_severity == "CAUTION":
        msg = f"Cảnh báo thành phần bảo vệ ({', '.join(detected_vendors)}). Hãy sao lưu kỹ trước khi thử."
    else:
        msg = "Sạch Anti-Cheat • An toàn tuyệt đối để dịch file ngôn ngữ gốc."

    return {
        "risk_level": highest_severity,
        "detected_ac": detected_vendors,
        "matched_files": matched_files[:50],
        "message": msg,
    }
