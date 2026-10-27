# -*- coding: utf-8 -*-
"""验收分级（方案 §8.4 C）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AcceptanceInput:
    n_trades: int
    n_years: float
    alpha_annual: Optional[float] = None
    alpha_ci_low: Optional[float] = None
    information_ratio: Optional[float] = None
    profit_factor_stress: Optional[float] = None
    max_drawdown: Optional[float] = None
    positive_year_ratio: Optional[float] = None
    residual_alpha: Optional[float] = None
    execution_passed: bool = True


@dataclass
class AcceptanceResult:
    level: str
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.level == "SIMULATE"

    def to_dict(self) -> dict:
        return {"level": self.level, "reasons": self.reasons}


def classify_acceptance(data: AcceptanceInput) -> AcceptanceResult:
    """按 §8.4 输出 REJECT / WATCH / SIMULATE。

    SIMULATE 要求执行层与有效性层全部通过；样本不足或 CI 下界为负进入 WATCH。
    """
    if not data.execution_passed:
        return AcceptanceResult("REJECT", ["execution_layer_failed"])

    if data.n_trades == 0:
        return AcceptanceResult("REJECT", ["no_trades"])

    failures: list[str] = []
    if data.alpha_annual is None or data.alpha_annual <= 0:
        failures.append("alpha_not_positive")
    if data.alpha_ci_low is None or data.alpha_ci_low <= 0:
        failures.append("alpha_ci_low_not_positive")
    if data.information_ratio is None or data.information_ratio < 0.5:
        failures.append("ir_below_0.5")
    if data.profit_factor_stress is None or data.profit_factor_stress < 1.2:
        failures.append("pf_stress_below_1.2")
    if data.max_drawdown is None or data.max_drawdown > 0.15:
        failures.append("max_drawdown_above_15pct")
    if data.residual_alpha is None or data.residual_alpha <= 0:
        failures.append("residual_alpha_not_positive")
    if data.positive_year_ratio is not None and data.positive_year_ratio < 2 / 3:
        failures.append("positive_years_below_2_of_3")
    if data.n_trades < 300:
        failures.append("trades_below_300")
    if data.n_years < 4:
        failures.append("sample_below_4y")

    if not failures:
        return AcceptanceResult("SIMULATE", [])

    if data.alpha_annual is not None and data.alpha_annual > 0:
        if any(reason in failures for reason in ("alpha_ci_low_not_positive", "trades_below_300",
                                                 "sample_below_4y", "positive_years_below_2_of_3")):
            return AcceptanceResult("WATCH", failures)

    if data.profit_factor_stress is not None and data.profit_factor_stress < 1.1:
        failures.append("pf_stress_below_1.1")
    return AcceptanceResult("REJECT", failures)
