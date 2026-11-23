"""Optional Offline Vietnamese Unicode Font Patcher using tools/fonts/."""

from __future__ import annotations

import gc
import re
import shutil
import struct
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core import get_app_dir
from core.atomic_io import atomic_write_bytes

APP_DIR = get_app_dir(__file__)
TOOLS_FONTS_DIR = APP_DIR / "tools" / "fonts"

_UNITY_PATCH_LATIN_RE = re.compile(
    r"^(?:NotoSans|LiberationSans|Roboto|Inter|OpenSans)(?:$|[-_,A-Za-z0-9]*?(?:Regular|Medium|Bold|VariableFont|Fallback))",
    re.IGNORECASE,
)
_UNITY_NON_LATIN_RE = re.compile(
    r"(?:KR|SC|TC|JP|JPN|Thai|Bengali|Devanagari|Arabic|Hebrew|Tamil|Telugu|Kannada)",
    re.IGNORECASE,
)


def _ttf_has_vietnamese(data: bytes) -> bool:
    """Fast pure-Python TTF cmap check for extended Vietnamese glyphs (ư, ơ, đ, ệ, ắ, ộ)."""
    if len(data) < 64:
        return False
    try:
        num_tables = struct.unpack_from(">H", data, 4)[0]
        cmap_off = None
        for i in range(num_tables):
            off = 12 + i * 16
            if off + 16 > len(data):
                break
            if data[off : off + 4] == b"cmap":
                cmap_off = struct.unpack_from(">I", data, off + 8)[0]
                break
        if cmap_off is None or cmap_off + 4 > len(data):
            return False
        num_sub = struct.unpack_from(">H", data, cmap_off + 2)[0]
        probe_cps = (ord("ư"), ord("ơ"), ord("đ"), ord("ệ"), ord("ắ"), ord("ộ"))
        covered = set()
        for i in range(num_sub):
            rec_off = cmap_off + 4 + i * 8
            if rec_off + 8 > len(data):
                break
            _plat, _enc, rel = struct.unpack_from(">HHI", data, rec_off)
            sub = cmap_off + rel
            if sub + 16 > len(data):
                continue
            fmt = struct.unpack_from(">H", data, sub)[0]
            if fmt == 4:
                seg2 = struct.unpack_from(">H", data, sub + 6)[0]
                seg_cnt = seg2 // 2
                if sub + 16 + 4 * seg2 > len(data):
                    continue
                end_c = struct.unpack_from(f">{seg_cnt}H", data, sub + 14)
                start_c = struct.unpack_from(f">{seg_cnt}H", data, sub + 16 + seg2)
                id_delta = struct.unpack_from(f">{seg_cnt}h", data, sub + 16 + 2 * seg2)
                id_off = struct.unpack_from(f">{seg_cnt}H", data, sub + 16 + 3 * seg2)
                for idx, (s, e, d, ro) in enumerate(zip(start_c, end_c, id_delta, id_off)):
                    if s == 0xFFFF:
                        continue
                    for ch in probe_cps:
                        if s <= ch <= e:
                            if ro == 0:
                                gid = (ch + d) & 0xFFFF
                            else:
                                g_off = sub + 16 + 3 * seg2 + idx * 2 + ro + (ch - s) * 2
                                gid = struct.unpack_from(">H", data, g_off)[0] if g_off + 2 <= len(data) else 0
                            if gid != 0:
                                covered.add(ch)
            elif fmt == 12:
                ng = struct.unpack_from(">I", data, sub + 12)[0]
                for j in range(ng):
                    g_off = sub + 16 + j * 12
                    if g_off + 12 > len(data):
                        break
                    sc, ec, _sg = struct.unpack_from(">III", data, g_off)
                    for ch in probe_cps:
                        if sc <= ch <= ec:
                            covered.add(ch)
        return len(covered) >= 6
    except Exception:
        return True  # fail safe: don't blindly overwrite corrupt/unknown blobs


class FontPatcher:
    """Copies BeVietnamPro / NotoSans fonts and patches Unity TMP dynamic fallback Bold fonts cleanly."""

    def __init__(self, tools_fonts_dir: Path | None = None) -> None:
        self.fonts_dir = Path(tools_fonts_dir) if tools_fonts_dir else TOOLS_FONTS_DIR

    def available_fonts(self) -> List[Path]:
        candidates = [
            self.fonts_dir / "BeVietnamPro-Regular.ttf",
            self.fonts_dir / "NotoSans-Regular.ttf",
        ]
        return [p for p in candidates if p.exists() and p.is_file() and p.stat().st_size > 1024]

    def _patch_unity_embedded_fonts(
        self,
        root: Path,
        font_path: Path,
        modified: List[str],
        backup_cb: Optional[Callable[[List[str], List[str]], None]] = None,
    ) -> List[str]:
        """Swap dynamic Latin Bold/Variable fallback fonts (e.g. NotoSans-Bold) inside Unity .assets with clean Regular TTF."""
        try:
            import UnityPy
        except Exception:
            return []

        patched_names: List[str] = []
        reg_bytes = font_path.read_bytes()
        data_dirs = [d for d in root.glob("*_Data") if d.is_dir()]
        asset_candidates: List[Path] = []
        for dd in data_dirs:
            res = dd / "resources.assets"
            if res.exists():
                asset_candidates.append(res)
            for sh in sorted(dd.glob("sharedassets*.assets")):
                asset_candidates.append(sh)

        for ap in asset_candidates:
            try:
                if ap.stat().st_size > 400 * 1024 * 1024:
                    continue
                env = UnityPy.load(str(ap))
                dirty = False
                local_patched: List[str] = []
                for obj in env.objects:
                    if obj.type.name != "Font":
                        continue
                    try:
                        tree = obj.read_typetree()
                    except Exception:
                        continue
                    fname = str(tree.get("m_Name") or "")
                    if not fname or _UNITY_NON_LATIN_RE.search(fname):
                        continue
                    raw_fd = tree.get("m_FontData") or b""
                    if isinstance(raw_fd, (bytes, bytearray)):
                        cur_data = bytes(raw_fd)
                    else:
                        cur_data = bytes((int(b) & 0xFF) for b in raw_fd)
                    if not cur_data or cur_data == reg_bytes:
                        continue
                    should_patch = bool(_UNITY_PATCH_LATIN_RE.search(fname)) or (
                        len(cur_data) < 2 * 1024 * 1024 and not _ttf_has_vietnamese(cur_data)
                    )
                    if not should_patch:
                        continue
                    tree["m_FontData"] = reg_bytes
                    obj.save_typetree(tree)
                    dirty = True
                    local_patched.append(fname)

                if dirty:
                    if backup_cb:
                        backup_cb([str(ap)], [])
                    out_asset_bytes = env.file.save()
                    del env
                    gc.collect()
                    atomic_write_bytes(ap, out_asset_bytes)
                    modified.append(str(ap))
                    patched_names.extend(local_patched)
                else:
                    del env
            except Exception:
                continue

        return patched_names

    def apply_font_patch(
        self,
        game_dir: str | Path,
        engine: str,
        target_slot: str = "vi",
        backup_cb: Optional[Callable[[List[str], List[str]], None]] = None,
    ) -> Dict[str, Any]:
        """Copy bundled Unicode fonts and patch Unity/RPGMaker/RenPy fonts safely; backs up modified assets first."""
        fonts = self.available_fonts()
        if not fonts:
            return {
                "applied": False,
                "reason": "Chưa có font trong tools/fonts/. Hãy chạy [Cập nhật Tools] trước.",
                "modified_files": [],
                "created_files": [],
            }

        root = Path(game_dir)
        eng_lower = engine.lower()
        primary_font = fonts[0]
        modified: List[str] = []
        created: List[str] = []
        patched_unity_fonts: List[str] = []

        # RPG Maker MV / MZ: overwrite or add to www/fonts or fonts/
        if "rpg" in eng_lower:
            font_folders = [root / "www" / "fonts", root / "fonts"]
            for ff in font_folders:
                if ff.exists():
                    dest = ff / primary_font.name
                    if dest.exists():
                        if backup_cb:
                            backup_cb([str(dest)], [])
                        modified.append(str(dest))
                    else:
                        created.append(str(dest))
                    shutil.copy2(primary_font, dest)

                    # Patch mplus-1m-regular.ttf if present (common RPG Maker font)
                    for legacy_name in ("mplus-1m-regular.ttf", "VL-Gothic-Regular.ttf"):
                        legacy_file = ff / legacy_name
                        if legacy_file.exists():
                            if backup_cb:
                                backup_cb([str(legacy_file)], [])
                            modified.append(str(legacy_file))
                            shutil.copy2(primary_font, legacy_file)

                    css_file = ff / "gamefont.css"
                    if css_file.exists():
                        if backup_cb:
                            backup_cb([str(css_file)], [])
                        modified.append(str(css_file))
                        old_css = css_file.read_text(encoding="utf-8", errors="ignore")
                        if primary_font.name not in old_css:
                            patch_css = (
                                f"@font-face {{\n  font-family: GameFont;\n  src: url('{primary_font.name}');\n}}\n"
                            )
                            css_file.write_text(patch_css, encoding="utf-8")
                    break

        # Ren'Py: copy into game/tl/<slot>/ or game/fonts/
        elif "ren" in eng_lower:
            tl_slot_dir = root / "game" / "tl" / target_slot
            fonts_dest_dir = tl_slot_dir if tl_slot_dir.exists() else (root / "game" / "fonts")
            fonts_dest_dir.mkdir(parents=True, exist_ok=True)
            for f in fonts:
                dest = fonts_dest_dir / f.name
                if dest.exists():
                    if backup_cb:
                        backup_cb([str(dest)], [])
                    modified.append(str(dest))
                else:
                    created.append(str(dest))
                shutil.copy2(f, dest)
            if tl_slot_dir.exists():
                hook_rpy = tl_slot_dir / "sakuga_font_override.rpy"
                created.append(str(hook_rpy))
                hook_rpy.write_text(
                    f'init -10 python:\n    style.default.font = "tl/{target_slot}/{primary_font.name}"\n',
                    encoding="utf-8",
                )

        # Unity / Unreal / Generic
        else:
            if (root / "Content").exists():
                dest_dir = root / "Content" / "Localization" / "SakugaFonts"
            else:
                streaming = list(root.glob("*_Data/StreamingAssets"))
                dest_dir = (streaming[0] / "SakugaFonts") if streaming else (root / "SakugaFonts")
            dest_dir.mkdir(parents=True, exist_ok=True)
            for f in fonts:
                dest = dest_dir / f.name
                if dest.exists():
                    if backup_cb:
                        backup_cb([str(dest)], [])
                    modified.append(str(dest))
                else:
                    created.append(str(dest))
                shutil.copy2(f, dest)

            if "unity" in eng_lower or list(root.glob("*_Data")):
                # Prefer NotoSans-Regular for Unity TMP metrics parity (avoids oversized/bold Vietnamese diacritics)
                unity_font = next((f for f in fonts if "NotoSans" in f.name), primary_font)
                patched_unity_fonts = self._patch_unity_embedded_fonts(
                    root, unity_font, modified, backup_cb=backup_cb
                )

        if backup_cb and created:
            backup_cb([], created)

        detail_extra = f" (đã thay font fallback Unity: {', '.join(patched_unity_fonts)})" if patched_unity_fonts else ""
        return {
            "applied": bool(modified or created),
            "font_used": primary_font.name,
            "patched_unity_fonts": patched_unity_fonts,
            "modified_files": modified,
            "created_files": created,
            "reason": f"Đã vá font {primary_font.name}{detail_extra}.",
        }
