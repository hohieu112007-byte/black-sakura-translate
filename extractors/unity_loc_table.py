"""Unity Native Localization Extractor & Writer (StringTable / I2Languages + Addressables catalog.bin sync)."""

from __future__ import annotations

import base64
import gc
import io
import json
import logging
import os
import re
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import UnityPy

from core.atomic_io import atomic_write_bytes, atomic_write_text
from extractors.subtitle_filter import LocEntry, classify_entry_category, is_translatable_string

logger = logging.getLogger("sakuga.unity_loc")

UNITY_LOC_FILE_PATTERNS = (
    "*localization*.bundle",
    "*stringtable*.bundle",
    "*string-table*.bundle",
    "*i2languages*.bundle",
    "*language*.bundle",
)

LANG_NAME_TO_CODE = {
    "english": "en",
    "vietnamese": "vi",
    "tiengviet": "vi",
    "french": "fr",
    "francais": "fr",
    "spanish": "es",
    "espanol": "es",
    "german": "de",
    "deutsch": "de",
    "polish": "pl",
    "polski": "pl",
    "portuguese": "pt",
    "portugues": "pt",
    "portuguesebrazil": "pt-BR",
    "brazilian": "pt-BR",
    "italian": "it",
    "italiano": "it",
    "japanese": "ja",
    "korean": "ko",
    "russian": "ru",
    "turkish": "tr",
    "ukrainian": "uk",
    "swedish": "sv",
    "dutch": "nl",
    "chinese": "zh",
    "schinese": "zh",
    "tchinese": "zh-TW",
    "simplifiedchinese": "zh",
    "traditionalchinese": "zh-TW",
}


def normalize_lang_token(raw: str) -> str:
    if not raw:
        return "unknown"
    s = raw.strip()
    paren = re.search(r"\(([a-zA-Z]{2,3}(?:[-_][a-zA-Z0-9]{2,4})?)\)", s)
    if paren:
        return _canon_code(paren.group(1))
    # Remove suffixes like _assets_all
    cleaned = re.sub(r"(localization|string[-_]?tables?|language|source|assets|all)", "", s, flags=re.IGNORECASE)
    cleaned_alpha = re.sub(r"[^a-zA-Z0-9_-]", "", cleaned).strip("_-")
    if len(cleaned_alpha) in (2, 3) and cleaned_alpha.isalpha():
        return _canon_code(cleaned_alpha)
    low_compact = re.sub(r"[^a-z]", "", s.lower())
    for k, v in LANG_NAME_TO_CODE.items():
        if k in low_compact:
            return v
    if re.match(r"^[a-zA-Z]{2}(?:[-_][A-Za-z0-9]{2,4})?$", s):
        return _canon_code(s)
    return s.lower()


def _canon_code(code: str) -> str:
    c = code.replace("_", "-").strip()
    low = c.lower()
    if low in ("pt-br", "ptbr"):
        return "pt-BR"
    if low in ("zh-tw", "zhtw", "zh-hant"):
        return "zh-TW"
    if low in ("zh-cn", "zhcn", "zh-hans"):
        return "zh"
    if len(low) > 2 and "-" in low:
        return low.split("-")[0]
    return low


def find_unity_loc_candidates(game_dir: Path) -> Dict[str, List[Path]]:
    """Locate ONLY native localization bundles / resources.assets / I2 assets without touching level*.assets."""
    aa_bundles: List[Path] = []
    resource_assets: List[Path] = []

    data_dirs = [p for p in game_dir.iterdir() if p.is_dir() and p.name.endswith("_Data")]
    for dd in data_dirs:
        aa_dir = dd / "StreamingAssets" / "aa"
        if aa_dir.exists():
            seen: set[Path] = set()
            for pattern in UNITY_LOC_FILE_PATTERNS:
                for bf in aa_dir.rglob(pattern):
                    if bf.is_file() and bf not in seen:
                        seen.add(bf)
                        aa_bundles.append(bf)
            # Also accept any bundle whose filename contains common locale markers like (en), (fr), _en_
            for bf in aa_dir.rglob("*.bundle"):
                if bf in seen or not bf.is_file():
                    continue
                low = bf.name.lower()
                if "loc" in low or "lang" in low or "i18n" in low or re.search(r"\([a-z]{2}(?:-[a-z]{2})?\)", low):
                    seen.add(bf)
                    aa_bundles.append(bf)

        res_file = dd / "resources.assets"
        if res_file.exists() and res_file.is_file():
            resource_assets.append(res_file)

    return {"bundles": sorted(aa_bundles), "resources": sorted(resource_assets)}


def _is_i2_language_source(tree: Dict[str, Any]) -> bool:
    return isinstance(tree, dict) and ("mSource" in tree or ("mTerms" in tree and "mLanguages" in tree))


def _is_unity_string_table(tree: Dict[str, Any]) -> bool:
    return isinstance(tree, dict) and "m_TableData" in tree and isinstance(tree.get("m_TableData"), list)


def scan_unity_loc_tables(game_dir: str | Path) -> List[Dict[str, Any]]:
    """Fast scan of Unity native localization assets (StringTable & I2Languages)."""
    root = Path(game_dir)
    candidates = find_unity_loc_candidates(root)
    slots: Dict[str, Dict[str, Any]] = {}

    # 1. Addressables StringTable bundles (fast scan by filename + verification of first MonoBehaviour)
    for bf in candidates["bundles"]:
        lang_hint = normalize_lang_token(bf.stem)
        try:
            env = UnityPy.load(str(bf))
            found_tables = 0
            found_i2 = False
            for obj in env.objects:
                if obj.type.name != "MonoBehaviour":
                    continue
                tree = obj.read_typetree()
                if _is_unity_string_table(tree):
                    found_tables += 1
                    locale_id = tree.get("m_LocaleId")
                    loc_code = ""
                    if isinstance(locale_id, dict):
                        loc_code = locale_id.get("m_Code", "") or ""
                    elif isinstance(locale_id, str):
                        loc_code = locale_id
                    code = normalize_lang_token(loc_code or lang_hint)
                    slot = slots.setdefault(
                        code,
                        {
                            "slot_id": code,
                            "raw_code": loc_code or lang_hint,
                            "provider": "Unity StringTable (Addressables)",
                            "files": [],
                            "count": 0,
                        },
                    )
                    if str(bf) not in slot["files"]:
                        slot["files"].append(str(bf))
                    slot["count"] += sum(1 for row in tree.get("m_TableData", []) if row.get("m_Localized"))
                elif _is_i2_language_source(tree):
                    found_i2 = True
                    _register_i2_slots(tree, bf, slots)
            if not found_tables and not found_i2:
                continue
        except Exception as exc:
            logger.debug("Skipping Unity bundle %s: %s", bf.name, exc)

    # 2. Check resources.assets strictly for MonoBehaviour objects named I2Languages / StringTable / LanguageSource
    for rf in candidates["resources"]:
        try:
            env = UnityPy.load(str(rf))
            for obj in env.objects:
                if obj.type.name != "MonoBehaviour":
                    continue
                try:
                    data = obj.read()
                    name = getattr(data, "m_Name", "") or ""
                except Exception:
                    name = ""
                if not any(tok in name.lower() for tok in ("i2", "lang", "loc", "stringtable", "text")):
                    continue
                try:
                    tree = obj.read_typetree()
                except Exception:
                    continue
                if _is_i2_language_source(tree):
                    _register_i2_slots(tree, rf, slots)
                elif _is_unity_string_table(tree):
                    locale_id = tree.get("m_LocaleId")
                    loc_code = locale_id.get("m_Code", "") if isinstance(locale_id, dict) else str(locale_id or "")
                    code = normalize_lang_token(loc_code or name or "en")
                    slot = slots.setdefault(
                        code,
                        {
                            "slot_id": code,
                            "raw_code": loc_code or code,
                            "provider": "Unity StringTable (resources.assets)",
                            "files": [],
                            "count": 0,
                        },
                    )
                    if str(rf) not in slot["files"]:
                        slot["files"].append(str(rf))
                    slot["count"] += len(tree.get("m_TableData", []))
        except Exception as exc:
            logger.debug("Skipping Unity resource %s: %s", rf.name, exc)

    return list(slots.values())


def _register_i2_slots(tree: Dict[str, Any], asset_file: Path, slots: Dict[str, Dict[str, Any]]) -> None:
    src = tree.get("mSource", tree)
    langs = src.get("mLanguages", [])
    terms = src.get("mTerms", [])
    for idx, lang_info in enumerate(langs):
        raw_name = lang_info.get("Name") or lang_info.get("Code") or f"lang_{idx}"
        raw_code = lang_info.get("Code") or raw_name
        code = normalize_lang_token(raw_code or raw_name)
        slot = slots.setdefault(
            code,
            {
                "slot_id": code,
                "raw_code": raw_code,
                "raw_name": raw_name,
                "lang_index": idx,
                "provider": "Unity I2 Localization",
                "files": [],
                "count": 0,
            },
        )
        if str(asset_file) not in slot["files"]:
            slot["files"].append(str(asset_file))
        non_empty = 0
        for t in terms:
            vals = t.get("Languages", [])
            if idx < len(vals) and vals[idx]:
                non_empty += 1
        slot["count"] += non_empty


def _load_shared_table_keys(env: UnityPy.Environment) -> Dict[int, str]:
    """Read StringTableSharedData inside a bundle (if packaged alongside) to resolve friendly key names."""
    key_map: Dict[int, str] = {}
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            tree = obj.read_typetree()
        except Exception:
            continue
        entries = tree.get("m_Entries")
        if isinstance(entries, list) and entries and isinstance(entries[0], dict) and "m_Key" in entries[0]:
            for item in entries:
                if "m_Id" in item and "m_Key" in item:
                    key_map[int(item["m_Id"])] = str(item["m_Key"])
    return key_map


def extract_unity_slot_entries(game_dir: str | Path, slot_info: Dict[str, Any]) -> List[LocEntry]:
    """Extract all translatable entries from the chosen Unity localization slot."""
    entries: List[LocEntry] = []
    files = [Path(p) for p in slot_info.get("files", [])]
    slot_code = slot_info["slot_id"]
    provider = slot_info.get("provider", "")

    for fpath in files:
        if not fpath.exists():
            continue
        env = UnityPy.load(str(fpath))
        shared_keys = _load_shared_table_keys(env)
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                tree = obj.read_typetree()
            except Exception:
                continue

            table_name = str(tree.get("m_Name") or fpath.stem)

            if "I2" in provider or _is_i2_language_source(tree):
                src = tree.get("mSource", tree)
                langs = src.get("mLanguages", [])
                terms = src.get("mTerms", [])
                target_idx = slot_info.get("lang_index")
                if target_idx is None:
                    for i, l in enumerate(langs):
                        if normalize_lang_token(l.get("Code") or l.get("Name") or "") == slot_code:
                            target_idx = i
                            break
                if target_idx is None:
                    continue
                for t_idx, term in enumerate(terms):
                    key = str(term.get("Term") or f"I2_{t_idx}")
                    vals = term.get("Languages", [])
                    val = str(vals[target_idx]) if target_idx < len(vals) and vals[target_idx] is not None else ""
                    if not is_translatable_string(val):
                        continue
                    cat = classify_entry_category(key, val)
                    eid = f"unity_i2::{fpath.name}::{obj.path_id}::{t_idx}"
                    entries.append(
                        LocEntry(
                            entry_id=eid,
                            key=key,
                            text=val,
                            file_path=str(fpath),
                            lang=slot_code,
                            fmt="unity_i2",
                            category=cat,
                            location_meta={
                                "path_id": obj.path_id,
                                "term_index": t_idx,
                                "lang_index": target_idx,
                                "table_name": table_name,
                            },
                        )
                    )

            elif _is_unity_string_table(tree):
                rows = tree.get("m_TableData", [])
                for r_idx, row in enumerate(rows):
                    val = str(row.get("m_Localized") or "")
                    if not is_translatable_string(val):
                        continue
                    row_id = int(row.get("m_Id", r_idx))
                    friendly_key = shared_keys.get(row_id) or str(row.get("m_Key") or f"{table_name}_{row_id}")
                    cat = classify_entry_category(friendly_key, val)
                    eid = f"unity_st::{fpath.name}::{obj.path_id}::{row_id}"
                    entries.append(
                        LocEntry(
                            entry_id=eid,
                            key=friendly_key,
                            text=val,
                            file_path=str(fpath),
                            lang=slot_code,
                            fmt="unity_stringtable",
                            category=cat,
                            location_meta={
                                "path_id": obj.path_id,
                                "row_id": row_id,
                                "row_index": r_idx,
                                "table_name": table_name,
                            },
                        )
                    )
    return entries


def _find_catalog_bin(game_dir: Path) -> Optional[Path]:
    """Locate Unity Addressables catalog.bin (Unity 6/Binary) or catalog.json (Addressables 1.x)."""
    for dd in game_dir.iterdir():
        if dd.is_dir() and dd.name.endswith("_Data"):
            aa_dir = dd / "StreamingAssets" / "aa"
            for name in ("catalog.bin", "catalog.json"):
                cat = aa_dir / name
                if cat.exists() and cat.is_file():
                    return cat
    return None


def _read_dotnet_7bit_str(buf: bytes, pos: int) -> Tuple[bytes, int]:
    """Read a .NET BinaryReader 7-bit length-prefixed string slice cleanly."""
    strlen = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        strlen |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    end = pos + strlen
    return buf[pos:end], end


def _sync_json_catalog(
    catalog_path: Path,
    bundle_updates: List[Tuple[str, int, int]],
) -> bool:
    """Update m_Crc -> 0, m_UseCrcForCachedBundles -> False, m_BundleSize -> new_size in catalog.json."""
    try:
        cat = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not parse Addressables catalog.json %s: %s", catalog_path, exc)
        return False

    ids = cat.get("m_InternalIds")
    ent_b64 = cat.get("m_EntryDataString")
    ext_b64 = cat.get("m_ExtraDataString")
    if not isinstance(ids, list) or not isinstance(ent_b64, str) or not isinstance(ext_b64, str):
        return False

    bundle_size_map = {bname: int(new_sz) for bname, _old_sz, new_sz in bundle_updates if new_sz > 0}
    if not bundle_size_map:
        return False

    try:
        ent = bytearray(base64.b64decode(ent_b64))
        ext = base64.b64decode(ext_b64)
    except Exception:
        return False

    if len(ent) < 4:
        return False
    count = struct.unpack_from("<I", ent, 0)[0]
    if len(ent) < 4 + count * 28:
        return False

    # Map each referenced ExtraDataString byte offset -> target new bundle size
    target_offsets: Dict[int, int] = {}
    for k in range(count):
        vals = struct.unpack_from("<iiiiiii", ent, 4 + k * 28)
        id_idx, data_idx = vals[0], vals[4]
        if data_idx >= 0 and 0 <= id_idx < len(ids):
            bname = Path(str(ids[id_idx]).replace("\\", "/")).name
            if bname in bundle_size_map:
                target_offsets[data_idx] = bundle_size_map[bname]

    if not target_offsets:
        return False

    # Walk sequential 0x07 records inside m_ExtraDataString, update target JSON blocks, and remap offsets
    new_ext = bytearray()
    offset_map: Dict[int, int] = {}
    pos = 0
    modified = False

    while pos < len(ext):
        old_off = pos
        marker = ext[pos]
        if marker != 0x07:
            # Unexpected record type — bail safely without corrupting catalog.json
            return False
        p = pos + 1
        asm_bytes, p = _read_dotnet_7bit_str(ext, p)
        type_bytes, p = _read_dotnet_7bit_str(ext, p)
        if p + 4 > len(ext):
            return False
        jlen = struct.unpack_from("<I", ext, p)[0]
        jstart = p + 4
        jend = jstart + jlen
        if jend > len(ext):
            return False

        header_prefix = ext[old_off:jstart - 4]
        payload_bytes = ext[jstart:jend]

        if old_off in target_offsets:
            target_sz = target_offsets[old_off]
            try:
                obj = json.loads(payload_bytes.decode("utf-16-le"))
                if (
                    int(obj.get("m_Crc", 0) or 0) != 0
                    or bool(obj.get("m_UseCrcForCachedBundles"))
                    or int(obj.get("m_BundleSize", 0) or 0) != target_sz
                ):
                    obj["m_Crc"] = 0
                    if "m_UseCrcForCachedBundles" in obj:
                        obj["m_UseCrcForCachedBundles"] = False
                    if "m_BundleSize" in obj:
                        obj["m_BundleSize"] = target_sz
                    payload_bytes = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-16-le")
                    modified = True
            except Exception:
                pass

        new_off = len(new_ext)
        offset_map[old_off] = new_off
        new_ext.extend(header_prefix)
        new_ext.extend(struct.pack("<I", len(payload_bytes)))
        new_ext.extend(payload_bytes)
        pos = jend

    if not modified:
        return False

    # Rewrite dataIndex field (index 4 of 7 int32 fields) for every entry in m_EntryDataString
    for k in range(count):
        base = 4 + k * 28
        vals = list(struct.unpack_from("<iiiiiii", ent, base))
        if vals[4] >= 0:
            if vals[4] not in offset_map:
                return False
            vals[4] = offset_map[vals[4]]
            struct.pack_into("<iiiiiii", ent, base, *vals)

    cat["m_EntryDataString"] = base64.b64encode(bytes(ent)).decode("ascii")
    cat["m_ExtraDataString"] = base64.b64encode(bytes(new_ext)).decode("ascii")
    atomic_write_text(catalog_path, json.dumps(cat, separators=(",", ":"), ensure_ascii=False))
    return True


def _sync_addressables_catalog(
    catalog_path: Path,
    bundle_updates: List[Tuple[str, int, int]],
) -> bool:
    """Update m_Crc -> 0 and m_BundleSize -> new_size inside Unity Addressables catalog.bin or catalog.json.

    bundle_updates: list of (bundle_filename, old_size, new_size)
    """
    if not catalog_path.exists() or not bundle_updates:
        return False

    if catalog_path.suffix.lower() == ".json":
        return _sync_json_catalog(catalog_path, bundle_updates)

    data = bytearray(catalog_path.read_bytes())
    modified = False

    for bundle_name, old_size, new_size in bundle_updates:
        if old_size <= 0 or new_size <= 0:
            continue
        # Search for UTF-16LE or ASCII filename occurrence to bound search region, fallback full scan of size pattern
        old_size_le = struct.pack("<Q", old_size)
        old_size_u32 = struct.pack("<I", old_size)

        # Unity Addressables AssetBundleRequestOptions typically packs m_Crc (uint32) right next to m_BundleSize (int64/uint32) or serialized JSON/UTF-16 strings
        needle_ascii = bundle_name.encode("utf-8")
        needle_u16 = bundle_name.encode("utf-16-le")
        text_view = bytes(data)

        # Case A: JSON-like or ASCII string inside catalog extra data ("m_Crc":12345,"m_BundleSize":67890)
        if b'"m_BundleSize"' in text_view or b"m_BundleSize" in text_view:
            s_text = text_view.decode("utf-8", errors="latin1")
            if bundle_name in s_text:
                # Replace JSON bundle size & zero out m_Crc near the bundle name
                pattern = re.compile(
                    re.escape(bundle_name) + r'.{0,500}?"m_Crc"\s*:\s*(\d+).{0,120}?"m_BundleSize"\s*:\s*' + str(old_size),
                    re.DOTALL,
                )
                def _json_repl(m: re.Match) -> str:
                    seg = m.group(0)
                    seg = re.sub(r'"m_Crc"\s*:\s*\d+', '"m_Crc":0', seg, count=1)
                    seg = re.sub(r'"m_BundleSize"\s*:\s*\d+', f'"m_BundleSize":{new_size}', seg, count=1)
                    return seg
                patched_text, count = pattern.subn(_json_repl, s_text)
                if count > 0:
                    data = bytearray(patched_text.encode("latin1"))
                    modified = True
                    continue

        # Case B: UTF-16LE serialized JSON in Unity catalog binary m_ExtraDataString
        if b'"\x00m\x00_\x00B\x00u\x00n\x00d\x00l\x00e\x00S\x00i\x00z\x00e\x00"\x00' in text_view:
            # Locate bundle name in UTF-16LE and update UTF-16LE m_Crc / m_BundleSize
            idx = text_view.find(needle_u16)
            if idx != -1:
                window_start = max(0, idx - 256)
                window_end = min(len(text_view), idx + len(needle_u16) + 2048)
                chunk = text_view[window_start:window_end]
                # Replace inside chunk if even-aligned UTF-16LE
                try:
                    decoded = chunk.decode("utf-16-le", errors="ignore")
                    if str(old_size) in decoded and "m_Crc" in decoded:
                        new_dec = re.sub(r'"m_Crc"\s*:\s*\d+', '"m_Crc":0', decoded)
                        new_dec = re.sub(r'"m_BundleSize"\s*:\s*' + str(old_size), f'"m_BundleSize":{new_size}', new_dec)
                        if len(new_dec) != len(decoded):
                            # Keep exact binary length using spaces after colon if shrunk/grown? Wait: catalog stores length prefix before m_ExtraDataString!
                            pass
                except Exception:
                    pass

        # Case C: Binary catalog (Unity Addressables 1.21+/Unity 6)
        bundle_matched = False

        # C1: Unity 6 binary catalog record layout:
        # <bundle_name> ... \x20\x00\x00\x00 <32-byte ASCII hex hash> <uint32 ptr1> <uint32 ptr2> <uint32 m_Crc> <uint32 m_BundleSize>
        search_from = 0
        while True:
            b_idx = bytes(data).find(needle_ascii, search_from)
            if b_idx == -1:
                break
            b_end = b_idx + len(needle_ascii)
            tail = bytes(data[b_end : b_end + 96])
            hash_hdr = tail.find(b"\x20\x00\x00\x00")
            if hash_hdr != -1:
                hash_start = b_end + hash_hdr + 4
                rec_base = hash_start + 32
                if rec_base + 16 <= len(data):
                    hash_bytes = bytes(data[hash_start:rec_base])
                    if all(c in b"0123456789abcdefABCDEF" for c in hash_bytes):
                        cur_crc = struct.unpack_from("<I", data, rec_base + 8)[0]
                        cur_sz = struct.unpack_from("<I", data, rec_base + 12)[0]
                        if cur_crc != 0:
                            struct.pack_into("<I", data, rec_base + 8, 0)
                            modified = True
                        if cur_sz != new_size:
                            struct.pack_into("<I", data, rec_base + 12, new_size)
                            modified = True
                        bundle_matched = True
            search_from = b_end

        # C2: Generic 64-bit m_BundleSize scan + preceding uint32 m_Crc zeroing
        if not bundle_matched and old_size > 0:
            offset = 0
            while True:
                pos = data.find(old_size_le, offset)
                if pos == -1:
                    break
                if new_size != old_size:
                    struct.pack_into("<Q", data, pos, new_size)
                    modified = True
                if pos >= 4:
                    prev_u32 = struct.unpack_from("<I", data, pos - 4)[0]
                    if prev_u32 != 0 and prev_u32 > 0xFFFF:
                        struct.pack_into("<I", data, pos - 4, 0)
                        modified = True
                bundle_matched = True
                offset = pos + 8

        # C3: 32-bit m_BundleSize fallback if neither C1 nor C2 matched
        if not bundle_matched and old_size > 0:
            def _patch_u32_at(pos32: int) -> None:
                nonlocal modified, bundle_matched
                if new_size != old_size:
                    struct.pack_into("<I", data, pos32, new_size)
                    modified = True
                if pos32 >= 4:
                    prev_u32 = struct.unpack_from("<I", data, pos32 - 4)[0]
                    if prev_u32 != 0 and prev_u32 > 0xFFFF:
                        struct.pack_into("<I", data, pos32 - 4, 0)
                        modified = True
                bundle_matched = True

            b_idx = bytes(data).find(needle_ascii)
            if b_idx != -1:
                win_start = b_idx + len(needle_ascii)
                win_end = min(len(data), win_start + 256)
                pos32 = bytes(data).find(old_size_u32, win_start, win_end)
                while pos32 != -1:
                    _patch_u32_at(pos32)
                    pos32 = bytes(data).find(old_size_u32, pos32 + 4, win_end)

            if not bundle_matched and old_size != new_size:
                pos32 = data.find(old_size_u32)
                while pos32 != -1:
                    _patch_u32_at(pos32)
                    pos32 = data.find(old_size_u32, pos32 + 4)

    if modified:
        atomic_write_bytes(catalog_path, bytes(data))
    return modified


_PIGGYBACK_AUTONYMS: Dict[str, Tuple[str, ...]] = {
    "fr": ("Français", "French"),
    "de": ("Deutsch", "German"),
    "es": ("Español", "Spanish"),
    "it": ("Italiano", "Italian"),
    "pt-br": ("Português (Brasil)", "Português do Brasil", "Portuguese (Brazil)", "Portuguese (Brasil)", "Português"),
    "ptbr": ("Português (Brasil)", "Português do Brasil", "Portuguese (Brazil)", "Portuguese (Brasil)", "Português"),
    "pt": ("Português", "Portuguese"),
    "ru": ("Русский", "Russian"),
    "ja": ("日本語", "Japanese"),
    "ko": ("한국어", "Korean"),
    "zh": ("简体中文", "中文", "Chinese (Simplified)"),
    "zh-cn": ("简体中文", "中文", "Chinese (Simplified)"),
    "zh-hans": ("简体中文", "中文", "Chinese (Simplified)"),
    "zh-tw": ("繁體中文", "Chinese (Traditional)"),
    "zh-hk": ("繁體中文", "Chinese (Traditional)"),
    "zh-hant": ("繁體中文", "Chinese (Traditional)"),
    "sv": ("Svenska", "Swedish"),
    "uk": ("Українська", "Ukrainian"),
    "pl": ("Polski", "Polish"),
    "tr": ("Türkçe", "Turkish"),
    "nl": ("Nederlands", "Dutch"),
    "th": ("ไทย", "Thai"),
    "id": ("Bahasa Indonesia", "Indonesian"),
    "ar": ("العربية", "Arabic"),
}
_TARGET_DISPLAY_NAMES: Dict[str, str] = {
    "vi": "Tiếng Việt",
    "en": "English",
}


_ALL_KNOWN_LOCALE_DISPLAY_NAMES = frozenset(
    {name for names in _PIGGYBACK_AUTONYMS.values() for name in names} | {"English"}
)


def _rename_managed_dll_locale_label(
    root: Path,
    norm_slot: str,
    autonym_candidates: set[str],
    display_label: str,
    backup_cb,
) -> bool:
    """Safely patch hardcoded UI dropdown labels (e.g. MainMenuUIController.LOCALE_DROPDOWN) inside Assembly-CSharp.dll."""
    dll_candidates = list(root.glob("*_Data/Managed/Assembly-CSharp.dll"))
    if not dll_candidates:
        dll_candidates = [p for p in root.rglob("Assembly-CSharp.dll") if p.is_file()]
    if not dll_candidates:
        return False

    changed_any = False
    for dll_path in dll_candidates:
        try:
            raw_bytes = dll_path.read_bytes()
        except OSError:
            continue
        if len(raw_bytes) < 0x100 or raw_bytes[:2] != b"MZ":
            continue
        pe_off = struct.unpack_from("<I", raw_bytes, 0x3C)[0]
        if pe_off + 24 >= len(raw_bytes) or raw_bytes[pe_off : pe_off + 4] != b"PE\x00\x00":
            continue
        num_sec = struct.unpack_from("<H", raw_bytes, pe_off + 6)[0]
        opt_sz = struct.unpack_from("<H", raw_bytes, pe_off + 20)[0]
        magic = struct.unpack_from("<H", raw_bytes, pe_off + 24)[0]
        if magic not in (0x10B, 0x20B):
            continue
        dd_off = pe_off + 24 + (96 if magic == 0x10B else 112)
        if dd_off + 15 * 8 > len(raw_bytes):
            continue
        cli_rva, cli_sz = struct.unpack_from("<II", raw_bytes, dd_off + 14 * 8)
        if not cli_rva or cli_sz < 40:
            continue

        sec_off = pe_off + 24 + opt_sz
        sections = [struct.unpack_from("<IIII", raw_bytes, sec_off + i * 40 + 8) for i in range(num_sec)]

        def _rva2off(rva: int) -> Tuple[int, int, int, int, int]:
            for vsz, va, rsz, rptr in sections:
                if va <= rva < va + max(vsz, rsz):
                    return rptr + (rva - va), vsz, va, rsz, rptr
            return 0, 0, 0, 0, 0

        cli_off = _rva2off(cli_rva)[0]
        if not cli_off or cli_off + 40 > len(raw_bytes):
            continue
        sn_rva, sn_sz = struct.unpack_from("<II", raw_bytes, cli_off + 32)
        if sn_rva != 0 or sn_sz != 0:
            # Never alter strong-named assemblies
            continue

        meta_rva, meta_sz = struct.unpack_from("<II", raw_bytes, cli_off + 8)
        meta_off, cli_vsz, cli_va, cli_rsz, cli_rptr = _rva2off(meta_rva)
        if not meta_off or meta_off + meta_sz > len(raw_bytes) or raw_bytes[meta_off : meta_off + 4] != b"BSJB":
            continue

        ver_len = struct.unpack_from("<I", raw_bytes, meta_off + 12)[0]
        pos = meta_off + 16 + ((ver_len + 3) & ~3) + 2
        if pos + 2 > len(raw_bytes):
            continue
        n_streams = struct.unpack_from("<H", raw_bytes, pos)[0]
        pos += 2

        streams: Dict[str, Tuple[int, int, int]] = {}
        stream_list: List[Tuple[str, int, int, int]] = []
        for _ in range(n_streams):
            hdr_pos = pos
            soff, ssz = struct.unpack_from("<II", raw_bytes, pos)
            pos += 8
            end = raw_bytes.find(b"\x00", pos)
            if end == -1:
                break
            sname = raw_bytes[pos:end].decode("ascii", "ignore")
            pos = meta_off + (((end - meta_off) + 4) & ~3)
            streams[sname] = (soff, ssz, hdr_pos)
            stream_list.append((sname, soff, ssz, hdr_pos))

        if "#US" not in streams or "#~" not in streams or "#Strings" not in streams:
            continue

        us_soff, us_ssz, us_hdr_pos = streams["#US"]
        us_off = meta_off + us_soff
        us_end_file = us_off + us_ssz
        str_off = meta_off + streams["#Strings"][0]
        tbl_off = meta_off + streams["#~"][0]

        def _get_meta_str(idx: int) -> str:
            end_s = raw_bytes.find(b"\x00", str_off + idx)
            return raw_bytes[str_off + idx : end_s].decode("utf-8", "replace") if end_s != -1 else ""

        # Walk ECMA-335 #US stream entries to map offset -> string & find exact tail
        us_slice = raw_bytes[us_off:us_end_file]
        us_idx = 1
        us_used_end = 1
        token_to_text: Dict[int, str] = {}
        text_to_rel: Dict[str, int] = {}
        while us_idx < len(us_slice):
            b0 = us_slice[us_idx]
            if b0 == 0:
                if not any(us_slice[us_idx:]):
                    break
                us_idx += 1
                continue
            if (b0 & 0x80) == 0:
                ln, hdr_ln = b0, 1
            elif (b0 & 0xC0) == 0x80 and us_idx + 1 < len(us_slice):
                ln, hdr_ln = ((b0 & 0x3F) << 8) | us_slice[us_idx + 1], 2
            elif us_idx + 3 < len(us_slice):
                ln, hdr_ln = (
                    ((b0 & 0x1F) << 24) | (us_slice[us_idx + 1] << 16) | (us_slice[us_idx + 2] << 8) | us_slice[us_idx + 3],
                    4,
                )
            else:
                break
            val = us_slice[us_idx + hdr_ln : us_idx + hdr_ln + max(0, ln - 1)].decode("utf-16-le", "replace")
            token_to_text[0x70000000 | us_idx] = val
            text_to_rel.setdefault(val, us_idx)
            us_idx += hdr_ln + ln
            us_used_end = us_idx

        target_tokens = {tok for tok, txt in token_to_text.items() if txt in autonym_candidates}
        if not target_tokens:
            continue

        # Parse #~ table directory (tables 0x00..0x0A) to identify locale dropdown methods & fields cleanly
        heap_sizes = raw_bytes[tbl_off + 6]
        str_wide, guid_wide, blob_wide = (heap_sizes & 1) != 0, (heap_sizes & 2) != 0, (heap_sizes & 4) != 0
        valid_mask = struct.unpack_from("<Q", raw_bytes, tbl_off + 8)[0]
        rows: Dict[int, int] = {}
        p2 = tbl_off + 24
        for t in range(64):
            if (valid_mask >> t) & 1:
                rows[t] = struct.unpack_from("<I", raw_bytes, p2)[0]
                p2 += 4
        if 0x06 not in rows:
            continue

        sz_str = 4 if str_wide else 2
        sz_guid = 4 if guid_wide else 2
        sz_blob = 4 if blob_wide else 2

        def _sz_tbl(t: int) -> int:
            return 4 if rows.get(t, 0) >= 65536 else 2

        def _sz_coded(tbls: List[int], bits: int) -> int:
            mx = max((rows.get(t, 0) for t in tbls), default=0)
            return 4 if mx >= (1 << (16 - bits)) else 2

        tbl_sizes = {
            0x00: 2 + sz_str + sz_guid * 3,
            0x01: _sz_coded([0x00, 0x01, 0x1A, 0x23], 2) + sz_str * 2,
            0x02: 4 + sz_str * 2 + _sz_coded([0x02, 0x01, 0x1B], 2) + _sz_tbl(0x04) + _sz_tbl(0x06),
            0x03: _sz_tbl(0x04),
            0x04: 2 + sz_str + sz_blob,
            0x05: _sz_tbl(0x06),
            0x06: 4 + 2 + 2 + sz_str + sz_blob + _sz_tbl(0x08),
            0x07: _sz_tbl(0x08),
            0x08: 2 + 2 + sz_str,
            0x09: _sz_tbl(0x02) + _sz_coded([0x02, 0x01, 0x1B], 2),
            0x0A: _sz_coded([0x02, 0x01, 0x00, 0x06, 0x1A], 3) + sz_str + sz_blob,
        }
        offsets: Dict[int, int] = {}
        cur = p2
        for t in range(0x0B):
            if t in rows:
                offsets[t] = cur
                cur += rows[t] * tbl_sizes[t]

        m_off, m_sz = offsets[0x06], tbl_sizes[0x06]
        slot_code_tokens = {
            tok for tok, txt in token_to_text.items() if txt.strip().lower() in {norm_slot.lower(), f"{norm_slot.lower()} "}
        }
        patch_token_file_offsets: List[int] = []

        for midx in range(rows[0x06]):
            mr = m_off + midx * m_sz
            rva = struct.unpack_from("<I", raw_bytes, mr)[0]
            if not rva:
                continue
            mname = _get_meta_str(struct.unpack_from("<I" if str_wide else "<H", raw_bytes, mr + 8)[0])
            il_off = _rva2off(rva)[0]
            if not il_off or il_off + 12 > len(raw_bytes):
                continue
            hdr = raw_bytes[il_off]
            if (hdr & 3) == 2:
                code_start = il_off + 1
                code_len = hdr >> 2
            elif (hdr & 3) == 3:
                hdr_words = (struct.unpack_from("<H", raw_bytes, il_off)[0] >> 12) * 4
                code_start = il_off + hdr_words
                code_len = struct.unpack_from("<I", raw_bytes, il_off + 4)[0]
            else:
                continue
            if code_start + code_len > len(raw_bytes):
                continue

            code = raw_bytes[code_start : code_start + code_len]
            if not any((b"\x72" + struct.pack("<I", tk)) in code for tk in target_tokens):
                continue

            found_langs = set()
            has_locale_fld = False
            has_slot_pair = False
            method_targets: List[int] = []
            seen_code_recent = -999
            i = 0
            while i < len(code):
                op = code[i]
                if op == 0x72 and i + 5 <= len(code):
                    tok = struct.unpack_from("<I", code, i + 1)[0]
                    txt = token_to_text.get(tok, "")
                    if txt in _ALL_KNOWN_LOCALE_DISPLAY_NAMES:
                        found_langs.add(txt)
                    if tok in slot_code_tokens:
                        seen_code_recent = i
                    if tok in target_tokens:
                        method_targets.append(code_start + i + 1)
                        if i - seen_code_recent <= 24:
                            has_slot_pair = True
                    i += 5
                elif op in (0x7B, 0x7C, 0x7E, 0x7F, 0x80) and i + 5 <= len(code):
                    tok = struct.unpack_from("<I", code, i + 1)[0]
                    tt, ti = tok >> 24, (tok & 0xFFFFFF) - 1
                    if tt == 0x04 and 0x04 in offsets and ti < rows.get(0x04, 0):
                        fr = offsets[0x04] + ti * tbl_sizes[0x04]
                        fname = _get_meta_str(struct.unpack_from("<I" if str_wide else "<H", raw_bytes, fr + 2)[0]).lower()
                        if any(k in fname for k in ("locale", "lang", "dropdown")):
                            has_locale_fld = True
                    i += 5
                else:
                    i += 1

            m_lo = mname.lower()
            is_locale_method = (
                has_locale_fld
                or (mname == ".cctor" and (has_slot_pair or len(found_langs) >= 4))
                or any(k in m_lo for k in ("locale", "language", "dropdown"))
            )
            if is_locale_method and len(found_langs) >= 2 and method_targets:
                patch_token_file_offsets.extend(method_targets)

        if not patch_token_file_offsets:
            continue

        # Reuse existing #US entry for display_label if present, otherwise append cleanly
        dll = bytearray(raw_bytes)
        delta = 0
        if display_label in text_to_rel:
            new_tok = 0x70000000 | text_to_rel[display_label]
            for foff in patch_token_file_offsets:
                struct.pack_into("<I", dll, foff, new_tok)
        else:
            u16 = display_label.encode("utf-16-le")
            needs_trail = any(
                ord(ch) > 0x7F
                or ord(ch) in {
                    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
                    0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15,
                    0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D,
                    0x1E, 0x1F, 0x27, 0x2D, 0x7F,
                }
                for ch in display_label
            )
            entry_bytes = u16 + (b"\x01" if needs_trail else b"\x00")
            eb_len = len(entry_bytes)
            len_prefix = bytes([eb_len]) if eb_len < 0x80 else struct.pack(">H", 0x8000 | eb_len)
            new_entry = len_prefix + entry_bytes

            new_rel = us_used_end
            new_us_sz = (us_used_end + len(new_entry) + 3) & ~3
            delta = max(0, new_us_sz - us_ssz)
            new_tok = 0x70000000 | new_rel
            if any(soff > us_soff and soff < us_soff + us_ssz for _, soff, _, _ in stream_list):
                continue

            meta_end = meta_off + meta_sz
            sec_raw_end = cli_rptr + cli_rsz
            sec_virt_end = cli_rptr + cli_vsz
            can_grow_inplace = (
                delta == 0
                or (
                    meta_end + delta <= min(sec_raw_end, sec_virt_end)
                    and not any(b != 0 for b in dll[meta_end : meta_end + delta])
                )
            )

            for foff in patch_token_file_offsets:
                adj_off = foff + (delta if can_grow_inplace and foff >= us_end_file else 0)
                struct.pack_into("<I", dll, adj_off, new_tok)

            if delta == 0:
                dll[us_off + new_rel : us_off + new_rel + len(new_entry)] = new_entry
            elif can_grow_inplace:
                tail_chunk = bytes(dll[us_end_file:meta_end])
                dll[us_end_file + delta : meta_end + delta] = tail_chunk
                dll[us_end_file : us_end_file + delta] = b"\x00" * delta
                dll[us_off + new_rel : us_off + new_rel + len(new_entry)] = new_entry
                struct.pack_into("<I", dll, us_hdr_pos + 4, new_us_sz)
                for _, soff, _, hp in stream_list:
                    if soff > us_soff:
                        struct.pack_into("<I", dll, hp, soff + delta)
                struct.pack_into("<I", dll, cli_off + 12, meta_sz + delta)
            else:
                # Relocate BSJB Metadata into a dedicated .sakuga section at end of PE sections so IL & RVAs stay untouched
                bsjb = bytearray(dll[meta_off:meta_end])
                new_bsjb = bytearray(len(bsjb) + delta)
                new_bsjb[: us_soff + us_ssz] = bsjb[: us_soff + us_ssz]
                new_bsjb[us_soff + new_rel : us_soff + new_rel + len(entry_bytes) + len(len_prefix)] = new_entry
                new_bsjb[us_soff + us_ssz + delta :] = bsjb[us_soff + us_ssz :]
                struct.pack_into("<I", new_bsjb, us_hdr_pos - meta_off + 4, new_us_sz)
                for _, soff, _, hp in stream_list:
                    if soff > us_soff:
                        struct.pack_into("<I", new_bsjb, hp - meta_off, soff + delta)

                size_of_hdrs = struct.unpack_from("<I", dll, pe_off + 24 + 60)[0]
                sec_align = struct.unpack_from("<I", dll, pe_off + 24 + 32)[0]
                file_align = struct.unpack_from("<I", dll, pe_off + 24 + 36)[0]
                already_has_sakuga_sec = (
                    num_sec > 0
                    and dll[sec_off + (num_sec - 1) * 40 : sec_off + (num_sec - 1) * 40 + 8] == b".sakuga\x00"
                )
                hdr_end = sec_off + num_sec * 40
                new_pe_off = pe_off
                if not already_has_sakuga_sec and size_of_hdrs - hdr_end < 40:
                    if 0x40 + (hdr_end - pe_off) + 40 > size_of_hdrs:
                        continue
                    pe_blob = bytes(dll[pe_off:hdr_end])
                    dll[0x40 : 0x40 + len(pe_blob)] = pe_blob
                    dll[0x40 + len(pe_blob) : size_of_hdrs] = b"\x00" * (size_of_hdrs - (0x40 + len(pe_blob)))
                    struct.pack_into("<I", dll, 0x3C, 0x40)
                    new_pe_off = 0x40

                new_dd_off = new_pe_off + 24 + (96 if magic == 0x10B else 112)
                new_sec_off = new_pe_off + 24 + opt_sz
                last_vsz, last_va, last_rsz, last_rptr = sections[-1]
                new_sec_vsz = len(new_bsjb)
                new_sec_rsz = ((new_sec_vsz + file_align - 1) // file_align) * file_align
                if already_has_sakuga_sec:
                    new_sec_va = last_va
                    new_sec_rptr = last_rptr
                    sh_off = new_sec_off + (num_sec - 1) * 40
                    final_num_sec = num_sec
                else:
                    new_sec_va = ((last_va + max(last_vsz, last_rsz) + sec_align - 1) // sec_align) * sec_align
                    raw_base_end = max(rptr + rsz for _, _, rsz, rptr in sections)
                    new_sec_rptr = ((raw_base_end + file_align - 1) // file_align) * file_align
                    sh_off = new_sec_off + num_sec * 40
                    final_num_sec = num_sec + 1

                out_dll = bytearray(new_sec_rptr + new_sec_rsz)
                out_dll[: min(len(dll), new_sec_rptr)] = dll[: min(len(dll), new_sec_rptr)]
                out_dll[new_sec_rptr : new_sec_rptr + len(new_bsjb)] = new_bsjb
                out_dll[sh_off : sh_off + 8] = b".sakuga\x00"
                struct.pack_into(
                    "<IIIIIIHHI",
                    out_dll,
                    sh_off + 8,
                    new_sec_vsz,
                    new_sec_va,
                    new_sec_rsz,
                    new_sec_rptr,
                    0,
                    0,
                    0,
                    0,
                    0x40000040,
                )
                struct.pack_into("<H", out_dll, new_pe_off + 6, final_num_sec)
                new_size_of_image = ((new_sec_va + new_sec_vsz + sec_align - 1) // sec_align) * sec_align
                struct.pack_into("<I", out_dll, new_pe_off + 24 + 56, new_size_of_image)
                struct.pack_into("<II", out_dll, new_dd_off + 4 * 8, 0, 0)
                struct.pack_into("<II", out_dll, cli_off + 8, new_sec_va, len(new_bsjb))
                dll = out_dll

        if backup_cb:
            backup_cb([str(dll_path)], [])
        atomic_write_bytes(dll_path, bytes(dll))
        changed_any = True
    return changed_any


def _rename_piggyback_language_labels(
    root: Path,
    target_slot_code: str,
    target_lang: str,
    backup_cb,
    bundle_updates: List[Tuple[str, int, int]],
) -> bool:
    """Rename ANY piggybacked slot's Settings label (e.g. 'Français'/'German'/'Russian' -> 'Tiếng Việt')."""
    norm_slot = normalize_lang_token(target_slot_code)
    display_label = _TARGET_DISPLAY_NAMES.get((target_lang or "").lower())
    autonyms = (
        _PIGGYBACK_AUTONYMS.get((target_slot_code or "").lower())
        or _PIGGYBACK_AUTONYMS.get(norm_slot)
        or ((target_slot_code.upper(),) if target_slot_code else ())
    )
    if not display_label or not autonyms:
        return False

    native_label = autonyms[0]
    changed_any = False

    # 1. Preserve canonical English m_LocaleName inside localization-locales_assets_all.bundle!
    # Games like Content Warning sort AvailableLocales via LocaleName.Contains("English"/"French"/...)
    # and Steam language lookup checks LocaleName.ToLower(). Replacing m_LocaleName with "Tiếng Việt"
    # causes FindIndex -> -1, swapping French(fr) ahead of English(en) at index 0!
    locales_bundles = list(root.rglob("localization-locales_assets_all*.bundle"))
    # 2. Update native language dropdown row in source English bundle (e.g. StaticTexts_en: 'Français' -> 'Tiếng Việt')
    en_bundles = list(root.rglob("localization-string-tables-english(en)*.bundle"))

    to_backup = [str(p) for p in (locales_bundles + en_bundles) if p.exists()]
    if backup_cb and to_backup:
        backup_cb(to_backup, [])

    canonical_en_name = autonyms[1] if len(autonyms) > 1 else ""
    autonym_candidates = {a.strip() for a in autonyms if a and a.strip()}
    renamed_string_table = False

    for en_b in en_bundles:
        if not en_b.exists():
            continue
        old_sz = en_b.stat().st_size
        env = UnityPy.load(io.BytesIO(en_b.read_bytes()))
        edited = False
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                tree = obj.read_typetree()
            except Exception:
                continue
            if _is_unity_string_table(tree):
                for row in tree.get("m_TableData", []):
                    cur_val = str(row.get("m_Localized") or "").strip()
                    if cur_val in autonym_candidates or cur_val == display_label:
                        if cur_val != display_label:
                            row["m_Localized"] = display_label
                            obj.save_typetree(tree)
                            edited = True
                        renamed_string_table = True
                        break
        if edited:
            out_b = env.file.save(packer="lz4")
            del env
            gc.collect()
            atomic_write_bytes(en_b, out_b)
            bundle_updates.append((en_b.name, old_sz, len(out_b)))
            changed_any = True
        else:
            del env
            bundle_updates.append((en_b.name, old_sz, old_sz))

    for l_bundle in locales_bundles:
        if not l_bundle.exists():
            continue
        old_sz = l_bundle.stat().st_size
        env = UnityPy.load(io.BytesIO(l_bundle.read_bytes()))
        edited = False
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                tree = obj.read_typetree()
            except Exception:
                continue
            ident = tree.get("m_Identifier")
            raw_m_code = str(ident.get("m_Code") or "") if isinstance(ident, dict) else ""
            if raw_m_code and (
                raw_m_code.lower() == target_slot_code.lower()
                or normalize_lang_token(raw_m_code) == norm_slot
            ):
                cur_loc_name = str(tree.get("m_LocaleName") or "")
                if renamed_string_table:
                    # Preserve canonical English m_LocaleName when UI label lives in a StringTable (e.g. Content Warning)
                    canonical_target = str(tree.get("m_Name") or "").strip() or (
                        f"{canonical_en_name} ({raw_m_code or target_slot_code})" if canonical_en_name else cur_loc_name
                    )
                    if cur_loc_name == display_label or (canonical_en_name and canonical_en_name.lower() not in cur_loc_name.lower()):
                        tree["m_LocaleName"] = canonical_target
                        obj.save_typetree(tree)
                        edited = True
                else:
                    # Games like Probably Stolen read dropdown labels directly from Locale.m_LocaleName
                    desired_locale_name = f"{display_label} ({raw_m_code or target_slot_code})"
                    if cur_loc_name != desired_locale_name:
                        tree["m_LocaleName"] = desired_locale_name
                        obj.save_typetree(tree)
                        edited = True
            elif raw_m_code:
                cur_loc_name = str(tree.get("m_LocaleName") or "")
                canonical_other = str(tree.get("m_Name") or "").strip()
                if cur_loc_name.startswith(display_label) and canonical_other and cur_loc_name != canonical_other:
                    tree["m_LocaleName"] = canonical_other
                    obj.save_typetree(tree)
                    edited = True
        if edited:
            out_b = env.file.save(packer="lz4")
            del env
            gc.collect()
            atomic_write_bytes(l_bundle, out_b)
            bundle_updates.append((l_bundle.name, old_sz, len(out_b)))
            changed_any = True
        else:
            del env
            bundle_updates.append((l_bundle.name, old_sz, old_sz))

    if _rename_managed_dll_locale_label(
        root=root,
        norm_slot=norm_slot,
        autonym_candidates=autonym_candidates,
        display_label=display_label,
        backup_cb=backup_cb,
    ):
        changed_any = True

    return changed_any


def apply_unity_translations(
    game_dir: str | Path,
    source_entries: List[LocEntry],
    translations: Dict[str, str],
    inject_slot_info: Dict[str, Any],
    backup_cb=None,
    target_lang: str = "vi",
    source_slot_code: str = "",
) -> Dict[str, Any]:
    """Write translated strings into inject_slot_info (same slot OR piggybacked slot) & sync catalog.bin."""
    root = Path(game_dir)
    target_files = [Path(p) for p in inject_slot_info.get("files", [])]
    if not target_files:
        return {"ok": False, "updated": 0, "error": "Slot Unity không có file đích."}

    target_slot_code = inject_slot_info["slot_id"]
    target_lang_idx = inject_slot_info.get("lang_index")
    src_code = source_slot_code or (source_entries[0].lang if source_entries else "")
    norm_slot_for_label = normalize_lang_token(target_slot_code)
    is_piggyback = bool(
        norm_slot_for_label != normalize_lang_token(target_lang or "")
        and (target_lang or "").lower() in _TARGET_DISPLAY_NAMES
    )
    display_label = _TARGET_DISPLAY_NAMES.get((target_lang or "").lower(), "")
    autonym_set = set(
        _PIGGYBACK_AUTONYMS.get(target_slot_code.lower())
        or _PIGGYBACK_AUTONYMS.get(norm_slot_for_label)
        or ()
    )

    # Build lookup by (table_name, row_id) and by entry key
    by_table_row: Dict[Tuple[str, Any], str] = {}
    by_key: Dict[str, str] = {}
    for e in source_entries:
        tr = translations.get(e.text) or translations.get(e.entry_id)
        if is_piggyback and e.text.strip() in autonym_set:
            tr = display_label
        if not tr:
            continue
        by_key[e.key] = tr
        tname = e.location_meta.get("table_name", "")
        if "row_id" in e.location_meta:
            # Strip language suffix from StringTable asset name (e.g. Dialogue_en -> Dialogue)
            base_tname = re.sub(r"[_-](?:en|fr|es|de|pl|pt|it|ja|ko|ru|zh|vi)(?:-[A-Za-z]+)?$", "", tname, flags=re.IGNORECASE)
            by_table_row[(base_tname, e.location_meta["row_id"])] = tr
            by_table_row[(tname, e.location_meta["row_id"])] = tr

    catalog_path = _find_catalog_bin(root)
    files_to_backup = [str(f) for f in target_files]
    if catalog_path:
        files_to_backup.append(str(catalog_path))
    if backup_cb:
        backup_cb(files_to_backup, [])

    updated_count = 0
    bundle_updates: List[Tuple[str, int, int]] = []

    for tf in target_files:
        if not tf.exists():
            continue
        old_size = tf.stat().st_size
        env = UnityPy.load(io.BytesIO(tf.read_bytes()))
        changed_file = False

        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                tree = obj.read_typetree()
            except Exception:
                continue

            if _is_i2_language_source(tree):
                src = tree.get("mSource", tree)
                langs = src.get("mLanguages", [])
                terms = src.get("mTerms", [])
                idx = target_lang_idx
                if idx is None:
                    for i, l in enumerate(langs):
                        if normalize_lang_token(l.get("Code") or l.get("Name") or "") == target_slot_code:
                            idx = i
                            break
                if idx is None:
                    continue
                changed_obj = False
                for term in terms:
                    k = str(term.get("Term") or "")
                    tr = by_key.get(k)
                    if not tr:
                        continue
                    vals = term.get("Languages", [])
                    while len(vals) <= idx:
                        vals.append("")
                    vals[idx] = tr
                    term["Languages"] = vals
                    updated_count += 1
                    changed_obj = True
                if changed_obj:
                    obj.save_typetree(tree)
                    changed_file = True

            elif _is_unity_string_table(tree):
                tname = str(tree.get("m_Name") or tf.stem)
                base_tname = re.sub(r"[_-](?:en|fr|es|de|pl|pt|it|ja|ko|ru|zh|vi)(?:-[A-Za-z]+)?$", "", tname, flags=re.IGNORECASE)
                rows = tree.get("m_TableData", [])
                changed_obj = False
                for r_idx, row in enumerate(rows):
                    row_id = row.get("m_Id", r_idx)
                    tr = by_table_row.get((base_tname, row_id)) or by_table_row.get((tname, row_id))
                    if tr:
                        row["m_Localized"] = tr
                        updated_count += 1
                        changed_obj = True
                if changed_obj:
                    obj.save_typetree(tree)
                    changed_file = True

        if changed_file:
            out_bytes = env.file.save(packer="lz4")
            del env
            gc.collect()
            atomic_write_bytes(tf, out_bytes)
            new_size = len(out_bytes)
            bundle_updates.append((tf.name, old_size, new_size))
        else:
            del env
            bundle_updates.append((tf.name, old_size, old_size))

    label_renamed = False
    if is_piggyback:
        label_renamed = _rename_piggyback_language_labels(
            root=root,
            target_slot_code=target_slot_code,
            target_lang=target_lang,
            backup_cb=backup_cb,
            bundle_updates=bundle_updates,
        )

    catalog_synced = False
    if catalog_path and bundle_updates:
        catalog_synced = _sync_addressables_catalog(catalog_path, bundle_updates)

    return {
        "ok": True,
        "updated": updated_count,
        "bundles_modified": len(bundle_updates),
        "catalog_synced": catalog_synced,
        "language_label_renamed": label_renamed,
    }
