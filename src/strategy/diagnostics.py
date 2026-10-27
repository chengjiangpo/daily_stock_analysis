# -*- coding: utf-8 -*-
"""条件诊断结构（方案 §5.4）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

import pandas as pd


@dataclass(frozen=True)
class ConditionResult:
    condition_name: str
    passed: bool
    observed_value: Optional[float]
    threshold_low: Optional[float]
    threshold_high: Optional[float]
    failure_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "condition_name": self.condition_name,
            "passed": self.passed,
            "observed_value": self.observed_value,
            "threshold_low": self.threshold_low,
            "threshold_high": self.threshold_high,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True)
class DiagnosticSpec:
    column: str
    name: str
    threshold_low: Optional[float] = None
    threshold_high: Optional[float] = None
    failure_reason: str = "condition_not_met"
    observed_column: Optional[str] = None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return number


def build_diagnostics(row: Mapping[str, Any], specs: Iterable[DiagnosticSpec]) -> tuple[ConditionResult, ...]:
    results = []
    for spec in specs:
        passed = bool(row.get(spec.column, False))
        observed = _to_float(row.get(spec.observed_column or spec.column))
        results.append(
            ConditionResult(
                condition_name=spec.name,
                passed=passed,
                observed_value=observed,
                threshold_low=spec.threshold_low,
                threshold_high=spec.threshold_high,
                failure_reason="" if passed else spec.failure_reason,
            )
        )
    return tuple(results)
