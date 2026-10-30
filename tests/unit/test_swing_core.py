# -*- coding: utf-8 -*-
"""回测核心规则、撮合、费用与组合的单元测试。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.sim.fees import FeeModel
from src.sim.matcher import Matcher, compute_limit_prices
from src.sim.portfolio import Portfolio, PortfolioConfig
from src.sim.rules import Board, MarketRules, classify_board, classify_exchange, quantize_price
from src.sim.types import Bar, Fill


@pytest.fixture(scope="module")
def rules() -> MarketRules:
    return MarketRules.load()


# ---------- 规则 ----------

def test_board_and_exchange_classification() -> None:
    assert classify_board("600519") is Board.MAIN
    assert classify_board("000001") is Board.MAIN
    assert classify_board("300750") is Board.GEM
    assert classify_board("688981") is Board.STAR
    assert classify_board("830799") is Board.BSE
    assert classify_exchange("600519") == "sh"
    assert classify_exchange("000001") == "sz"
    assert classify_exchange("830799") == "bj"


def test_main_st_limit_switches_2026_07_06(rules: MarketRules) -> None:
    before = rules.limit_rule("600000", date(2026, 7, 3), is_st=True)
    after = rules.limit_rule("600000", date(2026, 7, 6), is_st=True)
    normal = rules.limit_rule("600000", date(2026, 7, 6), is_st=False)
    assert (before.up, before.down) == (0.05, 0.05)
    assert (after.up, after.down) == (0.10, 0.10)
    assert (normal.up, normal.down) == (0.10, 0.10)


def test_gem_reform_and_star_bse(rules: MarketRules) -> None:
    assert rules.limit_rule("300750", date(2020, 8, 21)).up == 0.10
    assert rules.limit_rule("300750", date(2020, 8, 24)).up == 0.20
    assert rules.limit_rule("300750", date(2020, 8, 24), is_st=True).up == 0.20
    assert rules.limit_rule("688981", date(2019, 7, 22)).up == 0.20
    assert rules.limit_rule("830799", date(2021, 11, 15)).up == 0.30


def test_new_stock_and_delisting_rules(rules: MarketRules) -> None:
    old_ipo = rules.limit_rule("600000", date(2023, 4, 9), listed_days=1)
    new_ipo = rules.limit_rule("600000", date(2023, 4, 10), listed_days=3)
    new_ipo_later = rules.limit_rule("600000", date(2023, 4, 10), listed_days=6)
    assert (old_ipo.up, old_ipo.down) == (0.44, 0.36)
    assert new_ipo.unlimited
    assert new_ipo_later.up == 0.10

    assert rules.limit_rule("600000", date(2022, 5, 10), is_delisting=True,
                            delisting_day=1).unlimited
    assert rules.limit_rule("600000", date(2022, 5, 11), is_delisting=True,
                            delisting_day=2).up == 0.10
    assert rules.limit_rule("300750", date(2022, 5, 11), is_delisting=True,
                            delisting_day=2).up == 0.20


def test_limit_price_half_up_not_bankers_rounding() -> None:
    rules = MarketRules.load()
    rule = rules.limit_rule("600000", date(2024, 1, 2))
    up, down = compute_limit_prices(Decimal("10.05"), rule)
    assert up == Decimal("11.06")
    assert down == Decimal("9.05")
    assert quantize_price(Decimal("11.055")) == Decimal("11.06")
    assert quantize_price(Decimal("11.045")) == Decimal("11.05")


# ---------- 费用 ----------

def test_stamp_duty_switch_and_min_commission() -> None:
    model = FeeModel.load(cost_tier="base")
    before = model.compute("sell", 10, 1000, date(2023, 8, 25), "600000")
    after = model.compute("sell", 10, 1000, date(2023, 8, 28), "600000")
    assert before.stamp_duty == Decimal("10.00")
    assert after.stamp_duty == Decimal("5.00")
    assert before.commission == Decimal("5.00")
    assert after.commission == Decimal("5.00")


def test_transfer_fee_before_and_after_2022_04_29() -> None:
    # V3.0 errata E1：2015-08-01 起沪深统一 0.002% 双向（原「仅沪市」表述错误）
    model = FeeModel.load(cost_tier="base")
    sh_old = model.compute("buy", 10, 1000, date(2022, 4, 28), "600000")
    sz_old = model.compute("buy", 10, 1000, date(2022, 4, 28), "000001")
    sz_new = model.compute("buy", 10, 1000, date(2022, 4, 29), "000001")
    assert sh_old.transfer_fee == Decimal("0.20")
    assert sz_old.transfer_fee == Decimal("0.20")
    assert sz_new.transfer_fee == Decimal("0.10")


def test_cost_tiers_and_slippage() -> None:
    base = FeeModel.load(cost_tier="base")
    stress = FeeModel.load(cost_tier="stress")
    assert base.slippage_ticks == 1
    assert stress.slippage_ticks == 3
    assert base.slippage_price(Decimal("10.00"), "buy") == Decimal("10.01")
    assert base.slippage_price(Decimal("10.00"), "sell") == Decimal("9.99")
    assert stress.slippage_price(Decimal("10.00"), "buy") == Decimal("10.03")


# ---------- 撮合 ----------

def _bar(**kwargs) -> Bar:
    defaults = dict(
        code="600000",
        trade_date=date(2024, 1, 2),
        open=Decimal("10.00"),
        high=Decimal("10.20"),
        low=Decimal("9.90"),
        close=Decimal("10.10"),
        pre_close=Decimal("10.00"),
        volume=Decimal("10000000"),
        amount=Decimal("100000000"),
        tradestatus=1,
        limit_up=Decimal("11.00"),
        limit_down=Decimal("9.00"),
        avg_amount_20d=Decimal("100000000"),
    )
    defaults.update(kwargs)
    return Bar(**defaults)


def _order(code: str, side: str, qty: int) -> object:
    from src.sim.types import OrderIntent

    return OrderIntent(
        order_id="o1",
        code=code,
        side=side,
        quantity=qty,
        signal_date=date(2024, 1, 2),
        order_date=date(2024, 1, 3),
    )


def test_buy_rejects_one_word_and_open_limit_up() -> None:
    model = FeeModel.load()
    matcher = Matcher(model)
    one_word = _bar(open=Decimal("11"), high=Decimal("11"), low=Decimal("11"), close=Decimal("11"))
    assert matcher.match_buy(_order("600000", "buy", 1000), one_word).reason == "open_at_limit_up"
    open_up = _bar(open=Decimal("11"), high=Decimal("11.2"), low=Decimal("11"), close=Decimal("11.1"))
    assert matcher.match_buy(_order("600000", "buy", 1000), open_up).reason == "open_at_limit_up"
    suspended = _bar(tradestatus=0)
    assert matcher.match_buy(_order("600000", "buy", 1000), suspended).reason == "suspended"


def test_sell_limit_down_rules() -> None:
    matcher = Matcher(FeeModel.load())
    one_word_down = _bar(open=Decimal("9"), high=Decimal("9"), low=Decimal("9"), close=Decimal("9"))
    assert matcher.match_sell(_order("600000", "sell", 1000), one_word_down).reason == "one_word_limit_down"
    traded_at_down = _bar(open=Decimal("9"), high=Decimal("9.5"), low=Decimal("9"), close=Decimal("9.2"))
    outcome = matcher.match_sell(_order("600000", "sell", 1000), traded_at_down)
    assert outcome.filled
    # 跌停价是价格下限，滑点不得击穿
    assert outcome.price == Decimal("9.00")


def test_participation_cap_partial_fill() -> None:
    matcher = Matcher(FeeModel.load(), participation_cap=0.02)
    bar = _bar(volume=Decimal("1000000"), amount=Decimal("10000000"), avg_amount_20d=Decimal("10000000"))
    outcome = matcher.match_buy(_order("600000", "buy", 50000), bar)
    assert outcome.filled
    assert outcome.filled_qty == 19900  # 200000 / 10.01 向下取整手
    assert outcome.reason == "partial_participation_cap"


def test_buy_gap_guard() -> None:
    matcher = Matcher(FeeModel.load())
    bar = _bar(open=Decimal("10.40"))
    outcome = matcher.match_buy(_order("600000", "buy", 1000), bar, max_price=Decimal("10.30"))
    assert outcome.reason == "gap_exceeds_limit"


# ---------- 组合 ----------

def test_position_sizing_respects_risk_and_position_cap() -> None:
    portfolio = Portfolio(PortfolioConfig(aum=Decimal("300000"), risk_per_trade=0.01))
    qty, reason = portfolio.size_order(Decimal("10"), Decimal("9.5"))
    assert reason == "ok"
    assert qty == 6000  # 3000 / 0.5
    qty_capped, _ = portfolio.size_order(Decimal("10"), Decimal("9.8"))
    assert qty_capped == 7500  # 25% * 300000 / 10


def test_portfolio_t_plus_1_and_trade_accounting() -> None:
    portfolio = Portfolio(PortfolioConfig(aum=Decimal("300000")))
    buy = Fill("o1", "600000", "buy", date(2024, 1, 2), Decimal("10"), 1000, Decimal("5"), Decimal("0"))
    portfolio.apply_buy(buy, Decimal("9.5"), Decimal("11"), "银行")
    position = portfolio.positions["600000"]
    assert position.sellable_qty == 0
    portfolio.start_new_day()
    assert position.sellable_qty == 1000
    assert position.hold_days == 1

    sell = Fill("o2", "600000", "sell", date(2024, 1, 3), Decimal("11"), 1000, Decimal("10"), Decimal("0"))
    trade = portfolio.apply_sell(sell, reason="take_profit")
    assert trade is not None
    assert trade.pnl == Decimal("985.00")  # (11000 - 10) - (10000 + 5)
    assert "600000" not in portfolio.positions


def test_drawdown_circuit_breaker_blocks_new_positions() -> None:
    portfolio = Portfolio(PortfolioConfig(aum=Decimal("100000")))
    portfolio.cash = Decimal("81000")
    portfolio.peak_equity = Decimal("100000")
    portfolio.mark_to_market({}, date(2024, 1, 2))
    ok, reason = portfolio.can_open()
    assert not ok
    assert reason == "drawdown_cooldown"
    for _ in range(20):
        portfolio.start_new_day()
    ok_after, _ = portfolio.can_open()
    assert ok_after


def test_industry_limit() -> None:
    portfolio = Portfolio(PortfolioConfig(aum=Decimal("300000"), max_per_industry=1))
    for code in ("600000", "600001"):
        fill = Fill(code, code, "buy", date(2024, 1, 2), Decimal("10"), 100, Decimal("5"), Decimal("0"))
        portfolio.apply_buy(fill, Decimal("9.5"), Decimal("11"), "银行")
    ok, reason = portfolio.can_open("银行")
    assert not ok
    assert reason == "industry_limit"
