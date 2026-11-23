"""Universal Local Ollama Engine: Auto Hardware Profiler, Queue Worker Pool, Length Bucketing & Dual-Model."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import psutil

from core.database import GameCacheDB, GlobalTMDB
from core.hardware_profiler import HardwareProfiler, normalize_ollama_url
from core.prompts import (
    build_fallback_style_card,
    build_glossary_system_prompt,
    build_translation_system_prompt,
)
from safety_shield.encoding_guard import (
    detect_translation_anomaly,
    smart_word_wrap,
    strip_thinking,
    try_fix_mojibake,
)
from safety_shield.tag_firewall import mask_tags, unfold_numbers, validate_and_unmask

import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    try:
        import pynvml
        _HAS_NVML = True
    except Exception:
        _HAS_NVML = False

logger = logging.getLogger("sakuga.qwen")

MODEL_NAME = "qwen3.5:4b"
OLLAMA_URL = normalize_ollama_url(os.environ.get("OLLAMA_HOST"))
BATCH_SIZE = 25
MAX_CONCURRENT_STREAMS = 5

THINKING_MODEL_PREFIXES = ("qwen3", "qwen3.5", "deepseek-r1", "qwq", "gpt-oss", "nemotron", "cogito")


def supports_thinking(model_name: str) -> bool:
    """Return True if local Ollama model family supports native `think: True/False` reasoning parameter."""
    low = (model_name or "").strip().lower()
    if not low:
        return False
    base = low.split(":")[0].split("/")[-1]
    return any(base.startswith(p) or p in low for p in THINKING_MODEL_PREFIXES)


def _batch_needs_deep_think(
    batch_rows: List[Dict[str, Any]],
    mode: str,
    is_short_ui: bool,
    model_name: str,
) -> bool:
    """Selectively activate model thinking ONLY on narrative/dialogue/choice batches where reasoning improves quality."""
    if is_short_ui or mode == "UI_HUD":
        return False
    if not supports_thinking(model_name):
        return False
    for r in batch_rows:
        cat = str(r.get("category") or "").upper()
        txt = str(r.get("masked_text") or "").strip()
        if cat in ("SUBTITLE", "CHOICE") and len(txt) >= 18:
            return True
        if len(txt) >= 32 or any(ch in txt for ch in (".", "?", "!", "…", "\"", "—", ",")):
            return True
    return False

# 3 User-Facing Preset Levels -> automatically configure scope + AI engine settings
PRESET_PROFILES: Dict[str, Dict[str, Any]] = {
    "FAST": {
        "level": 1,
        "title": "DỊCH SIÊU TỐC",
        "subtitle": "Chơi ngay • Bỏ qua Pha 0 Pre-Think",
        "description": "Chạy đa luồng Flash No-Think tối đa tốc độ cho UI + Thoại theo cấu hình phần cứng máy. Phù hợp test nhanh hoặc game ít chữ.",
        "ai_mode": "FLASH",
        "scope": "FULL_PATCH",
        "translate_choices": True,
        "keep_proper_nouns": True,
        "patch_fonts": True,
        "chars_per_sec": 340.0,
        "prethink_sec": 0.0,
        "warmup_sec": 2.0,
    },
    "BALANCED": {
        "level": 2,
        "title": "CÂN BẰNG CHUẨN GAME",
        "subtitle": "Khuyên dùng • Pre-Think Glossary + Queue Pool",
        "description": "Lập từ điển tên riêng & ngôi xưng 1 lần (Pha 0), sau đó dịch song song bằng Worker Pool tối ưu KV-Cache theo VRAM máy.",
        "ai_mode": "AUTO_HYBRID",
        "scope": "FULL_PATCH",
        "translate_choices": True,
        "keep_proper_nouns": True,
        "patch_fonts": True,
        "chars_per_sec": 300.0,
        "prethink_sec": 9.0,
        "warmup_sec": 2.0,
    },
    "DEEP_STORY": {
        "level": 3,
        "title": "CỐT TRUYỆN SÂU SẮC",
        "subtitle": "Deep-Think thông minh • Giữ mạch hội thoại & stream suy luận",
        "description": "Tận dụng Thinking Mode cho các cụm thoại & lựa chọn theo đúng mạch cảnh (6–7 câu/gói), giữ nguyên tốc độ nhanh cho nhãn UI ngắn.",
        "ai_mode": "DEEP_THINK",
        "scope": "SUB_ONLY",
        "translate_choices": True,
        "keep_proper_nouns": True,
        "patch_fonts": True,
        "chars_per_sec": 125.0,
        "prethink_sec": 9.0,
        "warmup_sec": 2.5,
    },
}


def format_duration_vi(seconds: float) -> str:
    """Human-readable Vietnamese duration range without fake second-level precision before running."""
    sec = max(1, int(round(seconds)))
    if sec < 90:
        lo = max(3, int(sec * 0.85))
        hi = max(lo + 4, int(sec * 1.2))
        return f"~{lo}–{hi} giây"
    lo_m = max(1, int((sec * 0.85) // 60))
    hi_m = max(lo_m, int((sec * 1.25) // 60) + (0 if sec < 300 else 1))
    if lo_m == hi_m:
        return f"~{lo_m} phút"
    return f"~{lo_m}–{hi_m} phút"


def estimate_preset_durations(
    entries_or_rows: List[Any],
    tm_hit_texts: Optional[set[str]] = None,
    has_glossary: bool = False,
    profiler: Optional[HardwareProfiler] = None,
) -> Dict[str, Dict[str, Any]]:
    """Compute calibrated pre-run duration estimates using active hardware profiler workers."""
    tm_hits = tm_hit_texts or set()
    all_chars = 0
    sub_chars = 0
    all_pending = 0
    sub_pending = 0

    for item in entries_or_rows:
        if isinstance(item, dict):
            txt = item.get("masked_text") or item.get("source_text") or ""
            raw_txt = item.get("source_text") or txt
            cat = item.get("category") or "UI"
        else:
            txt = getattr(item, "text", "") or ""
            raw_txt = txt
            cat = getattr(item, "category", "UI")
        if raw_txt in tm_hits:
            continue
        clen = max(8, len(txt))
        all_chars += clen
        all_pending += 1
        if cat in ("SUBTITLE", "CHOICE"):
            sub_chars += clen
            sub_pending += 1

    eff = (profiler or HardwareProfiler()).get_effective_settings()
    workers = max(1, int(eff.get("parallel_workers", MAX_CONCURRENT_STREAMS)))
    sub_batch = max(5, int(eff.get("sub_batch_size", BATCH_SIZE)))
    worker_scale = max(0.35, min(1.8, workers / 4.0))

    out: Dict[str, Dict[str, Any]] = {}
    for key, prof in PRESET_PROFILES.items():
        chars = sub_chars if prof["scope"] == "SUB_ONLY" else all_chars
        count = sub_pending if prof["scope"] == "SUB_ONLY" else all_pending
        if count == 0:
            est = 0.0
            label = "0 giây (Đã cache/TM 100%)"
        else:
            batches = max(1, (count + sub_batch - 1) // sub_batch)
            waves = (batches + workers - 1) // workers
            prethink = 0.0 if has_glossary else prof["prethink_sec"]
            eff_cps = prof["chars_per_sec"] * worker_scale
            est = prof["warmup_sec"] + prethink + (chars / max(15.0, eff_cps)) + (waves * 0.35)
            label = format_duration_vi(est)
        out[key] = {
            "key": key,
            "level": prof["level"],
            "title": prof["title"],
            "subtitle": prof["subtitle"],
            "description": prof["description"],
            "ai_mode": prof["ai_mode"],
            "scope": prof["scope"],
            "translate_choices": prof["translate_choices"],
            "keep_proper_nouns": prof["keep_proper_nouns"],
            "patch_fonts": prof["patch_fonts"],
            "pending_count": count,
            "pending_chars": chars,
            "est_seconds": int(round(est)),
            "est_label": label,
        }
    return out


def _is_batch_aligned(raw_text: str, expected_ids: set[str]) -> bool:
    """Detect mid-batch ID shifting, duplicate IDs, or mass missing IDs before trusting pipe output."""
    cleaned = strip_thinking(raw_text)
    seen: List[str] = []
    for line in cleaned.splitlines():
        s = line.strip()
        if not s or s.startswith("```"):
            continue
        m = re.match(r"^(?:[-*]\s*)?(\d+)\s*\|", s)
        if m:
            seen.append(m.group(1))
    if not seen:
        return False
    if len(seen) != len(set(seen)):
        return False
    if set(seen) - expected_ids:
        return False
    missing = len(expected_ids) - len(seen)
    return missing <= max(1, len(expected_ids) // 4)


def parse_id_pipe_lines(raw_text: str) -> Dict[str, str]:
    """Parse flat `ID|Text` output lines cleanly while preserving internal pipes inside translated text."""
    cleaned = strip_thinking(raw_text)
    parsed: Dict[str, str] = {}
    for line in cleaned.splitlines():
        s = line.strip()
        if not s or s.startswith("```"):
            continue
        m = re.match(r"^(?:[-*]\s*)?(\d+)\s*\|\s*(.*)$", s)
        if m:
            parsed[m.group(1)] = m.group(2).strip()
    return parsed


def parse_style_card(
    raw_text: str,
    tone_preset: str = "natural",
    game_description: str = "",
) -> str:
    """Extract & clamp the 4-line (~100-120 token) Style Card from Phase 0 output."""
    cleaned = strip_thinking(raw_text)
    block = ""
    m = re.search(r"STYLE_START\s*(.*?)\s*STYLE_END", cleaned, re.DOTALL | re.IGNORECASE)
    if m:
        block = m.group(1)
    else:
        matched_lines = [
            ln for ln in cleaned.splitlines()
            if re.match(r"^(?:[-*•]\s*)?(?:Bối cảnh|Giọng điệu|Xưng hô|Lưu ý)", ln.strip(), re.IGNORECASE)
        ]
        if matched_lines:
            block = "\n".join(matched_lines)

    out_lines: List[str] = []
    for ln in block.splitlines():
        s = re.sub(r"^(?:[-*•]\s*|\d+\.\s*)", "", ln.strip()).replace("**", "").strip()
        if s and "|" not in s:
            out_lines.append(s[:140])
        if len(out_lines) >= 4:
            break

    if out_lines:
        return "\n".join(out_lines)[:420].strip()
    if (game_description or "").strip() or (tone_preset or "natural").lower() != "natural":
        return build_fallback_style_card(tone_preset, game_description)
    return ""


def parse_glossary_lines(raw_text: str) -> List[Dict[str, str]]:
    """Parse `TERM|SOURCE_TEXT|TARGET_TEXT|NOTE` rows returned by Phase 0 Pre-Think."""
    cleaned = re.sub(r"STYLE_START.*?STYLE_END", "", strip_thinking(raw_text), flags=re.DOTALL | re.IGNORECASE)
    terms: List[Dict[str, str]] = []
    seen: set[str] = set()
    for line in cleaned.splitlines():
        s = re.sub(r"^(?:[-*•]\s*|\d+\.\s*)", "", line.strip()).replace("**", "").strip()
        if not s or "|" not in s:
            continue
        parts = [p.strip() for p in s.split("|")]
        if parts[0].upper() == "TERM" and len(parts) >= 3:
            src_t, tgt_t = parts[1], parts[2]
            note = parts[3] if len(parts) >= 4 else ""
        elif len(parts) >= 2 and parts[0].upper() not in ("SOURCE_TEXT", "SOURCE", "ID"):
            src_t, tgt_t = parts[0], parts[1]
            note = parts[2] if len(parts) >= 3 else ""
        else:
            continue
        if src_t and tgt_t and src_t.lower() not in seen:
            seen.add(src_t.lower())
            terms.append({"source_term": src_t, "target_term": tgt_t, "note": note})
    return terms[:30]


def _split_qa_anchor_and_candidate(raw_rest: str, row_hint: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Split `EXACT_SOURCE|Polished_Target` safely even when source contains plural pipes like `{0:plural:a|b}`."""
    if "|" not in raw_rest:
        return raw_rest, ""
    src_pipes = (row_hint.get("masked_text") or "").count("|") if row_hint else 0
    if src_pipes > 0 and raw_rest.count("|") >= (2 * src_pipes) + 1:
        parts = raw_rest.split("|")
        return "|".join(parts[: src_pipes + 1]), "|".join(parts[src_pipes + 1 :])
    return raw_rest.split("|", 1)


class QwenEngine:
    """Async Producer-Consumer Ollama Engine supporting Auto Hardware Tiers & any local model."""

    def __init__(self, profiler: Optional[HardwareProfiler] = None) -> None:
        self.profiler = profiler or HardwareProfiler()
        self.profiler.apply_ollama_env()
        self._nvml_ready = self.profiler._nvml_ready
        self._pause_flags: Dict[str, asyncio.Event] = {}
        self._pause_accum: Dict[str, float] = {}
        self._pause_started_at: Dict[str, float] = {}
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._used_models: set[str] = set()
        self._active_runtime_model: str = ""
        self._active_runtime_role: str = "IDLE"

    def get_effective_config(self) -> Dict[str, Any]:
        return self.profiler.get_effective_settings()

    def get_base_url(self) -> str:
        return self.profiler.load_settings().get("ollama_base_url", OLLAMA_URL)

    def get_telemetry(self) -> Dict[str, Any]:
        eff = self.profiler.get_effective_settings()
        hw = eff.get("hardware", {})
        return {
            "gpu_name": hw.get("gpu_name", "CPU Fallback"),
            "vram_used_gb": hw.get("vram_used_gb", 0.0),
            "vram_total_gb": hw.get("vram_total_gb", 0.0),
            "cpu_pct": hw.get("cpu_pct", 0.0),
            "ram_used_gb": hw.get("ram_used_gb", 0.0),
            "ram_total_gb": hw.get("ram_total_gb", 0.0),
            "model": eff.get("main_model", MODEL_NAME),
            "dual_model_enabled": bool(eff.get("dual_model_enabled")),
            "ui_fast_model": eff.get("ui_fast_model", ""),
            "quiet_mode": bool(eff.get("quiet_mode", False)),
            "batch_cooldown_ms": int(eff.get("batch_cooldown_ms", 0)),
            "active_runtime_model": self._active_runtime_model or eff.get("main_model", MODEL_NAME),
            "active_runtime_role": self._active_runtime_role or "IDLE",
            "parallel_streams": eff.get("parallel_workers", MAX_CONCURRENT_STREAMS),
            "num_ctx": eff.get("num_ctx", 2048),
            "tier_label": hw.get("tier_label", "Auto"),
            "profile_badge": eff.get("profile_badge", ""),
        }

    async def list_local_models(self) -> List[Dict[str, Any]]:
        base_url = self.get_base_url()
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(f"{base_url}/api/tags")
                resp.raise_for_status()
                raw_models = resp.json().get("models", [])
                out: List[Dict[str, Any]] = []
                for m in raw_models:
                    name = m.get("name") or ""
                    if not name:
                        continue
                    sz_b = int(m.get("size") or 0)
                    out.append(
                        {
                            "name": name,
                            "size_bytes": sz_b,
                            "size_gb": round(sz_b / (1024 ** 3), 2),
                            "supports_thinking": supports_thinking(name),
                        }
                    )
                return out
        except Exception:
            return []

    async def check_ollama_status(self) -> Dict[str, Any]:
        eff = self.get_effective_config()
        base_url = eff["ollama_base_url"]
        main_model = eff["main_model"]
        hw = eff.get("hardware", {})
        try:
            models_meta = await self.list_local_models()
            models = [m["name"] for m in models_meta]
            # Validate active reachability if tags list came back empty cleanly
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{base_url}/api/tags")
                resp.raise_for_status()
            has_selected = any(m == main_model or m.startswith(main_model.split(":")[0]) for m in models)
            return {
                "online": True,
                "model_ready": bool(models) and (has_selected or True),
                "selected_installed": has_selected,
                "models": models,
                "models_meta": models_meta,
                "main_model": main_model,
                "dual_model_enabled": eff["dual_model_enabled"],
                "ui_fast_model": eff["ui_fast_model"],
                "parallel_workers": eff["parallel_workers"],
                "tier_label": hw.get("tier_label", "Auto"),
                "profile_badge": eff.get("profile_badge", ""),
                "ollama_env_warning": eff.get("ollama_env_warning", ""),
                "recommended_models": hw.get("recommended_models", []),
                "label": f"{main_model} • {eff.get('profile_badge', '')}",
            }
        except Exception:
            return {
                "online": False,
                "model_ready": False,
                "selected_installed": False,
                "models": [],
                "models_meta": [],
                "main_model": main_model,
                "dual_model_enabled": eff["dual_model_enabled"],
                "ui_fast_model": eff["ui_fast_model"],
                "parallel_workers": eff["parallel_workers"],
                "tier_label": hw.get("tier_label", "Auto"),
                "profile_badge": eff.get("profile_badge", ""),
                "ollama_env_warning": eff.get("ollama_env_warning", ""),
                "recommended_models": hw.get("recommended_models", []),
                "label": f"Ollama Offline • {main_model}",
            }

    async def pull_model_stream(
        self,
        model_name: str,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Pull an Ollama model from localhost registry with live NDJSON progress streaming."""
        base_url = self.get_base_url()
        await self.ensure_ollama_running()
        last_status = "starting"
        last_pct = 0.0
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", f"{base_url}/api/pull", json={"name": model_name, "stream": True}) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    if chunk.get("error"):
                        raise RuntimeError(str(chunk["error"]))
                    last_status = str(chunk.get("status") or last_status)
                    tot = int(chunk.get("total") or 0)
                    comp = int(chunk.get("completed") or 0)
                    if tot > 0:
                        last_pct = round((comp / tot) * 100.0, 1)
                    elif last_status == "success":
                        last_pct = 100.0
                    if progress_cb:
                        progress_cb(
                            {
                                "model": model_name,
                                "status": last_status,
                                "completed": comp,
                                "total": tot,
                                "pct": last_pct,
                            }
                        )
        return {"ok": True, "model": model_name, "status": last_status, "pct": 100.0}

    async def ensure_ollama_running(self) -> bool:
        st = await self.check_ollama_status()
        if st["online"]:
            return True
        ollama_bin = shutil.which("ollama")
        if not ollama_bin and os.name == "nt":
            for cand in (
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama" / "ollama.exe",
            ):
                if cand.exists() and cand.is_file():
                    ollama_bin = str(cand)
                    break
        if not ollama_bin:
            return False
        env = os.environ.copy()
        env.update(self.profiler.apply_ollama_env())
        flags = (
            (subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
            if os.name == "nt"
            else 0
        )
        try:
            subprocess.Popen(
                [ollama_bin, "serve"],
                env=env,
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(14):
                await asyncio.sleep(0.5)
                if (await self.check_ollama_status())["online"]:
                    return True
        except Exception:
            return False
        return False

    async def _unload_model(self, model_name: str, client: Optional[httpx.AsyncClient] = None) -> None:
        if not model_name:
            return
        base_url = self.get_base_url()
        try:
            if client is not None:
                await client.post(
                    f"{base_url}/api/generate",
                    json={"model": model_name, "prompt": "", "keep_alive": 0, "stream": False},
                )
            else:
                async with httpx.AsyncClient(timeout=6.0) as tmp_c:
                    await tmp_c.post(
                        f"{base_url}/api/generate",
                        json={"model": model_name, "prompt": "", "keep_alive": 0, "stream": False},
                    )
        except Exception as exc:
            logger.debug("unload model notice (%s): %s", model_name, exc)

    async def flush_vram(self) -> Dict[str, Any]:
        """Force Ollama to unload all active/cached models immediately (keep_alive=0 -> 0GB VRAM)."""
        eff = self.profiler.load_settings()
        targets = set(self._used_models)
        if eff.get("main_model"):
            targets.add(eff["main_model"])
        if eff.get("dual_model_enabled") and eff.get("ui_fast_model"):
            targets.add(eff["ui_fast_model"])
        for mdl in targets:
            await self._unload_model(mdl)
        return self.get_telemetry()

    async def _call_ollama_chat(
        self,
        client: httpx.AsyncClient,
        system_prompt: str,
        user_prompt: str,
        think: bool = False,
        temperature: float = 0.2,
        num_predict: int = 0,
        model_name: Optional[str] = None,
        num_ctx: Optional[int] = None,
        stream_cb: Optional[Callable[[int, float], None]] = None,
        thinking_cb: Optional[Callable[[str], None]] = None,
    ) -> Tuple[str, str, int, float]:
        """Call Ollama /api/chat with surrogate sanitization, multi-stage HTTP 400 fallback, and optional streaming."""
        eff = self.profiler.get_effective_config() if hasattr(self.profiler, "get_effective_config") else self.profiler.get_effective_settings()
        model = model_name or eff["main_model"]
        ctx = max(1024, int(num_ctx or eff["num_ctx"] or 2048))
        base_url = eff["ollama_base_url"]
        self._used_models.add(model)

        # Strip lone UTF-16 surrogates / NUL bytes from game strings that cause Ollama JSON 400 errors
        def _clean_text(s: str) -> str:
            return (s or "").replace("\x00", "").encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")

        sys_clean = _clean_text(system_prompt)
        usr_clean = _clean_text(user_prompt)

        est_in_tok = max(8, len(usr_clean) // 4)
        if num_predict > 0:
            dyn_predict = int(num_predict)
        elif think:
            dyn_predict = min(2400, int(est_in_tok * 3.0) + 500)
        else:
            dyn_predict = min(1800, int(est_in_tok * 1.9) + 150)

        opts: Dict[str, Any] = {
            "temperature": temperature,
            "num_ctx": ctx,
            "num_predict": dyn_predict,
            "top_p": 0.9,
        }

        can_think = supports_thinking(model)
        use_stream = (stream_cb is not None) or (thinking_cb is not None)
        payload: Dict[str, Any] = {
            "model": model,
            "stream": use_stream,
            "keep_alive": "10m",
            "options": opts,
            "messages": [
                {"role": "system", "content": sys_clean},
                {"role": "user", "content": usr_clean if (think or not can_think) else f"/no_think\n{usr_clean}"},
            ],
        }
        if can_think:
            payload["think"] = bool(think)

        def _fallback_no_think(src: Dict[str, Any]) -> Dict[str, Any]:
            fb = dict(src)
            fb.pop("think", None)
            fb["messages"] = [
                {"role": "system", "content": sys_clean},
                {"role": "user", "content": usr_clean},
            ]
            return fb

        def _fallback_minimal(src: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "model": model,
                "stream": bool(src.get("stream", False)),
                "options": {
                    "temperature": temperature,
                    "num_ctx": min(ctx, 2048),
                },
                "messages": [
                    {"role": "system", "content": sys_clean},
                    {"role": "user", "content": usr_clean},
                ],
            }

        def _extract_http_error(resp_obj: httpx.Response, raw_body: str = "") -> str:
            detail = raw_body.strip()
            if not detail:
                try:
                    detail = resp_obj.text.strip()
                except Exception:
                    detail = ""
            try:
                j = json.loads(detail) if detail else {}
                if isinstance(j, dict) and j.get("error"):
                    detail = str(j["error"])
            except Exception:
                pass
            return f"Ollama HTTP {resp_obj.status_code}: {detail[:260]}" if detail else f"Ollama HTTP {resp_obj.status_code}"

        t0 = time.perf_counter()
        if not use_stream:
            resp = await client.post(f"{base_url}/api/chat", json=payload)
            if resp.status_code == 400 and "think" in payload:
                resp = await client.post(f"{base_url}/api/chat", json=_fallback_no_think(payload))
            if resp.status_code == 400:
                resp = await client.post(f"{base_url}/api/chat", json=_fallback_minimal(payload))
            if resp.status_code >= 400:
                raise RuntimeError(_extract_http_error(resp))
            data = resp.json()
            elapsed = max(0.05, time.perf_counter() - t0)
            msg = data.get("message", {})
            content = msg.get("content", "")
            thinking = msg.get("thinking", "") or ""
            if not thinking and "<think>" in content:
                m = re.search(r"<think>(.*?)</think>", content, re.DOTALL | re.IGNORECASE)
                if m:
                    thinking = m.group(1).strip()
            eval_count = int(data.get("eval_count") or max(1, len(content) // 4))
            return content, thinking, eval_count, elapsed

        async def _consume_stream(req_payload: Dict[str, Any]) -> Tuple[str, str, int, float]:
            parts_c: List[str] = []
            parts_t: List[str] = []
            chunk_toks = 0
            last_emit = time.perf_counter()
            eval_cnt = 0
            async with client.stream("POST", f"{base_url}/api/chat", json=req_payload) as sresp:
                if sresp.status_code == 400:
                    raw_err = (await sresp.aread()).decode("utf-8", errors="ignore")
                    raise ValueError(f"HTTP_400::{raw_err}")
                if sresp.status_code >= 400:
                    raw_err = (await sresp.aread()).decode("utf-8", errors="ignore")
                    raise RuntimeError(_extract_http_error(sresp, raw_err))
                async for line in sresp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    if chunk.get("error"):
                        raise RuntimeError(str(chunk["error"]))
                    msg_c = chunk.get("message") or {}
                    dc = msg_c.get("content") or ""
                    dt = msg_c.get("thinking") or ""
                    if dc:
                        parts_c.append(dc)
                        chunk_toks += 1
                    if dt:
                        parts_t.append(dt)
                        chunk_toks += 1
                    if chunk.get("eval_count"):
                        eval_cnt = int(chunk["eval_count"])
                    now_t = time.perf_counter()
                    if (stream_cb or thinking_cb) and (now_t - last_emit >= 0.35 or chunk.get("done")):
                        last_emit = now_t
                        el = max(0.1, now_t - t0)
                        cur_toks = max(eval_cnt, chunk_toks)
                        if stream_cb:
                            try:
                                stream_cb(cur_toks, round(cur_toks / el, 1))
                            except Exception:
                                pass
                        if thinking_cb and parts_t:
                            try:
                                thinking_cb("".join(parts_t)[-1800:])
                            except Exception:
                                pass
            elapsed_s = max(0.05, time.perf_counter() - t0)
            full_c = "".join(parts_c)
            full_t = "".join(parts_t)
            if not full_t and "<think>" in full_c:
                m = re.search(r"<think>(.*?)</think>", full_c, re.DOTALL | re.IGNORECASE)
                if m:
                    full_t = m.group(1).strip()
            final_toks = eval_cnt or max(chunk_toks, len(full_c) // 4, 1)
            return full_c, full_t, final_toks, elapsed_s

        candidates = [payload]
        if "think" in payload:
            candidates.append(_fallback_no_think(payload))
        candidates.append(_fallback_minimal(payload))
        last_400 = ""
        for cand in candidates:
            try:
                return await _consume_stream(cand)
            except ValueError as exc:
                if str(exc).startswith("HTTP_400::"):
                    last_400 = str(exc)[10:]
                    continue
                raise
        raise RuntimeError(f"Ollama HTTP 400: {last_400[:260]}" if last_400 else "Ollama HTTP 400 Bad Request")

    async def pre_think_glossary(
        self,
        client: httpx.AsyncClient,
        sample_rows: List[Dict[str, Any]],
        source_lang: str,
        target_lang: str,
        cache_db: GameCacheDB,
        emit_cb: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        game_description: str = "",
        tone_preset: str = "natural",
        style_card_cb: Optional[Callable[[str], None]] = None,
        style_skill_id: Optional[str] = None,
        lang_skill_override: str = "auto",
        model_name: Optional[str] = None,
        use_deep_think: bool = False,
        stream_cb: Optional[Callable[[int, float], None]] = None,
    ) -> List[Dict[str, str]]:
        """Phase 0: Fast pass over <= 18 concise sample lines to lock glossary & compress 4-line Style Card."""
        effective_style = style_skill_id or tone_preset or "natural_standard"
        if not sample_rows and not (game_description or "").strip() and effective_style in ("natural", "natural_standard"):
            return []
        sample_subset = sample_rows[:18]
        user_lines = "\n".join(
            f"{r.get('sample_key') or i}|{(r['masked_text'] or '')[:160].replace(chr(10), ' ')}"
            for i, r in enumerate(sample_subset)
        )
        if not user_lines:
            user_lines = "0|Start Game\n1|Settings\n2|Continue"
        sys_prompt = build_glossary_system_prompt(
            source_lang,
            target_lang,
            game_description=game_description,
            tone_preset=tone_preset,
            style_skill_id=effective_style,
            lang_skill_override=lang_skill_override,
        )

        if emit_cb:
            emit_cb("prethink_start", {"sample_count": len(sample_subset)})

        content, thinking, eval_count, elapsed = await self._call_ollama_chat(
            client,
            sys_prompt,
            user_lines,
            think=bool(use_deep_think),
            temperature=0.25,
            num_predict=850 if use_deep_think else 620,
            model_name=model_name,
            stream_cb=stream_cb,
        )
        terms = parse_glossary_lines(content) or parse_glossary_lines(thinking)
        if terms:
            cache_db.upsert_glossary_terms(terms, source_lang, target_lang)
        style_card = parse_style_card(content, tone_preset, game_description) or parse_style_card(
            thinking, tone_preset, game_description
        )
        if style_card and style_card_cb:
            style_card_cb(style_card)

        think_log = thinking or content
        cache_db.save_prethink(think_log)

        if emit_cb:
            emit_cb(
                "prethink_done",
                {
                    "thinking": think_log,
                    "terms": terms,
                    "style_card": style_card,
                    "tok_per_sec": round(eval_count / elapsed, 1),
                },
            )
        return terms

    async def _translate_one_batch_with_retry(
        self,
        client: httpx.AsyncClient,
        sem: Optional[asyncio.Semaphore],
        batch_rows: List[Dict[str, Any]],
        mode: str,  # "UI_HUD" | "STORY" | "SUB_ONLY"
        source_lang: str,
        target_lang: str,
        ai_mode: str,  # "AUTO_HYBRID" | "DEEP_THINK" | "FLASH"
        keep_proper_nouns: bool,
        cache_db: GameCacheDB,
        global_tm: GlobalTMDB,
        pause_event: asyncio.Event,
        emit_cb: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        compiled_style_card: str = "",
        tone_preset: str = "natural",
        style_skill_id: Optional[str] = None,
        lang_skill_override: str = "auto",
        is_short_ui: bool = False,
        model_name: Optional[str] = None,
        num_ctx: Optional[int] = None,
        batch_stream_cb: Optional[Callable[[int, float], None]] = None,
    ) -> Tuple[int, float]:
        await pause_event.wait()

        eff_model = model_name or self.get_active_model()
        use_think = (ai_mode == "DEEP_THINK") and _batch_needs_deep_think(batch_rows, mode, is_short_ui, eff_model)

        # Dynamic RAG Glossary (<500 tokens)
        rag_terms = cache_db.match_dynamic_glossary(
            [r["masked_text"] for r in batch_rows],
            source_lang,
            target_lang,
            limit=16,
        )
        sys_prompt = build_translation_system_prompt(
            mode=mode,
            source_lang=source_lang,
            target_lang=target_lang,
            glossary_terms=rag_terms,
            keep_proper_nouns=keep_proper_nouns,
            compiled_style_card="" if is_short_ui else compiled_style_card,
            tone_preset=tone_preset,
            style_skill_id=style_skill_id or tone_preset or "natural_standard",
            lang_skill_override=lang_skill_override,
            is_short_ui=is_short_ui,
            deep_think=use_think,
        )

        # Build ID|Text payload
        id_to_row = {str(idx): row for idx, row in enumerate(batch_rows)}
        user_prompt = "\n".join(f"{idx}|{row['masked_text']}" for idx, row in enumerate(batch_rows))

        total_tokens = 0
        total_elapsed = 0.0

        async def _execute_batch() -> None:
            nonlocal total_tokens, total_elapsed
            content = ""
            thinking = ""
            batch_ctx = int(num_ctx or 2048)
            if use_think:
                probe_vram = float((self.profiler.probe_system() or {}).get("vram_total_gb") or 0.0)
                if probe_vram >= 7.0:
                    batch_ctx = max(batch_ctx, 3072)

            def _live_think_cb(snippet: str) -> None:
                if emit_cb and snippet:
                    emit_cb("thinking_chunk", {"thinking": snippet, "live": True})

            try:
                if use_think:
                    content, thinking, eval_cnt, elapsed = await asyncio.wait_for(
                        self._call_ollama_chat(
                            client,
                            sys_prompt,
                            user_prompt,
                            think=True,
                            temperature=0.22,
                            model_name=eff_model,
                            num_ctx=batch_ctx,
                            stream_cb=batch_stream_cb,
                            thinking_cb=_live_think_cb if emit_cb else None,
                        ),
                        timeout=95.0,
                    )
                else:
                    content, thinking, eval_cnt, elapsed = await self._call_ollama_chat(
                        client,
                        sys_prompt,
                        user_prompt,
                        think=False,
                        temperature=0.15,
                        model_name=eff_model,
                        num_ctx=batch_ctx,
                        stream_cb=batch_stream_cb,
                    )
                total_tokens += eval_cnt
                total_elapsed += elapsed
            except asyncio.CancelledError:
                raise
            except Exception as first_exc:
                if use_think:
                    logger.warning("DEEP_THINK batch failed (%s); auto-retrying batch with think=False.", first_exc)
                    try:
                        content, thinking, eval_cnt, elapsed = await self._call_ollama_chat(
                            client,
                            sys_prompt,
                            user_prompt,
                            think=False,
                            temperature=0.15,
                            model_name=eff_model,
                            num_ctx=num_ctx,
                        )
                        total_tokens += eval_cnt
                        total_elapsed += elapsed
                    except asyncio.CancelledError:
                        raise
                    except Exception as second_exc:
                        if len(batch_rows) == 1:
                            raise
                        logger.warning("Batch fallback also failed (%s); falling back to micro-chunk retries.", second_exc)
                elif len(batch_rows) > 1:
                    logger.warning("Batch call failed (%s); falling back to micro-chunk retries.", first_exc)
                else:
                    raise

            if thinking and emit_cb:
                emit_cb("thinking_chunk", {"thinking": thinking, "live": False})

            parsed = parse_id_pipe_lines(content)
            if len(id_to_row) > 4 and not _is_batch_aligned(content, set(id_to_row.keys())):
                logger.warning("Batch alignment drift detected (size=%d); splitting to micro-chunks.", len(id_to_row))
                parsed = {}

            def _validate_candidate(row: Dict[str, Any], raw_tr: str) -> Tuple[bool, str]:
                nmap = row.get("num_map") or {}
                ok, unmasked, _reason = validate_and_unmask(
                    row["masked_text"],
                    raw_tr,
                    row["tag_map"],
                    row["source_text"],
                    num_map=nmap,
                    keep_num_tokens=True,
                )
                if not ok:
                    return False, row["source_text"]
                fixed_moji = try_fix_mojibake(unmasked)
                if fixed_moji:
                    unmasked = fixed_moji
                unfolded_src = unfold_numbers(row["source_text"], nmap) if nmap else row["source_text"]
                unfolded_out = unfold_numbers(unmasked, nmap) if nmap else unmasked
                bad_q, _q_reason = detect_translation_anomaly(unfolded_src, unfolded_out, target_lang, source_lang=source_lang)
                if bad_q:
                    return False, row["source_text"]
                return True, smart_word_wrap(unmasked, 0)

            def _record_valid(row: Dict[str, Any], text_out: str, badge: str) -> None:
                nmap = row.get("num_map") or {}
                unfolded_src = unfold_numbers(row["source_text"], nmap) if nmap else row["source_text"]
                unfolded_out = unfold_numbers(text_out, nmap) if nmap else text_out
                final_updates.append((row["str_hash"], text_out, badge))
                tm_updates.append((unfolded_src, unfolded_out, row.get("category", "UI")))
                live_rows.append(
                    {
                        "key": row.get("sample_key") or row["str_hash"][:8],
                        "source": unfolded_src,
                        "translated": unfolded_out,
                        "badge": badge,
                    }
                )

            final_updates: List[Tuple[str, str, str]] = []
            tm_updates: List[Tuple[str, str, str]] = []
            live_rows: List[Dict[str, Any]] = []
            retry_ids: List[str] = []

            for idx_str, row in id_to_row.items():
                ok, cleaned = _validate_candidate(row, parsed.get(idx_str, ""))
                if ok:
                    _record_valid(row, cleaned, "OK")
                else:
                    retry_ids.append(idx_str)

            # Up to 2 automatic retries in micro-chunks of 4 lines at temp=0.0 to prevent ID shift & tag drift
            for attempt in range(2):
                if not retry_ids:
                    break
                chunk_sz = 1 if attempt == 1 else 4
                still_bad: List[str] = []
                for c_start in range(0, len(retry_ids), chunk_sz):
                    await pause_event.wait()
                    sub_ids = retry_ids[c_start : c_start + chunk_sz]
                    retry_prompt = "\n".join(f"{rid}|{id_to_row[rid]['masked_text']}" for rid in sub_ids)
                    try:
                        r_content, _r_think, r_tok, r_elap = await self._call_ollama_chat(
                            client,
                            sys_prompt
                            + "\nCRITICAL: Output ONLY 100% clean target-language translations matching input IDs verbatim; translate EVERY common word (including capitalized leading item/role nouns, never copy French/German words or switch to English), preserve all __TAG_x__, __NUM_x__, and __NL__, no foreign characters, no glued words, no repetition!",
                            retry_prompt,
                            think=False,
                            temperature=0.0,
                            model_name=model_name,
                            num_ctx=num_ctx,
                        )
                        total_tokens += r_tok
                        total_elapsed += r_elap
                    except asyncio.CancelledError:
                        raise
                    except Exception as retry_exc:
                        logger.warning("Micro-chunk retry failed (%s); keeping safe fallback for %d lines.", retry_exc, len(sub_ids))
                        still_bad.extend(sub_ids)
                        continue
                    r_parsed = parse_id_pipe_lines(r_content)
                    if len(sub_ids) == 1 and sub_ids[0] not in r_parsed:
                        plain_lines = [
                            ln.strip()
                            for ln in strip_thinking(r_content).splitlines()
                            if ln.strip() and not ln.strip().startswith("```")
                        ]
                        if plain_lines:
                            r_parsed[sub_ids[0]] = re.sub(r"^\d+\s*\|\s*", "", plain_lines[0]).strip()

                    for rid in sub_ids:
                        row = id_to_row[rid]
                        ok, cleaned = _validate_candidate(row, r_parsed.get(rid, ""))
                        if ok:
                            _record_valid(row, cleaned, "TAG FIXED")
                        else:
                            still_bad.append(rid)
                retry_ids = still_bad

            # Safe Fallback for any item still failing after 2 retries: preserve original template/text!
            for rid in retry_ids:
                row = id_to_row[rid]
                nmap = row.get("num_map") or {}
                unfolded_src = unfold_numbers(row["source_text"], nmap) if nmap else row["source_text"]
                final_updates.append((row["str_hash"], row["source_text"], "FALLBACK"))
                live_rows.append(
                    {
                        "key": row.get("sample_key") or row["str_hash"][:8],
                        "source": unfolded_src,
                        "translated": unfolded_src,
                        "badge": "FALLBACK",
                    }
                )

            cache_db.mark_translated_batch(final_updates)
            global_tm.upsert_batch(tm_updates, source_lang, target_lang)

            if emit_cb and live_rows:
                emit_cb("live_rows", {"rows": live_rows})

        if sem is not None:
            async with sem:
                await _execute_batch()
        else:
            await _execute_batch()

        return total_tokens, total_elapsed

    def reset_pause_state(self, game_id: str) -> asyncio.Event:
        ev = self._pause_flags.setdefault(game_id, asyncio.Event())
        ev.set()
        self._pause_accum[game_id] = 0.0
        self._pause_started_at.pop(game_id, None)
        return ev

    def pause_game(self, game_id: str) -> None:
        ev = self._pause_flags.setdefault(game_id, asyncio.Event())
        if ev.is_set():
            ev.clear()
            self._pause_started_at[game_id] = time.perf_counter()

    def resume_game(self, game_id: str) -> None:
        ev = self._pause_flags.setdefault(game_id, asyncio.Event())
        started = self._pause_started_at.pop(game_id, None)
        if started:
            self._pause_accum[game_id] = self._pause_accum.get(game_id, 0.0) + (time.perf_counter() - started)
        ev.set()

    def is_paused(self, game_id: str) -> bool:
        ev = self._pause_flags.get(game_id)
        return bool(ev and not ev.is_set())

    async def run_translation_pipeline(
        self,
        game_id: str,
        source_lang: str,
        target_lang: str,
        scope: str,
        ai_mode: str,
        keep_proper_nouns: bool,
        cache_db: GameCacheDB,
        global_tm: GlobalTMDB,
        emit_cb: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        game_description: str = "",
        tone_preset: str = "natural",
        compiled_style_card: str = "",
        style_card_cb: Optional[Callable[[str], None]] = None,
        style_skill_id: Optional[str] = None,
        lang_skill_override: str = "auto",
        bypass_global_tm: bool = False,
    ) -> Dict[str, Any]:
        """Run 2-Phase Queue Worker Pool with Length Bucketing, Template Folding & optional Dual-Model Local."""
        pause_event = self.reset_pause_state(game_id)
        eff = self.get_effective_config()
        workers = max(1, int(eff.get("parallel_workers", MAX_CONCURRENT_STREAMS)))
        # Keep runtime batch chunks compact for local 1.7B-12B models so ID alignment never drifts (>98% single-pass hit)
        ui_batch_sz = max(8, min(24, int(eff.get("ui_batch_size", 24))))
        sub_batch_sz = max(5, min(16, int(eff.get("sub_batch_size", 16))))
        ctx_sz = int(eff.get("num_ctx", 2048))
        cooldown_s = max(0.0, float(eff.get("batch_cooldown_ms", 0)) / 1000.0)
        main_model = eff.get("main_model", MODEL_NAME)
        dual_enabled = bool(eff.get("dual_model_enabled")) and bool((eff.get("ui_fast_model") or "").strip())
        ui_fast_model = (eff.get("ui_fast_model") or "").strip() if dual_enabled else main_model

        pending = cache_db.get_pending_strings(source_lang, target_lang)
        if not pending:
            prog = cache_db.get_progress(source_lang, target_lang)
            return {"ok": True, "tm_hits": 0, "llm_translated": 0, "progress": prog}

        # Step A: Global TM 0ms lookup (bypassed on explicit Re-apply Skill; skip template rows with __NUM_x__)
        tm_updates: List[Tuple[str, str, str]] = []
        tm_live_rows: List[Dict[str, Any]] = []
        remaining: List[Dict[str, Any]] = []
        tm_chars = 0

        if not bypass_global_tm:
            lookup_texts = [r["source_text"] for r in pending if not r.get("num_map")]
            tm_map = global_tm.lookup_exact_batch(lookup_texts, source_lang, target_lang) if lookup_texts else {}
            for row in pending:
                if row.get("num_map"):
                    remaining.append(row)
                    continue
                hit = tm_map.get(row["source_text"])
                hit_bad = detect_translation_anomaly(row["source_text"], hit, target_lang, source_lang=source_lang)[0] if hit else False
                if hit and not hit_bad:
                    tm_updates.append((row["str_hash"], hit, "TM 0ms"))
                    tm_chars += max(8, len(row["masked_text"]))
                    tm_live_rows.append(
                        {
                            "key": row.get("sample_key") or row["str_hash"][:8],
                            "source": row["source_text"],
                            "translated": hit,
                            "badge": "TM 0ms",
                        }
                    )
                else:
                    remaining.append(row)
        else:
            remaining = list(pending)

        if tm_updates:
            cache_db.mark_translated_batch(tm_updates)
            if emit_cb:
                emit_cb("live_rows", {"rows": tm_live_rows[:80]})
                prog = cache_db.get_progress(source_lang, target_lang)
                emit_cb(
                    "progress",
                    {
                        **prog,
                        "phase": "TM_HIT",
                        "tm_hits": len(tm_updates),
                        "tok_per_sec": 0.0,
                        "eta_seconds": 0 if not remaining else None,
                    },
                )

        if not remaining:
            prog = cache_db.get_progress(source_lang, target_lang)
            return {"ok": True, "tm_hits": len(tm_updates), "llm_translated": 0, "progress": prog}

        await self.ensure_ollama_running()

        baseline_cps = (
            PRESET_PROFILES["DEEP_STORY"]["chars_per_sec"]
            if ai_mode == "DEEP_THINK"
            else (
                PRESET_PROFILES["FAST"]["chars_per_sec"]
                if ai_mode == "FLASH"
                else PRESET_PROFILES["BALANCED"]["chars_per_sec"]
            )
        ) * max(0.35, min(1.8, workers / 4.0))
        total_rem_chars = sum(max(8, len(r["masked_text"])) for r in remaining)
        active_style_card = (compiled_style_card or "").strip()
        effective_style = style_skill_id or tone_preset or "natural_standard"
        has_custom_context = bool(
            (game_description or "").strip() or effective_style.lower() not in ("natural", "natural_standard")
        )
        need_prethink = ai_mode != "FLASH" and (
            not cache_db.list_glossary_terms(source_lang, target_lang)
            or (has_custom_context and not active_style_card)
            or (bypass_global_tm and has_custom_context)
        )

        http_limits = httpx.Limits(max_connections=16, max_keepalive_connections=12)
        async with httpx.AsyncClient(timeout=180.0, limits=http_limits) as client:
            async def _run_prethink() -> None:
                self._active_runtime_model = main_model
                self._active_runtime_role = "PRETHINK"
                if emit_cb:
                    est_total = int(round(9.0 + total_rem_chars / max(15.0, baseline_cps)))
                    prog = cache_db.get_progress(source_lang, target_lang)
                    emit_cb(
                        "progress",
                        {
                            **prog,
                            "phase": "PRETHINK",
                            "phase_label": f"Pha 0 ({main_model}): Nén Style Card & lập Từ điển...",
                            "active_model": main_model,
                            "active_model_role": "PRETHINK",
                            "dual_model_active": dual_enabled,
                            "tm_hits": len(tm_updates),
                            "tok_per_sec": 0.0,
                            "eta_seconds": est_total,
                            "telemetry": self.get_telemetry(),
                        },
                    )
                story_samples = [r for r in remaining if r.get("category") in ("SUBTITLE", "CHOICE")] or remaining

                def _on_style_card(card: str) -> None:
                    nonlocal active_style_card
                    active_style_card = card
                    if style_card_cb:
                        style_card_cb(card)

                def _on_prethink_stream(toks: int, tps: float) -> None:
                    if not emit_cb:
                        return
                    prog_now = cache_db.get_progress(source_lang, target_lang)
                    emit_cb(
                        "progress",
                        {
                            **prog_now,
                            "phase": "PRETHINK",
                            "phase_label": f"Pha 0 ({main_model}): Nén Style Card & lập Từ điển...",
                            "active_model": main_model,
                            "active_model_role": "PRETHINK",
                            "dual_model_active": dual_enabled,
                            "tm_hits": len(tm_updates),
                            "tokens_done": toks,
                            "tok_per_sec": tps,
                            "telemetry": self.get_telemetry(),
                        },
                    )

                try:
                    await asyncio.wait_for(
                        self.pre_think_glossary(
                            client,
                            story_samples[:18],
                            source_lang,
                            target_lang,
                            cache_db,
                            emit_cb=emit_cb,
                            game_description=game_description,
                            tone_preset=tone_preset,
                            style_card_cb=_on_style_card,
                            style_skill_id=effective_style,
                            lang_skill_override=lang_skill_override,
                            model_name=main_model,
                            use_deep_think=(ai_mode == "DEEP_THINK"),
                            stream_cb=_on_prethink_stream,
                        ),
                        timeout=65.0,
                    )
                except Exception as exc:
                    err_desc = str(exc).strip() or type(exc).__name__
                    logger.warning("Phase 0 pre-think skipped after fallback guard (%s); continuing with fallback style card.", err_desc)
                    if not active_style_card:
                        fallback_card = build_fallback_style_card(tone_preset, game_description)
                        if fallback_card:
                            _on_style_card(fallback_card)
                    if emit_cb:
                        emit_cb(
                            "prethink_done",
                            {
                                "thinking": f"Pha 0 chuyển sang chế độ nhanh ({err_desc}) — đã tự dùng Style Card mặc định để vào dịch ngay.",
                                "terms": cache_db.list_glossary_terms(source_lang, target_lang),
                                "style_card": active_style_card,
                                "tok_per_sec": 0.0,
                            },
                        )

            # Step C: Length Bucketing (Short UI / <=6 words vs Story/Dialogue) -> KV Prefix Cache optimization
            def _is_short_ui(row: Dict[str, Any]) -> bool:
                if row.get("category") == "UI":
                    return True
                return len((row.get("masked_text") or "").split()) <= 6

            short_ui_rows = sorted(
                (r for r in remaining if _is_short_ui(r)),
                key=lambda r: (r.get("category") != "UI", len(r.get("masked_text") or "")),
            )
            if ai_mode == "DEEP_THINK":
                # Preserve natural scene & file dialogue order so <think> tracks speaker pronouns coherently
                long_story_rows = sorted(
                    (r for r in remaining if not _is_short_ui(r)),
                    key=lambda r: (str(r.get("file_path") or ""), str(r.get("sample_key") or "")),
                )
            else:
                long_story_rows = sorted(
                    (r for r in remaining if not _is_short_ui(r)),
                    key=lambda r: len(r.get("masked_text") or ""),
                )

            def _chunk_by_budget(rows: List[Dict[str, Any]], max_items: int, max_chars: int) -> List[List[Dict[str, Any]]]:
                chunks: List[List[Dict[str, Any]]] = []
                cur: List[Dict[str, Any]] = []
                cur_chars = 0
                for row in rows:
                    rlen = max(12, len(row.get("masked_text") or ""))
                    if cur and (len(cur) >= max_items or (cur_chars + rlen) > max_chars):
                        chunks.append(cur)
                        cur = []
                        cur_chars = 0
                    cur.append(row)
                    cur_chars += rlen
                if cur:
                    chunks.append(cur)
                return chunks

            max_batch_chars = max(900, min(2400, int(ctx_sz * 1.1)))
            ui_batches: List[Tuple[str, bool, List[Dict[str, Any]]]] = [
                ("UI_HUD", True, chunk)
                for chunk in _chunk_by_budget(short_ui_rows, ui_batch_sz, max_batch_chars)
            ]

            story_prompt_mode = "SUB_ONLY" if scope == "SUB_ONLY" else "STORY"
            # In DEEP_THINK mode, keep story chunks coherent & compact (<=7 lines / <=680 chars) for crisp reasoning
            eff_story_items = min(sub_batch_sz, 7) if ai_mode == "DEEP_THINK" else sub_batch_sz
            eff_story_chars = min(max_batch_chars, 680) if ai_mode == "DEEP_THINK" else max_batch_chars
            story_batches: List[Tuple[str, bool, List[Dict[str, Any]]]] = []
            for chunk in _chunk_by_budget(long_story_rows, eff_story_items, eff_story_chars):
                pm = "SUB_ONLY" if scope == "SUB_ONLY" else (
                    "UI_HUD" if all(x.get("category") == "UI" for x in chunk) else story_prompt_mode
                )
                story_batches.append((pm, False, chunk))

            vram_tot_gb = float((eff.get("hardware") or {}).get("vram_total_gb") or 8.0)
            deep_workers = max(1, min(workers, 2 if vram_tot_gb < 11.0 else 3))

            total_batches = len(ui_batches) + len(story_batches)
            t_phase_start = time.perf_counter()
            cum_tokens = 0
            in_flight_tokens: Dict[int, int] = {}
            batch_seq_counter = 0
            completed_batches = 0
            completed_chars = 0
            ema_cps = baseline_cps
            last_eta_sec: Optional[int] = None
            last_stream_emit = 0.0

            async def _run_batch_and_report(
                prompt_mode: str,
                short_flag: bool,
                b_rows: List[Dict[str, Any]],
                use_model: str,
                pool_width: int = 0,
            ) -> None:
                nonlocal cum_tokens, batch_seq_counter, completed_batches, completed_chars, ema_cps, last_eta_sec, last_stream_emit
                await pause_event.wait()
                batch_seq_counter += 1
                my_bid = batch_seq_counter
                eff_pool_w = pool_width or workers
                b_chars = sum(max(8, len(r["masked_text"])) for r in b_rows)
                is_deep_batch = (ai_mode == "DEEP_THINK") and _batch_needs_deep_think(b_rows, prompt_mode, short_flag, use_model)

                def _on_batch_stream(cur_toks: int, _tps: float) -> None:
                    nonlocal last_stream_emit
                    in_flight_tokens[my_bid] = cur_toks
                    now_s = time.perf_counter()
                    if not emit_cb or (now_s - last_stream_emit < 0.4):
                        return
                    last_stream_emit = now_s
                    live_total_toks = cum_tokens + sum(in_flight_tokens.values())
                    wall_s = max(0.25, (now_s - t_phase_start) - self._pause_accum.get(game_id, 0.0))
                    live_tps = round(live_total_toks / wall_s, 1)
                    prog_now = cache_db.get_progress(source_lang, target_lang)
                    emit_cb(
                        "progress",
                        {
                            **prog_now,
                            "phase": "TRANSLATING",
                            "phase_label": (
                                f"🧠 Deep-Think {eff_pool_w}x ({use_model}) • Gói {completed_batches}/{total_batches} • Đang suy luận..."
                                if is_deep_batch
                                else f"Pool {eff_pool_w}x ({use_model}) • {completed_batches}/{total_batches} gói..."
                            ),
                            "active_model": use_model,
                            "active_model_role": "MAIN_STORY",
                            "dual_model_active": dual_enabled,
                            "tm_hits": len(tm_updates),
                            "tokens_done": live_total_toks,
                            "tok_per_sec": live_tps,
                            "chars_done": completed_chars + tm_chars,
                            "chars_total": total_rem_chars + tm_chars,
                            "telemetry": self.get_telemetry(),
                        },
                    )

                try:
                    toks, _elap = await self._translate_one_batch_with_retry(
                        client=client,
                        sem=None,
                        batch_rows=b_rows,
                        mode=prompt_mode,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        ai_mode=ai_mode,
                        keep_proper_nouns=keep_proper_nouns,
                        cache_db=cache_db,
                        global_tm=global_tm,
                        pause_event=pause_event,
                        emit_cb=emit_cb,
                        compiled_style_card=active_style_card,
                        tone_preset=tone_preset,
                        style_skill_id=effective_style,
                        lang_skill_override=lang_skill_override,
                        is_short_ui=short_flag,
                        model_name=use_model,
                        num_ctx=ctx_sz,
                        batch_stream_cb=_on_batch_stream if is_deep_batch else None,
                    )
                finally:
                    in_flight_tokens.pop(my_bid, None)

                cum_tokens += toks
                completed_batches += 1
                completed_chars += b_chars

                active_wall = max(0.25, (time.perf_counter() - t_phase_start) - self._pause_accum.get(game_id, 0.0))
                tps = round((cum_tokens + sum(in_flight_tokens.values())) / active_wall, 1)

                cum_cps = completed_chars / active_wall
                if completed_batches <= 4:
                    warmup = min(1.0, completed_chars / 1200.0)
                    target_cps = (warmup * cum_cps) + ((1.0 - warmup) * baseline_cps)
                else:
                    target_cps = cum_cps
                ema_cps = (0.85 * ema_cps) + (0.15 * max(12.0, target_cps))

                rem_chars = max(0, total_rem_chars - completed_chars)
                rem_batches = max(0, total_batches - completed_batches)
                rem_waves = (rem_batches + eff_pool_w - 1) // eff_pool_w
                raw_eta = int(round((rem_chars / max(20.0, ema_cps)) + (rem_waves * 0.25))) if rem_batches > 0 else 0
                if rem_batches == 0:
                    eta = 0
                elif last_eta_sec is not None:
                    eta = min(last_eta_sec, raw_eta)
                else:
                    eta = raw_eta
                last_eta_sec = eta

                prog = cache_db.get_progress(source_lang, target_lang)
                weighted_pct = (
                    round(((completed_chars + tm_chars) / max(1, total_rem_chars + tm_chars)) * 100.0, 1)
                    if total_batches > 0
                    else prog["pct"]
                )
                if emit_cb:
                    role_tag = "UI_FAST" if (dual_enabled and short_flag and use_model == ui_fast_model and ui_fast_model != main_model) else "MAIN_STORY"
                    self._active_runtime_model = use_model
                    self._active_runtime_role = role_tag
                    pool_tag = f"🧠 Deep-Think {eff_pool_w}x" if is_deep_batch else f"Pool {eff_pool_w}x"
                    emit_cb(
                        "progress",
                        {
                            **prog,
                            "weighted_pct": max(prog["pct"], min(99.5 if rem_batches > 0 else 100.0, weighted_pct)),
                            "phase": "TRANSLATING",
                            "phase_label": f"{pool_tag} ({use_model}) • {completed_batches}/{total_batches} gói • {rem_chars:,} ký tự còn lại",
                            "active_model": use_model,
                            "active_model_role": role_tag,
                            "dual_model_active": dual_enabled,
                            "tm_hits": len(tm_updates),
                            "tokens_done": cum_tokens + sum(in_flight_tokens.values()),
                            "tok_per_sec": tps,
                            "eta_seconds": eta,
                            "chars_done": completed_chars + tm_chars,
                            "chars_total": total_rem_chars + tm_chars,
                            "telemetry": self.get_telemetry(),
                        },
                    )

            async def _drain_queue(
                items: List[Tuple[str, bool, List[Dict[str, Any]], str]],
                max_workers: Optional[int] = None,
            ) -> None:
                if not items:
                    return
                q: asyncio.Queue[Tuple[str, bool, List[Dict[str, Any]], str]] = asyncio.Queue()
                for it in items:
                    q.put_nowait(it)
                n_workers = min(max_workers or workers, len(items))

                async def _worker() -> None:
                    while True:
                        try:
                            pm, short_fl, b_rows, mdl = q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        try:
                            await _run_batch_and_report(pm, short_fl, b_rows, mdl, pool_width=n_workers)
                            if cooldown_s > 0:
                                await asyncio.sleep(cooldown_s)
                        finally:
                            q.task_done()

                await asyncio.gather(*(_worker() for _ in range(n_workers)))

            try:
                if dual_enabled and ui_fast_model != main_model and ui_batches:
                    # Dual-Model: run fast UI batches first, purge ui_fast_model from VRAM, then run Pre-Think + Story on main_model
                    self._active_runtime_model = ui_fast_model
                    self._active_runtime_role = "UI_FAST"
                    await _drain_queue([(pm, sf, br, ui_fast_model) for pm, sf, br in ui_batches], max_workers=workers)
                    self._active_runtime_model = main_model
                    self._active_runtime_role = "VRAM_SWAP"
                    if emit_cb:
                        swap_prog = cache_db.get_progress(source_lang, target_lang)
                        emit_cb(
                            "progress",
                            {
                                **swap_prog,
                                "weighted_pct": swap_prog["pct"],
                                "phase": "VRAM_SWAP",
                                "phase_label": f"⚡ Xong Pha 1 UI ({ui_fast_model}) • Đang xả VRAM & chuyển sang {main_model} dịch Cốt truyện...",
                                "active_model": main_model,
                                "active_model_role": "VRAM_SWAP",
                                "dual_model_active": True,
                                "tokens_done": cum_tokens,
                                "tok_per_sec": 0.0,
                                "telemetry": self.get_telemetry(),
                            },
                        )
                    await self._unload_model(ui_fast_model, client=client)
                    if story_batches:
                        if need_prethink:
                            await _run_prethink()
                        self._active_runtime_model = main_model
                        self._active_runtime_role = "MAIN_STORY"
                        story_w = deep_workers if ai_mode == "DEEP_THINK" else workers
                        await _drain_queue([(pm, sf, br, main_model) for pm, sf, br in story_batches], max_workers=story_w)
                else:
                    if need_prethink:
                        await _run_prethink()
                    self._active_runtime_model = main_model
                    self._active_runtime_role = "MAIN_STORY"
                    if ai_mode == "DEEP_THINK" and story_batches:
                        # Run fast UI batches at full concurrency first, then run coherent Deep-Think story batches with dedicated KV-safe concurrency
                        if ui_batches:
                            await _drain_queue([(pm, sf, br, main_model) for pm, sf, br in ui_batches], max_workers=workers)
                        await _drain_queue([(pm, sf, br, main_model) for pm, sf, br in story_batches], max_workers=deep_workers)
                    else:
                        merged_queue = [(pm, sf, br, main_model) for pm, sf, br in (ui_batches + story_batches)]
                        await _drain_queue(merged_queue, max_workers=workers)
            finally:
                self._active_runtime_role = "IDLE"

        final_prog = cache_db.get_progress(source_lang, target_lang)
        return {
            "ok": True,
            "tm_hits": len(tm_updates),
            "llm_translated": len(remaining),
            "progress": final_prog,
        }

    async def qa_polish_translations(
        self,
        cache_db: GameCacheDB,
        global_tm: GlobalTMDB,
        source_lang: str,
        target_lang: str,
        emit_cb: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        game_description: str = "",
        tone_preset: str = "natural",
        compiled_style_card: str = "",
        game_id: str = "",
        style_skill_id: Optional[str] = None,
        lang_skill_override: str = "auto",
    ) -> Dict[str, Any]:
        """Semantic QA review pass using Worker Pool, Skill Hub prefix & Template Folding safety."""
        if (target_lang or "").lower() != "vi":
            return {"reviewed": 0, "polished": 0, "samples": []}

        pause_event = self._pause_flags.setdefault(game_id, asyncio.Event()) if game_id else asyncio.Event()
        if not game_id or (not pause_event.is_set() and game_id not in self._pause_started_at):
            pause_event.set()

        done_rows = cache_db.get_done_rows_for_qa(source_lang, target_lang)
        if not done_rows:
            return {"reviewed": 0, "polished": 0, "samples": []}

        await self.ensure_ollama_running()
        eff = self.get_effective_config()
        workers = max(1, int(eff.get("parallel_workers", MAX_CONCURRENT_STREAMS)))
        cooldown_s = max(0.0, float(eff.get("batch_cooldown_ms", 0)) / 1000.0)
        main_model = eff.get("main_model", MODEL_NAME)
        ctx_sz = int(eff.get("num_ctx", 2048))

        from core.prompts import get_lang_display_name
        from core.skill_manager import SkillManager

        effective_style = style_skill_id or tone_preset or "natural_standard"
        resolved = SkillManager().resolve_active_skills(
            source_lang=source_lang,
            target_lang=target_lang,
            style_skill_id=effective_style,
            lang_skill_override=lang_skill_override,
            source_lang_name=get_lang_display_name(source_lang),
            target_lang_name=get_lang_display_name(target_lang),
        )
        skill_prefix = f"{resolved['combined_prefix']}\n\n" if resolved.get("combined_prefix") else ""
        style_card = (compiled_style_card or "").strip() or build_fallback_style_card(tone_preset, game_description)
        style_prefix = f"{skill_prefix}[GAME STYLE CARD - FIXED PREFIX]\n{style_card}\n\n"
        qa_sys = style_prefix + (
            "You are a Senior Vietnamese Game Localization QA Lead.\n"
            "Inspect each `ID|[EN] ... || [VI] ...` line.\n"
            "Rewrite ONLY lines that have a REAL defect:\n"
            "1. Wrong/shifted meaning vs [EN] (CRITICAL: e.g. 'Sit' must be 'Ngồi' NOT 'Đảo ngược trục Y'; 'Host'/'HOST' means lobby host 'Chủ phòng'/'Tạo phòng' NEVER 'Truyền tải'; 'Inventory' is 'Túi đồ'/'Kho đồ' NEVER 'Hồi tài sản'; 'Options' is 'Tùy chọn' NEVER 'Tình trạng'; 'Matchmaking' is 'Ghép trận' NEVER 'Tìm kép'; 'Sleep' is 'Đi ngủ' NEVER 'Mất ngủ'; 'Submerge' is 'Lặn xuống'; 'Spider' is 'Nhện'; 'Hat' is 'Mũ').\n"
            "2. Typos, missing Vietnamese diacritics ('DUNG CẢM'->'DŨNG CẢM', 'KHÔNG THE'->'KHÔNG THỂ', 'Mù ma'->'Mũ ma', 'SỢI THẬT'->'SỢ THẬT'), or stray English glue words.\n"
            "Keep natural simple gaming words blended into Vietnamese (Video, Camera, Server, Micro, Game, Lobby, Ping, Offline, VSync, Steam Deck) unchanged!\n"
            "STRICT OUTPUT FORMAT (3 fields separated by `|` so IDs NEVER shift):\n"
            "`ID|EXACT_ENGLISH_TEXT|Polished_Vietnamese`\n"
            "Example: `3|HOST|TẠO PHÒNG`\n"
            "- NEVER include `[EN]`, `[VI]`, `=>`, `->`, or `||`.\n"
            "- Preserve every __TAG_x__, __NUM_x__, and __NL__ verbatim. If all lines are good, output `NONE`."
        )

        from rapidfuzz import fuzz

        qa_batch_size = 10
        batches = [done_rows[i : i + qa_batch_size] for i in range(0, len(done_rows), qa_batch_size)]
        polished_updates: List[Tuple[str, str, str]] = []
        pending_flush_updates: List[Tuple[str, str, str]] = []
        pending_tm_updates: List[Tuple[str, str, str]] = []
        tm_updates: List[Tuple[str, str, str]] = []
        samples: List[Dict[str, str]] = []
        total_qa = len(batches)
        completed_qa = 0
        cum_tokens = 0
        last_qa_eta: Optional[int] = None
        t_qa_start = time.perf_counter()
        base_prog = cache_db.get_progress(source_lang, target_lang)

        self._active_runtime_model = main_model
        self._active_runtime_role = "QA_POLISH"
        if emit_cb and total_qa > 0:
            emit_cb(
                "progress",
                {
                    **base_prog,
                    "weighted_pct": 1.0,
                    "phase": "REPAIR_QA",
                    "phase_label": f"Fix lỗi • AI QA Pool {workers}x ({main_model}) • 0/{total_qa} gói",
                    "active_model": main_model,
                    "active_model_role": "QA_POLISH",
                    "dual_model_active": bool(eff.get("dual_model_enabled")),
                    "tokens_done": 0,
                    "tok_per_sec": 0.0,
                    "qa_done": 0,
                    "qa_total": total_qa,
                    "telemetry": self.get_telemetry(),
                },
            )

        http_limits = httpx.Limits(max_connections=16, max_keepalive_connections=12)
        async with httpx.AsyncClient(timeout=120.0, limits=http_limits) as client:
            async def _qa_one(b_rows: List[Dict[str, Any]]) -> None:
                nonlocal completed_qa, cum_tokens, last_qa_eta
                await pause_event.wait()
                id_map = {str(i): r for i, r in enumerate(b_rows)}
                lines = []
                for i, r in enumerate(b_rows):
                    masked_cur, _ = mask_tags(r["translated_text"])
                    lines.append(f"{i}|[EN] {r['masked_text']} || [VI] {masked_cur}")
                user_p = "\n".join(lines)
                _tok = 0
                content = ""
                try:
                    content, _th, _tok, _el = await self._call_ollama_chat(
                        client,
                        qa_sys,
                        user_p,
                        think=False,
                        temperature=0.05,
                        num_predict=750,
                        model_name=main_model,
                        num_ctx=ctx_sz,
                    )
                except Exception as exc:
                    logger.warning("QA batch skipped due to error: %s", exc)
                cleaned = strip_thinking(content).strip()
                if cleaned and cleaned.upper() != "NONE":
                    parsed = parse_id_pipe_lines(cleaned)
                    for rid, raw_rest in parsed.items():
                        if "|" not in raw_rest:
                            continue
                        row_hint = id_map.get(rid)
                        anchor_part, cand_masked = _split_qa_anchor_and_candidate(raw_rest, row_hint)
                        anchor_clean = re.sub(r"^\[EN\]\s*", "", anchor_part).strip()
                        cand_masked = re.sub(r"^(?:\|\s*)?(?:\[VI\]\s*)?", "", cand_masked).strip()
                        if "||" in cand_masked or "[EN]" in cand_masked or "[VI]" in cand_masked or "=>" in cand_masked or " -> " in cand_masked:
                            continue
                        row = row_hint
                        if not row or fuzz.ratio(anchor_clean.lower(), row["masked_text"].lower()) < 75:
                            best_row = max(
                                b_rows,
                                key=lambda cand_r: fuzz.ratio(anchor_clean.lower(), cand_r["masked_text"].lower()),
                                default=None,
                            )
                            if not best_row or fuzz.ratio(anchor_clean.lower(), best_row["masked_text"].lower()) < 85:
                                continue
                            row = best_row
                        if not cand_masked or cand_masked == row["masked_text"]:
                            continue
                        if cand_masked.count("|") != row["masked_text"].count("|"):
                            continue
                        if re.match(r"^[^|{}]*\}\s*[^|]*\|", cand_masked):
                            continue
                        nmap = row.get("num_map") or {}
                        ok, unmasked, _ = validate_and_unmask(
                            row["masked_text"],
                            cand_masked,
                            row["tag_map"],
                            row["source_text"],
                            num_map=nmap,
                            keep_num_tokens=True,
                        )
                        if not ok:
                            continue
                        unfolded_src = unfold_numbers(row["source_text"], nmap) if nmap else row["source_text"]
                        unfolded_out = unfold_numbers(unmasked, nmap) if nmap else unmasked
                        if "(" in unfolded_out and "(" not in unfolded_src:
                            continue
                        if re.match(r"^[A-Z]\s+[a-zà-ỹ]", unfolded_out.strip()) and not re.match(r"^[A-Z]\s+", unfolded_src.strip()):
                            continue
                        bad_q, _ = detect_translation_anomaly(unfolded_src, unfolded_out, target_lang, source_lang=source_lang)
                        if bad_q:
                            continue
                        if unmasked.strip() and unmasked.strip() != row["translated_text"].strip():
                            row_upd = (row["str_hash"], unmasked, "QA POLISHED")
                            tm_upd = (unfolded_src, unfolded_out, row.get("category", "UI"))
                            polished_updates.append(row_upd)
                            pending_flush_updates.append(row_upd)
                            tm_updates.append(tm_upd)
                            pending_tm_updates.append(tm_upd)
                            if len(samples) < 12:
                                samples.append(
                                    {
                                        "source": unfolded_src,
                                        "before": unfold_numbers(row["translated_text"], nmap) if nmap else row["translated_text"],
                                        "after": unfolded_out,
                                    }
                                )

                cum_tokens += int(_tok or 0)
                completed_qa += 1
                if completed_qa % 20 == 0 and pending_flush_updates:
                    batch_u = list(pending_flush_updates)
                    batch_tm = list(pending_tm_updates)
                    pending_flush_updates.clear()
                    pending_tm_updates.clear()
                    cache_db.mark_translated_batch(batch_u)
                    global_tm.upsert_batch(batch_tm, source_lang, target_lang)
                if emit_cb:
                    paused_sec = self._pause_accum.get(game_id, 0.0) if game_id else 0.0
                    active_wall = max(0.2, (time.perf_counter() - t_qa_start) - paused_sec)
                    tps = round(cum_tokens / active_wall, 1)
                    rem_batches = max(0, total_qa - completed_qa)
                    raw_eta = int(round((active_wall / max(1, completed_qa)) * rem_batches)) if rem_batches > 0 else 0
                    if rem_batches == 0:
                        eta = 0
                    elif last_qa_eta is not None:
                        eta = min(last_qa_eta, raw_eta)
                    else:
                        eta = raw_eta
                    last_qa_eta = eta
                    qa_pct = round((completed_qa / max(1, total_qa)) * 100.0, 1)
                    emit_cb(
                        "progress",
                        {
                            **base_prog,
                            "weighted_pct": max(1.0, min(99.5 if rem_batches > 0 else 100.0, qa_pct)),
                            "phase": "REPAIR_QA",
                            "phase_label": f"Fix lỗi • AI QA ({completed_qa}/{total_qa} gói • chuốt {len(polished_updates)} câu)",
                            "active_model": main_model,
                            "active_model_role": "QA_POLISH",
                            "dual_model_active": bool(eff.get("dual_model_enabled")),
                            "tokens_done": cum_tokens,
                            "tok_per_sec": tps,
                            "eta_seconds": eta,
                            "qa_done": completed_qa,
                            "qa_total": total_qa,
                            "telemetry": self.get_telemetry(),
                        },
                    )

            qa_q: asyncio.Queue[List[Dict[str, Any]]] = asyncio.Queue()
            for b in batches:
                qa_q.put_nowait(b)

            async def _qa_worker() -> None:
                while True:
                    try:
                        b = qa_q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        await _qa_one(b)
                        if cooldown_s > 0:
                            await asyncio.sleep(cooldown_s)
                    finally:
                        qa_q.task_done()

            try:
                n_qa_workers = min(workers, len(batches))
                await asyncio.gather(*(_qa_worker() for _ in range(n_qa_workers)))
            finally:
                self._active_runtime_role = "IDLE"

        if pending_flush_updates:
            cache_db.mark_translated_batch(pending_flush_updates)
            global_tm.upsert_batch(pending_tm_updates, source_lang, target_lang)
        if polished_updates:
            if emit_cb:
                emit_cb(
                    "qa_polish_done",
                    {
                        "reviewed": len(done_rows),
                        "polished": len(polished_updates),
                        "tokens_done": cum_tokens,
                        "samples": samples,
                    },
                )

        return {"reviewed": len(done_rows), "polished": len(polished_updates), "tokens_done": cum_tokens, "samples": samples}
