# -*- coding: utf-8 -*-
"""事件研究（I-6）单测：口径与 fund-analysis `event-study.ts` 对齐。

覆盖：
- 统计工具（t 分布 p 值、聚类稳健 t、聚类 bootstrap）；
- 收益与入场拦截口径；
- 累加器的留一对照精确性（同日同市值分档均值 − 事件本身）；
- 初筛门槛 7 项判定；
- 事件定义（策略入场信号 / 条件事件）。
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.validation.event_study import (
    EventGateConfig,
    EventGateOptions,
    EventHorizonStats,
    EventStudyAccumulator,
    EventStudyOptions,
    build_calendar,
    cluster_bootstrap_mean_ci,
    cluster_robust_mean,
    evaluate_event_entry,
    evaluate_event_gate,
    forward_return_pct,
    two_sided_p_value,
)


# ---------------------------------------------------------------------------
# 统计工具
# ---------------------------------------------------------------------------


def test_two_sided_p_value_matches_reference():
    """t 分布双侧 p 值与 scipy 一致（scipy 缺席时用解析已知值）。"""
    try:
        from scipy import stats as scipy_stats
    except ImportError:  # pragma: no cover - scipy 通常可用
        scipy_stats = None

    cases = [(1.96, 1000), (2.228, 10), (3.0, 29), (-1.2, 5), (0.0, 30), (5.0, 2)]
    for t, df in cases:
        mine = two_sided_p_value(t, df)
        if scipy_stats is not None:
            reference = 2 * scipy_stats.t.sf(abs(t), df)
            assert abs(mine - reference) < 1e-6, (t, df, mine, reference)
    # 正态分支
    assert abs(two_sided_p_value(1.959964) - 0.05) < 1e-5
    assert two_sided_p_value(0.0) == pytest.approx(1.0)


def test_cluster_robust_mean_hand_computed():
    """两聚类的聚类稳健 t 值可手算复核。"""
    values = [1.0, 2.0, 4.0, 5.0]
    clusters = [0, 0, 1, 1]
    result = cluster_robust_mean(values, clusters)
    mean = 3.0
    # 各聚类偏离和：c0 = -3，c1 = +3；G=2, N=4
    se = math.sqrt((2 / 1) * (9 + 9)) / 4
    assert result.mean == pytest.approx(mean)
    assert result.se == pytest.approx(se)
    assert result.t == pytest.approx(mean / se)
    assert result.clusters == 2


def test_cluster_robust_mean_single_cluster_has_no_t():
    result = cluster_robust_mean([1.0, 2.0, 3.0], [0, 0, 0])
    assert result.clusters == 1
    assert result.t is None and result.p is None


def test_cluster_robust_mean_empty():
    result = cluster_robust_mean([], [])
    assert result.mean == 0.0 and result.clusters == 0 and result.t is None


def test_cluster_bootstrap_reproducible_and_brackets_mean():
    values = [0.5, -0.2, 1.1, 0.3, -0.6, 0.9, 0.1, 0.4]
    clusters = [0, 0, 1, 1, 2, 2, 3, 3]
    first = cluster_bootstrap_mean_ci(values, clusters, n_boot=500, seed=7)
    second = cluster_bootstrap_mean_ci(values, clusters, n_boot=500, seed=7)
    assert first == second
    assert first["ci_low"] <= first["estimate"] <= first["ci_high"]


def test_cluster_bootstrap_needs_two_clusters():
    result = cluster_bootstrap_mean_ci([1.0, 2.0], [0, 0], n_boot=100)
    assert result["ci_low"] is None and result["clusters"] == 1


# ---------------------------------------------------------------------------
# 收益与入场拦截
# ---------------------------------------------------------------------------


def _row(**kwargs):
    base = {"open": 10.0, "close": 10.0, "adj_factor": 1.0, "volume": 1000.0, "limit_up": 100.0}
    base.update(kwargs)
    return base


def test_forward_return_uses_adj_factor():
    entry = _row(open=10.0, adj_factor=1.0)
    exit_row = _row(close=12.0, adj_factor=2.0)
    # (12*2)/(10*1) - 1 = 140%
    assert forward_return_pct(entry, exit_row) == pytest.approx(140.0)


def test_forward_return_missing_rows():
    assert forward_return_pct(None, _row()) is None
    assert forward_return_pct(_row(), None) is None
    assert forward_return_pct(_row(open=0.0), _row()) is None


@pytest.mark.parametrize(
    ("signal", "entry", "expected"),
    [
        (_row(), None, "no_entry_bar"),
        (_row(close=0.0), _row(), "missing_price"),
        (_row(), _row(open=0.0), "missing_open"),
        (_row(), _row(volume=0.0), "no_volume"),
        (_row(), _row(limit_up=float("nan")), "up_limit_missing"),
        (_row(), _row(open=100.0, limit_up=100.0), "limit_up_open"),
        (_row(adj_factor=0.0), _row(), "missing_adj"),
        (_row(close=10.0), _row(open=12.0), "gap_up"),
        (_row(), _row(), None),
    ],
)
def test_evaluate_event_entry_reasons(signal, entry, expected):
    gate = EventGateConfig(max_entry_gap_pct=8.0)
    assert evaluate_event_entry(signal, entry, gate) == expected


def test_gap_up_can_be_disabled():
    gate = EventGateConfig(max_entry_gap_pct=0.0)
    assert evaluate_event_entry(_row(close=10.0), _row(open=12.0), gate) is None


# ---------------------------------------------------------------------------
# 累加器：留一对照精确性
# ---------------------------------------------------------------------------


CALENDAR = [pd.Timestamp("2019-01-02") + pd.Timedelta(days=offset) for offset in range(8)]


def _make_frame(code: str, slope: float, circ_mv: float = 5e9) -> pd.DataFrame:
    """构造价格递增的合成行情：open 恒为 10，close = 10 + slope × i。"""
    rows = []
    for index, timestamp in enumerate(CALENDAR):
        rows.append(
            {
                "code": code,
                "open": 10.0,
                "close": 10.0 + slope * index,
                "high": 10.0 + slope * index,
                "low": 10.0,
                "adj_factor": 1.0,
                "volume": 1_000_000.0,
                "limit_up": 100.0,
                "circ_mv": circ_mv,
                "is_st": False,
            }
        )
    frame = pd.DataFrame(rows, index=pd.DatetimeIndex(CALENDAR, name="date"))
    return frame


def _options(**kwargs) -> EventStudyOptions:
    payload = {
        "calendar": CALENDAR,
        "start": CALENDAR[0].strftime("%Y-%m-%d"),
        "end": CALENDAR[-1].strftime("%Y-%m-%d"),
        "horizons": (1, 3),
        "universe": lambda code, row: True,
        "cost_bps": 40.2,
        "bootstrap": 0,
    }
    payload.update(kwargs)
    return EventStudyOptions(**payload)


def test_accumulator_leave_one_out_control_is_exact():
    """四只同档股票，事件在 A：对照 = 同日同档其余三只均值（留一）。"""
    accumulator = EventStudyAccumulator(_options(), ["e"])
    signal_date = CALENDAR[0].date()
    for code, slope in (("A", 1.0), ("B", 2.0), ("C", 3.0), ("D", 4.0)):
        accumulator.add_stock(
            _make_frame(code, slope),
            {"e": {signal_date} if code == "A" else set()},
        )
    result = accumulator.finalize()
    event = result.events[0]
    assert event.detected_count == 1
    assert event.tradable_count == 1
    assert event.blocked_count == 0

    by_horizon = {item.horizon: item for item in event.horizons}
    # h=1：A 收益 = 11/10−1 = 10%；对照 = (20%+30%+40%)/3 = 30%
    h1 = by_horizon[1]
    assert h1.n == 1
    assert h1.mean_pct == pytest.approx(10.0)
    assert h1.control_mean_pct == pytest.approx(30.0)
    assert h1.excess_mean_pct == pytest.approx(-20.0)
    # h=3：A = 30%；对照 = (60%+90%+120%)/3 = 90%
    h3 = by_horizon[3]
    assert h3.mean_pct == pytest.approx(30.0)
    assert h3.control_mean_pct == pytest.approx(90.0)
    assert h3.excess_mean_pct == pytest.approx(-60.0)
    # 聚类数 = 信号日区块数
    assert h1.cluster_count == 1


def test_accumulator_blocked_event_is_counted_but_not_tradable():
    accumulator = EventStudyAccumulator(_options(), ["e"])
    frame = _make_frame("A", 1.0)
    frame.loc[CALENDAR[1], "limit_up"] = 10.0  # T+1 开盘即涨停
    accumulator.add_stock(frame, {"e": {CALENDAR[0].date()}})
    result = accumulator.finalize()
    event = result.events[0]
    assert event.detected_count == 1
    assert event.tradable_count == 0
    assert event.blocked_count == 1
    assert event.blocked_by_reason["limit_up_open"] == 1


def test_accumulator_skips_rows_without_circ_mv():
    accumulator = EventStudyAccumulator(_options(), ["e"])
    frame = _make_frame("A", 1.0, circ_mv=float("nan"))
    accumulator.add_stock(frame, {"e": {CALENDAR[0].date()}})
    result = accumulator.finalize()
    assert result.events[0].detected_count == 0
    assert result.pool_observations == 0


def test_accumulator_universe_filter():
    accumulator = EventStudyAccumulator(_options(universe=lambda code, row: code == "A"), ["e"])
    for code, slope in (("A", 1.0), ("B", 2.0)):
        accumulator.add_stock(
            _make_frame(code, slope), {"e": {CALENDAR[0].date()} if code == "B" else set()}
        )
    result = accumulator.finalize()
    assert result.events[0].detected_count == 0  # B 不在池内


def test_accumulator_size_buckets_split_control():
    """不同市值分档互不进入彼此对照。"""
    accumulator = EventStudyAccumulator(_options(), ["e"])
    # A: 50 亿 → 档 3；B/C: 10 亿 → 档 0
    for code, slope, circ in (("A", 1.0, 50e8), ("B", 2.0, 10e8), ("C", 3.0, 10e8)):
        accumulator.add_stock(
            _make_frame(code, slope, circ_mv=circ), {"e": {CALENDAR[0].date()} if code == "A" else set()}
        )
    result = accumulator.finalize()
    h1 = {item.horizon: item for item in result.events[0].horizons}[1]
    # A 所在档只有自己 → 无对照
    assert h1.control_mean_pct is None
    assert h1.excess_mean_pct is None


def test_accumulator_drops_samples_without_exit_bar():
    accumulator = EventStudyAccumulator(_options(horizons=(1, 7)), ["e"])
    frame = _make_frame("A", 1.0)
    # 信号日放在倒数第二天 → h=7 缺退出行情
    accumulator.add_stock(frame, {"e": {CALENDAR[-2].date()}})
    result = accumulator.finalize()
    stats = {item.horizon: item for item in result.events[0].horizons}
    assert stats[1].n == 1 and stats[1].dropped_no_exit == 0
    assert stats[7].n == 0 and stats[7].dropped_no_exit == 1


def test_accumulator_cost_deduction():
    accumulator = EventStudyAccumulator(_options(cost_bps=100.0), ["e"])
    for code, slope in (("A", 1.0), ("B", 2.0)):
        accumulator.add_stock(
            _make_frame(code, slope), {"e": {CALENDAR[0].date()} if code == "A" else set()}
        )
    stats = {item.horizon: item for item in accumulator.finalize().events[0].horizons}[1]
    # 100bp = 1.00%
    assert stats.net_mean_pct == pytest.approx(stats.mean_pct - 1.0, abs=1e-4)


def test_build_calendar_union():
    first = _make_frame("A", 1.0)
    second = _make_frame("B", 1.0).iloc[2:]
    calendar = build_calendar({"A": first, "B": second})
    assert len(calendar) == len(CALENDAR)


# ---------------------------------------------------------------------------
# 初筛门槛
# ---------------------------------------------------------------------------


def _stats(**kwargs) -> EventHorizonStats:
    payload = dict(
        horizon=10,
        n=300,
        dropped_no_exit=0,
        date_count=40,
        mean_pct=1.0,
        median_pct=0.5,
        win_rate=0.55,
        net_mean_pct=0.6,
        control_mean_pct=0.2,
        excess_mean_pct=0.8,
        excess_median_pct=-0.1,
        excess_win_rate=0.45,
        excess_t_stat=2.5,
        excess_p_value=0.01,
        cluster_count=40,
        excess_ex_top5_mean_pct=0.5,
        excess_trimmed_mean_pct=0.7,
        market_excess_mean_pct=0.7,
        market_excess_t_stat=2.3,
        by_year=(
            {"year": "2020", "n": 100, "meanPct": 1.0, "excessMeanPct": 0.9},
            {"year": "2021", "n": 100, "meanPct": 1.0, "excessMeanPct": 0.6},
            {"year": "2022", "n": 100, "meanPct": 1.0, "excessMeanPct": -0.1},
        ),
    )
    payload.update(kwargs)
    return EventHorizonStats(**payload)


def test_gate_passes_when_all_checks_hold():
    # 3 年里有 2 年超额为正 → 2/3 ≈ 0.667 ≥ 0.6，通过
    gate = evaluate_event_gate(_stats(), EventGateOptions())
    assert gate.passed
    assert len(gate.checks) == 7


@pytest.mark.parametrize(
    ("field", "value", "failed_key"),
    [
        ("n", 199, "samples"),
        ("cluster_count", 29, "clusters"),
        ("excess_mean_pct", -0.1, "excess_positive"),
        ("excess_t_stat", 1.9, "t_stat"),
        ("excess_ex_top5_mean_pct", -0.1, "ex_top5"),
        ("net_mean_pct", -0.1, "net_positive"),
    ],
)
def test_gate_single_check_failure(field, value, failed_key):
    gate = evaluate_event_gate(_stats(**{field: value}), EventGateOptions())
    assert not gate.passed
    failed = {check.key for check in gate.checks if not check.passed}
    assert failed_key in failed


def test_gate_year_stability_failure():
    # 3 年只有 1 年为正 → 1/3 < 0.6
    stats = _stats(
        by_year=(
            {"year": "2020", "n": 100, "meanPct": 1.0, "excessMeanPct": 0.9},
            {"year": "2021", "n": 100, "meanPct": 1.0, "excessMeanPct": -0.6},
            {"year": "2022", "n": 100, "meanPct": 1.0, "excessMeanPct": -0.1},
        )
    )
    gate = evaluate_event_gate(stats, EventGateOptions())
    assert not gate.passed
    year_check = next(check for check in gate.checks if check.key == "year_stability")
    assert not year_check.passed


def test_gate_year_stability_ignores_small_years():
    stats = _stats(
        by_year=(
            {"year": "2020", "n": 100, "meanPct": 1.0, "excessMeanPct": 0.9},
            {"year": "2021", "n": 100, "meanPct": 1.0, "excessMeanPct": -0.6},
            {"year": "2022", "n": 5, "meanPct": 1.0, "excessMeanPct": -5.0},  # 样本不足，不计
        )
    )
    # 有效年份 2 个，只有 1 年为正 → 0.5 < 0.6，仍不通过
    gate = evaluate_event_gate(stats, EventGateOptions())
    year_check = next(check for check in gate.checks if check.key == "year_stability")
    assert year_check.actual == "1/2"


# ---------------------------------------------------------------------------
# 事件定义
# ---------------------------------------------------------------------------


def _trend_frame(periods: int = 200, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2020-01-02", periods=periods)
    close = 10 + np.cumsum(rng.normal(0.01, 0.2, periods))
    close = np.maximum(close, 1.0)
    frame = pd.DataFrame(
        {
            "code": "000001",
            "open": close * 0.995,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": rng.integers(1_000_000, 5_000_000, periods).astype(float),
            "amount": rng.integers(1e8, 5e8, periods).astype(float),
            "tradestatus": 1,
            "is_st": False,
            "adj_factor": 1.0,
        },
        index=index,
    )
    frame["pre_close"] = frame["close"].shift(1).fillna(frame["close"])
    return frame


def test_strategy_entry_detector_respects_warmup():
    from src.strategy.events import build_detector

    detector = build_detector("trend_pullback_entry")
    dates = detector.detect("000001", _trend_frame(200))
    warmup_end = _trend_frame(200).index[119].date()
    assert all(item > warmup_end for item in dates)
    assert isinstance(dates, set)


def test_strategy_entry_detector_skips_short_history():
    from src.strategy.events import build_detector

    detector = build_detector("trend_pullback_entry")
    assert detector.detect("000001", _trend_frame(30)) == set()


def test_condition_detector_fires():
    from src.strategy.events import condition_detector

    detector = condition_detector(
        "always", "总是触发", lambda frame: pd.Series(True, index=frame.index), warmup=10
    )
    dates = detector.detect("000001", _trend_frame(30))
    assert len(dates) == 20  # 30 − 10 预热


def test_build_detectors_dedupes():
    from src.strategy.events import build_detectors

    detectors = build_detectors(["trend_pullback_entry", "trend_pullback_entry"])
    assert len(detectors) == 1


def test_unknown_detector_raises():
    from src.strategy.events import build_detector

    with pytest.raises(KeyError):
        build_detector("nope")
