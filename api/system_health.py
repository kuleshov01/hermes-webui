"""Safe aggregate host resource metrics for the WebUI VPS panel (#693).

The browser only needs coarse CPU/RAM/disk usage. Uses psutil when available
(Linux, macOS, Windows) with a /proc/stat fallback for Linux without psutil.
"""

from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HAS_PSUTIL = False
try:
    import psutil as _psutil
    _HAS_PSUTIL = True
except ImportError:
    _psutil = None

_PROC_STAT = Path("/proc/stat")
_PROC_MEMINFO = Path("/proc/meminfo")
_IS_LINUX = sys.platform.startswith("linux")
_CPU_SAMPLE_SECONDS = 0.05


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_percent(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if numeric < 0:
        numeric = 0.0
    if numeric > 100:
        numeric = 100.0
    return round(numeric, 1)


# ── CPU ──

def _cpu_percent_psutil() -> float:
    return _psutil.cpu_percent(interval=_CPU_SAMPLE_SECONDS)


def _read_proc_stat_cpu() -> tuple[int, int]:
    """Return (idle_ticks, total_ticks) from Linux /proc/stat."""
    with _PROC_STAT.open("r", encoding="utf-8") as handle:
        first = handle.readline().strip().split()
    if not first or first[0] != "cpu":
        raise RuntimeError("proc_stat_unavailable")
    values = [int(part) for part in first[1:]]
    if len(values) < 4:
        raise RuntimeError("proc_stat_unavailable")
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    if total <= 0:
        raise RuntimeError("proc_stat_unavailable")
    return idle, total


def _cpu_delta_percent(start: tuple[int, int], end: tuple[int, int]) -> float:
    idle_delta = end[0] - start[0]
    total_delta = end[1] - start[1]
    if total_delta <= 0:
        return 0.0
    busy_delta = max(0, total_delta - max(0, idle_delta))
    return _clamp_percent((busy_delta / total_delta) * 100.0)


def _cpu_percent_proc() -> float:
    """Sample aggregate CPU usage from /proc/stat (Linux only)."""
    start = _read_proc_stat_cpu()
    time.sleep(_CPU_SAMPLE_SECONDS)
    end = _read_proc_stat_cpu()
    return _cpu_delta_percent(start, end)


def _cpu_percent() -> float:
    """Sample aggregate CPU usage."""
    if _HAS_PSUTIL:
        return _cpu_percent_psutil()
    if _IS_LINUX:
        return _cpu_percent_proc()
    raise RuntimeError("unsupported_platform")


# ── Memory ──

def _memory_usage_psutil() -> dict[str, int | float]:
    mem = _psutil.virtual_memory()
    return {
        "used_bytes": int(mem.used),
        "total_bytes": int(mem.total),
        "percent": _clamp_percent(mem.percent),
    }


def _read_meminfo_kib() -> dict[str, int]:
    data: dict[str, int] = {}
    with _PROC_MEMINFO.open("r", encoding="utf-8") as handle:
        for line in handle:
            key, _, rest = line.partition(":")
            if not key or not rest:
                continue
            parts = rest.strip().split()
            if not parts:
                continue
            try:
                data[key] = int(parts[0])
            except ValueError:
                continue
    return data


def _memory_usage_proc() -> dict[str, int | float]:
    meminfo = _read_meminfo_kib()
    total = int(meminfo.get("MemTotal") or 0) * 1024
    if total <= 0:
        raise RuntimeError("meminfo_unavailable")
    available_kib = meminfo.get("MemAvailable")
    if available_kib is None:
        available_kib = (
            meminfo.get("MemFree", 0)
            + meminfo.get("Buffers", 0)
            + meminfo.get("Cached", 0)
            + meminfo.get("SReclaimable", 0)
            - meminfo.get("Shmem", 0)
        )
    available = max(0, int(available_kib) * 1024)
    used = max(0, min(total, total - available))
    return {
        "used_bytes": used,
        "total_bytes": total,
        "percent": _clamp_percent((used / total) * 100.0),
    }


def _memory_usage() -> dict[str, int | float]:
    """Report memory usage."""
    if _HAS_PSUTIL:
        return _memory_usage_psutil()
    if _IS_LINUX:
        return _memory_usage_proc()
    raise RuntimeError("unsupported_platform")


# ── Disk (cross-platform via shutil) ──

def _disk_usage() -> dict[str, int | float]:
    usage = shutil.disk_usage("/")
    total = int(usage.total)
    if total <= 0:
        raise RuntimeError("disk_unavailable")
    used = int(usage.used)
    return {
        "used_bytes": used,
        "total_bytes": total,
        "percent": _clamp_percent((used / total) * 100.0),
    }


# ── Safe error reporting ──

def _safe_error(metric: str, exc: Exception) -> dict[str, str]:
    return {"metric": metric, "code": type(exc).__name__}


# ── Public entry point ──

def build_system_health_payload() -> dict[str, Any]:
    metrics: dict[str, Any] = {"cpu": None, "memory": None, "disk": None}
    errors: list[dict[str, str]] = []

    collectors = {
        "cpu": _cpu_percent,
        "memory": _memory_usage,
        "disk": _disk_usage,
    }
    for name, collect in collectors.items():
        try:
            value = collect()
            if name == "cpu":
                metrics[name] = {"percent": _clamp_percent(value)}
            else:
                metrics[name] = {
                    "used_bytes": max(0, int(value["used_bytes"])),
                    "total_bytes": max(0, int(value["total_bytes"])),
                    "percent": _clamp_percent(value["percent"]),
                }
        except Exception as exc:
            errors.append(_safe_error(name, exc))

    available = any(metrics[name] is not None for name in metrics)
    status = "ok" if available and not errors else "partial" if available else "unavailable"
    return {
        "status": status,
        "available": available,
        "checked_at": _checked_at(),
        "cpu": metrics["cpu"],
        "memory": metrics["memory"],
        "disk": metrics["disk"],
        "errors": errors,
    }
