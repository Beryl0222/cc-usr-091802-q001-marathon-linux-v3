"""业务时间工具。

现场设备上报的时间保留其原始时区字符串（如 ``2026-09-20T07:30:00+08:00``），
判定一律使用换算后的 UTC，避免不同时区设备混在一起排序出错。
"""

from datetime import datetime, timezone


def parse_iso(value):
    """把带时区偏移的 ISO-8601 字符串解析为 UTC :class:`datetime`。

    拒绝朴素时间：没有时区就无法保证“保留原始时区”，由调用方按 400 处理。
    """
    if not isinstance(value, str):
        raise ValueError("时间必须是 ISO-8601 字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"时间缺少时区偏移: {value!r}")
    return dt.astimezone(timezone.utc)


def utc_iso(dt):
    """UTC datetime 输出为规范字符串。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def epoch(dt):
    """UTC 秒（浮点），用于排序与差值。"""
    return dt.astimezone(timezone.utc).timestamp()
