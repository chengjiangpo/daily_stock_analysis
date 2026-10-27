# -*- coding: utf-8 -*-
"""回测引擎集成测试（合成行情，覆盖方案 §12.2 的关键场景）。"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from src.data.fixtures import build_trend_pullback_frame
from src.sim.engine import BacktestEngine
from src.sim.fees import FeeModel
from src.sim.portfolio import PortfolioConfig
from src.strategy import build_strategy

FLAT_DAYS = 30
UP_DAYS = 60
DROP_DAYS = 5
BASE_DAYS = 4


def _build_series(
    continuation_days: int = 31,
    volume: float = 8_000_000.0,
    mutate_next_open: bool = False,
    suspend_next_day: bool = False,
) -> tuple[pd.DataFrame, pd.Timestamp]:
    return build_trend_pullback_frame(
        continuation_days=continuation_days,
        volume=volume,
        mutate_next_open=mutate_next_open,
        suspend_next_day=suspend_next_day,
    )


def _run(frame: pd.DataFrame) -> object:
    strategy = build_strategy("trend_pullback_restart")
    engine = BacktestEngine(
        strategy,
        {"600000": frame},
        FeeModel.load(cost_tier="base"),
        PortfolioConfig(aum=Decimal("300000")),
    )
    return engine.run()


def test_signal_entry_and_time_exit_flow() -> None:
    frame, signal_day = _build_series()
    strategy = build_strategy("trend_pullback_restart")
    prepared = strategy.prepare_and_signal(frame)
    assert bool(prepared.loc[signal_day, "entry"]) is True
    assert len(
        [column for column in prepared.columns if column.startswith("c_") and not bool(prepared.loc[signal_day, column])]
    ) == 0

    result = _run(frame)
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    expected_entry_day = frame.index[frame.index.get_loc(signal_day) + 1].date()
    assert trade["entry_date"] == expected_entry_day
    assert trade["exit_reason"] == "time_exit"
    assert trade["quantity"] % 100 == 0
    assert float(trade["pnl"]) == pytest.approx(1795.98, abs=0.02)

    assert result.metadata["trades"] == 1
    assert float(result.equity.iloc[-1]["equity"]) == pytest.approx(301795.98, abs=0.02)
    assert float(result.equity.iloc[-1]["cash"]) == pytest.approx(301795.98, abs=0.02)
    assert result.equity.iloc[-1]["positions"] == 0
    assert (result.equity["equity"] > 0).all()


def test_same_day_buy_is_not_sellable_and_entry_price_has_one_tick_slippage() -> None:
    frame, signal_day = _build_series()
    result = _run(frame)
    entry_order = result.orders[(result.orders["side"] == "buy") & (result.orders["status"] == "filled")].iloc[0]
    entry_ts = pd.Timestamp(entry_order["order_date"])
    expected_price = round(float(frame.loc[entry_ts, "open"]) + 0.01, 2)
    assert float(entry_order["fill_price"]) == pytest.approx(expected_price, abs=1e-9)
    # 买入当日不产生卖出成交
    entry_sells = result.fills[(result.fills["side"] == "sell") & (result.fills["trade_date"] == entry_order["order_date"])]
    assert entry_sells.empty


def test_one_word_limit_up_blocks_entry() -> None:
    frame, signal_day = _build_series(mutate_next_open=True)
    result = _run(frame)
    assert result.trades.empty
    rejected = result.orders[result.orders["status"] == "rejected"]
    assert not rejected.empty
    assert "open_at_limit_up" in set(rejected["reject_reason"])
    assert float(result.equity.iloc[-1]["equity"]) == pytest.approx(300000.0, abs=1e-6)


def test_suspension_blocks_entry() -> None:
    frame, _ = _build_series(suspend_next_day=True)
    result = _run(frame)
    assert result.trades.empty
    rejected = result.orders[result.orders["status"] == "rejected"]
    assert "suspended" in set(rejected["reject_reason"])


def test_repeat_run_is_identical() -> None:
    frame, _ = _build_series()
    first = _run(frame)
    second = _run(frame)
    pd.testing.assert_frame_equal(first.equity, second.equity)
    pd.testing.assert_frame_equal(first.trades, second.trades)
    pd.testing.assert_frame_equal(first.orders, second.orders)


def test_equity_reconstructs_from_cash_and_pnl() -> None:
    frame, _ = _build_series()
    result = _run(frame)
    total_pnl = float(result.trades["pnl"].sum())
    final_equity = float(result.equity.iloc[-1]["equity"])
    assert final_equity == pytest.approx(300000.0 + total_pnl, abs=0.02)
    # orders → fills → trades 数量一致
    fills = result.fills
    assert fills["quantity"].sum() > 0
    assert len(result.orders[result.orders["status"] == "filled"]) == len(fills)


def test_gap_through_stop_fills_at_open_not_stop_price() -> None:
    frame, signal_day = _build_series()
    entry_pos = frame.index.get_loc(signal_day) + 1
    stop_price = float(frame.loc[signal_day, "close"]) * 0.92
    crash_pos = entry_pos + 2
    frame.iloc[crash_pos, frame.columns.get_loc("open")] = stop_price + 0.4
    frame.iloc[crash_pos, frame.columns.get_loc("high")] = stop_price + 0.6
    frame.iloc[crash_pos, frame.columns.get_loc("low")] = stop_price - 1.0
    frame.iloc[crash_pos, frame.columns.get_loc("close")] = stop_price - 0.5
    gap_pos = crash_pos + 1
    # 跌停价 = 前收 * 0.9；开盘直接跌停但仍非一字（high > limit_down）
    gap_open = round(float(frame.iloc[crash_pos]["close"]) * 0.90, 2)
    frame.iloc[gap_pos, frame.columns.get_loc("open")] = gap_open
    frame.iloc[gap_pos, frame.columns.get_loc("high")] = gap_open + 0.3
    frame.iloc[gap_pos, frame.columns.get_loc("low")] = gap_open
    frame.iloc[gap_pos, frame.columns.get_loc("close")] = gap_open + 0.2
    for position in range(crash_pos + 1, len(frame)):
        frame.iloc[position, frame.columns.get_loc("pre_close")] = frame.iloc[position - 1]["close"]

    result = _run(frame)
    trade = result.trades.iloc[0]
    assert trade["exit_reason"] == "hard_stop"
    assert float(trade["exit_price"]) == pytest.approx(gap_open, abs=0.011)
    assert float(trade["pnl"]) < 0


def test_suspension_freezes_position_valuation() -> None:
    frame, signal_day = _build_series()
    entry_pos = frame.index.get_loc(signal_day) + 1
    last_close = float(frame.iloc[entry_pos]["close"])
    for offset in range(1, 4):
        position = entry_pos + offset
        for column in ("open", "high", "low", "close"):
            frame.iloc[position, frame.columns.get_loc(column)] = last_close
        frame.iloc[position, frame.columns.get_loc("tradestatus")] = 0

    result = _run(frame)
    suspended_dates = [frame.index[entry_pos + offset].date() for offset in range(1, 4)]
    fills_during = result.fills[result.fills["trade_date"].isin(suspended_dates)]
    assert fills_during.empty
    equity = result.equity[pd.to_datetime(result.equity["trade_date"]).dt.date.isin(suspended_dates)]
    assert (equity["market_value"] > 0).all()
    assert equity["market_value"].nunique() == 1
    assert not result.trades.empty


def test_same_day_signals_limited_to_two_entries() -> None:
    frames = {}
    for offset in range(6):
        code = f"6000{offset:02d}"
        frame, _ = build_trend_pullback_frame(code=code, continuation_days=31)
        frames[code] = frame
    strategy = build_strategy("trend_pullback_restart")
    engine = BacktestEngine(
        strategy,
        frames,
        FeeModel.load(cost_tier="base"),
        PortfolioConfig(aum=Decimal("300000")),
    )
    result = engine.run()
    buy_fills = result.fills[result.fills["side"] == "buy"]
    entry_days = pd.to_datetime(buy_fills["trade_date"]).dt.date.unique()
    assert len(entry_days) == 1
    assert len(buy_fills) == 2
    assert set(buy_fills["code"]) == {"600000", "600001"}


def test_unconstrained_signal_level_takes_all_signals() -> None:
    frames = {}
    for offset in range(6):
        code = f"6000{offset:02d}"
        frame, _ = build_trend_pullback_frame(code=code, continuation_days=31)
        frames[code] = frame
    strategy = build_strategy("trend_pullback_restart")
    engine = BacktestEngine(
        strategy,
        frames,
        FeeModel.load(cost_tier="base"),
        PortfolioConfig(aum=Decimal("100000000")),
        constrained=False,
        fixed_notional=Decimal("60000"),
    )
    result = engine.run()
    buy_fills = result.fills[result.fills["side"] == "buy"]
    entry_days = pd.to_datetime(buy_fills["trade_date"]).dt.date.unique()
    assert len(entry_days) == 1
    assert len(buy_fills) == 6
    assert set(buy_fills["code"]) == {f"6000{offset:02d}" for offset in range(6)}
