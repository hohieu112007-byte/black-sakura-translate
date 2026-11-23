"""SQLite Layer: library.db, global_tm.db (0ms hit), and per-game Streaming cache/<game_id>.db + RapidFuzz RAG Glossary."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rapidfuzz import fuzz

from core import get_app_dir
from safety_shield.tag_firewall import fold_source_numbers, mask_tags, unfold_numbers

APP_DIR = get_app_dir(__file__)


def sha1_text(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


class LibraryDB:
    """Manages <app_dir>/library.db storing games, launch profiles, and Anti-Cheat state."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else (APP_DIR / "library.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS games (
                    game_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    game_dir TEXT NOT NULL UNIQUE,
                    exe_path TEXT DEFAULT '',
                    steam_appid TEXT DEFAULT '',
                    launch_mode TEXT DEFAULT 'direct_exe',
                    shortcut_path TEXT DEFAULT '',
                    cover_path TEXT DEFAULT '',
                    engine TEXT DEFAULT 'Unknown',
                    method TEXT DEFAULT '',
                    anticheat_status TEXT DEFAULT 'SAFE',
                    anticheat_vendors TEXT DEFAULT '[]',
                    anticheat_files TEXT DEFAULT '[]',
                    detected_slots_json TEXT DEFAULT '[]',
                    source_lang TEXT DEFAULT 'en',
                    target_lang TEXT DEFAULT 'vi',
                    inject_slot TEXT DEFAULT '',
                    scope TEXT DEFAULT 'FULL_PATCH',
                    game_description TEXT DEFAULT '',
                    tone_preset TEXT DEFAULT 'natural',
                    style_skill_id TEXT DEFAULT 'natural_standard',
                    lang_skill_override TEXT DEFAULT 'auto',
                    applied_skill_signature TEXT DEFAULT '',
                    compiled_style_card TEXT DEFAULT '',
                    progress_pct REAL DEFAULT 0.0,
                    total_strings INTEGER DEFAULT 0,
                    translated_strings INTEGER DEFAULT 0,
                    is_patched INTEGER DEFAULT 0,
                    last_played_at INTEGER DEFAULT 0,
                    created_at INTEGER DEFAULT 0,
                    updated_at INTEGER DEFAULT 0
                )
                """
            )
            # Safe schema migration for existing library.db files
            existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(games)").fetchall()}
            for col_name, col_def in (
                ("game_description", "TEXT DEFAULT ''"),
                ("tone_preset", "TEXT DEFAULT 'natural'"),
                ("style_skill_id", "TEXT DEFAULT 'natural_standard'"),
                ("lang_skill_override", "TEXT DEFAULT 'auto'"),
                ("applied_skill_signature", "TEXT DEFAULT ''"),
                ("compiled_style_card", "TEXT DEFAULT ''"),
            ):
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE games ADD COLUMN {col_name} {col_def}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def upsert_game(self, record: Dict[str, Any]) -> Dict[str, Any]:
        now = int(time.time())
        with self._connect() as conn:
            existing = conn.execute("SELECT * FROM games WHERE game_dir = ?", (record["game_dir"],)).fetchone()
            if existing:
                game_id = existing["game_id"]
                merged = dict(existing)
                for k, v in record.items():
                    if v is None:
                        continue
                    # Preserve user-edited/saved game_description, tone_preset, compiled_style_card unless empty
                    if k in ("game_description", "compiled_style_card") and (existing[k] or "").strip():
                        continue
                    if k == "tone_preset" and (existing[k] or "").strip():
                        continue
                    merged[k] = v
                merged["game_id"] = game_id
                merged["updated_at"] = now
            else:
                merged = {
                    "game_id": record["game_id"],
                    "title": record.get("title", "Unknown Game"),
                    "game_dir": record["game_dir"],
                    "exe_path": record.get("exe_path", ""),
                    "steam_appid": record.get("steam_appid", ""),
                    "launch_mode": record.get("launch_mode", "direct_exe"),
                    "shortcut_path": record.get("shortcut_path", ""),
                    "cover_path": record.get("cover_path", ""),
                    "engine": record.get("engine", "Unknown"),
                    "method": record.get("method", ""),
                    "anticheat_status": record.get("anticheat_status", "SAFE"),
                    "anticheat_vendors": record.get("anticheat_vendors", "[]"),
                    "anticheat_files": record.get("anticheat_files", "[]"),
                    "detected_slots_json": record.get("detected_slots_json", "[]"),
                    "source_lang": record.get("source_lang", "en"),
                    "target_lang": record.get("target_lang", "vi"),
                    "inject_slot": record.get("inject_slot", ""),
                    "scope": record.get("scope", "FULL_PATCH"),
                    "game_description": record.get("game_description", ""),
                    "tone_preset": record.get("tone_preset") or "natural",
                    "style_skill_id": record.get("style_skill_id") or record.get("tone_preset") or "natural_standard",
                    "lang_skill_override": record.get("lang_skill_override") or "auto",
                    "applied_skill_signature": record.get("applied_skill_signature", ""),
                    "compiled_style_card": record.get("compiled_style_card", ""),
                    "progress_pct": record.get("progress_pct", 0.0),
                    "total_strings": record.get("total_strings", 0),
                    "translated_strings": record.get("translated_strings", 0),
                    "is_patched": record.get("is_patched", 0),
                    "last_played_at": record.get("last_played_at", 0),
                    "created_at": now,
                    "updated_at": now,
                }

            cols = list(merged.keys())
            placeholders = ", ".join("?" for _ in cols)
            col_names = ", ".join(cols)
            updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "game_id")
            conn.execute(
                f"INSERT INTO games ({col_names}) VALUES ({placeholders}) ON CONFLICT(game_id) DO UPDATE SET {updates}",
                [merged[c] for c in cols],
            )
            conn.commit()
        return self.get_game(merged["game_id"])

    def _hydrate(self, row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        d = dict(row)
        d["anticheat_vendors"] = json.loads(d.get("anticheat_vendors") or "[]")
        d["anticheat_files"] = json.loads(d.get("anticheat_files") or "[]")
        d["detected_slots"] = json.loads(d.get("detected_slots_json") or "[]")
        return d

    def get_game(self, game_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
            return self._hydrate(row)

    def list_games(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM games ORDER BY updated_at DESC, created_at DESC").fetchall()
            return [self._hydrate(r) for r in rows if r is not None]

    def update_fields(self, game_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get_game(game_id)
        fields["updated_at"] = int(time.time())
        serialized: Dict[str, Any] = {}
        for k, v in fields.items():
            if k in ("anticheat_vendors", "anticheat_files", "detected_slots_json") and not isinstance(v, str):
                serialized[k] = json.dumps(v, ensure_ascii=False)
            else:
                serialized[k] = v
        sets = ", ".join(f"{k}=?" for k in serialized.keys())
        vals = list(serialized.values()) + [game_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE games SET {sets} WHERE game_id = ?", vals)
            conn.commit()
        return self.get_game(game_id)

    def delete_game(self, game_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM games WHERE game_id = ?", (game_id,))
            conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, str(value)),
            )
            conn.commit()

    def set_job_journal(self, game_id: str, info: Dict[str, Any]) -> None:
        self.set_setting(f"active_job::{game_id}", json.dumps(info, ensure_ascii=False))

    def get_job_journal(self, game_id: str) -> Optional[Dict[str, Any]]:
        raw = self.get_setting(f"active_job::{game_id}", "")
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def clear_job_journal(self, game_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (f"active_job::{game_id}",))
            conn.commit()


class GlobalTMDB:
    """Persistent cross-game Translation Memory providing 0ms exact & RapidFuzz fuzzy hits."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else (APP_DIR / "global_tm.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tm_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_lang TEXT NOT NULL,
                    target_lang TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    translated_text TEXT NOT NULL,
                    domain TEXT DEFAULT 'UI',
                    updated_at INTEGER DEFAULT 0,
                    UNIQUE(source_lang, target_lang, source_hash)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tm_pair ON tm_entries(source_lang, target_lang, source_hash)")
            conn.commit()

    def lookup_exact_batch(self, texts: List[str], source_lang: str, target_lang: str) -> Dict[str, str]:
        if not texts:
            return {}
        hashes = {sha1_text(t): t for t in texts}
        found: Dict[str, str] = {}
        keys = list(hashes.keys())
        with self._connect() as conn:
            for i in range(0, len(keys), 500):
                chunk = keys[i : i + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT source_hash, translated_text FROM tm_entries WHERE source_lang=? AND target_lang=? AND source_hash IN ({placeholders})",
                    [source_lang, target_lang] + chunk,
                ).fetchall()
                for r in rows:
                    src = hashes[r["source_hash"]]
                    found[src] = r["translated_text"]
        return found

    def upsert_batch(self, items: List[Tuple[str, str, str]], source_lang: str, target_lang: str) -> int:
        """items: [(source_text, translated_text, domain)]"""
        if not items:
            return 0
        now = int(time.time())
        rows = [(source_lang, target_lang, sha1_text(s), s, t, d, now) for (s, t, d) in items if s and t]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO tm_entries(source_lang, target_lang, source_hash, source_text, translated_text, domain, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_lang, target_lang, source_hash) DO UPDATE SET
                    translated_text=excluded.translated_text,
                    domain=excluded.domain,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def search_tm(self, query: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if query.strip():
                like = f"%{query.strip()}%"
                rows = conn.execute(
                    "SELECT * FROM tm_entries WHERE source_text LIKE ? OR translated_text LIKE ? ORDER BY updated_at DESC LIMIT ?",
                    (like, like, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM tm_entries ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def stats(self) -> Dict[str, int]:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM tm_entries").fetchone()
            return {"count": int(row["cnt"]) if row else 0}

    def delete_entry(self, entry_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM tm_entries WHERE id = ?", (entry_id,))
            conn.commit()

    def delete_by_sources(self, source_texts: List[str], source_lang: str, target_lang: str) -> int:
        if not source_texts:
            return 0
        hashes = [sha1_text(s) for s in source_texts if s]
        deleted = 0
        with self._connect() as conn:
            for i in range(0, len(hashes), 400):
                chunk = hashes[i : i + 400]
                placeholders = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    f"DELETE FROM tm_entries WHERE source_lang=? AND target_lang=? AND source_hash IN ({placeholders})",
                    [source_lang, target_lang] + chunk,
                )
                deleted += cur.rowcount or 0
            conn.commit()
        return deleted

    def purge_anomalies(self, anomaly_check_fn, source_lang: str = "en", target_lang: str = "vi") -> int:
        if not anomaly_check_fn:
            return 0
        bad_ids: List[int] = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, source_text, translated_text FROM tm_entries WHERE source_lang=? AND target_lang=?",
                (source_lang, target_lang),
            ).fetchall()
            for r in rows:
                try:
                    bad, _ = anomaly_check_fn(r["source_text"], r["translated_text"], target_lang, source_lang)
                except TypeError:
                    bad, _ = anomaly_check_fn(r["source_text"], r["translated_text"], target_lang)
                if bad:
                    bad_ids.append(int(r["id"]))
            for i in range(0, len(bad_ids), 400):
                chunk = bad_ids[i : i + 400]
                ph = ",".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM tm_entries WHERE id IN ({ph})", chunk)
            conn.commit()
        return len(bad_ids)


class GameCacheDB:
    """Per-game SQLite streaming store (<app_dir>/cache/<game_id>.db) with deduplication & RapidFuzz RAG Glossary."""

    def __init__(self, game_id: str, cache_dir: Optional[Path] = None) -> None:
        cdir = Path(cache_dir) if cache_dir else (APP_DIR / "cache")
        cdir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(game_id))
        self.db_path = cdir / f"{safe}.db"
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS unique_strings (
                    str_hash TEXT PRIMARY KEY,
                    source_lang TEXT NOT NULL,
                    target_lang TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    masked_text TEXT NOT NULL,
                    tag_map_json TEXT DEFAULT '{}',
                    num_map_json TEXT DEFAULT '{}',
                    category TEXT DEFAULT 'UI',
                    translated_text TEXT DEFAULT '',
                    status TEXT DEFAULT 'PENDING',
                    tag_status TEXT DEFAULT 'PENDING',
                    manual_locked INTEGER DEFAULT 0,
                    updated_at INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS string_locations (
                    loc_id TEXT PRIMARY KEY,
                    str_hash TEXT NOT NULL,
                    entry_key TEXT DEFAULT '',
                    file_path TEXT DEFAULT '',
                    fmt TEXT DEFAULT '',
                    category TEXT DEFAULT 'UI',
                    raw_source_text TEXT DEFAULT '',
                    num_map_json TEXT DEFAULT '{}',
                    location_json TEXT DEFAULT '{}',
                    FOREIGN KEY (str_hash) REFERENCES unique_strings(str_hash)
                );

                CREATE TABLE IF NOT EXISTS glossary_terms (
                    term_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_lang TEXT NOT NULL,
                    target_lang TEXT NOT NULL,
                    source_term TEXT NOT NULL,
                    target_term TEXT NOT NULL,
                    note TEXT DEFAULT '',
                    UNIQUE(source_lang, target_lang, source_term)
                );

                CREATE TABLE IF NOT EXISTS prethink_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thinking_text TEXT DEFAULT '',
                    created_at INTEGER DEFAULT 0
                );
                """
            )
            # Safe migrations for existing game cache DBs
            u_cols = {r["name"] for r in conn.execute("PRAGMA table_info(unique_strings)").fetchall()}
            if "num_map_json" not in u_cols:
                conn.execute("ALTER TABLE unique_strings ADD COLUMN num_map_json TEXT DEFAULT '{}'")
            if "manual_locked" not in u_cols:
                conn.execute("ALTER TABLE unique_strings ADD COLUMN manual_locked INTEGER DEFAULT 0")
            l_cols = {r["name"] for r in conn.execute("PRAGMA table_info(string_locations)").fetchall()}
            if "raw_source_text" not in l_cols:
                conn.execute("ALTER TABLE string_locations ADD COLUMN raw_source_text TEXT DEFAULT ''")
            if "num_map_json" not in l_cols:
                conn.execute("ALTER TABLE string_locations ADD COLUMN num_map_json TEXT DEFAULT '{}'")
            conn.commit()

    def store_extracted_entries(
        self,
        entries: List[Any],
        source_lang: str,
        target_lang: str,
        mask_fn=None,
    ) -> Dict[str, Any]:
        """Deduplicate & Template-Fold (__TAG_x__ + __NUM_x__) entries into unique_strings."""
        now = int(time.time())
        unique_map: Dict[str, Tuple] = {}
        location_rows: List[Tuple] = []
        exact_texts: set[str] = set()
        use_folding = mask_fn is None or mask_fn is mask_tags

        for e in entries:
            exact_texts.add(e.text)
            if use_folding:
                folded_src, folded_masked, tag_map, num_map = fold_source_numbers(e.text)
            else:
                folded_src = e.text
                folded_masked, tag_map = mask_fn(e.text)
                num_map = {}

            h = f"{source_lang}:{target_lang}:{sha1_text(folded_src)}"
            nmap_json = json.dumps(num_map, ensure_ascii=False)
            if h not in unique_map:
                unique_map[h] = (
                    h,
                    source_lang,
                    target_lang,
                    folded_src,
                    folded_masked,
                    json.dumps(tag_map, ensure_ascii=False),
                    nmap_json,
                    e.category,
                    now,
                )
            location_rows.append(
                (
                    e.entry_id,
                    h,
                    e.key,
                    e.file_path,
                    e.fmt,
                    e.category,
                    e.text,
                    nmap_json,
                    json.dumps(e.location_meta, ensure_ascii=False),
                )
            )

        with self._connect() as conn:
            conn.execute("DELETE FROM string_locations")
            # Preserve already-translated & manual_locked hashes when re-scanning same pair!
            conn.executemany(
                """
                INSERT INTO unique_strings(str_hash, source_lang, target_lang, source_text, masked_text, tag_map_json, num_map_json, category, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(str_hash) DO UPDATE SET
                    category=excluded.category,
                    num_map_json=excluded.num_map_json
                """,
                list(unique_map.values()),
            )
            conn.executemany(
                """
                INSERT INTO string_locations(loc_id, str_hash, entry_key, file_path, fmt, category, raw_source_text, num_map_json, location_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(loc_id) DO UPDATE SET
                    str_hash=excluded.str_hash,
                    entry_key=excluded.entry_key,
                    file_path=excluded.file_path,
                    category=excluded.category,
                    raw_source_text=excluded.raw_source_text,
                    num_map_json=excluded.num_map_json
                """,
                location_rows,
            )
            # Prune stale unreferenced PENDING rows (e.g. after restoring original game files)
            conn.execute(
                """
                DELETE FROM unique_strings
                WHERE source_lang = ? AND target_lang = ? AND status = 'PENDING'
                  AND str_hash NOT IN (SELECT DISTINCT str_hash FROM string_locations)
                """,
                (source_lang, target_lang),
            )
            conn.commit()

        raw_cnt = len(entries)
        exact_uniq_cnt = len(exact_texts)
        uniq_cnt = len(unique_map)
        num_folded_saved = max(0, exact_uniq_cnt - uniq_cnt)
        dedup_pct = round(((raw_cnt - uniq_cnt) / raw_cnt) * 100.0, 1) if raw_cnt > 0 else 0.0
        return {
            "raw_count": raw_cnt,
            "exact_unique_count": exact_uniq_cnt,
            "unique_count": uniq_cnt,
            "num_folded_saved": num_folded_saved,
            "dedup_saved_pct": dedup_pct,
        }

    def get_pending_strings(self, source_lang: str, target_lang: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT u.*, MIN(l.entry_key) AS sample_key
                FROM unique_strings u
                LEFT JOIN string_locations l ON u.str_hash = l.str_hash
                WHERE u.source_lang = ? AND u.target_lang = ? AND u.status != 'DONE'
                GROUP BY u.str_hash
                ORDER BY CASE u.category WHEN 'UI' THEN 0 WHEN 'CHOICE' THEN 1 ELSE 2 END, u.rowid
                """,
                (source_lang, target_lang),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["tag_map"] = json.loads(d.get("tag_map_json") or "{}")
                d["num_map"] = json.loads(d.get("num_map_json") or "{}")
                out.append(d)
            return out

    def get_all_translations(self, source_lang: str, target_lang: str) -> Dict[str, str]:
        """Return merged lookup mapping raw_source_text / loc_id / source_text -> unfolded translated text."""
        with self._connect() as conn:
            u_rows = conn.execute(
                "SELECT source_text, translated_text, num_map_json FROM unique_strings WHERE source_lang=? AND target_lang=? AND status='DONE' AND translated_text != ''",
                (source_lang, target_lang),
            ).fetchall()
            l_rows = conn.execute(
                """
                SELECT l.loc_id, l.raw_source_text, l.num_map_json AS loc_num_map_json, u.translated_text, u.num_map_json AS u_num_map_json
                FROM string_locations l
                JOIN unique_strings u ON l.str_hash = u.str_hash
                WHERE u.source_lang=? AND u.target_lang=? AND u.status='DONE' AND u.translated_text != ''
                """,
                (source_lang, target_lang),
            ).fetchall()

        out: Dict[str, str] = {}
        for r in u_rows:
            tr = r["translated_text"]
            nmap = json.loads(r["num_map_json"] or "{}")
            out[r["source_text"]] = unfold_numbers(tr, nmap) if nmap else tr
        for lr in l_rows:
            tr = lr["translated_text"]
            nmap = json.loads(lr["loc_num_map_json"] or lr["u_num_map_json"] or "{}")
            resolved = unfold_numbers(tr, nmap) if nmap else tr
            out[lr["loc_id"]] = resolved
            if lr["raw_source_text"]:
                out[lr["raw_source_text"]] = resolved
        return out

    def get_done_rows_for_qa(self, source_lang: str, target_lang: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM unique_strings WHERE source_lang=? AND target_lang=? AND status='DONE' AND COALESCE(manual_locked, 0)=0 AND translated_text != '' ORDER BY CASE category WHEN 'UI' THEN 0 WHEN 'CHOICE' THEN 1 ELSE 2 END, rowid",
                (source_lang, target_lang),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["tag_map"] = json.loads(d.get("tag_map_json") or "{}")
                d["num_map"] = json.loads(d.get("num_map_json") or "{}")
                out.append(d)
            return out

    def get_progress(self, source_lang: str, target_lang: str) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'DONE' THEN 1 ELSE 0 END) AS done
                FROM unique_strings
                WHERE source_lang = ? AND target_lang = ?
                """,
                (source_lang, target_lang),
            ).fetchone()
            total = int(row["total"] or 0) if row else 0
            done = int(row["done"] or 0) if row else 0
            pct = round((done / total) * 100.0, 1) if total > 0 else 0.0
            return {"total": total, "done": done, "pct": pct}

    def mark_translated_batch(self, items: List[Tuple[str, str, str]]) -> None:
        """items: [(str_hash, translated_text, tag_status)]"""
        if not items:
            return
        now = int(time.time())
        with self._connect() as conn:
            conn.executemany(
                "UPDATE unique_strings SET translated_text=?, status='DONE', tag_status=?, updated_at=? WHERE str_hash=?",
                [(tr, tag_st, now, sh) for (sh, tr, tag_st) in items],
            )
            conn.commit()

    def get_recent_translations(self, source_lang: str, target_lang: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT u.str_hash, u.source_text, u.translated_text, u.tag_status, u.category, u.updated_at, MIN(l.entry_key) AS entry_key
                FROM unique_strings u
                LEFT JOIN string_locations l ON u.str_hash = l.str_hash
                WHERE u.source_lang=? AND u.target_lang=? AND u.status='DONE'
                GROUP BY u.str_hash
                ORDER BY u.updated_at DESC
                LIMIT ?
                """,
                (source_lang, target_lang, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_single_translation(self, str_hash: str, new_text: str) -> Optional[Dict[str, Any]]:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE unique_strings SET translated_text=?, status='DONE', tag_status='EDITED', manual_locked=1, updated_at=? WHERE str_hash=?",
                (new_text, now, str_hash),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM unique_strings WHERE str_hash=?", (str_hash,)).fetchone()
            return dict(row) if row else None

    def reset_for_reapply_skill(
        self,
        source_lang: str,
        target_lang: str,
        scope: str = "sub_only",
        keep_manual_locked: bool = True,
    ) -> int:
        """Reset selected cached rows back to PENDING for re-translation under a new Skill."""
        now = int(time.time())
        lock_clause = " AND COALESCE(manual_locked, 0) = 0" if keep_manual_locked else ""
        with self._connect() as conn:
            if scope == "flagged_only":
                cur = conn.execute(
                    f"UPDATE unique_strings SET status='PENDING', tag_status='PENDING', translated_text='', updated_at=? WHERE source_lang=? AND target_lang=? AND (tag_status='FALLBACK' OR status!='DONE'){lock_clause}",
                    (now, source_lang, target_lang),
                )
            elif scope == "sub_only":
                cur = conn.execute(
                    f"UPDATE unique_strings SET status='PENDING', tag_status='PENDING', translated_text='', updated_at=? WHERE source_lang=? AND target_lang=? AND category IN ('SUBTITLE', 'CHOICE'){lock_clause}",
                    (now, source_lang, target_lang),
                )
            else:
                # all_unlocked
                cur = conn.execute(
                    f"UPDATE unique_strings SET status='PENDING', tag_status='PENDING', translated_text='', updated_at=? WHERE source_lang=? AND target_lang=?{lock_clause}",
                    (now, source_lang, target_lang),
                )
            conn.commit()
            return int(cur.rowcount or 0)

    def get_dedup_summary(self, source_lang: str, target_lang: str) -> Dict[str, Any]:
        with self._connect() as conn:
            loc_cnt_row = conn.execute("SELECT COUNT(*) AS cnt FROM string_locations").fetchone()
            exact_cnt_row = conn.execute(
                "SELECT COUNT(DISTINCT raw_source_text) AS cnt FROM string_locations WHERE raw_source_text != ''"
            ).fetchone()
            uniq_cnt_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM unique_strings WHERE source_lang=? AND target_lang=?",
                (source_lang, target_lang),
            ).fetchone()
            raw_cnt = int(loc_cnt_row["cnt"] or 0) if loc_cnt_row else 0
            exact_cnt = int(exact_cnt_row["cnt"] or 0) if exact_cnt_row else 0
            uniq_cnt = int(uniq_cnt_row["cnt"] or 0) if uniq_cnt_row else 0
            num_folded_saved = max(0, exact_cnt - uniq_cnt) if exact_cnt > 0 else 0
            dedup_pct = round(((raw_cnt - uniq_cnt) / raw_cnt) * 100.0, 1) if raw_cnt > 0 else 0.0
            return {
                "raw_count": raw_cnt,
                "exact_unique_count": exact_cnt or uniq_cnt,
                "unique_count": uniq_cnt,
                "num_folded_saved": num_folded_saved,
                "dedup_saved_pct": dedup_pct,
            }

    def audit_and_repair_cache(
        self,
        source_lang: str,
        target_lang: str,
        strip_think_fn,
        unmask_validate_fn,
        anomaly_check_fn=None,
        mojibake_fix_fn=None,
    ) -> Dict[str, int]:
        """Inspect cached translations for leaked <think>, raw __TAG_x__, mojibake, gibberish/CJK leaks, or FALLBACK rows."""
        cleaned_think = 0
        unmasked_repaired = 0
        mojibake_repaired = 0
        gibberish_reset = 0
        reset_pending = 0
        total_checked = 0
        reset_sources: List[str] = []
        now = int(time.time())

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM unique_strings WHERE source_lang=? AND target_lang=?",
                (source_lang, target_lang),
            ).fetchall()

            for r in rows:
                total_checked += 1
                sh = r["str_hash"]
                tr = r["translated_text"] or ""
                src = r["source_text"] or ""
                masked = r["masked_text"] or ""
                tmap = json.loads(r["tag_map_json"] or "{}")
                nmap = json.loads(r["num_map_json"] or "{}") if "num_map_json" in r.keys() else {}
                tag_st = r["tag_status"] or ""
                status = r["status"] or "PENDING"

                # Mark FALLBACK or blank DONE rows back to PENDING for clean AI retry
                if tag_st == "FALLBACK" or (status == "DONE" and not tr.strip()):
                    conn.execute(
                        "UPDATE unique_strings SET status='PENDING', tag_status='PENDING', translated_text='', updated_at=? WHERE str_hash=?",
                        (now, sh),
                    )
                    reset_pending += 1
                    continue

                if status != "DONE":
                    continue

                new_tr = tr
                changed = False
                meta_changed = False

                # Re-normalize stale "[g:...]" or false "% p"/"% d" tag_maps using latest tag_firewall
                old_tmap_raw = r["tag_map_json"] or "{}"
                if "[g:" in src or "% " in old_tmap_raw:
                    from safety_shield.tag_firewall import mask_tags as _remask_tags
                    new_masked, new_tmap = _remask_tags(src)
                    new_tmap_json = json.dumps(new_tmap, ensure_ascii=False)
                    if new_masked != masked or new_tmap_json != old_tmap_raw:
                        masked = new_masked
                        tmap = new_tmap
                        conn.execute(
                            "UPDATE unique_strings SET masked_text=?, tag_map_json=?, updated_at=? WHERE str_hash=?",
                            (masked, new_tmap_json, now, sh),
                        )
                        meta_changed = True
                if meta_changed and (("[g:" in new_tr) or ("% " in old_tmap_raw and status == "DONE")):
                    conn.execute(
                        "UPDATE unique_strings SET status='PENDING', tag_status='PENDING', translated_text='', updated_at=? WHERE str_hash=?",
                        (now, sh),
                    )
                    reset_pending += 1
                    continue

                # Auto-strip QA pipe-prefix corruption like "{} crédits}. |Tớ có thể..."
                if re.match(r"^[^|{}]*\}\s*[^|]*\|", new_tr) or (new_tr.count("|") > src.count("|") and "|" in new_tr):
                    candidate_tail = new_tr.split("|", 1)[1].strip()
                    if candidate_tail and candidate_tail.count("{") == src.count("{") and candidate_tail.count("|") == src.count("|"):
                        new_tr = candidate_tail
                        unmasked_repaired += 1
                        changed = True
                    else:
                        conn.execute(
                            "UPDATE unique_strings SET status='PENDING', tag_status='PENDING', translated_text='', updated_at=? WHERE str_hash=?",
                            (now, sh),
                        )
                        reset_pending += 1
                        continue

                # Auto-repair localized SmartString plural headers (:số lượng: -> :plural:) & French units inside {N:plural:...}
                if ":plural:" in src:
                    repaired_plural = re.sub(
                        r"(\{\d+:)(?:số lượng|số nhiều|số ít)(:)",
                        r"\1plural\2",
                        new_tr,
                        flags=re.IGNORECASE,
                    )

                    def _fix_plural_units(m_p: re.Match) -> str:
                        inner = m_p.group(2)
                        for pat_fr, rep_vi in (
                            (r"\bcrédits?\b", "tín dụng"),
                            (r"\bjours?\b", "ngày"),
                            (r"\bheures?\b", "giờ"),
                            (r"\bflacons?\b", "lọ"),
                            (r"\bobjets?\b", "vật phẩm"),
                            (r"\barticles?\b", "món hàng"),
                            (r"\bœufs?\b", "trứng"),
                            (r"\balevins?\b", "cá con"),
                        ):
                            inner = re.sub(pat_fr, rep_vi, inner, flags=re.IGNORECASE)
                        return f"{m_p.group(1)}{inner}{m_p.group(3)}"

                    repaired_plural = re.sub(
                        r"(\{\d+:plural:)([^}]+)(\})",
                        _fix_plural_units,
                        repaired_plural,
                        flags=re.IGNORECASE,
                    )
                    if repaired_plural != new_tr:
                        new_tr = repaired_plural
                        unmasked_repaired += 1
                        changed = True

                # Strip leaked <think> blocks
                if "<think>" in new_tr.lower() or "</think>" in new_tr.lower():
                    new_tr = strip_think_fn(new_tr)
                    cleaned_think += 1
                    changed = True

                # Auto-repair UTF-8 decoded as Latin-1/cp1252 (mojibake) if cleanly recoverable
                if mojibake_fix_fn:
                    fixed_moji = mojibake_fix_fn(new_tr)
                    if fixed_moji and fixed_moji != new_tr:
                        new_tr = fixed_moji
                        mojibake_repaired += 1
                        changed = True

                # Recover leaked __TAG_x__, stray __NL__, or unmerged __NUM_x__ when no num_map expected
                has_bad_num = ("__NUM_" in new_tr and not nmap) or any(nk not in new_tr for nk in nmap.keys())
                if "__TAG_" in new_tr or "__NL__" in new_tr or has_bad_num:
                    try:
                        ok, fixed, _ = unmask_validate_fn(masked, new_tr, tmap, src, num_map=nmap, keep_num_tokens=True)
                    except TypeError:
                        ok, fixed, _ = unmask_validate_fn(masked, new_tr, tmap, src)
                    if ok:
                        new_tr = fixed
                        unmasked_repaired += 1
                        changed = True
                    else:
                        conn.execute(
                            "UPDATE unique_strings SET status='PENDING', tag_status='PENDING', translated_text='', updated_at=? WHERE str_hash=?",
                            (now, sh),
                        )
                        reset_pending += 1
                        continue

                # Check every literal tag token from tag_map exists inside final translated text
                missing_tag = any(raw_tok not in new_tr for raw_tok in tmap.values())
                if missing_tag:
                    conn.execute(
                        "UPDATE unique_strings SET status='PENDING', tag_status='PENDING', translated_text='', updated_at=? WHERE str_hash=?",
                        (now, sh),
                    )
                    reset_pending += 1
                    continue

                # Check semantic/encoding/language anomalies (unfold sample numbers before judging)
                if anomaly_check_fn:
                    src_eval = unfold_numbers(src, nmap) if nmap else src
                    tr_eval = unfold_numbers(new_tr, nmap) if nmap else new_tr
                    try:
                        bad_q, _reason = anomaly_check_fn(src_eval, tr_eval, target_lang, source_lang)
                    except TypeError:
                        bad_q, _reason = anomaly_check_fn(src_eval, tr_eval, target_lang)
                    if bad_q:
                        conn.execute(
                            "UPDATE unique_strings SET status='PENDING', tag_status='PENDING', translated_text='', updated_at=? WHERE str_hash=?",
                            (now, sh),
                        )
                        gibberish_reset += 1
                        reset_pending += 1
                        if src:
                            reset_sources.append(src)
                        continue

                if changed:
                    conn.execute(
                        "UPDATE unique_strings SET translated_text=?, tag_status='TAG FIXED', updated_at=? WHERE str_hash=?",
                        (new_tr, now, sh),
                    )

            conn.commit()

        return {
            "total_checked": total_checked,
            "cleaned_think": cleaned_think,
            "unmasked_repaired": unmasked_repaired,
            "mojibake_repaired": mojibake_repaired,
            "gibberish_reset": gibberish_reset,
            "reset_pending": reset_pending,
            "reset_sources": reset_sources,
        }

    def upsert_glossary_terms(self, terms: List[Dict[str, str]], source_lang: str, target_lang: str) -> None:
        rows = [
            (source_lang, target_lang, t["source_term"].strip(), t["target_term"].strip(), t.get("note", ""))
            for t in terms
            if t.get("source_term") and t.get("target_term")
        ]
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO glossary_terms(source_lang, target_lang, source_term, target_term, note)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_lang, target_lang, source_term) DO UPDATE SET
                    target_term=excluded.target_term,
                    note=excluded.note
                """,
                rows,
            )
            conn.commit()

    def list_glossary_terms(self, source_lang: Optional[str] = None, target_lang: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if source_lang and target_lang:
                rows = conn.execute(
                    "SELECT * FROM glossary_terms WHERE source_lang=? AND target_lang=? ORDER BY term_id DESC",
                    (source_lang, target_lang),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM glossary_terms ORDER BY term_id DESC").fetchall()
            return [dict(r) for r in rows]

    def delete_glossary_term(self, term_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM glossary_terms WHERE term_id=?", (term_id,))
            conn.commit()

    def save_prethink(self, thinking_text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO prethink_logs(thinking_text, created_at) VALUES (?, ?)",
                (thinking_text, int(time.time())),
            )
            conn.commit()

    def get_latest_prethink(self) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT thinking_text FROM prethink_logs ORDER BY id DESC LIMIT 1").fetchone()
            return row["thinking_text"] if row else ""

    def match_dynamic_glossary(
        self,
        batch_texts: List[str],
        source_lang: str,
        target_lang: str,
        limit: int = 16,
    ) -> List[Dict[str, Any]]:
        """RapidFuzz C++ Dynamic RAG: retrieve ONLY terms relevant to the current batch (<500 tokens)."""
        terms = self.list_glossary_terms(source_lang, target_lang)
        if not terms or not batch_texts:
            return []
        joined_low = "\n".join(batch_texts).lower()
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for t in terms:
            src_t = t["source_term"].strip()
            src_low = src_t.lower()
            if src_low in joined_low:
                scored.append((100.0, t))
                continue
            # Check rapidfuzz partial match against individual sentences in batch
            best = max((fuzz.partial_ratio(src_low, s.lower()) for s in batch_texts), default=0.0)
            if best >= 82.0:
                scored.append((best, t))
        scored.sort(key=lambda pair: -pair[0])
        return [item[1] for item in scored[:limit]]

    def list_saved_translations(self) -> List[Dict[str, Any]]:
        """List all cached (source_lang, target_lang) translation packs with >= 1 translated string."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    source_lang,
                    target_lang,
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'DONE' AND COALESCE(translated_text, '') != '' THEN 1 ELSE 0 END) AS done,
                    MAX(COALESCE(updated_at, 0)) AS updated_at
                FROM unique_strings
                GROUP BY source_lang, target_lang
                HAVING done > 0
                ORDER BY updated_at DESC, done DESC
                """
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            total = int(r["total"] or 0)
            done = int(r["done"] or 0)
            out.append(
                {
                    "source_lang": r["source_lang"],
                    "target_lang": r["target_lang"],
                    "total": total,
                    "done": done,
                    "pct": round((done / total * 100.0), 1) if total > 0 else 0.0,
                    "updated_at": int(r["updated_at"] or 0),
                    "db_path": str(self.db_path),
                }
            )
        return out

    def delete_saved_translations(self, source_lang: str, target_lang: str) -> int:
        """Delete a cached (source_lang, target_lang) translation pack from this game's cache DB."""
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM string_locations
                WHERE str_hash IN (
                    SELECT str_hash FROM unique_strings WHERE source_lang=? AND target_lang=?
                )
                """,
                (source_lang, target_lang),
            )
            cur = conn.execute(
                "DELETE FROM unique_strings WHERE source_lang=? AND target_lang=?",
                (source_lang, target_lang),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def search_translations(
        self,
        source_lang: str,
        target_lang: str,
        query: str = "",
        category_filter: str = "ALL",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Full-text search & category filtering across all cached translations."""
        q_clean = (query or "").strip().lower()
        cat = (category_filter or "ALL").upper()
        where_parts = ["u.source_lang=?", "u.target_lang=?"]
        params: List[Any] = [source_lang, target_lang]

        if cat == "EDITED":
            where_parts.append("(COALESCE(u.manual_locked, 0)=1 OR u.tag_status='EDITED')")
        elif cat == "SUBTITLE":
            where_parts.append("u.category IN ('SUBTITLE', 'CHOICE') AND u.status='DONE'")
        elif cat == "UI":
            where_parts.append("u.category='UI' AND u.status='DONE'")
        elif cat == "PENDING":
            where_parts.append("(u.status!='DONE' OR u.tag_status='FALLBACK')")
        elif not q_clean:
            where_parts.append("u.status='DONE'")

        if q_clean:
            like = f"%{q_clean}%"
            where_parts.append(
                "(LOWER(u.source_text) LIKE ? OR LOWER(COALESCE(u.translated_text, '')) LIKE ? OR LOWER(COALESCE(l.entry_key, '')) LIKE ?)"
            )
            params.extend([like, like, like])

        params.append(max(1, min(500, int(limit or 200))))
        sql = f"""
            SELECT u.str_hash, u.source_text, u.translated_text, u.tag_status, u.category,
                   COALESCE(u.manual_locked, 0) AS manual_locked, u.updated_at, MIN(l.entry_key) AS entry_key
            FROM unique_strings u
            LEFT JOIN string_locations l ON u.str_hash = l.str_hash
            WHERE {' AND '.join(where_parts)}
            GROUP BY u.str_hash
            ORDER BY u.updated_at DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def bulk_find_replace(
        self,
        source_lang: str,
        target_lang: str,
        find_text: str,
        replace_text: str,
        case_sensitive: bool = False,
        category_filter: str = "ALL",
    ) -> Dict[str, Any]:
        """Replace matching substrings across DONE translations while protecting __TAG_x__ / __NUM_x__ tokens."""
        if not find_text:
            return {"replaced_count": 0, "updated_pairs": []}

        cat = (category_filter or "ALL").upper()
        where_parts = ["source_lang=?", "target_lang=?", "status='DONE'"]
        if cat == "EDITED":
            where_parts.append("(COALESCE(manual_locked, 0)=1 OR tag_status='EDITED')")
        elif cat == "SUBTITLE":
            where_parts.append("category IN ('SUBTITLE', 'CHOICE')")
        elif cat == "UI":
            where_parts.append("category='UI'")

        pat = re.compile(re.escape(find_text), 0 if case_sensitive else re.IGNORECASE)
        tok_split_re = re.compile(r"(__TAG_\d+__|__NUM_\d+__|__NL__|<[^>]+>|\{[^{}\r\n]{1,64}\})")
        now = int(time.time())
        updates: List[Tuple[str, int, str]] = []
        updated_pairs: List[Tuple[str, str, str]] = []

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT str_hash, source_text, translated_text, category FROM unique_strings WHERE {' AND '.join(where_parts)}",
                (source_lang, target_lang),
            ).fetchall()
            for r in rows:
                old_tr = r["translated_text"] or ""
                if not old_tr:
                    continue
                parts = tok_split_re.split(old_tr)
                new_parts = [
                    part if tok_split_re.fullmatch(part) else pat.sub(lambda _: replace_text, part)
                    for part in parts
                ]
                new_tr = "".join(new_parts)
                if new_tr != old_tr:
                    updates.append((new_tr, now, r["str_hash"]))
                    updated_pairs.append((r["source_text"], new_tr, r["category"] or "UI"))
            if updates:
                conn.executemany(
                    "UPDATE unique_strings SET translated_text=?, tag_status='EDITED', manual_locked=1, updated_at=? WHERE str_hash=?",
                    updates,
                )
                conn.commit()
        return {"replaced_count": len(updates), "updated_pairs": updated_pairs}

    def export_sakugapack_data(
        self,
        source_lang: str,
        target_lang: str,
        game_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Export DONE translations + glossary for (source_lang, target_lang) as sakugapack_v1 dict."""
        with self._connect() as conn:
            str_rows = conn.execute(
                """
                SELECT str_hash, source_lang, target_lang, source_text, masked_text,
                       tag_map_json, num_map_json, category, translated_text, status,
                       tag_status, COALESCE(manual_locked, 0) AS manual_locked, updated_at
                FROM unique_strings
                WHERE source_lang=? AND target_lang=? AND status='DONE' AND COALESCE(translated_text, '') != ''
                """,
                (source_lang, target_lang),
            ).fetchall()
            gl_rows = conn.execute(
                "SELECT source_term, target_term, note FROM glossary_terms WHERE source_lang=? AND target_lang=?",
                (source_lang, target_lang),
            ).fetchall()
        return {
            "format": "sakugapack_v1",
            "exported_at": int(time.time()),
            "source_lang": source_lang,
            "target_lang": target_lang,
            "game_meta": game_meta or {},
            "strings": [dict(r) for r in str_rows],
            "glossary": [dict(g) for g in gl_rows],
        }

    def import_sakugapack_data(
        self,
        pack: Dict[str, Any],
        overwrite_locked: bool = False,
    ) -> Dict[str, Any]:
        """Import a sakugapack_v1 payload into this game's cache DB + glossary."""
        if not isinstance(pack, dict) or pack.get("format") != "sakugapack_v1" or not isinstance(pack.get("strings"), list):
            raise ValueError("Gói .sakugapack không hợp lệ hoặc sai định dạng sakugapack_v1.")
        src = str(pack.get("source_lang") or "en")
        tgt = str(pack.get("target_lang") or "vi")
        now = int(time.time())
        rows_to_upsert: List[Tuple[Any, ...]] = []
        for item in pack["strings"]:
            if not isinstance(item, dict):
                continue
            tr_txt = str(item.get("translated_text") or "")
            src_txt = str(item.get("source_text") or "")
            sh = str(item.get("str_hash") or "")
            if not sh or not src_txt or not tr_txt:
                continue
            rows_to_upsert.append(
                (
                    sh,
                    str(item.get("source_lang") or src),
                    str(item.get("target_lang") or tgt),
                    src_txt,
                    str(item.get("masked_text") or src_txt),
                    str(item.get("tag_map_json") or "{}"),
                    str(item.get("num_map_json") or "{}"),
                    str(item.get("category") or "UI"),
                    tr_txt,
                    "DONE",
                    str(item.get("tag_status") or "OK"),
                    int(item.get("manual_locked") or 0),
                    int(item.get("updated_at") or now),
                )
            )

        conflict_where = "" if overwrite_locked else "WHERE COALESCE(unique_strings.manual_locked, 0) = 0"
        with self._connect() as conn:
            conn.executemany(
                f"""
                INSERT INTO unique_strings(
                    str_hash, source_lang, target_lang, source_text, masked_text,
                    tag_map_json, num_map_json, category, translated_text, status,
                    tag_status, manual_locked, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(str_hash) DO UPDATE SET
                    translated_text=excluded.translated_text,
                    status='DONE',
                    tag_status=excluded.tag_status,
                    manual_locked=excluded.manual_locked,
                    updated_at=excluded.updated_at
                {conflict_where}
                """,
                rows_to_upsert,
            )
            conn.commit()

        gl_list = [g for g in (pack.get("glossary") or []) if isinstance(g, dict)]
        if gl_list:
            self.upsert_glossary_terms(gl_list, src, tgt)

        return {
            "imported_strings": len(rows_to_upsert),
            "imported_glossary": len(gl_list),
            "source_lang": src,
            "target_lang": tgt,
        }

    def checkpoint(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
