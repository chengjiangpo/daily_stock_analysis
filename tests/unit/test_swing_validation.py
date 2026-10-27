# -*- coding: utf-8 -*-
"""验证层与运行服务测试。"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.data.fixtures import build_fixture_frames, build_trend_pullback_frame
from src.services.backtest_run_service import load_portfolio_config, run_backtest
from src.validation.split import DEFAULT_SPLIT, make_split, split_frames
from src.validation.walk_forward import assert_non_overlapping, generate_windows


def test_default_split_is_strictly_increasing() -> None:
    DEFAULT_SPLIT.validate()
    assert DEFAULT_SPLIT.development_end < DEFAULT_SPLIT.validation_start
    assert DEFAULT_SPLIT.validation_end < DEFAULT_SPLIT.test_start
    with pytest.raises(ValueError):
        make_split(development_end=date(2024, 12, 31), validation_end=date(2023, 1, 1))


def test_walk_forward_windows_do_not_overlap() -> None:
    windows = generate_windows(date(2016, 1, 1), date(2024, 12, 31), dev_years=3, val_years=1,
                               test_months=6, step_months=6)
    assert windows
    assert_non_overlapping(windows)
    for window in windows:
        assert window.dev_end < window.val_start
        assert window.val_end < window.test_start
    with pytest.raises(ValueError):
        generate_windows(date(2016, 1, 1), date(2024, 1, 1), dev_years=0)


def test_split_frames_keeps_ranges() -> None:
    frame, _ = build_trend_pullback_frame()
    split = make_split(development_start=frame.index.min().date(),
                       development_end=date(2023, 3, 31),
                       validation_end=date(2023, 5, 31),
                       test_end=date(2023, 12, 31))
    parts = split_frames({"600000": frame}, split)
    assert parts["development"]["600000"].index.max().date() <= split.development_end
    assert parts["test"]["600000"].index.min().date() >= split.test_start


def test_portfolio_config_loaded_from_yaml() -> None:
    config = load_portfolio_config()
    assert config.max_positions == 5
    assert float(config.aum) == pytest.approx(300000.0)
    assert config.max_position_pct == pytest.approx(0.25)
    assert config.risk_per_trade == pytest.approx(0.01)


def test_run_backtest_service_writes_report_files(tmp_path) -> None:
    frames = build_fixture_frames()
    outputs = run_backtest(
        "trend_pullback_restart",
        frames,
        date(2023, 1, 1),
        date(2023, 12, 31),
        cost_tier="base",
        out_dir=tmp_path,
    )
    for name in (
        "daily_equity.csv",
        "orders.csv",
        "trades.csv",
        "signals.csv",
        "diagnostics.csv",
        "daily_positions.csv",
        "run_metadata.json",
        "performance_metrics.json",
        "summary.md",
        "monthly_returns.csv",
        "yearly_returns.csv",
    ):
        assert (tmp_path / name).exists(), name
    metadata = outputs.metadata
    assert len(metadata["data_hash"]) == 64
    assert metadata["engine"]["trades"] >= 1
    equity = pd.read_csv(tmp_path / "daily_equity.csv")
    assert (equity["equity"] > 0).all()


def test_acceptance_classification_levels() -> None:
    from src.validation.verdict import AcceptanceInput, classify_acceptance

    good = AcceptanceInput(
        n_trades=400,
        n_years=5,
        alpha_annual=0.05,
        alpha_ci_low=0.01,
        information_ratio=0.8,
        profit_factor_stress=1.3,
        max_drawdown=0.12,
        positive_year_ratio=0.75,
        residual_alpha=0.02,
    )
    assert classify_acceptance(good).level == "SIMULATE"

    watch = AcceptanceInput(**{**good.__dict__, "n_trades": 120})
    assert classify_acceptance(watch).level == "WATCH"

    reject = AcceptanceInput(**{**good.__dict__, "alpha_annual": -0.02})
    assert classify_acceptance(reject).level == "REJECT"

    broken = AcceptanceInput(**{**good.__dict__, "execution_passed": False})
    assert classify_acceptance(broken).level == "REJECT"


def test_parameter_grid_expands_and_budget_guard() -> None:
    from src.validation.sensitivity import check_experiment_budget, expand_grid

    combos = expand_grid({"a": 1}, {"b": [1, 2], "c": [3, 4]})
    assert len(combos) == 4
    assert combos[0] == {"a": 1, "b": 1, "c": 3}
    check_experiment_budget(200)
    with pytest.raises(ValueError):
        check_experiment_budget(201)
    with pytest.raises(ValueError):
        expand_grid({}, {"empty": []})
