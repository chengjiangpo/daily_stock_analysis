# -*- coding: utf-8 -*-
"""A 股交易费用模型（方案 §4.3 / §6.3）。

费用一律用 Decimal 计算并按 0.01 元四舍五入；滑点按 tick 建模，不按百分比。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import yaml

from .rules import (
    PROJECT_ROOT,
    classify_exchange,
    parse_date,
    quantize_price,
    to_decimal,
)

DEFAULT_FEES_PATH = PROJECT_ROOT / "config" / "fees.yaml"
DEFAULT_BACKTEST_PATH = PROJECT_ROOT / "config" / "backtest.yaml"
COST_TIERS = ("base", "higher", "stress")


@dataclass(frozen=True)
class FeeSegment:
    date_from: Optional[date]
    date_to: Optional[date]
    rate: float
    exchanges: Optional[frozenset[str]] = None

    def matches(self, trade_date: date, exchange: str) -> bool:
        if self.date_from is not None and trade_date < self.date_from:
            return False
        if self.date_to is not None and trade_date > self.date_to:
            return False
        if self.exchanges is not None and exchange not in self.exchanges:
            return False
        return True


@dataclass(frozen=True)
class FeeBreakdown:
    commission: Decimal = Decimal("0")
    stamp_duty: Decimal = Decimal("0")
    transfer_fee: Decimal = Decimal("0")
    handling_fee: Decimal = Decimal("0")
    regulatory_fee: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return (
            self.commission
            + self.stamp_duty
            + self.transfer_fee
            + self.handling_fee
            + self.regulatory_fee
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "commission": float(self.commission),
            "stamp_duty": float(self.stamp_duty),
            "transfer_fee": float(self.transfer_fee),
            "handling_fee": float(self.handling_fee),
            "regulatory_fee": float(self.regulatory_fee),
            "total": float(self.total),
        }


def _load_segments(items: Iterable[dict[str, Any]]) -> tuple[FeeSegment, ...]:
    segments = []
    for item in items or []:
        exchanges = item.get("exchanges")
        segments.append(
            FeeSegment(
                date_from=parse_date(item.get("date_from")),
                date_to=parse_date(item.get("date_to")),
                rate=float(item["rate"]),
                exchanges=frozenset(exchanges) if exchanges else None,
            )
        )
    return tuple(segments)


def _pick_rate(segments: Sequence[FeeSegment], trade_date: date, exchange: str) -> float:
    for segment in segments:
        if segment.matches(trade_date, exchange):
            return segment.rate
    return 0.0


class FeeModel:
    """按交易日、交易所、买卖方向计算费用。"""

    def __init__(
        self,
        tick_size: Decimal,
        commission_rate: float,
        min_commission: Decimal,
        stamp_duty_segments: Sequence[FeeSegment],
        transfer_fee_segments: Sequence[FeeSegment],
        handling_fee_segments: Sequence[FeeSegment],
        regulatory_fee_rate: float,
        slippage_ticks: int,
    ) -> None:
        self.tick_size = tick_size
        self.commission_rate = to_decimal(commission_rate)
        self.min_commission = min_commission
        self.stamp_duty_segments = tuple(stamp_duty_segments)
        self.transfer_fee_segments = tuple(transfer_fee_segments)
        self.handling_fee_segments = tuple(handling_fee_segments)
        self.regulatory_fee_rate = to_decimal(regulatory_fee_rate)
        self.slippage_ticks = int(slippage_ticks)

    @classmethod
    def load(
        cls,
        fees_path: str | Path | None = None,
        backtest_path: str | Path | None = None,
        cost_tier: str = "base",
    ) -> "FeeModel":
        if cost_tier not in COST_TIERS:
            raise ValueError(f"未知成本档: {cost_tier}，可选 {COST_TIERS}")
        fees_file = Path(fees_path) if fees_path else Path(os.getenv("BACKTEST_FEES_PATH", DEFAULT_FEES_PATH))
        backtest_file = Path(backtest_path) if backtest_path else DEFAULT_BACKTEST_PATH

        with fees_file.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        with backtest_file.open("r", encoding="utf-8") as handle:
            backtest = yaml.safe_load(handle) or {}

        tier = (backtest.get("cost_tiers") or {}).get(cost_tier, {})
        commission_rate = float(
            os.getenv("BACKTEST_COMMISSION_RATE", tier.get("commission_rate", raw["commission"]["rate"]))
        )
        slippage_ticks = int(
            os.getenv(
                "BACKTEST_SLIPPAGE_TICKS",
                tier.get("slippage_ticks", (raw.get("slippage_ticks") or {}).get(cost_tier, 1)),
            )
        )

        return cls(
            tick_size=to_decimal(raw.get("tick_size", "0.01")),
            commission_rate=commission_rate,
            min_commission=to_decimal(
                os.getenv("BACKTEST_MIN_COMMISSION", raw["commission"].get("min_per_order", 5.0))
            ),
            stamp_duty_segments=_load_segments(raw.get("stamp_duty", {}).get("segments", [])),
            transfer_fee_segments=_load_segments(raw.get("transfer_fee", {}).get("segments", [])),
            handling_fee_segments=_load_segments(raw.get("handling_fee", {}).get("segments", [])),
            regulatory_fee_rate=float(raw.get("regulatory_fee", {}).get("rate", 0.0)),
            slippage_ticks=slippage_ticks,
        )

    def compute(
        self,
        side: str,
        price: Decimal | float | str,
        quantity: int,
        trade_date: date,
        code: str,
    ) -> FeeBreakdown:
        """side 取 buy/sell；price 为成交价；quantity 为股数。"""
        side = side.lower()
        if side not in ("buy", "sell"):
            raise ValueError(f"side 必须是 buy/sell，收到 {side!r}")
        if quantity < 0:
            raise ValueError("quantity 不能为负")

        exchange = classify_exchange(code)
        notional = quantize_price(to_decimal(price)) * to_decimal(quantity)
        fee_two_places = Decimal("0.01")

        commission = quantize_price(notional * self.commission_rate)
        if quantity > 0:
            commission = max(commission, self.min_commission)
        else:
            commission = Decimal("0")

        stamp_rate = _pick_rate(self.stamp_duty_segments, trade_date, exchange) if side == "sell" else 0.0
        stamp_duty = quantize_price(notional * to_decimal(stamp_rate)).quantize(fee_two_places)

        transfer_rate = _pick_rate(self.transfer_fee_segments, trade_date, exchange)
        transfer_fee = quantize_price(notional * to_decimal(transfer_rate)).quantize(fee_two_places)

        handling_rate = _pick_rate(self.handling_fee_segments, trade_date, exchange)
        handling_fee = quantize_price(notional * to_decimal(handling_rate)).quantize(fee_two_places)

        regulatory_fee = quantize_price(notional * self.regulatory_fee_rate).quantize(fee_two_places)

        return FeeBreakdown(
            commission=commission,
            stamp_duty=stamp_duty,
            transfer_fee=transfer_fee,
            handling_fee=handling_fee,
            regulatory_fee=regulatory_fee,
        )

    def slippage_price(self, base_price: Decimal | float | str, side: str) -> Decimal:
        """按 tick 计算滑点后价格（未做涨跌停截断，截断由撮合层负责）。"""
        price = quantize_price(base_price)
        offset = self.tick_size * to_decimal(self.slippage_ticks)
        if side.lower() == "buy":
            return price + offset
        if side.lower() == "sell":
            return max(price - offset, Decimal("0.01"))
        raise ValueError(f"side 必须是 buy/sell，收到 {side!r}")


def build_fee_model(cost_tier: str = "base") -> FeeModel:
    return FeeModel.load(cost_tier=cost_tier)
