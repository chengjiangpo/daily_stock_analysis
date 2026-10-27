# -*- coding: utf-8 -*-
"""滚动前推窗口生成（方案 §8.2）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

import pandas as pd


@dataclass(frozen=True)
class WalkForwardWindow:
    dev_start: date
    dev_end: date
    val_start: date
    val_end: date
    test_start: date
    test_end: date

    def validate(self) -> None:
        if not (self.dev_start <= self.dev_end < self.val_start <= self.val_end < self.test_start <= self.test_end):
            raise ValueError(f"窗口顺序非法: {self}")

    def to_dict(self) -> dict:
        return {
            "dev_start": self.dev_start,
            "dev_end": self.dev_end,
            "val_start": self.val_start,
            "val_end": self.val_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
        }


def generate_windows(
    start: date,
    end: date,
    dev_years: int = 3,
    val_years: int = 1,
    test_months: int = 6,
    step_months: int = 6,
) -> list[WalkForwardWindow]:
    """生成不相重叠的测试窗；dev_end < val_start < test_start。"""
    if dev_years <= 0 or val_years <= 0 or test_months <= 0 or step_months <= 0:
        raise ValueError("窗口参数必须为正")

    windows: list[WalkForwardWindow] = []
    cursor = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    while True:
        dev_start = cursor
        dev_end = dev_start + pd.DateOffset(years=dev_years) - pd.Timedelta(days=1)
        val_start = dev_end + pd.Timedelta(days=1)
        val_end = val_start + pd.DateOffset(years=val_years) - pd.Timedelta(days=1)
        test_start = val_end + pd.Timedelta(days=1)
        test_end = test_start + pd.DateOffset(months=test_months) - pd.Timedelta(days=1)
        if test_end > end_ts:
            break
        window = WalkForwardWindow(
            dev_start=dev_start.date(),
            dev_end=dev_end.date(),
            val_start=val_start.date(),
            val_end=val_end.date(),
            test_start=test_start.date(),
            test_end=test_end.date(),
        )
        window.validate()
        windows.append(window)
        cursor = cursor + pd.DateOffset(months=step_months)

    return windows


def assert_non_overlapping(windows: Iterable[WalkForwardWindow]) -> None:
    previous_test_end = None
    for window in windows:
        if previous_test_end is not None and window.test_start <= previous_test_end:
            raise ValueError(f"测试窗重叠: {window}")
        previous_test_end = window.test_end
