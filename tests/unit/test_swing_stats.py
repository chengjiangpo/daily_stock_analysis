# -*- coding: utf-8 -*-
"""Unit tests for src.validation.stats."""

import math

import numpy as np
import pandas as pd
import pytest

from src.validation.stats import (
    alpha_vs_benchmark,
    block_bootstrap_ci,
    decompose_pnl,
    deflated_sharpe_ratio,
    performance_metrics,
    probability_of_backtest_overfitting,
    trade_metrics,
)

TOL = 1e-9


def test_performance_metrics_cagr_and_drawdown():
    # 252 个日收益，恒定日增长 0.1%
    equity = pd.Series(100.0 * 1.001 ** np.arange(253))
    result = performance_metrics(equity)

    assert result["cagr"] == pytest.approx(1.001**252 - 1.0, abs=TOL)
    assert result["total_return"] == pytest.approx(1.001**252 - 1.0, abs=1e-12)
    assert result["max_drawdown"] == pytest.approx(0.0, abs=TOL)
    assert result["n_days"] == 253.0

    # 手工构造 20% 回撤
    drawdown_equity = pd.Series([100.0, 120.0, 90.0, 110.0])
    drawdown_result = performance_metrics(drawdown_equity)
    assert drawdown_result["max_drawdown"] == pytest.approx(0.25, abs=TOL)
    assert drawdown_result["total_return"] == pytest.approx(0.10, abs=TOL)


def test_performance_metrics_sharpe_analytic():
    daily_returns = np.array([0.01, 0.02, -0.01, 0.015, -0.005])
    equity = pd.Series(np.concatenate([[100.0], 100.0 * np.cumprod(1.0 + daily_returns)]))
    result = performance_metrics(equity)

    expected_sharpe = daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(252)
    expected_vol = daily_returns.std(ddof=1) * math.sqrt(252)
    expected_cagr = (equity.iloc[-1] / equity.iloc[0]) ** (252 / len(daily_returns)) - 1.0

    assert result["sharpe"] == pytest.approx(expected_sharpe, abs=TOL)
    assert result["annual_vol"] == pytest.approx(expected_vol, abs=TOL)
    assert result["cagr"] == pytest.approx(expected_cagr, abs=TOL)
    assert result["calmar"] == pytest.approx(expected_cagr / result["max_drawdown"], abs=TOL)


def test_performance_metrics_edge_cases():
    empty = performance_metrics(pd.Series([], dtype=float))
    single = performance_metrics(pd.Series([100.0]))
    for result in (empty, single):
        assert result["n_days"] == 0.0
        assert result["sharpe"] == 0.0
        assert result["cagr"] == 0.0

    with_nan = performance_metrics(pd.Series([100.0, np.nan, 110.0, np.inf]))
    assert with_nan["n_days"] == 2.0
    assert with_nan["total_return"] == pytest.approx(0.10, abs=TOL)


def test_trade_metrics_basic_math():
    result = trade_metrics([10.0, -5.0, 20.0, -2.0])

    assert result["n_trades"] == 4.0
    assert result["win_rate"] == pytest.approx(0.5, abs=TOL)
    assert result["profit_factor"] == pytest.approx(30.0 / 7.0, abs=TOL)
    assert result["avg_pnl"] == pytest.approx(5.75, abs=TOL)
    assert result["median_pnl"] == pytest.approx(4.0, abs=TOL)
    assert result["expectancy"] == pytest.approx(5.75, abs=TOL)
    assert result["max_consecutive_losses"] == 1.0


def test_trade_metrics_profit_factor_extremes():
    no_losses = trade_metrics([1.0, 2.0, 3.0])
    assert no_losses["profit_factor"] == float("inf")

    all_losses = trade_metrics([-1.0, -2.0, -3.0])
    assert all_losses["profit_factor"] == 0.0

    empty = trade_metrics([])
    assert empty["n_trades"] == 0.0
    assert empty["profit_factor"] == 0.0

    with_nan = trade_metrics([-1.0, np.nan, -2.0, 4.0, -3.0])
    assert with_nan["n_trades"] == 4.0
    assert with_nan["max_consecutive_losses"] == 2.0


def test_block_bootstrap_reproducible_and_conservative():
    rng = np.random.default_rng(7)
    returns = pd.Series(0.0008 + rng.normal(0.0, 0.01, 400))

    first = block_bootstrap_ci(returns, np.mean, n_boot=500, block=20, seed=123)
    second = block_bootstrap_ci(returns, np.mean, n_boot=500, block=20, seed=123)

    assert first == second
    assert first["ci_low"] < first["estimate"]
    assert first["ci_high"] > first["ci_low"]

    short = block_bootstrap_ci(pd.Series([0.01, -0.02, 0.03, 0.0, 0.01]), np.mean, n_boot=50, block=20, seed=1)
    assert math.isfinite(short["estimate"])
    assert math.isfinite(short["ci_low"])
    assert math.isfinite(short["ci_high"])


def test_alpha_vs_benchmark_recovers_parameters():
    rng = np.random.default_rng(11)
    n = 2000
    benchmark = rng.normal(0.0002, 0.01, n)
    noise = rng.normal(0.0, 0.001, n)
    strategy = 0.0005 + 1.2 * benchmark + noise

    result = alpha_vs_benchmark(pd.Series(strategy), pd.Series(benchmark))

    assert result["alpha_annual"] == pytest.approx(0.126, abs=0.02)
    assert result["beta"] == pytest.approx(1.2, abs=0.05)
    assert result["n_days"] == float(n)
    assert result["alpha_t"] > 0
    assert result["ir"] > 0


def test_alpha_vs_benchmark_alignment_edge_cases():
    empty = alpha_vs_benchmark(pd.Series([], dtype=float), pd.Series([], dtype=float))
    assert empty["n_days"] == 0.0
    assert empty["alpha_annual"] == 0.0

    strategy = pd.Series([0.01, 0.02, np.nan], index=[0, 1, 2])
    benchmark = pd.Series([0.01, 0.02, 0.03], index=[1, 2, 3])
    aligned = alpha_vs_benchmark(strategy, benchmark)
    assert aligned["n_days"] == 1.0


def test_deflated_sharpe_ratio_multiple_testing_penalty():
    rng = np.random.default_rng(5)
    returns = pd.Series(rng.normal(0.002, 0.01, 252))

    single = deflated_sharpe_ratio(returns, n_trials=1)
    many = deflated_sharpe_ratio(returns, n_trials=100)

    assert single["dsr"] > 0.95
    assert many["dsr"] < single["dsr"] - 0.05
    assert 0.0 <= many["dsr"] <= 1.0
    assert many["sr_expected_max"] > single["sr_expected_max"]

    with pytest.raises(ValueError):
        deflated_sharpe_ratio(returns, n_trials=0)

    empty = deflated_sharpe_ratio(pd.Series([], dtype=float), n_trials=1)
    assert empty["dsr"] == 0.0
    assert empty["n_days"] == 0.0


def test_probability_of_backtest_overfitting_stable_strategy():
    rng = np.random.default_rng(21)
    n_days = 480
    stable = rng.normal(0.002, 0.001, n_days)
    noise = rng.normal(0.0, 0.01, (n_days, 9))
    matrix = np.column_stack([stable, noise])

    result = probability_of_backtest_overfitting(matrix, n_splits=8)

    assert result["pbo"] < 0.5
    assert result["n_splits"] == 8.0
    assert result["n_combos"] == math.comb(8, 4)


def test_probability_of_backtest_overfitting_random_and_insufficient():
    rng = np.random.default_rng(22)
    random_matrix = rng.normal(0.0, 0.01, (480, 8))
    result = probability_of_backtest_overfitting(random_matrix, n_splits=8)
    assert 0.0 <= result["pbo"] <= 1.0
    assert math.isfinite(result["logit_mean"])

    insufficient = probability_of_backtest_overfitting(np.zeros((10, 3)), n_splits=8)
    assert math.isnan(insufficient["pbo"])
    assert "reason" in insufficient

    one_column = probability_of_backtest_overfitting(np.zeros((100, 1)), n_splits=8)
    assert math.isnan(one_column["pbo"])
    assert "reason" in one_column


def test_decompose_pnl_by_hold_days():
    trades = pd.DataFrame(
        {
            "pnl": [10.0, -5.0, 20.0, -2.0, 15.0, -8.0],
            "hold_days": [1, 2, 3, 5, 10, 25],
            "r_multiple": [1.0, -0.5, 2.0, -0.2, 1.5, -0.8],
        }
    )
    result = decompose_pnl(trades)

    assert result.index.tolist() == ["1", "2-3", "4-5", "6-10", "21+"]
    assert result.loc["1", "n_trades"] == 1
    assert result.loc["1", "win_rate"] == pytest.approx(1.0)
    assert result.loc["1", "total_pnl"] == pytest.approx(10.0)

    assert result.loc["2-3", "n_trades"] == 2
    assert result.loc["2-3", "win_rate"] == pytest.approx(0.5)
    assert result.loc["2-3", "avg_pnl"] == pytest.approx(7.5)
    assert result.loc["2-3", "avg_r_multiple"] == pytest.approx(0.75)

    assert result.loc["4-5", "n_trades"] == 1
    assert result.loc["4-5", "total_pnl"] == pytest.approx(-2.0)
    assert result.loc["21+", "n_trades"] == 1


def test_decompose_pnl_edge_cases():
    empty = decompose_pnl(pd.DataFrame({"pnl": [], "hold_days": []}))
    assert empty.empty
    assert {"n_trades", "win_rate", "avg_pnl", "total_pnl"}.issubset(empty.columns)

    without_r = decompose_pnl(pd.DataFrame({"pnl": [1.0], "hold_days": [2]}))
    assert "avg_r_multiple" not in without_r.columns

    with pytest.raises(ValueError):
        decompose_pnl(pd.DataFrame({"pnl": [1.0]}))
