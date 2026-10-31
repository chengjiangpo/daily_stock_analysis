# -*- coding: utf-8 -*-
"""事件研究：信号级 alpha 检验（V3.0 §4.4 / §3.2 准入条件 3）。

在做组合回测之前，用更便宜、更严格的方法回答「这个信号本身有没有 alpha」。
本模块是 ``fund-analysis`` 的 ``src/app/server/event-study.ts`` 的 Python 移植，
口径逐项对齐，供 ``daily_stock_analysis`` 侧作为再出发准入流程的执行载体（I-6）。

口径（与 fund-analysis event-study-guide.md / §4.4 一致）：
- 信号日 T 收盘确认 → T+1 开盘买入 → 持有 h 个交易日，在第 T+h 日收盘卖出
  （h=1 即 T+1 收盘）；
- 收益使用复权价：``(close_exit × adj_exit) / (open_entry × adj_entry) − 1``，
  除权不造成假涨跌；
- 入场拦截：次日无行情 / 无开盘 / 无成交 / 缺涨停价 / 开盘即涨停 / 高开超过阈值，
  这些信号**不计入**收益统计，只计入 ``blocked_by_reason``；对照组同样套用，保证口径一致；
- 对照组（B2 matched）：同一信号日、同一市值分档内**所有可买股票**的平均收益
  （等价于无限次随机抽样的期望，无抽样噪声、无需种子），并对事件股票本身留一剔除；
  另给「仅同日同股票池」的市场口径对照；
- 显著性：按「信号日所在的 h 日区块」做聚类稳健标准误（处理同日截面相关与持有期重叠），
  可选按聚类重采样做 bootstrap 置信区间（§4.6 P-3）。

已知局限（与 fund-analysis 一致，报告须披露）：
- 持有期内停牌/退市导致缺少退出日行情的样本被剔除（轻微乐观），单独给出剔除数；
- 退出按收盘价成交，不模拟跌停无法卖出；
- 不含冲击成本与容量约束。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Literal, Mapping, Sequence

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_EVENT_HORIZONS: tuple[int, ...] = (1, 3, 5, 10, 20)
# 流通市值分档边界（亿元）：<20、20~30、30~45、45~70、≥70
DEFAULT_SIZE_EDGES_YI: tuple[float, ...] = (20.0, 30.0, 45.0, 70.0)
# 往返成本（bp），与 fund-analysis DEFAULT_BOX_BACKTEST_CONFIG 一致：
# 滑点 15×2 + 佣金 2.5×2 + 印花税 5 + 过户费 0.1×2 = 40.2bp
DEFAULT_ROUND_TRIP_COST_BPS: float = 40.2

EventBlockReason = Literal[
    "no_entry_bar",
    "missing_price",
    "missing_adj",
    "missing_open",
    "no_volume",
    "up_limit_missing",
    "limit_up_open",
    "gap_up",
]

EVENT_BLOCK_REASONS: tuple[EventBlockReason, ...] = (
    "no_entry_bar",
    "missing_price",
    "missing_adj",
    "missing_open",
    "no_volume",
    "up_limit_missing",
    "limit_up_open",
    "gap_up",
)

# 事件研究需要的行情列
REQUIRED_COLUMNS = ("open", "close", "adj_factor")
OPTIONAL_COLUMNS = (
    "limit_up",
    "circ_mv",
    "volume",
    "amount",
    "tradestatus",
    "is_st",
    "pre_close",
    # 自定义股票池（pool_config / --universe v1-pool）会预先算好 in_pool 列，
    # 必须进行缓存，否则 universe 回调拿不到该字段、全部判定为不在池内。
    "in_pool",
    "listed_days",
)


# ---------------------------------------------------------------------------
# 统计工具（纯 Python，无 scipy 运行时依赖）
# ---------------------------------------------------------------------------


def _erf(x: float) -> float:
    """erf 的 Abramowitz-Stegun 7.1.26 近似，误差 < 1.5e-7。"""
    sign = -1.0 if x < 0 else 1.0
    ax = abs(x)
    t = 1.0 / (1.0 + 0.3275911 * ax)
    poly = (
        ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t
    )
    return sign * (1.0 - poly * math.exp(-ax * ax))


def _log_gamma(x: float) -> float:
    """ln Γ(x)，Lanczos 近似。"""
    coefficients = (
        76.18009172947146,
        -86.50532032941677,
        24.01409824083091,
        -1.231739572450155,
        0.1208650973866179e-2,
        -0.5395239384953e-5,
    )
    y = x
    tmp = x + 5.5
    tmp -= (x + 0.5) * math.log(tmp)
    series = 1.000000000190015
    for coefficient in coefficients:
        y += 1.0
        series += coefficient / y
    return -tmp + math.log((2.5066282746310005 * series) / x)


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """正则化不完全 Beta 函数的连分式（Numerical Recipes betacf）。"""
    tiny = 1e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - (qab * x) / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = (m * (b - m) * x) / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = (-(a + m) * (qab + m) * x) / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-12:
            break
    return h


def _regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(
        _log_gamma(a + b) - _log_gamma(a) - _log_gamma(b) + a * math.log(x) + b * math.log(1 - x)
    )
    if x < (a + 1) / (a + b + 2):
        return front * _beta_continued_fraction(a, b, x) / a
    return 1.0 - front * _beta_continued_fraction(b, a, 1 - x) / b


def two_sided_p_value(t: float, df: float | None = None) -> float:
    """双侧 p 值。

    ``df`` 为自由度（聚类稳健检验用聚类数 − 1）：聚类很少时（例如 20 日周期只有
    二十来个区块）正态近似明显偏乐观，必须用 t 分布。``df`` 缺省或非有限时退化为标准正态。
    """
    if df is None or not math.isfinite(df) or df > 1e6:
        return min(1.0, max(0.0, 1.0 - _erf(abs(t) / math.sqrt(2.0))))
    if not df > 0:
        return 1.0
    x = df / (df + t * t)
    return min(1.0, max(0.0, _regularized_incomplete_beta(x, df / 2.0, 0.5)))


@dataclass(frozen=True)
class ClusterTResult:
    """均值的聚类稳健检验结果。"""

    mean: float
    se: float | None
    t: float | None
    p: float | None
    clusters: int


def cluster_robust_mean(values: Sequence[float], cluster_ids: Sequence[int]) -> ClusterTResult:
    """均值的聚类稳健检验。

    ``se = sqrt(G/(G-1) × Σ_c (Σ_{i∈c}(x_i − mean))²) / N``；
    聚类数 < 2 或 se 为 0 时 t 为 None。
    """
    n = len(values)
    if n == 0:
        return ClusterTResult(mean=0.0, se=None, t=None, p=None, clusters=0)
    data = np.asarray(values, dtype=float)
    ids = np.asarray(cluster_ids, dtype=np.int64)
    mean = float(data.mean())

    order = np.argsort(ids, kind="stable")
    sorted_ids = ids[order]
    deviations = data[order] - mean
    # 按聚类聚合偏离量之和
    boundaries = np.flatnonzero(np.diff(sorted_ids)) + 1
    group_sums = np.add.reduceat(deviations, np.concatenate(([0], boundaries)))
    g = int(group_sums.size)
    if g < 2:
        return ClusterTResult(mean=mean, se=None, t=None, p=None, clusters=g)
    ss = float(np.sum(group_sums * group_sums))
    se = math.sqrt((g / (g - 1)) * ss) / n
    if not se > 0:
        return ClusterTResult(mean=mean, se=se, t=None, p=None, clusters=g)
    t = mean / se
    return ClusterTResult(mean=mean, se=se, t=t, p=two_sided_p_value(t, g - 1), clusters=g)


def cluster_bootstrap_mean_ci(
    values: Sequence[float],
    cluster_ids: Sequence[int],
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 42,
) -> dict:
    """按聚类重采样的均值置信区间（§4.6 P-3：信号级超额必须按信号日聚类重采样）。

    返回 ``estimate`` / ``ci_low`` / ``ci_high`` / ``n_boot`` / ``clusters``；
    聚类数 < 2 时返回 ``None`` 区间。
    """
    n = len(values)
    if n == 0:
        return {"estimate": None, "ci_low": None, "ci_high": None, "n_boot": 0, "clusters": 0}
    data = np.asarray(values, dtype=float)
    ids = np.asarray(cluster_ids, dtype=np.int64)
    estimate = float(data.mean())

    unique = np.unique(ids)
    g = int(unique.size)
    if g < 2 or n_boot < 1:
        return {"estimate": estimate, "ci_low": None, "ci_high": None, "n_boot": 0, "clusters": g}

    # 预先按聚类分桶，重采样时按聚类整体抽取（保留组内相关）
    order = np.argsort(ids, kind="stable")
    sorted_ids = ids[order]
    sorted_values = data[order]
    boundaries = np.concatenate(([0], np.flatnonzero(np.diff(sorted_ids)) + 1, [sorted_values.size]))
    groups = [sorted_values[boundaries[i] : boundaries[i + 1]] for i in range(g)]

    rng = np.random.default_rng(seed)
    means = np.empty(n_boot, dtype=float)
    for index in range(n_boot):
        picked = rng.integers(0, g, size=g)
        pooled = np.concatenate([groups[i] for i in picked])
        means[index] = float(pooled.mean()) if pooled.size else math.nan
    means = means[np.isfinite(means)]
    if means.size == 0:
        return {"estimate": estimate, "ci_low": None, "ci_high": None, "n_boot": 0, "clusters": g}
    ci_low, ci_high = np.quantile(means, [alpha / 2.0, 1.0 - alpha / 2.0])
    return {
        "estimate": estimate,
        "ci_low": float(ci_low),
        "ci_high": float(ci_high),
        "n_boot": int(means.size),
        "clusters": g,
    }


# ---------------------------------------------------------------------------
# 入场 / 收益
# ---------------------------------------------------------------------------


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class EventGateConfig:
    """入场拦截参数（与主策略/组合回测同口径）。"""

    max_entry_gap_pct: float = 8.0
    limit_price_tolerance: float = 0.01

    @classmethod
    def from_config(cls, raw: Mapping | None = None) -> "EventGateConfig":
        raw = raw or {}
        return cls(
            max_entry_gap_pct=float(raw.get("max_entry_gap_pct", 8.0)),
            limit_price_tolerance=float(raw.get("limit_price_tolerance", 0.01)),
        )


def evaluate_event_entry(
    signal_row: Mapping,
    entry_row: Mapping | None,
    gate: EventGateConfig,
) -> EventBlockReason | None:
    """判断信号日次日是否可按开盘价买入；返回 None 表示可买。

    ``signal_row`` / ``entry_row`` 需含 ``open``/``close``/``adj_factor``/``limit_up``/``volume``。
    与 ``simulateBoxBreakoutTrade`` 的拦截口径一致（不含 back_in_box，它依赖具体箱体）。
    """
    if entry_row is None:
        return "no_entry_bar"
    if not _positive(_get(signal_row, "close")):
        return "missing_price"
    if not _positive(_get(entry_row, "open")):
        return "missing_open"
    if not _positive(_get(entry_row, "volume")):
        return "no_volume"
    limit_up = _get(entry_row, "limit_up")
    if limit_up is None or not math.isfinite(limit_up):
        return "up_limit_missing"
    if _get(entry_row, "open") + gate.limit_price_tolerance >= limit_up:
        return "limit_up_open"
    adj_signal = _get(signal_row, "adj_factor")
    adj_entry = _get(entry_row, "adj_factor")
    if not _positive(adj_signal) or not _positive(adj_entry):
        return "missing_adj"
    if gate.max_entry_gap_pct > 0:
        gap_pct = (_get(entry_row, "open") * adj_entry / (_get(signal_row, "close") * adj_signal) - 1.0) * 100.0
        if gap_pct > gate.max_entry_gap_pct:
            return "gap_up"
    return None


def forward_return_pct(entry_row: Mapping | None, exit_row: Mapping | None) -> float | None:
    """T+1 开盘买入、第 T+h 日收盘卖出的复权收益（%）；缺退出行情返回 None。"""
    if entry_row is None or exit_row is None:
        return None
    entry_open = _get(entry_row, "open")
    entry_adj = _get(entry_row, "adj_factor")
    exit_close = _get(exit_row, "close")
    exit_adj = _get(exit_row, "adj_factor")
    if not _positive(entry_open) or not _positive(entry_adj):
        return None
    if not _positive(exit_close) or not _positive(exit_adj):
        return None
    return (exit_close * exit_adj) / (entry_open * entry_adj) * 100.0 - 100.0


def _get(row: Mapping | None, key: str) -> float | None:
    """从 dict 或 Series 安全取浮点值（NaN → None）。"""
    if row is None:
        return None
    try:
        value = row[key]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(number) else number


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventHorizonStats:
    """单个事件在单个持有周期上的统计。"""

    horizon: int
    n: int
    dropped_no_exit: int
    date_count: int
    mean_pct: float | None
    median_pct: float | None
    win_rate: float | None
    net_mean_pct: float | None
    control_mean_pct: float | None
    excess_mean_pct: float | None
    excess_median_pct: float | None
    excess_win_rate: float | None
    excess_t_stat: float | None
    excess_p_value: float | None
    cluster_count: int
    excess_ex_top5_mean_pct: float | None
    excess_trimmed_mean_pct: float | None
    market_excess_mean_pct: float | None
    market_excess_t_stat: float | None
    excess_ci_low_pct: float | None = None
    excess_ci_high_pct: float | None = None
    by_year: tuple[dict, ...] = ()
    by_month: tuple[dict, ...] = ()

    def to_dict(self) -> dict:
        payload = {
            "horizon": self.horizon,
            "n": self.n,
            "droppedNoExit": self.dropped_no_exit,
            "dateCount": self.date_count,
            "meanPct": self.mean_pct,
            "medianPct": self.median_pct,
            "winRate": self.win_rate,
            "netMeanPct": self.net_mean_pct,
            "controlMeanPct": self.control_mean_pct,
            "excessMeanPct": self.excess_mean_pct,
            "excessMedianPct": self.excess_median_pct,
            "excessWinRate": self.excess_win_rate,
            "excessTStat": self.excess_t_stat,
            "excessPValue": self.excess_p_value,
            "clusterCount": self.cluster_count,
            "excessExTop5MeanPct": self.excess_ex_top5_mean_pct,
            "excessTrimmedMeanPct": self.excess_trimmed_mean_pct,
            "marketExcessMeanPct": self.market_excess_mean_pct,
            "marketExcessTStat": self.market_excess_t_stat,
            "excessCiLowPct": self.excess_ci_low_pct,
            "excessCiHighPct": self.excess_ci_high_pct,
            "byYear": list(self.by_year),
            "byMonth": list(self.by_month),
        }
        return payload


@dataclass(frozen=True)
class EventResult:
    """单个事件定义的完整结果。"""

    name: str
    detected_count: int
    blocked_count: int
    blocked_by_reason: dict[str, int]
    tradable_count: int
    horizons: tuple[EventHorizonStats, ...]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "detectedCount": self.detected_count,
            "blockedCount": self.blocked_count,
            "blockedByReason": dict(self.blocked_by_reason),
            "tradableCount": self.tradable_count,
            "horizons": [stats.to_dict() for stats in self.horizons],
        }


@dataclass(frozen=True)
class EventStudyResult:
    """一次事件研究的完整结果。"""

    start: str
    end: str
    stock_count: int
    pool_observations: int
    cost_bps: float
    size_edges_yi: tuple[float, ...]
    events: tuple[EventResult, ...]

    def to_dict(self) -> dict:
        return {
            "range": {"start": self.start, "end": self.end},
            "stockCount": self.stock_count,
            "poolObservations": self.pool_observations,
            "costBps": self.cost_bps,
            "sizeEdgesYi": list(self.size_edges_yi),
            "events": [event.to_dict() for event in self.events],
        }


# ---------------------------------------------------------------------------
# 累加器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventStudyOptions:
    """事件研究配置。

    ``universe``：``(code, row) -> bool``，判定某只股票某日是否在股票池内；
    缺省用 ``default_universe``（主板 + 非 ST + 流通市值上限）。
    """

    calendar: Sequence[date | str | pd.Timestamp]
    start: str
    end: str
    horizons: Sequence[int] = DEFAULT_EVENT_HORIZONS
    size_edges_yi: Sequence[float] = DEFAULT_SIZE_EDGES_YI
    gate: EventGateConfig = field(default_factory=EventGateConfig)
    cost_bps: float = DEFAULT_ROUND_TRIP_COST_BPS
    universe: Callable[[str, Mapping], bool] | None = None
    max_circ_mv_yi: float | None = None
    bootstrap: int = 0
    seed: int = 42


def default_universe(max_circ_mv_yi: float | None = None) -> Callable[[str, Mapping], bool]:
    """缺省股票池：主板 + 非 ST（时点）+ 可选流通市值上限（亿元）。"""
    from src.sim.rules import Board, classify_board

    def predicate(code: str, row: Mapping) -> bool:
        try:
            if classify_board(code) != Board.MAIN:
                return False
        except ValueError:
            return False
        is_st = row.get("is_st") if hasattr(row, "get") else None
        if is_st is not None:
            try:
                if bool(is_st) and not (isinstance(is_st, float) and math.isnan(is_st)):
                    return False
            except (TypeError, ValueError):
                pass
        if max_circ_mv_yi is not None:
            circ_mv = _get(row, "circ_mv")
            if circ_mv is None or circ_mv <= 0:
                return False
            if circ_mv / 1e8 >= max_circ_mv_yi:
                return False
        return True

    return predicate


@dataclass
class _Record:
    signal_idx: int
    date: str
    code: str
    bucket: int
    rets: list[float | None]


class EventStudyAccumulator:
    """逐只股票喂入行情，累计对照池与事件样本；全部喂完后 ``finalize()`` 输出统计。

    对照池与事件无关，多个事件共用一次扫描。
    """

    def __init__(self, options: EventStudyOptions, event_names: Iterable[str]) -> None:
        self.horizons = sorted(int(h) for h in options.horizons) or list(DEFAULT_EVENT_HORIZONS)
        if any(h <= 0 for h in self.horizons):
            raise ValueError("horizons 必须为正整数")
        self.edges_yi = sorted(float(edge) for edge in options.size_edges_yi)
        self.bucket_count = len(self.edges_yi) + 1
        self.gate = options.gate
        self.cost_bps = float(options.cost_bps)
        self.max_circ_mv_yi = options.max_circ_mv_yi
        self.universe = options.universe or default_universe(options.max_circ_mv_yi)
        self.bootstrap = int(options.bootstrap)
        self.seed = int(options.seed)

        self.calendar = [pd.Timestamp(item).normalize() for item in options.calendar]
        if not self.calendar:
            raise ValueError("calendar 不能为空")
        self.cal_index = {timestamp: index for index, timestamp in enumerate(self.calendar)}
        self.start = str(options.start)[:10]
        self.end = str(options.end)[:10]

        start_ts = pd.Timestamp(self.start)
        end_ts = pd.Timestamp(self.end)
        self.start_idx = next(
            (i for i, ts in enumerate(self.calendar) if ts >= start_ts), len(self.calendar)
        )
        self.end_idx = -1
        for index, ts in enumerate(self.calendar):
            if ts <= end_ts:
                self.end_idx = index
        self._calendar_len = len(self.calendar)

        horizon_count = len(self.horizons)
        days = self._calendar_len
        # 对照池累加器：(signal_idx, size_bucket, horizon) → 收益和/计数
        self._sum_bucket = np.zeros(days * self.bucket_count * horizon_count, dtype=float)
        self._cnt_bucket = np.zeros(days * self.bucket_count * horizon_count, dtype=np.int64)
        # 市场口径累加器：(signal_idx, horizon)
        self._sum_date = np.zeros(days * horizon_count, dtype=float)
        self._cnt_date = np.zeros(days * horizon_count, dtype=np.int64)

        self.event_names = list(event_names)
        self._records: dict[str, list[_Record]] = {name: [] for name in self.event_names}
        self._detected: dict[str, int] = {name: 0 for name in self.event_names}
        self._blocked: dict[str, dict[str, int]] = {
            name: {reason: 0 for reason in EVENT_BLOCK_REASONS} for name in self.event_names
        }
        self.pool_observations = 0
        self.stock_count = 0

    # -- 分档 ---------------------------------------------------------------

    def size_bucket(self, circ_mv: float) -> int:
        """流通市值（元）→ 分档序号。"""
        yi = circ_mv / 1e8
        return sum(1 for edge in self.edges_yi if yi >= edge)

    # -- 喂数据 -------------------------------------------------------------

    def add_stock(
        self,
        frame: pd.DataFrame,
        event_dates: Mapping[str, Iterable[date | str | pd.Timestamp]] | None = None,
    ) -> None:
        """喂入一只股票。

        ``frame``：DatetimeIndex 行情，需含 ``open``/``close``/``adj_factor``，
        以及 ``limit_up``（涨停价；缺失则所有信号记 ``up_limit_missing``）、
        ``volume``、``circ_mv``（流通市值，元）、``is_st``。
        ``event_dates``：``{事件名: 该股票上的信号日集合}``。
        """
        if self.end_idx < self.start_idx:
            return
        self.stock_count += 1

        missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
        if missing:
            raise ValueError(f"事件研究行情缺少必要列: {missing}")

        position = {timestamp: index for index, timestamp in enumerate(frame.index)}
        # 把该股票的每一行映射到全局日历下标
        aligned: dict[int, int] = {}
        for timestamp, row_index in position.items():
            cal_idx = self.cal_index.get(pd.Timestamp(timestamp).normalize())
            if cal_idx is not None:
                aligned[cal_idx] = row_index

        horizon_count = len(self.horizons)
        hits_by_date: dict[int, list[str]] = {}
        for name, dates in (event_dates or {}).items():
            if name not in self._records:
                continue
            for item in dates:
                cal_idx = self.cal_index.get(pd.Timestamp(item).normalize())
                if cal_idx is None:
                    continue
                hits_by_date.setdefault(cal_idx, []).append(name)

        # 预取需要的行字段，避免在循环里反复 .loc
        columns = [column for column in OPTIONAL_COLUMNS if column in frame.columns]
        row_cache: dict[int, dict] = {}

        def row_at(cal_idx: int) -> Mapping | None:
            if not 0 <= cal_idx < self._calendar_len:
                return None
            frame_index = aligned.get(cal_idx)
            if frame_index is None:
                return None
            if frame_index not in row_cache:
                series = frame.iloc[frame_index]
                payload = {"close": series.get("close"), "open": series.get("open"),
                           "adj_factor": series.get("adj_factor")}
                for column in columns:
                    payload[column] = series.get(column)
                row_cache[frame_index] = payload
            return row_cache[frame_index]

        for cal_idx in range(self.start_idx, self.end_idx + 1):
            frame_index = aligned.get(cal_idx)
            if frame_index is None:
                continue
            signal_row = row_at(cal_idx)
            if signal_row is None:
                continue
            code = str(frame.iloc[frame_index].get("code", "") or "")
            if not self.universe(code, signal_row):
                continue
            circ_mv = _get(signal_row, "circ_mv")
            if circ_mv is None or circ_mv <= 0:
                continue

            date_text = self.calendar[cal_idx].strftime("%Y-%m-%d")
            hits = hits_by_date.get(cal_idx, [])

            entry_row = row_at(cal_idx + 1)
            reason = evaluate_event_entry(signal_row, entry_row, self.gate)
            if reason is not None:
                for name in hits:
                    self._detected[name] += 1
                    self._blocked[name][reason] += 1
                continue

            bucket = self.size_bucket(circ_mv)
            returns: list[float | None] = []
            for horizon in self.horizons:
                returns.append(forward_return_pct(entry_row, row_at(cal_idx + horizon)))

            counted = False
            for horizon_index in range(horizon_count):
                value = returns[horizon_index]
                if value is None:
                    continue
                counted = True
                bucket_index = (cal_idx * self.bucket_count + bucket) * horizon_count + horizon_index
                self._sum_bucket[bucket_index] += value
                self._cnt_bucket[bucket_index] += 1
                date_index = cal_idx * horizon_count + horizon_index
                self._sum_date[date_index] += value
                self._cnt_date[date_index] += 1
            if counted:
                self.pool_observations += 1

            for name in hits:
                self._detected[name] += 1
                self._records[name].append(
                    _Record(signal_idx=cal_idx, date=date_text, code=code, bucket=bucket, rets=returns)
                )

    # -- 汇总 ---------------------------------------------------------------

    def finalize(self) -> EventStudyResult:
        horizon_count = len(self.horizons)
        events = []
        for name in self.event_names:
            records = self._records[name]
            blocked = self._blocked[name]
            horizon_stats = []
            for horizon_index, horizon in enumerate(self.horizons):
                rows: list[dict] = []
                dropped = 0
                for record in records:
                    value = record.rets[horizon_index]
                    if value is None:
                        dropped += 1
                        continue
                    bucket_index = (
                        record.signal_idx * self.bucket_count + record.bucket
                    ) * horizon_count + horizon_index
                    bucket_count = int(self._cnt_bucket[bucket_index])
                    control = (
                        (self._sum_bucket[bucket_index] - value) / (bucket_count - 1)
                        if bucket_count > 1
                        else None
                    )
                    date_index = record.signal_idx * horizon_count + horizon_index
                    date_count = int(self._cnt_date[date_index])
                    market = (
                        (self._sum_date[date_index] - value) / (date_count - 1)
                        if date_count > 1
                        else None
                    )
                    rows.append(
                        {
                            "ret": value,
                            "ctrl": control,
                            "mkt": market,
                            "signal_idx": record.signal_idx,
                            "year": record.date[:4],
                            "month": record.date[:7],
                        }
                    )
                horizon_stats.append(
                    self._summarize_horizon(horizon, rows, dropped)
                )
            events.append(
                EventResult(
                    name=name,
                    detected_count=self._detected[name],
                    blocked_count=sum(blocked.values()),
                    blocked_by_reason=dict(blocked),
                    tradable_count=len(records),
                    horizons=tuple(horizon_stats),
                )
            )

        return EventStudyResult(
            start=self.start,
            end=self.end,
            stock_count=self.stock_count,
            pool_observations=self.pool_observations,
            cost_bps=self.cost_bps,
            size_edges_yi=tuple(self.edges_yi),
            events=tuple(events),
        )

    def _summarize_horizon(self, horizon: int, rows: Sequence[dict], dropped: int) -> EventHorizonStats:
        block = max(1, int(horizon))
        rets = [row["ret"] for row in rows]
        matched = [row for row in rows if row["ctrl"] is not None]
        excess = [row["ret"] - row["ctrl"] for row in matched]
        excess_ids = [int(row["signal_idx"] // block) for row in matched]
        test = cluster_robust_mean(excess, excess_ids)

        market_rows = [row for row in rows if row["mkt"] is not None]
        market_excess = [row["ret"] - row["mkt"] for row in market_rows]
        market_ids = [int(row["signal_idx"] // block) for row in market_rows]
        market_test = cluster_robust_mean(market_excess, market_ids)

        sorted_excess = sorted(excess, reverse=True)
        ex_top5 = float(np.mean(sorted_excess[5:])) if len(sorted_excess) > 5 else None
        trim_n = int(math.floor(len(sorted_excess) * 0.01))
        trimmed = (
            float(np.mean(sorted_excess[trim_n : len(sorted_excess) - trim_n]))
            if len(sorted_excess) - 2 * trim_n > 0
            else None
        )

        by_year: dict[str, list[dict]] = {}
        by_month: dict[str, list[dict]] = {}
        for row in rows:
            by_year.setdefault(row["year"], []).append(row)
            by_month.setdefault(row["month"], []).append(row)

        year_stats = []
        for key in sorted(by_year):
            group = by_year[key]
            group_excess = [row["ret"] - row["ctrl"] for row in group if row["ctrl"] is not None]
            year_stats.append(
                {
                    "year": key,
                    "n": len(group),
                    "meanPct": _round4(_mean([row["ret"] for row in group])),
                    "excessMeanPct": _round4(_mean(group_excess)),
                }
            )
        month_stats = []
        for key in sorted(by_month):
            group = by_month[key]
            group_excess = [row["ret"] - row["ctrl"] for row in group if row["ctrl"] is not None]
            month_stats.append(
                {
                    "month": key,
                    "n": len(group),
                    "meanPct": _round4(_mean([row["ret"] for row in group])),
                    "excessMeanPct": _round4(_mean(group_excess)),
                }
            )

        mean_ret = _mean(rets)
        bootstrap_ci = (
            cluster_bootstrap_mean_ci(excess, excess_ids, n_boot=self.bootstrap, seed=self.seed)
            if self.bootstrap > 0
            else {"ci_low": None, "ci_high": None}
        )
        return EventHorizonStats(
            horizon=horizon,
            n=len(rows),
            dropped_no_exit=dropped,
            date_count=len({row["signal_idx"] for row in rows}),
            mean_pct=_round4(mean_ret),
            median_pct=_round4(_median(rets)),
            win_rate=_round4(sum(1 for value in rets if value > 0) / len(rets)) if rets else None,
            net_mean_pct=(round(mean_ret - self.cost_bps / 100.0, 4) if mean_ret is not None else None),
            control_mean_pct=_round4(_mean([row["ctrl"] for row in matched])),
            excess_mean_pct=round(test.mean, 4) if excess else None,
            excess_median_pct=_round4(_median(excess)),
            excess_win_rate=(
                _round4(sum(1 for value in excess if value > 0) / len(excess)) if excess else None
            ),
            excess_t_stat=_round4(test.t),
            excess_p_value=_round4(test.p),
            cluster_count=test.clusters,
            excess_ex_top5_mean_pct=_round4(ex_top5),
            excess_trimmed_mean_pct=_round4(trimmed),
            market_excess_mean_pct=round(market_test.mean, 4) if market_excess else None,
            market_excess_t_stat=_round4(market_test.t),
            excess_ci_low_pct=_round4(bootstrap_ci.get("ci_low")),
            excess_ci_high_pct=_round4(bootstrap_ci.get("ci_high")),
            by_year=tuple(year_stats),
            by_month=tuple(month_stats),
        )


# ---------------------------------------------------------------------------
# 初筛门槛（V3.0 §4.4：全部通过才允许进入组合回测）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventGateOptions:
    """初筛门槛参数（默认值即 V3.0 §4.4 冻结口径）。"""

    min_samples: int = 200
    min_t_stat: float = 2.0
    min_clusters: int = 30
    min_positive_year_ratio: float = 0.6
    min_year_samples: int = 20


@dataclass(frozen=True)
class EventGateCheck:
    key: str
    label: str
    passed: bool
    actual: str

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "passed": self.passed, "actual": self.actual}


@dataclass(frozen=True)
class EventGateResult:
    horizon: int
    passed: bool
    checks: tuple[EventGateCheck, ...]

    def to_dict(self) -> dict:
        return {
            "horizon": self.horizon,
            "pass": self.passed,
            "checks": [check.to_dict() for check in self.checks],
        }


def _fmt(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def evaluate_event_gate(
    stats: EventHorizonStats,
    options: EventGateOptions | None = None,
) -> EventGateResult:
    """单个 horizon 的初筛门槛。全部通过才允许进入组合回测。

    注意：同时看多个 horizon / 多个事件定义会放大多重检验问题，
    应事先指定主 horizon，并记录总共试了多少个事件定义（试验账本 §3.2）。
    """
    options = options or EventGateOptions()
    eligible_years = [
        item
        for item in stats.by_year
        if item["n"] >= options.min_year_samples and item["excessMeanPct"] is not None
    ]
    positive_years = sum(1 for item in eligible_years if item["excessMeanPct"] > 0)
    year_ratio = positive_years / len(eligible_years) if eligible_years else None

    checks = (
        EventGateCheck(
            "samples",
            f"样本数 ≥ {options.min_samples}",
            stats.n >= options.min_samples,
            str(stats.n),
        ),
        EventGateCheck(
            "clusters",
            f"聚类数 ≥ {options.min_clusters}（t 值可靠）",
            stats.cluster_count >= options.min_clusters,
            str(stats.cluster_count),
        ),
        EventGateCheck(
            "excess_positive",
            "平均超额 > 0",
            stats.excess_mean_pct is not None and stats.excess_mean_pct > 0,
            f"{_fmt(stats.excess_mean_pct)}%",
        ),
        EventGateCheck(
            "t_stat",
            f"聚类 t ≥ {options.min_t_stat:g}",
            stats.excess_t_stat is not None and stats.excess_t_stat >= options.min_t_stat,
            _fmt(stats.excess_t_stat, 2),
        ),
        EventGateCheck(
            "ex_top5",
            "剔除超额最高 5 笔后仍 > 0",
            stats.excess_ex_top5_mean_pct is not None and stats.excess_ex_top5_mean_pct > 0,
            f"{_fmt(stats.excess_ex_top5_mean_pct)}%",
        ),
        EventGateCheck(
            "net_positive",
            "扣往返成本后绝对收益 > 0",
            stats.net_mean_pct is not None and stats.net_mean_pct > 0,
            f"{_fmt(stats.net_mean_pct)}%",
        ),
        EventGateCheck(
            "year_stability",
            f"超额为正的年份占比 ≥ {options.min_positive_year_ratio * 100:.0f}%"
            f"（年样本 ≥ {options.min_year_samples}）",
            year_ratio is not None and year_ratio >= options.min_positive_year_ratio,
            "无可评估年份" if year_ratio is None else f"{positive_years}/{len(eligible_years)}",
        ),
    )
    return EventGateResult(horizon=stats.horizon, passed=all(check.passed for check in checks), checks=checks)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return float(np.mean(values))


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return float(np.median(values))


def _round4(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 4)


def build_calendar(frames: Mapping[str, pd.DataFrame]) -> list[pd.Timestamp]:
    """从多只股票行情里取并集交易日历（升序）。"""
    union: set[pd.Timestamp] = set()
    for frame in frames.values():
        if frame is None or frame.empty:
            continue
        union.update(pd.Timestamp(item).normalize() for item in frame.index)
    return sorted(union)


__all__ = [
    "DEFAULT_EVENT_HORIZONS",
    "DEFAULT_ROUND_TRIP_COST_BPS",
    "DEFAULT_SIZE_EDGES_YI",
    "EVENT_BLOCK_REASONS",
    "EventBlockReason",
    "EventGateCheck",
    "EventGateConfig",
    "EventGateOptions",
    "EventGateResult",
    "EventHorizonStats",
    "EventResult",
    "EventStudyAccumulator",
    "EventStudyOptions",
    "EventStudyResult",
    "build_calendar",
    "cluster_bootstrap_mean_ci",
    "cluster_robust_mean",
    "default_universe",
    "evaluate_event_entry",
    "evaluate_event_gate",
    "forward_return_pct",
    "two_sided_p_value",
]
