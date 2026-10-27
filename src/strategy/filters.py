# -*- coding: utf-8 -*-
"""时点股票池过滤（方案 §4.6，阶段 1 先只做主板）。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.sim.rules import Board, classify_board


@dataclass(frozen=True)
class PoolConfig:
    boards: tuple[Board, ...] = (Board.MAIN,)
    min_listed_days: int = 120
    min_avg_amount_20d: float = 50_000_000.0
    min_price: float = 2.0


REASON_PRIORITY = (
    "delisting",
    "board_not_allowed",
    "insufficient_listed_days",
    "is_st",
    "suspended",
    "low_liquidity",
    "low_price",
)


def filter_universe(df: pd.DataFrame, config: PoolConfig | None = None) -> pd.DataFrame:
    """对单只股票的历史行情逐日判定是否入池。

    输入需含：close、tradestatus、is_st、avg_amount_20d（可由指标层提供）。
    输出新增 in_pool(bool) 与 reason(str)。
    """
    config = config or PoolConfig()
    out = df.copy()
    n = len(out)
    if n == 0:
        out["in_pool"] = pd.Series(dtype=bool)
        out["reason"] = pd.Series(dtype=str)
        return out

    code = str(out["code"].iloc[0]) if "code" in out.columns else ""
    board_allowed = classify_board(code) in config.boards if code else False

    if "listed_days" in out.columns:
        listed_days = pd.to_numeric(out["listed_days"], errors="coerce").fillna(1).astype(int)
    else:
        listed_days = pd.Series(np.arange(1, n + 1), index=out.index)
    avg_amount = out["avg_amount_20d"] if "avg_amount_20d" in out.columns else out["amount"].rolling(
        20, min_periods=20
    ).mean()
    is_delisting = out["is_delisting"].fillna(False).astype(bool) if "is_delisting" in out.columns else False
    is_st = out["is_st"].fillna(False).astype(bool) if "is_st" in out.columns else False
    suspended = out["tradestatus"].fillna(0).astype(int) == 0 if "tradestatus" in out.columns else False

    conditions = [
        is_delisting,
        np.full(n, not board_allowed),
        listed_days < config.min_listed_days,
        is_st,
        suspended,
        avg_amount < config.min_avg_amount_20d,
        out["close"] < config.min_price,
    ]
    reasons = ["delisting", "board_not_allowed", "insufficient_listed_days", "is_st", "suspended",
               "low_liquidity", "low_price"]
    choice = np.select(
        [np.asarray(cond, dtype=bool) for cond in conditions],
        reasons,
        default="ok",
    )
    out["reason"] = choice
    out["in_pool"] = out["reason"] == "ok"
    out["listed_days"] = listed_days
    return out
