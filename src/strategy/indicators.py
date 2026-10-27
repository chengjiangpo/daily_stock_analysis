# -*- coding: utf-8 -*-
"""技术指标（信号一律在复权序列上计算）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

EPS = 1e-12


def safe_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """分母为 0/NaN 时返回 NaN，不抛 ZeroDivisionError。"""
    return numerator / denominator.replace(0, np.nan)


def ensure_sorted(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("行情 DataFrame 必须使用 DatetimeIndex")
    if not df.index.is_monotonic_increasing:
        df = df.sort_index()
    if df.index.has_duplicates:
        raise ValueError("行情 DataFrame 存在重复交易日")
    return df


def add_price_columns(df: pd.DataFrame) -> pd.DataFrame:
    """补充复权价列。adj_factor 为 后复权价 / 不复权价。"""
    out = df.copy()
    if "adj_factor" not in out.columns:
        out["adj_factor"] = 1.0
    out["adj_factor"] = pd.to_numeric(out["adj_factor"], errors="coerce").fillna(1.0)
    for column in ("open", "high", "low", "close"):
        out[f"adj_{column}"] = out[column].astype(float) * out["adj_factor"]
    return out


def add_indicators(
    df: pd.DataFrame,
    ma_fast: int = 20,
    ma_slow: int = 60,
    slope_window: int = 10,
) -> pd.DataFrame:
    out = add_price_columns(df)
    close = out["adj_close"]

    out["ma_fast"] = close.rolling(ma_fast, min_periods=ma_fast).mean()
    out["ma_slow"] = close.rolling(ma_slow, min_periods=ma_slow).mean()
    out["ma10"] = close.rolling(10, min_periods=10).mean()
    out["ma20"] = close.rolling(20, min_periods=20).mean()
    out["ma60"] = close.rolling(60, min_periods=60).mean()
    out["ma_slope"] = safe_div(
        out["ma_slow"] - out["ma_slow"].shift(slope_window),
        out["ma_slow"].shift(slope_window),
    )
    out["ret20"] = safe_div(close, close.shift(20)) - 1.0
    out["ret1"] = safe_div(close, close.shift(1)) - 1.0
    out["vol_ma20"] = out["volume"].rolling(20, min_periods=20).mean()
    out["vol_recent3"] = out["volume"].rolling(3, min_periods=3).mean()
    out["vol_base10"] = out["volume"].shift(3).rolling(10, min_periods=10).mean()
    out["shrink_ratio"] = safe_div(out["vol_recent3"], out["vol_base10"])
    out["close_pos"] = safe_div(out["adj_close"] - out["adj_low"], out["adj_high"] - out["adj_low"])
    out.loc[(out["adj_high"] - out["adj_low"]).abs() < EPS, "close_pos"] = np.nan
    out["avg_amount_20d"] = out["amount"].rolling(20, min_periods=20).mean()
    out["volatility_20d"] = out["adj_close"].pct_change().rolling(20, min_periods=20).std() * np.sqrt(252.0)
    return out


def swing_high(high: pd.Series, window: int) -> pd.Series:
    """T 日之前的 window 日最高价（不含 T）。"""
    return high.shift(1).rolling(window, min_periods=window).max()


def swing_low(low: pd.Series, window: int) -> pd.Series:
    return low.shift(1).rolling(window, min_periods=window).min()


def count_gap_downs(high: pd.Series, low: pd.Series, window: int) -> pd.Series:
    """统计 T 日之前 window 日内的向下跳空次数（high < 前一日 low）。"""
    gap_down = high < low.shift(1)
    return gap_down.shift(1).rolling(window, min_periods=window).sum()
