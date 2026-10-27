# -*- coding: utf-8 -*-
"""时间切分（方案 §8.1）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict

import pandas as pd


@dataclass(frozen=True)
class TimeSplit:
    development_start: date
    development_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    forward_start: date

    def validate(self) -> None:
        if not (self.development_start <= self.development_end < self.validation_start
                <= self.validation_end < self.test_start <= self.test_end < self.forward_start):
            raise ValueError(f"时间切分不满足严格递增: {self}")


DEFAULT_SPLIT = TimeSplit(
    development_start=date(2016, 1, 1),
    development_end=date(2022, 12, 31),
    validation_start=date(2023, 1, 1),
    validation_end=date(2024, 12, 31),
    test_start=date(2025, 1, 1),
    test_end=date(2026, 9, 30),
    forward_start=date(2026, 10, 1),
)


def make_split(
    development_start: date = DEFAULT_SPLIT.development_start,
    development_end: date = DEFAULT_SPLIT.development_end,
    validation_end: date = DEFAULT_SPLIT.validation_end,
    test_end: date = DEFAULT_SPLIT.test_end,
) -> TimeSplit:
    dev_end = pd.Timestamp(development_end)
    val_end = pd.Timestamp(validation_end)
    tst_end = pd.Timestamp(test_end)
    split = TimeSplit(
        development_start=pd.Timestamp(development_start).date(),
        development_end=dev_end.date(),
        validation_start=(dev_end + pd.Timedelta(days=1)).date(),
        validation_end=val_end.date(),
        test_start=(val_end + pd.Timedelta(days=1)).date(),
        test_end=tst_end.date(),
        forward_start=(tst_end + pd.Timedelta(days=1)).date(),
    )
    split.validate()
    return split


def slice_frames(frames: Dict[str, pd.DataFrame], start: date, end: date) -> Dict[str, pd.DataFrame]:
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    sliced = {}
    for code, frame in frames.items():
        part = frame[(frame.index >= start_ts) & (frame.index <= end_ts)]
        if not part.empty:
            sliced[code] = part
    return sliced


def split_frames(frames: Dict[str, pd.DataFrame], split: TimeSplit = DEFAULT_SPLIT) -> dict[str, Dict[str, pd.DataFrame]]:
    return {
        "development": slice_frames(frames, split.development_start, split.development_end),
        "validation": slice_frames(frames, split.validation_start, split.validation_end),
        "test": slice_frames(frames, split.test_start, split.test_end),
    }
