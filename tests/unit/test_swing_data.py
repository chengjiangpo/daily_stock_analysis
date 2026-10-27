# -*- coding: utf-8 -*-
"""数据层与基准构造测试。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.data.bars import mark_limit_prices, normalize_daily_bars, quality_report, snapshot_hash
from src.data.pool import pool_snapshot
from src.sim.rules import MarketRules
from src.strategy.benchmarks import add_buckets, forward_open_return, sample_baskets


def _frame(rows: int = 30, code: str = "600000", start: str = "2023-01-03") -> pd.DataFrame:
    index = pd.bdate_range(start, periods=rows)
    close = np.linspace(10, 12, rows)
    return pd.DataFrame(
        {
            "code": code,
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000_000.0,
            "amount": close * 1_000_000.0,
            "pre_close": np.r_[close[0], close[:-1]],
            "tradestatus": 1,
            "is_st": 0,
            "adj_factor": 1.0,
            "industry": "银行",
        },
        index=index,
    )


def test_normalize_fills_pre_close_and_types() -> None:
    raw = _frame().reset_index().rename(columns={"index": "date"})
    raw = raw.drop(columns=["pre_close"])
    raw["is_st"] = raw["is_st"].astype(str)
    normalized = normalize_daily_bars(raw, code="600000")
    assert isinstance(normalized.index, pd.DatetimeIndex)
    assert normalized.index.is_monotonic_increasing
    assert normalized["pre_close"].iloc[1] == pytest.approx(normalized["close"].iloc[0])
    assert normalized["is_st"].dtype == bool
    assert normalized["tradestatus"].dtype == int


def test_normalize_rejects_duplicate_dates_and_missing_columns() -> None:
    frame = _frame()
    duplicated = pd.concat([frame, frame.iloc[[0]]]).sort_index()
    normalized = normalize_daily_bars(duplicated, code="600000")
    assert len(normalized) == len(frame)

    with pytest.raises(ValueError):
        normalize_daily_bars(frame.drop(columns=["volume"]), code="600000")


def test_quality_report_detects_bad_ohlc_and_passes_valid() -> None:
    frame = normalize_daily_bars(_frame(), code="600000")
    clean = quality_report(frame, "600000")
    assert clean["passed"], clean

    broken = frame.copy()
    broken.iloc[3, broken.columns.get_loc("high")] = 1.0
    report = quality_report(broken, "600000")
    assert not report["passed"]
    ohlc_check = next(item for item in report["checks"] if item["check_id"] == 2)
    assert ohlc_check["failures"] >= 1


def test_snapshot_hash_is_stable_and_order_independent() -> None:
    first = normalize_daily_bars(_frame(code="600000"), code="600000")
    second = normalize_daily_bars(_frame(code="000001"), code="000001")
    hash_a = snapshot_hash({"600000": first, "000001": second})
    hash_b = snapshot_hash({"000001": second, "600000": first})
    assert hash_a == hash_b
    second.iloc[0, second.columns.get_loc("close")] += 0.01
    assert snapshot_hash({"600000": first, "000001": second}) != hash_a


def test_mark_limit_prices_uses_segments() -> None:
    frame = normalize_daily_bars(_frame(), code="600000")
    marked = mark_limit_prices(frame, "600000", MarketRules.load())
    row = marked.iloc[1]
    expected_up = round(float(row["pre_close"]) * 1.1 + 1e-9, 2)
    assert row["limit_up"] == pytest.approx(expected_up, abs=0.011)
    assert row["limit_down"] < row["pre_close"]


def test_pool_snapshot_excludes_st_and_uses_size() -> None:
    frame = normalize_daily_bars(_frame(), code="600000")
    frame["in_pool"] = True
    frame["avg_amount_20d"] = 100_000_000.0
    frame["volatility_20d"] = 0.3
    st_frame = frame.copy()
    st_frame["in_pool"] = False
    snapshot = pool_snapshot({"600000": frame, "600001": st_frame}, frame.index[-1])
    assert list(snapshot.index) == ["600000"]
    assert snapshot.loc["600000", "size"] == pytest.approx(100_000_000.0)


def test_b2_basket_sampling_is_reproducible_and_excludes_target() -> None:
    snapshot = pd.DataFrame(
        {
            "size": np.arange(20, dtype=float),
            "volatility": np.arange(20, dtype=float) / 10,
            "industry": ["银行"] * 10 + ["医药"] * 10,
        },
        index=[f"6000{i:02d}" for i in range(20)],
    )
    snapshot = add_buckets(snapshot, q=5)
    first = sample_baskets(snapshot, 2, 2, "银行", exclude=["600005"], n=5, sims=20, seed=7)
    second = sample_baskets(snapshot, 2, 2, "银行", exclude=["600005"], n=5, sims=20, seed=7)
    assert first == second
    assert all("600005" not in basket for basket in first)
    assert all(len(basket) <= 5 for basket in first)


def test_forward_open_return_uses_open_prices() -> None:
    frame = normalize_daily_bars(_frame(), code="600000")
    entry = frame.index[5].date()
    exit_ = frame.index[10].date()
    expected = float(frame.iloc[10]["open"]) / float(frame.iloc[5]["open"]) - 1.0
    assert forward_open_return(frame, entry, exit_) == pytest.approx(expected)
    assert forward_open_return(frame, date(2030, 1, 1), date(2030, 2, 1)) is None
