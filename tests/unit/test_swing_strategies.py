# -*- coding: utf-8 -*-
"""策略注册、参数冻结与对照策略测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.data.fixtures import build_trend_pullback_frame
from src.strategy import build_strategy, load_strategy_config
from src.strategy.momentum import select_momentum


def test_frozen_v1_parameters_match_appendix_a() -> None:
    config = load_strategy_config("trend_pullback_restart")
    assert config["trend"] == {
        "ma_fast": 20,
        "ma_slow": 60,
        "ma_slope_window": 10,
        "ma_slope_min": -0.02,
        "ret20_min": -0.05,
        "deviation_max": 0.08,
    }
    assert config["pullback"]["depth_min"] == 0.04
    assert config["pullback"]["depth_max"] == 0.12
    assert config["volume"]["shrink_min"] == 0.50
    assert config["restart"]["close_pos_min"] == 0.60
    assert config["exit"]["max_hold_days"] == 8
    assert config["exit"]["take_profit_r"] == 2.0


def test_box_breakout_pipeline_produces_diagnostics() -> None:
    frame, _ = build_trend_pullback_frame()
    strategy = build_strategy("box_breakout")
    out = strategy.prepare_and_signal(frame)
    for column in (
        "box_high",
        "box_low",
        "obs_box_amplitude",
        "obs_box_drift",
        "c_breakout",
        "c_breakout_volume",
        "entry",
        "score",
        "stop_price_real",
    ):
        assert column in out.columns
    assert out["entry"].dtype == bool
    assert out["stop_price_real"].dropna().gt(0).all()


def test_momentum_selection_is_ranked_and_deterministic() -> None:
    snapshot = pd.DataFrame(
        {
            "ret20": np.linspace(-0.1, 0.3, 20),
            "close": np.full(20, 10.0),
            "ma20": np.full(20, 9.0),
        },
        index=[f"6000{i:02d}" for i in range(20)],
    )
    first = select_momentum(snapshot, top_pct=0.10, n=2)
    second = select_momentum(snapshot, top_pct=0.10, n=2)
    assert first == second
    assert first == ["600019", "600018"]

    below_ma = snapshot.copy()
    below_ma["close"] = 8.0
    assert select_momentum(below_ma) == []
