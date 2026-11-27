"""Automatic GitHub Releases / Raw tool & font bootstrapper into <app_dir>/tools/."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import httpx

from core import get_app_dir

logger = logging.getLogger("sakuga.bootstrapper")

APP_DIR = get_app_dir(__file__)
DEFAULT_TOOLS_DIR = APP_DIR / "tools"

TOOL_SPECS = {
    "repak": {
        "name": "repak (Unreal .pak)",
        "repo": "trumank/repak",
        "kind": "github_release_zip",
        "asset_keywords": ["x86_64-pc-windows-msvc", ".zip"],
        "target_rel": "repak/repak.exe",
        "extract_binary": "repak.exe",
    },
    "unreallocres": {
        "name": "UnrealLocres (UE4/UE5 .locres)",
        "repo": "akintos/UnrealLocres",
        "kind": "github_release_zip_or_exe",
        "asset_keywords": [".zip", ".exe"],
        "target_rel": "unreallocres/UnrealLocres.exe",
        "extract_binary": "UnrealLocres.exe",
    },
    "aesdumpster": {
        "name": "AESDumpster (Unreal AES Key)",
        "repo": "GHFear/AESDumpster",
        "kind": "github_release_zip_or_exe",
        "asset_keywords": ["win64", ".exe", ".zip"],
        "target_rel": "AESDumpster.exe",
        "extract_binary": "AESDumpster.exe",
    },
    "retoc": {
        "name": "retoc (UE5 IoStore .utoc/.ucas)",
        "repo": "trumank/retoc",
        "kind": "github_release_zip",
        "asset_keywords": ["x86_64-pc-windows-msvc", ".zip"],
        "target_rel": "retoc/retoc.exe",
        "extract_binary": "retoc.exe",
    },
    "rpatool": {
        "name": "rpatool (Ren'Py .rpa)",
        "repo": "Shizmob/rpatool",
        "kind": "raw_file",
        "urls": [
            "https://raw.githubusercontent.com/Shizmob/rpatool/master/rpatool",
        ],
        "target_rel": "rpatool.py",
    },
    "font_notosans": {
        "name": "NotoSans-Regular.ttf",
        "repo": "google/fonts",
        "kind": "raw_file",
        "urls": [
            "https://github.com/notofonts/latin-greek-cyrillic/raw/main/fonts/NotoSans/hinted/ttf/NotoSans-Regular.ttf",
            "https://github.com/google/fonts/raw/main/ofl/notosans/NotoSans%5Bwdth%2Cwght%5D.ttf",
        ],
        "target_rel": "fonts/NotoSans-Regular.ttf",
    },
    "font_bevietnam": {
        "name": "BeVietnamPro-Regular.ttf",
        "repo": "google/fonts",
        "kind": "raw_file",
        "urls": [
            "https://github.com/google/fonts/raw/main/ofl/bevietnampro/BeVietnamPro-Regular.ttf",
            "https://raw.githubusercontent.com/google/fonts/main/ofl/bevietnampro/BeVietnamPro-Regular.ttf",
        ],
        "target_rel": "fonts/BeVietnamPro-Regular.ttf",
    },
}

HTTP_HEADERS = {
    "User-Agent": "Black-Sakura-Translate/2.0",
    "Accept": "application/vnd.github+json",
}


class ToolBootstrapper:
    """Checks, downloads, and caches external GitHub CLI tools and fonts into tools/."""

    def __init__(self, tools_dir: Optional[Path] = None) -> None:
        self.tools_dir = Path(tools_dir) if tools_dir else DEFAULT_TOOLS_DIR
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        (self.tools_dir / "repak").mkdir(parents=True, exist_ok=True)
        (self.tools_dir / "unreallocres").mkdir(parents=True, exist_ok=True)
        (self.tools_dir / "retoc").mkdir(parents=True, exist_ok=True)
        (self.tools_dir / "fonts").mkdir(parents=True, exist_ok=True)

    def get_tool_path(self, key: str) -> Path:
        spec = TOOL_SPECS[key]
        return self.tools_dir / spec["target_rel"]

    def is_installed(self, key: str) -> bool:
        p = self.get_tool_path(key)
        return p.exists() and p.is_file() and p.stat().st_size > 128

    def get_status(self) -> Dict[str, Any]:
        tools = {}
        ready_count = 0
        for key, spec in TOOL_SPECS.items():
            p = self.get_tool_path(key)
            ok = self.is_installed(key)
            if ok:
                ready_count += 1
            tools[key] = {
                "key": key,
                "name": spec["name"],
                "repo": spec["repo"],
                "path": str(p),
                "installed": ok,
                "size_bytes": p.stat().st_size if ok else 0,
            }
        return {
            "tools": tools,
            "ready_count": ready_count,
            "total_count": len(TOOL_SPECS),
            "all_ready": ready_count == len(TOOL_SPECS),
            "repak_ready": tools["repak"]["installed"],
            "unreallocres_ready": tools["unreallocres"]["installed"],
            "aesdumpster_ready": tools["aesdumpster"]["installed"],
            "retoc_ready": tools["retoc"]["installed"],
            "rpatool_ready": tools["rpatool"]["installed"],
            "fonts_ready": tools["font_notosans"]["installed"] and tools["font_bevietnam"]["installed"],
        }

    def _pick_windows_asset(self, key: str, assets: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if key in ("repak", "retoc"):
            for asset in assets:
                name = (asset.get("name") or "").lower()
                if "x86_64-pc-windows-msvc" in name and name.endswith(".zip"):
                    return asset
        elif key == "aesdumpster":
            for ext in (".exe", ".zip"):
                for asset in assets:
                    name = (asset.get("name") or "").lower()
                    if name.endswith(ext) and ("aesdumpster" in name or "win64" in name or "win" in name):
                        return asset
        elif key == "unreallocres":
            for ext in (".zip", ".exe"):
                for asset in assets:
                    name = (asset.get("name") or "").lower()
                    if name.endswith(ext) and ("win" in name or "x64" in name or "unreallocres" in name):
                        return asset
            if assets:
                return assets[0]
        return None

    async def _download_bytes(self, client: httpx.AsyncClient, url: str) -> bytes:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content

    def _extract_release_asset(self, raw: bytes, asset_name: str, target_path: Path, binary_name: str) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if asset_name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
                extracted_any = False
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    base = os.path.basename(member.filename)
                    if not base:
                        continue
                    # Extract target binary and sibling runtime DLLs if present
                    if base.lower() == binary_name.lower() or base.lower().endswith(".dll"):
                        out_file = target_path if base.lower() == binary_name.lower() else target_path.parent / base
                        with zf.open(member, "r") as src, open(out_file, "wb") as dst:
                            dst.write(src.read())
                        extracted_any = True
                if not target_path.exists():
                    bin_stem = Path(binary_name).stem.lower()
                    for member in zf.infolist():
                        if member.is_dir():
                            continue
                        base = os.path.basename(member.filename)
                        if base.lower().endswith(".exe") and bin_stem in base.lower():
                            with zf.open(member, "r") as src, open(target_path, "wb") as dst:
                                dst.write(src.read())
                            extracted_any = True
                            break
                if not extracted_any or not target_path.exists():
                    raise RuntimeError(f"{binary_name} not found inside {asset_name}")
        else:
            with open(target_path, "wb") as dst:
                dst.write(raw)

    async def _bootstrap_single(
        self,
        client: httpx.AsyncClient,
        key: str,
        force: bool,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        spec = TOOL_SPECS[key]
        target_path = self.get_tool_path(key)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if not force and self.is_installed(key):
            info = {"key": key, "status": "cached", "path": str(target_path), "message": f"{spec['name']} đã sẵn sàng"}
            if progress_cb:
                progress_cb(info)
            return info

        if progress_cb:
            progress_cb({"key": key, "status": "downloading", "path": str(target_path), "message": f"Đang tải {spec['name']}..."})

        try:
            kind = spec["kind"]
            if kind == "raw_file":
                last_err: Optional[Exception] = None
                content: Optional[bytes] = None
                for url in spec["urls"]:
                    try:
                        content = await self._download_bytes(client, url)
                        if content and len(content) > 128:
                            break
                    except Exception as exc:
                        last_err = exc
                if not content:
                    raise RuntimeError(str(last_err or "Download empty"))
                with open(target_path, "wb") as f:
                    f.write(content)
            else:
                api_url = f"https://api.github.com/repos/{spec['repo']}/releases/latest"
                rel_resp = await client.get(api_url, follow_redirects=True)
                rel_resp.raise_for_status()
                release = rel_resp.json()
                chosen = self._pick_windows_asset(key, release.get("assets", []))

                if not chosen:
                    # Fallback across recent releases (e.g. GHFear/AESDumpster latest tag 1.3 has empty assets, 1.2.5 has AESDumpster-Win64.exe)
                    list_url = f"https://api.github.com/repos/{spec['repo']}/releases?per_page=10"
                    list_resp = await client.get(list_url, follow_redirects=True)
                    list_resp.raise_for_status()
                    for rel_item in list_resp.json():
                        chosen = self._pick_windows_asset(key, rel_item.get("assets", []))
                        if chosen:
                            break

                if not chosen:
                    raise RuntimeError(f"Không tìm thấy release Windows phù hợp cho {spec['repo']}")

                raw = await self._download_bytes(client, chosen["browser_download_url"])
                self._extract_release_asset(raw, chosen["name"], target_path, spec["extract_binary"])

            info = {"key": key, "status": "installed", "path": str(target_path), "message": f"Đã cài {spec['name']}"}
            if progress_cb:
                progress_cb(info)
            return info
        except Exception as exc:
            logger.warning("Failed to bootstrap tool %s: %s", key, exc)
            info = {"key": key, "status": "error", "path": str(target_path), "message": f"Lỗi tải {spec['name']}: {exc}"}
            if progress_cb:
                progress_cb(info)
            return info

    async def ensure_tool(
        self,
        key: str,
        force: bool = False,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        if key not in TOOL_SPECS:
            raise KeyError(f"Unknown tool key: {key}")
        if not force and self.is_installed(key):
            return {
                "key": key,
                "status": "cached",
                "path": str(self.get_tool_path(key)),
                "message": f"{TOOL_SPECS[key]['name']} đã sẵn sàng",
            }
        async with httpx.AsyncClient(headers=HTTP_HEADERS, timeout=60.0) as client:
            return await self._bootstrap_single(client, key, force=force, progress_cb=progress_cb)

    async def ensure_all(
        self,
        force: bool = False,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        async with httpx.AsyncClient(headers=HTTP_HEADERS, timeout=60.0) as client:
            tasks = [
                self._bootstrap_single(client, key, force=force, progress_cb=progress_cb)
                for key in TOOL_SPECS
            ]
            done = await asyncio.gather(*tasks, return_exceptions=False)
            for item in done:
                results[item["key"]] = item
        return {
            "results": results,
            "status": self.get_status(),
        }


def bootstrap_sync(force: bool = False, tools_dir: Optional[Path] = None) -> Dict[str, Any]:
    boot = ToolBootstrapper(tools_dir=tools_dir)
    return asyncio.run(boot.ensure_all(force=force))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    res = bootstrap_sync(force=False)
    for k, v in res["results"].items():
        print(f"[{v['status']}] {k}: {v['message']}")
