# -*- coding: utf-8 -*-
"""对照策略：箱体突破（方案 §5.2）。撮合、退出与主策略完全一致。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from src.sim.rules import quantize_price, to_decimal

from .base import BaseStrategy, Signal
from .diagnostics import DiagnosticSpec, build_diagnostics
from .indicators import add_indicators, ensure_sorted, safe_div

CONDITION_SPECS: tuple[DiagnosticSpec, ...] = (
    DiagnosticSpec("c_box_amplitude", "box.amplitude", threshold_high=0.10,
                   failure_reason="box_too_wide", observed_column="obs_box_amplitude"),
    DiagnosticSpec("c_box_drift", "box.no_drift", threshold_high=0.08, failure_reason="box_drift_too_large",
                   observed_column="obs_box_drift"),
    DiagnosticSpec("c_breakout", "breakout.close_above_box", failure_reason="not_breakout"),
    DiagnosticSpec("c_breakout_volume", "breakout.volume", threshold_low=1.5, threshold_high=5.0,
                   failure_reason="breakout_volume_out_of_range", observed_column="obs_volume_ratio"),
    DiagnosticSpec("c_breakout_close_pos", "breakout.close_position", threshold_low=0.7,
                   failure_reason="close_not_in_upper_range", observed_column="obs_close_pos"),
    DiagnosticSpec("c_breakout_ret", "breakout.return_band", threshold_low=0.01, threshold_high=0.07,
                   failure_reason="daily_return_out_of_band", observed_column="obs_ret1"),
    DiagnosticSpec("c_trend_ma20", "trend.close_above_ma20", failure_reason="close_below_ma20"),
    DiagnosticSpec("c_trend_ma60", "trend.ma20_guard", threshold_low=0.98,
                   failure_reason="ma20_below_ma60_guard", observed_column="obs_ma20_guard"),
    DiagnosticSpec("c_not_one_word", "breakout.not_one_word", failure_reason="one_word_limit_up"),
    DiagnosticSpec("c_stop_valid", "entry.stop_distance", failure_reason="invalid_stop_distance"),
)


class BoxBreakout(BaseStrategy):
    name = "box_breakout"

    def __init__(self, params: dict[str, Any]) -> None:
        super().__init__(params)
        self.version = params.get("version", "v1")
        self.box = params["box"]
        self.breakout = params["breakout"]
        self.trend = params["trend"]

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return add_indicators(
            ensure_sorted(df),
            ma_fast=int(self.trend["ma_fast"]),
            ma_slow=int(self.trend["ma_slow"]),
            slope_window=10,
        )

    def generate_signals(self, prepared: pd.DataFrame) -> pd.DataFrame:
        out = prepared.copy()
        close = out["adj_close"]
        high, low = out["adj_high"], out["adj_low"]
        raw_open, raw_high, raw_low, raw_close = out["open"], out["high"], out["low"], out["close"]
        volume = out["volume"]
        window = int(self.box["window"])

        out["box_high"] = high.shift(1).rolling(window, min_periods=window).max()
        out["box_low"] = low.shift(1).rolling(window, min_periods=window).min()
        out["obs_box_amplitude"] = safe_div(out["box_high"] - out["box_low"], out["box_low"])
        out["c_box_amplitude"] = out["obs_box_amplitude"] <= float(self.box["amplitude_max"])
        out["obs_box_drift"] = safe_div(close.shift(1), close.shift(window)) - 1.0
        out["c_box_drift"] = out["obs_box_drift"].abs() <= float(self.box["drift_max"])

        out["c_breakout"] = close > out["box_high"]
        base_vol = volume.shift(1).rolling(window, min_periods=window).mean()
        out["obs_volume_ratio"] = safe_div(volume, base_vol)
        out["c_breakout_volume"] = out["obs_volume_ratio"].between(
            float(self.breakout["vol_mult_min"]), float(self.breakout["vol_mult_max"]), inclusive="both"
        )
        out["obs_close_pos"] = out["close_pos"]
        out["c_breakout_close_pos"] = out["close_pos"] >= float(self.breakout["close_pos_min"])
        out["obs_ret1"] = safe_div(close, close.shift(1)) - 1.0
        out["c_breakout_ret"] = out["obs_ret1"].between(
            float(self.breakout["ret_min"]), float(self.breakout["ret_max"]), inclusive="both"
        )

        out["obs_ma20_guard"] = safe_div(out["ma20"], out["ma60"])
        out["c_trend_ma20"] = close > out["ma20"]
        out["c_trend_ma60"] = out["obs_ma20_guard"] >= float(self.trend["ma_slow_guard"])

        stop_buffer = float(self.exit_params.get("stop_buffer", 0.995))
        stop_max_pct = float(self.exit_params.get("stop_max_pct", 0.08))
        stop_min_pct = float(self.exit_params.get("stop_min_pct", 0.03))
        stop_adj = np.minimum(
            np.maximum(out["box_low"] * stop_buffer, close * (1.0 - stop_max_pct)),
            close * (1.0 - stop_min_pct),
        )
        out["stop_price_adj"] = stop_adj
        factor = out["adj_factor"].replace(0, np.nan)
        out["stop_price_real"] = stop_adj / factor
        out["entry_ref"] = raw_close
        out["r_value"] = raw_close - out["stop_price_real"]
        out["obs_stop_pct"] = safe_div(out["r_value"], raw_close)
        out["c_stop_valid"] = (out["r_value"] > 0) & out["obs_stop_pct"].between(
            stop_min_pct * 0.99, stop_max_pct * 1.01, inclusive="both"
        )

        one_word_up = (
            (raw_open == raw_high)
            & (raw_high == raw_low)
            & (raw_low == raw_close)
            & out.get("limit_up", pd.Series(np.nan, index=out.index)).notna()
            & (raw_close >= out.get("limit_up", pd.Series(np.nan, index=out.index)))
        )
        out["c_not_one_word"] = ~one_word_up

        condition_columns = [spec.column for spec in CONDITION_SPECS] + ["c_not_one_word"]
        for column in condition_columns:
            out[column] = out[column].fillna(False).astype(bool)
        out["entry"] = out[condition_columns].all(axis=1)

        vol_component = np.clip((out["obs_volume_ratio"] - 1.5) / 2.0, 0.0, 1.0)
        pos_component = np.clip((out["obs_close_pos"] - 0.7) / 0.3, 0.0, 1.0)
        out["score"] = np.nan_to_num(
            (0.6 * vol_component + 0.4 * pos_component).to_numpy(dtype=float), nan=0.0
        )
        return out

    def evaluate_row(self, row: pd.Series, code: str, trade_date: date, index: int) -> Signal:
        diagnostics = build_diagnostics(row, CONDITION_SPECS)
        stop_real = _decimal_or_none(row.get("stop_price_real"))
        entry_ref = _decimal_or_none(row.get("close")) or Decimal("0")
        take_profit = entry_ref + 2 * (entry_ref - stop_real) if stop_real is not None else None
        return Signal(
            code=code,
            signal_date=trade_date,
            passed=bool(row.get("entry", False)),
            score=float(row.get("score", 0.0) or 0.0),
            entry_ref=entry_ref,
            stop_price_real=stop_real,
            take_profit_real=take_profit,
            stop_price_adj=_decimal_or_none(row.get("stop_price_adj")),
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
