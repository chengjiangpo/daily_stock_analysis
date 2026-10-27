# -*- coding: utf-8 -*-
"""策略基类与统一退出规则（方案 §5.1-F）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Optional

import pandas as pd

from .diagnostics import ConditionResult


@dataclass(frozen=True)
class Signal:
    code: str
    signal_date: date
    passed: bool
    score: float
    entry_ref: Decimal
    stop_price_real: Optional[Decimal]
    take_profit_real: Optional[Decimal]
    stop_price_adj: Optional[Decimal]
    rule_version: str
    diagnostics: tuple[ConditionResult, ...] = ()

    def diagnostics_frame(self) -> list[dict]:
        rows = []
        for item in self.diagnostics:
            row = item.to_dict()
            row.update({"code": self.code, "signal_date": self.signal_date, "rule_version": self.rule_version})
            rows.append(row)
        return rows


class BaseStrategy(ABC):
    name: str = "base"
    version: str = "v1"

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params
        self.exit_params = params.get("exit", {}) or {}
        self.entry_params = params.get("entry", {}) or {}

    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算指标与条件列，返回新 DataFrame。"""

    @abstractmethod
    def evaluate_row(self, row: pd.Series, code: str, trade_date: date, index: int) -> Signal:
        """对单行生成信号与诊断。"""

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """向量化生成信号列。"""

    def prepare_and_signal(self, df: pd.DataFrame) -> pd.DataFrame:
        prepared = self.prepare(df)
        signals = self.generate_signals(prepared)
        return signals

    # ---------- 退出 ----------

    def exit_reason(self, position: Any, row: pd.Series, trade_date: date) -> Optional[str]:
        """按方案优先级返回退出原因；None 表示继续持有。

        所有判断基于 T 日收盘，T+1 开盘执行。
        """
        close_real = _safe_float(row.get("close"))
        adj_close = _safe_float(row.get("adj_close"))
        if close_real is None or adj_close is None:
            return None

        stop_price = _to_decimal(getattr(position, "stop_price", None))
        if stop_price is not None and close_real <= float(stop_price):
            return "hard_stop"

        take_profit = _to_decimal(getattr(position, "take_profit_price", None))
        if take_profit is not None and close_real >= float(take_profit):
            return "take_profit"

        ma_column = f"ma{int(self.exit_params.get('trend_exit_ma', 10))}"
        ma_value = _safe_float(row.get(ma_column))
        if ma_value is not None and adj_close < ma_value:
            return "trend_break"

        max_hold_days = int(self.exit_params.get("max_hold_days", 8))
        if int(getattr(position, "hold_days", 0)) >= max_hold_days:
            return "time_exit"

        return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return number


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
