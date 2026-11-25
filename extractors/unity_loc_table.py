"""Unity Native Localization Extractor & Writer (StringTable / I2Languages / TextAsset + Binary IL2CPP Fallback + Addressables sync)."""

from __future__ import annotations

import base64
import csv
import gc
import io
import json
import logging
import os
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import polib
import UnityPy
from UnityPy.files import BundleFile

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

_AA_EXTRA_HINT_TOKENS = (
    "i2",
    "text",
    "string",
    "dialog",
    "sub",
    "table",
    "defaultlocalgroup",
    "shared",
    "ui",
    "menu",
    "common",
    "data",
)

_NON_AA_BUNDLE_EXTS = {".bundle", ".unity3d", ".ab", ".assetbundle"}
_BUNDLE_MAGICS = (b"UnityFS", b"UnityRaw", b"UnityWeb")

LANG_NAME_TO_CODE = {
    "english": "en",
    "englishus": "en",
    "englishuk": "en",
    "englishgb": "en",
    "vietnamese": "vi",
    "tiengviet": "vi",
    "french": "fr",
    "frenchcanada": "fr",
    "francais": "fr",
    "spanish": "es",
    "spanishlatinamerica": "es",
    "espanol": "es",
    "german": "de",
    "deutsch": "de",
    "polish": "pl",
    "polski": "pl",
    "portuguese": "pt",
    "portugues": "pt",
    "portuguesebrazil": "pt-BR",
    "portuguesdobrasil": "pt-BR",
    "brazilian": "pt-BR",
    "ptbr": "pt-BR",
    "italian": "it",
    "italiano": "it",
    "japanese": "ja",
    "korean": "ko",
    "russian": "ru",
    "turkish": "tr",
    "ukrainian": "uk",
    "swedish": "sv",
    "dutch": "nl",
    "indonesian": "id",
    "thai": "th",
    "arabic": "ar",
    "czech": "cs",
    "danish": "da",
    "finnish": "fi",
    "norwegian": "nb",
    "hungarian": "hu",
    "romanian": "ro",
    "greek": "el",
    "chinese": "zh",
    "schinese": "zh",
    "tchinese": "zh-TW",
    "simplifiedchinese": "zh",
    "chinesesimplified": "zh",
    "traditionalchinese": "zh-TW",
    "chinesetraditional": "zh-TW",
    "zhtw": "zh-TW",
    "zhcn": "zh",
}

NATIVE_AUTONYM_TO_CODE = {
    "english": "en",
    "tiếng việt": "vi",
    "français": "fr",
    "francais": "fr",
    "deutsch": "de",
    "español": "es",
    "espanol": "es",
    "italiano": "it",
    "polski": "pl",
    "português": "pt",
    "português (brasil)": "pt-BR",
    "português do brasil": "pt-BR",
    "日本語": "ja",
    "한국어": "ko",
    "简体中文": "zh",
    "中文": "zh",
    "繁體中文": "zh-TW",
    "русский": "ru",
    "türkçe": "tr",
    "українська": "uk",
    "nederlands": "nl",
    "svenska": "sv",
    "bahasa indonesia": "id",
    "ไทย": "th",
    "العربية": "ar",
    "čeština": "cs",
    "dansk": "da",
    "suomi": "fi",
    "norsk": "nb",
    "magyar": "hu",
    "română": "ro",
    "ελληνικά": "el",
}

KNOWN_LANG_CODES = frozenset(
    {
        "en", "vi", "fr", "es", "de", "pl", "pt", "pt-BR", "it",
        "ja", "ko", "ru", "zh", "zh-TW", "tr", "uk", "sv", "nl",
        "id", "th", "ar", "cs", "da", "fi", "nb", "hu", "ro", "el",
    }
)

_CSV_NON_LANG_HEADERS = frozenset(
    {
        "key", "id", "string_id", "comment", "notes", "note", "context",
        "description", "category", "speaker", "character", "type", "desc",
        "group", "tag", "tags", "section", "file", "index", "no", "num",
        "version", "status", "max_length", "limit",
    }
)


def is_known_lang_code(code: str) -> bool:
    return code in KNOWN_LANG_CODES


def _canon_code(code: str) -> str:
    c = code.replace("_", "-").strip()
    low = c.lower()
    if low in ("pt-br", "ptbr"):
        return "pt-BR"
    if low in ("zh-tw", "zhtw", "zh-hant", "zh-hk"):
        return "zh-TW"
    if low in ("zh-cn", "zhcn", "zh-hans"):
        return "zh"
    if low in ("eng",):
        return "en"
    if low in ("fre", "fra"):
        return "fr"
    if low in ("ger", "deu"):
        return "de"
    if low in ("spa",):
        return "es"
    if low in ("ita",):
        return "it"
    if low in ("pol",):
        return "pl"
    if low in ("por",):
        return "pt"
    if low in ("rus",):
        return "ru"
    if low in ("jpn", "jp"):
        return "ja"
    if low in ("kor",):
        return "ko"
    if low in ("vie", "vn"):
        return "vi"
    if low in ("tur",):
        return "tr"
    if low in ("ukr",):
        return "uk"
    if low in ("swe",):
        return "sv"
    if low in ("nld",):
        return "nl"
    if len(low) > 2 and "-" in low:
        return low.split("-")[0]
    return low


def normalize_lang_token(raw: str) -> str:
    if not raw:
        return "unknown"
    s = raw.strip()
    low_s = s.lower()
    if low_s in NATIVE_AUTONYM_TO_CODE:
        return NATIVE_AUTONYM_TO_CODE[low_s]

    bracket = re.search(
        r"[\(\[]([a-zA-Z]{2,3}(?:[-_][a-zA-Z0-9]{2,4})?|ptbr|zhtw|zhcn)[\)\]]",
        s,
        re.IGNORECASE,
    )
    if bracket:
        cand = _canon_code(bracket.group(1))
        if is_known_lang_code(cand):
            return cand

    # Compact name match before aggressive stripping (captures "Chinese (Simplified)", "English (US)", etc.)
    low_compact_full = re.sub(r"[^a-z]", "", low_s)
    for k in sorted(LANG_NAME_TO_CODE, key=len, reverse=True):
        if k in low_compact_full:
            return LANG_NAME_TO_CODE[k]

    # Remove suffixes like _assets_all
    cleaned = re.sub(r"(localization|string[-_]?tables?|language|source|assets|all)", "", s, flags=re.IGNORECASE)
    cleaned_alpha = re.sub(r"[^a-zA-Z0-9_-]", "", cleaned).strip("_-")
    if len(cleaned_alpha) in (2, 3) and cleaned_alpha.isalpha():
        return _canon_code(cleaned_alpha)
    low_compact = re.sub(r"[^a-z]", "", cleaned.lower())
    for k in sorted(LANG_NAME_TO_CODE, key=len, reverse=True):
        if k in low_compact:
            return LANG_NAME_TO_CODE[k]
    if re.match(r"^[a-zA-Z]{2}(?:[-_][A-Za-z0-9]{2,4})?$", s):
        return _canon_code(s)
    return low_s


def sniff_csv_headers_from_text(text: str, default_delim: str = ",") -> Optional[Tuple[str, List[str]]]:
    """Detect delimiter and >= 2 valid language columns in CSV/TSV text."""
    delims = ["\t", ",", ";"] if default_delim == "\t" else [default_delim, "," if default_delim != "," else ";", ";", "\t"]
    seen_delims: List[str] = []
    for d in delims:
        if d not in seen_delims:
            seen_delims.append(d)

    for delim in seen_delims:
        try:
            reader = csv.reader(io.StringIO(text[:32768]), delimiter=delim)
            header = next(reader, None)
        except Exception:
            continue
        if not header or len(header) < 2:
            continue
        lang_cols: List[str] = []
        for col in header:
            raw_col = col.strip()
            if not raw_col:
                continue
            c_clean = raw_col.lower().replace("_", "-")
            if c_clean in _CSV_NON_LANG_HEADERS:
                continue
            norm = normalize_lang_token(raw_col)
            if is_known_lang_code(norm):
                lang_cols.append(raw_col)
        if len(lang_cols) >= 2:
            return delim, lang_cols
    return None


def _has_bundle_magic(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
        return any(head.startswith(m) for m in _BUNDLE_MAGICS)
    except OSError:
        return False


def _sharedassets_sort_key(path: Path) -> Tuple[int, str]:
    m = re.search(r"sharedassets(\d+)\.assets$", path.name, re.IGNORECASE)
    return (int(m.group(1)) if m else 999999, path.name.lower())


def find_unity_loc_candidates(game_dir: Path) -> Dict[str, List[Path]]:
    """Locate native localization bundles (Addressables & StreamingAssets), resources.assets, and sharedassets*.assets."""
    bundles: List[Path] = []
    aa_other_bundles: List[Path] = []
    resource_assets: List[Path] = []
    data_dirs = sorted([p for p in game_dir.iterdir() if p.is_dir() and p.name.endswith("_Data")])

    seen_bundles: set[Path] = set()
    for dd in data_dirs:
        sa_dir = dd / "StreamingAssets"
        aa_dir = sa_dir / "aa"

        # 1. Addressables bundles under StreamingAssets/aa/**
        if aa_dir.exists():
            for pattern in UNITY_LOC_FILE_PATTERNS:
                for bf in aa_dir.rglob(pattern):
                    if bf.is_file() and bf not in seen_bundles:
                        seen_bundles.add(bf)
                        bundles.append(bf)
            for bf in aa_dir.rglob("*.bundle"):
                if bf in seen_bundles or not bf.is_file():
                    continue
                low = bf.name.lower()
                is_strong = "loc" in low or "lang" in low or "i18n" in low or re.search(r"\([a-z]{2}(?:-[a-z]{2})?\)", low)
                if is_strong:
                    seen_bundles.add(bf)
                    bundles.append(bf)
                elif any(tok in low for tok in _AA_EXTRA_HINT_TOKENS):
                    try:
                        if bf.stat().st_size <= 25 * 1024 * 1024:
                            seen_bundles.add(bf)
                            bundles.append(bf)
                    except OSError:
                        pass
                else:
                    try:
                        if bf.stat().st_size <= 30 * 1024 * 1024:
                            aa_other_bundles.append(bf)
                    except OSError:
                        pass

        # 2. Non-Addressables AssetBundles anywhere inside StreamingAssets/**
        if sa_dir.exists():
            for bf in sa_dir.rglob("*"):
                if not bf.is_file() or bf in seen_bundles:
                    continue
                if aa_dir in bf.parents:
                    continue
                ext = bf.suffix.lower()
                try:
                    sz = bf.stat().st_size
                except OSError:
                    continue
                if sz < 128 or sz > 100 * 1024 * 1024:
                    continue
                if ext in _NON_AA_BUNDLE_EXTS:
                    seen_bundles.add(bf)
                    bundles.append(bf)
                elif not ext:
                    rel_low = str(bf.relative_to(sa_dir)).lower()
                    if any(tok in rel_low for tok in ("loc", "lang", "i2", "text", "string", "i18n", "table", "dialogue", "sub")):
                        if _has_bundle_magic(bf):
                            seen_bundles.add(bf)
                            bundles.append(bf)

        # 3. resources.assets + startup sharedassets*.assets (up to first 12, <= 120 MB each)
        res_file = dd / "resources.assets"
        if res_file.exists() and res_file.is_file():
            resource_assets.append(res_file)

        shared_files = sorted(
            [p for p in dd.glob("sharedassets*.assets") if p.is_file()],
            key=_sharedassets_sort_key,
        )[:12]
        for sf in shared_files:
            try:
                if sf.stat().st_size <= 120 * 1024 * 1024:
                    resource_assets.append(sf)
            except OSError:
                pass

    aa_other_bundles.sort(key=lambda p: p.stat().st_size if p.exists() else 0)
    return {
        "bundles": sorted(bundles),
        "aa_other_bundles": aa_other_bundles[:25],
        "resources": resource_assets,
        "data_dirs": data_dirs,
    }


def _is_i2_language_source(tree: Dict[str, Any]) -> bool:
    return isinstance(tree, dict) and ("mSource" in tree or ("mTerms" in tree and "mLanguages" in tree))


def _is_unity_string_table(tree: Dict[str, Any]) -> bool:
    return isinstance(tree, dict) and "m_TableData" in tree and isinstance(tree.get("m_TableData"), list)


def _index_env_monoscripts(env: UnityPy.Environment, idx: Dict[Tuple[str, int], Tuple[str, str]]) -> None:
    for obj in env.objects:
        if obj.type.name != "MonoScript":
            continue
        try:
            ms = obj.read()
            cls = getattr(ms, "m_ClassName", "") or ""
            mname = getattr(ms, "m_Name", "") or ""
            fname = Path(getattr(obj.assets_file, "name", "") or "").name.lower()
            idx[(fname, obj.path_id)] = (cls, mname)
            idx[("", obj.path_id)] = (cls, mname)
        except Exception:
            continue


def _build_monoscript_index(data_dirs: List[Path]) -> Dict[Tuple[str, int], Tuple[str, str]]:
    """Index MonoScript definitions in globalgamemanagers.assets for fast external m_Script resolution."""
    idx: Dict[Tuple[str, int], Tuple[str, str]] = {}
    for dd in data_dirs:
        for ggm_name in ("globalgamemanagers.assets", "globalgamemanagers"):
            ggm = dd / ggm_name
            if not ggm.exists() or not ggm.is_file():
                continue
            try:
                if ggm.stat().st_size > 40 * 1024 * 1024:
                    continue
                env = UnityPy.load(str(ggm))
                _index_env_monoscripts(env, idx)
            except Exception as exc:
                logger.debug("Could not index MonoScripts from %s: %s", ggm, exc)
    return idx


def _resolve_monobehaviour_identity(
    obj: Any,
    local_scripts: Dict[Tuple[str, int], Tuple[str, str]],
    global_scripts: Dict[Tuple[str, int], Tuple[str, str]],
) -> Tuple[str, str, str, str]:
    """Return (mb_name, script_class, script_name, game_object_name) even on prefab-attached MonoBehaviours."""
    mb_name = ""
    script_class = ""
    script_name = ""
    go_name = ""

    data = None
    try:
        data = obj.read()
        mb_name = getattr(data, "m_Name", "") or ""
    except Exception:
        data = None

    sf_ver = getattr(getattr(obj.assets_file, "header", None), "version", 22)
    externals = getattr(obj.assets_file, "externals", []) or []
    cur_file = Path(getattr(obj.assets_file, "name", "") or "").name.lower()

    go_fid, go_pid = 0, 0
    sc_fid, sc_pid = 0, 0

    if data is not None:
        m_go = getattr(data, "m_GameObject", None)
        if m_go is not None:
            go_fid = getattr(m_go, "file_id", 0) or 0
            go_pid = getattr(m_go, "path_id", 0) or 0
            if go_fid == 0 and go_pid != 0:
                try:
                    go = m_go.read()
                    go_name = getattr(go, "m_Name", "") or ""
                except Exception:
                    pass
        m_sc = getattr(data, "m_Script", None)
        if m_sc is not None:
            sc_fid = getattr(m_sc, "file_id", 0) or 0
            sc_pid = getattr(m_sc, "path_id", 0) or 0
            if sc_fid == 0 and sc_pid != 0:
                try:
                    sc = m_sc.read()
                    script_class = getattr(sc, "m_ClassName", "") or ""
                    script_name = getattr(sc, "m_Name", "") or ""
                except Exception:
                    pass

    # Fallback raw header parse if stripped IL2CPP MonoBehaviour lacked TypeTree for base fields
    if sc_pid == 0 and go_pid == 0 and not mb_name:
        try:
            raw = obj.get_raw_data()
            hdr = _skip_mb_header(raw, sf_ver)
            if hdr:
                mb_name = hdr["mb_name"]
                go_fid, go_pid = hdr["go_fid"], hdr["go_pid"]
                sc_fid, sc_pid = hdr["sc_fid"], hdr["sc_pid"]
                if go_fid == 0 and go_pid != 0 and go_pid in obj.assets_file.objects:
                    try:
                        go_obj = obj.assets_file.objects[go_pid].read()
                        go_name = getattr(go_obj, "m_Name", "") or ""
                    except Exception:
                        pass
        except Exception:
            pass

    if not script_class and sc_pid != 0:
        ext_name = cur_file
        if sc_fid > 0 and sc_fid - 1 < len(externals):
            ext_path = getattr(externals[sc_fid - 1], "path", "") or getattr(externals[sc_fid - 1], "name", "") or ""
            ext_name = Path(str(ext_path).replace("\\", "/")).name.lower()
        hit = (
            local_scripts.get((ext_name, sc_pid))
            or global_scripts.get((ext_name, sc_pid))
            or local_scripts.get(("", sc_pid))
            or global_scripts.get(("", sc_pid))
        )
        if hit:
            script_class, script_name = hit

    return mb_name, script_class, script_name, go_name


# ---------------------------------------------------------------------------
# Stripped TypeTree Raw Binary Fallback Helpers (Unity IL2CPP)
# ---------------------------------------------------------------------------

def _align4(pos: int) -> int:
    return (pos + 3) & ~3


def _read_unity_str(buf: bytes, pos: int, max_bytes: int = 1_000_000) -> Optional[Tuple[str, int]]:
    if pos < 0 or pos + 4 > len(buf):
        return None
    ln = struct.unpack_from("<i", buf, pos)[0]
    if ln < 0 or ln > max_bytes or pos + 4 + ln > len(buf):
        return None
    raw_slice = buf[pos + 4 : pos + 4 + ln]
    try:
        text = raw_slice.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    return text, _align4(pos + 4 + ln)


def _write_unity_str(text: str) -> bytes:
    raw = text.encode("utf-8")
    pad_len = (-len(raw)) % 4
    return struct.pack("<i", len(raw)) + raw + (b"\x00" * pad_len)


def _skip_mb_header(buf: bytes, sf_version: int) -> Optional[Dict[str, Any]]:
    pptr_sz = 12 if sf_version >= 14 else 8
    min_sz = pptr_sz + 4 + pptr_sz + 4
    if len(buf) < min_sz:
        return None
    pos = 0
    if sf_version >= 14:
        go_fid, go_pid = struct.unpack_from("<iq", buf, pos)
        sc_fid, sc_pid = struct.unpack_from("<iq", buf, pos + pptr_sz + 4)
    else:
        go_fid, go_pid = struct.unpack_from("<ii", buf, pos)
        sc_fid, sc_pid = struct.unpack_from("<ii", buf, pos + pptr_sz + 4)
    pos += pptr_sz + 4 + pptr_sz
    name_res = _read_unity_str(buf, pos, max_bytes=4096)
    if name_res is None:
        return None
    mb_name, end_pos = name_res
    return {
        "mb_name": mb_name,
        "go_fid": go_fid,
        "go_pid": go_pid,
        "sc_fid": sc_fid,
        "sc_pid": sc_pid,
        "end_pos": end_pos,
        "pptr_sz": pptr_sz,
    }


def _parse_binary_string_table(raw: bytes, sf_version: int) -> Optional[Dict[str, Any]]:
    """Parse stripped-TypeTree Unity StringTable binary payload cleanly."""
    hdr = _skip_mb_header(raw, sf_version)
    if not hdr:
        return None
    pos = hdr["end_pos"]
    pptr_sz = hdr["pptr_sz"]

    loc_res = _read_unity_str(raw, pos, max_bytes=64)
    if loc_res is None:
        return None
    loc_code, pos_after_loc = loc_res
    if not loc_code or not re.match(r"^[A-Za-z0-9_-]{1,24}$", loc_code):
        return None
    if not is_known_lang_code(normalize_lang_token(loc_code)):
        return None
    if pos_after_loc + pptr_sz + 4 > len(raw):
        return None

    shared_bytes = raw[pos_after_loc : pos_after_loc + pptr_sz]
    after_shared = pos_after_loc + pptr_sz

    # Try Unity Localization v1.4+ (with table-level MetadataCollection) first, then v1.0..v1.3 (without)
    candidate_starts: List[Tuple[bytes, int]] = []
    if after_shared + 4 <= len(raw):
        tmeta_cnt = struct.unpack_from("<i", raw, after_shared)[0]
        if 0 <= tmeta_cnt <= 32 and after_shared + 4 + tmeta_cnt * 8 + 4 <= len(raw):
            tmeta_end = after_shared + 4 + tmeta_cnt * 8
            candidate_starts.append((raw[after_shared:tmeta_end], tmeta_end))
    candidate_starts.append((b"", after_shared))

    for table_meta_bytes, tbl_off in candidate_starts:
        if tbl_off + 4 > len(raw):
            continue
        row_count = struct.unpack_from("<i", raw, tbl_off)[0]
        if row_count < 1 or row_count > 200_000:
            continue

        cur = tbl_off + 4
        rows: List[Dict[str, Any]] = []
        valid = True
        for _ in range(row_count):
            if cur + 12 > len(raw):
                valid = False
                break
            row_id = struct.unpack_from("<q", raw, cur)[0]
            cur += 8
            str_res = _read_unity_str(raw, cur)
            if str_res is None:
                valid = False
                break
            localized_text, cur = str_res
            if cur + 4 > len(raw):
                valid = False
                break
            meta_cnt = struct.unpack_from("<i", raw, cur)[0]
            if meta_cnt < 0 or meta_cnt > 64 or cur + 4 + meta_cnt * 8 > len(raw):
                valid = False
                break
            meta_bytes = raw[cur : cur + 4 + meta_cnt * 8]
            cur += 4 + meta_cnt * 8
            rows.append({"m_Id": row_id, "m_Localized": localized_text, "meta_bytes": meta_bytes})

        if not valid:
            continue
        remaining = len(raw) - cur
        if remaining == 0 or remaining <= 64 or (remaining >= 4 and struct.unpack_from("<i", raw, cur)[0] in (0, 1, 2)):
            return {
                "m_Name": hdr["mb_name"],
                "m_LocaleId": {"m_Code": loc_code},
                "prefix_bytes": raw[:pos_after_loc] + shared_bytes + table_meta_bytes,
                "rows": rows,
                "suffix_bytes": raw[cur:],
            }
    return None


def _patch_binary_string_table(
    raw: bytes,
    sf_version: int,
    updates_by_row_id: Dict[int, str],
) -> Tuple[bytes, int]:
    parsed = _parse_binary_string_table(raw, sf_version)
    if not parsed:
        return raw, 0
    changed = 0
    out = bytearray(parsed["prefix_bytes"])
    rows = parsed["rows"]
    out.extend(struct.pack("<i", len(rows)))
    for row in rows:
        rid = row["m_Id"]
        val = row["m_Localized"]
        if rid in updates_by_row_id:
            val = updates_by_row_id[rid]
            changed += 1
        out.extend(struct.pack("<q", rid))
        out.extend(_write_unity_str(val))
        out.extend(row["meta_bytes"])
    out.extend(parsed["suffix_bytes"])
    return bytes(out), changed


def _try_parse_i2_languages_array(raw: bytes, pos: int) -> Optional[Tuple[List[Dict[str, Any]], int, int]]:
    """Try parsing I2 LanguageSourceData.mLanguages at byte offset `pos`."""
    if pos + 4 > len(raw):
        return None
    lang_count = struct.unpack_from("<i", raw, pos)[0]
    if not (1 <= lang_count <= 64):
        return None
    for flags_step in (4, 1):
        cur = pos + 4
        langs: List[Dict[str, Any]] = []
        valid = True
        recognized = 0
        for _ in range(lang_count):
            name_res = _read_unity_str(raw, cur, max_bytes=128)
            if name_res is None:
                valid = False
                break
            lname, cur = name_res
            code_res = _read_unity_str(raw, cur, max_bytes=64)
            if code_res is None:
                valid = False
                break
            lcode, cur = code_res
            if cur + flags_step > len(raw):
                valid = False
                break
            flags_bytes = raw[cur : cur + flags_step]
            cur += flags_step
            if not lname and not lcode:
                valid = False
                break
            if any(ord(c) < 0x20 for c in lname + lcode):
                valid = False
                break
            norm = normalize_lang_token(lcode or lname)
            if is_known_lang_code(norm):
                recognized += 1
            langs.append({"Name": lname, "Code": lcode, "flags_bytes": flags_bytes})
        if valid and recognized >= 1:
            return langs, cur, flags_step
    return None


def _try_parse_i2_terms_array(
    raw: bytes,
    pos: int,
    lang_count: int,
    max_end: int,
) -> Optional[Tuple[List[Dict[str, Any]], int, bool]]:
    """Try parsing I2 LanguageSourceData.mTerms array starting at `pos`."""
    if pos + 4 > max_end:
        return None
    term_count = struct.unpack_from("<i", raw, pos)[0]
    if not (1 <= term_count <= 200_000):
        return None
    for has_touch in (True, False):
        cur = pos + 4
        terms: List[Dict[str, Any]] = []
        valid = True
        for _ in range(term_count):
            t_res = _read_unity_str(raw, cur, max_bytes=4096)
            if t_res is None:
                valid = False
                break
            term_key, cur = t_res
            if cur + 4 > max_end:
                valid = False
                break
            term_type = struct.unpack_from("<i", raw, cur)[0]
            if not (0 <= term_type <= 32):
                valid = False
                break
            cur += 4
            d_res = _read_unity_str(raw, cur, max_bytes=16384)
            if d_res is None:
                valid = False
                break
            desc, cur = d_res
            if cur + 4 > max_end:
                valid = False
                break
            arr_len = struct.unpack_from("<i", raw, cur)[0]
            if arr_len < 0 or arr_len > 64 or abs(arr_len - lang_count) > 2:
                valid = False
                break
            cur += 4
            vals: List[str] = []
            for _j in range(arr_len):
                v_res = _read_unity_str(raw, cur)
                if v_res is None:
                    valid = False
                    break
                vals.append(v_res[0])
                cur = v_res[1]
            if not valid or cur + 4 > max_end:
                valid = False
                break
            flag_cnt = struct.unpack_from("<i", raw, cur)[0]
            if flag_cnt < 0 or flag_cnt > 64:
                valid = False
                break
            flag_end = _align4(cur + 4 + flag_cnt)
            if flag_end > max_end:
                valid = False
                break
            flags_blob = raw[cur:flag_end]
            cur = flag_end

            touch_blob = b""
            if has_touch:
                if cur + 4 > max_end:
                    valid = False
                    break
                t_cnt = struct.unpack_from("<i", raw, cur)[0]
                if t_cnt < 0 or t_cnt > 64:
                    valid = False
                    break
                t_start = cur
                cur += 4
                for _j in range(t_cnt):
                    tv_res = _read_unity_str(raw, cur)
                    if tv_res is None:
                        valid = False
                        break
                    cur = tv_res[1]
                if not valid or cur > max_end:
                    valid = False
                    break
                touch_blob = raw[t_start:cur]

            terms.append(
                {
                    "Term": term_key,
                    "TermType": term_type,
                    "Description": desc,
                    "Languages": vals,
                    "flags_blob": flags_blob,
                    "touch_blob": touch_blob,
                }
            )
        if valid and cur <= max_end:
            return terms, cur, has_touch
    return None


def _parse_binary_i2_source(raw: bytes, sf_version: int) -> Optional[Dict[str, Any]]:
    """Parse stripped-TypeTree Unity I2 LanguageSource / LanguageSourceAsset binary payload."""
    hdr = _skip_mb_header(raw, sf_version)
    if not hdr:
        return None
    start = hdr["end_pos"]
    # Scan 4-byte aligned offsets for mLanguages array, then locate preceding mTerms array
    for lang_off in range(start, max(start, len(raw) - 16), 4):
        lcount = struct.unpack_from("<i", raw, lang_off)[0]
        if not (1 <= lcount <= 64):
            continue
        langs_parsed = _try_parse_i2_languages_array(raw, lang_off)
        if not langs_parsed:
            continue
        langs, langs_end, _flags_step = langs_parsed
        # Search for mTerms array between header end and lang_off
        for term_off in range(start, lang_off, 4):
            tcount = struct.unpack_from("<i", raw, term_off)[0]
            if not (1 <= tcount <= 200_000):
                continue
            terms_parsed = _try_parse_i2_terms_array(raw, term_off, len(langs), lang_off)
            if not terms_parsed:
                continue
            terms, terms_end, has_touch = terms_parsed
            gap = lang_off - terms_end
            # Typical gap between mTerms and mLanguages in LanguageSourceData (includes flags, AppName, optional Assets/Google fields)
            if 0 <= gap <= 4096:
                return {
                    "m_Name": hdr["mb_name"],
                    "prefix_bytes": raw[:term_off],
                    "terms": terms,
                    "mid_bytes": raw[terms_end:lang_off],
                    "languages": langs,
                    "langs_end": langs_end,
                    "has_touch": has_touch,
                    "suffix_bytes": raw[lang_off:],
                }
    return None


def _patch_binary_i2_source(
    raw: bytes,
    sf_version: int,
    lang_index: int,
    updates_by_term: Dict[str, str],
) -> Tuple[bytes, int]:
    parsed = _parse_binary_i2_source(raw, sf_version)
    if not parsed:
        return raw, 0
    changed = 0
    out = bytearray(parsed["prefix_bytes"])
    terms = parsed["terms"]
    out.extend(struct.pack("<i", len(terms)))
    for term in terms:
        k = term["Term"]
        vals = list(term["Languages"])
        if k in updates_by_term:
            while len(vals) <= lang_index:
                vals.append("")
            vals[lang_index] = updates_by_term[k]
            changed += 1
        out.extend(_write_unity_str(k))
        out.extend(struct.pack("<i", term["TermType"]))
        out.extend(_write_unity_str(term["Description"]))
        out.extend(struct.pack("<i", len(vals)))
        for v in vals:
            out.extend(_write_unity_str(v))
        out.extend(term["flags_blob"])
        out.extend(term["touch_blob"])
    out.extend(parsed["mid_bytes"])
    out.extend(parsed["suffix_bytes"])
    return bytes(out), changed


# ---------------------------------------------------------------------------
# Unity TextAsset Localization Scan / Extract / Write Helpers
# ---------------------------------------------------------------------------

_TEXTASSET_HINT_TOKENS = (
    "loc", "lang", "i18n", "l10n", "i2", "string", "text", "dialog",
    "sub", "translat", "table", "caption", "glossary",
)


def _decode_textasset_script(ta: Any) -> Optional[str]:
    script = getattr(ta, "m_Script", None)
    if script is None:
        return None
    if isinstance(script, bytes):
        if len(script) < 16 or len(script) > 15 * 1024 * 1024 or b"\x00" in script[:2048]:
            return None
        if script.startswith(b"\xef\xbb\xbf"):
            return script[3:].decode("utf-8", errors="replace")
        return script.decode("utf-8", errors="replace")
    if isinstance(script, str):
        if len(script) < 16 or len(script) > 15 * 1024 * 1024 or "\x00" in script[:2048]:
            return None
        return script
    return None


def _walk_json_strings_local(obj: Any, path: str = "") -> List[Tuple[str, str]]:
    found: List[Tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            nxt = f"{path}.{k}" if path else str(k)
            found.extend(_walk_json_strings_local(v, nxt))
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            nxt = f"{path}[{idx}]"
            found.extend(_walk_json_strings_local(v, nxt))
    elif isinstance(obj, str) and is_translatable_string(obj):
        found.append((path, obj))
    return found


def _set_json_path_local(obj: Any, path: str, value: str) -> None:
    tokens = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    cur = obj
    for i, tok in enumerate(tokens):
        is_last = i == len(tokens) - 1
        if tok.startswith("[") and tok.endswith("]"):
            idx = int(tok[1:-1])
            if is_last:
                cur[idx] = value
            else:
                cur = cur[idx]
        else:
            if is_last:
                cur[tok] = value
            else:
                cur = cur[tok]


def _count_csv_non_empty_rows(text: str, delim: str, col_hdr: str) -> int:
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        if len(rows) < 2:
            return 0
        idx = rows[0].index(col_hdr)
        return sum(1 for r in rows[1:] if idx < len(r) and str(r[idx]).strip())
    except Exception:
        return 0


def _infer_lang_from_sample_texts(texts: List[str], fallback: str = "en") -> str:
    """Infer source language from sample strings (handles single-language English or CJK/Cyrillic tables)."""
    sample = " ".join(str(t) for t in texts[:60] if t)[:4000]
    if not sample.strip():
        return fallback
    if re.search(r"[\u3040-\u30ff]", sample):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", sample):
        return "ko"
    if re.search(r"[\u4e00-\u9fff]", sample):
        return "zh"
    if re.search(r"[\u0400-\u04ff]", sample):
        return "ru"
    if re.search(r"[ăâđêôơưĂÂĐÊÔƠƯ]", sample):
        return "vi"
    return fallback


def _sniff_single_csv_column(text: str) -> Optional[Tuple[str, int, int, List[str]]]:
    """Detect single-language CSV/TSV (e.g. Key,Text or Key,Value) in localization text."""
    for delim in (",", ";", "\t"):
        try:
            rows = list(csv.reader(io.StringIO(text[:65536]), delimiter=delim))
        except Exception:
            continue
        if len(rows) < 4 or not rows[0]:
            continue
        headers = [h.strip() for h in rows[0]]
        if len(headers) == 1 and "\t" not in text[:1024] and "," not in text[:1024] and ";" not in text[:1024]:
            continue
        # Pick value column by known header names or column 1 (Key, Text)
        val_col = -1
        for i, h in enumerate(headers):
            hl = h.lower()
            if hl in ("text", "value", "string", "english", "en", "localized", "translation", "source", "content", "dialogue"):
                val_col = i
                break
        if val_col == -1:
            val_col = 1 if len(headers) >= 2 else 0
        samples: List[str] = []
        valid_cnt = 0
        for r in rows[1:]:
            if val_col < len(r) and is_translatable_string(r[val_col]):
                valid_cnt += 1
                if len(samples) < 40:
                    samples.append(r[val_col])
        if valid_cnt >= 3:
            return delim, val_col, valid_cnt, samples
    return None


def _scan_text_asset(obj: Any, asset_file: Path, slots: Dict[str, Dict[str, Any]]) -> None:
    """Detect localization tables stored as Unity TextAsset (CSV/TSV, JSON, XML, PO, KV)."""
    try:
        ta = obj.read()
        name = getattr(ta, "m_Name", "") or ""
    except Exception:
        return
    name_low = name.lower()
    name_code = normalize_lang_token(name)
    has_hint = any(tok in name_low for tok in _TEXTASSET_HINT_TOKENS)
    if not has_hint and not is_known_lang_code(name_code):
        return

    text = _decode_textasset_script(ta)
    if not text:
        return
    stripped = text.lstrip()

    def _add_ta_slot(
        code: str,
        raw_code: str,
        provider: str,
        fmt_kind: str,
        count: int,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        slot = slots.setdefault(
            code,
            {
                "slot_id": code,
                "raw_code": raw_code,
                "provider": provider,
                "files": [],
                "text_assets": [],
                "count": 0,
            },
        )
        if str(asset_file) not in slot["files"]:
            slot["files"].append(str(asset_file))
        meta = {
            "file": str(asset_file),
            "path_id": obj.path_id,
            "name": name,
            "format": fmt_kind,
        }
        if extra:
            meta.update(extra)
        slot.setdefault("text_assets", []).append(meta)
        slot["count"] += count

    # 1. Multi-column CSV/TSV inside TextAsset
    csv_hit = sniff_csv_headers_from_text(text)
    if csv_hit:
        delim, cols = csv_hit
        for col_hdr in cols:
            code = normalize_lang_token(col_hdr)
            cnt = _count_csv_non_empty_rows(text, delim, col_hdr)
            if cnt > 0:
                _add_ta_slot(
                    code,
                    col_hdr,
                    "Unity TextAsset (CSV)",
                    "csv_multi",
                    cnt,
                    {"delimiter": delim, "column": col_hdr},
                )
        return

    # 1b. Single-language CSV/TSV (e.g. Key,Text or Key,Value in single-English games)
    single_csv = _sniff_single_csv_column(text)
    if single_csv:
        delim, col_idx, cnt, samples = single_csv
        code = name_code if is_known_lang_code(name_code) else _infer_lang_from_sample_texts(samples)
        _add_ta_slot(code, name or code, "Unity TextAsset (CSV)", "csv_single", cnt, {"delimiter": delim, "col_idx": col_idx})
        return

    # 2. JSON (multi-language top-level dict OR single-language dict/array)
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(text)
        except Exception:
            data = None
        if isinstance(data, dict):
            lang_keys = [k for k, v in data.items() if is_known_lang_code(normalize_lang_token(str(k))) and isinstance(v, (dict, list))]
            if len(lang_keys) >= 2:
                for lk in lang_keys:
                    code = normalize_lang_token(str(lk))
                    cnt = len(_walk_json_strings_local(data[lk]))
                    if cnt > 0:
                        _add_ta_slot(
                            code,
                            str(lk),
                            "Unity TextAsset (JSON)",
                            "json_multi",
                            cnt,
                            {"json_lang_key": str(lk)},
                        )
                return
        if data is not None:
            strings = _walk_json_strings_local(data)
            if is_known_lang_code(name_code) and len(strings) >= 3:
                _add_ta_slot(name_code, name, "Unity TextAsset (JSON)", "json_single", len(strings))
                return
            if has_hint and len(strings) >= 5:
                inferred = _infer_lang_from_sample_texts([v for _, v in strings])
                _add_ta_slot(inferred, name or inferred, "Unity TextAsset (JSON)", "json_single", len(strings))
                return

    # 3. Single-language XML
    if stripped.startswith("<"):
        try:
            root_xml = ET.fromstring(text)
            vals = [el.text for el in root_xml.iter() if el.text and is_translatable_string(el.text)]
            if (is_known_lang_code(name_code) and len(vals) >= 3) or (has_hint and len(vals) >= 5):
                code = name_code if is_known_lang_code(name_code) else _infer_lang_from_sample_texts(vals)
                _add_ta_slot(code, name or code, "Unity TextAsset (XML)", "xml_single", len(vals))
                return
        except Exception:
            pass

    # 4. Single-language PO
    if "msgid " in text[:2048] and "msgstr " in text[:2048]:
        try:
            po = polib.pofile(text)
            vals = [
                (e.msgstr if e.msgstr.strip() else e.msgid)
                for e in po
                if is_translatable_string(e.msgstr if e.msgstr.strip() else e.msgid)
            ]
            if (is_known_lang_code(name_code) and len(vals) >= 3) or (has_hint and len(vals) >= 5):
                code = name_code if is_known_lang_code(name_code) else _infer_lang_from_sample_texts(vals)
                _add_ta_slot(code, name or code, "Unity TextAsset (PO)", "po_single", len(vals))
                return
        except Exception:
            pass

    # 5. Single-language Key=Value
    kv_vals: List[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith(("#", ";", "[")):
            continue
        sep = "=" if "=" in line else None
        if sep:
            _, v = line.split(sep, 1)
            if is_translatable_string(v):
                kv_vals.append(v)
    if (is_known_lang_code(name_code) and len(kv_vals) >= 3) or (has_hint and len(kv_vals) >= 5):
        code = name_code if is_known_lang_code(name_code) else _infer_lang_from_sample_texts(kv_vals)
        _add_ta_slot(code, name or code, "Unity TextAsset (Text)", "kv_single", len(kv_vals))


def _extract_from_text_asset(
    ta: Any,
    ta_meta: Dict[str, Any],
    fpath: Path,
    slot_code: str,
) -> List[LocEntry]:
    entries: List[LocEntry] = []
    text = _decode_textasset_script(ta)
    if not text:
        return entries

    fmt_kind = ta_meta.get("format", "")
    ta_name = getattr(ta, "m_Name", "") or fpath.stem

    if fmt_kind in ("csv_multi", "csv_single"):
        delim = ta_meta.get("delimiter", ",")
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        if not rows:
            return entries
        if fmt_kind == "csv_multi":
            col_name = ta_meta.get("column", "")
            if col_name not in rows[0]:
                return entries
            col_idx = rows[0].index(col_name)
        else:
            col_idx = int(ta_meta.get("col_idx", 1 if len(rows[0]) >= 2 else 0))
        for r_i in range(1, len(rows)):
            row = rows[r_i]
            if col_idx >= len(row):
                continue
            val = row[col_idx]
            k = row[0] if row else f"ROW_{r_i}"
            if not is_translatable_string(val):
                continue
            entries.append(
                LocEntry(
                    entry_id=f"unity_ta::{fpath.name}::{ta_meta['path_id']}::{r_i}::{col_idx}",
                    key=k,
                    text=val,
                    file_path=str(fpath),
                    lang=slot_code,
                    fmt="unity_textasset_csv",
                    category=classify_entry_category(k, val),
                    location_meta={"path_id": ta_meta["path_id"], "row_idx": r_i, "col_idx": col_idx, "table_name": ta_name},
                )
            )
    elif fmt_kind in ("json_multi", "json_single"):
        data = json.loads(text)
        sub = data.get(ta_meta.get("json_lang_key", ""), {}) if fmt_kind == "json_multi" else data
        for jpath, val in _walk_json_strings_local(sub):
            entries.append(
                LocEntry(
                    entry_id=f"unity_ta::{fpath.name}::{ta_meta['path_id']}::{jpath}",
                    key=jpath,
                    text=val,
                    file_path=str(fpath),
                    lang=slot_code,
                    fmt="unity_textasset_json",
                    category=classify_entry_category(jpath, val),
                    location_meta={"path_id": ta_meta["path_id"], "json_path": jpath, "table_name": ta_name},
                )
            )
    elif fmt_kind == "xml_single":
        root_xml = ET.fromstring(text)
        for idx, elem in enumerate(root_xml.iter()):
            if elem.text and is_translatable_string(elem.text):
                k = elem.attrib.get("name") or elem.attrib.get("id") or f"{elem.tag}_{idx}"
                entries.append(
                    LocEntry(
                        entry_id=f"unity_ta::{fpath.name}::{ta_meta['path_id']}::{idx}",
                        key=k,
                        text=elem.text,
                        file_path=str(fpath),
                        lang=slot_code,
                        fmt="unity_textasset_xml",
                        category=classify_entry_category(k, elem.text),
                        location_meta={"path_id": ta_meta["path_id"], "xml_idx": idx, "table_name": ta_name},
                    )
                )
    elif fmt_kind == "po_single":
        po = polib.pofile(text)
        for idx, e in enumerate(po):
            val = e.msgstr if e.msgstr.strip() else e.msgid
            if not is_translatable_string(val):
                continue
            k = e.msgctxt or f"PO_{idx}"
            entries.append(
                LocEntry(
                    entry_id=f"unity_ta::{fpath.name}::{ta_meta['path_id']}::{idx}",
                    key=k,
                    text=val,
                    file_path=str(fpath),
                    lang=slot_code,
                    fmt="unity_textasset_po",
                    category=classify_entry_category(k, val),
                    location_meta={"path_id": ta_meta["path_id"], "po_idx": idx, "table_name": ta_name},
                )
            )
    elif fmt_kind == "kv_single":
        for l_i, line in enumerate(text.splitlines()):
            if line.lstrip().startswith(("#", ";", "[")):
                continue
            sep = "=" if "=" in line else ("\t" if "\t" in line else None)
            if not sep:
                continue
            k, v = line.split(sep, 1)
            if not is_translatable_string(v):
                continue
            entries.append(
                LocEntry(
                    entry_id=f"unity_ta::{fpath.name}::{ta_meta['path_id']}::{l_i}",
                    key=k.strip(),
                    text=v,
                    file_path=str(fpath),
                    lang=slot_code,
                    fmt="unity_textasset_kv",
                    category=classify_entry_category(k.strip(), v),
                    location_meta={"path_id": ta_meta["path_id"], "line_idx": l_i, "table_name": ta_name},
                )
            )
    return entries


def _patch_text_asset_script(
    ta: Any,
    ta_meta: Dict[str, Any],
    source_entries: List[LocEntry],
    by_key: Dict[str, str],
    by_src_text: Dict[str, str],
) -> int:
    text = _decode_textasset_script(ta)
    if not text:
        return 0
    fmt_kind = ta_meta.get("format", "")
    newline = "\r\n" if "\r\n" in text else "\n"
    updated = 0
    new_text: Optional[str] = None

    if fmt_kind in ("csv_multi", "csv_single"):
        delim = ta_meta.get("delimiter", ",")
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        col_idx = -1
        if rows:
            if fmt_kind == "csv_multi":
                col_name = ta_meta.get("column", "")
                if col_name in rows[0]:
                    col_idx = rows[0].index(col_name)
            else:
                col_idx = int(ta_meta.get("col_idx", 1 if len(rows[0]) >= 2 else 0))
        if rows and col_idx >= 0:
            src_by_row = {
                e.location_meta.get("row_idx"): (by_key.get(e.key) or by_src_text.get(e.text))
                for e in source_entries
                if "row_idx" in e.location_meta
            }
            for r_i in range(1, len(rows)):
                row = rows[r_i]
                while len(row) <= col_idx:
                    row.append("")
                k_val = row[0] if row else ""
                tr = src_by_row.get(r_i) or by_key.get(k_val) or by_src_text.get(row[col_idx])
                if tr:
                    row[col_idx] = tr
                    updated += 1
            buf = io.StringIO()
            writer = csv.writer(buf, delimiter=delim, lineterminator=newline)
            writer.writerows(rows)
            new_text = buf.getvalue()

    elif fmt_kind in ("json_multi", "json_single"):
        data = json.loads(text)
        sub = data.get(ta_meta.get("json_lang_key", ""), {}) if fmt_kind == "json_multi" else data
        for jpath, val in _walk_json_strings_local(sub):
            tr = by_key.get(jpath) or by_src_text.get(val)
            if tr:
                _set_json_path_local(sub, jpath, tr)
                updated += 1
        new_text = json.dumps(data, indent=2, ensure_ascii=False)

    elif fmt_kind == "xml_single":
        tree_xml = ET.ElementTree(ET.fromstring(text))
        for idx, elem in enumerate(tree_xml.getroot().iter()):
            k = elem.attrib.get("name") or elem.attrib.get("id") or f"{elem.tag}_{idx}"
            tr = by_key.get(k) or (elem.text and by_src_text.get(elem.text))
            if tr:
                elem.text = tr
                updated += 1
        new_text = ET.tostring(tree_xml.getroot(), encoding="unicode")

    elif fmt_kind == "po_single":
        po = polib.pofile(text)
        for e in po:
            k = e.msgctxt or ""
            tr = (k and by_key.get(k)) or by_src_text.get(e.msgid) or by_src_text.get(e.msgstr)
            if tr:
                e.msgstr = tr
                updated += 1
        new_text = str(po)

    elif fmt_kind == "kv_single":
        out_lines = []
        for line in text.splitlines():
            sep = "=" if "=" in line else ("\t" if "\t" in line else None)
            if sep and not line.lstrip().startswith(("#", ";", "[")):
                k, v = line.split(sep, 1)
                tr = by_key.get(k.strip()) or by_src_text.get(v)
                if tr:
                    out_lines.append(f"{k}{sep}{tr}")
                    updated += 1
                    continue
            out_lines.append(line)
        new_text = newline.join(out_lines)

    if new_text is not None and updated > 0:
        ta.m_Script = new_text
        ta.save()
    return updated


def diagnose_unity_empty_scan(game_dir: str | Path) -> Optional[str]:
    """Return a helpful Vietnamese diagnostic hint when a Unity game yields 0 localization slots."""
    root = Path(game_dir)
    if (root / "GameAssembly.dll").exists():
        return "Unity IL2CPP (chưa tìm thấy StringTable/I2/TextAsset chuẩn — dữ liệu ngôn ngữ có thể đóng gói định dạng riêng)"
    if any(root.glob("*_Data")):
        return "Unity (chưa tìm thấy bảng ngôn ngữ chuẩn — có thể game lưu chữ trực tiếp trong Scene hoặc định dạng riêng)"
    return None


_NON_LOC_UNITY_CLASSES = frozenset(
    {
        "rectmask2d", "text", "textmeshpro", "textmeshprougui", "tmp_text",
        "inputfield", "tmp_inputfield", "scrollrect", "graphicraycaster",
        "canvasscaler", "eventsystem", "contentsizefitter", "image", "button",
        "mask", "shadow", "outline", "scrollbar", "dropdown", "tmp_dropdown",
        "slider", "toggle", "togglegroup", "verticallayoutgroup", "horizontallayoutgroup",
        "gridlayoutgroup", "aspectratiofitter", "layoutelement", "canvasgroup",
    }
)


def _identity_looks_localizable(mb_name: str, script_class: str, script_name: str, go_name: str) -> bool:
    cls_low = (script_class or "").strip().lower()
    if cls_low in _NON_LOC_UNITY_CLASSES:
        return False
    joined = " ".join((mb_name, script_class, script_name, go_name)).lower()
    return any(
        tok in joined
        for tok in ("i2", "lang", "loc", "stringtable", "text", "dialog", "sub", "i18n", "l10n")
    )


def scan_unity_loc_tables(game_dir: str | Path) -> List[Dict[str, Any]]:
    """Fast scan of Unity native localization assets (StringTable, I2Languages, TextAsset, stripped IL2CPP fallback)."""
    root = Path(game_dir)
    candidates = find_unity_loc_candidates(root)
    global_scripts = _build_monoscript_index(candidates.get("data_dirs", []))
    slots: Dict[str, Dict[str, Any]] = {}

    def _register_st_slot(
        loc_code: str,
        hint_name: str,
        asset_file: Path,
        provider_label: str,
        count: int,
        raw_binary: bool = False,
    ) -> None:
        code = normalize_lang_token(loc_code or hint_name)
        slot = slots.setdefault(
            code,
            {
                "slot_id": code,
                "raw_code": loc_code or hint_name,
                "provider": provider_label,
                "files": [],
                "count": 0,
            },
        )
        if str(asset_file) not in slot["files"]:
            slot["files"].append(str(asset_file))
        if raw_binary:
            slot["raw_binary"] = True
        slot["count"] += count

    def _scan_bundle_file(bf: Path) -> None:
        lang_hint = normalize_lang_token(bf.stem)
        try:
            env = UnityPy.load(str(bf))
            local_scripts: Dict[Tuple[str, int], Tuple[str, str]] = {}
            _index_env_monoscripts(env, local_scripts)

            for obj in env.objects:
                if obj.type.name == "TextAsset":
                    _scan_text_asset(obj, bf, slots)
                    continue
                if obj.type.name != "MonoBehaviour":
                    continue

                mb_name, sc_cls, sc_name, go_name = _resolve_monobehaviour_identity(obj, local_scripts, global_scripts)
                try:
                    tree = obj.read_typetree()
                except Exception:
                    tree = None

                if _is_unity_string_table(tree):
                    locale_id = tree.get("m_LocaleId")
                    loc_code = (
                        (locale_id.get("m_Code", "") or "")
                        if isinstance(locale_id, dict)
                        else (locale_id if isinstance(locale_id, str) else "")
                    )
                    cnt = sum(1 for row in tree.get("m_TableData", []) if row.get("m_Localized"))
                    _register_st_slot(loc_code, lang_hint or mb_name, bf, "Unity StringTable (Addressables)", cnt)
                elif _is_i2_language_source(tree):
                    _register_i2_slots(tree, bf, slots)
                elif _identity_looks_localizable(mb_name, sc_cls, sc_name, go_name):
                    # Stripped TypeTree binary fallback (common in Unity IL2CPP)
                    sf_ver = getattr(getattr(obj.assets_file, "header", None), "version", 22)
                    raw_bytes = obj.get_raw_data()
                    st_bin = _parse_binary_string_table(raw_bytes, sf_ver)
                    if st_bin:
                        loc_code = st_bin["m_LocaleId"]["m_Code"]
                        cnt = sum(1 for r in st_bin["rows"] if r["m_Localized"])
                        _register_st_slot(loc_code, lang_hint or mb_name, bf, "Unity StringTable (IL2CPP Binary)", cnt, raw_binary=True)
                    else:
                        i2_bin = _parse_binary_i2_source(raw_bytes, sf_ver)
                        if i2_bin:
                            _register_i2_slots(i2_bin, bf, slots, provider="Unity I2 Localization (IL2CPP Binary)", raw_binary=True)
        except Exception as exc:
            logger.debug("Skipping Unity bundle %s: %s", bf.name, exc)

    # 1. Addressables & StreamingAssets AssetBundles (StringTable, I2Languages, TextAsset)
    for bf in candidates["bundles"]:
        _scan_bundle_file(bf)

    # 2. Check resources.assets & startup sharedassets*.assets for I2Languages Prefabs, StringTable & TextAsset
    for rf in candidates["resources"]:
        try:
            env = UnityPy.load(str(rf))
            local_scripts = {}
            _index_env_monoscripts(env, local_scripts)

            for obj in env.objects:
                if obj.type.name == "TextAsset":
                    _scan_text_asset(obj, rf, slots)
                    continue
                if obj.type.name != "MonoBehaviour":
                    continue

                mb_name, sc_cls, sc_name, go_name = _resolve_monobehaviour_identity(obj, local_scripts, global_scripts)
                if not _identity_looks_localizable(mb_name, sc_cls, sc_name, go_name):
                    continue

                try:
                    tree = obj.read_typetree()
                except Exception:
                    tree = None

                if _is_i2_language_source(tree):
                    _register_i2_slots(tree, rf, slots)
                elif _is_unity_string_table(tree):
                    locale_id = tree.get("m_LocaleId")
                    loc_code = locale_id.get("m_Code", "") if isinstance(locale_id, dict) else str(locale_id or "")
                    cand_code = normalize_lang_token(loc_code or mb_name or go_name or "en")
                    if cand_code in slots and "Addressables" in slots[cand_code].get("provider", ""):
                        continue
                    _register_st_slot(loc_code, mb_name or go_name or "en", rf, f"Unity StringTable ({rf.name})", len(tree.get("m_TableData", [])))
                else:
                    # Stripped TypeTree fallback on Unity IL2CPP Prefabs / ScriptableObjects
                    sf_ver = getattr(getattr(obj.assets_file, "header", None), "version", 22)
                    raw_bytes = obj.get_raw_data()
                    i2_bin = _parse_binary_i2_source(raw_bytes, sf_ver)
                    if i2_bin:
                        _register_i2_slots(i2_bin, rf, slots, provider="Unity I2 Localization (IL2CPP Binary)", raw_binary=True)
                    else:
                        st_bin = _parse_binary_string_table(raw_bytes, sf_ver)
                        if st_bin:
                            loc_code = st_bin["m_LocaleId"]["m_Code"]
                            cand_code = normalize_lang_token(loc_code or mb_name or go_name or "en")
                            if cand_code in slots and "Addressables" in slots[cand_code].get("provider", ""):
                                continue
                            cnt = sum(1 for r in st_bin["rows"] if r["m_Localized"])
                            _register_st_slot(loc_code, mb_name or go_name or "en", rf, f"Unity StringTable ({rf.name} Binary)", cnt, raw_binary=True)
        except Exception as exc:
            logger.debug("Skipping Unity resource %s: %s", rf.name, exc)

    # 3. Hashed Addressables fallback: if 0 slots found so far, inspect non-hint aa/*.bundle files (<= 30 MB)
    if not slots:
        for bf in candidates.get("aa_other_bundles", []):
            _scan_bundle_file(bf)

    return list(slots.values())


def _register_i2_slots(
    tree: Dict[str, Any],
    asset_file: Path,
    slots: Dict[str, Dict[str, Any]],
    provider: str = "Unity I2 Localization",
    raw_binary: bool = False,
) -> None:
    if "languages" in tree and "terms" in tree:
        langs = tree["languages"]
        terms = tree["terms"]
    else:
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
                "provider": provider,
                "files": [],
                "count": 0,
            },
        )
        if raw_binary:
            slot["raw_binary"] = True
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
    """Extract all translatable entries from the chosen Unity localization slot (StringTable / I2 / TextAsset)."""
    entries: List[LocEntry] = []
    files = [Path(p) for p in slot_info.get("files", [])]
    slot_code = slot_info["slot_id"]
    provider = slot_info.get("provider", "")

    # Group TextAsset targets by file when present
    ta_by_file: Dict[str, List[Dict[str, Any]]] = {}
    for ta_meta in slot_info.get("text_assets", []):
        ta_by_file.setdefault(str(Path(ta_meta["file"]).resolve()), []).append(ta_meta)

    for fpath in files:
        if not fpath.exists():
            continue
        env = UnityPy.load(str(fpath))
        resolved_str = str(fpath.resolve())

        # Extract TextAssets registered for this slot
        ta_targets = ta_by_file.get(resolved_str, [])
        if ta_targets:
            by_pid = {t["path_id"]: t for t in ta_targets}
            by_name = {t["name"]: t for t in ta_targets if t.get("name")}
            for obj in env.objects:
                if obj.type.name != "TextAsset":
                    continue
                meta = by_pid.get(obj.path_id)
                ta = obj.read()
                if meta is None:
                    meta = by_name.get(getattr(ta, "m_Name", ""))
                if meta:
                    entries.extend(_extract_from_text_asset(ta, meta, fpath, slot_code))
            continue

        shared_keys = _load_shared_table_keys(env)
        sf_ver = getattr(getattr(env.file, "header", None), "version", 22)
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                tree = obj.read_typetree()
            except Exception:
                tree = None

            if tree is not None and (_is_i2_language_source(tree) or _is_unity_string_table(tree)):
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
                continue

            # Raw binary fallback for stripped TypeTree IL2CPP MonoBehaviours
            obj_sf_ver = getattr(getattr(obj.assets_file, "header", None), "version", sf_ver)
            raw_bytes = obj.get_raw_data()
            i2_bin = _parse_binary_i2_source(raw_bytes, obj_sf_ver)
            if i2_bin:
                langs = i2_bin["languages"]
                terms = i2_bin["terms"]
                table_name = i2_bin.get("m_Name") or fpath.stem
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
                    entries.append(
                        LocEntry(
                            entry_id=f"unity_i2::{fpath.name}::{obj.path_id}::{t_idx}",
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
                                "raw_binary": True,
                            },
                        )
                    )
                continue

            st_bin = _parse_binary_string_table(raw_bytes, obj_sf_ver)
            if st_bin:
                table_name = st_bin.get("m_Name") or fpath.stem
                for r_idx, row in enumerate(st_bin["rows"]):
                    val = row["m_Localized"]
                    if not is_translatable_string(val):
                        continue
                    row_id = int(row["m_Id"])
                    friendly_key = shared_keys.get(row_id) or f"{table_name}_{row_id}"
                    cat = classify_entry_category(friendly_key, val)
                    entries.append(
                        LocEntry(
                            entry_id=f"unity_st::{fpath.name}::{obj.path_id}::{row_id}",
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
                                "raw_binary": True,
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
    """Write translated strings into inject_slot_info (StringTable / I2 / TextAsset / IL2CPP binary) & sync catalog."""
    root = Path(game_dir)
    target_files = [Path(p) for p in inject_slot_info.get("files", [])]
    if not target_files:
        return {"ok": False, "updated": 0, "error": "Slot Unity không có file đích."}

    target_slot_code = inject_slot_info["slot_id"]
    target_lang_idx = inject_slot_info.get("lang_index")
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

    # Build lookup by (table_name, row_id), key, and source text
    by_table_row: Dict[Tuple[str, Any], str] = {}
    by_key: Dict[str, str] = {}
    by_src_text: Dict[str, str] = {}
    for e in source_entries:
        tr = translations.get(e.text) or translations.get(e.entry_id)
        if is_piggyback and e.text.strip() in autonym_set:
            tr = display_label
        if not tr:
            continue
        by_key[e.key] = tr
        by_src_text[e.text] = tr
        tname = e.location_meta.get("table_name", "")
        if "row_id" in e.location_meta:
            base_tname = re.sub(r"[_-](?:en|fr|es|de|pl|pt|it|ja|ko|ru|zh|vi)(?:-[A-Za-z]+)?$", "", tname, flags=re.IGNORECASE)
            by_table_row[(base_tname, e.location_meta["row_id"])] = tr
            by_table_row[(tname, e.location_meta["row_id"])] = tr

    ta_by_file: Dict[str, List[Dict[str, Any]]] = {}
    for ta_meta in inject_slot_info.get("text_assets", []):
        ta_by_file.setdefault(str(Path(ta_meta["file"]).resolve()), []).append(ta_meta)

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
        is_bundle = isinstance(env.file, BundleFile)
        changed_file = False
        resolved_tf = str(tf.resolve())
        ta_targets = ta_by_file.get(resolved_tf, [])
        ta_by_pid = {t["path_id"]: t for t in ta_targets}
        ta_by_name = {t["name"]: t for t in ta_targets if t.get("name")}
        sf_ver = getattr(getattr(env.file, "header", None), "version", 22)

        for obj in env.objects:
            if obj.type.name == "TextAsset" and ta_targets:
                ta = obj.read()
                ta_meta = ta_by_pid.get(obj.path_id) or ta_by_name.get(getattr(ta, "m_Name", ""))
                if ta_meta:
                    cnt = _patch_text_asset_script(ta, ta_meta, source_entries, by_key, by_src_text)
                    if cnt > 0:
                        updated_count += cnt
                        changed_file = True
                continue

            if obj.type.name != "MonoBehaviour":
                continue
            try:
                tree = obj.read_typetree()
            except Exception:
                tree = None

            if tree is not None and _is_i2_language_source(tree):
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

            elif tree is not None and _is_unity_string_table(tree):
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

            else:
                # Raw binary fallback for stripped TypeTree Unity IL2CPP assets
                obj_sf_ver = getattr(getattr(obj.assets_file, "header", None), "version", sf_ver)
                raw_bytes = obj.get_raw_data()
                i2_bin = _parse_binary_i2_source(raw_bytes, obj_sf_ver)
                if i2_bin:
                    idx = target_lang_idx
                    if idx is None:
                        for i, l in enumerate(i2_bin["languages"]):
                            if normalize_lang_token(l.get("Code") or l.get("Name") or "") == target_slot_code:
                                idx = i
                                break
                    if idx is not None:
                        patched_raw, cnt = _patch_binary_i2_source(raw_bytes, obj_sf_ver, idx, by_key)
                        if cnt > 0:
                            obj.set_raw_data(patched_raw)
                            updated_count += cnt
                            changed_file = True
                    continue

                st_bin = _parse_binary_string_table(raw_bytes, obj_sf_ver)
                if st_bin:
                    tname = st_bin.get("m_Name") or tf.stem
                    base_tname = re.sub(r"[_-](?:en|fr|es|de|pl|pt|it|ja|ko|ru|zh|vi)(?:-[A-Za-z]+)?$", "", tname, flags=re.IGNORECASE)
                    updates_by_rid: Dict[int, str] = {}
                    for row in st_bin["rows"]:
                        rid = int(row["m_Id"])
                        tr = by_table_row.get((base_tname, rid)) or by_table_row.get((tname, rid))
                        if tr:
                            updates_by_rid[rid] = tr
                    if updates_by_rid:
                        patched_raw, cnt = _patch_binary_string_table(raw_bytes, obj_sf_ver, updates_by_rid)
                        if cnt > 0:
                            obj.set_raw_data(patched_raw)
                            updated_count += cnt
                            changed_file = True

        if changed_file:
            out_bytes = env.file.save(packer="lz4") if is_bundle else env.file.save()
            del env
            gc.collect()
            atomic_write_bytes(tf, out_bytes)
            new_size = len(out_bytes)
            if is_bundle:
                bundle_updates.append((tf.name, old_size, new_size))
        else:
            del env
            if is_bundle:
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
