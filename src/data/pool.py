# -*- coding: utf-8 -*-
"""时点股票池与横截面快照（方案 §4.6 / §5.3 B2）。"""

from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from src.strategy.filters import PoolConfig, filter_universe


def prepare_pool_frame(frame: pd.DataFrame, config: Optional[PoolConfig] = None) -> pd.DataFrame:
    """补齐池判定所需字段并返回 in_pool/reason。"""
    out = frame.copy()
    if "avg_amount_20d" not in out.columns:
        out["avg_amount_20d"] = out["amount"].rolling(20, min_periods=20).mean()
    if "volatility_20d" not in out.columns and "adj_close" in out.columns:
        out["volatility_20d"] = out["adj_close"].pct_change().rolling(20, min_periods=20).std() * (252 ** 0.5)
    if "industry" not in out.columns:
        out["industry"] = "UNKNOWN"
    return filter_universe(out, config)


def build_pool_table(frames: Dict[str, pd.DataFrame], config: Optional[PoolConfig] = None) -> pd.DataFrame:
    rows = []
    for code, frame in frames.items():
        prepared = prepare_pool_frame(frame, config)
        for timestamp, row in prepared.iterrows():
            rows.append(
                {
                    "trade_date": timestamp.date(),
                    "code": code,
                    "in_pool": bool(row.get("in_pool", False)),
                    "reason": str(row.get("reason", "")),
                }
            )
    return pd.DataFrame(rows)


def pool_snapshot(
    frames: Dict[str, pd.DataFrame],
    timestamp: pd.Timestamp,
    config: Optional[PoolConfig] = None,
    size_column: str = "avg_amount_20d",
) -> pd.DataFrame:
    """返回某日入池股票的横截面快照。

    注意：没有市值数据时以 20 日均额作为 size 代理，报告必须声明该局限。
    """
    rows = []
    for code, frame in frames.items():
        if timestamp not in frame.index:
            continue
        row = frame.loc[timestamp]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        if "in_pool" in row.index:
            in_pool = bool(row["in_pool"])
        else:
            prepared = prepare_pool_frame(frame.loc[[timestamp]], config)
            in_pool = bool(prepared.iloc[0]["in_pool"])
        if not in_pool:
            continue
        rows.append(
            {
                "code": code,
                "size": float(row.get(size_column, 0.0) or 0.0),
                "volatility": float(row.get("volatility_20d", 0.0) or 0.0),
                "industry": row.get("industry", "UNKNOWN") or "UNKNOWN",
            }
        )
    if not rows:
        return pd.DataFrame(columns=["size", "volatility", "industry"])
    return pd.DataFrame(rows).set_index("code")


def build_pool_panel(frames: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """一次性构建入池股票的日期 × 代码面板（B2 基准用，避免逐笔重复扫描）。"""
    parts = []
    for code, frame in frames.items():
        if "in_pool" not in frame.columns:
            continue
        subset = frame[frame["in_pool"].fillna(False)]
        if subset.empty:
            continue
        industry = subset["industry"] if "industry" in subset.columns else "UNKNOWN"
        parts.append(
            pd.DataFrame(
                {
                    "date": subset.index,
                    "code": code,
                    "size": subset.get("avg_amount_20d", pd.Series(0.0, index=subset.index)),
                    "volatility": subset.get("volatility_20d", pd.Series(0.0, index=subset.index)),
                    "industry": industry,
                }
            )
        )
    if not parts:
        return pd.DataFrame(columns=["date", "code", "size", "volatility", "industry"])
    return pd.concat(parts, ignore_index=True)
