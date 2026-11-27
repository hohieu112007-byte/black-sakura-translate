"""Unreal Engine .locres & .pak localization extractor/writer (repak + UnrealLocres + pure-Python fallback)."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from extractors.subtitle_filter import LocEntry, classify_entry_category, is_translatable_string

from core import get_app_dir

logger = logging.getLogger("sakuga.unreal_locres")

APP_DIR = get_app_dir(__file__)
DEFAULT_REPAK = APP_DIR / "tools" / "repak" / "repak.exe"
DEFAULT_UNREAL_LOCRES = APP_DIR / "tools" / "unreallocres" / "UnrealLocres.exe"
DEFAULT_AES_DUMPSTER = APP_DIR / "tools" / "AESDumpster.exe"
DEFAULT_RETOC = APP_DIR / "tools" / "retoc" / "retoc.exe"

_HEX64_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
_AES_ERR_TOKENS = ("aes", "encrypt", "decryption key", "missing key", "invalid key", "key required", "cipher")

LOCRES_MAGIC = bytes.fromhex("0e147475674a03fc4a15909d7f124ac6")  # Little-endian GUID 7574140E-FC03-4A67-9D90-154A127F08C6? Standard UE Locres magic
# Canonical UE Locres magic bytes in file order:
UE_LOCRES_MAGIC = bytes([0x0E, 0x14, 0x74, 0x75, 0x67, 0x4A, 0x03, 0xFC, 0x4A, 0x15, 0x90, 0x9D, 0xC6, 0x08, 0x7F, 0x12])
# Note: Unreal versions serialize FGuid(0x7574140E, 0xFC034A67, 0x9D90154A, 0x127F08C6) little-endian:
ALT_LOCRES_MAGIC = bytes.fromhex("0e147475674a03fc4a15909d127f08c6")
VALID_MAGICS = {
    bytes.fromhex("0e147475674a03fc4a15909d127f08c6"),
    bytes.fromhex("0e147475674a03fc4a15909dc6087f12"),
    bytes.fromhex("7574140efc034a679d90154a127f08c6"),
}


class _BinReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read_u8(self) -> int:
        v = self.data[self.pos]
        self.pos += 1
        return v

    def read_i32(self) -> int:
        v = int.from_bytes(self.data[self.pos : self.pos + 4], "little", signed=True)
        self.pos += 4
        return v

    def read_u32(self) -> int:
        v = int.from_bytes(self.data[self.pos : self.pos + 4], "little", signed=False)
        self.pos += 4
        return v

    def read_i64(self) -> int:
        v = int.from_bytes(self.data[self.pos : self.pos + 8], "little", signed=True)
        self.pos += 8
        return v

    def read_bytes(self, n: int) -> bytes:
        b = self.data[self.pos : self.pos + n]
        self.pos += n
        return b

    def read_fstring(self) -> str:
        length = self.read_i32()
        if length == 0:
            return ""
        if length < 0:
            # UTF-16LE
            char_count = -length
            raw = self.read_bytes(char_count * 2)
            return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        raw = self.read_bytes(length)
        return raw.decode("utf-8", errors="replace").rstrip("\x00")


def _encode_fstring(text: str, force_utf16: bool = False) -> bytes:
    if not text:
        return (0).to_bytes(4, "little", signed=True)
    is_ascii = all(ord(c) < 128 for c in text)
    if force_utf16 or not is_ascii:
        payload = (text + "\x00").encode("utf-16-le")
        char_len = -(len(payload) // 2)
        return char_len.to_bytes(4, "little", signed=True) + payload
    payload = (text + "\x00").encode("utf-8")
    return len(payload).to_bytes(4, "little", signed=True) + payload


def parse_locres_bytes_python(raw: bytes) -> Dict[str, Any]:
    """Pure-Python reader for UE4/UE5 .locres binary files (v0-v3 fallback)."""
    r = _BinReader(raw)
    magic = r.read_bytes(16)
    version = 0
    localized_strings: List[Tuple[str, int]] = []  # (text, ref_count)
    namespaces: List[Dict[str, Any]] = []

    if magic in VALID_MAGICS or (len(raw) > 25 and raw[16] in (1, 2, 3)):
        version = r.read_u8()
        str_offset = r.read_i64()

        # Parse localized string array first if version >= 1
        saved_pos = r.pos
        r.pos = str_offset
        str_count = r.read_i32()
        for _ in range(max(0, str_count)):
            s = r.read_fstring()
            ref_count = r.read_i32() if version >= 2 else 1
            localized_strings.append((s, ref_count))

        # Parse namespace/key index table
        r.pos = saved_pos
        if version >= 2:
            _entries_count = r.read_i32()
        ns_count = r.read_i32()
        for _ in range(max(0, ns_count)):
            ns_hash = r.read_u32() if version >= 2 else 0
            ns_name = r.read_fstring()
            key_count = r.read_i32()
            keys = []
            for _ in range(max(0, key_count)):
                key_hash = r.read_u32() if version >= 2 else 0
                key_str = r.read_fstring()
                src_hash = r.read_u32()
                str_idx = r.read_i32()
                val = localized_strings[str_idx][0] if 0 <= str_idx < len(localized_strings) else ""
                keys.append({
                    "key_hash": key_hash,
                    "key": key_str,
                    "source_hash": src_hash,
                    "string_index": str_idx,
                    "text": val,
                })
            namespaces.append({"ns_hash": ns_hash, "namespace": ns_name, "keys": keys})
        return {
            "magic": magic,
            "version": version,
            "has_magic": True,
            "localized_strings": localized_strings,
            "namespaces": namespaces,
        }

    # Version 0 Legacy (no magic header)
    r.pos = 0
    ns_count = r.read_i32()
    for _ in range(max(0, ns_count)):
        ns_name = r.read_fstring()
        key_count = r.read_i32()
        keys = []
        for _ in range(max(0, key_count)):
            key_str = r.read_fstring()
            src_hash = r.read_u32()
            val = r.read_fstring()
            idx = len(localized_strings)
            localized_strings.append((val, 1))
            keys.append({
                "key_hash": 0,
                "key": key_str,
                "source_hash": src_hash,
                "string_index": idx,
                "text": val,
            })
        namespaces.append({"ns_hash": 0, "namespace": ns_name, "keys": keys})
    return {
        "magic": b"",
        "version": 0,
        "has_magic": False,
        "localized_strings": localized_strings,
        "namespaces": namespaces,
    }


def write_locres_bytes_python(parsed: Dict[str, Any], updates_by_ns_key: Dict[Tuple[str, str], str]) -> bytes:
    """Serialize patched .locres binary cleanly preserving hashes, keys, and UTF-16LE support."""
    version = parsed["version"]
    namespaces = parsed["namespaces"]

    if not parsed["has_magic"] or version == 0:
        buf = bytearray()
        buf += len(namespaces).to_bytes(4, "little", signed=True)
        for ns in namespaces:
            buf += _encode_fstring(ns["namespace"])
            keys = ns["keys"]
            buf += len(keys).to_bytes(4, "little", signed=True)
            for k in keys:
                new_val = updates_by_ns_key.get((ns["namespace"], k["key"]), k["text"])
                buf += _encode_fstring(k["key"])
                buf += int(k["source_hash"]).to_bytes(4, "little", signed=False)
                buf += _encode_fstring(new_val)
        return bytes(buf)

    # Rebuild string table preserving unique strings or exact per-entry mapping safely
    new_strings: List[Tuple[str, int]] = []
    # Keep track per string_index so untouched strings preserve exact structure, while updated keys get updated strings
    orig_strings = parsed["localized_strings"]
    # Build mapping from (ns, key) -> new index
    total_entries = 0
    rebuilt_ns: List[Tuple[int, str, List[Tuple[int, str, int, int]]]] = []

    for ns in namespaces:
        ns_name = ns["namespace"]
        ns_keys_out = []
        for k in ns["keys"]:
            total_entries += 1
            old_idx = k["string_index"]
            orig_val = k["text"]
            new_val = updates_by_ns_key.get((ns_name, k["key"]), orig_val)
            # Allocate string slot
            new_idx = len(new_strings)
            ref_c = orig_strings[old_idx][1] if 0 <= old_idx < len(orig_strings) else 1
            new_strings.append((new_val, max(1, ref_c)))
            ns_keys_out.append((k["key_hash"], k["key"], k["source_hash"], new_idx))
        rebuilt_ns.append((ns["ns_hash"], ns_name, ns_keys_out))

    # Build directory section first to compute exact string_table_offset
    dir_buf = bytearray()
    if version >= 2:
        dir_buf += total_entries.to_bytes(4, "little", signed=True)
    dir_buf += len(rebuilt_ns).to_bytes(4, "little", signed=True)
    for ns_hash, ns_name, ns_keys in rebuilt_ns:
        if version >= 2:
            dir_buf += int(ns_hash).to_bytes(4, "little", signed=False)
        dir_buf += _encode_fstring(ns_name)
        dir_buf += len(ns_keys).to_bytes(4, "little", signed=True)
        for k_hash, k_str, s_hash, s_idx in ns_keys:
            if version >= 2:
                dir_buf += int(k_hash).to_bytes(4, "little", signed=False)
            dir_buf += _encode_fstring(k_str)
            dir_buf += int(s_hash).to_bytes(4, "little", signed=False)
            dir_buf += int(s_idx).to_bytes(4, "little", signed=True)

    header_size = 16 + 1 + 8
    str_offset = header_size + len(dir_buf)

    out = bytearray()
    out += parsed["magic"]
    out.append(version)
    out += str_offset.to_bytes(8, "little", signed=True)
    out += dir_buf

    out += len(new_strings).to_bytes(4, "little", signed=True)
    for s_val, ref_c in new_strings:
        out += _encode_fstring(s_val)
        if version >= 2:
            out += int(ref_c).to_bytes(4, "little", signed=True)
    return bytes(out)


def _normalize_ue_lang(raw: str) -> str:
    low = (raw or "").strip().lower().replace("_", "-")
    if low.startswith("en"):
        return "en"
    if low.startswith("pt-br") or low == "ptbr":
        return "pt-BR"
    if low.startswith("zh-tw") or low.startswith("zh-hant"):
        return "zh-TW"
    if low.startswith("zh"):
        return "zh"
    return low.split("-")[0] if "-" in low else low


def _run_tool(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 90) -> subprocess.CompletedProcess:
    from core import hidden_subprocess_kwargs
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **hidden_subprocess_kwargs(),
    )


def _resolve_aes_dumpster_path() -> Optional[Path]:
    for cand in (
        DEFAULT_AES_DUMPSTER,
        APP_DIR / "tools" / "AESDumpster-Win64.exe",
        APP_DIR / "tools" / "aesdumpster" / "AESDumpster.exe",
        APP_DIR / "tools" / "aesdumpster" / "AESDumpster-Win64.exe",
    ):
        if cand.exists() and cand.is_file():
            return cand
    return None


def _resolve_retoc_path() -> Optional[Path]:
    for cand in (DEFAULT_RETOC, APP_DIR / "tools" / "retoc.exe"):
        if cand.exists() and cand.is_file():
            return cand
    return None


def normalize_aes_key(raw_key: str) -> str:
    """Validate and normalize a 256-bit Unreal AES key (0x+hex, 64 hex, or Base64) -> '0x' + 64 uppercase hex."""
    s = (raw_key or "").strip()
    if not s:
        raise ValueError("AES Key không được để trống.")

    candidate_hex = s[2:].strip() if s.lower().startswith("0x") else s
    if _HEX64_RE.match(candidate_hex):
        return f"0x{candidate_hex.upper()}"

    # Try Base64 (typically 44 chars ending with '=' representing 32 bytes)
    if re.fullmatch(r"[A-Za-z0-9+/=]{40,48}", s) and not s.lower().startswith("0x"):
        try:
            padded = s + ("=" * ((-len(s)) % 4))
            decoded = base64.b64decode(padded, validate=True)
            if len(decoded) == 32:
                return f"0x{decoded.hex().upper()}"
        except (binascii.Error, ValueError):
            pass

    raise ValueError("AES Key không hợp lệ: phải đúng 256-bit (64 ký tự Hex có/không '0x', hoặc 44 ký tự Base64).")


def _safe_norm_aes_key(raw_key: Optional[str]) -> str:
    if not raw_key or not str(raw_key).strip():
        return ""
    try:
        return normalize_aes_key(str(raw_key))
    except ValueError:
        return ""


def _repak_aes_hex(aes_key: Optional[str]) -> str:
    norm = _safe_norm_aes_key(aes_key)
    return norm[2:] if norm else ""


def _repak_cmd(args: List[str], aes_key: Optional[str] = None) -> List[str]:
    clean_hex = _repak_aes_hex(aes_key)
    if clean_hex:
        return [str(DEFAULT_REPAK), "-a", clean_hex, *args]
    return [str(DEFAULT_REPAK), *args]


def _retoc_cmd(args: List[str], aes_key: Optional[str] = None) -> List[str]:
    retoc_bin = _resolve_retoc_path()
    norm = _safe_norm_aes_key(aes_key)
    base = [str(retoc_bin)] if retoc_bin else [str(DEFAULT_RETOC)]
    if norm:
        return [*base, "--aes-key", norm, *args]
    return [*base, *args]


def _is_aes_error_output(stdout: str, stderr: str) -> bool:
    combined = ((stderr or "") + "\n" + (stdout or "")).lower()
    return any(tok in combined for tok in _AES_ERR_TOKENS)


def inspect_unreal_paks(game_dir: str | Path) -> Dict[str, Any]:
    """Detect Unreal Paks layout, UE5 IoStore (.utoc/.ucas), .sig signature guard, and Win64-Shipping.exe."""
    root = Path(game_dir)
    paks_dirs = sorted(
        {
            p
            for p in ([root / "Content" / "Paks"] + list(root.glob("*/Content/Paks")) + list(root.rglob("Paks")))
            if p.is_dir()
        }
    )
    search_bases = paks_dirs if paks_dirs else [root]

    all_paks: List[Path] = []
    orig_paks: List[Path] = []
    utoc_files: List[Path] = []
    ucas_files: List[Path] = []
    sig_files: List[Path] = []

    for base in search_bases:
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            if any(part.lower() == "~mods" for part in p.parts):
                continue
            ext = p.suffix.lower()
            name_low = p.name.lower()
            if ext == ".pak":
                all_paks.append(p)
                if not name_low.endswith("_p.pak"):
                    orig_paks.append(p)
            elif ext == ".utoc":
                utoc_files.append(p)
            elif ext == ".ucas":
                ucas_files.append(p)
            elif ext == ".sig" and not name_low.endswith("_p.sig"):
                # Count original pak signature files (ignore mod _P.sig if any)
                if p.with_suffix(".pak").exists() or p.with_suffix(".utoc").exists() or any(
                    sp.stem.lower() == p.stem.lower() for sp in all_paks
                ):
                    sig_files.append(p)

    # Also catch .sig discovered before its sibling .pak during alphabetical rglob traversal
    if not sig_files:
        for base in search_bases:
            for p in sorted(base.rglob("*.sig")):
                if p.is_file() and "~mods" not in {pt.lower() for pt in p.parts} and not p.name.lower().endswith("_p.sig"):
                    if p.with_suffix(".pak").exists() or p.with_suffix(".utoc").exists():
                        sig_files.append(p)

    # Locate *-Win64-Shipping.exe for optional static AES scan
    shipping_candidates: List[Path] = []
    fallback_exes: List[Path] = []
    for pattern in ("Binaries/Win64/*-Win64-Shipping.exe", "*/Binaries/Win64/*-Win64-Shipping.exe"):
        shipping_candidates.extend(p for p in root.glob(pattern) if p.is_file())
    if not shipping_candidates:
        for pattern in ("Binaries/Win64/*.exe", "*/Binaries/Win64/*.exe"):
            for exe in root.glob(pattern):
                if exe.is_file() and "crash" not in exe.name.lower() and "prereq" not in exe.name.lower():
                    fallback_exes.append(exe)
    best_pool = shipping_candidates or fallback_exes
    best_pool.sort(key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
    shipping_exe_path = str(best_pool[0]) if best_pool else ""

    return {
        "paks_dir": str(paks_dirs[0]) if paks_dirs else str(root / "Content" / "Paks"),
        "pak_files": orig_paks if orig_paks else all_paks,
        "all_pak_files": all_paks,
        "utoc_files": utoc_files,
        "ucas_files": ucas_files,
        "sig_files": sig_files,
        "is_iostore": bool(utoc_files and ucas_files),
        "has_pak_signature": bool(sig_files),
        "shipping_exe_path": shipping_exe_path,
    }


def _read_pak_file_bytes(pak_path: Path, internal_rel: str, aes_key: Optional[str] = None) -> Optional[bytes]:
    """Read a single file from an Unreal .pak via `repak get` with `repak unpack -f -i` fallback."""
    if not DEFAULT_REPAK.exists() or not pak_path.exists():
        return None
    from core import hidden_subprocess_kwargs

    rel = internal_rel.strip().strip('"').replace("\\", "/").lstrip("/")
    if not rel:
        return None

    # 1. Primary: `repak [-a KEY] get <pak> <internal_path>` directly to stdout (binary mode)
    try:
        proc = subprocess.run(
            _repak_cmd(["get", str(pak_path), rel], aes_key=aes_key),
            capture_output=True,
            timeout=60,
            **hidden_subprocess_kwargs(),
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except Exception as exc:
        logger.debug("repak get failed on %s (%s): %s", pak_path.name, rel, exc)

    # 2. Fallback: `repak [-a KEY] unpack -f -i <pattern> -o <out_dir> <pak>`
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            _run_tool(_repak_cmd(["unpack", "-f", "-i", rel, "-o", str(out_dir), str(pak_path)], aes_key=aes_key))
            candidate = out_dir / rel
            if not candidate.exists():
                matches = list(out_dir.rglob(Path(rel).name))
                candidate = matches[0] if matches else candidate
            if candidate.exists() and candidate.is_file():
                return candidate.read_bytes()
    except Exception as exc:
        logger.debug("repak unpack fallback failed on %s (%s): %s", pak_path.name, rel, exc)
    return None


def _read_utoc_locres_bytes(utoc_path: Path, internal_rel: str, aes_key: Optional[str] = None) -> Optional[bytes]:
    """Extract a .locres file from a UE5 IoStore .utoc container via `retoc unpack`."""
    retoc_bin = _resolve_retoc_path()
    if not retoc_bin or not utoc_path.exists():
        return None
    rel = internal_rel.strip().strip('"').replace("\\", "/").lstrip("/")
    target_name = Path(rel).name.lower()
    if not rel:
        return None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            cmd = _retoc_cmd(["unpack", str(utoc_path), str(out_dir)], aes_key=aes_key)
            _run_tool(cmd, timeout=90)
            direct = out_dir / rel
            if direct.exists() and direct.is_file():
                return direct.read_bytes()
            for cand in out_dir.rglob("*.locres"):
                cand_rel = str(cand.relative_to(out_dir)).replace("\\", "/").lower()
                if cand_rel.endswith(rel.lower()) or cand.name.lower() == target_name:
                    return cand.read_bytes()
    except Exception as exc:
        logger.debug("retoc unpack failed on %s (%s): %s", utoc_path.name, rel, exc)
    return None


def _detect_pak_version(pak_path: Path, aes_key: Optional[str] = None) -> Optional[str]:
    """Inspect .pak header version via `repak info` so mod paks match the target engine version."""
    if not DEFAULT_REPAK.exists() or not pak_path.exists():
        return None
    try:
        proc = _run_tool(_repak_cmd(["info", str(pak_path)], aes_key=aes_key))
        if proc.returncode == 0:
            m = re.search(r"\b(V\d+[AB]?)\b", proc.stdout or "")
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def verify_unreal_aes_key(game_dir: str | Path, raw_key: str) -> Dict[str, Any]:
    """Validate and test an AES key against the game's encrypted .pak or .utoc files."""
    norm_key = normalize_aes_key(raw_key)
    info = inspect_unreal_paks(game_dir)
    pak_files = info["all_pak_files"]
    utoc_files = info["utoc_files"]
    retoc_bin = _resolve_retoc_path()

    tested_any = False
    unlocked_any = False
    last_err = ""

    if DEFAULT_REPAK.exists() and pak_files:
        for pak in pak_files[:5]:
            tested_any = True
            proc = _run_tool(_repak_cmd(["list", str(pak)], aes_key=norm_key), timeout=30)
            if proc.returncode == 0:
                unlocked_any = True
                break
            last_err = (proc.stderr or proc.stdout or "").strip()

    if not unlocked_any and info["is_iostore"] and retoc_bin and utoc_files:
        for utoc in utoc_files[:5]:
            tested_any = True
            proc = _run_tool(_retoc_cmd(["list", str(utoc)], aes_key=norm_key), timeout=30)
            if proc.returncode == 0:
                unlocked_any = True
                break
            last_err = (proc.stderr or proc.stdout or "").strip()

    if not tested_any:
        return {
            "ok": False,
            "aes_key": norm_key,
            "is_iostore": info["is_iostore"],
            "has_pak_signature": info["has_pak_signature"],
            "error": "Không tìm thấy công cụ repak/retoc hoặc file .pak/.utoc để kiểm chứng AES Key.",
        }

    if not unlocked_any:
        return {
            "ok": False,
            "aes_key": norm_key,
            "is_iostore": info["is_iostore"],
            "has_pak_signature": info["has_pak_signature"],
            "error": "AES Key không khớp với file mã hóa của game này." + (f" ({last_err})" if last_err else ""),
        }

    return {
        "ok": True,
        "aes_key": norm_key,
        "is_iostore": info["is_iostore"],
        "has_pak_signature": info["has_pak_signature"],
        "shipping_exe_path": info["shipping_exe_path"],
        "error": "",
    }


def auto_dump_unreal_aes_keys(game_dir: str | Path) -> Dict[str, Any]:
    """Tier 2 static AES key scan via GHFear/AESDumpster on *-Win64-Shipping.exe (never unpacks DRM)."""
    info = inspect_unreal_paks(game_dir)
    ship_exe = info["shipping_exe_path"]
    dumper = _resolve_aes_dumpster_path()
    if not dumper:
        return {
            "ok": False,
            "error_code": "AES_KEY_REQUIRED",
            "shipping_exe_path": ship_exe,
            "is_iostore": info["is_iostore"],
            "has_pak_signature": info["has_pak_signature"],
            "message": "Chưa có AESDumpster.exe trong tools/. Hãy tải Tools hoặc nhập AES Key thủ công.",
        }
    if not ship_exe or not Path(ship_exe).exists():
        return {
            "ok": False,
            "error_code": "AES_KEY_REQUIRED",
            "shipping_exe_path": ship_exe,
            "is_iostore": info["is_iostore"],
            "has_pak_signature": info["has_pak_signature"],
            "message": "Không tìm thấy tệp *-Win64-Shipping.exe để dò khóa tự động.",
        }

    from core import hidden_subprocess_kwargs
    out_text = ""
    try:
        proc = subprocess.run(
            [str(dumper), str(ship_exe)],
            input="\r\n\r\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            **hidden_subprocess_kwargs(),
        )
        out_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out_text = (exc.stdout or "") + "\n" + (exc.stderr or "")
    except Exception as exc:
        logger.debug("AESDumpster failed on %s: %s", ship_exe, exc)

    raw_candidates = re.findall(r"0x[0-9A-Fa-f]{64}", out_text)
    seen: set[str] = set()
    candidates: List[str] = []
    for c in raw_candidates:
        norm = f"0x{c[2:].upper()}"
        if norm not in seen:
            seen.add(norm)
            candidates.append(norm)

    for cand in candidates:
        vr = verify_unreal_aes_key(game_dir, cand)
        if vr["ok"]:
            return {
                "ok": True,
                "aes_key": vr["aes_key"],
                "candidates": candidates,
                "shipping_exe_path": ship_exe,
                "is_iostore": info["is_iostore"],
                "has_pak_signature": info["has_pak_signature"],
            }

    return {
        "ok": False,
        "error_code": "AES_KEY_REQUIRED",
        "candidates": candidates,
        "shipping_exe_path": ship_exe,
        "is_iostore": info["is_iostore"],
        "has_pak_signature": info["has_pak_signature"],
        "message": "Không dò được AES Key tĩnh (file .exe có thể đang bị bọc DRM).",
    }


def scan_unreal_localization_detailed(
    game_dir: str | Path,
    aes_key: Optional[str] = None,
    auto_dump: bool = True,
) -> Dict[str, Any]:
    """3-Tier Unreal scanner: (1) direct/saved key via repak+retoc, (2) AESDumpster static scan, (3) AES_KEY_REQUIRED."""
    root = Path(game_dir)
    info = inspect_unreal_paks(root)
    norm_key = _safe_norm_aes_key(aes_key)
    discovered_key = ""

    def _run_tier1(active_key: str) -> Tuple[List[Dict[str, Any]], bool]:
        slots: Dict[str, Dict[str, Any]] = {}
        aes_blocked = False

        # A. Loose Content/Localization/**/<lang>/*.locres
        for locres_path in root.rglob("*.locres"):
            if not locres_path.is_file():
                continue
            if any(part.lower() == "~mods" for part in locres_path.parts):
                continue
            lang_dir = locres_path.parent.name
            code = _normalize_ue_lang(lang_dir)
            slot = slots.setdefault(
                code,
                {
                    "slot_id": code,
                    "raw_code": lang_dir,
                    "provider": "Unreal Loose .locres",
                    "files": [],
                    "pak_entries": [],
                    "utoc_entries": [],
                    "count": 0,
                },
            )
            slot["files"].append(str(locres_path))
            try:
                parsed = parse_locres_bytes_python(locres_path.read_bytes())
                slot["count"] += sum(len(ns["keys"]) for ns in parsed["namespaces"])
            except Exception:
                pass

        # B. Packed Content/Paks/*.pak via repak.exe (only *.locres)
        if DEFAULT_REPAK.exists():
            for pak in info["all_pak_files"]:
                try:
                    proc = _run_tool(_repak_cmd(["list", str(pak)], aes_key=active_key))
                    if proc.returncode != 0:
                        if _is_aes_error_output(proc.stdout, proc.stderr):
                            aes_blocked = True
                        continue
                    for line in proc.stdout.splitlines():
                        rel = line.strip().strip('"').replace("\\", "/").lstrip("/")
                        if rel.lower().endswith(".locres"):
                            parts = [p for p in rel.split("/") if p]
                            lang_dir = parts[-2] if len(parts) >= 2 else "en"
                            code = _normalize_ue_lang(lang_dir)
                            slot = slots.setdefault(
                                code,
                                {
                                    "slot_id": code,
                                    "raw_code": lang_dir,
                                    "provider": "Unreal .pak (repak)",
                                    "files": [],
                                    "pak_entries": [],
                                    "utoc_entries": [],
                                    "count": 0,
                                },
                            )
                            if active_key:
                                slot["aes_key"] = active_key
                            slot["pak_entries"].append({"pak_path": str(pak), "internal_path": rel})
                except Exception as exc:
                    logger.debug("repak list failed on %s: %s", pak, exc)

        # C. UE5 IoStore (.utoc/.ucas) via retoc.exe when .pak didn't expose .locres
        retoc_bin = _resolve_retoc_path()
        has_pak_locres = any(s.get("pak_entries") for s in slots.values())
        if info["is_iostore"] and retoc_bin and not has_pak_locres:
            for utoc in info["utoc_files"]:
                try:
                    proc = _run_tool(_retoc_cmd(["list", str(utoc)], aes_key=active_key), timeout=60)
                    if proc.returncode != 0:
                        if _is_aes_error_output(proc.stdout, proc.stderr):
                            aes_blocked = True
                        continue
                    for line in proc.stdout.splitlines():
                        rel = line.strip().strip('"').replace("\\", "/").lstrip("/")
                        if rel.lower().endswith(".locres"):
                            parts = [p for p in rel.split("/") if p]
                            lang_dir = parts[-2] if len(parts) >= 2 else "en"
                            code = _normalize_ue_lang(lang_dir)
                            slot = slots.setdefault(
                                code,
                                {
                                    "slot_id": code,
                                    "raw_code": lang_dir,
                                    "provider": "Unreal UE5 IoStore (retoc)",
                                    "files": [],
                                    "pak_entries": [],
                                    "utoc_entries": [],
                                    "count": 0,
                                },
                            )
                            if active_key:
                                slot["aes_key"] = active_key
                            slot["utoc_entries"].append({"utoc_path": str(utoc), "internal_path": rel})
                except Exception as exc:
                    logger.debug("retoc list failed on %s: %s", utoc, exc)

        # Populate accurate entry counts quickly for slots discovered inside .pak / .utoc
        for slot in slots.values():
            pak_entries = slot.get("pak_entries", [])
            utoc_entries = slot.get("utoc_entries", [])
            if (not pak_entries and not utoc_entries) or slot["count"] > 0:
                continue
            real_count = 0
            if pak_entries and len(pak_entries) <= 15:
                for item in pak_entries:
                    raw = _read_pak_file_bytes(Path(item["pak_path"]), item["internal_path"], aes_key=active_key)
                    if raw:
                        try:
                            real_count += sum(len(ns["keys"]) for ns in parse_locres_bytes_python(raw)["namespaces"])
                        except Exception:
                            pass
            elif utoc_entries and len(utoc_entries) <= 5:
                for item in utoc_entries:
                    raw = _read_utoc_locres_bytes(Path(item["utoc_path"]), item["internal_path"], aes_key=active_key)
                    if raw:
                        try:
                            real_count += sum(len(ns["keys"]) for ns in parse_locres_bytes_python(raw)["namespaces"])
                        except Exception:
                            pass
            slot["count"] = real_count if real_count > 0 else 100

        return list(slots.values()), aes_blocked

    # Tier 1
    slots_list, aes_blocked = _run_tier1(norm_key)
    is_aes_encrypted = bool(norm_key or aes_blocked)

    # Tier 2: Static AESDumpster discovery when encrypted & no key supplied yet
    if aes_blocked and not norm_key and auto_dump and not slots_list:
        dump_res = auto_dump_unreal_aes_keys(root)
        if dump_res.get("ok") and dump_res.get("aes_key"):
            discovered_key = dump_res["aes_key"]
            is_aes_encrypted = True
            slots_list, aes_blocked = _run_tier1(discovered_key)

    # Tier 3: Structured AES_KEY_REQUIRED if still blocked
    error_code: Optional[str] = None
    diagnostic: Optional[str] = None
    if not slots_list:
        if aes_blocked:
            error_code = "AES_KEY_REQUIRED"
            diagnostic = (
                "Unreal Engine (AES Key vừa nhập không khớp với file .pak/.utoc)"
                if norm_key
                else "Unreal Engine (.pak/.utoc được mã hóa AES — cần nhập AES Key)"
            )
        else:
            diagnostic = diagnose_unreal_empty_scan(root, aes_key=norm_key)

    return {
        "slots": slots_list,
        "is_aes_encrypted": is_aes_encrypted,
        "is_iostore": info["is_iostore"],
        "has_pak_signature": info["has_pak_signature"],
        "shipping_exe_path": info["shipping_exe_path"],
        "discovered_aes_key": discovered_key,
        "error_code": error_code,
        "diagnostic": diagnostic,
    }


def diagnose_unreal_empty_scan(game_dir: str | Path, aes_key: Optional[str] = None) -> Optional[str]:
    """Return a clear Vietnamese explanation when an Unreal game has 0 readable .locres slots."""
    info = inspect_unreal_paks(game_dir)
    paks = info["all_pak_files"]
    utocs = info["utoc_files"]
    clean_key = _safe_norm_aes_key(aes_key)
    retoc_bin = _resolve_retoc_path()

    has_utoc = bool(utocs)
    if has_utoc and not retoc_bin and not paks:
        return "Unreal Engine (chưa tải công cụ retoc.exe cho UE5 IoStore .utoc/.ucas — mở Kho Tools để tải)"
    if paks and not DEFAULT_REPAK.exists():
        return "Unreal Engine (chưa tải công cụ repak.exe — mở Cài đặt nâng cao để tải Tools)"

    aes_blocked = False
    if paks and DEFAULT_REPAK.exists():
        for pak in paks[:4]:
            try:
                proc = _run_tool(_repak_cmd(["list", str(pak)], aes_key=clean_key))
                if proc.returncode != 0 and _is_aes_error_output(proc.stdout, proc.stderr):
                    aes_blocked = True
                    break
            except Exception:
                pass
    if not aes_blocked and has_utoc and retoc_bin and utocs:
        for utoc in utocs[:4]:
            try:
                proc = _run_tool(_retoc_cmd(["list", str(utoc)], aes_key=clean_key))
                if proc.returncode != 0 and _is_aes_error_output(proc.stdout, proc.stderr):
                    aes_blocked = True
                    break
            except Exception:
                pass

    if aes_blocked:
        if clean_key:
            return "Unreal Engine (AES Key vừa nhập không khớp với file .pak/.utoc)"
        return "Unreal Engine (.pak/.utoc được mã hóa AES — cần nhập AES Key)"
    if has_utoc:
        if not retoc_bin:
            return "Unreal Engine (dữ liệu UE5 IoStore .utoc/.ucas — hãy tải công cụ retoc.exe trong Tools)"
        return "Unreal Engine (dữ liệu UE5 IoStore .utoc/.ucas không chứa file .locres chuẩn)"
    return None


def scan_unreal_localization(game_dir: str | Path, aes_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """Scan loose .locres, .pak (repak), or UE5 IoStore .utoc (retoc)."""
    return scan_unreal_localization_detailed(game_dir, aes_key=aes_key, auto_dump=False)["slots"]


def _extract_locres_with_unreallocres(locres_path: Path) -> Optional[List[Tuple[str, str, str]]]:
    """Use akintos/UnrealLocres.exe to export CSV -> [(namespace_or_key, source, target)] if present."""
    if not DEFAULT_UNREAL_LOCRES.exists():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        tmp_loc = tmp_dir / locres_path.name
        shutil.copy2(locres_path, tmp_loc)
        proc = _run_tool([str(DEFAULT_UNREAL_LOCRES), "export", str(tmp_loc)], cwd=tmp_dir)
        csv_path = tmp_loc.with_suffix(".csv")
        if not csv_path.exists():
            alt = Path(str(tmp_loc) + ".csv")
            csv_path = alt if alt.exists() else csv_path
        if proc.returncode != 0 or not csv_path.exists():
            return None
        rows: List[Tuple[str, str, str]] = []
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = row.get("key") or row.get("Key") or ""
                src = row.get("source") or row.get("Source") or ""
                tgt = row.get("target") or row.get("Target") or src
                rows.append((key, src, tgt))
        return rows


def _patch_locres_bytes(raw_src: bytes, updates_by_ns_key: Dict[Tuple[str, str], str]) -> Tuple[bytes, int]:
    """Patch .locres bytes via UnrealLocres.exe export/import (CSV) with pure-Python fallback."""
    if DEFAULT_UNREAL_LOCRES.exists() and updates_by_ns_key:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                in_loc = tmp_dir / "Source.locres"
                in_loc.write_bytes(raw_src)
                proc_exp = _run_tool([str(DEFAULT_UNREAL_LOCRES), "export", str(in_loc)], cwd=tmp_dir)
                csv_path = in_loc.with_suffix(".csv")
                if not csv_path.exists():
                    alt = Path(str(in_loc) + ".csv")
                    csv_path = alt if alt.exists() else csv_path
                if proc_exp.returncode == 0 and csv_path.exists():
                    updates_by_full_key: Dict[str, str] = {}
                    for (ns, lk), tr in updates_by_ns_key.items():
                        full_slash = f"{ns}/{lk}" if ns else lk
                        full_colon = f"{ns}::{lk}" if ns else lk
                        updates_by_full_key[full_slash] = tr
                        updates_by_full_key[full_colon] = tr
                        if not ns:
                            updates_by_full_key[lk] = tr

                    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
                        rows = list(csv.reader(f))
                    if rows:
                        hdr = [h.strip() for h in rows[0]]
                        low_hdr = [h.lower() for h in hdr]
                        if "key" in low_hdr and "target" in low_hdr:
                            k_idx = low_hdr.index("key")
                            t_idx = low_hdr.index("target")
                            s_idx = low_hdr.index("source") if "source" in low_hdr else -1
                            patched_cnt = 0
                            for r_i in range(1, len(rows)):
                                row = rows[r_i]
                                while len(row) <= max(k_idx, t_idx, s_idx):
                                    row.append("")
                                k_val = row[k_idx]
                                tr = updates_by_full_key.get(k_val)
                                if not tr and "/" not in k_val:
                                    tr = updates_by_ns_key.get(("", k_val))
                                if tr:
                                    row[t_idx] = tr
                                    patched_cnt += 1
                                elif s_idx >= 0 and not row[t_idx] and row[s_idx]:
                                    row[t_idx] = row[s_idx]
                            if patched_cnt > 0:
                                patched_csv = tmp_dir / "Patched.csv"
                                with open(patched_csv, "w", encoding="utf-8", newline="") as f:
                                    writer = csv.writer(f)
                                    writer.writerows(rows)
                                out_loc = tmp_dir / "Patched.locres"
                                proc_imp = _run_tool(
                                    [str(DEFAULT_UNREAL_LOCRES), "import", str(in_loc), str(patched_csv), "-f", "csv", "-o", str(out_loc)],
                                    cwd=tmp_dir,
                                )
                                if proc_imp.returncode == 0 and out_loc.exists() and out_loc.stat().st_size > 0:
                                    return out_loc.read_bytes(), patched_cnt
        except Exception as exc:
            logger.debug("UnrealLocres.exe import failed, falling back to Python writer: %s", exc)

    parsed = parse_locres_bytes_python(raw_src)
    out_bytes = write_locres_bytes_python(parsed, updates_by_ns_key)
    cnt = sum(1 for ns in parsed["namespaces"] for k in ns["keys"] if (ns["namespace"], k["key"]) in updates_by_ns_key)
    return out_bytes, cnt


def extract_unreal_slot_entries(game_dir: str | Path, slot_info: Dict[str, Any]) -> List[LocEntry]:
    """Extract strings from either loose .locres files or inside .pak via repak."""
    entries: List[LocEntry] = []
    code = slot_info["slot_id"]

    def _consume_locres_bytes(
        raw: bytes,
        fname: str,
        origin_pak: Optional[str] = None,
        internal_rel: Optional[str] = None,
        fpath_str: str = "",
    ) -> None:
        parsed = parse_locres_bytes_python(raw)
        if not parsed.get("namespaces") and DEFAULT_UNREAL_LOCRES.exists():
            with tempfile.TemporaryDirectory() as tmp_u:
                tmp_loc = Path(tmp_u) / fname
                tmp_loc.write_bytes(raw)
                rows = _extract_locres_with_unreallocres(tmp_loc)
                if rows:
                    for idx, (k, src, tgt) in enumerate(rows):
                        val = tgt or src
                        if not is_translatable_string(val):
                            continue
                        entries.append(
                            LocEntry(
                                entry_id=f"ue::{fname}::{k or idx}",
                                key=k or f"UE_{idx}",
                                text=val,
                                file_path=origin_pak or fpath_str,
                                lang=code,
                                fmt="unreal_locres",
                                category=classify_entry_category(k or "", val),
                                location_meta={
                                    "namespace": "",
                                    "locres_key": k or f"UE_{idx}",
                                    "internal_path": internal_rel or "",
                                    "raw_locres_path": fpath_str,
                                },
                            )
                        )
                    return

        for ns in parsed["namespaces"]:
            ns_name = ns["namespace"]
            for k in ns["keys"]:
                val = k["text"]
                if not is_translatable_string(val):
                    continue
                full_key = f"{ns_name}::{k['key']}" if ns_name else k["key"]
                cat = classify_entry_category(full_key, val)
                entries.append(
                    LocEntry(
                        entry_id=f"ue::{fname}::{full_key}",
                        key=full_key,
                        text=val,
                        file_path=origin_pak or fpath_str,
                        lang=code,
                        fmt="unreal_locres",
                        category=cat,
                        location_meta={
                            "namespace": ns_name,
                            "locres_key": k["key"],
                            "internal_path": internal_rel or "",
                            "raw_locres_path": fpath_str,
                        },
                    )
                )

    for fp in slot_info.get("files", []):
        p = Path(fp)
        if p.exists():
            _consume_locres_bytes(p.read_bytes(), p.name, fpath_str=str(p))

    # Extract pak_entries / utoc_entries directly via repak or retoc
    pak_entries = slot_info.get("pak_entries", [])
    utoc_entries = slot_info.get("utoc_entries", [])
    aes_key = slot_info.get("aes_key", "")
    if pak_entries and DEFAULT_REPAK.exists():
        for item in pak_entries:
            pak_path = Path(item["pak_path"])
            rel = item["internal_path"].lstrip("/")
            raw = _read_pak_file_bytes(pak_path, rel, aes_key=aes_key)
            if raw:
                _consume_locres_bytes(raw, Path(rel).name, origin_pak=str(pak_path), internal_rel=rel)

    if utoc_entries and _resolve_retoc_path():
        for item in utoc_entries:
            utoc_path = Path(item["utoc_path"])
            rel = item["internal_path"].lstrip("/")
            raw = _read_utoc_locres_bytes(utoc_path, rel, aes_key=aes_key)
            if raw:
                _consume_locres_bytes(raw, Path(rel).name, origin_pak=str(utoc_path), internal_rel=rel)

    return entries


def apply_unreal_translations(
    game_dir: str | Path,
    source_entries: List[LocEntry],
    translations: Dict[str, str],
    inject_slot_info: Dict[str, Any],
    backup_cb=None,
) -> Dict[str, Any]:
    """Patch loose .locres directly or build Content/Paks/~mods/pakchunk99-SakugaLoc_P.pak via repak."""
    root = Path(game_dir)
    updates_by_ns_key: Dict[Tuple[str, str], str] = {}
    for e in source_entries:
        tr = translations.get(e.text) or translations.get(e.entry_id)
        if tr:
            ns = e.location_meta.get("namespace", "")
            lk = e.location_meta.get("locres_key", e.key)
            updates_by_ns_key[(ns, lk)] = tr

    updated = 0
    # Case 1: Loose .locres files exist for inject_slot_info
    loose_files = [Path(p) for p in inject_slot_info.get("files", [])]
    if loose_files:
        if backup_cb:
            backup_cb([str(p) for p in loose_files], [])
        for lf in loose_files:
            if not lf.exists():
                continue
            out_bytes, cnt = _patch_locres_bytes(lf.read_bytes(), updates_by_ns_key)
            lf.write_bytes(out_bytes)
            updated += cnt
        return {"ok": True, "updated": updated, "mode": "loose_locres"}

    # Case 2: Pak / UE5 IoStore (.utoc) localization -> emit clean unencrypted mod pak Content/Paks/~mods/pakchunk99-SakugaLoc_P.pak
    pak_entries = inject_slot_info.get("pak_entries", [])
    utoc_entries = inject_slot_info.get("utoc_entries", [])
    aes_key = inject_slot_info.get("aes_key", "")
    if (pak_entries or utoc_entries) and DEFAULT_REPAK.exists():
        info = inspect_unreal_paks(root)
        paks_dir = Path(info["paks_dir"])
        mods_dir = paks_dir / "~mods"
        mods_dir.mkdir(parents=True, exist_ok=True)
        mod_pak_path = mods_dir / "pakchunk99-SakugaLoc_P.pak"

        if mod_pak_path.exists():
            if backup_cb:
                backup_cb([str(mod_pak_path)], [])
        else:
            if backup_cb:
                backup_cb([], [str(mod_pak_path)])

        pak_ver = _detect_pak_version(Path(pak_entries[0]["pak_path"]), aes_key=aes_key) if pak_entries else "V11"
        with tempfile.TemporaryDirectory() as tmpdir:
            stage_root = Path(tmpdir) / "pak_stage"
            stage_root.mkdir(parents=True, exist_ok=True)
            for item in pak_entries:
                pak_path = Path(item["pak_path"])
                rel = item["internal_path"].lstrip("/")
                raw_src = _read_pak_file_bytes(pak_path, rel, aes_key=aes_key)
                if not raw_src:
                    continue
                patched_bytes, cnt = _patch_locres_bytes(raw_src, updates_by_ns_key)
                dest_loc = stage_root / rel
                dest_loc.parent.mkdir(parents=True, exist_ok=True)
                dest_loc.write_bytes(patched_bytes)
                updated += cnt
            for item in utoc_entries:
                utoc_path = Path(item["utoc_path"])
                rel = item["internal_path"].lstrip("/")
                raw_src = _read_utoc_locres_bytes(utoc_path, rel, aes_key=aes_key)
                if not raw_src:
                    continue
                patched_bytes, cnt = _patch_locres_bytes(raw_src, updates_by_ns_key)
                dest_loc = stage_root / rel
                dest_loc.parent.mkdir(parents=True, exist_ok=True)
                dest_loc.write_bytes(patched_bytes)
                updated += cnt
            cmd = [str(DEFAULT_REPAK), "pack", "--quiet"]
            if pak_ver:
                cmd.extend(["--version", pak_ver])
            cmd.extend([str(stage_root), str(mod_pak_path)])
            proc_pack = _run_tool(cmd)
            if proc_pack.returncode != 0 or not mod_pak_path.exists():
                return {
                    "ok": False,
                    "updated": 0,
                    "error": f"repak pack thất bại: {(proc_pack.stderr or proc_pack.stdout or '').strip()}",
                }

        return {
            "ok": mod_pak_path.exists(),
            "updated": updated,
            "mode": "mod_pak",
            "mod_pak": str(mod_pak_path),
            "has_pak_signature": info["has_pak_signature"],
        }

    return {"ok": False, "updated": 0, "error": "Không tìm thấy tệp .locres đích."}
