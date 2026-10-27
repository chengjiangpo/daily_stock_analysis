# -*- coding: utf-8 -*-
"""策略注册与加载。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.sim.rules import PROJECT_ROOT

from .base import BaseStrategy
from .box_breakout import BoxBreakout
from .trend_pullback import TrendPullbackRestart

DEFAULT_BACKTEST_PATH = PROJECT_ROOT / "config" / "backtest.yaml"
STRATEGY_CLASSES: dict[str, type[BaseStrategy]] = {
    "trend_pullback_restart": TrendPullbackRestart,
    "box_breakout": BoxBreakout,
}


def load_strategy_config(name: str, path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else DEFAULT_BACKTEST_PATH
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    strategies = raw.get("strategies") or {}
    if name not in strategies:
        raise KeyError(f"配置中不存在策略 {name!r}，可用: {sorted(strategies)}")
    return strategies[name]


def build_strategy(name: str, path: str | Path | None = None) -> BaseStrategy:
    if name not in STRATEGY_CLASSES:
        raise KeyError(f"未注册策略 {name!r}，可用: {sorted(STRATEGY_CLASSES)}")
    return STRATEGY_CLASSES[name](load_strategy_config(name, path))


__all__ = ["BaseStrategy", "BoxBreakout", "TrendPullbackRestart", "build_strategy", "load_strategy_config"]
