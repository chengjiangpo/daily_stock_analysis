# -*- coding: utf-8 -*-
"""A 股分段交易规则加载器（方案 §4.2）。

所有涨跌幅、tick、板块判定集中在此，撮合与回测不得硬编码单一比例。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "market_rules.yaml"


class Board(str, Enum):
    MAIN = "main"
    GEM = "gem"
    STAR = "star"
    BSE = "bse"


def normalize_code(code: str) -> str:
    """去掉 sh./sz./bj. 前缀与 .SH/.SZ 后缀，返回 6 位代码。"""
    text = str(code).strip().lower()
    for prefix in ("sh.", "sz.", "bj."):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    for suffix in (".sh", ".sz", ".bj"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text


def classify_board(code: str) -> Board:
    normalized = normalize_code(code)
    if len(normalized) != 6 or not normalized.isdigit():
        raise ValueError(f"无法识别的 A 股代码: {code!r}")
    if normalized.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return Board.MAIN
    if normalized.startswith(("300", "301")):
        return Board.GEM
    if normalized.startswith(("688", "689")):
        return Board.STAR
    if normalized.startswith(("4", "8", "920")):
        return Board.BSE
    raise ValueError(f"不在已知板块范围内的代码: {code!r}")


def classify_exchange(code: str) -> str:
    """返回 sh/sz/bj，用于过户费规则。"""
    normalized = normalize_code(code)
    board = classify_board(normalized)
    if board == Board.BSE:
        return "bj"
    if board in (Board.STAR,) or normalized.startswith(("6",)):
        return "sh"
    return "sz"


def to_decimal(value: Any) -> Decimal:
    """把 float/str/np.float64 安全转成 Decimal，避免二进制浮点误差。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_price(value: Decimal | float | str, tick: Decimal | str = Decimal("0.01")) -> Decimal:
    """按 A 股最小价位四舍五入（ROUND_HALF_UP，禁止银行家舍入）。"""
    tick_dec = to_decimal(tick) if not isinstance(tick, Decimal) else tick
    return to_decimal(value).quantize(tick_dec, rounding=ROUND_HALF_UP)


def parse_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


@dataclass(frozen=True)
class LimitRule:
    """当日涨跌幅规则；up/down 为 None 表示不设限。"""

    up: Optional[float]
    down: Optional[float]
    reason: str

    @property
    def unlimited(self) -> bool:
        return self.up is None and self.down is None


@dataclass(frozen=True)
class _LimitSegment:
    board: Optional[Board]
    st: Optional[bool]
    date_from: Optional[date]
    date_to: Optional[date]
    up: Optional[float]
    down: Optional[float]

    def matches(self, board: Board, trade_date: date, is_st: bool) -> bool:
        if self.board is not None and self.board != board:
            return False
        if self.st is not None and self.st != is_st:
            return False
        if self.date_from is not None and trade_date < self.date_from:
            return False
        if self.date_to is not None and trade_date > self.date_to:
            return False
        return True


@dataclass(frozen=True)
class _NewStockSegment:
    board: Board
    date_from: Optional[date]
    date_to: Optional[date]
    first_days: int
    up: Optional[float]
    down: Optional[float]

    def matches(self, board: Board, trade_date: date, listed_days: int) -> bool:
        if self.board != board or listed_days > self.first_days:
            return False
        if self.date_from is not None and trade_date < self.date_from:
            return False
        if self.date_to is not None and trade_date > self.date_to:
            return False
        return True


class MarketRules:
    """分段规则集合。实例不可变，可安全复用。"""

    def __init__(
        self,
        tick_size: Decimal,
        lot_size: int,
        limit_segments: Iterable[_LimitSegment],
        new_stock_segments: Iterable[_NewStockSegment],
        delisting_board_pct: dict[Board, float],
    ) -> None:
        self.tick_size = tick_size
        self.lot_size = lot_size
        self._limit_segments = tuple(limit_segments)
        self._new_stock_segments = tuple(new_stock_segments)
        self._delisting_board_pct = dict(delisting_board_pct)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "MarketRules":
        rules_path = Path(path) if path else Path(os.getenv("BACKTEST_RULES_PATH", DEFAULT_RULES_PATH))
        with rules_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}

        limit_segments = [
            _LimitSegment(
                board=Board(item["board"]) if item.get("board") else None,
                st=item.get("st"),
                date_from=parse_date(item.get("date_from")),
                date_to=parse_date(item.get("date_to")),
                up=item.get("up"),
                down=item.get("down"),
            )
            for item in raw.get("limit_segments", [])
        ]
        new_stock_segments = [
            _NewStockSegment(
                board=Board(item["board"]),
                date_from=parse_date(item.get("date_from")),
                date_to=parse_date(item.get("date_to")),
                first_days=int(item.get("first_days", 1)),
                up=item.get("up"),
                down=item.get("down"),
            )
            for item in raw.get("new_stock_segments", [])
        ]
        delisting_pct = {
            Board(name): float(value)
            for name, value in (raw.get("delisting", {}).get("board_pct", {}) or {}).items()
        }
        return cls(
            tick_size=to_decimal(raw.get("tick_size", "0.01")),
            lot_size=int(raw.get("lot_size", 100)),
            limit_segments=limit_segments,
            new_stock_segments=new_stock_segments,
            delisting_board_pct=delisting_pct,
        )

    def limit_rule(
        self,
        code: str,
        trade_date: date,
        is_st: bool = False,
        listed_days: Optional[int] = None,
        is_delisting: bool = False,
        delisting_day: Optional[int] = None,
    ) -> LimitRule:
        """返回该股当日的涨跌停幅度。

        listed_days 从 1 开始（上市首日为 1）；delisting_day 同样从 1 开始。
        """
        board = classify_board(code)

        if is_delisting:
            if delisting_day == 1:
                return LimitRule(None, None, "delisting_first_day")
            pct = self._delisting_board_pct.get(board)
            if pct is None:
                raise ValueError(f"缺少 {board.value} 退市整理期涨跌幅配置")
            return LimitRule(pct, pct, "delisting_period")

        if listed_days is not None and listed_days >= 1:
            for segment in self._new_stock_segments:
                if segment.matches(board, trade_date, listed_days):
                    return LimitRule(segment.up, segment.down, "new_stock")

        for segment in self._limit_segments:
            if segment.matches(board, trade_date, bool(is_st)):
                return LimitRule(segment.up, segment.down, "regular")

        raise ValueError(
            f"无匹配涨跌幅规则: code={code} date={trade_date} is_st={is_st} board={board.value}"
        )


_DEFAULT_RULES: Optional[MarketRules] = None


def get_market_rules() -> MarketRules:
    global _DEFAULT_RULES
    if _DEFAULT_RULES is None:
        _DEFAULT_RULES = MarketRules.load()
    return _DEFAULT_RULES
