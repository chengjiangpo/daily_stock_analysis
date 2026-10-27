# -*- coding: utf-8 -*-
"""基准策略：动量（方案 §5.3 B3）。"""

from __future__ import annotations

import pandas as pd


def momentum_snapshot(data: dict[str, pd.DataFrame], trade_date: pd.Timestamp) -> pd.DataFrame:
    """取指定日各股 ret20/close/ma20，返回按代码索引的快照。"""
    rows = []
    for code, df in data.items():
        if trade_date not in df.index:
            continue
        row = df.loc[trade_date]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        ret20 = row.get("ret20")
        close = row.get("adj_close")
        ma20 = row.get("ma20")
        if pd.isna(ret20) or pd.isna(close) or pd.isna(ma20):
            continue
        rows.append({"code": code, "ret20": float(ret20), "close": float(close), "ma20": float(ma20)})
    if not rows:
        return pd.DataFrame(columns=["ret20", "close", "ma20"])
    return pd.DataFrame(rows).set_index("code")


def select_momentum(snapshot: pd.DataFrame, top_pct: float = 0.10, n: int = 5) -> list[str]:
    """在 close > MA20 的股票中，按 20 日收益取前 top_pct，再取前 n 只。"""
    if snapshot.empty:
        return []
    eligible = snapshot[snapshot["close"] > snapshot["ma20"]]
    if eligible.empty:
        return []
    threshold = eligible["ret20"].quantile(1.0 - top_pct)
    selected = eligible[eligible["ret20"] >= threshold]
    ordered = selected.assign(_code=selected.index.astype(str)).sort_values(
        ["ret20", "_code"], ascending=[False, True]
    )
    return list(ordered.index[:n])
