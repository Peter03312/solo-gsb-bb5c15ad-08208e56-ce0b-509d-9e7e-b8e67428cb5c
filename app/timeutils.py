"""UTC 时间解析与半开 [start, end) 时间窗工具。"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import List, Tuple


class TimeError(ValueError):
    """时间字符串非法。"""


def parse_utc(value: object, *, field: str = "时刻") -> datetime:
    """解析 ISO 8601 时间，必须带时区信息（内部统一转 UTC，朴素时间拒绝）。"""
    if not isinstance(value, str) or not value.strip():
        raise TimeError(f"{field}必须是非空 ISO 8601 字符串，收到: {value!r}")
    text = value.strip()
    # 兼容结尾 Z
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimeError(f"{field}不是合法的 ISO 8601 时间: {value!r}") from exc
    if dt.tzinfo is None:
        raise TimeError(f"{field}必须携带时区偏移（如 Z 或 +00:00）: {value!r}")
    return dt.astimezone(timezone.utc)


def validate_window(start: datetime, end: datetime) -> None:
    if not (math.isfinite(start.timestamp()) and math.isfinite(end.timestamp())):
        raise TimeError("生效时刻必须为有限值")
    if start >= end:
        raise TimeError(f"生效时段必须左闭右开且长度为正: [{start.isoformat()}, {end.isoformat()})")


def merge_windows(windows: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """合并重叠或端点相接的 UTC epoch 秒区间。"""
    ordered = sorted(windows)
    merged: List[Tuple[float, float]] = []
    for a, b in ordered:
        if not merged or a > merged[-1][1]:
            merged.append((a, b))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
    return merged


def clip_intervals_halfopen(
    intervals: List[Tuple[float, float]],
    windows: List[Tuple[float, float]],
) -> List[Tuple[float, float]]:
    """把覆盖区间与生效时段做半开区间求交。

    intervals: 左闭右开覆盖区间；windows: 左闭右开生效区间。
    交集仍为左闭右开；end 恰好等于 window end 的接触不产生时长。
    """
    out: List[Tuple[float, float]] = []
    for a, b in intervals:
        for wa, wb in windows:
            lo, hi = max(a, wa), min(b, wb)
            if hi > lo:
                out.append((lo, hi))
    return out


def subtract_intervals_halfopen(
    intervals: List[Tuple[float, float]],
    covers: List[Tuple[float, float]],
    eps: float = 1e-9,
) -> List[Tuple[float, float]]:
    """从一组左闭右开区间 intervals 中减去 covers 覆盖的部分（扫描线）。

    两端均为左闭右开，仅端点接触不产生覆盖（[a,b) 减去 [b,c) 仍为整段）；
    输出合并后的左闭右开正时长区间。intervals/covers 内部可重叠，先各自合并。
    """
    base = merge_windows(intervals)
    cuts = merge_windows(covers)
    out: List[Tuple[float, float]] = []
    for a, b in base:
        cur = a
        for c, d in cuts:
            if d <= cur + eps or c >= b - eps:
                continue  # 与当前剩余段不相交（含仅端点接触）
            if c > cur:
                out.append((cur, min(b, c)))
            cur = max(cur, d)
            if cur >= b - eps:
                break
        if cur < b - eps:
            out.append((cur, b))
    return [(s, e) for s, e in merge_windows(out) if e - s > eps]
