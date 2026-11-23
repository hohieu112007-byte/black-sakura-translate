"""Auto Hardware Profiler & Local AI Settings Persistence (<app_dir>/settings.json)."""

from __future__ import annotations

import json
import os
import time
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil

from core.atomic_io import atomic_write_text

with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    try:
        import pynvml
        _HAS_NVML = True
    except Exception:
        _HAS_NVML = False

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = APP_DIR / "settings.json"

_INITIAL_SYSTEM_OLLAMA_PARALLEL = os.environ.get("OLLAMA_NUM_PARALLEL")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "auto_tune": True,
    "main_model": "qwen3.5:4b",
    "dual_model_enabled": False,
    "ui_fast_model": "qwen3:1.7b",
    "quiet_mode": False,
    "parallel_workers": 5,
    "num_ctx": 2048,
    "kv_cache_type": "q8_0",
    "ui_batch_size": 80,
    "sub_batch_size": 25,
    "ollama_base_url": "http://localhost:11434",
}


def normalize_ollama_url(url: Optional[str]) -> str:
    raw = (url or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").strip().rstrip("/")
    if not raw.startswith("http"):
        raw = f"http://{raw}"
    return raw


def classify_hardware_tier(
    has_nvidia_gpu: bool,
    vram_total_gb: float,
    ram_free_gb: float = 8.0,
) -> Dict[str, Any]:
    """Derive safe Local Ollama concurrency, context, KV cache & batch profile from GPU VRAM / System RAM."""
    if not has_nvidia_gpu or vram_total_gb <= 4.5:
        workers = 1 if not has_nvidia_gpu else 2
        if not has_nvidia_gpu and ram_free_gb >= 12.0:
            workers = 2
        return {
            "tier": "LOW_CPU_4GB",
            "tier_label": "CPU Only" if not has_nvidia_gpu else "Low VRAM ≤ 4GB",
            "parallel_workers": workers,
            "num_ctx": 2048,
            "kv_cache_type": "q4_0",
            "ui_batch_size": 40,
            "sub_batch_size": 15,
            "recommended_models": [
                {"name": "qwen3:1.7b", "size_gb": 1.4, "desc": "Siêu nhẹ cho máy CPU hoặc GPU ≤ 4GB VRAM"},
                {"name": "qwen2.5:3b", "size_gb": 1.9, "desc": "Nhanh, ổn định, ít tốn RAM/VRAM"},
                {"name": "qwen3.5:4b", "size_gb": 2.7, "desc": "Cân bằng chất lượng nếu RAM trống ≥ 8GB"},
            ],
        }

    if vram_total_gb <= 6.5:
        return {
            "tier": "MID_6GB",
            "tier_label": "Auto 6GB",
            "parallel_workers": 3,
            "num_ctx": 2048,
            "kv_cache_type": "q4_0",
            "ui_batch_size": 60,
            "sub_batch_size": 20,
            "recommended_models": [
                {"name": "qwen3.5:4b", "size_gb": 2.7, "desc": "Khuyên dùng chuẩn cho 6GB VRAM (3 luồng)"},
                {"name": "qwen3:4b", "size_gb": 2.6, "desc": "Hỗ trợ Deep-Think mượt trên 6GB VRAM"},
                {"name": "qwen3:1.7b", "size_gb": 1.4, "desc": "Model phụ siêu nhanh chuyên dịch UI"},
            ],
        }

    if vram_total_gb < 11.0:
        return {
            "tier": "BALANCED_8GB",
            "tier_label": "Auto 8GB",
            "parallel_workers": 5,
            "num_ctx": 2048,
            "kv_cache_type": "q8_0",
            "ui_batch_size": 80,
            "sub_batch_size": 25,
            "recommended_models": [
                {"name": "qwen3.5:4b", "size_gb": 2.7, "desc": "Tối ưu tốc độ & chất lượng cho 8GB VRAM (5 luồng)"},
                {"name": "qwen3:8b", "size_gb": 4.9, "desc": "Model 8B văn phong sâu cho 8GB VRAM"},
                {"name": "qwen3:1.7b", "size_gb": 1.4, "desc": "Model phụ Dual-Model dịch UI cực nhanh"},
            ],
        }

    return {
        "tier": "HIGH_12GB_PLUS",
        "tier_label": "Auto 12GB+",
        "parallel_workers": 8,
        "num_ctx": 4096,
        "kv_cache_type": "q8_0",
        "ui_batch_size": 100,
        "sub_batch_size": 35,
        "recommended_models": [
            {"name": "gemma4:12b", "size_gb": 7.5, "desc": "Model 12B ngữ cảnh rộng cho VRAM ≥ 12GB"},
            {"name": "qwen3:8b", "size_gb": 4.9, "desc": "Tốc độ cao 8 luồng song song"},
            {"name": "qwen3.5:4b", "size_gb": 2.7, "desc": "Siêu tốc 8 luồng hoặc Dual-Model UI"},
        ],
    }


class HardwareProfiler:
    """Probes NVIDIA VRAM & System RAM, computes hardware tier, and persists settings.json."""

    def __init__(self, settings_path: Optional[Path] = None) -> None:
        self.settings_path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
        self._nvml_ready = False
        self._nvml_handle = None
        self._gpu_name = "CPU Fallback"
        self._vram_total_gb = 0.0
        self._probe_cache: Optional[Dict[str, Any]] = None
        self._probe_ts: float = 0.0
        self._settings_cache: Optional[Dict[str, Any]] = None
        self._settings_mtime_ns: int = -1
        if _HAS_NVML:
            try:
                pynvml.nvmlInit()
                self._nvml_ready = True
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                raw_name = pynvml.nvmlDeviceGetName(handle)
                self._nvml_handle = handle
                self._gpu_name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                self._vram_total_gb = round(mem.total / (1024 ** 3), 2)
            except Exception:
                self._nvml_ready = False
                self._nvml_handle = None

    def probe_system(self, force_refresh: bool = False) -> Dict[str, Any]:
        now = time.monotonic()
        if not force_refresh and self._probe_cache is not None and (now - self._probe_ts) < 2.5:
            return dict(self._probe_cache)

        vm = psutil.virtual_memory()
        ram_total_gb = round(vm.total / (1024 ** 3), 2)
        ram_available_gb = round(vm.available / (1024 ** 3), 2)
        ram_used_gb = round(vm.used / (1024 ** 3), 2)

        has_gpu = False
        gpu_name = self._gpu_name if self._nvml_handle is not None else "CPU Fallback"
        vram_used_gb = 0.0
        vram_total_gb = self._vram_total_gb
        vram_free_gb = 0.0

        if self._nvml_ready and self._nvml_handle is not None:
            try:
                mem = pynvml.nvmlDeviceGetMemoryInfo(self._nvml_handle)
                vram_total_gb = round(mem.total / (1024 ** 3), 2)
                vram_used_gb = round(mem.used / (1024 ** 3), 2)
                vram_free_gb = round(mem.free / (1024 ** 3), 2)
                has_gpu = vram_total_gb > 0.5
            except Exception:
                has_gpu = False

        tier_profile = classify_hardware_tier(has_gpu, vram_total_gb, ram_available_gb)
        res = {
            "has_nvidia_gpu": has_gpu,
            "gpu_name": gpu_name,
            "vram_total_gb": vram_total_gb,
            "vram_used_gb": vram_used_gb,
            "vram_free_gb": vram_free_gb,
            "ram_total_gb": ram_total_gb,
            "ram_used_gb": ram_used_gb,
            "ram_available_gb": ram_available_gb,
            "cpu_pct": round(psutil.cpu_percent(interval=None), 1),
            **tier_profile,
        }
        self._probe_cache = dict(res)
        self._probe_ts = now
        return res

    def load_settings(self) -> Dict[str, Any]:
        cur_mtime = -1
        if self.settings_path.exists():
            try:
                cur_mtime = self.settings_path.stat().st_mtime_ns
            except OSError:
                cur_mtime = -1
        if self._settings_cache is not None and cur_mtime == self._settings_mtime_ns:
            return dict(self._settings_cache)

        cfg = dict(DEFAULT_SETTINGS)
        if self.settings_path.exists():
            try:
                saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    for k, v in saved.items():
                        if k in cfg and v is not None:
                            cfg[k] = v
            except Exception:
                pass
        cfg["ollama_base_url"] = normalize_ollama_url(cfg.get("ollama_base_url"))
        cfg["auto_tune"] = bool(cfg.get("auto_tune", True))
        cfg["dual_model_enabled"] = bool(cfg.get("dual_model_enabled", False))
        cfg["quiet_mode"] = bool(cfg.get("quiet_mode", False))
        cfg["parallel_workers"] = max(1, min(12, int(cfg.get("parallel_workers", 5))))
        cfg["num_ctx"] = max(1024, min(8192, int(cfg.get("num_ctx", 2048))))
        cfg["ui_batch_size"] = max(10, min(150, int(cfg.get("ui_batch_size", 80))))
        cfg["sub_batch_size"] = max(5, min(60, int(cfg.get("sub_batch_size", 25))))
        self._settings_cache = dict(cfg)
        self._settings_mtime_ns = cur_mtime
        return cfg

    def save_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        cur = self.load_settings()
        for k in DEFAULT_SETTINGS:
            if k in updates and updates[k] is not None:
                cur[k] = updates[k]
        cur["ollama_base_url"] = normalize_ollama_url(cur.get("ollama_base_url"))
        cur["auto_tune"] = bool(cur.get("auto_tune", True))
        cur["dual_model_enabled"] = bool(cur.get("dual_model_enabled", False))
        cur["quiet_mode"] = bool(cur.get("quiet_mode", False))
        cur["parallel_workers"] = max(1, min(12, int(cur["parallel_workers"])))
        cur["num_ctx"] = max(1024, min(8192, int(cur["num_ctx"])))
        cur["ui_batch_size"] = max(10, min(150, int(cur["ui_batch_size"])))
        cur["sub_batch_size"] = max(5, min(60, int(cur["sub_batch_size"])))
        if cur["kv_cache_type"] not in ("q4_0", "q8_0", "f16"):
            cur["kv_cache_type"] = "q8_0"

        atomic_write_text(self.settings_path, json.dumps(cur, indent=2, ensure_ascii=False))
        self._settings_cache = None
        self._settings_mtime_ns = -1
        return self.get_effective_settings()

    def get_effective_settings(self) -> Dict[str, Any]:
        hw = self.probe_system()
        cfg = self.load_settings()
        if cfg["auto_tune"]:
            cfg["parallel_workers"] = hw["parallel_workers"]
            cfg["num_ctx"] = hw["num_ctx"]
            cfg["kv_cache_type"] = hw["kv_cache_type"]
            cfg["ui_batch_size"] = hw["ui_batch_size"]
            cfg["sub_batch_size"] = hw["sub_batch_size"]
            cfg["profile_badge"] = f"{cfg['parallel_workers']} Luồng ({hw['tier_label']})"
        else:
            cfg["profile_badge"] = f"{cfg['parallel_workers']} Luồng (Tùy chỉnh)"

        if cfg.get("quiet_mode"):
            cfg["parallel_workers"] = min(int(cfg["parallel_workers"]), 2)
            cfg["batch_cooldown_ms"] = 65
            cfg["profile_badge"] = f"{cfg['parallel_workers']} Luồng (Quiet 🎧)"
        else:
            cfg["batch_cooldown_ms"] = 0

        ollama_parallel_env = _INITIAL_SYSTEM_OLLAMA_PARALLEL or os.environ.get("OLLAMA_NUM_PARALLEL", "")
        cfg["ollama_env_warning"] = (
            "Hệ thống đang đặt OLLAMA_NUM_PARALLEL=1; nên đặt OLLAMA_NUM_PARALLEL lớn hơn để chạy đa luồng tối đa."
            if (_INITIAL_SYSTEM_OLLAMA_PARALLEL == "1" and cfg["parallel_workers"] > 1)
            else ""
        )
        cfg["ollama_num_parallel_detected"] = ollama_parallel_env
        return {**cfg, "hardware": hw}

    def apply_ollama_env(self) -> Dict[str, str]:
        eff = self.get_effective_settings()
        env_updates = {
            "OLLAMA_NUM_PARALLEL": str(eff["parallel_workers"]),
            "OLLAMA_KV_CACHE_TYPE": str(eff["kv_cache_type"]),
            "OLLAMA_FLASH_ATTENTION": "1",
            "OLLAMA_KEEP_ALIVE": "10m",
        }
        for k, v in env_updates.items():
            os.environ[k] = v
        return env_updates
