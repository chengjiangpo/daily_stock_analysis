# -*- coding: utf-8 -*-
"""主策略：趋势回调再启动（方案 §5.1，v1 冻结配置）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from src.sim.rules import quantize_price, to_decimal

from .base import BaseStrategy, Signal
from .diagnostics import DiagnosticSpec, build_diagnostics
from .indicators import (
    add_indicators,
    count_gap_downs,
    ensure_sorted,
    safe_div,
    swing_high,
    swing_low,
)

CONDITION_SPECS: tuple[DiagnosticSpec, ...] = (
    DiagnosticSpec("c_trend_ma20", "trend.close_above_ma20", failure_reason="close_below_ma20"),
    DiagnosticSpec("c_trend_ma20_ma60", "trend.ma20_above_ma60", failure_reason="ma20_below_ma60"),
    DiagnosticSpec(
        "c_trend_slope", "trend.ma60_slope", threshold_low=-0.02, failure_reason="ma60_slope_too_negative"
    ),
    DiagnosticSpec("c_trend_ret20", "trend.ret20", threshold_low=-0.05, failure_reason="ret20_too_low",
                   observed_column="ret20"),
    DiagnosticSpec(
        "c_trend_deviation", "trend.deviation", threshold_high=0.08, failure_reason="too_far_above_ma20",
        observed_column="obs_deviation"
    ),
    DiagnosticSpec("c_pullback_depth", "pullback.depth", threshold_low=0.04, threshold_high=0.12,
                   failure_reason="pullback_depth_out_of_range", observed_column="obs_depth"),
    DiagnosticSpec("c_pullback_low", "pullback.low_guard", threshold_low=0.85, failure_reason="low_broken",
                   observed_column="obs_low_guard"),
    DiagnosticSpec("c_pullback_ma60", "pullback.ma60_guard", threshold_low=0.98,
                   failure_reason="close_below_ma60_guard", observed_column="obs_ma60_guard"),
    DiagnosticSpec("c_volume_shrink", "volume.shrink", threshold_low=0.50, threshold_high=0.90,
                   failure_reason="volume_shrink_out_of_range", observed_column="obs_shrink"),
    DiagnosticSpec("c_volume_no_bear", "volume.no_long_bear", failure_reason="long_bear_volume_day"),
    DiagnosticSpec("c_volume_gaps", "volume.gap_count", threshold_low=0, threshold_high=1,
                   failure_reason="too_many_gap_downs", observed_column="obs_gap_count"),
    DiagnosticSpec("c_restart_close_up", "restart.close_up", failure_reason="close_not_up"),
    DiagnosticSpec("c_restart_breakout", "restart.breakout", failure_reason="not_breakout_local_high"),
    DiagnosticSpec("c_restart_volume", "restart.volume", threshold_low=1.2, failure_reason="volume_not_expanded",
                   observed_column="obs_volume_ratio"),
    DiagnosticSpec("c_restart_close_pos", "restart.close_position", threshold_low=0.60,
                   failure_reason="close_not_in_upper_range", observed_column="obs_close_pos"),
    DiagnosticSpec("c_restart_ret", "restart.return_band", threshold_low=0.005, threshold_high=0.05,
                   failure_reason="daily_return_out_of_band", observed_column="obs_ret1"),
    DiagnosticSpec("c_restart_not_one_word", "restart.not_one_word", failure_reason="one_word_limit_up"),
    DiagnosticSpec("c_stop_valid", "entry.stop_distance", failure_reason="invalid_stop_distance"),
)


class TrendPullbackRestart(BaseStrategy):
    name = "trend_pullback_restart"

    def __init__(self, params: dict[str, Any]) -> None:
        super().__init__(params)
        self.version = params.get("version", "v1")
        self.trend = params["trend"]
        self.pullback = params["pullback"]
        self.volume = params["volume"]
        self.restart = params["restart"]
        self.entry = params.get("entry", {}) or {}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = add_indicators(
            ensure_sorted(df),
            ma_fast=int(self.trend["ma_fast"]),
            ma_slow=int(self.trend["ma_slow"]),
            slope_window=int(self.trend["ma_slope_window"]),
        )
        return out

    def generate_signals(self, prepared: pd.DataFrame) -> pd.DataFrame:
        out = prepared.copy()
        if "code" not in out.columns:
            raise ValueError("行情 DataFrame 缺少 code 列")
        close = out["adj_close"]
        raw_close = out["close"]
        open_ = out["adj_open"]
        high = out["adj_high"]
        low = out["adj_low"]
        raw_open, raw_high, raw_low = out["open"], out["high"], out["low"]
        volume = out["volume"]

        ma_fast = out["ma_fast"]
        ma_slow = out["ma_slow"]
        ma60 = out["ma60"]

        # A. 趋势过滤
        out["c_trend_ma20"] = close > ma_fast
        out["c_trend_ma20_ma60"] = ma_fast > ma_slow
        out["c_trend_slope"] = out["ma_slope"] >= float(self.trend["ma_slope_min"])
        out["c_trend_ret20"] = out["ret20"] >= float(self.trend["ret20_min"])
        out["obs_deviation"] = safe_div(close, ma_fast) - 1.0
        out["c_trend_deviation"] = out["obs_deviation"] <= float(self.trend["deviation_max"])

        # B. 回调识别
        window = int(self.pullback["swing_window"])
        out["swing_high"] = swing_high(high, window)
        out["swing_low"] = swing_low(low, window)
        out["obs_depth"] = safe_div(out["swing_high"] - close, out["swing_high"])
        out["c_pullback_depth"] = out["obs_depth"].between(
            float(self.pullback["depth_min"]), float(self.pullback["depth_max"]), inclusive="both"
        )
        out["obs_low_guard"] = safe_div(out["swing_low"], out["swing_high"])
        out["c_pullback_low"] = out["obs_low_guard"] >= float(self.pullback["low_guard"])
        out["obs_ma60_guard"] = safe_div(close, ma60)
        out["c_pullback_ma60"] = out["obs_ma60_guard"] >= float(self.pullback["ma60_guard"])

        # C. 回调质量
        shrink_window = int(self.volume["shrink_window"])
        base_window = int(self.volume["base_window"])
        recent = volume.shift(1).rolling(shrink_window, min_periods=shrink_window).mean()
        base = volume.shift(shrink_window).rolling(base_window, min_periods=base_window).mean()
        out["obs_shrink"] = safe_div(recent, base)
        out["c_volume_shrink"] = out["obs_shrink"].between(
            float(self.volume["shrink_min"]), float(self.volume["shrink_max"]), inclusive="both"
        )
        long_bear = (safe_div(close, open_) - 1.0 <= -0.06) & (volume > 1.5 * out["vol_ma20"])
        out["c_volume_no_bear"] = long_bear.shift(1).rolling(window, min_periods=window).max().fillna(1) < 0.5
        out["obs_gap_count"] = count_gap_downs(high, low, window)
        out["c_volume_gaps"] = out["obs_gap_count"] < 2

        # D. 再启动
        breakout_window = int(self.restart["breakout_window"])
        local_high = high.shift(1).rolling(breakout_window, min_periods=breakout_window).max()
        out["c_restart_close_up"] = close > close.shift(1)
        out["c_restart_breakout"] = close > local_high
        out["obs_volume_ratio"] = safe_div(volume, base)
        out["c_restart_volume"] = volume > float(self.restart["vol_mult"]) * base
        out["obs_close_pos"] = out["close_pos"]
        out["c_restart_close_pos"] = out["close_pos"] >= float(self.restart["close_pos_min"])
        out["obs_ret1"] = safe_div(close, close.shift(1)) - 1.0
        out["c_restart_ret"] = out["obs_ret1"].between(
            float(self.restart["ret_min"]), float(self.restart["ret_max"]), inclusive="both"
        )
        one_word_up = (
            (raw_open == raw_high)
            & (raw_high == raw_low)
            & (raw_low == raw_close)
            & out.get("limit_up", pd.Series(np.nan, index=out.index)).notna()
            & (raw_close >= out.get("limit_up", pd.Series(np.nan, index=out.index)))
        )
        out["c_restart_not_one_word"] = ~one_word_up

        # E. 止损距离（复权空间计算，换算回真实价）
        stop_buffer = float(self.exit_params.get("stop_buffer", 0.995))
        stop_max_pct = float(self.exit_params.get("stop_max_pct", 0.08))
        stop_min_pct = float(self.exit_params.get("stop_min_pct", 0.03))
        stop_adj = np.minimum(
            np.maximum(out["swing_low"] * stop_buffer, close * (1.0 - stop_max_pct)),
            close * (1.0 - stop_min_pct),
        )
        out["stop_price_adj"] = stop_adj
        factor = out["adj_factor"].replace(0, np.nan)
        out["stop_price_real"] = stop_adj / factor
        out["entry_ref"] = raw_close
        out["r_value"] = raw_close - out["stop_price_real"]
        out["take_profit_real"] = raw_close + 2.0 * out["r_value"]
        out["obs_stop_pct"] = safe_div(out["r_value"], raw_close)
        out["c_stop_valid"] = (out["r_value"] > 0) & out["obs_stop_pct"].between(
            stop_min_pct * 0.99, stop_max_pct * 1.01, inclusive="both"
        )

        condition_columns = [
            "c_trend_ma20",
            "c_trend_ma20_ma60",
            "c_trend_slope",
            "c_trend_ret20",
            "c_trend_deviation",
            "c_pullback_depth",
            "c_pullback_low",
            "c_pullback_ma60",
            "c_volume_shrink",
            "c_volume_no_bear",
            "c_volume_gaps",
            "c_restart_close_up",
            "c_restart_breakout",
            "c_restart_volume",
            "c_restart_close_pos",
            "c_restart_ret",
            "c_restart_not_one_word",
            "c_stop_valid",
        ]
        for column in condition_columns:
            out[column] = out[column].fillna(False).astype(bool)
        out["entry"] = out[condition_columns].all(axis=1)

        # 评分
        shrink_component = np.clip((0.90 - out["obs_shrink"]) / 0.40, 0.0, 1.0)
        vol_component = np.clip((out["obs_volume_ratio"] - 1.2) / 2.8, 0.0, 1.0)
        ret_component = np.clip((out["obs_ret1"] - 0.005) / 0.045, 0.0, 1.0)
        depth_component = np.clip(1.0 - (out["obs_depth"] - 0.08).abs() / 0.04, 0.0, 1.0)
        stop_component = np.clip((0.08 - out["obs_stop_pct"]) / 0.05, 0.0, 1.0)
        score = (
            0.3 * shrink_component
            + 0.3 * (0.5 * vol_component + 0.5 * ret_component)
            + 0.2 * depth_component
            + 0.2 * stop_component
        )
        out["score"] = np.nan_to_num(score.to_numpy(dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
        return out

    def evaluate_row(self, row: pd.Series, code: str, trade_date: date, index: int) -> Signal:
        diagnostics = build_diagnostics(row, CONDITION_SPECS)
        passed = bool(row.get("entry", False))
        stop_real = _decimal_or_none(row.get("stop_price_real"))
        stop_adj = _decimal_or_none(row.get("stop_price_adj"))
        entry_ref = _decimal_or_none(row.get("close")) or Decimal("0")
        take_profit = quantize_price(entry_ref + 2 * (entry_ref - stop_real)) if stop_real is not None else None
        return Signal(
            code=code,
            signal_date=trade_date,
            passed=passed,
            score=float(row.get("score", 0.0) or 0.0),
            entry_ref=entry_ref,
            stop_price_real=stop_real,
            take_profit_real=take_profit,
            stop_price_adj=stop_adj,
            rule_version=self.version,
            diagnostics=diagnostics,
        )


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return quantize_price(to_decimal(number))
