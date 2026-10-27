# -*- coding: utf-8 -*-
"""同日多信号评分（方案 §5.5）。

禁止使用未来收益排序；权重与归一区间本身是自由参数，纳入敏感性测试。
"""

from __future__ import annotations

import numpy as np


def _clip01(value: float) -> float:
    if not np.isfinite(value):
        return 0.0
    return float(min(max(value, 0.0), 1.0))


def normalize(value: float, low: float, high: float, invert: bool = False) -> float:
    if high <= low or not np.isfinite(value):
        return 0.0
    score = _clip01((value - low) / (high - low))
    return 1.0 - score if invert else score


def shrink_score(shrink_ratio: float) -> float:
    """缩量越明显得分越高：0.50 → 1，0.90 → 0。"""
    return normalize(shrink_ratio, 0.50, 0.90, invert=True)


def restart_score(volume_ratio: float, ret1: float) -> float:
    vol_part = normalize(volume_ratio, 1.2, 4.0)
    ret_part = normalize(ret1, 0.005, 0.05)
    return 0.5 * vol_part + 0.5 * ret_part


def depth_score(depth: float) -> float:
    """回调深度越接近 8% 得分越高（配置中心 0.04–0.12）。"""
    return _clip01(1.0 - abs(depth - 0.08) / 0.04)


def stop_score(stop_pct: float) -> float:
    """止损空间 3% 得 1 分，8% 得 0 分。"""
    return normalize(stop_pct, 0.03, 0.08, invert=True)


def compute_score(
    shrink_ratio: float,
    volume_ratio: float,
    ret1: float,
    depth: float,
    stop_pct: float,
) -> float:
    return (
        0.3 * shrink_score(shrink_ratio)
        + 0.3 * restart_score(volume_ratio, ret1)
        + 0.2 * depth_score(depth)
        + 0.2 * stop_score(stop_pct)
    )
