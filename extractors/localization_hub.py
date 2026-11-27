"""Unified <1s Native Localization Scanner & Slot Injector (External + Multi-Column + Ren'Py + Unity + Unreal)."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import polib
from ruamel.yaml import YAML

from extractors.subtitle_filter import (
    LocEntry,
    classify_entry_category,
    filter_entries_by_scope,
    is_translatable_string,
)
from extractors.unity_loc_table import (
    apply_unity_translations,
    diagnose_unity_empty_scan,
    extract_unity_slot_entries,
    is_known_lang_code,
    normalize_lang_token,
    scan_unity_loc_tables,
    sniff_csv_headers_from_text,
)
from extractors.unreal_locres import (
    apply_unreal_translations,
    diagnose_unreal_empty_scan,
    extract_unreal_slot_entries,
    scan_unreal_localization,
    scan_unreal_localization_detailed,
)
from safety_shield.encoding_guard import detect_encoding_and_eol, write_preserving_encoding

from core import get_app_dir

logger = logging.getLogger("sakuga.hub")

APP_DIR = get_app_dir(__file__)
RPATOOL_PATH = APP_DIR / "tools" / "rpatool.py"

EXTERNAL_LOC_DIRS = {
    "localization", "localisation", "languages", "language", "locale", "locales",
    "i18n", "l10n", "text", "texts", "lang", "langs", "tl", "translation", "translations",
    "data",  # RPG Maker www/data or data
}

EXTERNAL_EXTS = {
    ".json", ".csv", ".tsv", ".po", ".pot", ".xml", ".yaml", ".yml",
    ".ini", ".txt", ".rpy", ".loc", ".lang", ".strings", ".bytes",
}
MULTI_COL_NON_LANG_HEADERS = {
    "key", "id", "string_id", "comment", "notes", "note", "context", "description", "category", "speaker", "character",
}

KNOWN_LANG_ALIASES = {
    "en", "eng", "english", "en-us", "en-gb",
    "vi", "vie", "vietnamese", "vn",
    "fr", "fre", "fra", "french",
    "es", "spa", "spanish",
    "de", "ger", "deu", "german",
    "pl", "pol", "polish",
    "pt", "pt-br", "ptbr", "por", "portuguese", "brazilian",
    "it", "ita", "italian",
    "ja", "jp", "jpn", "japanese",
    "ko", "kor", "korean",
    "ru", "rus", "russian",
    "zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant", "schinese", "tchinese", "chinese",
    "tr", "tur", "turkish",
    "nl", "nld", "dutch",
    "uk", "ukr", "ukrainian",
    "sv", "swe", "swedish",
}

LANG_DISPLAY_NAMES = {
    "en": "English",
    "vi": "Tiếng Việt",
    "fr": "Français (French)",
    "pl": "Polski (Polish)",
    "pt-BR": "Português (Brasil)",
    "pt": "Português",
    "es": "Español (Spanish)",
    "de": "Deutsch (German)",
    "it": "Italiano (Italian)",
    "ja": "日本語 (Japanese)",
    "ko": "한국어 (Korean)",
    "ru": "Русский (Russian)",
    "zh": "简体中文 (Chinese)",
    "zh-TW": "繁體中文",
    "tr": "Türkçe",
    "uk": "Українська",
    "nl": "Nederlands",
    "sv": "Svenska",
}

yaml_rw = YAML()
yaml_rw.preserve_quotes = True


def detect_game_engine(game_dir: Path) -> str:
    if any(game_dir.glob("*_Data")) and (
        any(game_dir.glob("UnityPlayer.dll")) or any(game_dir.glob("GameAssembly.dll")) or (game_dir / "MonoBleedingEdge").exists()
    ):
        if (game_dir / "GameAssembly.dll").exists():
            return "Unity IL2CPP"
        return "Unity Mono"
    if (game_dir / "renpy").exists() and (game_dir / "game").exists():
        return "Ren'Py"
    if (game_dir / "www" / "data").exists() or (game_dir / "js" / "rmmz_core.js").exists() or (game_dir / "data" / "System.json").exists():
        return "RPG Maker MV/MZ"
    if any(game_dir.rglob("*.uproject")) or (game_dir / "Engine").exists() or any((game_dir / p).exists() for p in ("Content/Paks",)):
        return "Unreal Engine"
    if list(game_dir.glob("*/Content/Paks")):
        return "Unreal Engine"
    return "Generic Engine"


def _guess_lang_from_path(fpath: Path, game_dir: Path) -> Optional[str]:
    try:
        rel_parts = list(fpath.relative_to(game_dir).parts)
    except ValueError:
        rel_parts = [fpath.name]
    stem = fpath.stem.lower().replace("_", "-")
    # Direct folder like Localization/en/strings.json, Localization/English (US)/ui.json, or game/tl/french/dialogue.rpy
    for part in reversed(rel_parts[:-1]):
        p_low = part.lower().replace("_", "-")
        if p_low in KNOWN_LANG_ALIASES:
            return normalize_lang_token(p_low)
        cand = normalize_lang_token(part)
        if is_known_lang_code(cand):
            return cand
    # Filename stem: strings_en.json, locale.fr.yaml, English (US).json, Chinese_Simplified.json, pt_BR.json
    if stem in KNOWN_LANG_ALIASES:
        return normalize_lang_token(stem)
    cand_stem = normalize_lang_token(fpath.stem)
    if is_known_lang_code(cand_stem):
        return cand_stem
    for tok in reversed(re.split(r"[-_.]", stem)):
        if tok in KNOWN_LANG_ALIASES:
            return normalize_lang_token(tok)
    return None


def _sniff_csv_columns(fpath: Path) -> Optional[Tuple[str, List[str]]]:
    """Detect whether a CSV/TSV has multiple language columns and which delimiter it uses."""
    default_delim = "\t" if fpath.suffix.lower() == ".tsv" else ","
    try:
        info = detect_encoding_and_eol(fpath.read_bytes()[:32768])
        return sniff_csv_headers_from_text(info["text"], default_delim=default_delim)
    except Exception:
        return None


def _scan_rpa_tl_archives(game_dir: Path) -> List[Dict[str, Any]]:
    """Use tools/rpatool.py if present to inspect Ren'Py .rpa archives containing tl/<lang>/."""
    if not RPATOOL_PATH.exists():
        return []
    tl_slots: Dict[str, Dict[str, Any]] = {}
    from core import hidden_subprocess_kwargs
    win_kw = hidden_subprocess_kwargs()
    for rpa in game_dir.rglob("*.rpa"):
        try:
            proc = subprocess.run(
                [sys.executable, str(RPATOOL_PATH), "-l", str(rpa)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                **win_kw,
            )
            if proc.returncode != 0:
                continue
            for line in proc.stdout.splitlines():
                rel = line.strip().replace("\\", "/")
                m = re.match(r"^(?:game/)?tl/([^/]+)/.+\.rpy$", rel, re.IGNORECASE)
                if m:
                    raw_lang = m.group(1)
                    if raw_lang.lower() == "none":
                        continue
                    code = normalize_lang_token(raw_lang)
                    slot = tl_slots.setdefault(
                        code,
                        {
                            "slot_id": code,
                            "raw_code": raw_lang,
                            "provider": "Ren'Py .rpa (rpatool)",
                            "files": [],
                            "rpa_files": [],
                            "count": 0,
                        },
                    )
                    slot["rpa_files"].append({"rpa_path": str(rpa), "internal_path": rel})
                    slot["count"] += 25
        except Exception:
            pass
    return list(tl_slots.values())


def _scan_external_files(game_dir: Path, engine: str) -> List[Dict[str, Any]]:
    """Fast scan for external localization files and multi-column CSV/TSV tables."""
    slots: Dict[str, Dict[str, Any]] = {}
    root_depth = len(game_dir.parts)

    # Walk at most 7 levels deep, skipping heavy folders
    skip_dirs = {"__pycache__", ".git", "node_modules", "monobleedingedge", "d3d12", "backups", "engine"}

    for dirpath, dirnames, filenames in os.walk(game_dir):
        cur = Path(dirpath)
        depth = len(cur.parts) - root_depth
        dirnames[:] = [d for d in dirnames if d.lower() not in skip_dirs]
        if depth >= 7:
            dirnames[:] = []

        # Quick relevance check: inside named loc folder, game/tl, or root config
        rel_parts_low = [p.lower() for p in cur.relative_to(game_dir).parts] if cur != game_dir else []
        in_loc_folder = any(p in EXTERNAL_LOC_DIRS for p in rel_parts_low)

        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in EXTERNAL_EXTS:
                continue
            fpath = cur / fname
            try:
                if fpath.stat().st_size > 15 * 1024 * 1024 or fpath.stat().st_size < 10:
                    continue
            except OSError:
                continue

            # Ignore random .txt/.ini/.xml/.loc/.lang/.strings/.bytes unless inside a loc folder or clearly loc-related
            if ext in (".txt", ".ini", ".xml", ".loc", ".lang", ".strings", ".bytes") and not in_loc_folder:
                if not any(h in fname.lower() for h in ("lang", "loc", "text", "string", "i18n", "sub", "dialog")):
                    continue
            if ext == ".bytes":
                try:
                    head = fpath.read_bytes()[:1024]
                    if b"\x00" in head:
                        continue
                except OSError:
                    continue

            if ext in (".csv", ".tsv"):
                csv_hit = _sniff_csv_columns(fpath)
                if csv_hit:
                    delim, cols = csv_hit
                    for col_hdr in cols:
                        code = normalize_lang_token(col_hdr)
                        slot = slots.setdefault(
                            code,
                            {
                                "slot_id": code,
                                "raw_code": col_hdr,
                                "provider": f"Multi-Column {ext.upper()}",
                                "files": [],
                                "csv_columns": {},
                                "csv_delimiters": {},
                                "count": 0,
                            },
                        )
                        if str(fpath) not in slot["files"]:
                            slot["files"].append(str(fpath))
                        slot.setdefault("csv_columns", {})[str(fpath)] = col_hdr
                        slot.setdefault("csv_delimiters", {})[str(fpath)] = delim
                        slot["count"] += 1
                    continue

            # Ren'Py game/tl/<lang>/*.rpy
            if ext == ".rpy" and "tl" in rel_parts_low:
                idx = rel_parts_low.index("tl")
                if idx + 1 < len(rel_parts_low):
                    raw_lang = cur.relative_to(game_dir).parts[idx + 1]
                    if raw_lang.lower() == "none":
                        continue
                    code = normalize_lang_token(raw_lang)
                    slot = slots.setdefault(
                        code,
                        {
                            "slot_id": code,
                            "raw_code": raw_lang,
                            "provider": "Ren'Py tl/ Script",
                            "files": [],
                            "count": 0,
                        },
                    )
                    slot["files"].append(str(fpath))
                    slot["count"] += 1
                    continue

            # RPG Maker data/ System/Map/CommonEvents -> default single source slot from system locale ("en" or "ja")
            if "rpg" in engine.lower() and ext == ".json" and "data" in rel_parts_low:
                code = _guess_lang_from_path(fpath, game_dir) or "en"
                slot = slots.setdefault(
                    code,
                    {
                        "slot_id": code,
                        "raw_code": code,
                        "provider": "RPG Maker JSON Data",
                        "files": [],
                        "count": 0,
                    },
                )
                slot["files"].append(str(fpath))
                slot["count"] += 1
                continue

            lang_code = _guess_lang_from_path(fpath, game_dir)
            if not lang_code and (in_loc_folder or any(h in fname.lower() for h in ("lang", "loc", "string", "dialog", "sub"))) and ext in (".json", ".po", ".xml", ".yaml", ".yml", ".loc", ".lang", ".strings", ".csv", ".tsv", ".txt", ".bytes"):
                # Single-file localization file without explicit lang tag (e.g. only English) -> default to 'en'
                lang_code = "en"
            if lang_code:
                slot = slots.setdefault(
                    lang_code,
                    {
                        "slot_id": lang_code,
                        "raw_code": lang_code,
                        "provider": f"External ({ext})",
                        "files": [],
                        "count": 0,
                    },
                )
                slot["files"].append(str(fpath))
                slot["count"] += 1

    return list(slots.values())


def scan_game_languages(game_dir: str | Path, aes_key: Optional[str] = None) -> Dict[str, Any]:
    """Unified <1s scanner returning all detected language slots & native method."""
    root = Path(game_dir)
    if not root.exists():
        return {"ok": False, "engine": "Unknown", "slots": [], "method": "Not found", "error": "Thư mục không tồn tại"}

    engine = detect_game_engine(root)
    merged: Dict[str, Dict[str, Any]] = {}

    def _merge(slot_list: List[Dict[str, Any]]) -> None:
        for s in slot_list:
            sid = s["slot_id"]
            if sid not in merged:
                s.setdefault("label", LANG_DISPLAY_NAMES.get(sid, sid.upper()))
                merged[sid] = s
            else:
                cur = merged[sid]
                for f in s.get("files", []):
                    if f not in cur.setdefault("files", []):
                        cur["files"].append(f)
                cur["count"] = max(cur.get("count", 0), s.get("count", 0))
                if s.get("pak_entries"):
                    cur.setdefault("pak_entries", []).extend(s["pak_entries"])
                if s.get("utoc_entries"):
                    cur.setdefault("utoc_entries", []).extend(s["utoc_entries"])
                if s.get("rpa_files"):
                    cur.setdefault("rpa_files", []).extend(s["rpa_files"])
                if s.get("csv_columns"):
                    cur.setdefault("csv_columns", {}).update(s["csv_columns"])
                if s.get("csv_delimiters"):
                    cur.setdefault("csv_delimiters", {}).update(s["csv_delimiters"])
                if s.get("text_assets"):
                    cur.setdefault("text_assets", []).extend(s["text_assets"])
                if s.get("raw_binary"):
                    cur["raw_binary"] = True
                if s.get("aes_key"):
                    cur["aes_key"] = s["aes_key"]

    # Run native hunters according to detected engine first, plus external files
    if "unity" in engine.lower():
        _merge(scan_unity_loc_tables(root))
    unreal_detail: Dict[str, Any] = {
        "is_aes_encrypted": False,
        "is_iostore": False,
        "has_pak_signature": False,
        "shipping_exe_path": "",
        "discovered_aes_key": "",
        "error_code": None,
        "diagnostic": None,
    }
    if "unreal" in engine.lower() or any(root.rglob("*.locres")):
        unreal_detail = scan_unreal_localization_detailed(root, aes_key=aes_key, auto_dump=True)
        _merge(unreal_detail.get("slots", []))
    if "ren'py" in engine.lower():
        _merge(_scan_rpa_tl_archives(root))

    _merge(_scan_external_files(root, engine))

    slots_out = sorted(merged.values(), key=lambda x: (0 if x["slot_id"] == "en" else 1, -x.get("count", 0), x["slot_id"]))
    providers = sorted({s["provider"] for s in slots_out})
    if providers:
        method_label = " + ".join(providers)
    else:
        diag = None
        if "unity" in engine.lower():
            diag = diagnose_unity_empty_scan(root)
        elif "unreal" in engine.lower():
            diag = unreal_detail.get("diagnostic") or diagnose_unreal_empty_scan(root, aes_key=aes_key)
        method_label = diag or "Không tìm thấy bảng ngôn ngữ gốc"

    return {
        "ok": True,
        "engine": engine,
        "method": method_label,
        "slots": slots_out,
        "slot_count": len(slots_out),
        "is_aes_encrypted": bool(unreal_detail.get("is_aes_encrypted")),
        "is_iostore": bool(unreal_detail.get("is_iostore")),
        "has_pak_signature": bool(unreal_detail.get("has_pak_signature")),
        "shipping_exe_path": unreal_detail.get("shipping_exe_path", ""),
        "discovered_aes_key": unreal_detail.get("discovered_aes_key", ""),
        "error_code": unreal_detail.get("error_code"),
    }


def _walk_json_strings(obj: Any, path: str = "") -> List[Tuple[str, str]]:
    found: List[Tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            nxt = f"{path}.{k}" if path else str(k)
            found.extend(_walk_json_strings(v, nxt))
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            nxt = f"{path}[{idx}]"
            found.extend(_walk_json_strings(v, nxt))
    elif isinstance(obj, str) and is_translatable_string(obj):
        found.append((path, obj))
    return found


def _set_json_path(obj: Any, path: str, value: str) -> None:
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


def _parse_rpy_file(fpath: Path, text: str, slot_code: str) -> List[LocEntry]:
    entries: List[LocEntry] = []
    lines = text.splitlines()
    # Pattern A: old "..." followed by new "..."
    old_re = re.compile(r'^\s*old\s+"((?:\\.|[^"\\])*)"')
    new_re = re.compile(r'^\s*new\s+"((?:\\.|[^"\\])*)"')
    # Pattern B: dialogue inside translate block: character "dialogue" or "dialogue"
    dlg_re = re.compile(r'^\s+(?:[A-Za-z0-9_]+\s+)?"((?:\\.|[^"\\])*)"\s*(?:#.*)?$')

    in_strings_block = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("translate ") and stripped.endswith("strings:"):
            in_strings_block = True
            i += 1
            continue
        if stripped.startswith("translate ") and not stripped.endswith("strings:"):
            in_strings_block = False
            i += 1
            continue

        m_old = old_re.match(line)
        if m_old and i + 1 < len(lines):
            m_new = new_re.match(lines[i + 1])
            if m_new:
                raw_val = m_new.group(1) or m_old.group(1)
                if is_translatable_string(raw_val):
                    entries.append(
                        LocEntry(
                            entry_id=f"rpy::{fpath.name}::L{i+1}",
                            key=f"UI_RPY_{i+1}",
                            text=raw_val,
                            file_path=str(fpath),
                            lang=slot_code,
                            fmt=".rpy",
                            category="UI" if in_strings_block else "SUBTITLE",
                            location_meta={"line_idx": i + 1, "mode": "new_quote"},
                        )
                    )
                i += 2
                continue

        if not stripped.startswith("#") and not in_strings_block:
            m_dlg = dlg_re.match(line)
            if m_dlg:
                raw_val = m_dlg.group(1)
                if is_translatable_string(raw_val):
                    entries.append(
                        LocEntry(
                            entry_id=f"rpy::{fpath.name}::L{i}",
                            key=f"SUB_RPY_{i}",
                            text=raw_val,
                            file_path=str(fpath),
                            lang=slot_code,
                            fmt=".rpy",
                            category="SUBTITLE",
                            location_meta={"line_idx": i, "mode": "dlg_quote"},
                        )
                    )
        i += 1
    return entries


def extract_slot_entries(
    game_dir: str | Path,
    slot_info: Dict[str, Any],
    scope: str = "FULL_PATCH",
    translate_choices: bool = True,
) -> Dict[str, Any]:
    """Extract & scope-filter all entries from the selected Input Language Slot."""
    provider = slot_info.get("provider", "")
    raw_entries: List[LocEntry] = []

    if "Unity" in provider:
        raw_entries.extend(extract_unity_slot_entries(game_dir, slot_info))
    if "Unreal" in provider:
        raw_entries.extend(extract_unreal_slot_entries(game_dir, slot_info))

    code = slot_info["slot_id"]
    csv_columns = slot_info.get("csv_columns", {})
    csv_delimiters = slot_info.get("csv_delimiters", {})

    for fp in slot_info.get("files", []):
        p = Path(fp)
        if not p.exists():
            continue
        ext = p.suffix.lower()
        if ext not in EXTERNAL_EXTS:
            continue

        if ext in (".po", ".pot"):
            po = polib.pofile(str(p))
            for idx, entry in enumerate(po):
                src_text = entry.msgstr if entry.msgstr.strip() else entry.msgid
                if not is_translatable_string(src_text):
                    continue
                k = entry.msgctxt or f"PO_{idx}"
                raw_entries.append(
                    LocEntry(
                        entry_id=f"po::{p.name}::{idx}",
                        key=k,
                        text=src_text,
                        file_path=str(p),
                        lang=code,
                        fmt=".po",
                        category=classify_entry_category(k, src_text),
                        location_meta={"po_index": idx, "msgid": entry.msgid},
                    )
                )
            continue

        enc_info = detect_encoding_and_eol(p.read_bytes())
        text = enc_info["text"]
        if ext in (".loc", ".lang", ".strings", ".bytes"):
            trimmed = text.lstrip()
            if trimmed.startswith("{") or trimmed.startswith("["):
                effective_ext = ".json"
            elif trimmed.startswith("<"):
                effective_ext = ".xml"
            elif sniff_csv_headers_from_text(text) is not None:
                effective_ext = ".csv"
            else:
                effective_ext = ext
        else:
            effective_ext = ext

        if effective_ext == ".json":
            data = json.loads(text)
            for jpath, val in _walk_json_strings(data):
                raw_entries.append(
                    LocEntry(
                        entry_id=f"json::{p.name}::{jpath}",
                        key=jpath,
                        text=val,
                        file_path=str(p),
                        lang=code,
                        fmt=".json",
                        category=classify_entry_category(jpath, val),
                        location_meta={"json_path": jpath},
                    )
                )
        elif effective_ext in (".yaml", ".yml"):
            data = yaml_rw.load(io.StringIO(text))
            for ypath, val in _walk_json_strings(data):
                raw_entries.append(
                    LocEntry(
                        entry_id=f"yaml::{p.name}::{ypath}",
                        key=ypath,
                        text=val,
                        file_path=str(p),
                        lang=code,
                        fmt=".yaml",
                        category=classify_entry_category(ypath, val),
                        location_meta={"yaml_path": ypath},
                    )
                )
        elif effective_ext in (".csv", ".tsv"):
            delim = csv_delimiters.get(str(p), "\t" if ext == ".tsv" else ",")
            rows = list(csv.reader(io.StringIO(text), delimiter=delim))
            if not rows:
                continue
            headers = rows[0]
            target_col_name = csv_columns.get(str(p))
            col_idx = headers.index(target_col_name) if target_col_name in headers else (1 if len(headers) > 1 else 0)
            key_idx = 0
            for r_i in range(1, len(rows)):
                row = rows[r_i]
                if col_idx >= len(row):
                    continue
                val = row[col_idx]
                k = row[key_idx] if key_idx < len(row) else f"ROW_{r_i}"
                if not is_translatable_string(val):
                    continue
                raw_entries.append(
                    LocEntry(
                        entry_id=f"csv::{p.name}::{r_i}::{col_idx}",
                        key=k,
                        text=val,
                        file_path=str(p),
                        lang=code,
                        fmt=ext,
                        category=classify_entry_category(k, val),
                        location_meta={"row_idx": r_i, "col_idx": col_idx, "key_val": k},
                    )
                )
        elif ext == ".xml":
            tree_xml = ET.fromstring(text)
            for idx, elem in enumerate(tree_xml.iter()):
                if elem.text and is_translatable_string(elem.text):
                    k = elem.attrib.get("name") or elem.attrib.get("id") or f"{elem.tag}_{idx}"
                    raw_entries.append(
                        LocEntry(
                            entry_id=f"xml::{p.name}::{idx}",
                            key=k,
                            text=elem.text,
                            file_path=str(p),
                            lang=code,
                            fmt=".xml",
                            category=classify_entry_category(k, elem.text),
                            location_meta={"xml_idx": idx, "tag": elem.tag, "key": k},
                        )
                    )
        elif effective_ext in (".ini", ".txt", ".loc", ".lang", ".strings", ".bytes"):
            for l_i, line in enumerate(text.splitlines()):
                sep = "=" if "=" in line else ("\t" if "\t" in line else None)
                if not sep or line.lstrip().startswith(("#", ";", "[")):
                    continue
                k, v = line.split(sep, 1)
                if not is_translatable_string(v):
                    continue
                raw_entries.append(
                    LocEntry(
                        entry_id=f"kv::{p.name}::{l_i}",
                        key=k.strip(),
                        text=v,
                        file_path=str(p),
                        lang=code,
                        fmt=ext,
                        category=classify_entry_category(k.strip(), v),
                        location_meta={"line_idx": l_i, "sep": sep, "kv_key": k.strip()},
                    )
                )
        elif ext == ".rpy":
            raw_entries.extend(_parse_rpy_file(p, text, code))

    filtered = filter_entries_by_scope(raw_entries, scope=scope, translate_choices=translate_choices)
    ui_count = sum(1 for e in raw_entries if e.category == "UI")
    sub_count = sum(1 for e in raw_entries if e.category in ("SUBTITLE", "CHOICE"))

    return {
        "total_raw": len(raw_entries),
        "filtered_count": len(filtered),
        "ui_count": ui_count,
        "sub_count": sub_count,
        "entries": filtered,
        "all_entries": raw_entries,
    }


def apply_slot_translations(
    game_dir: str | Path,
    source_entries: List[LocEntry],
    translations: Dict[str, str],
    inject_slot_info: Dict[str, Any],
    backup_cb=None,
    target_lang: str = "vi",
    source_slot_code: str = "",
) -> Dict[str, Any]:
    """Write translations into the chosen Inject Slot (direct overwrite or cross-slot piggyback)."""
    provider = inject_slot_info.get("provider", "")
    if "Unity" in provider:
        return apply_unity_translations(
            game_dir,
            source_entries,
            translations,
            inject_slot_info,
            backup_cb=backup_cb,
            target_lang=target_lang,
            source_slot_code=source_slot_code,
        )
    if "Unreal" in provider:
        return apply_unreal_translations(game_dir, source_entries, translations, inject_slot_info, backup_cb=backup_cb)

    # External files (direct overwrite OR piggyback to target slot's file(s))
    target_files = [Path(p) for p in inject_slot_info.get("files", [])]
    if not target_files:
        return {"ok": False, "updated": 0, "error": "Slot đích không có file khả dụng."}

    if backup_cb:
        backup_cb([str(p) for p in target_files], [])

    by_key = {e.key: (translations.get(e.text) or translations.get(e.entry_id)) for e in source_entries}
    by_src_text = {e.text: translations[e.text] for e in source_entries if e.text in translations}
    csv_columns = inject_slot_info.get("csv_columns", {})
    csv_delimiters = inject_slot_info.get("csv_delimiters", {})
    updated = 0

    for tf in target_files:
        if not tf.exists():
            continue
        ext = tf.suffix.lower()

        if ext in (".po", ".pot"):
            po = polib.pofile(str(tf))
            for entry in po:
                k = entry.msgctxt or ""
                tr = (k and by_key.get(k)) or by_src_text.get(entry.msgid) or by_src_text.get(entry.msgstr)
                if tr:
                    entry.msgstr = tr
                    updated += 1
            po.save(str(tf))
            continue

        enc = detect_encoding_and_eol(tf.read_bytes())
        text = enc["text"]
        if ext in (".loc", ".lang", ".strings", ".bytes"):
            trimmed = text.lstrip()
            if trimmed.startswith("{") or trimmed.startswith("["):
                effective_ext = ".json"
            elif trimmed.startswith("<"):
                effective_ext = ".xml"
            elif sniff_csv_headers_from_text(text) is not None:
                effective_ext = ".csv"
            else:
                effective_ext = ext
        else:
            effective_ext = ext

        if effective_ext == ".json":
            data = json.loads(text)
            for jpath, val in _walk_json_strings(data):
                tr = by_key.get(jpath) or by_src_text.get(val)
                if tr:
                    _set_json_path(data, jpath, tr)
                    updated += 1
            out_str = json.dumps(data, indent=2, ensure_ascii=False)
            write_preserving_encoding(tf, out_str, enc["encoding"], enc["bom"], enc["newline"])

        elif effective_ext in (".yaml", ".yml"):
            data = yaml_rw.load(io.StringIO(text))
            for ypath, val in _walk_json_strings(data):
                tr = by_key.get(ypath) or by_src_text.get(val)
                if tr:
                    _set_json_path(data, ypath, tr)
                    updated += 1
            buf = io.StringIO()
            yaml_rw.dump(data, buf)
            write_preserving_encoding(tf, buf.getvalue(), enc["encoding"], enc["bom"], enc["newline"])

        elif effective_ext in (".csv", ".tsv"):
            delim = csv_delimiters.get(str(tf), "\t" if ext == ".tsv" else ",")
            rows = list(csv.reader(io.StringIO(text), delimiter=delim))
            if not rows:
                continue
            headers = rows[0]
            col_name = csv_columns.get(str(tf))
            col_idx = headers.index(col_name) if col_name in headers else (1 if len(headers) > 1 else 0)
            # Match by Key in col 0 or by source row entries matching exact row index
            src_by_row = {
                e.location_meta.get("row_idx"): (translations.get(e.text) or translations.get(e.entry_id))
                for e in source_entries
                if e.fmt in (".csv", ".tsv")
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
            writer = csv.writer(buf, delimiter=delim, lineterminator=enc["newline"])
            writer.writerows(rows)
            write_preserving_encoding(tf, buf.getvalue(), enc["encoding"], enc["bom"], enc["newline"])

        elif ext == ".xml":
            tree = ET.ElementTree(ET.fromstring(text))
            for idx, elem in enumerate(tree.getroot().iter()):
                k = elem.attrib.get("name") or elem.attrib.get("id") or f"{elem.tag}_{idx}"
                tr = by_key.get(k) or (elem.text and by_src_text.get(elem.text))
                if tr:
                    elem.text = tr
                    updated += 1
            xml_bytes = ET.tostring(tree.getroot(), encoding="unicode")
            write_preserving_encoding(tf, xml_bytes, enc["encoding"], enc["bom"], enc["newline"])

        elif effective_ext in (".ini", ".txt", ".loc", ".lang", ".strings", ".bytes"):
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
            write_preserving_encoding(tf, "\n".join(out_lines), enc["encoding"], enc["bom"], enc["newline"])

        elif ext == ".rpy":
            lines = text.splitlines()
            src_entries_same = [e for e in source_entries if e.file_path == str(tf)]
            for e in src_entries_same:
                tr = translations.get(e.text) or translations.get(e.entry_id)
                if not tr:
                    continue
                li = e.location_meta.get("line_idx")
                if li is None or li >= len(lines):
                    continue
                safe_tr = tr.replace('"', '\\"')
                old_line = lines[li]
                new_line = re.sub(r'"(?:\\.|[^"\\])*"', f'"{safe_tr}"', old_line, count=1)
                if new_line != old_line:
                    lines[li] = new_line
                    updated += 1
            write_preserving_encoding(tf, "\n".join(lines), enc["encoding"], enc["bom"], enc["newline"])

    return {"ok": True, "updated": updated, "mode": "external_files"}
