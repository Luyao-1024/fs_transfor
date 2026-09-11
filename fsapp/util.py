"""通用工具: 大小/速度格式化等."""
from __future__ import annotations

UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def fmt_size(n) -> str:
    """字节数 → 人类可读: 1.5 MB"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if n < 0:
        return "—"
    for unit in UNITS:
        if n < 1024 or unit == UNITS[-1]:
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_speed(bps) -> str:
    """字节/秒 → 人类可读: 1.5 MB/s"""
    return fmt_size(bps) + "/s"
