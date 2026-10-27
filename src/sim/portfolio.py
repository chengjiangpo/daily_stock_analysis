# -*- coding: utf-8 -*-
"""组合、仓位与风险预算（方案 §7）。

资金与费用使用 Decimal；股数为整数股，按 100 股整手。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Dict, Optional

from .types import EquityPoint, Fill, Position, TradeRecord, to_money


@dataclass
class PortfolioConfig:
    aum: Decimal = Decimal("300000")
    max_positions: int = 5
    max_position_pct: float = 0.25
    max_gross_exposure: float = 0.95
    risk_per_trade: float = 0.01
    max_new_positions_per_day: int = 2
    max_per_industry: int = 2
    lot_size: int = 100
    drawdown_halve_at: float = 0.12
    drawdown_stop_at: float = 0.18
    loss_streak_threshold: int = 5
    loss_streak_risk: float = 0.005
    drawdown_cooldown_days: int = 20


class Portfolio:
    def __init__(self, config: PortfolioConfig) -> None:
        self.config = config
        self.cash: Decimal = to_money(config.aum)
        self.positions: Dict[str, Position] = {}
        self.equity_curve: list[EquityPoint] = []
        self.trades: list[TradeRecord] = []
        self.peak_equity: Decimal = to_money(config.aum)
        self.consecutive_losses: int = 0
        self.trade_seq: int = 0
        self._halt_days: int = 0
        self._dd_halt_active: bool = False

    # ---------- 估值 ----------

    @property
    def market_value(self) -> Decimal:
        return to_money(sum((pos.market_value for pos in self.positions.values()), Decimal("0")))

    @property
    def equity(self) -> Decimal:
        return to_money(self.cash + self.market_value)

    @property
    def drawdown(self) -> float:
        equity = self.equity
        if self.peak_equity <= 0:
            return 0.0
        return float((self.peak_equity - equity) / self.peak_equity)

    @property
    def exposure(self) -> float:
        equity = self.equity
        if equity <= 0:
            return 0.0
        return float(self.market_value / equity)

    def effective_risk_per_trade(self) -> float:
        risk = self.config.risk_per_trade
        if self.drawdown >= self.config.drawdown_halve_at:
            risk *= 0.5
        if self.consecutive_losses >= self.config.loss_streak_threshold:
            risk = min(risk, self.config.loss_streak_risk)
        return risk

    def start_new_day(self) -> None:
        """新交易日：昨日买入的股份变为可卖，持有天数 +1。"""
        if self._halt_days > 0:
            self._halt_days -= 1
        for pos in self.positions.values():
            pos.sellable_qty = pos.quantity
            if pos.entry_date is not None:
                pos.hold_days += 1

    # ---------- 开仓 ----------

    def can_open(self, industry: Optional[str] = None) -> tuple[bool, str]:
        if self._halt_days > 0:
            return False, "drawdown_cooldown"
        if len(self.positions) >= self.config.max_positions:
            return False, "max_positions"
        if industry is not None:
            count = sum(1 for pos in self.positions.values() if pos.industry == industry)
            if count >= self.config.max_per_industry:
                return False, "industry_limit"
        return True, "ok"

    def size_order(
        self,
        entry_price: Decimal,
        stop_price: Decimal,
        industry: Optional[str] = None,
    ) -> tuple[int, str]:
        """按单笔风险预算与上限计算可买股数。"""
        ok, reason = self.can_open(industry)
        if not ok:
            return 0, reason
        entry = Decimal(entry_price)
        stop = Decimal(stop_price)
        if entry <= 0 or stop <= 0:
            return 0, "invalid_price"
        risk_per_share = entry - stop
        if risk_per_share <= 0:
            return 0, "invalid_stop"

        equity = self.equity
        risk_budget = equity * Decimal(str(self.effective_risk_per_trade()))
        qty_by_risk = int(risk_budget / risk_per_share)
        qty_by_position = int((equity * Decimal(str(self.config.max_position_pct))) / entry)
        gross_cap = equity * Decimal(str(self.config.max_gross_exposure)) - self.market_value
        qty_by_gross = int(max(gross_cap, Decimal("0")) / entry)
        # 预留约 0.5% 费用与滑点
        qty_by_cash = int((self.cash * Decimal("0.995")) / entry)

        qty = min(qty_by_risk, qty_by_position, qty_by_gross, qty_by_cash)
        lot = self.config.lot_size
        qty = (qty // lot) * lot
        if qty <= 0:
            return 0, "insufficient_budget"
        return qty, "ok"

    def apply_buy(self, fill: Fill, stop_price: Decimal, take_profit_price: Optional[Decimal],
                  industry: Optional[str], reason: str = "", signal_date: Optional[date] = None) -> None:
        if fill.quantity <= 0:
            return
        cost = fill.price * Decimal(fill.quantity) + fill.fees
        self.cash = to_money(self.cash - cost)
        position = self.positions.get(fill.code)
        if position is None:
            position = Position(
                code=fill.code,
                quantity=0,
                sellable_qty=0,
                entry_date=fill.trade_date,
                signal_date=signal_date,
                entry_price=fill.price,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                initial_risk=fill.price - stop_price,
                industry=industry,
                last_price=fill.price,
                entry_reason=reason,
            )
            self.positions[fill.code] = position
        total_qty = position.quantity + fill.quantity
        position.avg_cost = (position.cost_value + fill.price * Decimal(fill.quantity) + fill.fees) / Decimal(total_qty)
        position.quantity = total_qty
        position.entry_fees += fill.fees
        # T+1：当日买入不可卖
        position.last_price = fill.price

    def apply_sell(self, fill: Fill, reason: str = "") -> Optional[TradeRecord]:
        position = self.positions.get(fill.code)
        if position is None or fill.quantity <= 0:
            return None
        qty = min(fill.quantity, position.quantity)
        proceeds = fill.price * Decimal(qty) - fill.fees
        self.cash = to_money(self.cash + proceeds)
        cost_part = position.avg_cost * Decimal(qty)
        pnl = to_money(proceeds - cost_part)

        self.trade_seq += 1
        initial_risk = position.initial_risk or Decimal("0")
        r_multiple = 0.0
        if initial_risk > 0 and qty > 0:
            r_multiple = float((fill.price - position.entry_price) * Decimal(qty) / (initial_risk * Decimal(qty)))

        entry_fee_share = (
            position.entry_fees * Decimal(qty) / Decimal(position.quantity) if position.quantity > 0 else Decimal("0")
        )
        position.entry_fees -= entry_fee_share

        trade = TradeRecord(
            trade_id=f"T{self.trade_seq:06d}",
            code=fill.code,
            signal_date=position.signal_date,
            entry_date=position.entry_date or fill.trade_date,
            entry_price=position.entry_price,
            exit_date=fill.trade_date,
            exit_price=fill.price,
            quantity=qty,
            r_multiple=r_multiple,
            fees=entry_fee_share + fill.fees,
            pnl=pnl,
            exit_reason=reason or fill.reason,
            hold_days=position.hold_days,
            entry_reason=position.entry_reason,
            industry=position.industry,
        )
        self.trades.append(trade)

        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        position.quantity -= qty
        position.sellable_qty = max(position.sellable_qty - qty, 0)
        if position.quantity <= 0:
            del self.positions[fill.code]
        return trade

    # ---------- 收盘估值 ----------

    def mark_to_market(self, prices: Dict[str, Decimal], trade_date: date) -> EquityPoint:
        for code, position in self.positions.items():
            price = prices.get(code)
            if price is not None and price > 0:
                position.last_price = Decimal(price)
        equity = self.equity
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = 0.0
        if self.peak_equity > 0:
            drawdown = float((self.peak_equity - equity) / self.peak_equity)

        # 回撤熔断：首次触发进入冷却期；冷却结束后以半风险恢复开仓。
        if drawdown >= self.config.drawdown_stop_at and not self._dd_halt_active:
            self._halt_days = max(self.config.drawdown_cooldown_days, 0)
            self._dd_halt_active = True
        elif drawdown < self.config.drawdown_halve_at:
            self._dd_halt_active = False
        point = EquityPoint(
            trade_date=trade_date,
            cash=self.cash,
            market_value=self.market_value,
            equity=equity,
            drawdown=drawdown,
            positions=len(self.positions),
            exposure=self.exposure,
        )
        self.equity_curve.append(point)
        return point

    def industry_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for position in self.positions.values():
            if position.industry:
                counts[position.industry] = counts.get(position.industry, 0) + 1
        return counts

    def open_codes(self) -> set[str]:
        return set(self.positions)

    def snapshot(self) -> dict:
        return {
            "cash": float(self.cash),
            "market_value": float(self.market_value),
            "equity": float(self.equity),
            "drawdown": self.drawdown,
            "positions": len(self.positions),
            "consecutive_losses": self.consecutive_losses,
        }
