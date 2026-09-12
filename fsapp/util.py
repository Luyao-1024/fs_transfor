"""通用工具: 大小/速度格式化、名称校验等."""
from __future__ import annotations

import os

UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def validate_item_name(raw: str) -> tuple[str, str]:
    """校验用户输入的文件/目录名, 返回 (可用名称, 错误信息).

    只接受单个路径分量: '/' 与 '..' 会让新建/重命名落到面板目录之外,
    控制字符会污染终端和日志。错误信息为空表示名称可用。
    """
    name = (raw or "").strip()
    if not name:
        return "", "名称不能为空"
    if name in (".", ".."):
        return name, "名称不能是 . 或 .."
    if "/" in name or os.sep in name or (os.altsep and os.altsep in name):
        return name, "名称不能包含路径分隔符"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return name, "名称不能包含控制字符"
    return name, ""


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
