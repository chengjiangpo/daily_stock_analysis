# -*- coding: utf-8 -*-
"""市值截面数据与 B2 修正口径测试（V3.0 §4.4/§4.5）。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.data.market_caps import (
    CoverageReport,
    MatchedControlConfig,
    attach_market_caps,
    bucket_of_circ_mv,
    load_market_caps,
)
from src.strategy.benchmarks import MatchedControlBench, entry_buyable


def _caps_frame(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime([r[0] for r in rows]),
            "code": [r[1] for r in rows],
            "circ_mv": [r[2] for r in rows],
            "total_mv": [r[2] for r in rows],
            "adj_factor": 1.0,
            "up_limit": np.nan,
            "down_limit": np.nan,
            "is_st": False,
        }
    )


def _panel(date_str: str, codes: list[str], sizes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime([date_str] * len(codes)),
            "code": codes,
            "size": sizes,
            "volatility": [0.3] * len(codes),
            "industry": ["银行"] * len(codes),
        }
    )


def _bar_frame(
    code: str,
    dates: list[str],
    opens: list[float],
    *,
    pre_closes: list[float] | None = None,
    tradestatus: int = 1,
    limit_up: float | None = None,
) -> pd.DataFrame:
    n = len(dates)
    close = [o * 1.01 for o in opens]
    pre = pre_closes or [opens[0] * 0.99] + [opens[i - 1] for i in range(1, n)]
    return pd.DataFrame(
        {
            "code": code,
            "open": opens,
            "high": [max(o, c) * 1.02 for o, c in zip(opens, close)],
            "low": [min(o, c) * 0.98 for o, c in zip(opens, close)],
            "close": close,
            "pre_close": pre,
            "volume": [1e6] * n,
            "amount": [1e7] * n,
            "tradestatus": tradestatus,
            "is_st": 0,
            "adj_factor": 1.0,
            "limit_up": [limit_up if limit_up is not None else np.nan] * n,
        },
        index=pd.to_datetime(dates),
    )


# ---------------------------------------------------------------------------
# market_caps
# ---------------------------------------------------------------------------


def test_bucket_of_circ_mv_edges_are_yi_and_half_open() -> None:
    edges = (20.0, 30.0, 45.0, 70.0)
    assert bucket_of_circ_mv(15e8, edges) == 0  # <20 亿
    assert bucket_of_circ_mv(20e8, edges) == 1  # [20, 30)
    assert bucket_of_circ_mv(29.99e8, edges) == 1
    assert bucket_of_circ_mv(30e8, edges) == 2
    assert bucket_of_circ_mv(70e8, edges) == 4  # >=70 亿
    assert bucket_of_circ_mv(150e8, edges) == 4


def test_load_market_caps_requires_cache() -> None:
    with pytest.raises(FileNotFoundError):
        load_market_caps("/tmp/__nonexistent_caps_dir__")


def test_attach_market_caps_full_coverage_uses_circ_mv_bucket() -> None:
    caps = _caps_frame(
        [
            ("2023-06-01", "600001", 15e8),
            ("2023-06-01", "600002", 25e8),
            ("2023-06-01", "600003", 80e8),
        ]
    )
    panel = _panel("2023-06-01", ["600001", "600002", "600003"], [1e7, 2e7, 3e7])
    merged, report = attach_market_caps(panel, caps)
    assert report.dates_full_coverage == 1
    assert report.dates_fallback_proxy == 0
    by_code = merged.set_index("code")
    assert by_code.loc["600001", "size_bucket"] == 0
    assert by_code.loc["600002", "size_bucket"] == 1
    assert by_code.loc["600003", "size_bucket"] == 4
    assert (merged["size_source"] == "circ_mv").all()


def test_attach_market_caps_low_coverage_falls_back_to_proxy_quantile() -> None:
    # 只有 1/3 有市值 → 覆盖率 0.33 < 0.9 → 该日回退代理分位桶
    caps = _caps_frame([("2023-06-01", "600001", 15e8)])
    panel = _panel("2023-06-01", ["600001", "600002", "600003"], [1e7, 2e7, 3e7])
    merged, report = attach_market_caps(panel, caps)
    assert report.dates_full_coverage == 0
    assert report.dates_fallback_proxy == 1
    assert (merged["size_source"] == "proxy_quantile").all()
    # 分位桶 0..4，缺失代理值 -> -1；这里三只均有代理值
    assert set(merged["size_bucket"]) <= {0, 1, 2, 3, 4}


def test_matcher_control_config_loads_edges_from_yaml() -> None:
    config = MatchedControlConfig.load()
    assert config.size_edges_yi == (20.0, 30.0, 45.0, 70.0)
    assert 0 < config.min_coverage <= 1
    assert config.entry_gap_max > 0


# ---------------------------------------------------------------------------
# entry_buyable / MatchedControlBench
# ---------------------------------------------------------------------------


def test_entry_buyable_rejects_suspended_missing_gap_and_limit_up() -> None:
    ok = _bar_frame("600001", ["2023-06-01", "2023-06-02"], [10.0, 10.2])
    assert entry_buyable(ok, date(2023, 6, 2)) is True

    # 停牌日（无行情记录）
    assert entry_buyable(ok, date(2023, 6, 5)) is False

    suspended = _bar_frame("600001", ["2023-06-02"], [10.0], tradestatus=0)
    assert entry_buyable(suspended, date(2023, 6, 2)) is False

    # 开盘涨停买不到
    limit = _bar_frame("600001", ["2023-06-02"], [11.0], pre_closes=[10.0], limit_up=11.0)
    assert entry_buyable(limit, date(2023, 6, 2)) is False

    # 跳空 > 3%
    gap = _bar_frame("600001", ["2023-06-02"], [10.5], pre_closes=[10.0])
    assert entry_buyable(gap, date(2023, 6, 2), gap_max=0.03) is False
    assert entry_buyable(gap, date(2023, 6, 2), gap_max=0.06) is True


def test_entry_buyable_computes_limit_from_rules_when_column_missing() -> None:
    from src.sim.rules import MarketRules

    rules = MarketRules.load()
    # frames 不带 limit_up 列（MVP BaoStock 路径）：pre_close=10 → 主板 10% 涨停 11.0
    frame = _bar_frame("600001", ["2023-06-02"], [11.0], pre_closes=[10.0]).drop(columns=["limit_up"])
    assert "limit_up" not in frame.columns
    assert entry_buyable(frame, date(2023, 6, 2), code="600001", rules=rules) is False

    frame_ok = _bar_frame("600001", ["2023-06-02"], [10.2], pre_closes=[10.0]).drop(columns=["limit_up"])
    assert entry_buyable(frame_ok, date(2023, 6, 2), code="600001", rules=rules) is True

    # 无 rules 且无列：跳过涨停检查，仅按跳空拦截（口径降级）。
    # 主板涨停开盘（10%/5%）必然伴随 >3% 跳空，跳空拦截仍会拒绝：
    assert entry_buyable(frame, date(2023, 6, 2)) is False
    # 而无涨停且跳空小的场景不受影响：
    frame_plain = _bar_frame("600001", ["2023-06-02"], [10.1], pre_closes=[10.0]).drop(columns=["limit_up"])
    assert entry_buyable(frame_plain, date(2023, 6, 2)) is True

    # 列存在但为 NaN：同样回退 rules 计算
    frame_nan = _bar_frame("600001", ["2023-06-02"], [11.0], pre_closes=[10.0])
    assert pd.isna(frame_nan["limit_up"].iloc[0])
    assert entry_buyable(frame_nan, date(2023, 6, 2), code="600001", rules=rules) is False


def _matched_setup() -> tuple[dict, pd.DataFrame]:
    """两只小市值（<20 亿）+ 一只大市值（>=70 亿），事件股 600001。"""
    caps = _caps_frame(
        [
            ("2023-06-01", "600001", 15e8),
            ("2023-06-01", "600002", 18e8),
            ("2023-06-01", "600003", 16e8),
            ("2023-06-01", "600004", 90e8),
        ]
    )
    panel = _panel(
        "2023-06-01",
        ["600001", "600002", "600003", "600004"],
        [1e7, 2e7, 3e7, 4e7],
    )
    merged, _report = attach_market_caps(panel, caps)
    frames = {
        "600001": _bar_frame("600001", ["2023-06-02", "2023-06-09"], [10.0, 10.5]),
        # 同桶对照：600002 +5%，600003 +2%（600003 开盘涨停 → 不可买，被剔除）
        "600002": _bar_frame("600002", ["2023-06-02", "2023-06-09"], [10.0, 10.5]),
        "600003": _bar_frame(
            "600003", ["2023-06-02", "2023-06-09"], [10.0, 10.2], limit_up=10.0
        ),
        # 大市值（不同桶）：+10%，只应进入市场口径对照
        "600004": _bar_frame("600004", ["2023-06-02", "2023-06-09"], [10.0, 11.0]),
    }
    # 600003 的开盘涨停需要落在 entry 日；重构造使其 entry 日开盘=涨停
    frames["600003"] = _bar_frame("600003", ["2023-06-02", "2023-06-09"], [10.0, 10.2])
    frames["600003"]["limit_up"] = [10.0, np.nan]
    return frames, merged


def test_matched_control_loo_and_bucket_and_market() -> None:
    frames, merged = _matched_setup()
    bench = MatchedControlBench(frames, merged)
    # 事件股 600001 自身收益 +5%
    result = bench.benchmark(
        "600001", date(2023, 6, 1), date(2023, 6, 2), date(2023, 6, 9), size_bucket=0
    )
    # 同桶可买对照 = {600002}（600003 开盘涨停被剔除；事件股留一剔除）→ 对照 = +5%
    assert result["control_n"] == 1
    assert result["control_mean"] == pytest.approx(0.05)
    # 市场口径 = {600002, 600004} → (+5% + 10%) / 2 = 7.5%
    assert result["market_n"] == 2
    assert result["market_mean"] == pytest.approx(0.075)
    assert result["size_source"] == "circ_mv"


def test_matched_control_empty_bucket_returns_none() -> None:
    frames, merged = _matched_setup()
    bench = MatchedControlBench(frames, merged)
    # 事件股在大市值桶（实际在桶 0）——查一个无成员的桶
    result = bench.benchmark(
        "600004", date(2023, 6, 1), date(2023, 6, 2), date(2023, 6, 9), size_bucket=2
    )
    assert result["control_mean"] is None
    assert result["control_n"] == 0
    # 市场口径仍应可用
    assert result["market_mean"] is not None
