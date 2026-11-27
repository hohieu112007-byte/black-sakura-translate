"""Black Sakura Translate — FastAPI Backend, SSE Live Stream & PyWebView Desktop Window."""

from __future__ import annotations

import asyncio
import base64
import gc
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx
import uvicorn
from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS")) if (getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")) else APP_DIR
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(BUNDLE_DIR))

from core import hidden_subprocess_kwargs
from core.atomic_io import atomic_write_text, cleanup_orphaned_temps
from core.database import GameCacheDB, GlobalTMDB, LibraryDB
from core.hardware_profiler import HardwareProfiler
from core.win_tray import WinSystemTray
from core.game_launcher import (
    COVERS_DIR,
    collect_steam_apps,
    download_steam_cover,
    fetch_steam_app_context,
    ingest_game_input,
    launch_game_safely,
)
from core.prompts import (
    QWEN35_SUPPORTED_LANGS,
    get_lang_display_name,
    is_qwen_supported_lang,
)
from core.qwen_engine import PRESET_PROFILES, QwenEngine, estimate_preset_durations
from core.skill_manager import SkillManager, build_ai_skill_gen_prompt, parse_ai_generated_skill
from core.slot_recommender import rank_inject_slots
from core.tool_bootstrapper import ToolBootstrapper
from extractors.localization_hub import (
    apply_slot_translations,
    extract_slot_entries,
    scan_game_languages,
)
from extractors.unreal_locres import (
    auto_dump_unreal_aes_keys,
    inspect_unreal_paks,
    normalize_aes_key,
    verify_unreal_aes_key,
)
from safety_shield.anticheat_guard import scan_anticheat
from safety_shield.backup_manager import BackupManager
from safety_shield.encoding_guard import (
    detect_translation_anomaly,
    strip_thinking,
    try_fix_mojibake,
    validate_serialized_syntax,
)
from safety_shield.font_patcher import FontPatcher
from safety_shield.tag_firewall import mask_tags, validate_and_unmask

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sakuga.app")

FRONTEND_DIR = BUNDLE_DIR / "frontend"
for _runtime_dir in ("backups", "cache", "covers", "exports", "tools", "skills"):
    (APP_DIR / _runtime_dir).mkdir(parents=True, exist_ok=True)
if BUNDLE_DIR != APP_DIR and (BUNDLE_DIR / "skills").exists():
    for _layer in ("languages", "styles"):
        _src_layer = BUNDLE_DIR / "skills" / _layer
        _dst_layer = APP_DIR / "skills" / _layer
        _dst_layer.mkdir(parents=True, exist_ok=True)
        if _src_layer.exists():
            for _md in _src_layer.glob("*.md"):
                _target_md = _dst_layer / _md.name
                if not _target_md.exists():
                    shutil.copy2(_md, _target_md)

hw_profiler = HardwareProfiler(APP_DIR / "settings.json")
hw_profiler.apply_ollama_env()
skill_manager = SkillManager(APP_DIR / "skills")
library_db = LibraryDB(APP_DIR / "library.db")
global_tm = GlobalTMDB(APP_DIR / "global_tm.db")
backup_mgr = BackupManager(APP_DIR / "backups")
qwen_engine = QwenEngine(profiler=hw_profiler)
tool_boot = ToolBootstrapper(APP_DIR / "tools")
font_patcher = FontPatcher(APP_DIR / "tools" / "fonts")

# Active translation jobs & SSE subscribers
_job_states: Dict[str, Dict[str, Any]] = {}
_sse_queues: Set[asyncio.Queue] = set()
_wv_window = None
_active_server = None
_tray_controller: Optional[WinSystemTray] = None
_force_app_exit: bool = False
_page_loaded: bool = False
_tray_balloon_shown: bool = False
_instance_mutex_handle: Optional[int] = None

_APP_TITLE = "Black Sakura Translate"
_APP_VERSION = "1.0.3"
_GITHUB_REPO = "hohieu112007-byte/black-sakura-translate"
_GITHUB_RELEASES_API = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
_GITHUB_RELEASES_PAGE = f"https://github.com/{_GITHUB_REPO}/releases"
_UPDATE_CHECK_TTL_SEC = 12 * 3600
_APP_IDENTITY = "sakuga_localization_launcher"
_SINGLE_INSTANCE_MUTEX_NAME = "Local\\SakugaLocalizationLauncher_SingleInstance_v1"
_INSTANCE_LOCK_PATH = APP_DIR / ".instance_lock.json"


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_MAX_COVER_BYTES = 10 * 1024 * 1024
_MAX_SAKUGAPACK_BYTES = 32 * 1024 * 1024


def _validate_safe_id(value: str, label: str = "ID") -> str:
    clean = (value or "").strip()
    if not clean or ".." in clean or not _SAFE_ID_RE.match(clean):
        raise HTTPException(status_code=400, detail=f"{label} không hợp lệ.")
    return clean


def _safe_zip_read_limited(zf: zipfile.ZipFile, member_name: str, max_bytes: int) -> bytes:
    info = zf.getinfo(member_name)
    if info.file_size > max_bytes:
        raise ValueError(f"File nén '{member_name}' vượt quá giới hạn an toàn ({max_bytes // (1024 * 1024)} MB).")
    with zf.open(info, "r") as fp:
        data = fp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"Dữ liệu giải nén '{member_name}' vượt quá giới hạn cho phép.")
    return data


def _any_job_running() -> bool:
    return any(st.get("status") == "running" for st in _job_states.values())


def _perform_full_exit(destroy_window: bool = True) -> None:
    global _force_app_exit
    _force_app_exit = True
    if _active_server is not None:
        _active_server.should_exit = True
    if _tray_controller is not None:
        try:
            _tray_controller.stop()
        except Exception:
            pass
    if destroy_window and _wv_window is not None:
        try:
            _wv_window.destroy()
        except Exception:
            pass


def _restore_window_from_tray() -> None:
    if _wv_window is None:
        return
    try:
        _wv_window.show()
        _wv_window.restore()
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            user32.FindWindowW.restype = wintypes.HWND
            hwnd = user32.FindWindowW(None, _APP_TITLE)
            if hwnd:
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                else:
                    user32.ShowWindow(hwnd, 5)  # SW_SHOW
                user32.SetForegroundWindow(hwnd)
        except Exception:
            pass


def _hide_window_to_tray(show_tip: bool = True) -> None:
    global _tray_balloon_shown
    if _wv_window is None:
        return
    try:
        _wv_window.hide()
    except Exception:
        pass
    if show_tip and _tray_controller is not None and not _tray_balloon_shown:
        _tray_balloon_shown = True
        msg = (
            "Tiến trình Dịch / Fix lỗi đang chạy ngầm an toàn. Bấm icon Black Sakura Translate dưới khay để mở lại."
            if _any_job_running()
            else "Đã thu gọn xuống khay hệ thống. Bấm icon Black Sakura Translate ở khay để mở lại."
        )
        _tray_controller.show_balloon("Black Sakura Translate", msg)


def emit_sse(event: str, data: Dict[str, Any]) -> None:
    payload = {"event": event, "data": data, "ts": time.time()}
    for q in list(_sse_queues):
        try:
            q.put_nowait(payload)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    async def _initial_bootstrap():
        try:
            roots = [APP_DIR / "backups"] + [g.get("game_dir", "") for g in library_db.list_games()]
            await asyncio.to_thread(cleanup_orphaned_temps, roots)
        except Exception:
            pass
        st = tool_boot.get_status()
        if not st["all_ready"]:
            def _cb(info):
                emit_sse("tool_progress", info)
            await tool_boot.ensure_all(force=False, progress_cb=_cb)
            emit_sse("tool_status", tool_boot.get_status())
        # Silently wake installed Ollama headless server on startup so users don't need to open Ollama manually
        try:
            if await qwen_engine.ensure_ollama_running():
                emit_sse("tool_status", tool_boot.get_status())
        except Exception:
            pass

    asyncio.create_task(_initial_bootstrap())
    yield


app = FastAPI(title="Black Sakura Translate", lifespan=lifespan)
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", re.IGNORECASE)


@app.middleware("http")
async def _origin_csrf_guard(request, call_next):
    origin = (request.headers.get("origin") or "").strip()
    if origin and origin.lower() != "null" and not _LOCAL_ORIGIN_RE.match(origin):
        return JSONResponse(status_code=403, content={"detail": "Cross-origin requests from external sites are blocked."})
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ---------------- Pydantic Models ---------------- #

class AddGameRequest(BaseModel):
    path: str


class UnrealAesKeyRequest(BaseModel):
    aes_key: str


class ScanRequest(BaseModel):
    source_lang: Optional[str] = None
    target_lang: Optional[str] = "vi"
    scope: str = "FULL_PATCH"
    translate_choices: bool = True
    aes_key: Optional[str] = None


class SlotRankRequest(BaseModel):
    source_lang: str = "en"
    target_lang: str = "vi"
    current_inject_slot: Optional[str] = None
    persist: bool = False


class UpdateProfileRequest(BaseModel):
    title: Optional[str] = None
    launch_mode: Optional[str] = None
    steam_appid: Optional[str] = None
    shortcut_path: Optional[str] = None
    exe_path: Optional[str] = None
    game_description: Optional[str] = None
    tone_preset: Optional[str] = None
    style_skill_id: Optional[str] = None
    lang_skill_override: Optional[str] = None
    compiled_style_card: Optional[str] = None
    inject_slot: Optional[str] = None
    source_lang: Optional[str] = None
    target_lang: Optional[str] = None
    scope: Optional[str] = None
    preset_level: Optional[str] = None
    ai_mode: Optional[str] = None
    translate_choices: Optional[bool] = None
    keep_proper_nouns: Optional[bool] = None
    patch_fonts: Optional[bool] = None
    aes_key: Optional[str] = None


class StartTranslateRequest(BaseModel):
    game_id: str
    source_lang: str = "en"
    target_lang: str = "vi"
    inject_slot: str
    scope: str = "FULL_PATCH"
    ai_mode: str = "AUTO_HYBRID"
    translate_choices: bool = True
    keep_proper_nouns: bool = True
    patch_fonts: bool = True
    force_offline_override: bool = False
    preset_level: Optional[str] = None
    style_skill_id: Optional[str] = None
    lang_skill_override: Optional[str] = None
    bypass_global_tm: bool = False


class GlossaryAddRequest(BaseModel):
    source_lang: str = "en"
    target_lang: str = "vi"
    source_term: str
    target_term: str
    note: str = ""


class EditTranslationRequest(BaseModel):
    str_hash: str
    translated_text: str


class RepairRequest(BaseModel):
    retry_with_ai: bool = True
    polish_awkward: bool = True
    fix_fonts: bool = True
    force_offline_override: bool = False
    style_skill_id: Optional[str] = None
    lang_skill_override: Optional[str] = None


class ApplySavedTranslationRequest(BaseModel):
    source_lang: str = "en"
    target_lang: str = "vi"
    inject_slot: Optional[str] = None
    patch_fonts: bool = True
    force_offline_override: bool = False


class UiThemeRequest(BaseModel):
    theme: str = "mono-ink"


class UiLangRequest(BaseModel):
    lang: str = "vi"


class AutoUpdateSettingRequest(BaseModel):
    enabled: bool = True


class ApplyAppUpdateRequest(BaseModel):
    confirmed: bool = False
    download_url: Optional[str] = None


class MinimizeTrayRequest(BaseModel):
    enabled: bool = True


class HardwareSettingsRequest(BaseModel):
    auto_tune: Optional[bool] = None
    main_model: Optional[str] = None
    dual_model_enabled: Optional[bool] = None
    ui_fast_model: Optional[str] = None
    quiet_mode: Optional[bool] = None
    low_ram_mode: Optional[bool] = None
    parallel_workers: Optional[int] = None
    num_ctx: Optional[int] = None
    ui_batch_size: Optional[int] = None
    sub_batch_size: Optional[int] = None
    kv_cache_type: Optional[str] = None
    ollama_base_url: Optional[str] = None


class BulkReplaceRequest(BaseModel):
    find_text: str
    replace_text: str
    case_sensitive: bool = False
    category_filter: str = "ALL"
    hot_apply: bool = False


class SakugaPackImportRequest(BaseModel):
    filename: str = "pack.sakugapack"
    content_b64: str
    auto_apply: bool = True
    inject_slot: Optional[str] = None


class ExternalTranslationImportRequest(BaseModel):
    filename: str = "translated.json"
    content_b64: str
    source_lang: Optional[str] = None
    target_lang: Optional[str] = None
    auto_apply: bool = True
    inject_slot: Optional[str] = None
    patch_fonts: bool = True
    force_offline_override: bool = False


class PullModelRequest(BaseModel):
    model: str


class SaveSkillRequest(BaseModel):
    id: Optional[str] = None
    layer: str = "styles"
    name: str
    icon: str = "🎭"
    description: str = ""
    language_pair: str = "any"
    source_lang: str = "any"
    target_lang: str = "any"
    source_langs: Optional[List[str]] = None
    target_langs: Optional[List[str]] = None
    priority: int = 50
    rules: Any = ""
    rules_text: Optional[str] = None
    examples: List[Dict[str, str]] = []


class ImportSkillRequest(BaseModel):
    filename: str
    content_b64: Optional[str] = None
    raw_text: Optional[str] = None


class AiSkillGenRequest(BaseModel):
    idea: str
    layer: str = "styles"
    source_lang: str = "en"
    target_lang: str = "vi"
    auto_save: bool = False


class ReapplySkillRequest(BaseModel):
    style_skill_id: Optional[str] = None
    lang_skill_override: Optional[str] = None
    scope: str = "flagged_only"
    keep_manual_locked: bool = True
    auto_start: bool = True


# ---------------- Frontend & Health ---------------- #

@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/favicon.ico")
async def favicon():
    ico = BUNDLE_DIR / "sakuga_icon.ico"
    if ico.exists():
        return FileResponse(str(ico))
    raise HTTPException(status_code=404, detail="Icon not found")


@app.get("/covers/{filename}")
async def serve_cover(filename: str):
    target = (COVERS_DIR / filename).resolve()
    if target.parent != COVERS_DIR.resolve() or not target.exists():
        raise HTTPException(status_code=404, detail="Cover not found")
    return FileResponse(str(target))


@app.get("/api/health")
async def api_health():
    await qwen_engine.fetch_ollama_ps()
    ollama_st, telemetry, hw_eff = await asyncio.gather(
        qwen_engine.check_ollama_status(),
        asyncio.to_thread(qwen_engine.get_telemetry),
        asyncio.to_thread(hw_profiler.get_effective_settings),
    )
    return {
        "ok": True,
        "ollama": ollama_st,
        "telemetry": telemetry,
        "hardware": hw_eff,
        "tools": tool_boot.get_status(),
        "tm_stats": global_tm.stats(),
        "supported_langs": QWEN35_SUPPORTED_LANGS,
        "app_version": _APP_VERSION,
        "auto_check_updates": library_db.get_setting("auto_check_updates", "1") == "1",
        "ui_theme": library_db.get_setting("ui_theme", "mono-ink"),
        "ui_lang": library_db.get_setting("ui_lang", "vi"),
        "minimize_to_tray": library_db.get_setting("minimize_to_tray", "1") == "1",
    }


@app.post("/api/ui/theme")
async def api_save_ui_theme(req: UiThemeRequest):
    allowed = {"mono-ink", "obsidian", "sakuga-indigo", "cyber-ice", "amber-ink"}
    theme = req.theme if req.theme in allowed else "mono-ink"
    library_db.set_setting("ui_theme", theme)
    return {"ok": True, "theme": theme}


@app.post("/api/ui/lang")
async def api_save_ui_lang(req: UiLangRequest):
    lang = "en" if (req.lang or "").strip().lower() == "en" else "vi"
    library_db.set_setting("ui_lang", lang)
    return {"ok": True, "lang": lang}


@app.post("/api/ui/minimize-to-tray")
async def api_save_minimize_to_tray(req: MinimizeTrayRequest):
    library_db.set_setting("minimize_to_tray", "1" if req.enabled else "0")
    return {"ok": True, "enabled": bool(req.enabled)}


def _parse_version_tuple(raw: str) -> tuple[int, ...]:
    """Extract numeric version tuple like (1, 0, 1) from tags such as 'v1.0.1'."""
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", str(raw or ""), re.IGNORECASE)
    if not m:
        return (0,)
    return tuple(int(part) for part in m.group(1).split("."))


def _is_newer_version(remote_tag: str, current_ver: str) -> bool:
    r_parts = list(_parse_version_tuple(remote_tag))
    c_parts = list(_parse_version_tuple(current_ver))
    max_len = max(len(r_parts), len(c_parts))
    r_parts.extend([0] * (max_len - len(r_parts)))
    c_parts.extend([0] * (max_len - len(c_parts)))
    return tuple(r_parts) > tuple(c_parts)


_app_update_in_progress: bool = False


def _sanitize_release_url(url: Optional[str]) -> str:
    candidate = str(url or "").strip()
    allowed_prefix = f"https://github.com/{_GITHUB_REPO}/releases"
    if candidate.startswith(allowed_prefix):
        return candidate
    return _GITHUB_RELEASES_PAGE


def _sanitize_asset_download_url(url: Optional[str]) -> str:
    candidate = str(url or "").strip()
    allowed_prefix = f"https://github.com/{_GITHUB_REPO}/releases/download/"
    if candidate.startswith(allowed_prefix) and candidate.lower().endswith(".zip"):
        return candidate
    return ""


def _pick_windows_standalone_asset(assets: Any) -> Dict[str, Any]:
    if not isinstance(assets, list):
        return {"asset_name": "", "asset_size": 0, "download_url": ""}
    chosen = None
    for a in assets:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        low = name.lower()
        dl = _sanitize_asset_download_url(a.get("browser_download_url"))
        if dl and "windows" in low and "standalone" in low and low.endswith(".zip"):
            chosen = a
            break
    if chosen is None:
        for a in assets:
            if not isinstance(a, dict):
                continue
            name = str(a.get("name") or "").lower()
            dl = _sanitize_asset_download_url(a.get("browser_download_url"))
            if dl and "windows" in name and name.endswith(".zip"):
                chosen = a
                break
    if not chosen:
        return {"asset_name": "", "asset_size": 0, "download_url": ""}
    return {
        "asset_name": str(chosen.get("name") or ""),
        "asset_size": int(chosen.get("size") or 0),
        "download_url": _sanitize_asset_download_url(chosen.get("browser_download_url")),
    }


def _detect_update_mode(download_url: str) -> str:
    if os.name == "nt" and getattr(sys, "frozen", False) and bool(_sanitize_asset_download_url(download_url)):
        return "exe_zip"
    if (APP_DIR / ".git").exists():
        return "git_pull"
    return "manual"


def _safe_extract_update_zip(zip_path: Path, dest_dir: Path) -> Path:
    """Safely extract verified release ZIP (blocking Zip-Slip & size bombs) and locate BlackSakuraTranslate.exe."""
    max_total_bytes = 400 * 1024 * 1024
    total_bytes = 0
    dest_resolved = dest_dir.resolve()
    dest_resolved.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            member = info.filename
            if not member or member.endswith("/"):
                continue
            if member.startswith(("/", "\\")) or ":" in member.split("/")[0]:
                raise ValueError(f"Unsafe archive path: {member}")
            target = (dest_resolved / member).resolve()
            if dest_resolved not in target.parents and target != dest_resolved:
                raise ValueError(f"Zip-Slip path rejected: {member}")
            total_bytes += max(0, int(info.file_size))
            if total_bytes > max_total_bytes:
                raise ValueError("Archive exceeds safe size limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src_f, open(target, "wb") as out_f:
                shutil.copyfileobj(src_f, out_f, 1024 * 256)
            if os.name == "nt":
                ads = f"{target}:Zone.Identifier"
                try:
                    if os.path.exists(ads):
                        os.remove(ads)
                except OSError:
                    pass

    if (dest_resolved / "BlackSakuraTranslate.exe").exists():
        return dest_resolved
    for exe_hit in dest_resolved.rglob("BlackSakuraTranslate.exe"):
        return exe_hit.parent
    raise ValueError("Gói tải về không chứa BlackSakuraTranslate.exe hợp lệ.")


def _write_windows_update_script(new_app_root: Path, app_dir: Path, pid: int, staging_dir: Path) -> Path:
    """Generate a self-cleaning CMD helper that swaps binaries after exit while preserving all user data."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    script_path = staging_dir / "apply_update.cmd"
    src_s = str(new_app_root.resolve())
    dst_s = str(app_dir.resolve())
    stg_s = str(staging_dir.resolve())
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        f"set \"PID={int(pid)}\"",
        f"set \"SRC={src_s}\"",
        f"set \"DST={dst_s}\"",
        f"set \"STG={stg_s}\"",
        "set /a TRIES=0",
        ":waitloop",
        "tasklist /FI \"PID eq %PID%\" 2>nul | findstr /C:\"%PID%\" >nul",
        "if not errorlevel 1 (",
        "  set /a TRIES+=1",
        "  if %TRIES% LSS 30 (",
        "    timeout /t 1 /nobreak >nul",
        "    goto waitloop",
        "  )",
        ")",
        "timeout /t 1 /nobreak >nul",
        "copy /Y \"%SRC%\\BlackSakuraTranslate.exe\" \"%DST%\\BlackSakuraTranslate.exe\" >nul",
        "if exist \"%SRC%\\_internal\" (",
        "  robocopy \"%SRC%\\_internal\" \"%DST%\\_internal\" /MIR /NFL /NDL /NJH /NJS /NC /NS /R:3 /W:1 >nul",
        ")",
        "for %%F in (HUONG_DAN_SU_DUNG.txt README.md LICENSE) do (",
        "  if exist \"%SRC%\\%%F\" copy /Y \"%SRC%\\%%F\" \"%DST%\\%%F\" >nul",
        ")",
        "start \"\" /D \"%DST%\" \"%DST%\\BlackSakuraTranslate.exe\"",
        "timeout /t 2 /nobreak >nul",
        "rd /s /q \"%STG%\" 2>nul",
        "endlocal",
    ]
    script_path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return script_path


@app.get("/api/app/update-check")
async def api_app_update_check(force: bool = False):
    auto_enabled = library_db.get_setting("auto_check_updates", "1") == "1"
    now = int(time.time())
    cached_raw = library_db.get_setting("app_update_cache", "")
    cached_data: Optional[Dict[str, Any]] = None
    if cached_raw:
        try:
            parsed = json.loads(cached_raw)
            if isinstance(parsed, dict):
                cached_data = parsed
        except Exception:
            cached_data = None

    if not force:
        if cached_data and (now - int(cached_data.get("checked_at", 0))) < _UPDATE_CHECK_TTL_SEC:
            latest_tag = str(cached_data.get("latest_tag") or _APP_VERSION)
            dl_url = _sanitize_asset_download_url(cached_data.get("download_url"))
            upd_mode = _detect_update_mode(dl_url)
            return {
                **cached_data,
                "ok": True,
                "current_version": _APP_VERSION,
                "update_available": _is_newer_version(latest_tag, _APP_VERSION),
                "release_url": _sanitize_release_url(cached_data.get("release_url")),
                "download_url": dl_url,
                "update_mode": upd_mode,
                "can_auto_update": upd_mode in ("exe_zip", "git_pull"),
                "auto_check_updates": auto_enabled,
                "cached": True,
            }
        if not auto_enabled:
            upd_mode = _detect_update_mode("")
            return {
                "ok": True,
                "skipped": True,
                "current_version": _APP_VERSION,
                "latest_version": _APP_VERSION,
                "latest_tag": f"v{_APP_VERSION}",
                "update_available": False,
                "release_name": "",
                "release_notes": "",
                "release_url": _GITHUB_RELEASES_PAGE,
                "asset_name": "",
                "asset_size": 0,
                "download_url": "",
                "update_mode": upd_mode,
                "can_auto_update": upd_mode in ("exe_zip", "git_pull"),
                "published_at": "",
                "checked_at": int(cached_data.get("checked_at", 0)) if cached_data else 0,
                "auto_check_updates": False,
                "cached": True,
            }

    try:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"BlackSakuraTranslate/{_APP_VERSION}",
        }
        async with httpx.AsyncClient(timeout=4.5, follow_redirects=False) as client:
            resp = await client.get(_GITHUB_RELEASES_API, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"GitHub API HTTP {resp.status_code}")
        payload = resp.json()
        latest_tag = str(payload.get("tag_name") or f"v{_APP_VERSION}").strip()
        latest_ver = latest_tag.lstrip("vV") or _APP_VERSION
        notes = str(payload.get("body") or "").strip()
        if len(notes) > 1200:
            notes = notes[:1200].rstrip() + "..."
        asset_info = _pick_windows_standalone_asset(payload.get("assets"))
        upd_mode = _detect_update_mode(asset_info["download_url"])
        result = {
            "ok": True,
            "current_version": _APP_VERSION,
            "latest_version": latest_ver,
            "latest_tag": latest_tag,
            "update_available": _is_newer_version(latest_tag, _APP_VERSION),
            "release_name": str(payload.get("name") or latest_tag).strip(),
            "release_notes": notes,
            "release_url": _sanitize_release_url(payload.get("html_url")),
            "asset_name": asset_info["asset_name"],
            "asset_size": asset_info["asset_size"],
            "download_url": asset_info["download_url"],
            "update_mode": upd_mode,
            "can_auto_update": upd_mode in ("exe_zip", "git_pull"),
            "published_at": str(payload.get("published_at") or "").strip(),
            "checked_at": now,
            "auto_check_updates": auto_enabled,
            "cached": False,
            "error": None,
        }
        library_db.set_setting("app_update_cache", json.dumps(result, ensure_ascii=False))
        return result
    except Exception as exc:
        logger.debug("App update check failed/offline: %s", exc)
        upd_mode = _detect_update_mode("")
        return {
            "ok": False,
            "current_version": _APP_VERSION,
            "latest_version": _APP_VERSION,
            "latest_tag": f"v{_APP_VERSION}",
            "update_available": False,
            "release_name": "",
            "release_notes": "",
            "release_url": _GITHUB_RELEASES_PAGE,
            "asset_name": "",
            "asset_size": 0,
            "download_url": "",
            "update_mode": upd_mode,
            "can_auto_update": False,
            "published_at": "",
            "checked_at": now,
            "auto_check_updates": auto_enabled,
            "cached": False,
            "error": str(exc),
        }


@app.post("/api/app/update-settings")
async def api_save_update_settings(req: AutoUpdateSettingRequest):
    library_db.set_setting("auto_check_updates", "1" if req.enabled else "0")
    return {"ok": True, "auto_check_updates": bool(req.enabled)}


@app.post("/api/app/open-release")
async def api_open_release_page(url: Optional[str] = None):
    import webbrowser

    safe_url = _sanitize_release_url(url)
    try:
        webbrowser.open(safe_url)
        return {"ok": True, "url": safe_url}
    except Exception as exc:
        return {"ok": False, "url": safe_url, "message": str(exc)}


@app.post("/api/app/update-apply")
async def api_apply_app_update(req: ApplyAppUpdateRequest):
    global _app_update_in_progress
    if not req.confirmed:
        raise HTTPException(status_code=400, detail="Cần người dùng bấm xác nhận cho phép trước khi cập nhật.")
    if _any_job_running():
        raise HTTPException(
            status_code=409,
            detail="Đang có tiến trình Dịch hoặc Fix lỗi chạy ngầm. Hãy đợi xong hoặc tạm dừng trước khi cập nhật ứng dụng.",
        )
    if _app_update_in_progress:
        return {"ok": True, "started": False, "message": "Đang tải bản cập nhật..."}

    # Resolve validated download_url from request or cached release check
    dl_url = _sanitize_asset_download_url(req.download_url)
    if not dl_url:
        try:
            cached_info = json.loads(library_db.get_setting("app_update_cache", "") or "{}")
            dl_url = _sanitize_asset_download_url(cached_info.get("download_url"))
        except Exception:
            dl_url = ""

    mode = _detect_update_mode(dl_url)
    if mode == "manual":
        return {
            "ok": False,
            "mode": "manual",
            "release_url": _GITHUB_RELEASES_PAGE,
            "message": "Chế độ cài đặt hiện tại không hỗ trợ tự ghi đè; hãy tải trực tiếp từ GitHub Releases.",
        }

    if mode == "git_pull":
        def _run_git_update() -> Dict[str, Any]:
            st = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(APP_DIR),
                capture_output=True,
                text=True,
                timeout=10,
                **hidden_subprocess_kwargs(),
            )
            if st.returncode != 0:
                return {"ok": False, "mode": "git_pull", "message": "Không đọc được trạng thái Git."}
            if (st.stdout or "").strip():
                return {
                    "ok": False,
                    "mode": "git_pull",
                    "message": "Thư mục mã nguồn đang có thay đổi chưa commit; vui lòng tải thủ công hoặc commit trước.",
                }
            pull = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=str(APP_DIR),
                capture_output=True,
                text=True,
                timeout=45,
                **hidden_subprocess_kwargs(),
            )
            if pull.returncode != 0:
                return {
                    "ok": False,
                    "mode": "git_pull",
                    "message": (pull.stderr or pull.stdout or "git pull thất bại").strip()[:220],
                }
            return {
                "ok": True,
                "mode": "git_pull",
                "restart_required": True,
                "message": "Đã cập nhật mã nguồn bằng git pull! Hãy khởi động lại app để áp dụng.",
            }

        res = await asyncio.to_thread(_run_git_update)
        emit_sse(
            "app_update_progress",
            {
                "stage": "done_restart_needed" if res.get("ok") else "error",
                "pct": 100 if res.get("ok") else 0,
                "message": res.get("message", ""),
            },
        )
        return res

    # Frozen Windows Standalone .EXE self-update
    _app_update_in_progress = True

    async def _perform_exe_update() -> None:
        global _app_update_in_progress, _force_app_exit, _instance_mutex_handle
        staging_dir = APP_DIR / ".update_staging"
        zip_path = staging_dir / "release_update.zip"
        extract_dir = staging_dir / "extracted"
        try:
            shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir.mkdir(parents=True, exist_ok=True)
            emit_sse("app_update_progress", {"stage": "downloading", "pct": 1, "message": "Đang kết nối GitHub Releases..."})

            allowed_hosts = ("github.com", "githubusercontent.com")
            headers = {"User-Agent": f"BlackSakuraTranslate/{_APP_VERSION}"}
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=120.0), follow_redirects=True) as client:
                async with client.stream("GET", dl_url, headers=headers) as resp:
                    host = (resp.url.host or "").lower()
                    if not any(host == h or host.endswith("." + h) for h in allowed_hosts):
                        raise RuntimeError(f"Blocked untrusted redirect host: {host}")
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length") or 0)
                    downloaded = 0
                    last_emit = time.perf_counter()
                    with open(zip_path, "wb") as out_f:
                        async for chunk in resp.aiter_bytes(chunk_size=131072):
                            if not chunk:
                                continue
                            out_f.write(chunk)
                            downloaded += len(chunk)
                            if downloaded > 350 * 1024 * 1024:
                                raise RuntimeError("Gói cập nhật vượt quá dung lượng an toàn.")
                            now_t = time.perf_counter()
                            if now_t - last_emit >= 0.3:
                                last_emit = now_t
                                pct = min(90, int(round((downloaded / total) * 90))) if total > 0 else 45
                                emit_sse(
                                    "app_update_progress",
                                    {
                                        "stage": "downloading",
                                        "pct": pct,
                                        "downloaded_mb": round(downloaded / (1024 * 1024), 1),
                                        "total_mb": round(total / (1024 * 1024), 1) if total > 0 else 0,
                                    },
                                )

            emit_sse("app_update_progress", {"stage": "extracting", "pct": 94, "message": "Đang giải nén & kiểm tra tính toàn vẹn..."})
            new_app_root = await asyncio.to_thread(_safe_extract_update_zip, zip_path, extract_dir)
            script_path = await asyncio.to_thread(_write_windows_update_script, new_app_root, APP_DIR, os.getpid(), staging_dir)

            emit_sse("app_update_progress", {"stage": "restarting", "pct": 100, "message": "Đang áp dụng bản mới & khởi động lại app..."})
            await asyncio.sleep(0.5)

            subprocess.Popen(
                ["cmd.exe", "/c", str(script_path)],
                cwd=str(staging_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **hidden_subprocess_kwargs(detached_group=True),
            )

            def _graceful_exit() -> None:
                global _force_app_exit, _instance_mutex_handle
                time.sleep(0.8)
                _force_app_exit = True
                if _tray_controller is not None:
                    try:
                        _tray_controller.stop()
                    except Exception:
                        pass
                if _wv_window is not None:
                    try:
                        _wv_window.destroy()
                    except Exception:
                        pass
                release_single_instance_lock(_instance_mutex_handle)
                _instance_mutex_handle = None
                os._exit(0)

            threading.Thread(target=_graceful_exit, daemon=True).start()
        except Exception as exc:
            logger.exception("Auto-update failed")
            shutil.rmtree(staging_dir, ignore_errors=True)
            emit_sse(
                "app_update_progress",
                {
                    "stage": "error",
                    "pct": 0,
                    "message": str(exc).strip() or type(exc).__name__,
                },
            )
        finally:
            _app_update_in_progress = False

    asyncio.create_task(_perform_exe_update())
    return {"ok": True, "started": True, "mode": "exe_zip"}


@app.post("/api/ui/hide-to-tray")
async def api_hide_to_tray():
    if _wv_window is None:
        return {"ok": False, "tray_active": False}
    threading.Thread(target=lambda: _hide_window_to_tray(True), daemon=True).start()
    return {"ok": True, "tray_active": True}


@app.post("/api/ui/exit")
async def api_ui_exit():
    if _wv_window is not None or _active_server is not None:
        threading.Thread(target=lambda: _perform_full_exit(destroy_window=True), daemon=True).start()
    return {"ok": True, "exiting": True}


@app.get("/api/ui/identity")
async def api_ui_identity():
    return {"ok": True, "app": _APP_IDENTITY, "pid": os.getpid()}


@app.post("/api/ui/activate")
async def api_ui_activate():
    if _wv_window is not None:
        threading.Thread(target=_restore_window_from_tray, daemon=True).start()
    return {
        "ok": True,
        "app": _APP_IDENTITY,
        "pid": os.getpid(),
        "activated": _wv_window is not None,
    }


@app.get("/api/tools/status")
async def api_tools_status():
    return tool_boot.get_status()


@app.post("/api/tools/bootstrap")
async def api_tools_bootstrap(force: bool = True):
    async def _run():
        res = await tool_boot.ensure_all(force=force, progress_cb=lambda info: emit_sse("tool_progress", info))
        emit_sse("tool_status", res["status"])
    asyncio.create_task(_run())
    return {"ok": True, "started": True}


# ---------------- Game Library & Launcher ---------------- #

@app.get("/api/steam/discover")
async def api_steam_discover():
    apps = await asyncio.to_thread(collect_steam_apps)
    existing_dirs = {os.path.normcase(g["game_dir"]) for g in library_db.list_games()}
    for a in apps:
        a["already_added"] = os.path.normcase(a["game_dir"]) in existing_dirs
    return {"apps": apps}


_cover_retry_after: Dict[str, float] = {}


def _pick_default_source_lang(slots: List[Dict[str, Any]]) -> str:
    non_empty_supported = [
        s for s in slots if int(s.get("count", 0) or 0) > 0 and is_qwen_supported_lang(s.get("slot_id", ""))
    ]
    if any(s["slot_id"] == "en" for s in non_empty_supported):
        return "en"
    if non_empty_supported:
        return non_empty_supported[0]["slot_id"]
    supported = [s for s in slots if is_qwen_supported_lang(s.get("slot_id", ""))]
    if any(s["slot_id"] == "en" for s in supported):
        return "en"
    if supported:
        return supported[0]["slot_id"]
    return slots[0]["slot_id"] if slots else "en"


async def _resolve_cover_url(game: Dict[str, Any], force_refresh: bool = False) -> str:
    gid = game["game_id"]
    cpath = game.get("cover_path") or ""
    if not force_refresh and cpath and Path(cpath).exists() and Path(cpath).stat().st_size > 1024:
        return f"/covers/{Path(cpath).name}"
    appid = (game.get("steam_appid") or "").strip()
    now = time.time()
    if appid and appid.isdigit() and (force_refresh or _cover_retry_after.get(gid, 0.0) <= now):
        downloaded = await download_steam_cover(gid, appid, covers_dir=COVERS_DIR)
        if downloaded and Path(downloaded).exists() and Path(downloaded).stat().st_size > 1024:
            library_db.update_fields(gid, cover_path=downloaded)
            game["cover_path"] = downloaded
            _cover_retry_after.pop(gid, None)
            return f"/covers/{Path(downloaded).name}?t={int(now)}"
        _cover_retry_after[gid] = now + 300.0
    return ""


def _enrich_game_skill_metadata(game: Dict[str, Any]) -> Dict[str, Any]:
    """Attach resolved 2-Layer Skill Hub metadata and detect skill mismatch against applied signature."""
    src = game.get("source_lang") or "en"
    tgt = game.get("target_lang") or "vi"
    eff_style = (game.get("style_skill_id") or "").strip() or (game.get("tone_preset") or "natural_standard")
    lang_ov = (game.get("lang_skill_override") or "auto").strip() or "auto"
    resolved = skill_manager.resolve_active_skills(
        source_lang=src,
        target_lang=tgt,
        style_skill_id=eff_style,
        lang_skill_override=lang_ov,
        source_lang_name=get_lang_display_name(src),
        target_lang_name=get_lang_display_name(tgt),
    )
    game["style_skill_id"] = resolved["style_skill"]["id"] if resolved.get("style_skill") else eff_style
    game["lang_skill_override"] = lang_ov
    applied_sig = game.get("applied_skill_signature") or ""
    game["applied_skill_signature"] = applied_sig
    game["current_skill_signature"] = resolved["skill_signature"]
    game["active_lang_skill"] = resolved.get("lang_skill")
    game["active_style_skill"] = resolved.get("style_skill")
    game["skill_tokens"] = resolved.get("combined_tokens", 0)
    done_cnt = int(game.get("translated_strings") or 0)
    game["skill_changed"] = bool(
        done_cnt > 0
        and (
            (applied_sig and applied_sig != resolved["skill_signature"])
            or (not applied_sig and game["style_skill_id"] != "natural_standard")
        )
    )
    return game


def _hydrate_saved_translation_flags(game: Dict[str, Any]) -> None:
    """Detect cached translations in cache/<game_id>.db so restored/re-added games still show 'Đã dịch'."""
    gid = game["game_id"]
    cache_db = GameCacheDB(gid, APP_DIR / "cache")
    saved_packs = cache_db.list_saved_translations()
    game["has_saved_translation"] = len(saved_packs) > 0
    game["saved_translations_count"] = len(saved_packs)
    game["saved_pct"] = max((float(p.get("pct") or 0.0) for p in saved_packs), default=0.0)
    game["interrupted_job"] = (
        None
        if _job_states.get(gid, {}).get("status") == "running"
        else library_db.get_job_journal(gid)
    )
    if saved_packs and (not game.get("total_strings") or not game.get("translated_strings")):
        src = game.get("source_lang") or "en"
        tgt = game.get("target_lang") or "vi"
        preferred = next((p for p in saved_packs if p["source_lang"] == src and p["target_lang"] == tgt), saved_packs[0])
        game["total_strings"] = int(preferred["total"])
        game["translated_strings"] = int(preferred["done"])
        game["progress_pct"] = float(preferred["pct"])
        library_db.update_fields(
            gid,
            total_strings=game["total_strings"],
            translated_strings=game["translated_strings"],
            progress_pct=game["progress_pct"],
        )
    game["update_watch"] = backup_mgr.check_game_update_status(
        gid, game.get("game_dir"), bool(game.get("is_patched"))
    )
    _enrich_game_skill_metadata(game)


@app.get("/api/games")
async def api_list_games():
    games = library_db.list_games()
    for g in games:
        gid = g["game_id"]
        _hydrate_saved_translation_flags(g)
        g["cover_url"] = await _resolve_cover_url(g)
        g["has_backup"] = backup_mgr.has_backup(gid, g.get("game_dir"))
        g["ranked_slots"] = rank_inject_slots(g.get("detected_slots", []), g.get("source_lang", "en"), g.get("target_lang", "vi"))
        g["job_running"] = _job_states.get(gid, {}).get("status") == "running"
        g["job_paused"] = qwen_engine.is_paused(gid)
    return {"games": games}


@app.post("/api/games/add")
async def api_add_game(req: AddGameRequest):
    try:
        profile = await ingest_game_input(req.path, covers_dir=COVERS_DIR)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Không thể nạp game: {exc}")

    slots = profile.get("detected_slots", [])
    source_lang = _pick_default_source_lang(slots)
    ranked = rank_inject_slots(slots, source_lang, "vi")
    default_inject = ranked[0]["slot_id"] if ranked else source_lang

    saved = library_db.upsert_game(
        {
            "game_id": profile["game_id"],
            "title": profile["title"],
            "game_dir": profile["game_dir"],
            "exe_path": profile["exe_path"],
            "steam_appid": profile["steam_appid"],
            "launch_mode": profile["launch_mode"],
            "shortcut_path": profile["shortcut_path"],
            "cover_path": profile["cover_path"],
            "game_description": profile.get("game_description", ""),
            "engine": profile["engine"],
            "method": profile["method"],
            "anticheat_status": profile["anticheat_status"],
            "anticheat_vendors": json.dumps(profile["anticheat_vendors"], ensure_ascii=False),
            "anticheat_files": json.dumps(profile["anticheat_files"], ensure_ascii=False),
            "detected_slots_json": json.dumps(slots, ensure_ascii=False),
            "source_lang": source_lang,
            "target_lang": "vi",
            "inject_slot": default_inject,
            "aes_key": profile.get("aes_key") or None,
            "is_aes_encrypted": int(profile.get("is_aes_encrypted", 0) or 0),
            "is_iostore": int(profile.get("is_iostore", 0) or 0),
            "has_pak_signature": int(profile.get("has_pak_signature", 0) or 0),
        }
    )
    saved["cover_url"] = await _resolve_cover_url(saved)
    saved["ranked_slots"] = ranked
    saved["has_backup"] = backup_mgr.has_backup(saved["game_id"], saved["game_dir"])
    _hydrate_saved_translation_flags(saved)
    return {
        "ok": True,
        "game": saved,
        "error_code": profile.get("error_code"),
        "shipping_exe_path": profile.get("shipping_exe_path", ""),
        "is_aes_encrypted": bool(saved.get("is_aes_encrypted")),
        "is_iostore": bool(saved.get("is_iostore")),
        "has_pak_signature": bool(saved.get("has_pak_signature")),
    }


@app.get("/api/games/{game_id}")
async def api_get_game(game_id: str):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    _hydrate_saved_translation_flags(game)
    game["cover_url"] = await _resolve_cover_url(game)
    game["has_backup"] = backup_mgr.has_backup(game_id, game.get("game_dir"))
    game["backup_info"] = backup_mgr.get_backup_info(game_id, game.get("game_dir"))
    game["ranked_slots"] = rank_inject_slots(game.get("detected_slots", []), game.get("source_lang", "en"), game.get("target_lang", "vi"))
    return {"game": game}


@app.delete("/api/games/{game_id}")
async def api_delete_game(game_id: str):
    game_id = _validate_safe_id(game_id, "game_id")
    game = library_db.get_game(game_id)
    library_db.delete_game(game_id)
    gdir = (game or {}).get("game_dir") or ""
    tmp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    is_temp_or_missing = (not gdir) or (not Path(gdir).exists()) or os.path.normcase(os.path.abspath(gdir)).startswith(tmp_root)
    if is_temp_or_missing:
        gc.collect()
        for suffix in (".db", ".db-wal", ".db-shm"):
            try:
                (APP_DIR / "cache" / f"{game_id}{suffix}").unlink(missing_ok=True)
            except OSError:
                pass
        for cov in (APP_DIR / "covers").glob(f"{game_id}.*"):
            try:
                cov.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(APP_DIR / "backups" / game_id, ignore_errors=True)
    return {"ok": True}


@app.patch("/api/games/{game_id}/profile")
async def api_update_profile(game_id: str, req: UpdateProfileRequest):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    for bool_col in ("translate_choices", "keep_proper_nouns", "patch_fonts"):
        if bool_col in updates:
            updates[bool_col] = 1 if updates[bool_col] else 0
    if "style_skill_id" in updates and "tone_preset" not in updates:
        updates["tone_preset"] = updates["style_skill_id"]
    elif "tone_preset" in updates and "style_skill_id" not in updates:
        updates["style_skill_id"] = updates["tone_preset"]

    # Force Phase 0 to recompile Style Card whenever user changes game_description, skill, or tone
    if any(k in updates for k in ("game_description", "tone_preset", "style_skill_id", "lang_skill_override")) and "compiled_style_card" not in updates:
        if (
            updates.get("game_description", game.get("game_description")) != game.get("game_description")
            or updates.get("tone_preset", game.get("tone_preset")) != game.get("tone_preset")
            or updates.get("style_skill_id", game.get("style_skill_id")) != game.get("style_skill_id")
            or updates.get("lang_skill_override", game.get("lang_skill_override")) != game.get("lang_skill_override")
        ):
            updates["compiled_style_card"] = ""
    updated = library_db.update_fields(game_id, **updates)
    _enrich_game_skill_metadata(updated)
    return {"ok": True, "game": updated}


@app.post("/api/games/{game_id}/fetch-context")
async def api_fetch_steam_context(game_id: str):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    appid = (game.get("steam_appid") or "").strip()
    if not appid:
        return {"ok": False, "game_description": game.get("game_description", ""), "message": "Game không có Steam AppID."}
    desc = await fetch_steam_app_context(appid)
    if desc:
        updated = library_db.update_fields(game_id, game_description=desc, compiled_style_card="")
        return {"ok": True, "game_description": desc, "game": updated}
    return {"ok": False, "game_description": game.get("game_description", ""), "message": "Không lấy được mô tả từ Steam Store (Offline hoặc AppID ẩn)."}


@app.post("/api/games/{game_id}/cover")
async def api_upload_cover(game_id: str, file: UploadFile = File(...)):
    game_id = _validate_safe_id(game_id, "game_id")
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    raw_cover = await file.read(_MAX_COVER_BYTES + 1)
    if len(raw_cover) > _MAX_COVER_BYTES:
        raise HTTPException(status_code=413, detail="Ảnh bìa vượt quá giới hạn 10 MB.")
    is_png = raw_cover.startswith(b"\x89PNG\r\n\x1a\n")
    is_jpg = raw_cover.startswith(b"\xff\xd8\xff")
    is_webp = len(raw_cover) >= 12 and raw_cover[:4] == b"RIFF" and raw_cover[8:12] == b"WEBP"
    if not (is_png or is_jpg or is_webp):
        raise HTTPException(status_code=400, detail="Chỉ hỗ trợ định dạng ảnh PNG, JPG hoặc WEBP hợp lệ.")
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    ext = ".png" if is_png else ".webp" if is_webp else ".jpg"
    dest = COVERS_DIR / f"{game_id}{ext}"
    dest.write_bytes(raw_cover)
    library_db.update_fields(game_id, cover_path=str(dest))
    return {"ok": True, "cover_url": f"/covers/{dest.name}?t={int(time.time())}"}


@app.post("/api/games/{game_id}/slots")
async def api_rank_slots(game_id: str, req: SlotRankRequest):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    ranked = rank_inject_slots(game.get("detected_slots", []), req.source_lang, req.target_lang)
    valid_ids = {s["slot_id"] for s in ranked}
    cur_slot = (req.current_inject_slot or game.get("inject_slot") or "").strip()
    if cur_slot and cur_slot in valid_ids and (cur_slot.lower() != req.source_lang.lower() or len(ranked) == 1):
        chosen_slot = cur_slot
    else:
        chosen_slot = ranked[0]["slot_id"] if ranked else (cur_slot or req.source_lang)
    if req.persist:
        library_db.update_fields(
            game_id,
            source_lang=req.source_lang,
            target_lang=req.target_lang,
            inject_slot=chosen_slot,
        )
    return {"ranked_slots": ranked, "inject_slot": chosen_slot}


def _game_export_dir(game_id: str) -> Path:
    return APP_DIR / "exports" / _validate_safe_id(game_id, "game_id")


def _game_export_slug(game: Dict[str, Any], game_id: str) -> str:
    raw = (game.get("title") or game_id or "game")
    slug = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(raw)).strip("_")
    return slug or str(game_id)


def _sync_external_source_snapshot(
    game_id: str,
    game: Dict[str, Any],
    cache_db: GameCacheDB,
    src_lang: str,
    tgt_lang: str,
) -> Dict[str, Any]:
    meta = {
        "game_id": game_id,
        "title": game.get("title") or game_id,
        "engine": game.get("engine") or "",
        "steam_appid": game.get("steam_appid") or "",
    }
    snap = cache_db.write_external_source_snapshot(
        export_dir=_game_export_dir(game_id),
        safe_slug=_game_export_slug(game, game_id),
        source_lang=src_lang,
        target_lang=tgt_lang,
        game_meta=meta,
    )
    if snap:
        return snap
    return {
        "available": False,
        "dir_path": str(_game_export_dir(game_id)),
        "source_lang": src_lang,
        "target_lang": tgt_lang,
        "total_strings": 0,
    }


def _get_or_sync_external_source_info(
    game_id: str,
    game: Dict[str, Any],
    cache_db: GameCacheDB,
    src_lang: str,
    tgt_lang: str,
) -> Dict[str, Any]:
    export_dir = _game_export_dir(game_id)
    slug = _game_export_slug(game, game_id)
    json_path = export_dir / f"{slug}_{src_lang}_source.json"
    txt_path = export_dir / f"{slug}_{src_lang}_source.txt"
    prog = cache_db.get_progress(src_lang, tgt_lang)
    total = int(prog.get("total") or 0)
    if total <= 0:
        return {
            "available": False,
            "dir_path": str(export_dir),
            "source_lang": src_lang,
            "target_lang": tgt_lang,
            "total_strings": 0,
        }
    if json_path.exists() and txt_path.exists():
        try:
            st = json_path.stat()
            return {
                "available": True,
                "dir_path": str(export_dir),
                "json_path": str(json_path),
                "txt_path": str(txt_path),
                "json_filename": json_path.name,
                "txt_filename": txt_path.name,
                "source_lang": src_lang,
                "target_lang": tgt_lang,
                "total_strings": total,
                "size_bytes": int(st.st_size),
                "updated_at": int(st.st_mtime),
            }
        except OSError:
            pass
    return _sync_external_source_snapshot(game_id, game, cache_db, src_lang, tgt_lang)


async def _perform_game_scan(
    game_id: str,
    game: Dict[str, Any],
    source_lang: Optional[str] = None,
    target_lang: Optional[str] = "vi",
    scope: str = "FULL_PATCH",
    translate_choices: bool = True,
    aes_key: Optional[str] = None,
) -> Dict[str, Any]:
    game_dir = game["game_dir"]
    eff_aes_key = (aes_key if aes_key is not None else game.get("aes_key")) or ""
    if eff_aes_key.strip():
        try:
            eff_aes_key = normalize_aes_key(eff_aes_key)
        except ValueError:
            eff_aes_key = eff_aes_key.strip()
    ac_report = await asyncio.to_thread(scan_anticheat, game_dir)
    loc_report = await asyncio.to_thread(scan_game_languages, game_dir, eff_aes_key)
    slots = loc_report.get("slots", [])
    final_aes_key = loc_report.get("discovered_aes_key") or eff_aes_key.strip()

    src_lang = source_lang or game.get("source_lang") or "en"
    slot_map = {s["slot_id"]: s for s in slots}
    if slots and (src_lang not in slot_map or not is_qwen_supported_lang(src_lang)):
        src_lang = _pick_default_source_lang(slots)
    tgt_lang = target_lang or game.get("target_lang") or "vi"
    if not is_qwen_supported_lang(tgt_lang):
        tgt_lang = "vi"

    extract_stats = {"raw_count": 0, "unique_count": 0, "dedup_saved_pct": 0.0, "ui_count": 0, "sub_count": 0, "tm_hits": 0, "estimates": {}}
    external_source_info: Dict[str, Any] = {
        "available": False,
        "dir_path": str(_game_export_dir(game_id)),
        "source_lang": src_lang,
        "target_lang": tgt_lang,
        "total_strings": 0,
    }
    if src_lang in slot_map:
        extracted = await asyncio.to_thread(
            backup_mgr.extract_entries_preferring_backup,
            game_id,
            game_dir,
            slot_map[src_lang],
            extract_slot_entries,
            scope,
            translate_choices,
        )
        full_extracted = (
            extracted
            if scope == "FULL_PATCH"
            else await asyncio.to_thread(
                backup_mgr.extract_entries_preferring_backup,
                game_id,
                game_dir,
                slot_map[src_lang],
                extract_slot_entries,
                "FULL_PATCH",
                True,
            )
        )
        cache_db = GameCacheDB(game_id, APP_DIR / "cache")
        dedup = cache_db.store_extracted_entries(extracted["entries"], src_lang, tgt_lang, mask_tags)
        external_source_info = await asyncio.to_thread(
            _sync_external_source_snapshot,
            game_id,
            game,
            cache_db,
            src_lang,
            tgt_lang,
        )
        pending = cache_db.get_pending_strings(src_lang, tgt_lang)
        tm_hit_map = global_tm.lookup_exact_batch([e.text for e in full_extracted["entries"]], src_lang, tgt_lang)
        has_gloss = bool(cache_db.list_glossary_terms(src_lang, tgt_lang))
        estimates = estimate_preset_durations(
            full_extracted["entries"], set(tm_hit_map.keys()), has_glossary=has_gloss, profiler=hw_profiler
        )
        tm_hits = len(global_tm.lookup_exact_batch([p["source_text"] for p in pending], src_lang, tgt_lang))
        prog = cache_db.get_progress(src_lang, tgt_lang)
        extract_stats = {
            "raw_count": extracted["total_raw"],
            "filtered_count": extracted["filtered_count"],
            "exact_unique_count": dedup.get("exact_unique_count", dedup["unique_count"]),
            "unique_count": dedup["unique_count"],
            "num_folded_saved": dedup.get("num_folded_saved", 0),
            "dedup_saved_pct": dedup["dedup_saved_pct"],
            "ui_count": extracted["ui_count"],
            "sub_count": extracted["sub_count"],
            "tm_hits": tm_hits,
            "progress_pct": prog["pct"],
            "done": prog["done"],
            "estimates": estimates,
        }

    ranked = rank_inject_slots(slots, src_lang, tgt_lang)
    inject_slot = game.get("inject_slot") or (ranked[0]["slot_id"] if ranked else src_lang)

    is_aes_enc = 1 if (loc_report.get("is_aes_encrypted") or final_aes_key) else int(game.get("is_aes_encrypted") or 0)
    is_iostore = 1 if loc_report.get("is_iostore") else 0
    has_sig = 1 if loc_report.get("has_pak_signature") else 0

    updated = library_db.update_fields(
        game_id,
        engine=loc_report.get("engine", game["engine"]),
        method=loc_report.get("method", game["method"]),
        anticheat_status=ac_report["risk_level"],
        anticheat_vendors=ac_report["detected_ac"],
        anticheat_files=ac_report["matched_files"],
        detected_slots_json=slots,
        source_lang=src_lang,
        target_lang=tgt_lang,
        inject_slot=inject_slot,
        scope=scope,
        aes_key=final_aes_key or None,
        is_aes_encrypted=is_aes_enc,
        is_iostore=is_iostore,
        has_pak_signature=has_sig,
        total_strings=extract_stats["unique_count"],
        translated_strings=extract_stats.get("done", 0),
        progress_pct=extract_stats.get("progress_pct", 0.0),
    )
    _hydrate_saved_translation_flags(updated)
    updated["ranked_slots"] = ranked
    updated["has_backup"] = backup_mgr.has_backup(game_id, game_dir)
    updated["backup_info"] = backup_mgr.get_backup_info(game_id, game_dir)
    updated["cover_url"] = await _resolve_cover_url(updated, force_refresh=not bool(updated.get("cover_path") and Path(updated["cover_path"]).exists()))
    return {
        "ok": True,
        "game": updated,
        "scan_stats": extract_stats,
        "external_source": external_source_info,
        "anticheat": ac_report,
        "error_code": loc_report.get("error_code"),
        "shipping_exe_path": loc_report.get("shipping_exe_path", ""),
        "is_aes_encrypted": bool(is_aes_enc),
        "is_iostore": bool(is_iostore),
        "has_pak_signature": bool(has_sig),
    }


@app.post("/api/games/{game_id}/scan")
async def api_scan_game(game_id: str, req: ScanRequest):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    return await _perform_game_scan(
        game_id=game_id,
        game=game,
        source_lang=req.source_lang,
        target_lang=req.target_lang,
        scope=req.scope,
        translate_choices=req.translate_choices,
        aes_key=req.aes_key,
    )


@app.post("/api/games/{game_id}/unreal/aes-key")
async def api_unreal_save_aes_key(game_id: str, req: UnrealAesKeyRequest):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    try:
        norm_key = normalize_aes_key(req.aes_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    vr = await asyncio.to_thread(verify_unreal_aes_key, game["game_dir"], norm_key)
    if not vr.get("ok"):
        raise HTTPException(status_code=400, detail=vr.get("error") or "AES Key không khớp với file mã hóa của game này.")

    library_db.update_fields(
        game_id,
        aes_key=norm_key,
        is_aes_encrypted=1,
        is_iostore=1 if vr.get("is_iostore") else 0,
        has_pak_signature=1 if vr.get("has_pak_signature") else 0,
    )
    refreshed_game = library_db.get_game(game_id) or game
    res = await _perform_game_scan(
        game_id=game_id,
        game=refreshed_game,
        source_lang=refreshed_game.get("source_lang"),
        target_lang=refreshed_game.get("target_lang") or "vi",
        scope=refreshed_game.get("scope") or "FULL_PATCH",
        translate_choices=bool(refreshed_game.get("translate_choices", 1)),
        aes_key=norm_key,
    )
    res["aes_key"] = norm_key
    return res


@app.post("/api/games/{game_id}/unreal/auto-dump-aes")
async def api_unreal_auto_dump_aes(game_id: str):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    try:
        await tool_boot.ensure_tool("aesdumpster", force=False)
    except Exception:
        pass
    dump_res = await asyncio.to_thread(auto_dump_unreal_aes_keys, game["game_dir"])
    if dump_res.get("ok") and dump_res.get("aes_key"):
        found_key = dump_res["aes_key"]
        library_db.update_fields(
            game_id,
            aes_key=found_key,
            is_aes_encrypted=1,
            is_iostore=1 if dump_res.get("is_iostore") else 0,
            has_pak_signature=1 if dump_res.get("has_pak_signature") else 0,
        )
        refreshed_game = library_db.get_game(game_id) or game
        scan_res = await _perform_game_scan(
            game_id=game_id,
            game=refreshed_game,
            source_lang=refreshed_game.get("source_lang"),
            target_lang=refreshed_game.get("target_lang") or "vi",
            scope=refreshed_game.get("scope") or "FULL_PATCH",
            translate_choices=bool(refreshed_game.get("translate_choices", 1)),
            aes_key=found_key,
        )
        scan_res["aes_key"] = found_key
        return scan_res

    library_db.update_fields(
        game_id,
        is_aes_encrypted=1,
        is_iostore=1 if dump_res.get("is_iostore") else 0,
        has_pak_signature=1 if dump_res.get("has_pak_signature") else 0,
    )
    return {
        "ok": False,
        "error_code": "AES_KEY_REQUIRED",
        "shipping_exe_path": dump_res.get("shipping_exe_path", ""),
        "is_iostore": bool(dump_res.get("is_iostore")),
        "has_pak_signature": bool(dump_res.get("has_pak_signature")),
        "message": dump_res.get("message") or "Không tìm thấy AES Key tự động (file .exe có thể bị bọc DRM). Hãy nhập khóa thủ công.",
    }


@app.post("/api/games/{game_id}/open-binaries")
async def api_open_game_binaries(game_id: str):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    info = await asyncio.to_thread(inspect_unreal_paks, game["game_dir"])
    ship_exe = info.get("shipping_exe_path") or ""
    gdir = Path(game["game_dir"])
    target_dir = gdir
    if ship_exe and Path(ship_exe).exists():
        win64_exe = str(Path(ship_exe).resolve())
        try:
            subprocess.Popen(["explorer", f"/select,{win64_exe}"])
            return {"ok": True, "path": win64_exe, "selected": True}
        except Exception:
            target_dir = Path(win64_exe).parent
    else:
        for pat in ("Binaries/Win64", "*/Binaries/Win64"):
            hits = [p for p in gdir.glob(pat) if p.is_dir()]
            if hits:
                target_dir = hits[0]
                break
    if not target_dir.exists():
        raise HTTPException(status_code=404, detail="Không tìm thấy thư mục Binaries/Win64 của game.")
    os.startfile(str(target_dir))
    return {"ok": True, "path": str(target_dir), "selected": False}


@app.post("/api/games/{game_id}/launch")
async def api_launch_game(game_id: str):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    try:
        res = await launch_game_safely(game, qwen_engine)
        library_db.update_fields(game_id, last_played_at=int(time.time()))
        emit_sse("vram_flushed", {"game_id": game_id, "telemetry": qwen_engine.get_telemetry()})
        return res
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Lỗi khởi chạy game: {exc}")


@app.post("/api/games/{game_id}/restore")
async def api_restore_game(game_id: str):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    res = await asyncio.to_thread(backup_mgr.restore_original, game_id, game["game_dir"])
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Không thể khôi phục bản gốc."))
    library_db.update_fields(game_id, is_patched=0)
    emit_sse("game_restored", {"game_id": game_id, **res})
    return res


@app.post("/api/games/{game_id}/open-backup")
async def api_open_backup_dir(game_id: str):
    game_id = _validate_safe_id(game_id, "game_id")
    game = library_db.get_game(game_id)
    info = backup_mgr.get_backup_info(game_id, game.get("game_dir") if game else None)
    bdir = Path(info["backup_dir"]).resolve()
    backups_root = (APP_DIR / "backups").resolve()
    if not bdir.is_relative_to(backups_root):
        raise HTTPException(status_code=400, detail="Đường dẫn thư mục backup không hợp lệ.")
    bdir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(bdir))
    return {"ok": True, "path": str(bdir)}


@app.delete("/api/games/{game_id}/interrupted-job")
async def api_clear_interrupted_job(game_id: str):
    library_db.clear_job_journal(game_id)
    return {"ok": True}


@app.post("/api/games/{game_id}/repair")
async def api_repair_game(game_id: str, req: RepairRequest = RepairRequest()):
    """Post-translation 1-Click Error Fixer:
    1. Audit & strip <think>, recover raw __TAG_x__/__NL__, reset FALLBACK/tag-broken rows.
    2. Re-translate pending/broken rows if Ollama is online and retry_with_ai=True.
    3. Fix Steam DRM black-screen (direct_exe -> steam when steam_appid exists).
    4. Re-apply translations, sync Unity Addressables catalog.bin CRC/Size, validate XML/JSON syntax, optionally patch fonts.
    """
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    ac_report = await asyncio.to_thread(scan_anticheat, game["game_dir"])
    if ac_report["risk_level"] == "BLOCKED" and not req.force_offline_override:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ANTICHEAT_BLOCKED",
                "message": ac_report["message"],
                "detected_ac": ac_report["detected_ac"],
            },
        )

    if _job_states.get(game_id, {}).get("status") == "running":
        raise HTTPException(status_code=409, detail="Tiến trình cho game này đang chạy.")

    src = game.get("source_lang") or "en"
    tgt = game.get("target_lang") or "vi"
    inject_slot = game.get("inject_slot") or src
    scope = game.get("scope") or "FULL_PATCH"
    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    qwen_engine.reset_pause_state(game_id)

    _job_states[game_id] = {"status": "running", "mode": "REPAIR", "started_at": time.time()}
    library_db.set_job_journal(
        game_id,
        {
            "mode": "REPAIR",
            "source_lang": src,
            "target_lang": tgt,
            "inject_slot": inject_slot,
            "fix_fonts": bool(req.fix_fonts),
            "started_at": int(time.time()),
        },
    )

    try:
        # Step 0: Sync clean source entries from original_backup.zip (recovers newly unmasked [brackets] & prevents overwritten source loops)
        slots = game.get("detected_slots", [])
        slot_map = {s["slot_id"]: s for s in slots}
        pre_extracted: Optional[Dict[str, Any]] = None
        if src in slot_map:
            pre_extracted = await asyncio.to_thread(
                backup_mgr.extract_entries_preferring_backup,
                game_id,
                game["game_dir"],
                slot_map[src],
                extract_slot_entries,
                scope,
                True,
            )
            if pre_extracted.get("entries"):
                cache_db.store_extracted_entries(pre_extracted["entries"], src, tgt, mask_tags)

        # Step 1: Audit cache & recover corrupted tags / <think> / mojibake / pipe-split / CJK & French leaks
        emit_sse(
            "progress",
            {
                "game_id": game_id,
                "weighted_pct": 5.0,
                "phase": "REPAIR_AUDIT",
                "phase_label": "Fix lỗi • Đang quét lỗi thẻ tag, UTF-8 & từ vô nghĩa...",
                "tokens_done": 0,
                "tok_per_sec": 0.0,
                "telemetry": await asyncio.to_thread(qwen_engine.get_telemetry),
            },
        )
        audit = await asyncio.to_thread(
            cache_db.audit_and_repair_cache,
            src,
            tgt,
            strip_thinking,
            validate_and_unmask,
            anomaly_check_fn=detect_translation_anomaly,
            mojibake_fix_fn=try_fix_mojibake,
        )
        if audit.get("reset_sources"):
            global_tm.delete_by_sources(audit["reset_sources"], src, tgt)
        global_tm.purge_anomalies(detect_translation_anomaly, src, tgt)

        # Step 2: Re-translate faulty/remaining items & run AI QA polish pass ONLY if game already has translated progress
        retranslated = 0
        qa_res: Dict[str, Any] = {"reviewed": 0, "polished": 0, "samples": []}
        ollama_st = await qwen_engine.check_ollama_status()
        had_done = cache_db.get_progress(src, tgt)["done"] > 0
        pending_after = cache_db.get_pending_strings(src, tgt) if (audit["reset_pending"] > 0 or had_done) else []
        if req.retry_with_ai and (pending_after or (req.polish_awkward and had_done)):
            if not ollama_st.get("online"):
                await qwen_engine.ensure_ollama_running()
                ollama_st = await qwen_engine.check_ollama_status()
            if ollama_st.get("online"):
                rep_style = req.style_skill_id or game.get("style_skill_id") or game.get("tone_preset") or "natural_standard"
                rep_lang_ov = req.lang_skill_override or game.get("lang_skill_override") or "auto"
                if pending_after:
                    res_ai = await qwen_engine.run_translation_pipeline(
                        game_id=game_id,
                        source_lang=src,
                        target_lang=tgt,
                        scope=scope,
                        ai_mode="AUTO_HYBRID",
                        keep_proper_nouns=True,
                        cache_db=cache_db,
                        global_tm=global_tm,
                        emit_cb=lambda ev, p: emit_sse(ev, {"game_id": game_id, **p}),
                        game_description=game.get("game_description", ""),
                        tone_preset=game.get("tone_preset") or "natural",
                        compiled_style_card=game.get("compiled_style_card", ""),
                        style_card_cb=lambda card: library_db.update_fields(game_id, compiled_style_card=card),
                        style_skill_id=rep_style,
                        lang_skill_override=rep_lang_ov,
                        bypass_global_tm=True,
                    )
                    retranslated = int(res_ai.get("llm_translated", 0)) + int(res_ai.get("tm_hits", 0))
                if req.polish_awkward and cache_db.get_progress(src, tgt)["done"] > 0:
                    qa_res = await qwen_engine.qa_polish_translations(
                        cache_db=cache_db,
                        global_tm=global_tm,
                        source_lang=src,
                        target_lang=tgt,
                        emit_cb=lambda ev, p: emit_sse(ev, {"game_id": game_id, **p}),
                        game_description=game.get("game_description", ""),
                        tone_preset=game.get("tone_preset") or "natural",
                        compiled_style_card=game.get("compiled_style_card", ""),
                        game_id=game_id,
                        style_skill_id=rep_style,
                        lang_skill_override=rep_lang_ov,
                    )

        # Step 3: Repair Launch Mode (avoid Steamworks black screen on direct_exe)
        fixed_launch_mode = False
        if game.get("steam_appid") and game.get("launch_mode") == "direct_exe":
            library_db.update_fields(game_id, launch_mode="steam")
            fixed_launch_mode = True

        # Step 4: Re-apply translations & sync catalog.bin / validate external file syntax
        slots = game.get("detected_slots", [])
        slot_map = {s["slot_id"]: s for s in slots}
        write_res: Dict[str, Any] = {"ok": False, "updated": 0, "catalog_synced": False}
        syntax_errors: List[str] = []

        def _do_backup(mod_files, created_files):
            backup_mgr.backup_files(game_id, game["game_dir"], mod_files, created_files)

        if src in slot_map and inject_slot in slot_map:
            emit_sse(
                "progress",
                {
                    "game_id": game_id,
                    "weighted_pct": 95.0,
                    "phase": "REPAIR_APPLY",
                    "phase_label": "Fix lỗi • Đang ghi lại file game & đồng bộ catalog.bin...",
                    "telemetry": await asyncio.to_thread(qwen_engine.get_telemetry),
                },
            )
            extracted = pre_extracted or await asyncio.to_thread(
                backup_mgr.extract_entries_preferring_backup,
                game_id,
                game["game_dir"],
                slot_map[src],
                extract_slot_entries,
                scope,
                True,
            )
            all_tr = cache_db.get_all_translations(src, tgt)
            if all_tr:
                write_res = await asyncio.to_thread(
                    apply_slot_translations,
                    game["game_dir"],
                    extracted["entries"],
                    all_tr,
                    slot_map[inject_slot],
                    _do_backup,
                    tgt,
                    src,
                )

            # Syntax check external target files (.json/.xml)
            for fp in slot_map[inject_slot].get("files", []):
                p = Path(fp)
                if p.exists() and p.suffix.lower() in (".json", ".xml"):
                    ok_syn, syn_msg = validate_serialized_syntax(p.suffix, p.read_text(encoding="utf-8-sig", errors="replace"))
                    if not ok_syn:
                        syntax_errors.append(f"{p.name}: {syn_msg}")

        font_res = {"applied": False}
        if req.fix_fonts:
            font_res = await asyncio.to_thread(
                font_patcher.apply_font_patch, game["game_dir"], game["engine"], inject_slot, _do_backup
            )
        if write_res.get("ok"):
            await asyncio.to_thread(backup_mgr.record_patched_state, game_id, game["game_dir"])

        prog = cache_db.get_progress(src, tgt)
        library_db.update_fields(
            game_id,
            total_strings=prog["total"],
            translated_strings=prog["done"],
            progress_pct=prog["pct"],
            is_patched=1 if write_res.get("ok") else game.get("is_patched", 0),
        )

        queued_cnt = len(pending_after)
        summary = [
            f"Kiểm tra {audit['total_checked']} câu: dọn {audit['cleaned_think']} <think>, khôi phục {audit['unmasked_repaired']} thẻ tag, sửa {audit.get('mojibake_repaired', 0)} lỗi bảng mã UTF-8.",
            f"Đặt lại {audit['reset_pending']} câu lỗi mới ({audit.get('gibberish_reset', 0)} từ vô nghĩa/lọt ngoại ngữ) • hàng chờ sửa {queued_cnt} câu • đã dịch lại {retranslated} câu"
            + (" (Ollama đang Offline nên giữ lại hàng chờ)." if (queued_cnt > 0 and not ollama_st.get("online")) else "."),
        ]
        if fixed_launch_mode:
            summary.append("Đã chuyển Launch Mode về Steam Launcher để chống lỗi đen màn hình.")
        if qa_res.get("reviewed"):
            summary.append(
                f"AI QA rà {qa_res['reviewed']} câu: chuốt mượt lại {qa_res['polished']} câu gượng / lệch nghĩa / sót từ nối."
            )
        if write_res.get("ok"):
            extra = " + đã đồng bộ catalog.bin" if write_res.get("catalog_synced") else ""
            if write_res.get("language_label_renamed"):
                extra += " + đã đổi tên hiển thị slot thành Tiếng Việt"
            summary.append(f"Đã ghi lại {write_res.get('updated', 0)} câu vào slot [{inject_slot}]{extra}.")
        if font_res.get("applied"):
            patched_unity = font_res.get("patched_unity_fonts") or []
            extra_f = f" (thay fallback: {', '.join(patched_unity)})" if patched_unity else ""
            summary.append(f"Đã vá Font Unicode ({font_res.get('font_used')}){extra_f}.")
        if syntax_errors:
            summary.append(f"Cảnh báo cú pháp file: {'; '.join(syntax_errors)}")

        emit_sse(
            "repair_complete",
            {
                "game_id": game_id,
                "progress": prog,
                "tokens_done": int(qa_res.get("tokens_done", 0) or 0),
                "telemetry": await asyncio.to_thread(qwen_engine.get_telemetry),
            },
        )

        return {
            "ok": len(syntax_errors) == 0,
            "audit": audit,
            "retranslated": retranslated,
            "qa_polish": qa_res,
            "ollama_online": bool(ollama_st.get("online")),
            "fixed_launch_mode": fixed_launch_mode,
            "write_result": write_res,
            "font_result": font_res,
            "syntax_errors": syntax_errors,
            "summary": summary,
        }
    finally:
        _job_states[game_id] = {"status": "done"}
        library_db.clear_job_journal(game_id)
        try:
            cache_db.checkpoint()
        except Exception:
            pass


# ---------------- Translation Pipeline & Anti-Cheat Gate ---------------- #

@app.post("/api/translate/start")
async def api_translate_start(req: StartTranslateRequest):
    game = library_db.get_game(req.game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    # LAYER 1 HARD GATE: Always re-verify Anti-Cheat before modifying ANY file
    ac_report = await asyncio.to_thread(scan_anticheat, game["game_dir"])
    library_db.update_fields(
        req.game_id,
        anticheat_status=ac_report["risk_level"],
        anticheat_vendors=ac_report["detected_ac"],
        anticheat_files=ac_report["matched_files"],
    )
    if ac_report["risk_level"] == "BLOCKED" and not req.force_offline_override:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ANTICHEAT_BLOCKED",
                "message": ac_report["message"],
                "detected_ac": ac_report["detected_ac"],
                "matched_files": ac_report["matched_files"],
            },
        )

    if _job_states.get(req.game_id, {}).get("status") == "running":
        raise HTTPException(status_code=409, detail="Tiến trình dịch cho game này đang chạy.")

    slots = game.get("detected_slots", [])
    slot_map = {s["slot_id"]: s for s in slots}
    if req.source_lang not in slot_map or req.inject_slot not in slot_map:
        raise HTTPException(status_code=400, detail="Slot ngôn ngữ nguồn hoặc slot đích không hợp lệ. Hãy quét lại game.")
    if not is_qwen_supported_lang(req.source_lang):
        raise HTTPException(
            status_code=400,
            detail=f"Ngôn ngữ đầu vào [{req.source_lang}] chưa nằm trong danh sách hỗ trợ chuẩn của Local Engine.",
        )
    if not is_qwen_supported_lang(req.target_lang):
        raise HTTPException(
            status_code=400,
            detail=f"Ngôn ngữ đầu ra [{req.target_lang}] chưa nằm trong danh sách hỗ trợ chuẩn của Local Engine.",
        )

    if req.preset_level and req.preset_level in PRESET_PROFILES:
        prof = PRESET_PROFILES[req.preset_level]
        req.ai_mode = prof["ai_mode"]
        req.scope = prof["scope"]
        req.translate_choices = prof["translate_choices"]
        req.keep_proper_nouns = prof["keep_proper_nouns"]

    eff_style = req.style_skill_id or game.get("style_skill_id") or game.get("tone_preset") or "natural_standard"
    eff_lang_ov = req.lang_skill_override or game.get("lang_skill_override") or "auto"
    library_db.update_fields(
        req.game_id,
        source_lang=req.source_lang,
        target_lang=req.target_lang,
        inject_slot=req.inject_slot,
        scope=req.scope,
        preset_level=(req.preset_level or "CUSTOM"),
        ai_mode=req.ai_mode,
        translate_choices=1 if req.translate_choices else 0,
        keep_proper_nouns=1 if req.keep_proper_nouns else 0,
        patch_fonts=1 if req.patch_fonts else 0,
        style_skill_id=eff_style,
        tone_preset=eff_style,
        lang_skill_override=eff_lang_ov,
    )

    _job_states[req.game_id] = {"status": "running", "mode": "TRANSLATE", "started_at": time.time()}
    library_db.set_job_journal(
        req.game_id,
        {
            "mode": "TRANSLATE",
            "source_lang": req.source_lang,
            "target_lang": req.target_lang,
            "inject_slot": req.inject_slot,
            "scope": req.scope,
            "ai_mode": req.ai_mode,
            "patch_fonts": bool(req.patch_fonts),
            "style_skill_id": eff_style,
            "lang_skill_override": eff_lang_ov,
            "started_at": int(time.time()),
        },
    )

    async def _worker():
        cache_db = GameCacheDB(req.game_id, APP_DIR / "cache")
        try:
            def _emit(ev: str, payload: Dict[str, Any]):
                emit_sse(ev, {"game_id": req.game_id, **payload})

            extracted = await asyncio.to_thread(
                backup_mgr.extract_entries_preferring_backup,
                req.game_id,
                game["game_dir"],
                slot_map[req.source_lang],
                extract_slot_entries,
                req.scope,
                req.translate_choices,
            )
            entries = extracted["entries"]
            cache_db.store_extracted_entries(entries, req.source_lang, req.target_lang, mask_tags)

            fresh_game = library_db.get_game(req.game_id) or game
            active_style = eff_style or fresh_game.get("style_skill_id") or fresh_game.get("tone_preset") or "natural_standard"
            active_lang_ov = eff_lang_ov or fresh_game.get("lang_skill_override") or "auto"
            res = await qwen_engine.run_translation_pipeline(
                game_id=req.game_id,
                source_lang=req.source_lang,
                target_lang=req.target_lang,
                scope=req.scope,
                ai_mode=req.ai_mode,
                keep_proper_nouns=req.keep_proper_nouns,
                cache_db=cache_db,
                global_tm=global_tm,
                emit_cb=_emit,
                game_description=fresh_game.get("game_description", ""),
                tone_preset=fresh_game.get("tone_preset") or "natural",
                compiled_style_card=fresh_game.get("compiled_style_card", ""),
                style_card_cb=lambda card: library_db.update_fields(req.game_id, compiled_style_card=card),
                style_skill_id=active_style,
                lang_skill_override=active_lang_ov,
                bypass_global_tm=bool(req.bypass_global_tm),
            )

            all_tr = cache_db.get_all_translations(req.source_lang, req.target_lang)

            def _do_backup(mod_files, created_files):
                backup_mgr.backup_files(req.game_id, game["game_dir"], mod_files, created_files)

            write_res = await asyncio.to_thread(
                apply_slot_translations,
                game["game_dir"],
                entries,
                all_tr,
                slot_map[req.inject_slot],
                _do_backup,
                req.target_lang,
                req.source_lang,
            )

            font_res = {"applied": False}
            if req.patch_fonts:
                font_res = await asyncio.to_thread(
                    font_patcher.apply_font_patch,
                    game["game_dir"],
                    game["engine"],
                    req.inject_slot,
                    _do_backup,
                )
            if write_res.get("ok"):
                await asyncio.to_thread(backup_mgr.record_patched_state, req.game_id, game["game_dir"])

            resolved_sig = skill_manager.resolve_active_skills(
                source_lang=req.source_lang,
                target_lang=req.target_lang,
                style_skill_id=active_style,
                lang_skill_override=active_lang_ov,
                source_lang_name=get_lang_display_name(req.source_lang),
                target_lang_name=get_lang_display_name(req.target_lang),
            ).get("skill_signature", "")

            prog = res["progress"]
            library_db.update_fields(
                req.game_id,
                total_strings=prog["total"],
                translated_strings=prog["done"],
                progress_pct=prog["pct"],
                is_patched=1 if write_res.get("ok") else 0,
                applied_skill_signature=resolved_sig,
            )
            _job_states[req.game_id] = {"status": "done"}
            emit_sse(
                "complete",
                {
                    "game_id": req.game_id,
                    "progress": prog,
                    "write_result": write_res,
                    "font_result": font_res,
                    "applied_skill_signature": resolved_sig,
                    "backup_info": backup_mgr.get_backup_info(req.game_id, game["game_dir"]),
                },
            )
        except Exception as exc:
            logger.exception("Translation pipeline failed for %s", req.game_id)
            err_msg = str(exc).strip() or f"{type(exc).__name__} (Ollama phản hồi quá thời gian hoặc mất kết nối)"
            _job_states[req.game_id] = {"status": "error", "error": err_msg}
            emit_sse("error", {"game_id": req.game_id, "message": err_msg})
        finally:
            library_db.clear_job_journal(req.game_id)
            try:
                cache_db.checkpoint()
            except Exception:
                pass

    asyncio.create_task(_worker())
    return {"ok": True, "started": True, "game_id": req.game_id}


@app.post("/api/translate/pause/{game_id}")
async def api_translate_pause(game_id: str):
    qwen_engine.pause_game(game_id)
    emit_sse("paused", {"game_id": game_id, "paused": True})
    return {"ok": True, "paused": True}


@app.post("/api/translate/resume/{game_id}")
async def api_translate_resume(game_id: str):
    qwen_engine.resume_game(game_id)
    emit_sse("paused", {"game_id": game_id, "paused": False})
    return {"ok": True, "paused": False}


# ---------------- Monitor, Glossary & Global TM ---------------- #

@app.get("/api/games/{game_id}/monitor")
async def api_game_monitor(game_id: str):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    src = game.get("source_lang") or "en"
    tgt = game.get("target_lang") or "vi"
    b_info = backup_mgr.get_backup_info(game_id, game.get("game_dir"))
    pending_rows = cache_db.get_pending_strings(src, tgt)
    if not pending_rows and game.get("detected_slots"):
        slot_map = {s["slot_id"]: s for s in game["detected_slots"]}
        if src in slot_map:
            full_ext = await asyncio.to_thread(
                backup_mgr.extract_entries_preferring_backup,
                game_id,
                game["game_dir"],
                slot_map[src],
                extract_slot_entries,
                "FULL_PATCH",
                True,
            )
            tm_hit_map = global_tm.lookup_exact_batch([e.text for e in full_ext["entries"]], src, tgt)
            estimates = estimate_preset_durations(
                full_ext["entries"],
                set(tm_hit_map.keys()),
                has_glossary=bool(cache_db.list_glossary_terms(src, tgt)),
                profiler=hw_profiler,
            )
        else:
            estimates = estimate_preset_durations([], set(), has_glossary=False, profiler=hw_profiler)
    else:
        tm_hit_map = global_tm.lookup_exact_batch([r["source_text"] for r in pending_rows if not r.get("num_map")], src, tgt)
        estimates = estimate_preset_durations(
            pending_rows,
            set(tm_hit_map.keys()),
            has_glossary=bool(cache_db.list_glossary_terms(src, tgt)),
            profiler=hw_profiler,
        )
    dedup_summary = cache_db.get_dedup_summary(src, tgt)
    ext_info = await asyncio.to_thread(_get_or_sync_external_source_info, game_id, game, cache_db, src, tgt)
    return {
        "recent_rows": cache_db.get_recent_translations(src, tgt, limit=100),
        "glossary_terms": cache_db.list_glossary_terms(src, tgt),
        "prethink": cache_db.get_latest_prethink(),
        "progress": cache_db.get_progress(src, tgt),
        "saved_translations": cache_db.list_saved_translations(),
        "external_source": ext_info,
        "cache_db_path": str(cache_db.db_path),
        "backup_info": b_info,
        "estimates": estimates,
        "stats": {**dedup_summary, "tm_hits": len(tm_hit_map)},
        "shields": [
            {
                "id": "01",
                "name": "01. Anti-Cheat Ban Protection",
                "ok": game.get("anticheat_status") != "BLOCKED",
                "detail": "Sạch chữ ký Anti-Cheat" if game.get("anticheat_status") != "BLOCKED" else f"Phát hiện: {', '.join(game.get('anticheat_vendors', []))}",
            },
            {
                "id": "02",
                "name": "02. Clean External Backup",
                "ok": b_info.get("has_backup", False),
                "detail": f"{b_info.get('file_count', 0)} file gốc được lưu tại backups/" if b_info.get("has_backup") else "Sẵn sàng sao lưu vi sai (không tạo rác trong thư mục game)",
            },
            {
                "id": "03",
                "name": "03. Native Slot Injection",
                "ok": bool(game.get("detected_slots")),
                "detail": f"Chèn an toàn vào slot [{game.get('inject_slot') or '--'}]",
            },
            {
                "id": "04",
                "name": "04. Tag Firewall __TAG_x__",
                "ok": True,
                "detail": "Khóa thẻ code + Retry temp=0.0 tối đa 2 lần",
            },
            {
                "id": "05",
                "name": "05. Encoding/BOM Guard",
                "ok": True,
                "detail": "charset-normalizer bảo toàn BOM/EOL & lọc <think>",
            },
            {
                "id": "06",
                "name": "06. 0GB VRAM Flush",
                "ok": True,
                "detail": "Tự xả GPU VRAM về 0GB trước khi mở game",
            },
        ],
    }


@app.get("/api/games/{game_id}/saved-translations")
async def api_list_saved_translations(game_id: str):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    src = game.get("source_lang") or "en"
    tgt = game.get("target_lang") or "vi"
    ext_info = await asyncio.to_thread(_get_or_sync_external_source_info, game_id, game, cache_db, src, tgt)
    return {
        "ok": True,
        "cache_db_path": str(cache_db.db_path),
        "saved_translations": cache_db.list_saved_translations(),
        "external_source": ext_info,
    }


@app.get("/api/games/{game_id}/external-source/download")
async def api_download_external_source(
    game_id: str,
    fmt: str = "json",
    source_lang: Optional[str] = None,
    target_lang: Optional[str] = None,
):
    game_id = _validate_safe_id(game_id, "game_id")
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    src = source_lang or game.get("source_lang") or "en"
    tgt = target_lang or game.get("target_lang") or "vi"
    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    snap = await asyncio.to_thread(_sync_external_source_snapshot, game_id, game, cache_db, src, tgt)
    if not snap.get("available"):
        raise HTTPException(status_code=400, detail="Chưa có dữ liệu ngôn ngữ gốc. Hãy quét game trước.")
    fmt_clean = (fmt or "json").strip().lower()
    if fmt_clean == "txt":
        target_path = Path(snap["txt_path"])
        media = "text/plain; charset=utf-8"
        fname = snap["txt_filename"]
    else:
        target_path = Path(snap["json_path"])
        media = "application/json; charset=utf-8"
        fname = snap["json_filename"]
    content = await asyncio.to_thread(target_path.read_bytes)
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/games/{game_id}/external-source/open-folder")
async def api_open_external_source_folder(game_id: str):
    game_id = _validate_safe_id(game_id, "game_id")
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    src = game.get("source_lang") or "en"
    tgt = game.get("target_lang") or "vi"
    await asyncio.to_thread(_get_or_sync_external_source_info, game_id, game, cache_db, src, tgt)
    export_dir = _game_export_dir(game_id)
    export_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        try:
            os.startfile(str(export_dir))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Không mở được thư mục exports: {exc}")
    return {"ok": True, "path": str(export_dir)}


@app.post("/api/games/{game_id}/external-translation/import")
async def api_import_external_translation(game_id: str, req: ExternalTranslationImportRequest):
    game_id = _validate_safe_id(game_id, "game_id")
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    if _job_states.get(game_id, {}).get("status") == "running":
        raise HTTPException(status_code=409, detail="Tiến trình cho game này đang chạy.")

    b64_data = req.content_b64.split(",", 1)[1] if "," in req.content_b64 else req.content_b64
    if len(b64_data) > int(_MAX_SAKUGAPACK_BYTES * 1.4):
        raise HTTPException(status_code=413, detail="File bản dịch vượt quá giới hạn 32 MB.")
    try:
        raw_bytes = base64.b64decode(b64_data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Không đọc được dữ liệu tải lên: {exc}")
    if len(raw_bytes) > _MAX_SAKUGAPACK_BYTES:
        raise HTTPException(status_code=413, detail="File bản dịch vượt quá giới hạn 32 MB.")

    src = req.source_lang or game.get("source_lang") or "en"
    tgt = req.target_lang or game.get("target_lang") or "vi"
    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    try:
        imp = await asyncio.to_thread(
            cache_db.import_external_translation_payload,
            raw_bytes,
            req.filename,
            src,
            tgt,
            False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if int(imp.get("imported_strings") or 0) <= 0:
        raise HTTPException(
            status_code=400,
            detail="Không tìm thấy câu dịch hợp lệ nào khớp với file gốc của game này (hoặc tất cả ô dịch đang trống).",
        )

    eff_src = imp.get("source_lang") or src
    eff_tgt = imp.get("target_lang") or tgt
    tm_pairs = imp.pop("tm_pairs", [])
    if tm_pairs:
        global_tm.upsert_batch(tm_pairs, eff_src, eff_tgt)

    write_res = None
    applied_blocked_anticheat = False
    if req.auto_apply:
        try:
            applied = await _hot_apply_cached_game(
                game_id,
                game,
                source_lang=eff_src,
                target_lang=eff_tgt,
                inject_slot_override=req.inject_slot,
                patch_fonts=req.patch_fonts,
                force_offline_override=req.force_offline_override,
            )
            write_res = applied["write_result"]
            updated = applied["game"]
            prog = applied["progress"]
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
            applied_blocked_anticheat = True
            prog = cache_db.get_progress(eff_src, eff_tgt)
            updated = library_db.update_fields(
                game_id,
                source_lang=eff_src,
                target_lang=eff_tgt,
                total_strings=prog["total"],
                translated_strings=prog["done"],
                progress_pct=prog["pct"],
            )
            _hydrate_saved_translation_flags(updated)
    else:
        prog = cache_db.get_progress(eff_src, eff_tgt)
        updated = library_db.update_fields(
            game_id,
            source_lang=eff_src,
            target_lang=eff_tgt,
            total_strings=prog["total"],
            translated_strings=prog["done"],
            progress_pct=prog["pct"],
        )
        _hydrate_saved_translation_flags(updated)

    ext_info = await asyncio.to_thread(_get_or_sync_external_source_info, game_id, updated, cache_db, eff_src, eff_tgt)
    emit_sse("saved_translation_applied", {"game_id": game_id, "source_lang": eff_src, "target_lang": eff_tgt})
    return {
        "ok": True,
        **imp,
        "applied_blocked_anticheat": applied_blocked_anticheat,
        "progress": prog,
        "write_result": write_res,
        "saved_translations": cache_db.list_saved_translations(),
        "external_source": ext_info,
        "game": updated,
    }


@app.post("/api/games/{game_id}/saved-translations/apply")
async def api_apply_saved_translation(game_id: str, req: ApplySavedTranslationRequest):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    if _job_states.get(game_id, {}).get("status") == "running":
        raise HTTPException(status_code=409, detail="Tiến trình dịch cho game này đang chạy.")

    ac_report = await asyncio.to_thread(scan_anticheat, game["game_dir"])
    if ac_report["risk_level"] == "BLOCKED" and not req.force_offline_override:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ANTICHEAT_BLOCKED",
                "message": ac_report["message"],
                "detected_ac": ac_report["detected_ac"],
            },
        )

    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    all_tr = cache_db.get_all_translations(req.source_lang, req.target_lang)
    if not all_tr:
        raise HTTPException(
            status_code=400,
            detail=f"Chưa có câu dịch nào cho cặp [{req.source_lang} -> {req.target_lang}] trong kho lưu.",
        )

    slots = game.get("detected_slots", [])
    if not slots:
        loc_report = await asyncio.to_thread(scan_game_languages, game["game_dir"], game.get("aes_key") or "")
        slots = loc_report.get("slots", [])
    slot_map = {s["slot_id"]: s for s in slots}
    if req.source_lang not in slot_map:
        raise HTTPException(status_code=400, detail=f"Không tìm thấy slot nguồn [{req.source_lang}] trong thư mục game.")

    ranked = rank_inject_slots(slots, req.source_lang, req.target_lang)
    inject_slot = req.inject_slot or game.get("inject_slot") or (ranked[0]["slot_id"] if ranked else req.source_lang)
    if inject_slot not in slot_map:
        inject_slot = ranked[0]["slot_id"] if ranked else req.source_lang

    extracted = await asyncio.to_thread(
        backup_mgr.extract_entries_preferring_backup,
        game_id,
        game["game_dir"],
        slot_map[req.source_lang],
        extract_slot_entries,
        game.get("scope") or "FULL_PATCH",
        True,
    )
    entries = extracted["entries"]

    def _do_backup(mod_files, created_files):
        backup_mgr.backup_files(game_id, game["game_dir"], mod_files, created_files)

    write_res = await asyncio.to_thread(
        apply_slot_translations,
        game["game_dir"],
        entries,
        all_tr,
        slot_map[inject_slot],
        _do_backup,
        req.target_lang,
        req.source_lang,
    )

    font_res = {"applied": False}
    if req.patch_fonts:
        font_res = await asyncio.to_thread(
            font_patcher.apply_font_patch,
            game["game_dir"],
            game["engine"],
            inject_slot,
            _do_backup,
        )
    if write_res.get("ok"):
        await asyncio.to_thread(backup_mgr.record_patched_state, game_id, game["game_dir"])

    prog = cache_db.get_progress(req.source_lang, req.target_lang)
    updated = library_db.update_fields(
        game_id,
        source_lang=req.source_lang,
        target_lang=req.target_lang,
        inject_slot=inject_slot,
        total_strings=prog["total"],
        translated_strings=prog["done"],
        progress_pct=prog["pct"],
        is_patched=1 if write_res.get("ok") else game.get("is_patched", 0),
    )
    updated["ranked_slots"] = ranked
    updated["has_backup"] = backup_mgr.has_backup(game_id, game["game_dir"])
    updated["backup_info"] = backup_mgr.get_backup_info(game_id, game["game_dir"])
    updated["cover_url"] = await _resolve_cover_url(updated)

    emit_sse(
        "saved_translation_applied",
        {
            "game_id": game_id,
            "source_lang": req.source_lang,
            "target_lang": req.target_lang,
            "inject_slot": inject_slot,
            "progress": prog,
            "write_result": write_res,
        },
    )
    return {
        "ok": bool(write_res.get("ok")),
        "game": updated,
        "progress": prog,
        "write_result": write_res,
        "font_result": font_res,
        "saved_translations": cache_db.list_saved_translations(),
    }


@app.delete("/api/games/{game_id}/saved-translations")
async def api_delete_saved_translation(game_id: str, source_lang: str, target_lang: str):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    if _job_states.get(game_id, {}).get("status") == "running" and game.get("source_lang") == source_lang and game.get("target_lang") == target_lang:
        raise HTTPException(status_code=409, detail="Không thể xóa bản dịch đang chạy.")

    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    deleted = cache_db.delete_saved_translations(source_lang, target_lang)
    if game.get("source_lang") == source_lang and game.get("target_lang") == target_lang:
        library_db.update_fields(game_id, translated_strings=0, progress_pct=0.0)
    return {
        "ok": True,
        "deleted": deleted,
        "saved_translations": cache_db.list_saved_translations(),
    }


@app.post("/api/games/{game_id}/edit-string")
async def api_edit_single_string(game_id: str, req: EditTranslationRequest):
    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    row = cache_db.update_single_translation(req.str_hash, req.translated_text)
    if not row:
        raise HTTPException(status_code=404, detail="String not found")
    global_tm.upsert_batch([(row["source_text"], req.translated_text, row.get("category", "UI"))], row["source_lang"], row["target_lang"])
    return {"ok": True, "row": row}


@app.post("/api/games/{game_id}/glossary")
async def api_add_glossary(game_id: str, req: GlossaryAddRequest):
    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    cache_db.upsert_glossary_terms(
        [{"source_term": req.source_term, "target_term": req.target_term, "note": req.note}],
        req.source_lang,
        req.target_lang,
    )
    return {"ok": True, "terms": cache_db.list_glossary_terms(req.source_lang, req.target_lang)}


@app.delete("/api/games/{game_id}/glossary/{term_id}")
async def api_delete_glossary(game_id: str, term_id: int):
    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    cache_db.delete_glossary_term(term_id)
    return {"ok": True}


async def _hot_apply_cached_game(
    game_id: str,
    game: Dict[str, Any],
    source_lang: Optional[str] = None,
    target_lang: Optional[str] = None,
    inject_slot_override: Optional[str] = None,
    patch_fonts: bool = True,
    force_offline_override: bool = False,
) -> Dict[str, Any]:
    """Shared helper to inject all DONE cached translations into game files & snapshot patched hashes."""
    if _job_states.get(game_id, {}).get("status") == "running":
        raise HTTPException(status_code=409, detail="Tiến trình cho game này đang chạy.")
    ac_report = await asyncio.to_thread(scan_anticheat, game["game_dir"])
    if ac_report["risk_level"] == "BLOCKED" and not force_offline_override:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ANTICHEAT_BLOCKED",
                "message": ac_report["message"],
                "detected_ac": ac_report["detected_ac"],
            },
        )

    src = source_lang or game.get("source_lang") or "en"
    tgt = target_lang or game.get("target_lang") or "vi"
    scope = game.get("scope") or "FULL_PATCH"

    slots = game.get("detected_slots", [])
    if not slots:
        loc_report = await asyncio.to_thread(scan_game_languages, game["game_dir"], game.get("aes_key") or "")
        slots = loc_report.get("slots", [])
    slot_map = {s["slot_id"]: s for s in slots}
    ranked = rank_inject_slots(slots, src, tgt)
    inject_slot = inject_slot_override or game.get("inject_slot") or (ranked[0]["slot_id"] if ranked else src)
    if inject_slot not in slot_map:
        inject_slot = ranked[0]["slot_id"] if ranked else src

    if src not in slot_map or inject_slot not in slot_map:
        raise HTTPException(status_code=400, detail=f"Không tìm thấy slot [{src}] / [{inject_slot}] trong thư mục game.")

    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    all_tr = cache_db.get_all_translations(src, tgt)
    if not all_tr:
        raise HTTPException(status_code=400, detail=f"Chưa có câu dịch nào cho cặp [{src} -> {tgt}].")

    extracted = await asyncio.to_thread(
        backup_mgr.extract_entries_preferring_backup,
        game_id,
        game["game_dir"],
        slot_map[src],
        extract_slot_entries,
        scope,
        True,
    )
    entries = extracted["entries"]

    def _do_backup(mod_files, created_files):
        backup_mgr.backup_files(game_id, game["game_dir"], mod_files, created_files)

    write_res = await asyncio.to_thread(
        apply_slot_translations,
        game["game_dir"],
        entries,
        all_tr,
        slot_map[inject_slot],
        _do_backup,
        tgt,
        src,
    )
    font_res = {"applied": False}
    if patch_fonts:
        font_res = await asyncio.to_thread(
            font_patcher.apply_font_patch,
            game["game_dir"],
            game["engine"],
            inject_slot,
            _do_backup,
        )
    if write_res.get("ok"):
        await asyncio.to_thread(backup_mgr.record_patched_state, game_id, game["game_dir"])

    prog = cache_db.get_progress(src, tgt)
    updated = library_db.update_fields(
        game_id,
        source_lang=src,
        target_lang=tgt,
        inject_slot=inject_slot,
        total_strings=prog["total"],
        translated_strings=prog["done"],
        progress_pct=prog["pct"],
        is_patched=1 if write_res.get("ok") else game.get("is_patched", 0),
    )
    updated["ranked_slots"] = ranked
    updated["has_backup"] = backup_mgr.has_backup(game_id, game["game_dir"])
    updated["backup_info"] = backup_mgr.get_backup_info(game_id, game["game_dir"])
    updated["cover_url"] = await _resolve_cover_url(updated)
    _hydrate_saved_translation_flags(updated)
    return {
        "ok": bool(write_res.get("ok")),
        "write_result": write_res,
        "font_result": font_res,
        "progress": prog,
        "game": updated,
    }


@app.get("/api/games/{game_id}/strings/search")
async def api_search_game_strings(game_id: str, q: str = "", filter: str = "ALL", limit: int = 200):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    src = game.get("source_lang") or "en"
    tgt = game.get("target_lang") or "vi"
    rows = await asyncio.to_thread(cache_db.search_translations, src, tgt, q, filter, limit)
    return {"ok": True, "rows": rows, "total_returned": len(rows)}


@app.post("/api/games/{game_id}/strings/bulk-replace")
async def api_bulk_replace_strings(game_id: str, req: BulkReplaceRequest):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    src = game.get("source_lang") or "en"
    tgt = game.get("target_lang") or "vi"
    rep = await asyncio.to_thread(
        cache_db.bulk_find_replace,
        src,
        tgt,
        req.find_text,
        req.replace_text,
        req.case_sensitive,
        req.category_filter,
    )
    if rep.get("updated_pairs"):
        global_tm.upsert_batch(rep["updated_pairs"], src, tgt)

    write_res = None
    updated_game = game
    hot_applied = False
    if req.hot_apply and rep["replaced_count"] > 0:
        applied = await _hot_apply_cached_game(game_id, game)
        write_res = applied["write_result"]
        updated_game = applied["game"]
        hot_applied = bool(applied["ok"])

    return {
        "ok": True,
        "replaced_count": rep["replaced_count"],
        "hot_applied": hot_applied,
        "write_result": write_res,
        "game": updated_game,
    }


@app.post("/api/games/{game_id}/hot-apply")
async def api_hot_apply_game(game_id: str):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    res = await _hot_apply_cached_game(game_id, game)
    emit_sse("saved_translation_applied", {"game_id": game_id, **res})
    return res


@app.get("/api/games/{game_id}/sakugapack/export")
async def api_export_sakugapack(game_id: str, source_lang: Optional[str] = None, target_lang: Optional[str] = None):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    src = source_lang or game.get("source_lang") or "en"
    tgt = target_lang or game.get("target_lang") or "vi"
    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    meta = {
        "game_id": game_id,
        "title": game.get("title") or game_id,
        "engine": game.get("engine") or "",
        "steam_appid": game.get("steam_appid") or "",
        "style_skill_id": game.get("style_skill_id") or "natural_standard",
    }
    pack = await asyncio.to_thread(cache_db.export_sakugapack_data, src, tgt, meta)
    if not pack.get("strings"):
        raise HTTPException(status_code=400, detail=f"Chưa có câu dịch hoàn tất nào cho [{src} -> {tgt}] để xuất.")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pack.json", json.dumps(pack, ensure_ascii=False, indent=2))
    safe_slug = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (game.get("title") or game_id))
    fname = f"{safe_slug}_{src}_to_{tgt}.sakugapack"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/games/{game_id}/sakugapack/import")
async def api_import_sakugapack(game_id: str, req: SakugaPackImportRequest):
    game_id = _validate_safe_id(game_id, "game_id")
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    if _job_states.get(game_id, {}).get("status") == "running":
        raise HTTPException(status_code=409, detail="Tiến trình cho game này đang chạy.")

    b64_data = req.content_b64.split(",", 1)[1] if "," in req.content_b64 else req.content_b64
    if len(b64_data) > int(_MAX_SAKUGAPACK_BYTES * 1.4):
        raise HTTPException(status_code=413, detail="Gói .sakugapack vượt quá giới hạn 32 MB.")
    try:
        raw_bytes = base64.b64decode(b64_data)
        if len(raw_bytes) > _MAX_SAKUGAPACK_BYTES:
            raise ValueError("Gói .sakugapack vượt quá giới hạn 32 MB.")
        if raw_bytes[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
                target_name = "pack.json" if "pack.json" in zf.namelist() else next(
                    (n for n in zf.namelist() if n.endswith(".json")), ""
                )
                if not target_name:
                    raise ValueError("File .sakugapack không chứa pack.json.")
                json_bytes = _safe_zip_read_limited(zf, target_name, _MAX_SAKUGAPACK_BYTES)
                pack = json.loads(json_bytes.decode("utf-8"))
        else:
            pack = json.loads(raw_bytes.decode("utf-8"))
    except ValueError:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Không đọc được file .sakugapack: {exc}")

    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    try:
        imp = await asyncio.to_thread(cache_db.import_sakugapack_data, pack, False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    src = imp["source_lang"]
    tgt = imp["target_lang"]
    tm_rows = [
        (str(it.get("source_text") or ""), str(it.get("translated_text") or ""), str(it.get("category") or "UI"))
        for it in (pack.get("strings") or [])
        if isinstance(it, dict) and it.get("source_text") and it.get("translated_text") and str(it.get("num_map_json") or "{}") == "{}"
    ]
    if tm_rows:
        global_tm.upsert_batch(tm_rows, src, tgt)

    write_res = None
    if req.auto_apply:
        applied = await _hot_apply_cached_game(
            game_id,
            game,
            source_lang=src,
            target_lang=tgt,
            inject_slot_override=req.inject_slot,
        )
        write_res = applied["write_result"]
        updated = applied["game"]
        prog = applied["progress"]
    else:
        prog = cache_db.get_progress(src, tgt)
        updated = library_db.update_fields(
            game_id,
            source_lang=src,
            target_lang=tgt,
            total_strings=prog["total"],
            translated_strings=prog["done"],
            progress_pct=prog["pct"],
        )
        _hydrate_saved_translation_flags(updated)

    emit_sse("saved_translation_applied", {"game_id": game_id, "source_lang": src, "target_lang": tgt})
    return {
        "ok": True,
        **imp,
        "progress": prog,
        "write_result": write_res,
        "saved_translations": cache_db.list_saved_translations(),
        "game": updated,
    }


@app.post("/api/games/{game_id}/delta-update")
async def api_delta_update_game(game_id: str):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    if _job_states.get(game_id, {}).get("status") == "running":
        raise HTTPException(status_code=409, detail="Tiến trình cho game này đang chạy.")

    src = game.get("source_lang") or "en"
    tgt = game.get("target_lang") or "vi"
    scope = game.get("scope") or "FULL_PATCH"

    watch = await asyncio.to_thread(
        backup_mgr.check_game_update_status,
        game_id,
        game["game_dir"],
        bool(game.get("is_patched")),
    )
    if watch.get("updated_files"):
        await asyncio.to_thread(
            backup_mgr.refresh_baseline_for_updated_files,
            game_id,
            game["game_dir"],
            watch["updated_files"],
        )

    loc_report = await asyncio.to_thread(scan_game_languages, game["game_dir"], game.get("aes_key") or "")
    slots = loc_report.get("slots", []) or game.get("detected_slots", [])
    slot_map = {s["slot_id"]: s for s in slots}
    ranked = rank_inject_slots(slots, src, tgt)
    inject_slot = game.get("inject_slot") or (ranked[0]["slot_id"] if ranked else src)
    if inject_slot not in slot_map:
        inject_slot = ranked[0]["slot_id"] if ranked else src

    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    write_res: Dict[str, Any] = {"ok": False, "updated": 0}
    if src in slot_map and inject_slot in slot_map:
        extracted = await asyncio.to_thread(
            backup_mgr.extract_entries_preferring_backup,
            game_id,
            game["game_dir"],
            slot_map[src],
            extract_slot_entries,
            scope,
            True,
        )
        entries = extracted.get("entries", [])
        cache_db.store_extracted_entries(entries, src, tgt, mask_tags)
        all_tr = cache_db.get_all_translations(src, tgt)

        def _do_backup(mod_files, created_files):
            backup_mgr.backup_files(game_id, game["game_dir"], mod_files, created_files)

        if all_tr and entries:
            write_res = await asyncio.to_thread(
                apply_slot_translations,
                game["game_dir"],
                entries,
                all_tr,
                slot_map[inject_slot],
                _do_backup,
                tgt,
                src,
            )
            if write_res.get("ok"):
                await asyncio.to_thread(backup_mgr.record_patched_state, game_id, game["game_dir"])

    prog = cache_db.get_progress(src, tgt)
    updated = library_db.update_fields(
        game_id,
        detected_slots_json=json.dumps(slots, ensure_ascii=False),
        total_strings=prog["total"],
        translated_strings=prog["done"],
        progress_pct=prog["pct"],
        is_patched=1 if write_res.get("ok") else game.get("is_patched", 0),
    )
    updated["ranked_slots"] = ranked
    updated["has_backup"] = backup_mgr.has_backup(game_id, game["game_dir"])
    updated["backup_info"] = backup_mgr.get_backup_info(game_id, game["game_dir"])
    updated["cover_url"] = await _resolve_cover_url(updated)
    _hydrate_saved_translation_flags(updated)
    return {
        "ok": True,
        "state": watch.get("state", "OK"),
        "total": prog["total"],
        "done": prog["done"],
        "pending_new": max(0, int(prog["total"]) - int(prog["done"])),
        "write_result": write_res,
        "game": updated,
    }


@app.get("/api/tm")
async def api_list_tm(q: str = "", limit: int = 120):
    return {"entries": global_tm.search_tm(q, limit=limit), "stats": global_tm.stats()}


@app.delete("/api/tm/{entry_id}")
async def api_delete_tm(entry_id: int):
    global_tm.delete_entry(entry_id)
    return {"ok": True}


# ---------------- Hardware Profiler, Ollama Models & 2-Layer Skill Hub ---------------- #

@app.get("/api/system/hardware")
async def api_system_hardware():
    hw_eff, ollama_st = await asyncio.gather(
        asyncio.to_thread(hw_profiler.get_effective_settings),
        qwen_engine.check_ollama_status(),
    )
    return {"ok": True, "hardware": hw_eff, "ollama": ollama_st}


@app.post("/api/system/settings")
async def api_save_system_settings(req: HardwareSettingsRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    hw_profiler.save_settings(updates)
    hw_profiler.apply_ollama_env()
    hw_eff, ollama_st = await asyncio.gather(
        asyncio.to_thread(hw_profiler.get_effective_settings),
        qwen_engine.check_ollama_status(),
    )
    telemetry = await asyncio.to_thread(qwen_engine.get_telemetry)
    return {"ok": True, "hardware": hw_eff, "ollama": ollama_st, "telemetry": telemetry}


@app.get("/api/ollama/models")
async def api_ollama_models():
    status = await qwen_engine.check_ollama_status()
    return {"ok": True, **status}


@app.post("/api/ollama/pull")
async def api_ollama_pull(req: PullModelRequest):
    model = (req.model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="Tên model không được để trống.")
    try:
        res = await qwen_engine.pull_model_stream(
            model,
            progress_cb=lambda p: emit_sse("ollama_pull_progress", p),
        )
        status = await qwen_engine.check_ollama_status()
        emit_sse("ollama_pull_progress", {"model": model, "status": "success", "pct": 100.0, "done": True})
        return {"ok": True, **res, "ollama": status}
    except Exception as exc:
        emit_sse("ollama_pull_progress", {"model": model, "status": "error", "error": str(exc), "done": True})
        raise HTTPException(status_code=500, detail=f"Lỗi tải model {model}: {exc}")


@app.post("/api/ollama/launch")
async def api_ollama_launch():
    """Attempt to launch local Ollama desktop app or background server for first-time users."""
    cur = await qwen_engine.check_ollama_status()
    if cur.get("online"):
        return {"ok": True, "already_running": True, "installed": True, "online": True, "ollama": cur}

    candidates = []
    cli_path = shutil.which("ollama")
    if cli_path:
        candidates.append((Path(cli_path), ["serve"]))
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "")
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        if local_app:
            candidates.append((Path(local_app) / "Programs" / "Ollama" / "ollama.exe", ["serve"]))
            candidates.append((Path(local_app) / "Programs" / "Ollama" / "ollama app.exe", []))
        if prog_files:
            candidates.append((Path(prog_files) / "Ollama" / "ollama.exe", ["serve"]))

    chosen = next(((p, extra) for p, extra in candidates if p.exists() and p.is_file()), None)
    if not chosen:
        return {
            "ok": False,
            "installed": False,
            "online": False,
            "download_url": "https://ollama.com/download",
            "message": "Chưa tìm thấy Ollama trên máy. Hãy bấm 'Tải Ollama (Miễn phí)' để cài đặt trong 1 phút.",
        }

    exe_path, extra_args = chosen
    try:
        env = os.environ.copy()
        env.update(hw_profiler.apply_ollama_env())
        kwargs: Dict[str, Any] = {
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            if extra_args:
                kwargs.update(hidden_subprocess_kwargs(detached_group=True))
            else:
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        subprocess.Popen([str(exe_path), *extra_args], **kwargs)
    except Exception as exc:
        return {
            "ok": False,
            "installed": True,
            "online": False,
            "message": f"Không khởi động được Ollama tự động: {exc}",
        }

    latest = cur
    for _ in range(6):
        await asyncio.sleep(0.75)
        latest = await qwen_engine.check_ollama_status()
        if latest.get("online"):
            break

    return {
        "ok": True,
        "installed": True,
        "launched": True,
        "online": bool(latest.get("online")),
        "ollama": latest,
        "message": "Ollama đã khởi động và kết nối thành công!" if latest.get("online") else "Đã mở Ollama, vui lòng đợi vài giây để máy chủ sẵn sàng.",
    }


@app.post("/api/ollama/open-download")
async def api_ollama_open_download():
    import webbrowser

    url = "https://ollama.com/download"
    try:
        webbrowser.open(url)
        return {"ok": True, "url": url}
    except Exception as exc:
        return {"ok": False, "url": url, "message": str(exc)}


@app.get("/api/skills")
async def api_list_skills(
    source_lang: Optional[str] = None,
    target_lang: Optional[str] = None,
    style_skill_id: Optional[str] = None,
    lang_skill_override: str = "auto",
):
    listing = skill_manager.list_skills()
    active_resolved = None
    if source_lang and target_lang:
        active_resolved = skill_manager.resolve_active_skills(
            source_lang=source_lang,
            target_lang=target_lang,
            style_skill_id=style_skill_id or "natural_standard",
            lang_skill_override=lang_skill_override or "auto",
            source_lang_name=get_lang_display_name(source_lang),
            target_lang_name=get_lang_display_name(target_lang),
        )
    return {
        "ok": True,
        **listing,
        "skills_dir": str(skill_manager.skills_dir),
        "active_resolved": active_resolved,
    }


@app.post("/api/skills")
async def api_save_skill(req: SaveSkillRequest):
    try:
        data = req.model_dump()
        if not (data.get("rules_text") or "").strip():
            raw_r = data.get("rules")
            if isinstance(raw_r, list):
                data["rules_text"] = "\n".join(str(x).strip() for x in raw_r if str(x).strip())
            elif isinstance(raw_r, str):
                data["rules_text"] = raw_r
        if not data.get("source_langs") or not data.get("target_langs"):
            pair = (data.get("language_pair") or "").strip()
            if "->" in pair:
                p_src, p_tgt = [x.strip() or "*" for x in pair.split("->", 1)]
                data["source_langs"] = data.get("source_langs") or (["*"] if p_src.lower() == "any" else [x.strip() for x in p_src.split(",") if x.strip()])
                data["target_langs"] = data.get("target_langs") or (["*"] if p_tgt.lower() == "any" else [x.strip() for x in p_tgt.split(",") if x.strip()])
            else:
                s1 = (data.get("source_lang") or "any").strip()
                t1 = (data.get("target_lang") or "any").strip()
                data["source_langs"] = data.get("source_langs") or (["*"] if s1.lower() in ("any", "*") else [s1])
                data["target_langs"] = data.get("target_langs") or (["*"] if t1.lower() in ("any", "*") else [t1])
        saved = skill_manager.save_skill(req.layer, data)
        return {"ok": True, "skill": saved}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/skills/{layer}/{skill_id}")
async def api_delete_skill(layer: str, skill_id: str):
    try:
        skill_manager.delete_skill(layer, skill_id)
        return {"ok": True}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/skills/import")
async def api_import_skill(req: ImportSkillRequest):
    try:
        if req.content_b64:
            raw_bytes = base64.b64decode(req.content_b64.split(",")[-1])
        elif req.raw_text is not None:
            raw_bytes = req.raw_text.encode("utf-8")
        else:
            raise ValueError("Thiếu nội dung file skill để nhập.")
        imp_res = skill_manager.import_skill(req.filename or "imported_skill.md", raw_bytes)
        return {
            "ok": True,
            "skill": imp_res["skill"],
            "glossary_rows": imp_res.get("glossary_rows", []),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Lỗi nhập Skill: {exc}")


@app.get("/api/skills/export/{layer}/{skill_id}")
async def api_export_skill(layer: str, skill_id: str, fmt: str = "md", game_id: Optional[str] = None):
    glossary_rows: Optional[List[Dict[str, str]]] = None
    if game_id:
        game = library_db.get_game(game_id)
        if game:
            cache_db = GameCacheDB(game_id, APP_DIR / "cache")
            glossary_rows = cache_db.list_glossary_terms(
                game.get("source_lang") or "en",
                game.get("target_lang") or "vi",
            )
    try:
        fname, mime, raw_bytes = skill_manager.export_skill(
            layer, skill_id, fmt=fmt, glossary_rows=glossary_rows
        )
        return Response(
            content=raw_bytes,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Lỗi xuất Skill: {exc}")


@app.post("/api/skills/ai-generate")
async def api_ai_generate_skill(req: AiSkillGenRequest):
    idea = (req.idea or "").strip()
    if not idea:
        raise HTTPException(status_code=400, detail="Hãy mô tả ý tưởng phong cách dịch (tối thiểu 1 câu).")
    src_name = get_lang_display_name(req.source_lang)
    tgt_name = get_lang_display_name(req.target_lang)
    sys_p, user_p = build_ai_skill_gen_prompt(idea, src_name, tgt_name, layer=req.layer)

    raw_out = ""
    used_ai = False
    try:
        ollama_st = await qwen_engine.check_ollama_status()
        if ollama_st.get("online"):
            async with httpx.AsyncClient(timeout=60.0) as client:
                raw_out, _th, _tok, _el = await qwen_engine._call_ollama_chat(
                    client, sys_p, user_p, think=False, temperature=0.35, num_predict=650
                )
            used_ai = bool(raw_out.strip())
    except Exception as exc:
        logger.warning("AI skill generation fallback to structured draft: %s", exc)

    parsed = parse_ai_generated_skill(raw_out, idea, layer=req.layer)
    if req.layer == "languages":
        parsed["language_pair"] = f"{req.source_lang}->{req.target_lang}"
        parsed["source_lang"] = req.source_lang
        parsed["target_lang"] = req.target_lang

    if req.auto_save:
        saved = skill_manager.save_skill(req.layer, parsed)
        return {"ok": True, "skill": saved, "used_ai": used_ai}
    return {"ok": True, "skill": parsed, "used_ai": used_ai}


@app.post("/api/skills/open-folder")
async def api_open_skills_folder():
    target_dir = skill_manager.skills_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(target_dir))
    else:
        subprocess.Popen(["xdg-open" if sys.platform.startswith("linux") else "open", str(target_dir)])
    return {"ok": True, "path": str(target_dir)}


@app.post("/api/games/{game_id}/reapply-skill")
async def api_reapply_skill(game_id: str, req: ReapplySkillRequest):
    game = library_db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    if _job_states.get(game_id, {}).get("status") == "running":
        raise HTTPException(status_code=409, detail="Tiến trình cho game này đang chạy.")

    src = game.get("source_lang") or "en"
    tgt = game.get("target_lang") or "vi"
    inject_slot = game.get("inject_slot") or src
    eff_style = (req.style_skill_id or "").strip() or (game.get("style_skill_id") or game.get("tone_preset") or "natural_standard")
    eff_lang_ov = (req.lang_skill_override or "").strip() or (game.get("lang_skill_override") or "auto")

    library_db.update_fields(
        game_id,
        style_skill_id=eff_style,
        tone_preset=eff_style,
        lang_skill_override=eff_lang_ov,
        compiled_style_card="",
    )

    cache_db = GameCacheDB(game_id, APP_DIR / "cache")
    reset_cnt = cache_db.reset_for_reapply_skill(
        src,
        tgt,
        scope=req.scope,
        keep_manual_locked=bool(req.keep_manual_locked),
    )
    prog = cache_db.get_progress(src, tgt)
    library_db.update_fields(
        game_id,
        total_strings=prog["total"],
        translated_strings=prog["done"],
        progress_pct=prog["pct"],
    )

    started_job = False
    if req.auto_start and reset_cnt > 0:
        await api_translate_start(
            StartTranslateRequest(
                game_id=game_id,
                source_lang=src,
                target_lang=tgt,
                inject_slot=inject_slot,
                scope=game.get("scope") or "FULL_PATCH",
                patch_fonts=True,
                style_skill_id=eff_style,
                lang_skill_override=eff_lang_ov,
                bypass_global_tm=True,
            )
        )
        started_job = True

    fresh = library_db.get_game(game_id) or game
    _hydrate_saved_translation_flags(fresh)
    return {
        "ok": True,
        "reset_count": reset_cnt,
        "started": started_job,
        "progress": prog,
        "game": fresh,
    }


# ---------------- PyWebView Native Pickers & SSE ---------------- #

@app.post("/api/ui/pick-folder")
async def api_pick_folder():
    if _wv_window is None:
        return {"path": ""}
    import webview
    res = _wv_window.create_file_dialog(webview.FOLDER_DIALOG)
    return {"path": res[0] if res else ""}


@app.post("/api/ui/pick-file")
async def api_pick_file():
    if _wv_window is None:
        return {"path": ""}
    import webview
    res = _wv_window.create_file_dialog(
        webview.OPEN_DIALOG,
        file_types=("Game Shortcuts & EXEs (*.url;*.lnk;*.exe)", "All files (*.*)"),
    )
    return {"path": res[0] if res else ""}


@app.get("/api/events")
async def api_events():
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _sse_queues.add(q)

    async def event_stream():
        try:
            await qwen_engine.fetch_ollama_ps()
            init_tele = await asyncio.to_thread(qwen_engine.get_telemetry)
            yield f"data: {json.dumps({'event': 'connected', 'data': {'ts': time.time(), 'telemetry': init_tele}})}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=3.0)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    await qwen_engine.fetch_ollama_ps()
                    tele = await asyncio.to_thread(qwen_engine.get_telemetry)
                    yield f"data: {json.dumps({'event': 'ping', 'data': {'telemetry': tele}})}\n\n"
        finally:
            _sse_queues.discard(q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _find_free_port(start: int = 8765) -> int:
    import socket
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def _probe_and_activate_port(port: int, timeout: float = 0.7) -> Optional[Dict[str, Any]]:
    import urllib.request
    base = f"http://127.0.0.1:{int(port)}"
    try:
        with urllib.request.urlopen(f"{base}/api/ui/identity", timeout=timeout) as resp:
            ident = json.loads(resp.read().decode("utf-8"))
        if ident.get("app") != _APP_IDENTITY:
            return None
        req = urllib.request.Request(f"{base}/api/ui/activate", data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as act_resp:
            act_data = json.loads(act_resp.read().decode("utf-8"))
        act_data["port"] = int(port)
        return act_data
    except Exception:
        return None


def _activate_existing_instance(
    lock_path: Path = _INSTANCE_LOCK_PATH,
    wait_for_lock: bool = True,
) -> Optional[Dict[str, Any]]:
    attempts = 8 if wait_for_lock else 1
    for idx in range(attempts):
        if lock_path.exists():
            try:
                lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
                port = int(lock_data.get("port") or 0)
                if port > 0:
                    res = _probe_and_activate_port(port)
                    if res:
                        return res
            except Exception:
                pass
        if idx < attempts - 1:
            time.sleep(0.15)
    for port in range(8765, 8795):
        res = _probe_and_activate_port(port, timeout=0.25)
        if res:
            return res
    return None


def acquire_single_instance_lock(
    lock_path: Path = _INSTANCE_LOCK_PATH,
    mutex_name: str = _SINGLE_INSTANCE_MUTEX_NAME,
) -> tuple[bool, Optional[int], Optional[Dict[str, Any]]]:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateMutexW(None, False, str(mutex_name))
        err = int(ctypes.get_last_error())
        if err == 183:  # ERROR_ALREADY_EXISTS
            if handle:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass
            existing = _activate_existing_instance(lock_path=lock_path, wait_for_lock=True)
            return False, None, existing
        if handle:
            return True, int(handle), None

    if lock_path.exists():
        existing = _activate_existing_instance(lock_path=lock_path, wait_for_lock=False)
        if existing:
            return False, None, existing
    return True, None, None


def write_instance_lock(port: int, lock_path: Path = _INSTANCE_LOCK_PATH) -> None:
    payload = {
        "app": _APP_IDENTITY,
        "pid": os.getpid(),
        "port": int(port),
        "started_at": time.time(),
    }
    atomic_write_text(lock_path, json.dumps(payload, ensure_ascii=False, indent=2))


def release_single_instance_lock(
    handle: Optional[int],
    lock_path: Path = _INSTANCE_LOCK_PATH,
) -> None:
    try:
        if lock_path.exists():
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            if int(data.get("pid") or -1) == os.getpid():
                lock_path.unlink(missing_ok=True)
    except Exception:
        pass
    if handle and os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(wintypes.HANDLE(int(handle)))
        except Exception:
            pass


def _unblock_bundled_dotnet_assemblies() -> None:
    """Clear Mark-of-the-Web (Zone.Identifier) ADS on bundled .NET/WebView2 DLLs from downloaded ZIPs."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    for sub in ("pythonnet", "clr_loader", "webview"):
        root = BUNDLE_DIR / sub
        if not root.exists():
            continue
        for fp in root.rglob("*"):
            if not fp.is_file():
                continue
            ads = f"{fp}:Zone.Identifier"
            try:
                if os.path.exists(ads):
                    os.remove(ads)
            except OSError:
                pass


def main() -> None:
    global _wv_window, _active_server, _tray_controller, _force_app_exit, _page_loaded, _instance_mutex_handle
    import argparse

    parser = argparse.ArgumentParser(description="Black Sakura Translate — Local AI Game Localization Launcher")
    parser.add_argument("--web", action="store_true", help="Chạy trên trình duyệt mặc định thay vì cửa sổ Desktop PyWebView")
    parser.add_argument("--no-browser", action="store_true", help="Chạy ở chế độ headless server (không tự mở trình duyệt)")
    parser.add_argument("--port", type=int, default=8765, help="Cổng ưu tiên cho máy chủ cục bộ (mặc định: 8765)")
    args, _ = parser.parse_known_args()

    acquired, mutex_handle, existing = acquire_single_instance_lock()
    if not acquired:
        logger.info("Black Sakura Translate already running (existing=%s); activated existing window and exiting.", existing)
        return
    _instance_mutex_handle = mutex_handle

    port = _find_free_port(max(1024, int(args.port)))
    url = f"http://127.0.0.1:{port}"

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None)
    server = uvicorn.Server(config)
    _active_server = server
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    # Wait briefly for uvicorn socket
    for _ in range(30):
        import urllib.request
        try:
            urllib.request.urlopen(f"{url}/api/tools/status", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)

    write_instance_lock(port)

    def _run_browser_fallback() -> None:
        import webbrowser

        logger.info("Black Sakura Translate running in Web mode at %s (Press Ctrl+C to stop).", url)
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        try:
            while t.is_alive() and not server.should_exit:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

    if args.web or args.no_browser:
        try:
            _run_browser_fallback()
        finally:
            server.should_exit = True
            release_single_instance_lock(_instance_mutex_handle)
            _instance_mutex_handle = None
        return

    _unblock_bundled_dotnet_assemblies()

    try:
        import webview
        from webview.dom import DOMEventHandler
    except Exception as exc:
        logger.warning("PyWebView không khả dụng (%s); tự động chuyển sang chế độ trình duyệt.", exc)
        try:
            _run_browser_fallback()
        finally:
            server.should_exit = True
            release_single_instance_lock(_instance_mutex_handle)
            _instance_mutex_handle = None
        return

    def _request_full_exit() -> None:
        if _any_job_running() and os.name == "nt":
            try:
                import ctypes
                hwnd = ctypes.windll.user32.FindWindowW(None, _APP_TITLE) or 0
                ans = ctypes.windll.user32.MessageBoxW(
                    hwnd,
                    "Đang có tiến trình Dịch hoặc Fix lỗi đang chạy ngầm!\n"
                    "Nếu thoát hẳn lúc này, các câu đã dịch đã được lưu an toàn trong SQLite để lần sau mở app tự động chạy tiếp.\n\n"
                    "Bạn có chắc chắn muốn thoát hẳn?",
                    "Black Sakura Translate // Cảnh báo tiến trình đang chạy",
                    0x00000004 | 0x00000030 | 0x00010000,  # MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND
                )
                if ans != 6:  # IDYES
                    return
            except Exception:
                pass
        _perform_full_exit(destroy_window=True)

    def _open_from_tray() -> None:
        if _wv_window is not None:
            _restore_window_from_tray()
        else:
            import webbrowser
            try:
                webbrowser.open(url)
            except Exception:
                pass

    ico_path = BUNDLE_DIR / "sakuga_icon.ico"
    _tray_controller = WinSystemTray(
        icon_path=ico_path,
        tooltip="Black Sakura Translate",
        on_open=_open_from_tray,
        on_hide=lambda: _hide_window_to_tray(True),
        on_exit=_request_full_exit,
        is_busy_fn=_any_job_running,
    )
    _tray_controller.start()

    try:
        _wv_window = webview.create_window(
            _APP_TITLE,
            url=url,
            width=1400,
            height=880,
            min_size=(1120, 720),
            background_color="#0B0B0E",
        )

        def _on_closing():
            if _force_app_exit:
                return True
            if _any_job_running():
                def _prompt_close_when_busy():
                    opened = False
                    if _page_loaded and _wv_window is not None:
                        try:
                            opened = bool(
                                _wv_window.evaluate_js("Boolean(window.openExitConfirmModal && window.openExitConfirmModal())")
                            )
                        except Exception:
                            opened = False
                    if opened:
                        return
                    if os.name == "nt":
                        try:
                            import ctypes
                            hwnd = ctypes.windll.user32.FindWindowW(None, _APP_TITLE) or 0
                            ans = ctypes.windll.user32.MessageBoxW(
                                hwnd,
                                "App đang chạy tiến trình Dịch / Fix lỗi!\n"
                                "Nếu đóng hẳn lúc này, các câu đã dịch vẫn được lưu an toàn trong SQLite để lần sau chạy tiếp.\n\n"
                                "Bạn có chắc chắn muốn đóng hẳn app không?",
                                "Black Sakura Translate // Xác nhận đóng app",
                                0x00000004 | 0x00000030 | 0x00010000,  # MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND
                            )
                            if ans != 6:
                                return
                        except Exception:
                            pass
                    _perform_full_exit(destroy_window=True)

                threading.Thread(target=_prompt_close_when_busy, daemon=True).start()
                return False
            # Idle state: X button exits completely (never hides to tray)
            _perform_full_exit(destroy_window=False)
            return True

        def _on_minimized():
            if library_db.get_setting("minimize_to_tray", "1") == "1":
                threading.Thread(target=lambda: _hide_window_to_tray(True), daemon=True).start()

        def _on_loaded():
            global _page_loaded
            _page_loaded = True

            def _on_drop(e):
                files = e.get("dataTransfer", {}).get("files", [])
                paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
                if paths and _wv_window:
                    _wv_window.evaluate_js(f"window.onSakugaNativeDrop && window.onSakugaNativeDrop({json.dumps(paths)})")

            try:
                _wv_window.dom.document.events.drop += DOMEventHandler(_on_drop, prevent_default=True, stop_propagation=True)
            except Exception as exc:
                logger.debug("DOM drop hook warning: %s", exc)

        _wv_window.events.closing += _on_closing
        _wv_window.events.minimized += _on_minimized
        _wv_window.events.loaded += _on_loaded
        webview.start(debug=False, icon=str(ico_path) if ico_path.exists() else None)
    except Exception as exc:
        logger.warning("PyWebView khởi động thất bại (%s); tự động chuyển sang chế độ trình duyệt.", exc)
        _wv_window = None
        _run_browser_fallback()
    finally:
        _perform_full_exit(destroy_window=False)
        _active_server = None
        release_single_instance_lock(_instance_mutex_handle)
        _instance_mutex_handle = None


if __name__ == "__main__":
    main()
