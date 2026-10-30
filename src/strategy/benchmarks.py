# -*- coding: utf-8 -*-
"""基准构造：B2 同市值同波动同行业匹配组合、B3 动量（方案 §5.3）。

两套 B2 口径：
- legacy：同分位桶随机抽 5 只 × N 次取中位数（有抽样噪声，MVP 冻结结果所用）；
- matched（V3.0 §4.4 修正口径，与 fund-analysis event-study 统一）：
  同一信号日、同一流通市值分档内「所有可买股票」的平均收益，对事件股票留一剔除，
  并给出同日全池「市场口径」对照。无抽样噪声、无需种子。
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


def entry_buyable(
    frame: Optional[pd.DataFrame],
    entry_date: date,
    gap_max: float = 0.03,
    code: str = "",
    rules: Optional[object] = None,
) -> bool:
    """对照股票 T+1 开盘可买判定（与主策略入场拦截一致，V3.0 §4.4）。

    - entry_date 当日无行情（停牌无 bar）→ 不可买；
    - tradestatus=0 → 不可买；
    - 开盘价 >= 涨停价（含一字板）→ 买不到；
    - 开盘跳空 > gap_max → 与主策略同样拦截。

    涨跌停价：frames 带 ``limit_up`` 列时直接使用（引擎同款回退）；
    缺列时用 ``rules``（MarketRules）按 pre_close 即时计算（镜像 engine._bar 语义）；
    两者皆缺时跳过涨停检查（口径降级须在报告中披露）。
    """
    if frame is None or frame.empty:
        return False
    entry_ts = pd.Timestamp(entry_date)
    if entry_ts not in frame.index:
        return False
    row = frame.loc[entry_ts]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[-1]
    if int(pd.to_numeric(row.get("tradestatus"), errors="coerce") or 0) == 0:
        return False
    open_price = pd.to_numeric(row.get("open"), errors="coerce")
    if pd.isna(open_price) or open_price <= 0:
        return False
    pre_close = pd.to_numeric(row.get("pre_close"), errors="coerce")

    limit_up = pd.to_numeric(row.get("limit_up"), errors="coerce")
    if pd.isna(limit_up) and rules is not None and code:
        from src.sim.matcher import compute_limit_prices
        from src.sim.rules import MarketRules

        assert isinstance(rules, MarketRules)
        if pd.notna(pre_close) and pre_close > 0:
            is_st_value = row.get("is_st", False)
            is_st = bool(is_st_value) if not pd.isna(is_st_value) else False
            listed_days_value = row.get("listed_days")
            listed_days = (
                int(listed_days_value) if listed_days_value is not None and pd.notna(listed_days_value) else None
            )
            try:
                rule = rules.limit_rule(code, entry_date, is_st=is_st, listed_days=listed_days)
            except ValueError:
                rule = None
            if rule is not None:
                computed_up, _computed_down = compute_limit_prices(pre_close, rule, rules.tick_size)
                if computed_up is not None:
                    limit_up = float(computed_up)
    if pd.notna(limit_up) and limit_up > 0 and open_price >= limit_up:
        return False
    if pd.notna(pre_close) and pre_close > 0 and open_price / pre_close - 1.0 > gap_max:
        return False
    return True


class MatchedControlBench:
    """B2 修正口径（V3.0 §4.4）：同日同市值分档全体可买股票均值 + 留一剔除。

    panel 为 attach_market_caps 的输出（含 size_bucket / size_source 列）；
    frames 为全部股票行情（用于 T+1 开盘可买判定与 open→open 收益）。
    """

    def __init__(
        self,
        frames: dict[str, pd.DataFrame],
        panel: pd.DataFrame,
        config: Optional["MatchedControlConfig"] = None,  # noqa: F821 - 延迟导入避免环
        rules: Optional[object] = None,
    ) -> None:
        from src.data.market_caps import MatchedControlConfig
        from src.sim.rules import MarketRules

        self.config = config or MatchedControlConfig()
        self.rules = rules if rules is not None else MarketRules.load()
        self.frames = frames
        self._pools: dict[pd.Timestamp, dict[int, list[str]]] = {}
        self._all_pools: dict[pd.Timestamp, list[str]] = {}
        self._size_sources: dict[pd.Timestamp, str] = {}
        if not panel.empty:
            normalized = panel.copy()
            normalized["date"] = pd.to_datetime(normalized["date"])
            for date_value, day in normalized.groupby("date", sort=False):
                timestamp = pd.Timestamp(date_value)
                pools: dict[int, list[str]] = {}
                for bucket, group in day.groupby("size_bucket"):
                    pools[int(bucket)] = [str(code) for code in group["code"]]
                self._pools[timestamp] = pools
                self._all_pools[timestamp] = [str(code) for code in day["code"]]
                self._size_sources[timestamp] = str(day["size_source"].iloc[0])

    def has_date(self, signal_date: date) -> bool:
        return pd.Timestamp(signal_date) in self._pools

    def size_source(self, signal_date: date) -> str:
        return self._size_sources.get(pd.Timestamp(signal_date), "unknown")

    def benchmark(
        self,
        event_code: str,
        signal_date: date,
        entry_date: date,
        exit_date: date,
        size_bucket: int,
    ) -> dict:
        """返回同分档对照均值（留一）与市场口径对照均值（留一）。"""
        timestamp = pd.Timestamp(signal_date)
        pools = self._pools.get(timestamp)
        if pools is None:
            return {
                "control_mean": None,
                "control_n": 0,
                "market_mean": None,
                "market_n": 0,
                "size_source": "unknown",
            }

        def mean_return(codes: list[str]) -> tuple[Optional[float], int]:
            returns = []
            for code in codes:
                if code == event_code:
                    continue
                frame = self.frames.get(code)
                if not entry_buyable(
                    frame, entry_date, self.config.entry_gap_max, code=code, rules=self.rules
                ):
                    continue
                value = forward_open_return(frame, entry_date, exit_date)
                if value is not None:
                    returns.append(value)
            if not returns:
                return None, 0
            return float(np.mean(returns)), len(returns)

        bucket_codes = pools.get(int(size_bucket), [])
        all_codes = self._all_pools.get(timestamp, [])
        control_mean, control_n = mean_return(bucket_codes)
        market_mean, market_n = mean_return(all_codes)
        return {
            "control_mean": control_mean,
            "control_n": control_n,
            "market_mean": market_mean,
            "market_n": market_n,
            "size_source": self._size_sources.get(timestamp, "unknown"),
        }
