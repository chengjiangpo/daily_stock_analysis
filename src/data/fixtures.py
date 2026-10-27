# -*- coding: utf-8 -*-
"""可复现的合成行情（离线冒烟测试与规则回归使用）。"""

from __future__ import annotations

from datetime import date
from typing import Dict

import numpy as np
import pandas as pd

FLAT_DAYS = 30
UP_DAYS = 60
DROP_DAYS = 5
BASE_DAYS = 4


def build_trend_pullback_frame(
    code: str = "600000",
    continuation_days: int = 31,
    volume: float = 8_000_000.0,
    mutate_next_open: bool = False,
    suspend_next_day: bool = False,
    start: str = "2023-01-02",
) -> tuple[pd.DataFrame, pd.Timestamp]:
    """30 日横盘 + 60 日上涨 + 5 日回调 + 4 日企稳 + 1 日再启动 + 延续。"""
    days = FLAT_DAYS + UP_DAYS + DROP_DAYS + BASE_DAYS + 1 + continuation_days
    index = pd.bdate_range(start, periods=days)
    closes = [10.0] * FLAT_DAYS + list(np.linspace(10.0, 24.0, UP_DAYS))
    peak = closes[-1]
    for i in range(1, DROP_DAYS + 1):
        closes.append(peak * (1 - 0.08 * i / DROP_DAYS))
    bottom = closes[-1]
    for i in range(1, BASE_DAYS + 1):
        closes.append(bottom * (1 + 0.003 * i))
    closes.append(closes[-1] * 1.025)
    for _ in range(continuation_days):
        closes.append(closes[-1] * 1.004)
    closes = np.array(closes)

    opens = closes * 0.998
    highs = closes * 1.02
    lows = closes * 0.985
    restart = FLAT_DAYS + UP_DAYS + DROP_DAYS + BASE_DAYS
    highs[restart] = closes[restart] * 1.003
    lows[restart] = closes[restart] * 0.986
    opens[restart] = closes[restart] * 0.995

    volumes = np.full(days, volume)
    volumes[FLAT_DAYS + UP_DAYS:restart] *= 0.4
    volumes[restart] *= 0.85

    if mutate_next_open:
        nxt = restart + 1
        limit_up = round(closes[restart] * 1.1, 2)
        opens[nxt] = highs[nxt] = lows[nxt] = closes[nxt] = limit_up
        volumes[nxt] = volume * 0.1
    if suspend_next_day:
        nxt = restart + 1
        opens[nxt] = highs[nxt] = lows[nxt] = closes[nxt] = closes[restart]

    frame = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "amount": closes * volumes,
            "pre_close": np.r_[closes[0], closes[:-1]],
            "tradestatus": 1,
            "is_st": False,
            "adj_factor": 1.0,
            "industry": "测试",
            "listed_days": np.arange(days) + 300,
            "code": code,
        },
        index=index,
    )
    if suspend_next_day:
        frame.iloc[restart + 1, frame.columns.get_loc("tradestatus")] = 0
    return frame, index[restart]


def build_fixture_frames() -> Dict[str, pd.DataFrame]:
    frames: Dict[str, pd.DataFrame] = {}
    for offset, code in enumerate(("600000", "600001", "600002")):
        frame, _ = build_trend_pullback_frame(
            code=code,
            continuation_days=31 + offset,
            start=f"2023-01-{2 + offset:02d}",
        )
        frames[code] = frame
    return frames


def fixture_signal_date(code: str = "600000") -> date:
    frame, signal_day = build_trend_pullback_frame(code=code)
    return signal_day.date()
