# -*- coding: utf-8 -*-
"""成本与参数敏感性（方案 §8.4 B / §5.5）。"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

import pandas as pd

COST_TIERS = ("base", "higher", "stress")


def expand_grid(base: dict[str, Any], grid: dict[str, Sequence[Any]]) -> list[dict[str, Any]]:
    """展开参数网格；超出试验预算由调用方拒绝。"""
    keys = list(grid)
    combos = [dict(base)]
    for key in keys:
        values = list(grid[key])
        if not values:
            raise ValueError(f"参数 {key} 的候选值为空")
        next_combos = []
        for combo in combos:
            for value in values:
                item = dict(combo)
                item[key] = value
                next_combos.append(item)
        combos = next_combos
    return combos


def run_cost_sensitivity(
    run_once: Callable[[str], dict[str, Any]],
    tiers: Iterable[str] = COST_TIERS,
) -> pd.DataFrame:
    """对每个成本档执行一次回测，返回汇总表。"""
    records = []
    for tier in tiers:
        metrics = run_once(tier)
        record = {"cost_tier": tier}
        record.update(metrics)
        records.append(record)
    return pd.DataFrame(records)


def check_experiment_budget(n_experiments: int, budget: int = 200) -> None:
    if n_experiments > budget:
        raise ValueError(f"试验次数 {n_experiments} 超过预算 {budget}，需书面说明后再运行")
