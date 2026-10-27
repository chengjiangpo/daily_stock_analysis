# -*- coding: utf-8 -*-
"""回测撮合与组合模块。"""

from .fees import FeeModel, build_fee_model
from .matcher import Matcher, compute_limit_prices
from .rules import Board, MarketRules, classify_board, get_market_rules
from .types import Bar, EquityPoint, Fill, OrderIntent, Position, TradeRecord

__all__ = [
    "Bar",
    "Board",
    "EquityPoint",
    "FeeModel",
    "Fill",
    "MarketRules",
    "Matcher",
    "OrderIntent",
    "Position",
    "TradeRecord",
    "build_fee_model",
    "classify_board",
    "compute_limit_prices",
    "get_market_rules",
]
