# -*- coding: utf-8 -*-
"""日循环回测引擎（方案 §6.1）。

顺序固定：
1) 新交易日解锁昨日买入股份；
2) 以开盘价执行昨日收盘生成的卖出单；
3) 以开盘价执行买入单，且不得使用当日卖出所得；
4) 收盘估值并生成 T+1 信号。

所有价格：信号在复权序列，成交/估值用不复权真实价。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional

import numpy as np
import pandas as pd

from src.strategy.base import BaseStrategy, Signal
from src.strategy.filters import PoolConfig, filter_universe

from .fees import FeeModel
from .matcher import Matcher, compute_limit_prices
from .portfolio import Portfolio, PortfolioConfig
from .rules import MarketRules, get_market_rules, quantize_price, to_decimal
from .types import Bar, Fill, OrderIntent, TradeRecord


@dataclass
class BacktestResult:
    equity: pd.DataFrame
    orders: pd.DataFrame
    fills: pd.DataFrame
    trades: pd.DataFrame
    signals: pd.DataFrame
    diagnostics: pd.DataFrame
    positions: pd.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, pd.DataFrame]:
        return {
            "daily_equity": self.equity,
            "orders": self.orders,
            "fills": self.fills,
            "trades": self.trades,
            "signals": self.signals,
            "diagnostics": self.diagnostics,
            "daily_positions": self.positions,
        }


class BacktestEngine:
    def __init__(
        self,
        strategy: BaseStrategy,
        data: Dict[str, pd.DataFrame],
        fee_model: FeeModel,
        portfolio_config: PortfolioConfig | None = None,
        pool_config: PoolConfig | None = None,
        rules: MarketRules | None = None,
        participation_cap: float = 0.02,
        start: Optional[date] = None,
        end: Optional[date] = None,
        record_diagnostics: bool = True,
        constrained: bool = True,
        fixed_notional: Decimal = Decimal("60000"),
    ) -> None:
        self.strategy = strategy
        self.raw_data = data
        self.fee_model = fee_model
        self.portfolio = Portfolio(portfolio_config or PortfolioConfig())
        self.pool_config = pool_config or PoolConfig()
        self.rules = rules or get_market_rules()
        self.matcher = Matcher(
            fee_model,
            participation_cap=participation_cap,
            lot_size=self.portfolio.config.lot_size,
        )
        self.start = pd.Timestamp(start) if start else None
        self.end = pd.Timestamp(end) if end else None
        self.record_diagnostics = record_diagnostics
        self.constrained = constrained
        self.fixed_notional = to_decimal(fixed_notional)
        self.exit_params = strategy.exit_params

        self.frames: Dict[str, pd.DataFrame] = {}
        self.entry_by_date: Dict[pd.Timestamp, list[tuple[str, float]]] = {}
        self._last_close: Dict[str, Decimal] = {}
        self._orders: list[dict] = []
        self._fills: list[dict] = []
        self._signals: list[dict] = []
        self._diagnostics: list[dict] = []
        self._position_rows: list[dict] = []
        self._trade_rows: list[dict] = []

    # ---------- 准备 ----------

    def prepare(self) -> None:
        for code, raw in self.raw_data.items():
            if raw is None or raw.empty:
                continue
            frame = raw.copy()
            if "code" not in frame.columns:
                frame["code"] = code
            frame = frame[~frame.index.duplicated(keep="last")].sort_index()
            if self.start is not None:
                frame = frame[frame.index >= self.start]
            if self.end is not None:
                frame = frame[frame.index <= self.end]
            if frame.empty:
                continue

            prepared = self.strategy.prepare_and_signal(frame)
            prepared = filter_universe(prepared, self.pool_config)
            self.frames[code] = prepared

            entries = prepared[prepared["entry"] & prepared["in_pool"]]
            for timestamp, row in entries.iterrows():
                self.entry_by_date.setdefault(timestamp, []).append((code, float(row.get("score", 0.0) or 0.0)))

    def _trading_dates(self) -> list[pd.Timestamp]:
        dates: set[pd.Timestamp] = set()
        for frame in self.frames.values():
            dates.update(frame.index)
        return sorted(dates)

    # ---------- 行情 ----------

    def _row(self, code: str, timestamp: pd.Timestamp) -> Optional[pd.Series]:
        frame = self.frames.get(code)
        if frame is None or timestamp not in frame.index:
            return None
        row = frame.loc[timestamp]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        return row

    def _bar(self, code: str, timestamp: pd.Timestamp, row: pd.Series) -> Bar:
        trade_date = timestamp.date()
        close = _decimal(row.get("close"))
        pre_close = _decimal(row.get("pre_close"))
        if pre_close is None or pre_close <= 0:
            pre_close = self._last_close.get(code, close)

        limit_up = _optional_decimal(row.get("limit_up"))
        limit_down = _optional_decimal(row.get("limit_down"))
        if limit_up is None or limit_down is None:
            is_st = bool(row.get("is_st", False)) if not pd.isna(row.get("is_st", False)) else False
            listed_days = _optional_int(row.get("listed_days"))
            is_delisting = bool(row.get("is_delisting", False)) if not pd.isna(row.get("is_delisting", False)) else False
            delisting_day = _optional_int(row.get("delisting_day"))
            rule = self.rules.limit_rule(
                code,
                trade_date,
                is_st=is_st,
                listed_days=listed_days,
                is_delisting=is_delisting,
                delisting_day=delisting_day,
            )
            computed_up, computed_down = compute_limit_prices(pre_close, rule, self.rules.tick_size)
            limit_up = limit_up if limit_up is not None else computed_up
            limit_down = limit_down if limit_down is not None else computed_down

        return Bar(
            code=code,
            trade_date=trade_date,
            open=_decimal(row.get("open")),
            high=_decimal(row.get("high")),
            low=_decimal(row.get("low")),
            close=close,
            pre_close=pre_close,
            volume=_decimal(row.get("volume")),
            amount=_decimal(row.get("amount")),
            tradestatus=int(row.get("tradestatus", 1) or 0),
            is_st=bool(row.get("is_st", False)) if not pd.isna(row.get("is_st", False)) else False,
            limit_up=limit_up,
            limit_down=limit_down,
            avg_amount_20d=_optional_decimal(row.get("avg_amount_20d")),
            industry=_optional_str(row.get("industry")),
            market_cap=_optional_float(row.get("market_cap")),
            volatility_20d=_optional_float(row.get("volatility_20d")),
            adj_factor=_optional_decimal(row.get("adj_factor")),
        )

    # ---------- 主循环 ----------

    def run(self) -> BacktestResult:
        self.prepare()
        dates = self._trading_dates()
        pending_buys: list[OrderIntent] = []
        pending_sells: list[OrderIntent] = []

        for timestamp in dates:
            trade_date = timestamp.date()
            self.portfolio.start_new_day()
            cash_for_buys = self.portfolio.cash

            # 1) 卖出（昨日收盘信号）
            for order in pending_sells:
                row = self._row(order.code, timestamp)
                position = self.portfolio.positions.get(order.code)
                if row is None:
                    self._record_order(order, None, "rejected", "data_gap", timestamp)
                    continue
                if position is None:
                    self._record_order(order, None, "cancelled", "no_position", timestamp)
                    continue
                bar = self._bar(order.code, timestamp, row)
                sellable = min(order.quantity, position.sellable_qty, position.quantity)
                if sellable <= 0:
                    self._record_order(order, None, "rejected", "t_plus_1_not_sellable", timestamp)
                    continue
                sell_order = OrderIntent(
                    order_id=order.order_id,
                    code=order.code,
                    side="sell",
                    quantity=sellable,
                    signal_date=order.signal_date,
                    order_date=trade_date,
                    ref_price=order.ref_price,
                    reason=order.reason,
                    score=order.score,
                )
                outcome = self.matcher.match_sell(sell_order, bar)
                self._record_order(order, outcome, "filled" if outcome.filled else "rejected", outcome.reason, timestamp)
                if outcome.filled:
                    fill = Fill(
                        order.order_id,
                        order.code,
                        "sell",
                        trade_date,
                        outcome.price or Decimal("0"),
                        outcome.filled_qty,
                        outcome.fee_total,
                        outcome.slippage_cost,
                        order.reason,
                    )
                    trade = self.portfolio.apply_sell(fill, reason=order.reason)
                    self._record_fill(fill)
                    if trade is not None:
                        self._trade_rows.append(_trade_to_row(trade))
            pending_sells = []

            # 2) 买入（使用昨日收盘后的现金，不含当日卖出所得）
            spent = Decimal("0")
            for order in sorted(pending_buys, key=lambda item: (-item.score, item.code)):
                row = self._row(order.code, timestamp)
                if row is None:
                    self._record_order(order, None, "rejected", "data_gap", timestamp)
                    continue
                if self.constrained:
                    ok, reason = self.portfolio.can_open(_optional_str(row.get("industry")))
                    if not ok:
                        self._record_order(order, None, "rejected", reason, timestamp)
                        continue
                bar = self._bar(order.code, timestamp, row)
                remaining = cash_for_buys - spent
                if remaining <= 0:
                    self._record_order(order, None, "rejected", "insufficient_cash", timestamp)
                    continue

                est_price = bar.open + self.rules.tick_size * to_decimal(self.fee_model.slippage_ticks)
                lot = self.portfolio.config.lot_size
                if self.constrained:
                    affordable = int(remaining / est_price) // lot * lot if est_price > 0 else 0
                    quantity = min(order.quantity, affordable)
                else:
                    quantity = (order.quantity // lot) * lot
                max_price = None
                gap_max = float(self.strategy.entry_params.get("gap_max", 0.0) or 0.0)
                if order.ref_price is not None and gap_max > 0:
                    max_price = quantize_price(order.ref_price * (Decimal("1") + to_decimal(gap_max)))

                outcome = self.matcher.match_buy(
                    OrderIntent(
                        order.order_id,
                        order.code,
                        "buy",
                        quantity,
                        order.signal_date,
                        trade_date,
                        order.ref_price,
                        order.stop_price,
                        order.take_profit_price,
                        order.reason,
                        order.score,
                    ),
                    bar,
                    max_price=max_price,
                ) if quantity > 0 else None

                if outcome is None or not outcome.filled:
                    reason_text = "insufficient_cash" if outcome is None else outcome.reason
                    self._record_order(order, outcome, "rejected", reason_text, timestamp)
                    continue

                price = outcome.price or bar.open
                qty = outcome.filled_qty
                cost = price * Decimal(qty) + outcome.fee_total
                while cost > remaining and qty > lot:
                    qty -= lot
                    outcome = self.matcher.match_buy(
                        OrderIntent(order.order_id, order.code, "buy", qty, order.signal_date, trade_date,
                                    order.ref_price, order.stop_price, order.take_profit_price, order.reason,
                                    order.score),
                        bar,
                        max_price=max_price,
                    )
                    if not outcome.filled:
                        break
                    price = outcome.price or bar.open
                    qty = outcome.filled_qty
                    cost = price * Decimal(qty) + outcome.fee_total
                if not outcome.filled or cost > remaining:
                    self._record_order(order, outcome, "rejected", "insufficient_cash_after_fees", timestamp)
                    continue

                fill = Fill(
                    order.order_id,
                    order.code,
                    "buy",
                    trade_date,
                    price,
                    qty,
                    outcome.fee_total,
                    outcome.slippage_cost,
                    order.reason,
                )
                self.portfolio.apply_buy(
                    fill,
                    stop_price=order.stop_price or Decimal("0"),
                    take_profit_price=order.take_profit_price,
                    industry=_optional_str(row.get("industry")),
                    reason=order.reason,
                    signal_date=order.signal_date,
                )
                self._record_fill(fill)
                self._record_order(order, outcome, "filled", outcome.reason, timestamp)
                spent += cost
            pending_buys = []

            # 3) 收盘估值
            prices: Dict[str, Decimal] = {}
            for code in list(self.portfolio.positions):
                row = self._row(code, timestamp)
                if row is None:
                    price = self._last_close.get(code)
                    if price is not None:
                        prices[code] = price
                    continue
                close_price = _decimal(row.get("close"))
                if close_price <= 0:
                    close_price = self._last_close.get(code) or position.last_price
                prices[code] = close_price
                if close_price > 0:
                    self._last_close[code] = close_price
            self.portfolio.mark_to_market(prices, trade_date)
            self._record_positions(timestamp)

            # 4) 退出信号（T 日收盘 → T+1 开盘）
            for code, position in list(self.portfolio.positions.items()):
                row = self._row(code, timestamp)
                if row is None:
                    continue
                reason = self.strategy.exit_reason(position, row, trade_date)
                if reason is None:
                    continue
                pending_sells.append(
                    OrderIntent(
                        order_id=self._next_order_id(code, trade_date, "sell"),
                        code=code,
                        side="sell",
                        quantity=position.quantity,
                        signal_date=trade_date,
                        order_date=trade_date,
                        ref_price=_decimal(row.get("close")),
                        stop_price=position.stop_price,
                        reason=reason,
                        score=0.0,
                    )
                )

            # 5) 新信号（T 日收盘 → T+1 开盘）
            lot = self.portfolio.config.lot_size
            candidates = self.entry_by_date.get(timestamp, [])
            if candidates:
                ranked = sorted(candidates, key=lambda item: (-item[1], item[0]))
                selected = 0
                for code, _ in ranked:
                    if self.constrained and selected >= self.portfolio.config.max_new_positions_per_day:
                        break
                    if code in self.portfolio.positions:
                        continue
                    row = self._row(code, timestamp)
                    if row is None or not bool(row.get("entry", False)) or not bool(row.get("in_pool", False)):
                        continue
                    signal = self.strategy.evaluate_row(row, code, trade_date, 0)
                    self._record_signal(signal, selected=False)
                    if not signal.passed or signal.stop_price_real is None:
                        continue
                    if self.constrained:
                        qty, reason = self.portfolio.size_order(
                            signal.entry_ref, signal.stop_price_real, _optional_str(row.get("industry"))
                        )
                    else:
                        qty = int(self.fixed_notional / signal.entry_ref / lot) * lot if signal.entry_ref > 0 else 0
                        reason = "ok" if qty > 0 else "insufficient_budget"
                    if qty <= 0:
                        self._record_order_reject(signal, reason, trade_date)
                        continue
                    pending_buys.append(
                        OrderIntent(
                            order_id=self._next_order_id(code, trade_date, "buy"),
                            code=code,
                            side="buy",
                            quantity=qty,
                            signal_date=trade_date,
                            order_date=trade_date,
                            ref_price=signal.entry_ref,
                            stop_price=signal.stop_price_real,
                            take_profit_price=signal.take_profit_real,
                            reason=self.strategy.name,
                            score=signal.score,
                        )
                    )
                    selected += 1

        return self._build_result(dates)

    # ---------- 记录 ----------

    def _next_order_id(self, code: str, trade_date: date, side: str) -> str:
        return f"{trade_date:%Y%m%d}-{side}-{code}"

    def _record_order(self, order: OrderIntent, outcome: Any, status: str, reason: str,
                      timestamp: pd.Timestamp) -> None:
        self._orders.append(
            {
                "order_id": order.order_id,
                "code": order.code,
                "side": order.side,
                "signal_date": order.signal_date,
                "order_date": timestamp.date(),
                "quantity": order.quantity,
                "ref_price": float(order.ref_price) if order.ref_price is not None else None,
                "fill_qty": outcome.filled_qty if outcome is not None else 0,
                "fill_price": float(outcome.price) if outcome is not None and outcome.price is not None else None,
                "fees": float(outcome.fee_total) if outcome is not None else 0.0,
                "slippage": float(outcome.slippage_cost) if outcome is not None else 0.0,
                "status": status,
                "reject_reason": "" if status == "filled" else reason,
                "score": order.score,
            }
        )

    def _record_order_reject(self, signal: Signal, reason: str, trade_date: date) -> None:
        self._orders.append(
            {
                "order_id": f"{trade_date:%Y%m%d}-buy-{signal.code}-skipped",
                "code": signal.code,
                "side": "buy",
                "signal_date": signal.signal_date,
                "order_date": trade_date,
                "quantity": 0,
                "ref_price": float(signal.entry_ref),
                "fill_qty": 0,
                "fill_price": None,
                "fees": 0.0,
                "slippage": 0.0,
                "status": "rejected",
                "reject_reason": reason,
                "score": signal.score,
            }
        )

    def _record_fill(self, fill: Fill) -> None:
        self._fills.append(
            {
                "order_id": fill.order_id,
                "code": fill.code,
                "side": fill.side,
                "trade_date": fill.trade_date,
                "price": float(fill.price),
                "quantity": fill.quantity,
                "fees": float(fill.fees),
                "slippage": float(fill.slippage),
                "reason": fill.reason,
            }
        )

    def _record_signal(self, signal: Signal, selected: bool) -> None:
        row = {
            "signal_date": signal.signal_date,
            "code": signal.code,
            "strategy": self.strategy.name,
            "rule_version": signal.rule_version,
            "score": signal.score,
            "passed": signal.passed,
            "entry_ref": float(signal.entry_ref),
            "stop_price": float(signal.stop_price_real) if signal.stop_price_real is not None else None,
            "take_profit": float(signal.take_profit_real) if signal.take_profit_real is not None else None,
        }
        self._signals.append(row)
        if self.record_diagnostics:
            self._diagnostics.extend(signal.diagnostics_frame())

    def _record_positions(self, timestamp: pd.Timestamp) -> None:
        for code, position in self.portfolio.positions.items():
            self._position_rows.append(
                {
                    "trade_date": timestamp.date(),
                    "code": code,
                    "quantity": position.quantity,
                    "sellable_qty": position.sellable_qty,
                    "avg_cost": float(position.avg_cost),
                    "close": float(position.last_price),
                    "market_value": float(position.market_value),
                    "stop_price": float(position.stop_price) if position.stop_price is not None else None,
                    "take_profit": float(position.take_profit_price) if position.take_profit_price is not None else None,
                    "hold_days": position.hold_days,
                    "industry": position.industry,
                }
            )

    def _build_result(self, dates: Iterable[pd.Timestamp]) -> BacktestResult:
        equity = pd.DataFrame(
            [
                {
                    "trade_date": point.trade_date,
                    "cash": float(point.cash),
                    "market_value": float(point.market_value),
                    "equity": float(point.equity),
                    "drawdown": point.drawdown,
                    "positions": point.positions,
                    "exposure": point.exposure,
                }
                for point in self.portfolio.equity_curve
            ]
        )
        orders = pd.DataFrame(self._orders)
        fills = pd.DataFrame(self._fills)
        trades = pd.DataFrame(self._trade_rows)
        signals = pd.DataFrame(self._signals)
        diagnostics = pd.DataFrame(self._diagnostics)
        positions = pd.DataFrame(self._position_rows)
        metadata = {
            "strategy": self.strategy.name,
            "rule_version": self.strategy.version,
            "start": str(next(iter(dates)).date()) if dates else None,
            "end": str(list(dates)[-1].date()) if dates else None,
            "codes": len(self.frames),
            "days": len(list(dates)),
            "aum": float(self.portfolio.config.aum),
            "trades": len(trades),
            "final_equity": float(self.portfolio.equity),
        }
        return BacktestResult(
            equity=equity,
            orders=orders,
            fills=fills,
            trades=trades,
            signals=signals,
            diagnostics=diagnostics,
            positions=positions,
            metadata=metadata,
        )


# ---------- 行转换助手 ----------


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return Decimal("0")
    if not np.isfinite(number):
        return Decimal("0")
    return to_decimal(number)


def _optional_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number) or number <= 0:
        return None
    return to_decimal(number)


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _optional_str(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    text = str(value).strip()
    return text or None


def _trade_to_row(trade: TradeRecord) -> dict:
    return {
        "trade_id": trade.trade_id,
        "code": trade.code,
        "signal_date": trade.signal_date,
        "entry_date": trade.entry_date,
        "entry_price": float(trade.entry_price),
        "exit_date": trade.exit_date,
        "exit_price": float(trade.exit_price),
        "quantity": trade.quantity,
        "r_multiple": trade.r_multiple,
        "fees": float(trade.fees),
        "pnl": float(trade.pnl),
        "exit_reason": trade.exit_reason,
        "hold_days": trade.hold_days,
        "entry_reason": trade.entry_reason,
        "industry": trade.industry,
    }
