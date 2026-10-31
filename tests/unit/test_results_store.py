# -*- coding: utf-8 -*-
"""回测结果落库（V3.0 I-4）单测。

覆盖：
- 9 张表的建表与幂等重写；
- 元数据/绩效指标抽取；
- 读回与 CSV 内容一致（行数 + 关键字段）；
- 股票池共享表按日期区间替换；
- list_runs / delete_run。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.data import results_store
from src.data.results_store import (
    PoolDailyRow,
    RunPayload,
    delete_run,
    list_runs,
    load_run,
    make_results_engine,
    ensure_results_schema,
    save_pool_daily,
    save_run,
    table_counts,
)


@pytest.fixture()
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'results.db'}"


def _equity() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
            "cash": [100_000.0, 90_000.0, 95_000.0],
            "market_value": [0.0, 10_000.0, 12_000.0],
            "equity": [100_000.0, 100_000.0, 107_000.0],
            "drawdown": [0.0, 0.0, 0.0],
            "positions": [0, 1, 1],
            "exposure": [0.0, 0.1, 0.11],
        }
    )


def _signals() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "signal_date": date(2024, 1, 2),
                "code": "000001",
                "strategy": "trend_pullback_restart",
                "rule_version": "v1",
                "score": 0.62,
                "passed": True,
                "entry_ref": 10.0,
                "stop_price": 9.2,
                "take_profit": 11.6,
            }
        ]
    )


def _orders() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "order_id": "20240103-buy-000001",
                "code": "000001",
                "side": "buy",
                "signal_date": date(2024, 1, 2),
                "order_date": date(2024, 1, 3),
                "quantity": 1000,
                "ref_price": 10.0,
                "fill_qty": 1000,
                "fill_price": 10.01,
                "fees": 5.0,
                "slippage": 10.0,
                "status": "filled",
                "reject_reason": "",
                "score": 0.62,
            },
            {
                "order_id": "20240104-buy-000002-skipped",
                "code": "000002",
                "side": "buy",
                "signal_date": date(2024, 1, 3),
                "order_date": date(2024, 1, 4),
                "quantity": 0,
                "ref_price": 20.0,
                "fill_qty": 0,
                "fill_price": None,
                "fees": 0.0,
                "slippage": 0.0,
                "status": "rejected",
                "reject_reason": "limit_up_open",
                "score": 0.4,
            },
        ]
    )


def _fills() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "order_id": "20240103-buy-000001",
                "code": "000001",
                "side": "buy",
                "trade_date": date(2024, 1, 3),
                "price": 10.01,
                "quantity": 1000,
                "fees": 5.0,
                "slippage": 10.0,
                "reason": "",
            }
        ]
    )


def _positions() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": date(2024, 1, 3),
                "code": "000001",
                "quantity": 1000,
                "sellable_qty": 0,
                "avg_cost": 10.01,
                "close": 10.05,
                "market_value": 10_050.0,
                "stop_price": 9.2,
                "take_profit": 11.6,
                "hold_days": 0,
                "industry": "银行",
            }
        ]
    )


def _trades() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_id": "T1",
                "code": "000001",
                "signal_date": date(2024, 1, 2),
                "entry_date": date(2024, 1, 3),
                "entry_price": 10.01,
                "exit_date": date(2024, 1, 4),
                "exit_price": 10.7,
                "quantity": 1000,
                "r_multiple": 0.9,
                "fees": 12.0,
                "pnl": 678.0,
                "exit_reason": "take_profit",
                "hold_days": 1,
                "entry_reason": "",
                "industry": "银行",
            }
        ]
    )


def _payload(run_id: str = "bt-test-001") -> RunPayload:
    metadata = {
        "run_id": run_id,
        "strategy": "trend_pullback_restart",
        "cost_tier": "base",
        "period": ["2024-01-01", "2024-12-31"],
        "data_hash": "a" * 64,
        "git_commit": "b" * 40,
        "mode": "signal",
        "params": {"trend": {"ma_fast": 20}},
        "engine": {"rule_version": "v1", "codes": 3, "days": 3, "aum": 1e8, "trades": 1,
                   "final_equity": 107_000.0},
        "created_at": "2024-12-31T23:00:00",
    }
    performance = {
        "total_return": 0.07,
        "cagr": 0.07,
        "annual_vol": 0.2,
        "sharpe": 0.35,
        "max_drawdown": 0.0,
    }
    trade_metrics = {"n_trades": 1, "win_rate": 1.0, "profit_factor": 1.0}
    return RunPayload(
        run_id=run_id,
        metadata=metadata,
        performance=performance,
        trade_metrics=trade_metrics,
        equity=_equity(),
        signals=_signals(),
        orders=_orders(),
        fills=_fills(),
        positions=_positions(),
        trades=_trades(),
        diagnostics=pd.DataFrame(),
    )


def test_save_and_load_roundtrip(sqlite_url: str):
    counts = save_run(_payload(), url=sqlite_url)
    assert counts["runs"] == 1
    assert counts["portfolio_daily"] == 3
    assert counts["signals"] == 1
    assert counts["orders"] == 2
    assert counts["fills"] == 1
    assert counts["positions"] == 1
    assert counts["trades"] == 1

    stored = load_run("bt-test-001", url=sqlite_url)
    assert stored is not None
    assert stored.metadata["strategy"] == "trend_pullback_restart"
    assert stored.performance["total_return"] == pytest.approx(0.07)
    assert len(stored.frame("portfolio_daily")) == 3
    assert len(stored.frame("orders")) == 2
    assert len(stored.frame("trades")) == 1
    # 自增主键与 run_id 不应泄漏回读结果
    assert "id" not in stored.frame("fills").columns
    assert "run_id" not in stored.frame("signals").columns


def test_run_row_metrics_are_extracted(sqlite_url: str):
    save_run(_payload(), url=sqlite_url)
    runs = list_runs(url=sqlite_url)
    assert len(runs) == 1
    run = runs[0]
    assert run["run_id"] == "bt-test-001"
    assert run["n_trades"] == 1
    assert run["total_return"] == pytest.approx(0.07)
    assert run["period"] == ["2024-01-01", "2024-12-31"]
    assert run["git_commit"] == "b" * 8


def test_resave_is_idempotent(sqlite_url: str):
    save_run(_payload(), url=sqlite_url)
    first = table_counts(url=sqlite_url)
    save_run(_payload(), url=sqlite_url)
    second = table_counts(url=sqlite_url)
    assert first == second, (first, second)


def test_resave_with_fewer_rows_removes_stale_rows(sqlite_url: str):
    payload = _payload()
    save_run(payload, url=sqlite_url)
    shrunk = _payload()
    shrunk.orders = shrunk.orders.iloc[:1]
    save_run(shrunk, url=sqlite_url)
    stored = load_run("bt-test-001", url=sqlite_url)
    assert len(stored.frame("orders")) == 1


def test_delete_run(sqlite_url: str):
    save_run(_payload(), url=sqlite_url)
    assert delete_run("bt-test-001", url=sqlite_url) is True
    assert load_run("bt-test-001", url=sqlite_url) is None
    assert delete_run("bt-test-001", url=sqlite_url) is False
    assert table_counts(url=sqlite_url)["orders"] == 0


def test_load_missing_run_returns_none(sqlite_url: str):
    assert load_run("nope", url=sqlite_url) is None


def test_all_nine_tables_created(sqlite_url: str):
    engine = make_results_engine(sqlite_url, None)
    ensure_results_schema(engine)
    from sqlalchemy import inspect

    tables = set(inspect(engine).get_table_names())
    expected = {
        "swing_backtest_runs",
        "swing_pool_daily",
        "swing_daily_signals",
        "swing_orders",
        "swing_fills",
        "swing_positions_daily",
        "swing_portfolio_daily",
        "swing_trade_records",
        "swing_bars_daily",
    }
    # swing_bars_daily 属于 bars_store（若同库则由其 ensure_schema 建）；此处只断言本模块的 8 张
    assert expected - {"swing_bars_daily"} <= tables


def test_save_pool_daily_replaces_range(sqlite_url: str):
    index = pd.bdate_range("2024-01-02", periods=3)
    frames = {
        "000001": pd.DataFrame(
            {
                "open": [10.0, 10.1, 10.2],
                "high": [10.2, 10.3, 10.4],
                "low": [9.9, 10.0, 10.1],
                "close": [10.1, 10.2, 10.3],
                "volume": [1e6, 1e6, 1e6],
                "amount": [1e8, 1e8, 1e8],
                "pre_close": [10.0, 10.1, 10.2],
                "tradestatus": [1, 1, 1],
                "is_st": [False, False, False],
                "adj_factor": [1.0, 1.0, 1.0],
            },
            index=index,
        )
    }
    written = save_pool_daily(frames, url=sqlite_url)
    assert written == 3
    written_again = save_pool_daily(frames, url=sqlite_url)
    assert written_again == 3
    # 幂等：同区间重写不累积
    assert table_counts(url=sqlite_url)["pool_daily"] == 3


def test_env_resolution_prefers_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("SWING_RESULTS_DATABASE_URL", f"sqlite:///{tmp_path / 'env.db'}")
    monkeypatch.setenv("SWING_BARS_DATABASE_URL", f"sqlite:///{tmp_path / 'bars.db'}")
    assert "env.db" in results_store.resolve_results_url()
    assert "explicit.db" in results_store.resolve_results_url(f"sqlite:///{tmp_path / 'explicit.db'}")


def test_env_resolution_falls_back_to_bars(monkeypatch, tmp_path):
    monkeypatch.delenv("SWING_RESULTS_DATABASE_URL", raising=False)
    monkeypatch.setenv("SWING_BARS_DATABASE_URL", f"sqlite:///{tmp_path / 'bars.db'}")
    assert "bars.db" in results_store.resolve_results_url()


def test_schema_resolution(monkeypatch):
    monkeypatch.delenv("SWING_RESULTS_SCHEMA", raising=False)
    monkeypatch.setenv("SWING_BARS_SCHEMA", "swing_bars")
    assert results_store.resolve_results_schema() == "swing_bars"
    assert results_store.resolve_results_schema("other") == "other"
