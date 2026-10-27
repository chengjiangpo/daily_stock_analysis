# -*- coding: utf-8 -*-
"""基准构造：B2 同市值同波动同行业匹配组合、B3 动量（方案 §5.3）。

B2 必须可复现：所有随机抽样使用显式 seed。
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd


def add_buckets(
    snapshot: pd.DataFrame,
    size_col: str = "market_cap",
    vol_col: str = "volatility_20d",
    q: int = 5,
) -> pd.DataFrame:
    """按当日横截面分位分桶；缺失值单独成桶 -1。"""
    out = snapshot.copy()
    for column, name in ((size_col, "size_bucket"), (vol_col, "vol_bucket")):
        if column not in out.columns:
            out[name] = 0
            continue
        values = pd.to_numeric(out[column], errors="coerce")
        try:
            buckets = pd.qcut(values, q=q, labels=False, duplicates="drop")
        except ValueError:
            buckets = pd.Series(0, index=out.index)
        out[name] = buckets.fillna(-1).astype(int)
    return out


def sample_baskets(
    snapshot: pd.DataFrame,
    size_bucket: int,
    vol_bucket: int,
    industry: Optional[str],
    exclude: Sequence[str] = (),
    n: int = 5,
    sims: int = 200,
    seed: int = 42,
) -> list[list[str]]:
    """从同市值/同波动/同行业分组抽 n 只等权；不足时逐级放宽并记录。"""
    exclude_set = set(exclude)
    pool = snapshot[~snapshot.index.isin(exclude_set)]
    if pool.empty:
        return []

    strict = pool
    if "size_bucket" in pool.columns:
        strict = strict[strict["size_bucket"] == size_bucket]
    if "vol_bucket" in pool.columns:
        strict = strict[strict["vol_bucket"] == vol_bucket]
    if industry and "industry" in pool.columns:
        strict = strict[strict["industry"] == industry]

    candidates = strict
    if len(candidates) < n:
        candidates = pool
        for column, value in (("size_bucket", size_bucket), ("vol_bucket", vol_bucket)):
            if column in candidates.columns:
                subset = candidates[candidates[column] == value]
                if len(subset) >= n:
                    candidates = subset
                    break
    if len(candidates) == 0:
        return []

    rng = np.random.default_rng(seed)
    codes = np.asarray(list(candidates.index))
    baskets: list[list[str]] = []
    for _ in range(sims):
        k = min(n, len(codes))
        picked = rng.choice(codes, size=k, replace=False)
        baskets.append([str(code) for code in picked])
    return baskets


def forward_open_return(df: pd.DataFrame, entry_date: date, exit_date: date) -> Optional[float]:
    """按开盘价买入、开盘价卖出计算单票收益；缺失日期返回 None。"""
    if df is None or df.empty:
        return None
    entry_ts = pd.Timestamp(entry_date)
    exit_ts = pd.Timestamp(exit_date)
    entry_pos = int(df.index.searchsorted(entry_ts, side="left"))
    exit_pos = int(df.index.searchsorted(exit_ts, side="left"))
    if entry_pos >= len(df) or exit_pos >= len(df):
        return None
    entry_open = float(df.iloc[entry_pos]["open"])
    exit_open = float(df.iloc[exit_pos]["open"])
    if entry_open <= 0 or exit_open <= 0:
        return None
    return exit_open / entry_open - 1.0


def basket_forward_return(
    data: dict[str, pd.DataFrame],
    baskets: Iterable[Sequence[str]],
    entry_date: date,
    exit_date: date,
) -> dict:
    """返回匹配组合收益的中位数、均值、样本数与有效模拟次数。"""
    sim_returns = []
    for basket in baskets:
        returns = []
        for code in basket:
            value = forward_open_return(data.get(code), entry_date, exit_date)
            if value is not None:
                returns.append(value)
        if returns:
            sim_returns.append(float(np.mean(returns)))
    if not sim_returns:
        return {"median": None, "mean": None, "sims": 0, "basket_size": 0}
    return {
        "median": float(np.median(sim_returns)),
        "mean": float(np.mean(sim_returns)),
        "sims": len(sim_returns),
        "basket_size": int(np.mean([len(b) for b in baskets])) if baskets else 0,
    }
