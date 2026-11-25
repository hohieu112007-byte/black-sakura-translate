"""Unreal Engine .locres & .pak localization extractor/writer (repak + UnrealLocres + pure-Python fallback)."""

from __future__ import annotations

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


def _run_tool(cmd: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    from core import hidden_subprocess_kwargs
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        **hidden_subprocess_kwargs(),
    )


def _normalize_aes_key(aes_key: Optional[str]) -> str:
    k = (aes_key or "").strip()
    if k.lower().startswith("0x"):
        k = k[2:].strip()
    return k


def _repak_cmd(args: List[str], aes_key: Optional[str] = None) -> List[str]:
    clean_key = _normalize_aes_key(aes_key)
    if clean_key:
        return [str(DEFAULT_REPAK), "-a", clean_key, *args]
    return [str(DEFAULT_REPAK), *args]


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


def diagnose_unreal_empty_scan(game_dir: str | Path, aes_key: Optional[str] = None) -> Optional[str]:
    """Return a clear Vietnamese explanation when an Unreal game has 0 readable .locres slots."""
    root = Path(game_dir)
    utocs = list(root.rglob("*.utoc"))
    paks = [p for p in root.rglob("*.pak") if p.parent.name.lower() != "~mods"]
    if utocs and not paks:
        return "Unreal Engine (dữ liệu đóng gói UE5 IoStore .utoc/.ucas — chưa có file .locres rời hoặc trong .pak)"
    if paks and not DEFAULT_REPAK.exists():
        return "Unreal Engine (chưa tải công cụ repak.exe — mở Cài đặt nâng cao để tải Tools)"
    if paks and DEFAULT_REPAK.exists():
        clean_key = _normalize_aes_key(aes_key)
        aes_blocked = False
        for pak in paks[:4]:
            try:
                proc = _run_tool(_repak_cmd(["list", str(pak)], aes_key=clean_key))
                err = (proc.stderr or "") + "\n" + (proc.stdout or "")
                if proc.returncode != 0 and any(tok in err.lower() for tok in ("aes", "encrypt", "key")):
                    aes_blocked = True
                    break
            except Exception:
                pass
        if aes_blocked:
            if clean_key:
                return "Unreal Engine (AES Key vừa nhập không khớp với file .pak)"
            return "Unreal Engine (.pak được mã hóa AES — nhập AES Key bên dưới để giải mã)"
        if utocs:
            return "Unreal Engine (dữ liệu đóng gói UE5 IoStore .utoc/.ucas — chưa có file .locres rời hoặc trong .pak)"
    return None


def scan_unreal_localization(game_dir: str | Path, aes_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """Scan loose Content/Localization/*/<lang>/*.locres OR Unreal .pak indexes via repak.exe."""
    root = Path(game_dir)
    slots: Dict[str, Dict[str, Any]] = {}
    clean_key = _normalize_aes_key(aes_key)

    # 1. Loose Content/Localization/**/<lang>/*.locres
    for locres_path in root.rglob("*.locres"):
        if not locres_path.is_file():
            continue
        if any(part.startswith("~mods") for part in locres_path.parts):
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
                "count": 0,
            },
        )
        slot["files"].append(str(locres_path))
        try:
            parsed = parse_locres_bytes_python(locres_path.read_bytes())
            slot["count"] += sum(len(ns["keys"]) for ns in parsed["namespaces"])
        except Exception:
            pass

    # 2. Packed Content/Paks/*.pak via repak.exe
    if DEFAULT_REPAK.exists():
        pak_files = sorted(root.rglob("*.pak"))
        for pak in pak_files:
            if pak.parent.name.lower() == "~mods":
                continue
            try:
                proc = _run_tool(_repak_cmd(["list", str(pak)], aes_key=clean_key))
                if proc.returncode != 0:
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
                                "count": 0,
                            },
                        )
                        if clean_key:
                            slot["aes_key"] = clean_key
                        slot["pak_entries"].append({"pak_path": str(pak), "internal_path": rel})
            except Exception as exc:
                logger.debug("repak list failed on %s: %s", pak, exc)

        # Populate accurate entry counts quickly for slots discovered inside .pak
        for slot in slots.values():
            pak_entries = slot.get("pak_entries", [])
            if not pak_entries or slot["count"] > 0:
                continue
            if len(pak_entries) <= 15:
                real_count = 0
                for item in pak_entries:
                    raw = _read_pak_file_bytes(Path(item["pak_path"]), item["internal_path"], aes_key=clean_key)
                    if not raw:
                        continue
                    try:
                        parsed = parse_locres_bytes_python(raw)
                        real_count += sum(len(ns["keys"]) for ns in parsed["namespaces"])
                    except Exception:
                        pass
                slot["count"] = real_count if real_count > 0 else 100
            else:
                slot["count"] = 100

    return list(slots.values())


def _extract_locres_with_unreallocres(locres_path: Path) -> Optional[List[Tuple[str, str, str]]]:
    """Use akintos/UnrealLocres.exe to export CSV -> [(namespace_or_key, source, target)] if present."""
    if not DEFAULT_UNREAL_LOCRES.exists():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        tmp_loc = Path(tmp) / locres_path.name
        shutil.copy2(locres_path, tmp_loc)
        proc = _run_tool([str(DEFAULT_UNREAL_LOCRES), "export", str(tmp_loc)])
        csv_path = Path(str(tmp_loc) + ".csv")
        if not csv_path.exists():
            alt = tmp_loc.with_suffix(".csv")
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

    # Extract pak_entries directly via repak get / unpack -f -i
    pak_entries = slot_info.get("pak_entries", [])
    aes_key = slot_info.get("aes_key", "")
    if pak_entries and DEFAULT_REPAK.exists():
        for item in pak_entries:
            pak_path = Path(item["pak_path"])
            rel = item["internal_path"].lstrip("/")
            raw = _read_pak_file_bytes(pak_path, rel, aes_key=aes_key)
            if raw:
                _consume_locres_bytes(raw, Path(rel).name, origin_pak=str(pak_path), internal_rel=rel)

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
            raw = lf.read_bytes()
            parsed = parse_locres_bytes_python(raw)
            out_bytes = write_locres_bytes_python(parsed, updates_by_ns_key)
            lf.write_bytes(out_bytes)
            updated += sum(
                1 for ns in parsed["namespaces"] for k in ns["keys"] if (ns["namespace"], k["key"]) in updates_by_ns_key
            )
        return {"ok": True, "updated": updated, "mode": "loose_locres"}

    # Case 2: Pak-based Unreal localization -> emit clean mod pak Content/Paks/~mods/pakchunk99-SakugaLoc_P.pak
    pak_entries = inject_slot_info.get("pak_entries", [])
    aes_key = inject_slot_info.get("aes_key", "")
    if pak_entries and DEFAULT_REPAK.exists():
        # Find Paks dir
        paks_dirs = [p for p in root.rglob("Paks") if p.is_dir()]
        paks_dir = paks_dirs[0] if paks_dirs else (root / "Content" / "Paks")
        mods_dir = paks_dir / "~mods"
        mods_dir.mkdir(parents=True, exist_ok=True)
        mod_pak_path = mods_dir / "pakchunk99-SakugaLoc_P.pak"

        if mod_pak_path.exists():
            if backup_cb:
                backup_cb([str(mod_pak_path)], [])
        else:
            if backup_cb:
                backup_cb([], [str(mod_pak_path)])

        pak_ver = _detect_pak_version(Path(pak_entries[0]["pak_path"]), aes_key=aes_key)
        with tempfile.TemporaryDirectory() as tmpdir:
            stage_root = Path(tmpdir) / "pak_stage"
            stage_root.mkdir(parents=True, exist_ok=True)
            for item in pak_entries:
                pak_path = Path(item["pak_path"])
                rel = item["internal_path"].lstrip("/")
                raw_src = _read_pak_file_bytes(pak_path, rel, aes_key=aes_key)
                if not raw_src:
                    continue
                parsed = parse_locres_bytes_python(raw_src)
                patched_bytes = write_locres_bytes_python(parsed, updates_by_ns_key)
                dest_loc = stage_root / rel
                dest_loc.parent.mkdir(parents=True, exist_ok=True)
                dest_loc.write_bytes(patched_bytes)
                updated += sum(
                    1 for ns in parsed["namespaces"] for k in ns["keys"] if (ns["namespace"], k["key"]) in updates_by_ns_key
                )
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
        }

    return {"ok": False, "updated": 0, "error": "Không tìm thấy tệp .locres đích."}
