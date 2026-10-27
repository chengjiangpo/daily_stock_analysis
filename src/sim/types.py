# -*- coding: utf-8 -*-
"""回测核心数据类型。金额与价格统一用 Decimal。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class Bar:
    """单只股票单日行情，价格为不复权真实价。"""

    code: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    pre_close: Decimal
    volume: Decimal
    amount: Decimal
    tradestatus: int = 1
    is_st: bool = False
    limit_up: Optional[Decimal] = None
    limit_down: Optional[Decimal] = None
    avg_amount_20d: Optional[Decimal] = None
    industry: Optional[str] = None
    market_cap: Optional[float] = None
    volatility_20d: Optional[float] = None
    adj_factor: Optional[Decimal] = None

    @property
    def is_suspended(self) -> bool:
        return int(self.tradestatus) == 0

    @property
    def is_limit_up_close(self) -> bool:
        return self.limit_up is not None and self.close >= self.limit_up

    @property
    def is_limit_down_close(self) -> bool:
        return self.limit_down is not None and self.close <= self.limit_down


@dataclass(frozen=True)
class OrderIntent:
    """由 T 日信号生成、计划在 T+1 开盘执行的委托。"""

    order_id: str
    code: str
    side: str
    quantity: int
    signal_date: date
    order_date: date
    ref_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    take_profit_price: Optional[Decimal] = None
    reason: str = ""
    score: float = 0.0


@dataclass(frozen=True)
class MatchOutcome:
    filled_qty: int = 0
    price: Optional[Decimal] = None
    reason: str = ""
    fee_total: Decimal = Decimal("0")
    slippage_cost: Decimal = Decimal("0")

    @property
    def filled(self) -> bool:
        return self.filled_qty > 0


@dataclass
class Fill:
    order_id: str
    code: str
    side: str
    trade_date: date
    price: Decimal
    quantity: int
    fees: Decimal
    slippage: Decimal
    reason: str = ""


@dataclass
class Position:
    code: str
    quantity: int = 0
    sellable_qty: int = 0
    avg_cost: Decimal = Decimal("0")
    entry_date: Optional[date] = None
    signal_date: Optional[date] = None
    entry_price: Decimal = Decimal("0")
    stop_price: Optional[Decimal] = None
    take_profit_price: Optional[Decimal] = None
    initial_risk: Optional[Decimal] = None
    industry: Optional[str] = None
    last_price: Decimal = Decimal("0")
    hold_days: int = 0
    entry_reason: str = ""
    entry_fees: Decimal = Decimal("0")

    @property
    def market_value(self) -> Decimal:
        return to_money(self.last_price * self.quantity)

    @property
    def cost_value(self) -> Decimal:
        return to_money(self.avg_cost * self.quantity)


@dataclass
class TradeRecord:
    trade_id: str
    code: str
    signal_date: Optional[date]
    entry_date: date
    entry_price: Decimal
    exit_date: date
    exit_price: Decimal
    quantity: int
    r_multiple: float
    fees: Decimal
    pnl: Decimal
    exit_reason: str
    hold_days: int
    entry_reason: str = ""
    industry: Optional[str] = None
    score: float = 0.0


def to_money(value: Decimal | float | str) -> Decimal:
    from .rules import quantize_price

    return quantize_price(value)


@dataclass
class EquityPoint:
    trade_date: date
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    drawdown: float
    positions: int = 0
    exposure: float = 0.0
    extra: dict = field(default_factory=dict)
