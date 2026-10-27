# -*- coding: utf-8 -*-
"""日线级成交判定（方案 §6.2）。

日线数据无法还原盘中撮合，这里采用方案写死的保守判据：
- 一字/开盘涨停买不到；一字跌停卖不出；跌停但 high > limit_down 可卖；
- 跳空穿越止损按开盘价成交；
- 参与率上限 min(2% 成交额, 2% 成交量×价格, 2% 20 日均额)。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .fees import FeeModel
from .rules import LimitRule, quantize_price, to_decimal
from .types import Bar, MatchOutcome, OrderIntent


def compute_limit_prices(
    pre_close: Decimal | float | str,
    rule: LimitRule,
    tick_size: Decimal = Decimal("0.01"),
) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """按 pre_close 与分段幅度计算涨跌停价；None 表示不设限。"""
    base = to_decimal(pre_close)
    if base <= 0:
        raise ValueError(f"pre_close 必须为正，收到 {pre_close!r}")
    if rule.up is None or rule.down is None:
        return None, None

    limit_up = quantize_price(base * (Decimal("1") + to_decimal(rule.up)), tick_size)
    limit_down = quantize_price(base * (Decimal("1") - to_decimal(rule.down)), tick_size)
    return max(limit_up, tick_size), max(limit_down, tick_size)


def is_one_word_limit_up(bar: Bar, limit_up: Optional[Decimal]) -> bool:
    if limit_up is None:
        return False
    return bar.open == bar.high == bar.low == bar.close == limit_up


def is_one_word_limit_down(bar: Bar, limit_down: Optional[Decimal]) -> bool:
    if limit_down is None:
        return False
    return bar.high == bar.low == bar.close == limit_down


def participation_cap_value(bar: Bar, cap_ratio: float) -> Optional[Decimal]:
    """返回当日可成交金额上限；缺少流动性数据时返回 None（拒绝成交）。"""
    ratio = to_decimal(cap_ratio)
    candidates = []
    if bar.amount is not None and bar.amount > 0:
        candidates.append(bar.amount * ratio)
    if bar.volume is not None and bar.volume > 0:
        candidates.append(bar.volume * bar.close * ratio)
    if bar.avg_amount_20d is not None and bar.avg_amount_20d > 0:
        candidates.append(bar.avg_amount_20d * ratio)
    if not candidates:
        return None
    return min(candidates)


@dataclass(frozen=True)
class MatcherConfig:
    participation_cap: float = 0.02
    lot_size: int = 100


class Matcher:
    """按 T 日开盘价撮合，费用由 FeeModel 计算。"""

    def __init__(
        self,
        fee_model: FeeModel,
        participation_cap: float = 0.02,
        lot_size: int = 100,
    ) -> None:
        self.fee_model = fee_model
        self.participation_cap = participation_cap
        self.lot_size = lot_size

    def _fill_price(self, base_price: Decimal, side: str, bar: Bar) -> Decimal:
        price = self.fee_model.slippage_price(base_price, side)
        if side == "buy" and bar.limit_up is not None:
            price = min(price, bar.limit_up)
        if side == "sell" and bar.limit_down is not None:
            price = max(price, bar.limit_down)
        return quantize_price(price)

    def _cap_quantity(self, bar: Bar, price: Decimal) -> int:
        cap_value = participation_cap_value(bar, self.participation_cap)
        if cap_value is None:
            return 0
        lots = int(cap_value // (price * to_decimal(self.lot_size)))
        return max(lots, 0) * self.lot_size

    def match_buy(self, order: OrderIntent, bar: Bar, max_price: Optional[Decimal] = None) -> MatchOutcome:
        if order.quantity <= 0:
            return MatchOutcome(reason="invalid_quantity")
        if bar.is_suspended:
            return MatchOutcome(reason="suspended")
        if bar.limit_up is not None and bar.open >= bar.limit_up:
            return MatchOutcome(reason="open_at_limit_up")
        if max_price is not None and bar.open > quantize_price(max_price):
            return MatchOutcome(reason="gap_exceeds_limit")

        price = self._fill_price(bar.open, "buy", bar)
        cap_qty = self._cap_quantity(bar, price)
        quantity = min(order.quantity, cap_qty)
        quantity = (quantity // self.lot_size) * self.lot_size
        if quantity <= 0:
            return MatchOutcome(reason="participation_cap")

        fees = self.fee_model.compute("buy", price, quantity, bar.trade_date, order.code).total
        slippage = (price - quantize_price(bar.open)) * to_decimal(quantity)
        reason = "filled" if quantity == order.quantity else "partial_participation_cap"
        return MatchOutcome(
            filled_qty=quantity,
            price=price,
            reason=reason,
            fee_total=fees,
            slippage_cost=slippage.quantize(Decimal("0.01")),
        )

    def match_sell(self, order: OrderIntent, bar: Bar) -> MatchOutcome:
        if order.quantity <= 0:
            return MatchOutcome(reason="invalid_quantity")
        if bar.is_suspended:
            return MatchOutcome(reason="suspended")
        if is_one_word_limit_down(bar, bar.limit_down):
            return MatchOutcome(reason="one_word_limit_down")
        if bar.limit_down is not None and bar.open <= bar.limit_down and bar.high <= bar.limit_down:
            return MatchOutcome(reason="limit_down_no_liquidity")

        price = self._fill_price(bar.open, "sell", bar)
        cap_qty = self._cap_quantity(bar, price)
        quantity = min(order.quantity, cap_qty)
        quantity = (quantity // self.lot_size) * self.lot_size
        if quantity <= 0:
            return MatchOutcome(reason="participation_cap")

        fees = self.fee_model.compute("sell", price, quantity, bar.trade_date, order.code).total
        slippage = (quantize_price(bar.open) - price) * to_decimal(quantity)
        reason = "filled" if quantity == order.quantity else "partial_participation_cap"
        return MatchOutcome(
            filled_qty=quantity,
            price=price,
            reason=reason,
            fee_total=fees,
            slippage_cost=slippage.quantize(Decimal("0.01")),
        )
