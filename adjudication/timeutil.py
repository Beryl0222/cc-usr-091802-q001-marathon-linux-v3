"""时间工具：业务时间保留原始时区，内部统一换算 UTC 参与判定。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def parse_instant(value):
    """解析带时区的 ISO 8601 时间，返回 aware datetime；拒绝裸时间。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"无法解析时间: {value!r}") from exc
    else:
        raise ValueError(f"不支持的时间类型: {type(value)!r}")
    if dt.tzinfo is None:
        raise ValueError(f"时间必须携带时区: {value!r}")
    return dt


def to_utc(dt):
    """把任意 aware datetime 换算为 UTC。"""
    return dt.astimezone(UTC)


def format_utc(dt):
    """以 Z 结尾的 ISO 8601 输出 UTC 时间。"""
    return to_utc(dt).isoformat().replace("+00:00", "Z")


_DURATION_RE = re.compile(r"^(?:(\d+):)?([0-5]?\d):([0-5]?\d)(?:\.(\d{1,6}))?$")


def parse_duration(value):
    """解析 'H:MM:SS.fff'、'MM:SS' 或秒数为 timedelta。"""
    if isinstance(value, (int, float)):
        return timedelta(seconds=value)
    if isinstance(value, str):
        match = _DURATION_RE.match(value.strip())
        if not match:
            raise ValueError(f"无法解析时长: {value!r}")
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        micros = int((match.group(4) or "0").ljust(6, "0"))
        return timedelta(hours=hours, minutes=minutes, seconds=seconds,
                         microseconds=micros)
    raise ValueError(f"不支持的时长类型: {type(value)!r}")


def format_duration(td):
    """把 timedelta 输出为 'HH:MM:SS.mmm'，None 原样返回。"""
    if td is None:
        return None
    total = td.total_seconds()
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    seconds = total % 60
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:06.3f}"
