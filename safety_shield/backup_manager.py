"""Clean External Differential Backup Manager with SHA-256 & 1-Click Restore."""

from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import get_app_dir
from core.atomic_io import atomic_write_text

APP_DIR = get_app_dir(__file__)
DEFAULT_BACKUPS_DIR = APP_DIR / "backups"


def compute_sha256(file_path: str | Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class BackupManager:
    """Stores differential original backups outside the game folder under <app_dir>/backups/."""

    def __init__(self, backups_root: Optional[Path] = None) -> None:
        self.backups_root = Path(backups_root) if backups_root else DEFAULT_BACKUPS_DIR
        self.backups_root.mkdir(parents=True, exist_ok=True)

    def _game_backup_dir(self, game_id: str) -> Path:
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(game_id))
        return self.backups_root / safe

    def _find_manifest_dir(self, game_id: str, game_dir: Optional[str] = None) -> Path:
        direct = self._game_backup_dir(game_id)
        if (direct / "manifest.json").exists():
            return direct
        # Fallback: match migrated backups (e.g. Content_Warning_6ffe9b57) by game_dir inside manifest.json
        if game_dir:
            norm_target = os.path.normcase(os.path.abspath(game_dir))
            for sub in self.backups_root.iterdir():
                if not sub.is_dir():
                    continue
                mf = sub / "manifest.json"
                if mf.exists():
                    try:
                        data = json.loads(mf.read_text(encoding="utf-8"))
                        if os.path.normcase(os.path.abspath(data.get("game_dir", ""))) == norm_target:
                            return sub
                    except Exception:
                        pass
        return direct

    def has_backup(self, game_id: str, game_dir: Optional[str] = None) -> bool:
        bdir = self._find_manifest_dir(game_id, game_dir=game_dir)
        return (bdir / "manifest.json").exists() and (bdir / "original_backup.zip").exists()

    def get_backup_info(self, game_id: str, game_dir: Optional[str] = None) -> Dict[str, Any]:
        bdir = self._find_manifest_dir(game_id, game_dir=game_dir)
        mf = bdir / "manifest.json"
        zpath = bdir / "original_backup.zip"
        if not mf.exists():
            return {"has_backup": False, "backup_dir": str(bdir), "file_count": 0, "created_count": 0}
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
            return {
                "has_backup": zpath.exists(),
                "backup_dir": str(bdir),
                "zip_path": str(zpath),
                "zip_size_bytes": zpath.stat().st_size if zpath.exists() else 0,
                "file_count": len(manifest.get("files", {})),
                "created_count": len(manifest.get("created_files", [])),
                "updated_at": manifest.get("updated_at"),
                "game_dir": manifest.get("game_dir"),
            }
        except Exception as exc:
            return {"has_backup": False, "backup_dir": str(bdir), "error": str(exc)}

    def _load_manifest(self, bdir: Path, game_id: str, game_dir: str) -> Dict[str, Any]:
        mf = bdir / "manifest.json"
        if mf.exists():
            return json.loads(mf.read_text(encoding="utf-8"))
        return {
            "game_id": game_id,
            "game_dir": str(Path(game_dir).resolve()),
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
            "files": {},
            "created_files": [],
        }

    def backup_files(
        self,
        game_id: str,
        game_dir: str | Path,
        modified_files: List[str | Path],
        created_files: Optional[List[str | Path]] = None,
    ) -> Dict[str, Any]:
        """Differential backup: preserves the FIRST clean copy of every modified file."""
        bdir = self._find_manifest_dir(game_id, game_dir=str(game_dir))
        bdir.mkdir(parents=True, exist_ok=True)
        zpath = bdir / "original_backup.zip"
        mf = bdir / "manifest.json"

        manifest = self._load_manifest(bdir, game_id, str(game_dir))
        game_root = Path(game_dir).resolve()
        files_map: Dict[str, Dict[str, Any]] = manifest.setdefault("files", {})
        created_list: List[str] = manifest.setdefault("created_files", [])

        added_count = 0
        pending_files: List[Tuple[Path, str, str, str, int]] = []
        existing_arcnames = set()
        if zpath.exists():
            try:
                with zipfile.ZipFile(zpath, "r") as zf_r:
                    existing_arcnames = set(zf_r.namelist())
            except Exception:
                existing_arcnames = set()

        for raw_path in modified_files:
            fpath = Path(raw_path).resolve()
            if not fpath.exists() or not fpath.is_file():
                continue
            try:
                rel = fpath.relative_to(game_root).as_posix()
            except ValueError:
                rel = fpath.name
            abs_key = str(fpath)
            if abs_key in files_map or rel in files_map:
                # Original clean version already safely archived
                continue
            arcname = rel
            if arcname in existing_arcnames:
                arcname = f"{hashlib.md5(abs_key.encode()).hexdigest()[:8]}_{fpath.name}"
            sha = compute_sha256(fpath)
            existing_arcnames.add(arcname)
            pending_files.append((fpath, abs_key, rel, arcname, fpath.stat().st_size))
            files_map[abs_key] = {
                "arcname": arcname,
                "rel_path": rel,
                "abs_path": abs_key,
                "sha256": sha,
                "size": fpath.stat().st_size,
                "backed_up_at": int(time.time()),
            }
            added_count += 1

        if pending_files or not zpath.exists():
            # Copy-on-write atomic zip update so killing app mid-zip never corrupts original_backup.zip
            tmp_zip = bdir / f"{zpath.name}.sakuga_tmp"
            try:
                if zpath.exists():
                    shutil.copy2(zpath, tmp_zip)
                with zipfile.ZipFile(tmp_zip, "a", compression=zipfile.ZIP_DEFLATED) as zf:
                    for fpath, _abs_key, _rel, arcname, _sz in pending_files:
                        zf.write(fpath, arcname=arcname)
                os.replace(tmp_zip, zpath)
            except Exception:
                try:
                    if tmp_zip.exists():
                        tmp_zip.unlink()
                except OSError:
                    pass
                raise

        if created_files:
            seen_created = {os.path.normcase(os.path.abspath(p)) for p in created_list}
            for cpath in created_files:
                abs_c = str(Path(cpath).resolve())
                if os.path.normcase(abs_c) not in seen_created:
                    # Only track as created_file if it wasn't originally backed up
                    if abs_c not in files_map:
                        created_list.append(abs_c)
                        seen_created.add(os.path.normcase(abs_c))

        manifest["updated_at"] = int(time.time())
        atomic_write_text(mf, json.dumps(manifest, indent=2, ensure_ascii=False))

        return {
            "backup_dir": str(bdir),
            "added_count": added_count,
            "total_backed_up": len(files_map),
            "created_tracked": len(created_list),
        }

    def restore_original(self, game_id: str, game_dir: Optional[str] = None) -> Dict[str, Any]:
        """Restore every backed-up file with SHA-256 verification and delete generated files."""
        bdir = self._find_manifest_dir(game_id, game_dir=game_dir)
        mf = bdir / "manifest.json"
        zpath = bdir / "original_backup.zip"

        if not mf.exists() or not zpath.exists():
            return {"ok": False, "restored_count": 0, "removed_count": 0, "error": "Chưa có bản sao lưu gốc."}

        manifest = json.loads(mf.read_text(encoding="utf-8"))
        files_map: Dict[str, Dict[str, Any]] = manifest.get("files", {})
        created_list: List[str] = manifest.get("created_files", [])
        created_dirs: List[str] = manifest.get("created_dirs", [])
        root_dir = Path(manifest.get("game_dir", game_dir or ""))

        def _resolve_rel_or_abs(raw_p: str) -> Path:
            p = Path(raw_p)
            return p if p.is_absolute() or not str(root_dir) else (root_dir / p)

        created_resolved = {os.path.normcase(str(_resolve_rel_or_abs(c))) for c in created_list}

        restored: List[str] = []
        sha_mismatches: List[str] = []
        removed: List[str] = []

        with zipfile.ZipFile(zpath, "r") as zf:
            for abs_key, meta in files_map.items():
                arcname = meta.get("arcname") or abs_key
                expected_sha = meta["sha256"]
                target = Path(meta.get("abs_path") or _resolve_rel_or_abs(abs_key))
                if os.path.normcase(str(target)) in created_resolved:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                data = zf.read(arcname)
                actual_sha = hashlib.sha256(data).hexdigest()
                if actual_sha != expected_sha:
                    sha_mismatches.append(str(target))
                    continue
                target.write_bytes(data)
                restored.append(str(target))

        # Remove extra files created by localization patching (mod paks, fonts, extra .rpy)
        for c_path_str in created_list:
            c_path = _resolve_rel_or_abs(c_path_str)
            if c_path.exists():
                if c_path.is_file():
                    c_path.unlink(missing_ok=True)
                    removed.append(str(c_path))
                elif c_path.is_dir():
                    shutil.rmtree(c_path, ignore_errors=True)
                    removed.append(str(c_path))
            # Prune empty ~mods folder if created cleanly
            try:
                if root_dir and c_path.parent.name == "~mods" and c_path.parent.exists():
                    if not any(c_path.parent.iterdir()):
                        c_path.parent.rmdir()
            except Exception:
                pass

        for d_str in created_dirs:
            d_path = _resolve_rel_or_abs(d_str)
            if d_path.exists() and d_path.is_dir():
                shutil.rmtree(d_path, ignore_errors=True)
                removed.append(str(d_path))

        return {
            "ok": len(sha_mismatches) == 0,
            "restored_count": len(restored),
            "removed_count": len(removed),
            "sha_verified": len(sha_mismatches) == 0,
            "sha_mismatches": sha_mismatches,
            "restored_files": restored,
            "removed_files": removed,
        }

    def extract_entries_preferring_backup(
        self,
        game_id: str,
        game_dir: str | Path,
        slot_info: Dict[str, Any],
        extract_fn: Callable[..., Dict[str, Any]],
        scope: str = "FULL_PATCH",
        translate_choices: bool = True,
    ) -> Dict[str, Any]:
        """Extract slot entries from clean original_backup.zip whenever backup files cover this slot.

        Prevents re-scanning or repairing `source_lang == inject_slot` from reading overwritten
        target text or missing entries skipped by older extractor filters.
        """
        bdir = self._find_manifest_dir(game_id, game_dir=str(game_dir))
        mf = bdir / "manifest.json"
        zpath = bdir / "original_backup.zip"
        orig_files = list(slot_info.get("files") or [])
        if not mf.exists() or not zpath.exists() or not orig_files:
            return extract_fn(game_dir, slot_info, scope, translate_choices)

        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            return extract_fn(game_dir, slot_info, scope, translate_choices)

        files_map: Dict[str, Dict[str, Any]] = manifest.get("files", {})
        root_dir = Path(manifest.get("game_dir") or game_dir).resolve()
        by_norm_abs: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        for k, meta in files_map.items():
            ap = meta.get("abs_path") or k
            by_norm_abs[os.path.normcase(os.path.abspath(ap))] = (k, meta)

        matched: List[Tuple[str, str, Dict[str, Any]]] = []
        for fp in orig_files:
            norm_fp = os.path.normcase(os.path.abspath(fp))
            if norm_fp in by_norm_abs:
                _, meta = by_norm_abs[norm_fp]
                matched.append((fp, meta.get("arcname") or meta.get("rel_path") or Path(fp).name, meta))

        if not matched:
            return extract_fn(game_dir, slot_info, scope, translate_choices)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            tmp_root = Path(tmp_dir)
            tmp_to_orig: Dict[str, str] = {}
            patched_files: List[str] = []

            with zipfile.ZipFile(zpath, "r") as zf:
                zip_names = set(zf.namelist())
                for orig_fp, arcname, meta in matched:
                    if arcname not in zip_names:
                        continue
                    rel_part = meta.get("rel_path") or Path(orig_fp).name
                    dst = (tmp_root / rel_part).resolve()
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(zf.read(arcname))
                    tmp_to_orig[os.path.normcase(str(dst))] = str(orig_fp)
                    patched_files.append(str(dst))

            for orig_fp in orig_files:
                norm_orig = os.path.normcase(os.path.abspath(orig_fp))
                if not any(os.path.normcase(os.path.abspath(m[0])) == norm_orig for m in matched):
                    patched_files.append(orig_fp)

            if not tmp_to_orig:
                return extract_fn(game_dir, slot_info, scope, translate_choices)

            patched_slot = dict(slot_info)
            patched_slot["files"] = patched_files
            res = extract_fn(str(root_dir), patched_slot, scope, translate_choices)
            for entry in res.get("entries", []):
                norm_efp = os.path.normcase(os.path.abspath(entry.file_path))
                if norm_efp in tmp_to_orig:
                    entry.file_path = tmp_to_orig[norm_efp]
            gc.collect()
            return res

    def _resolve_tracked_path(self, meta: Dict[str, Any], abs_key: str, root_dir: Path) -> Path:
        raw = meta.get("abs_path") or abs_key
        p = Path(raw)
        if not p.is_absolute() and str(root_dir):
            return (root_dir / p).resolve()
        if not p.exists() and meta.get("rel_path") and str(root_dir):
            cand = (root_dir / meta["rel_path"]).resolve()
            if cand.exists():
                return cand
        return p

    def record_patched_state(self, game_id: str, game_dir: Optional[str] = None) -> Dict[str, str]:
        """Snapshot SHA-256 of patched localization files in manifest.json to detect future Steam updates."""
        bdir = self._find_manifest_dir(game_id, game_dir=game_dir)
        mf = bdir / "manifest.json"
        if not mf.exists():
            return {}
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            return {}
        files_map: Dict[str, Dict[str, Any]] = manifest.get("files", {})
        root_dir = Path(manifest.get("game_dir") or game_dir or "")
        out_hashes: Dict[str, str] = {}
        dirty = False
        for abs_key, meta in files_map.items():
            p = self._resolve_tracked_path(meta, abs_key, root_dir)
            if p.exists() and p.is_file():
                sha = compute_sha256(p)
                sz = p.stat().st_size
                if meta.get("patched_sha256") != sha or meta.get("patched_size") != sz:
                    meta["patched_sha256"] = sha
                    meta["patched_size"] = sz
                    dirty = True
                out_hashes[abs_key] = sha
        if dirty:
            manifest["patched_at"] = int(time.time())
            atomic_write_text(mf, json.dumps(manifest, indent=2, ensure_ascii=False))
        return out_hashes

    def check_game_update_status(
        self,
        game_id: str,
        game_dir: Optional[str] = None,
        is_patched: bool = False,
    ) -> Dict[str, Any]:
        """Compare live game files against recorded patched & vanilla hashes to detect Steam updates/verify."""
        if not is_patched or not self.has_backup(game_id, game_dir=game_dir):
            return {
                "detected": False,
                "state": "CLEAN",
                "updated_files": [],
                "reverted_files": [],
                "message": "",
            }
        bdir = self._find_manifest_dir(game_id, game_dir=game_dir)
        mf = bdir / "manifest.json"
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            return {
                "detected": False,
                "state": "CLEAN",
                "updated_files": [],
                "reverted_files": [],
                "message": "",
            }

        files_map: Dict[str, Dict[str, Any]] = manifest.get("files", {})
        root_dir = Path(manifest.get("game_dir") or game_dir or "")
        updated_files: List[str] = []
        reverted_files: List[str] = []
        dirty = False

        for abs_key, meta in files_map.items():
            p = self._resolve_tracked_path(meta, abs_key, root_dir)
            if not p.exists() or not p.is_file():
                continue
            cur_sha = compute_sha256(p)
            orig_sha = meta.get("sha256") or ""
            patched_sha = meta.get("patched_sha256") or ""
            if not patched_sha:
                if orig_sha and cur_sha == orig_sha:
                    reverted_files.append(str(p))
                else:
                    meta["patched_sha256"] = cur_sha
                    meta["patched_size"] = p.stat().st_size
                    dirty = True
            elif cur_sha != patched_sha:
                if orig_sha and cur_sha == orig_sha:
                    reverted_files.append(str(p))
                else:
                    updated_files.append(str(p))

        if dirty:
            manifest["patched_at"] = int(time.time())
            atomic_write_text(mf, json.dumps(manifest, indent=2, ensure_ascii=False))

        if updated_files:
            msg = f"Phát hiện {len(updated_files)} file bản địa hóa vừa thay đổi do game cập nhật (bản dịch cũ vẫn còn nguyên trong cache)."
            state = "GAME_UPDATED"
        elif reverted_files:
            msg = f"Phát hiện {len(reverted_files)} file đã trở về bản gốc (có thể do Steam Verify / Update)."
            state = "STEAM_VERIFIED"
        else:
            msg = ""
            state = "OK"
        return {
            "detected": bool(updated_files or reverted_files),
            "state": state,
            "updated_files": updated_files,
            "reverted_files": reverted_files,
            "message": msg,
        }

    def refresh_baseline_for_updated_files(
        self,
        game_id: str,
        game_dir: str | Path,
        updated_files: List[str],
    ) -> int:
        """Update original_backup.zip and manifest SHA-256 baselines for newly updated game files."""
        bdir = self._find_manifest_dir(game_id, game_dir=str(game_dir))
        mf = bdir / "manifest.json"
        zpath = bdir / "original_backup.zip"
        if not mf.exists() or not zpath.exists() or not updated_files:
            return 0
        manifest = json.loads(mf.read_text(encoding="utf-8"))
        files_map: Dict[str, Dict[str, Any]] = manifest.get("files", {})
        root_dir = Path(manifest.get("game_dir") or game_dir).resolve()
        target_norms = {os.path.normcase(os.path.abspath(p)) for p in updated_files}

        replacements: Dict[str, Path] = {}
        refreshed = 0
        for abs_key, meta in files_map.items():
            p = self._resolve_tracked_path(meta, abs_key, root_dir)
            if os.path.normcase(os.path.abspath(str(p))) in target_norms and p.exists() and p.is_file():
                arcname = meta.get("arcname") or meta.get("rel_path") or p.name
                replacements[arcname] = p
                meta["sha256"] = compute_sha256(p)
                meta["size"] = p.stat().st_size
                meta.pop("patched_sha256", None)
                meta.pop("patched_size", None)
                meta["updated_baseline_at"] = int(time.time())
                refreshed += 1

        if not replacements:
            return 0

        tmp_zip = bdir / f"{zpath.name}.sakuga_tmp"
        try:
            with zipfile.ZipFile(zpath, "r") as zf_r, zipfile.ZipFile(
                tmp_zip, "w", compression=zipfile.ZIP_DEFLATED
            ) as zf_w:
                for info in zf_r.infolist():
                    if info.filename in replacements:
                        zf_w.write(replacements[info.filename], arcname=info.filename)
                    else:
                        zf_w.writestr(info, zf_r.read(info.filename))
            os.replace(tmp_zip, zpath)
        except Exception:
            try:
                if tmp_zip.exists():
                    tmp_zip.unlink()
            except OSError:
                pass
            raise

        manifest["updated_at"] = int(time.time())
        atomic_write_text(mf, json.dumps(manifest, indent=2, ensure_ascii=False))
        return refreshed
