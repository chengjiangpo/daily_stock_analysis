# -*- coding: utf-8 -*-
"""事件定义（事件研究 / I-6 的「入口信号」侧）。

事件研究只关心「信号日在哪」，不关心退出规则。本模块提供两类事件：

1. **策略入场信号**：直接复用已注册策略的 ``prepare`` + ``generate_signals``，
   取 ``entry`` 列为信号日。这样事件研究与组合回测的口径天然一致，
   避免「事件研究一套规则、回测另一套规则」的口径分裂；
2. **自定义条件**：用一行 lambda 表达待检验假设（如「close > MA20」），
   用于 R-1~R-4 这类还没有正式策略实现的候选方向做廉价筛选。

用法::

    from src.strategy.events import build_detector, list_detectors
    detector = build_detector("trend_pullback_restart")
    dates = detector.detect("000001", frame)   # -> set[date]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Mapping, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 事件函数：给定 (code, 已加载行情) → 信号日集合
DetectFn = Callable[[str, pd.DataFrame], "set[date]"]


@dataclass(frozen=True)
class EventDetector:
    """一个可复用于事件研究的事件定义。"""

    name: str
    label: str
    detect: DetectFn
    warmup: int = 0
    """信号确认所需的最少预热交易日；数据不足的股票直接跳过，避免前段全为 NaN 的假信号。"""
    hypothesis: str = ""
    """登记文本：动机与与负先验（N1–N3）的差异，写入报告便于试验账本引用（§3.2）。"""

    def __call__(self, code: str, frame: pd.DataFrame) -> "set[date]":
        return self.detect(code, frame)


def strategy_entry_detector(
    strategy_name: str,
    event_name: Optional[str] = None,
    label: Optional[str] = None,
    config_path: Optional[str] = None,
    warmup: int = 120,
    hypothesis: str = "",
) -> EventDetector:
    """把已注册策略的 ``entry`` 信号包装成事件定义。

    ``warmup`` 默认 120 个交易日：覆盖 MA60 + 各类滚动窗口，避免把预热期 NaN
    当信号。数据长度不足 ``warmup`` 时该股票不产生任何事件（由调用方计入跳过）。
    """
    from src.strategy import build_strategy

    strategy = build_strategy(strategy_name, config_path)

    def detect(code: str, frame: pd.DataFrame) -> "set[date]":
        if frame is None or len(frame) < warmup:
            return set()
        try:
            prepared = strategy.prepare(frame)
            signals = strategy.generate_signals(prepared)
        except Exception as error:  # noqa: BLE001 - 单票异常不应中断全市场扫描
            logger.warning("事件检测失败（%s / %s）：%s", strategy_name, code, error)
            return set()
        if "entry" not in signals.columns:
            return set()
        mask = signals["entry"].fillna(False).astype(bool)
        mask.iloc[:warmup] = False
        return {timestamp.date() for timestamp in signals.index[mask]}

    return EventDetector(
        name=event_name or strategy_name,
        label=label or f"{strategy_name} 入场信号",
        detect=detect,
        warmup=warmup,
        hypothesis=hypothesis,
    )


def condition_detector(
    name: str,
    label: str,
    condition: Callable[[pd.DataFrame], pd.Series],
    prepare: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
    warmup: int = 120,
    hypothesis: str = "",
) -> EventDetector:
    """用一行条件表达式定义事件（假设筛选阶段用）。

    ``condition`` 接收（可选 ``prepare`` 处理后的）DataFrame，返回布尔 Series；
    索引须为 DatetimeIndex。
    """
    from src.strategy.indicators import add_price_columns, ensure_sorted

    def detect(code: str, frame: pd.DataFrame) -> "set[date]":
        if frame is None or len(frame) < warmup:
            return set()
        try:
            work = ensure_sorted(frame)
            work = prepare(work) if prepare is not None else add_price_columns(work)
            mask = condition(work)
        except Exception as error:  # noqa: BLE001
            logger.warning("条件事件检测失败（%s / %s）：%s", name, code, error)
            return set()
        if not isinstance(mask, pd.Series):
            mask = pd.Series(mask, index=work.index)
        mask = mask.fillna(False).astype(bool)
        mask.iloc[:warmup] = False
        return {timestamp.date() for timestamp in mask.index[mask]}

    return EventDetector(
        name=name,
        label=label,
        detect=detect,
        warmup=warmup,
        hypothesis=hypothesis,
    )


# ---------------------------------------------------------------------------
# 内置事件（对应负先验登记册 N1–N3 与 V3.0 §5.3 候选方向）
# ---------------------------------------------------------------------------


def trend_pullback_entry(**kwargs) -> EventDetector:
    """N3 / R-1：v1 主策略「趋势回调再启动」的入场信号（不含任何退出规则）。"""
    kwargs.setdefault(
        "hypothesis",
        "R-1：v1 入场信号 + 纯时间退出的超额。N3 归因显示 trend_break（收盘<MA10）"
        "占 74.7% 退出且均值 -2.73%，是亏损主因；关掉 MA10 是新假设而非调参修复。",
    )
    return strategy_entry_detector("trend_pullback_restart", event_name="trend_pullback_entry", **kwargs)


def box_breakout_entry(**kwargs) -> EventDetector:
    """N1：箱体突破入场信号（已封存，仅用于复核负先验，不用于新假设）。"""
    kwargs.setdefault(
        "hypothesis",
        "N1：箱体突破（主板小市值 + 横盘箱体 + 放量突破）。已封存，仅作负先验复核。",
    )
    return strategy_entry_detector("box_breakout", event_name="box_breakout_entry", **kwargs)


def random_entry(seed: int = 42, rate: float = 0.01, **kwargs) -> EventDetector:
    """随机事件（阴性对照）：每个信号日以 ``rate`` 概率触发。

    用于校准门槛本身——随机事件的超额应不显著非零；若显著，说明对照组口径有问题。
    """
    import numpy as np

    def make_condition(seed_value: int, rate_value: float):
        def condition(frame: pd.DataFrame) -> pd.Series:
            rng = np.random.default_rng(seed_value)
            return pd.Series(rng.random(len(frame)) < rate_value, index=frame.index)

        return condition

    kwargs.setdefault("label", f"随机事件（p={rate}，阴性对照）")
    kwargs.setdefault("hypothesis", "阴性对照：随机信号不应产生显著超额。")
    return condition_detector(
        "random",
        kwargs.pop("label"),
        make_condition(seed, rate),
        warmup=kwargs.pop("warmup", 20),
        hypothesis=kwargs.pop("hypothesis"),
    )


BUILTIN_DETECTORS: dict[str, Callable[..., EventDetector]] = {
    "trend_pullback_entry": trend_pullback_entry,
    "box_breakout_entry": box_breakout_entry,
    "random": random_entry,
}


def list_detectors() -> list[str]:
    return sorted(BUILTIN_DETECTORS)


def build_detector(name: str, **kwargs) -> EventDetector:
    if name not in BUILTIN_DETECTORS:
        raise KeyError(f"未知事件定义 {name!r}，可用: {list_detectors()}（或用 --event-cli 自定义）")
    return BUILTIN_DETECTORS[name](**kwargs)


def build_detectors(names: Iterable[str], **kwargs) -> list[EventDetector]:
    seen: set[str] = set()
    detectors: list[EventDetector] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        detectors.append(build_detector(name, **kwargs))
    return detectors


__all__ = [
    "BUILTIN_DETECTORS",
    "DetectFn",
    "EventDetector",
    "box_breakout_entry",
    "build_detector",
    "build_detectors",
    "condition_detector",
    "list_detectors",
    "random_entry",
    "strategy_entry_detector",
    "trend_pullback_entry",
]
