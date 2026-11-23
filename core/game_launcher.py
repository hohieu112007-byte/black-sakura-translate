"""Smart Xbox Game Launcher: Steam vdf/acf parser, .url/.lnk/folder ingest, 2:3 Cover CDN, 0GB VRAM safe launch."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import vdf

from extractors.localization_hub import scan_game_languages
from safety_shield.anticheat_guard import scan_anticheat

try:
    import winreg
except Exception:
    winreg = None

logger = logging.getLogger("sakuga.launcher")

APP_DIR = Path(__file__).resolve().parent.parent
COVERS_DIR = APP_DIR / "covers"
COVERS_DIR.mkdir(parents=True, exist_ok=True)

IGNORED_EXES = {
    "unitycrashhandler64.exe",
    "unitycrashhandler32.exe",
    "unins000.exe",
    "uninstall.exe",
    "crashreportclient.exe",
    "ueprereqsetup_x64.exe",
    "start_protected_game.exe",
    "easyanticheat_setup.exe",
    "easyanticheat_eos_setup.exe",
    "beservice.exe",
    "beservice_x64.exe",
    "dxsetup.exe",
    "vcredist_x64.exe",
    "vcredist_x86.exe",
}


def compute_game_id(game_dir: str | Path) -> str:
    norm = os.path.normcase(str(Path(game_dir).resolve()))
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", Path(game_dir).name).strip("_") or "game"
    digest = hashlib.md5(norm.encode("utf-8")).hexdigest()[:8]
    return f"{stem}_{digest}"


def _find_steam_roots() -> List[Path]:
    roots: List[Path] = []
    if winreg is not None:
        for reg_path in (r"Software\Valve\Steam", r"SOFTWARE\WOW6432Node\Valve\Steam"):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path) as key:
                    val, _ = winreg.QueryValueEx(key, "SteamPath")
                    if val and Path(val).exists():
                        roots.append(Path(val))
            except OSError:
                pass
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
                    val, _ = winreg.QueryValueEx(key, "InstallPath")
                    if val and Path(val).exists():
                        roots.append(Path(val))
            except OSError:
                pass

    for fallback in (
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files\Steam"),
        Path(r"C:\steamgames"),
        Path(r"D:\SteamLibrary"),
        Path(r"E:\SteamLibrary"),
    ):
        if fallback.exists() and fallback not in roots:
            roots.append(fallback)
    return roots


def collect_steam_apps() -> List[Dict[str, str]]:
    """Parse Steam libraryfolders.vdf and every appmanifest_*.acf using ValvePython/vdf."""
    steamapps_dirs: set[Path] = set()
    for root in _find_steam_roots():
        sa = root if root.name.lower() == "steamapps" else (root / "steamapps")
        if sa.exists():
            steamapps_dirs.add(sa.resolve())
        lib_vdf = sa / "libraryfolders.vdf"
        if lib_vdf.exists():
            try:
                parsed = vdf.loads(lib_vdf.read_text(encoding="utf-8", errors="replace"))
                libs = parsed.get("libraryfolders", parsed)
                if isinstance(libs, dict):
                    for val in libs.values():
                        if isinstance(val, dict) and val.get("path"):
                            p = Path(val["path"]) / "steamapps"
                            if p.exists():
                                steamapps_dirs.add(p.resolve())
            except Exception as exc:
                logger.debug("vdf parse error on %s: %s", lib_vdf, exc)

    apps: List[Dict[str, str]] = []
    seen_appids: set[str] = set()
    _non_game_appids = {"228980", "1070560", "1391110", "1493710", "1628350", "2348590"}
    for sa_dir in steamapps_dirs:
        for acf in sa_dir.glob("appmanifest_*.acf"):
            try:
                data = vdf.loads(acf.read_text(encoding="utf-8", errors="replace"))
                state = data.get("AppState", {})
                appid = str(state.get("appid") or "").strip()
                installdir = str(state.get("installdir") or "").strip()
                name = str(state.get("name") or installdir).strip()
                name_low = name.lower()
                if (
                    appid in _non_game_appids
                    or "steamworks common redistributables" in name_low
                    or "steam linux runtime" in name_low
                    or name_low.startswith("proton ")
                ):
                    continue
                full_dir = sa_dir / "common" / installdir
                if appid and installdir and appid not in seen_appids and full_dir.exists():
                    seen_appids.add(appid)
                    apps.append(
                        {
                            "steam_appid": appid,
                            "title": name,
                            "installdir": installdir,
                            "game_dir": str(full_dir.resolve()),
                            "steamapps_dir": str(sa_dir),
                        }
                    )
            except Exception as exc:
                logger.debug("acf parse error on %s: %s", acf, exc)
    return apps


def _find_steam_info_for_dir(game_dir: Path) -> Dict[str, str]:
    # 1. Local steam_appid.txt
    appid_txt = game_dir / "steam_appid.txt"
    if appid_txt.exists():
        raw = appid_txt.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[0]
        if raw.isdigit():
            return {"steam_appid": raw, "title": game_dir.name}

    # 2. Check sibling steamapps/appmanifest_*.acf (direct parent of common/<game>)
    if game_dir.parent.name.lower() == "common" and game_dir.parent.parent.name.lower() == "steamapps":
        sa = game_dir.parent.parent
        for acf in sa.glob("appmanifest_*.acf"):
            try:
                data = vdf.loads(acf.read_text(encoding="utf-8", errors="replace"))
                state = data.get("AppState", {})
                if os.path.normcase(state.get("installdir", "")) == os.path.normcase(game_dir.name):
                    return {
                        "steam_appid": str(state.get("appid", "")).strip(),
                        "title": str(state.get("name", game_dir.name)).strip(),
                    }
            except Exception:
                pass

    # 3. Cross-reference global Steam libraryfolders.vdf cache
    norm_dir = os.path.normcase(str(game_dir.resolve()))
    for app in collect_steam_apps():
        if os.path.normcase(app["game_dir"]) == norm_dir:
            return {"steam_appid": app["steam_appid"], "title": app["title"]}

    return {"steam_appid": "", "title": game_dir.name}


def find_primary_exe(game_dir: Path, title_hint: str = "") -> str:
    exes = [
        p for p in game_dir.glob("*.exe")
        if p.is_file() and p.name.lower() not in IGNORED_EXES and "redist" not in p.name.lower()
    ]
    if not exes:
        # Also check 1 level down (e.g. Unreal Binaries/Win64)
        for sub in game_dir.glob("*/*.exe"):
            if sub.is_file() and sub.name.lower() not in IGNORED_EXES:
                exes.append(sub)
        for sub in game_dir.glob("*/Binaries/Win64/*.exe"):
            if sub.is_file() and sub.name.lower() not in IGNORED_EXES:
                exes.append(sub)
    if not exes:
        return ""
    # Prefer exe matching folder/title name, else largest root exe
    dir_low = re.sub(r"[^a-z0-9]", "", (title_hint or game_dir.name).lower())
    for p in exes:
        stem_low = re.sub(r"[^a-z0-9]", "", p.stem.lower())
        if stem_low and (stem_low == dir_low or dir_low in stem_low):
            return str(p.resolve())
    exes.sort(key=lambda p: p.stat().st_size, reverse=True)
    return str(exes[0].resolve())


def parse_steam_url_file(url_path: Path) -> Dict[str, str]:
    text = url_path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"steam://rungameid/(\d+)", text, re.IGNORECASE)
    if not m:
        raise ValueError("File .url không chứa steam://rungameid/<appid> hợp lệ.")
    appid = m.group(1)
    for app in collect_steam_apps():
        if app["steam_appid"] == appid:
            return {
                "steam_appid": appid,
                "title": app["title"],
                "game_dir": app["game_dir"],
                "shortcut_path": str(url_path.resolve()),
                "launch_mode": "steam",
            }
    raise FileNotFoundError(f"Không tìm thấy thư mục cài đặt của Steam AppID {appid} trên máy.")


def parse_windows_lnk_file(lnk_path: Path) -> Dict[str, str]:
    ps_script = (
        "$sh = New-Object -ComObject WScript.Shell; "
        f"$sc = $sh.CreateShortcut('{str(lnk_path).replace(chr(39), chr(39)*2)}'); "
        "Write-Output ($sc.TargetPath + '|' + $sc.WorkingDirectory + '|' + $sc.Arguments)"
    )
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        timeout=10,
    )
    if proc.returncode != 0 or "|" not in proc.stdout:
        raise ValueError(f"Không thể đọc shortcut .lnk: {proc.stderr}")
    target_path, work_dir, args = (proc.stdout.strip().split("|", 2) + ["", "", ""])[:3]

    # Check if shortcut invokes steam://rungameid/
    steam_m = re.search(r"steam://rungameid/(\d+)", f"{target_path} {args}", re.IGNORECASE)
    if steam_m:
        appid = steam_m.group(1)
        for app in collect_steam_apps():
            if app["steam_appid"] == appid:
                return {
                    "steam_appid": appid,
                    "title": app["title"],
                    "game_dir": app["game_dir"],
                    "shortcut_path": str(lnk_path.resolve()),
                    "launch_mode": "steam",
                }

    t_path = Path(target_path) if target_path else None
    g_dir = Path(work_dir) if work_dir and Path(work_dir).exists() else (t_path.parent if t_path else None)
    if not g_dir or not g_dir.exists():
        raise FileNotFoundError("Thư mục đích trong shortcut .lnk không tồn tại.")

    return {
        "steam_appid": "",
        "title": lnk_path.stem,
        "game_dir": str(g_dir.resolve()),
        "exe_path": str(t_path.resolve()) if t_path and t_path.exists() else "",
        "shortcut_path": str(lnk_path.resolve()),
        "launch_mode": "shortcut",
    }


def _looks_like_image(data: bytes) -> bool:
    if len(data) < 1024:
        return False
    return (
        data.startswith(b"\xff\xd8\xff")  # JPEG
        or data.startswith(b"\x89PNG\r\n\x1a\n")  # PNG
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")  # WebP
    )


def _find_local_steam_cover(steam_appid: str) -> Optional[Path]:
    """Check Steam's local appcache/librarycache for vertical or header art before hitting CDN."""
    if not steam_appid or not steam_appid.isdigit():
        return None
    preferred_stems = (
        "library_600x900_2x",
        "library_600x900",
        "library_capsule_2x",
        "library_capsule",
        "portrait",
        "header_2x",
        "header",
    )
    for root in _find_steam_roots():
        cache_dir = root / "appcache" / "librarycache"
        if not cache_dir.exists():
            continue
        # Modern Steam stores per-appid subdirs (<appid>/<hash>/library_600x900*.jpg or <appid>/library_600x900.jpg)
        app_sub = cache_dir / steam_appid
        candidates: List[Path] = []
        if app_sub.is_dir():
            for p in app_sub.rglob("*"):
                if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    candidates.append(p)
        # Legacy flat files (<appid>_library_600x900.jpg)
        for stem in preferred_stems:
            for ext in (".jpg", ".png", ".webp"):
                flat = cache_dir / f"{steam_appid}_{stem}{ext}"
                if flat.is_file():
                    candidates.append(flat)
        if candidates:
            def _score(path: Path) -> tuple[int, int]:
                name = path.stem.lower()
                for idx, stem in enumerate(preferred_stems):
                    if stem in name:
                        return (idx, -path.stat().st_size)
                return (99, -path.stat().st_size)
            candidates.sort(key=_score)
            for cand in candidates:
                try:
                    data = cand.read_bytes()
                    if _looks_like_image(data):
                        return cand
                except Exception:
                    continue
    return None


async def _resolve_steam_asset_urls(client: httpx.AsyncClient, steam_appid: str) -> List[str]:
    """Resolve modern hashed Steam CDN cover URLs via IStoreBrowseService & appdetails, plus legacy fallbacks."""
    urls: List[str] = []

    # 1. Steam IStoreBrowseService returns hashed vertical capsule paths (supports modern Steam Store assets)
    try:
        payload = json.dumps(
            {
                "ids": [{"appid": int(steam_appid)}],
                "context": {"language": "english", "country_code": "US"},
                "data_request": {"include_assets": True},
            },
            separators=(",", ":"),
        )
        resp = await client.get(
            "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/",
            params={"input_json": payload},
        )
        if resp.status_code == 200:
            items = resp.json().get("response", {}).get("store_items", [])
            if items:
                assets = items[0].get("assets") or {}
                fmt = assets.get("asset_url_format") or f"steam/apps/{steam_appid}/${{FILENAME}}"
                for key in (
                    "library_capsule_2x",
                    "library_capsule",
                    "main_capsule_2x",
                    "main_capsule",
                    "header_2x",
                    "header",
                ):
                    rel = assets.get(key)
                    if not rel:
                        continue
                    rel_clean = rel.lstrip("/")
                    formatted = fmt.replace("${FILENAME}", rel_clean) if "${FILENAME}" in fmt else f"steam/apps/{steam_appid}/{rel_clean}"
                    formatted = formatted.lstrip("/")
                    urls.append(f"https://shared.fastly.steamstatic.com/store_item_assets/{formatted}")
                    urls.append(f"https://shared.akamai.steamstatic.com/store_item_assets/{formatted}")
    except Exception:
        pass

    # 2. Storefront appdetails header_image / capsule_image fallback
    try:
        resp = await client.get(
            "https://store.steampowered.com/api/appdetails",
            params={"appids": steam_appid, "l": "english"},
        )
        if resp.status_code == 200:
            entry = resp.json().get(steam_appid, {})
            if entry.get("success") and isinstance(entry.get("data"), dict):
                data = entry["data"]
                for k in ("header_image", "capsule_imagev5", "capsule_image"):
                    u = data.get(k)
                    if isinstance(u, str) and u.startswith("http"):
                        urls.append(u)
    except Exception:
        pass

    # 3. Legacy unhashed CDN URLs
    urls.extend(
        [
            f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{steam_appid}/library_600x900_2x.jpg",
            f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{steam_appid}/library_600x900.jpg",
            f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{steam_appid}/header.jpg",
        ]
    )

    # Deduplicate preserving priority order
    seen = set()
    deduped: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


async def download_steam_cover(game_id: str, steam_appid: str, covers_dir: Path = COVERS_DIR) -> str:
    """Download Steam 2:3 vertical box art (or header fallback) into covers/<game_id>.jpg."""
    if not steam_appid or not steam_appid.isdigit():
        return ""
    covers_dir.mkdir(parents=True, exist_ok=True)
    out_path = covers_dir / f"{game_id}.jpg"
    if out_path.exists() and out_path.stat().st_size > 1024:
        return str(out_path)

    # Check Steam's local librarycache first (works offline too)
    local_img = await asyncio.to_thread(_find_local_steam_cover, steam_appid)
    if local_img is not None:
        try:
            data = local_img.read_bytes()
            if _looks_like_image(data):
                out_path.write_bytes(data)
                return str(out_path)
        except Exception:
            pass

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            urls = await _resolve_steam_asset_urls(client, steam_appid)
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200 and _looks_like_image(resp.content):
                        out_path.write_bytes(resp.content)
                        return str(out_path)
                except Exception:
                    continue
    except Exception:
        pass
    return ""


async def fetch_steam_app_context(steam_appid: str) -> str:
    """Async fetch genres + short_description from Steam Store appdetails API for Phase 0 context."""
    if not steam_appid or not str(steam_appid).isdigit():
        return ""
    url = f"https://store.steampowered.com/api/appdetails?appids={steam_appid}&l=english"
    try:
        async with httpx.AsyncClient(
            timeout=5.5,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return ""
            payload = resp.json()
            entry = payload.get(str(steam_appid), {})
            if not isinstance(entry, dict) or not entry.get("success"):
                return ""
            data = entry.get("data", {})
            genres = ", ".join(
                str(g.get("description", "")).strip()
                for g in data.get("genres", [])
                if isinstance(g, dict) and g.get("description")
            )
            raw_desc = str(data.get("short_description") or "")
            clean_desc = html.unescape(re.sub(r"<[^>]+>", " ", raw_desc))
            clean_desc = re.sub(r"\s+", " ", clean_desc).strip()
            parts = []
            if genres:
                parts.append(f"Thể loại: {genres}")
            if clean_desc:
                parts.append(clean_desc)
            return "\n".join(parts)
    except Exception as exc:
        logger.debug("fetch_steam_app_context notice for %s: %s", steam_appid, exc)
        return ""


async def ingest_game_input(raw_path: str | Path, covers_dir: Path = COVERS_DIR) -> Dict[str, Any]:
    """Resolve Folder / .exe / Steam .url / Windows .lnk into full Game profile + Anti-Cheat scan + Language scan."""
    p = Path(raw_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Đường dẫn không tồn tại: {raw_path}")

    shortcut_path = ""
    launch_mode = "direct_exe"
    exe_path = ""
    steam_appid = ""
    title = ""

    if p.is_file() and p.suffix.lower() == ".url":
        info = parse_steam_url_file(p)
        game_dir = Path(info["game_dir"])
        steam_appid = info["steam_appid"]
        title = info["title"]
        shortcut_path = info["shortcut_path"]
        launch_mode = "steam"
    elif p.is_file() and p.suffix.lower() == ".lnk":
        info = parse_windows_lnk_file(p)
        game_dir = Path(info["game_dir"])
        steam_appid = info.get("steam_appid", "")
        title = info["title"]
        exe_path = info.get("exe_path", "")
        shortcut_path = info["shortcut_path"]
        launch_mode = info["launch_mode"]
    elif p.is_file() and p.suffix.lower() == ".exe":
        game_dir = p.parent
        exe_path = str(p)
    elif p.is_dir():
        game_dir = p
    else:
        raise ValueError("Chỉ hỗ trợ kéo thả Thư mục Game, file .exe, Steam .url hoặc Windows .lnk.")

    if not steam_appid:
        st_info = _find_steam_info_for_dir(game_dir)
        steam_appid = st_info["steam_appid"]
        if not title:
            title = st_info["title"]
        if steam_appid and launch_mode == "direct_exe":
            launch_mode = "steam"

    if not title:
        title = game_dir.name
    if not exe_path:
        exe_path = find_primary_exe(game_dir, title)

    game_id = compute_game_id(game_dir)
    ac_report = scan_anticheat(game_dir)
    loc_report = scan_game_languages(game_dir)
    cover_path, game_description = await asyncio.gather(
        download_steam_cover(game_id, steam_appid, covers_dir=covers_dir),
        fetch_steam_app_context(steam_appid),
    )

    return {
        "game_id": game_id,
        "title": title,
        "game_dir": str(game_dir.resolve()),
        "exe_path": exe_path,
        "steam_appid": steam_appid,
        "launch_mode": launch_mode,
        "shortcut_path": shortcut_path,
        "cover_path": cover_path,
        "game_description": game_description,
        "engine": loc_report.get("engine", "Unknown"),
        "method": loc_report.get("method", ""),
        "anticheat_status": ac_report["risk_level"],
        "anticheat_vendors": ac_report["detected_ac"],
        "anticheat_files": ac_report["matched_files"],
        "detected_slots": loc_report.get("slots", []),
    }


async def launch_game_safely(game: Dict[str, Any], qwen_engine) -> Dict[str, Any]:
    """Flush Qwen3.5:4B VRAM to 0GB, then launch via Steam URI / Shortcut / Direct EXE."""
    await qwen_engine.flush_vram()

    mode = game.get("launch_mode") or "direct_exe"
    appid = (game.get("steam_appid") or "").strip()
    shortcut = (game.get("shortcut_path") or "").strip()
    exe_path = (game.get("exe_path") or "").strip()
    game_dir = game.get("game_dir") or ""

    if mode == "steam" and appid:
        uri = f"steam://rungameid/{appid}"
        os.startfile(uri)
        return {"ok": True, "launched_via": "steam", "target": uri}

    if mode == "shortcut" and shortcut and Path(shortcut).exists():
        os.startfile(shortcut)
        return {"ok": True, "launched_via": "shortcut", "target": shortcut}

    if exe_path and Path(exe_path).exists():
        flags = subprocess.DETACHED_PROCESS if os.name == "nt" else 0
        subprocess.Popen([exe_path], cwd=game_dir or str(Path(exe_path).parent), creationflags=flags)
        return {"ok": True, "launched_via": "direct_exe", "target": exe_path}

    if appid:
        uri = f"steam://rungameid/{appid}"
        os.startfile(uri)
        return {"ok": True, "launched_via": "steam", "target": uri}

    raise FileNotFoundError("Không tìm thấy Steam AppID, Shortcut hoặc tệp .exe hợp lệ để mở game.")
