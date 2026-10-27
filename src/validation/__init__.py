# -*- coding: utf-8 -*-
"""回测验证统计工具。"""

from src.validation.stats import (
    alpha_vs_benchmark,
    block_bootstrap_ci,
    decompose_pnl,
    deflated_sharpe_ratio,
    performance_metrics,
    probability_of_backtest_overfitting,
    trade_metrics,
)
from src.validation.verdict import AcceptanceInput, AcceptanceResult, classify_acceptance

__all__ = [
    "AcceptanceInput",
    "AcceptanceResult",
    "alpha_vs_benchmark",
    "block_bootstrap_ci",
    "classify_acceptance",
    "decompose_pnl",
    "deflated_sharpe_ratio",
    "performance_metrics",
    "probability_of_backtest_overfitting",
    "trade_metrics",
]
