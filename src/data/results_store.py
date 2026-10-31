# -*- coding: utf-8 -*-
"""回测结果落库（V3.0 §5.2 I-4 剩余部分）。

把 ``persist_result`` 写出的 CSV/JSON 结果同时装入关系库，使回测产物可查询、
可跨机器复核、可作为「试验账本」（§3.2 预算 ≤200 次）的机器可读底账。

与 ``bars_store`` 的分工：
- ``bars_store``：**输入**（行情、抓取溯源）——数据层，与运行无关；
- 本模块：**输出**（信号、委托、成交、持仓、净值、交易、运行元数据）——研究产物，按 ``run_id`` 组织。

表名沿用 §15.1 已确立的 ``swing_`` 前缀（与 ``swing_bars_daily`` 一致），
与 V3.0 §5.2 的清单对应如下：

| V3.0 §5.2 表名 | 实际表名 | 说明 |
|---|---|---|
| `bars_daily` | `swing_bars_daily` | 已由 ``bars_store`` 实现 |
| `pool_daily` | `swing_pool_daily` | **运行无关**的共享表（见下） |
| `daily_signals` | `swing_daily_signals` | 按 run_id 隔离 |
| `orders` | `swing_orders` | 按 run_id 隔离 |
| `fills` | `swing_fills` | 按 run_id 隔离 |
| `positions_daily` | `swing_positions_daily` | 按 run_id 隔离 |
| `portfolio_daily` | `swing_portfolio_daily` | 按 run_id 隔离 |
| `trade_records` | `swing_trade_records` | 按 run_id 隔离 |
| `backtest_runs` | `swing_backtest_runs` | 运行元数据与绩效指标 |

``pool_daily`` 为什么不分 run：股票池（``in_pool``/``reason``）是「行情 + ``PoolConfig``」
的纯函数，与策略参数、区间、AUM 无关。若按 run 存，3300 只 × 1900 日 ≈ 630 万行/次，
每次回测都要重写一遍既浪费又无信息量。因此它与 ``swing_bars_daily`` 同属数据层，
由 ``scripts/ingest_swing_bars.py --with-pool`` 一次性落库，各 run 直接按日期关联。

幂等：同一 ``run_id`` 重跑时先按 ``run_id`` 删除旧行再写入，不会残留上一次的行集合。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pandas as pd
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from src.data.bars_store import make_engine, mask_url, resolve_schema, resolve_url

logger = logging.getLogger(__name__)

ENV_URL_KEY = "SWING_RESULTS_DATABASE_URL"
ENV_SCHEMA_KEY = "SWING_RESULTS_SCHEMA"
# 结果表默认与行情表同库同 schema（生产即 swing_bars），便于同库关联查询
FALLBACK_URL_KEY = "SWING_BARS_DATABASE_URL"
FALLBACK_SCHEMA_KEY = "SWING_BARS_SCHEMA"


class ResultsBase(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# ORM
# ---------------------------------------------------------------------------


class BacktestRunRow(ResultsBase):
    """一次回测运行（对应 run_metadata.json + performance_metrics.json）。"""

    __tablename__ = "swing_backtest_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy: Mapped[Optional[str]] = mapped_column(String(64))
    rule_version: Mapped[Optional[str]] = mapped_column(String(32))
    cost_tier: Mapped[Optional[str]] = mapped_column(String(32))
    mode: Mapped[Optional[str]] = mapped_column(String(32))
    start_date: Mapped[Optional[date]] = mapped_column(Date)
    end_date: Mapped[Optional[date]] = mapped_column(Date)
    codes: Mapped[Optional[int]] = mapped_column(Integer)
    days: Mapped[Optional[int]] = mapped_column(Integer)
    aum: Mapped[Optional[float]] = mapped_column(Float)
    fixed_notional: Mapped[Optional[float]] = mapped_column(Float)
    final_equity: Mapped[Optional[float]] = mapped_column(Float)
    total_return: Mapped[Optional[float]] = mapped_column(Float)
    cagr: Mapped[Optional[float]] = mapped_column(Float)
    annual_vol: Mapped[Optional[float]] = mapped_column(Float)
    sharpe: Mapped[Optional[float]] = mapped_column(Float)
    max_drawdown: Mapped[Optional[float]] = mapped_column(Float)
    n_trades: Mapped[Optional[int]] = mapped_column(Integer)
    win_rate: Mapped[Optional[float]] = mapped_column(Float)
    profit_factor: Mapped[Optional[float]] = mapped_column(Float)
    data_hash: Mapped[Optional[str]] = mapped_column(String(64))
    git_commit: Mapped[Optional[str]] = mapped_column(String(64))
    params_json: Mapped[Optional[str]] = mapped_column(Text)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text)
    performance_json: Mapped[Optional[str]] = mapped_column(Text)
    trade_metrics_json: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False))

    __table_args__ = (Index("ix_swing_backtest_runs_strategy", "strategy", "created_at"),)


class PoolDailyRow(ResultsBase):
    """时点股票池（运行无关的共享表）。"""

    __tablename__ = "swing_pool_daily"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    in_pool: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[Optional[str]] = mapped_column(String(64))

    __table_args__ = (Index("ix_swing_pool_daily_date_pool", "trade_date", "in_pool"),)


class DailySignalRow(ResultsBase):
    __tablename__ = "swing_daily_signals"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signal_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    strategy: Mapped[Optional[str]] = mapped_column(String(64))
    rule_version: Mapped[Optional[str]] = mapped_column(String(32))
    score: Mapped[Optional[float]] = mapped_column(Float)
    passed: Mapped[Optional[bool]] = mapped_column(Boolean)
    entry_ref: Mapped[Optional[float]] = mapped_column(Float)
    stop_price: Mapped[Optional[float]] = mapped_column(Float)
    take_profit: Mapped[Optional[float]] = mapped_column(Float)


class OrderRow(ResultsBase):
    __tablename__ = "swing_orders"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[Optional[str]] = mapped_column(String(6))
    side: Mapped[Optional[str]] = mapped_column(String(8))
    signal_date: Mapped[Optional[date]] = mapped_column(Date)
    order_date: Mapped[Optional[date]] = mapped_column(Date)
    quantity: Mapped[Optional[int]] = mapped_column(Integer)
    ref_price: Mapped[Optional[float]] = mapped_column(Float)
    fill_qty: Mapped[Optional[int]] = mapped_column(Integer)
    fill_price: Mapped[Optional[float]] = mapped_column(Float)
    fees: Mapped[Optional[float]] = mapped_column(Float)
    slippage: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[Optional[str]] = mapped_column(String(16))
    reject_reason: Mapped[Optional[str]] = mapped_column(String(64))
    score: Mapped[Optional[float]] = mapped_column(Float)

    __table_args__ = (Index("ix_swing_orders_code_date", "code", "order_date"),)


class FillRow(ResultsBase):
    """成交记录。引擎侧无天然唯一键，故用自增主键 + (run_id, order_id) 索引。"""

    __tablename__ = "swing_fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[Optional[str]] = mapped_column(String(64))
    code: Mapped[Optional[str]] = mapped_column(String(6))
    side: Mapped[Optional[str]] = mapped_column(String(8))
    trade_date: Mapped[Optional[date]] = mapped_column(Date)
    price: Mapped[Optional[float]] = mapped_column(Float)
    quantity: Mapped[Optional[int]] = mapped_column(Integer)
    fees: Mapped[Optional[float]] = mapped_column(Float)
    slippage: Mapped[Optional[float]] = mapped_column(Float)
    reason: Mapped[Optional[str]] = mapped_column(String(64))

    __table_args__ = (Index("ix_swing_fills_run_order", "run_id", "order_id"),)


class PositionDailyRow(ResultsBase):
    __tablename__ = "swing_positions_daily"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    quantity: Mapped[Optional[int]] = mapped_column(Integer)
    sellable_qty: Mapped[Optional[int]] = mapped_column(Integer)
    avg_cost: Mapped[Optional[float]] = mapped_column(Float)
    close: Mapped[Optional[float]] = mapped_column(Float)
    market_value: Mapped[Optional[float]] = mapped_column(Float)
    stop_price: Mapped[Optional[float]] = mapped_column(Float)
    take_profit: Mapped[Optional[float]] = mapped_column(Float)
    hold_days: Mapped[Optional[int]] = mapped_column(Integer)
    industry: Mapped[Optional[str]] = mapped_column(String(64))


class PortfolioDailyRow(ResultsBase):
    __tablename__ = "swing_portfolio_daily"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    cash: Mapped[Optional[float]] = mapped_column(Float)
    market_value: Mapped[Optional[float]] = mapped_column(Float)
    equity: Mapped[Optional[float]] = mapped_column(Float)
    drawdown: Mapped[Optional[float]] = mapped_column(Float)
    positions: Mapped[Optional[int]] = mapped_column(Integer)
    exposure: Mapped[Optional[float]] = mapped_column(Float)


class TradeRecordRow(ResultsBase):
    __tablename__ = "swing_trade_records"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[Optional[str]] = mapped_column(String(6))
    signal_date: Mapped[Optional[date]] = mapped_column(Date)
    entry_date: Mapped[Optional[date]] = mapped_column(Date)
    entry_price: Mapped[Optional[float]] = mapped_column(Float)
    exit_date: Mapped[Optional[date]] = mapped_column(Date)
    exit_price: Mapped[Optional[float]] = mapped_column(Float)
    quantity: Mapped[Optional[int]] = mapped_column(Integer)
    r_multiple: Mapped[Optional[float]] = mapped_column(Float)
    fees: Mapped[Optional[float]] = mapped_column(Float)
    pnl: Mapped[Optional[float]] = mapped_column(Float)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(32))
    hold_days: Mapped[Optional[int]] = mapped_column(Integer)
    entry_reason: Mapped[Optional[str]] = mapped_column(String(128))
    industry: Mapped[Optional[str]] = mapped_column(String(64))

    __table_args__ = (Index("ix_swing_trade_records_code", "run_id", "code"),)


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------


def resolve_results_url(url: Optional[str] = None) -> str:
    """解析结果库 URL：显式参数 > SWING_RESULTS_DATABASE_URL > 行情库 URL > 默认 sqlite。"""
    if url:
        return resolve_url(url)
    import os

    explicit = os.environ.get(ENV_URL_KEY)
    if explicit:
        return resolve_url(explicit)
    fallback = os.environ.get(FALLBACK_URL_KEY) or os.environ.get("DATABASE_URL")
    return resolve_url(fallback or None)


def resolve_results_schema(schema: Optional[str] = None) -> Optional[str]:
    """解析结果库 schema：显式参数 > SWING_RESULTS_SCHEMA > SWING_BARS_SCHEMA > None。"""
    if schema:
        return schema
    import os

    return os.environ.get(ENV_SCHEMA_KEY) or os.environ.get(FALLBACK_SCHEMA_KEY) or None


def make_results_engine(url: Optional[str] = None, schema: Optional[str] = None) -> Engine:
    return make_engine(resolve_results_url(url), resolve_results_schema(schema))


def ensure_results_schema(engine: Engine) -> None:
    ResultsBase.metadata.create_all(engine)


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    try:
        if isinstance(value, float) and pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    return timestamp.date()


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not pd.notna(number):
        return None
    return number


def _to_int(value: Any) -> Optional[int]:
    number = _to_float(value)
    return None if number is None else int(number)


def _to_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return bool(value)


def _to_str(value: Any, limit: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value)
    return text[:limit] if limit else text


@dataclass
class RunPayload:
    """一次运行要落库的全部内容（缺列/空 DataFrame 均可）。"""

    run_id: str
    metadata: Dict[str, Any]
    performance: Dict[str, Any] = field(default_factory=dict)
    trade_metrics: Dict[str, Any] = field(default_factory=dict)
    equity: Optional[pd.DataFrame] = None
    signals: Optional[pd.DataFrame] = None
    orders: Optional[pd.DataFrame] = None
    fills: Optional[pd.DataFrame] = None
    positions: Optional[pd.DataFrame] = None
    trades: Optional[pd.DataFrame] = None
    diagnostics: Optional[pd.DataFrame] = None
    """诊断表当前不单独建表（列随策略条件变化），仅保留入口以便未来扩展。"""


_ROW_BUILDERS = {
    "signals": (
        DailySignalRow,
        lambda frame, run_id: [
            {
                "run_id": run_id,
                "signal_date": _to_date(row.get("signal_date")),
                "code": _to_str(row.get("code"), 6),
                "strategy": _to_str(row.get("strategy"), 64),
                "rule_version": _to_str(row.get("rule_version"), 32),
                "score": _to_float(row.get("score")),
                "passed": _to_bool(row.get("passed")),
                "entry_ref": _to_float(row.get("entry_ref")),
                "stop_price": _to_float(row.get("stop_price")),
                "take_profit": _to_float(row.get("take_profit")),
            }
            for _, row in frame.iterrows()
            if row.get("signal_date") is not None and row.get("code") is not None
        ],
    ),
    "orders": (
        OrderRow,
        lambda frame, run_id: [
            {
                "run_id": run_id,
                "order_id": _to_str(row.get("order_id"), 64),
                "code": _to_str(row.get("code"), 6),
                "side": _to_str(row.get("side"), 8),
                "signal_date": _to_date(row.get("signal_date")),
                "order_date": _to_date(row.get("order_date")),
                "quantity": _to_int(row.get("quantity")),
                "ref_price": _to_float(row.get("ref_price")),
                "fill_qty": _to_int(row.get("fill_qty")),
                "fill_price": _to_float(row.get("fill_price")),
                "fees": _to_float(row.get("fees")),
                "slippage": _to_float(row.get("slippage")),
                "status": _to_str(row.get("status"), 16),
                "reject_reason": _to_str(row.get("reject_reason"), 64),
                "score": _to_float(row.get("score")),
            }
            for _, row in frame.iterrows()
            if row.get("order_id") is not None
        ],
    ),
    "fills": (
        FillRow,
        lambda frame, run_id: [
            {
                "run_id": run_id,
                "order_id": _to_str(row.get("order_id"), 64),
                "code": _to_str(row.get("code"), 6),
                "side": _to_str(row.get("side"), 8),
                "trade_date": _to_date(row.get("trade_date")),
                "price": _to_float(row.get("price")),
                "quantity": _to_int(row.get("quantity")),
                "fees": _to_float(row.get("fees")),
                "slippage": _to_float(row.get("slippage")),
                "reason": _to_str(row.get("reason"), 64),
            }
            for _, row in frame.iterrows()
        ],
    ),
    "positions": (
        PositionDailyRow,
        lambda frame, run_id: [
            {
                "run_id": run_id,
                "trade_date": _to_date(row.get("trade_date")),
                "code": _to_str(row.get("code"), 6),
                "quantity": _to_int(row.get("quantity")),
                "sellable_qty": _to_int(row.get("sellable_qty")),
                "avg_cost": _to_float(row.get("avg_cost")),
                "close": _to_float(row.get("close")),
                "market_value": _to_float(row.get("market_value")),
                "stop_price": _to_float(row.get("stop_price")),
                "take_profit": _to_float(row.get("take_profit")),
                "hold_days": _to_int(row.get("hold_days")),
                "industry": _to_str(row.get("industry"), 64),
            }
            for _, row in frame.iterrows()
            if row.get("trade_date") is not None and row.get("code") is not None
        ],
    ),
    "trades": (
        TradeRecordRow,
        lambda frame, run_id: [
            {
                "run_id": run_id,
                "trade_id": _to_str(row.get("trade_id"), 64),
                "code": _to_str(row.get("code"), 6),
                "signal_date": _to_date(row.get("signal_date")),
                "entry_date": _to_date(row.get("entry_date")),
                "entry_price": _to_float(row.get("entry_price")),
                "exit_date": _to_date(row.get("exit_date")),
                "exit_price": _to_float(row.get("exit_price")),
                "quantity": _to_int(row.get("quantity")),
                "r_multiple": _to_float(row.get("r_multiple")),
                "fees": _to_float(row.get("fees")),
                "pnl": _to_float(row.get("pnl")),
                "exit_reason": _to_str(row.get("exit_reason"), 32),
                "hold_days": _to_int(row.get("hold_days")),
                "entry_reason": _to_str(row.get("entry_reason"), 128),
                "industry": _to_str(row.get("industry"), 64),
            }
            for _, row in frame.iterrows()
            if row.get("trade_id") is not None
        ],
    ),
}


def _equity_rows(frame: Optional[pd.DataFrame], run_id: str) -> list[dict]:
    if frame is None or frame.empty:
        return []
    rows = []
    for _, row in frame.iterrows():
        trade_date = _to_date(row.get("trade_date"))
        if trade_date is None:
            continue
        rows.append(
            {
                "run_id": run_id,
                "trade_date": trade_date,
                "cash": _to_float(row.get("cash")),
                "market_value": _to_float(row.get("market_value")),
                "equity": _to_float(row.get("equity")),
                "drawdown": _to_float(row.get("drawdown")),
                "positions": _to_int(row.get("positions")),
                "exposure": _to_float(row.get("exposure")),
            }
        )
    return rows


def save_run(
    payload: RunPayload,
    url: Optional[str] = None,
    schema: Optional[str] = None,
    batch_size: int = 5000,
) -> Dict[str, int]:
    """把一次运行写入结果库（幂等：先按 run_id 删除旧行）。

    返回各表写入行数，便于与 CSV 行数核对。
    """
    engine = make_results_engine(url, schema)
    ensure_results_schema(engine)
    counts: Dict[str, int] = {}

    with Session(engine) as session:
        # 1) 清理同一 run_id 的旧行（重跑幂等）
        for model in (PositionDailyRow, PortfolioDailyRow, FillRow, OrderRow,
                      DailySignalRow, TradeRecordRow, BacktestRunRow):
            session.execute(delete(model).where(model.run_id == payload.run_id))
        session.flush()

        # 2) 运行元数据
        metadata = payload.metadata or {}
        performance = payload.performance or {}
        trade_metrics = payload.trade_metrics or {}
        engine_meta = metadata.get("engine") or {}
        period = metadata.get("period") or [None, None]
        cost_stat = {
            "total_return": performance.get("total_return"),
            "cagr": performance.get("cagr"),
            "annual_vol": performance.get("annual_vol"),
            "sharpe": performance.get("sharpe"),
            "max_drawdown": performance.get("max_drawdown"),
        }
        run_row = BacktestRunRow(
            run_id=payload.run_id,
            strategy=_to_str(metadata.get("strategy"), 64),
            rule_version=_to_str(engine_meta.get("rule_version"), 32),
            cost_tier=_to_str(metadata.get("cost_tier"), 32),
            mode=_to_str(metadata.get("mode"), 32),
            start_date=_to_date(period[0] if len(period) > 0 else None),
            end_date=_to_date(period[1] if len(period) > 1 else None),
            codes=_to_int(metadata.get("codes") or engine_meta.get("codes")),
            days=_to_int(engine_meta.get("days")),
            aum=_to_float(engine_meta.get("aum")),
            fixed_notional=_to_float(metadata.get("fixed_notional")),
            final_equity=_to_float(engine_meta.get("final_equity")),
            total_return=_to_float(cost_stat["total_return"]),
            cagr=_to_float(cost_stat["cagr"]),
            annual_vol=_to_float(cost_stat["annual_vol"]),
            sharpe=_to_float(cost_stat["sharpe"]),
            max_drawdown=_to_float(cost_stat["max_drawdown"]),
            n_trades=_to_int(trade_metrics.get("n_trades") or engine_meta.get("trades")),
            win_rate=_to_float(trade_metrics.get("win_rate")),
            profit_factor=_to_float(trade_metrics.get("profit_factor")),
            data_hash=_to_str(metadata.get("data_hash"), 64),
            git_commit=_to_str(metadata.get("git_commit"), 64),
            params_json=json.dumps(metadata.get("params") or {}, ensure_ascii=False, default=str),
            metadata_json=json.dumps(metadata, ensure_ascii=False, default=str),
            performance_json=json.dumps(performance, ensure_ascii=False, default=str),
            trade_metrics_json=json.dumps(trade_metrics, ensure_ascii=False, default=str),
            created_at=pd.Timestamp.now().to_pydatetime(),
        )
        session.add(run_row)
        counts["runs"] = 1

        # 3) 净值曲线
        equity_rows = _equity_rows(payload.equity, payload.run_id)
        for start_at in range(0, len(equity_rows), batch_size):
            session.bulk_insert_mappings(
                PortfolioDailyRow, equity_rows[start_at : start_at + batch_size]
            )
        counts["portfolio_daily"] = len(equity_rows)

        # 4) 其余结果表
        for name, (model, builder) in _ROW_BUILDERS.items():
            frame = getattr(payload, name)
            if frame is None or frame.empty:
                counts[name] = 0
                continue
            rows = builder(frame, payload.run_id)
            for start_at in range(0, len(rows), batch_size):
                session.bulk_insert_mappings(model, rows[start_at : start_at + batch_size])
            counts[name] = len(rows)

        session.commit()
    return counts


def save_pool_daily(
    frames: Dict[str, pd.DataFrame],
    url: Optional[str] = None,
    schema: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    batch_size: int = 20000,
) -> int:
    """把时点股票池写入共享表 ``swing_pool_daily``（幂等：按日期区间先删后写）。

    股票池是行情 + ``PoolConfig`` 的纯函数，与回测运行无关，因此不按 run_id 隔离。
    """
    from src.data.pool import build_pool_table

    table = build_pool_table(frames)
    if table.empty:
        return 0
    table["trade_date"] = pd.to_datetime(table["trade_date"])
    if start:
        table = table[table["trade_date"] >= pd.Timestamp(start)]
    if end:
        table = table[table["trade_date"] <= pd.Timestamp(end)]
    if table.empty:
        return 0

    engine = make_results_engine(url, schema)
    ensure_results_schema(engine)
    with Session(engine) as session:
        session.execute(
            delete(PoolDailyRow).where(
                PoolDailyRow.trade_date.between(
                    table["trade_date"].min().date(), table["trade_date"].max().date()
                )
            )
        )
        rows = [
            {
                "trade_date": pd.Timestamp(row.trade_date).date(),
                "code": str(row.code).zfill(6),
                "in_pool": bool(row.in_pool),
                "reason": _to_str(row.reason, 64),
            }
            for row in table.itertuples(index=False)
        ]
        for start_at in range(0, len(rows), batch_size):
            session.bulk_insert_mappings(PoolDailyRow, rows[start_at : start_at + batch_size])
        session.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


@dataclass
class StoredRun:
    run_id: str
    metadata: Dict[str, Any]
    performance: Dict[str, Any]
    trade_metrics: Dict[str, Any]
    frames: Dict[str, pd.DataFrame]

    def frame(self, name: str) -> pd.DataFrame:
        return self.frames.get(name, pd.DataFrame())


def _read_frame(session: Session, model, run_id: str, order_by=None) -> pd.DataFrame:
    statement = select(model).where(model.run_id == run_id)
    if order_by is not None:
        statement = statement.order_by(*order_by)
    rows = session.execute(statement).scalars().all()
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(
        [
            {column.name: getattr(row, column.name) for column in model.__table__.columns}
            for row in rows
        ]
    )
    return frame.drop(columns=["run_id", "id"], errors="ignore")


_READ_ORDER = {
    "portfolio_daily": ("trade_date",),
    "positions_daily": ("trade_date", "code"),
    "signals": ("signal_date", "code"),
    "orders": ("order_date", "order_id"),
    "fills": ("trade_date", "order_id"),
    "trades": ("entry_date", "trade_id"),
}


def load_run(
    run_id: str,
    url: Optional[str] = None,
    schema: Optional[str] = None,
) -> Optional[StoredRun]:
    """按 run_id 读回一次回测运行；不存在时返回 None。"""
    engine = make_results_engine(url, schema)
    ensure_results_schema(engine)
    with Session(engine) as session:
        run_row = session.get(BacktestRunRow, run_id)
        if run_row is None:
            return None
        metadata = json.loads(run_row.metadata_json or "{}")
        performance = json.loads(run_row.performance_json or "{}")
        trade_metrics = json.loads(run_row.trade_metrics_json or "{}")
        frames = {
            "portfolio_daily": _read_frame(
                session, PortfolioDailyRow, run_id, (PortfolioDailyRow.trade_date,)
            ),
            "positions_daily": _read_frame(
                session, PositionDailyRow, run_id, (PositionDailyRow.trade_date, PositionDailyRow.code)
            ),
            "signals": _read_frame(
                session, DailySignalRow, run_id, (DailySignalRow.signal_date, DailySignalRow.code)
            ),
            "orders": _read_frame(
                session, OrderRow, run_id, (OrderRow.order_date, OrderRow.order_id)
            ),
            "fills": _read_frame(session, FillRow, run_id, (FillRow.trade_date, FillRow.order_id)),
            "trades": _read_frame(
                session, TradeRecordRow, run_id, (TradeRecordRow.entry_date, TradeRecordRow.trade_id)
            ),
        }
    return StoredRun(
        run_id=run_id,
        metadata=metadata,
        performance=performance,
        trade_metrics=trade_metrics,
        frames=frames,
    )


def list_runs(
    limit: int = 50,
    strategy: Optional[str] = None,
    url: Optional[str] = None,
    schema: Optional[str] = None,
) -> list[dict]:
    """列出最近的运行（按创建时间倒序），供试验账本与人工复核。"""
    engine = make_results_engine(url, schema)
    ensure_results_schema(engine)
    statement = select(BacktestRunRow).order_by(BacktestRunRow.created_at.desc()).limit(limit)
    if strategy:
        statement = statement.where(BacktestRunRow.strategy == strategy)
    with Session(engine) as session:
        rows = session.execute(statement).scalars().all()
        return [
            {
                "run_id": row.run_id,
                "strategy": row.strategy,
                "mode": row.mode,
                "cost_tier": row.cost_tier,
                "period": [str(row.start_date), str(row.end_date)],
                "codes": row.codes,
                "n_trades": row.n_trades,
                "total_return": row.total_return,
                "sharpe": row.sharpe,
                "max_drawdown": row.max_drawdown,
                "git_commit": (row.git_commit or "")[:8],
                "data_hash": (row.data_hash or "")[:16],
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]


def delete_run(run_id: str, url: Optional[str] = None, schema: Optional[str] = None) -> bool:
    """删除一次运行的全部行；返回是否确实删到了运行记录。"""
    engine = make_results_engine(url, schema)
    ensure_results_schema(engine)
    with Session(engine) as session:
        exists = session.get(BacktestRunRow, run_id) is not None
        for model in (PositionDailyRow, PortfolioDailyRow, FillRow, OrderRow,
                      DailySignalRow, TradeRecordRow, BacktestRunRow):
            session.execute(delete(model).where(model.run_id == run_id))
        session.commit()
    return exists


def table_counts(url: Optional[str] = None, schema: Optional[str] = None) -> dict:
    """各表行数（自检/报告用）。"""
    engine = make_results_engine(url, schema)
    ensure_results_schema(engine)
    counts: dict = {}
    with Session(engine) as session:
        for name, model in (
            ("runs", BacktestRunRow),
            ("pool_daily", PoolDailyRow),
            ("signals", DailySignalRow),
            ("orders", OrderRow),
            ("fills", FillRow),
            ("positions_daily", PositionDailyRow),
            ("portfolio_daily", PortfolioDailyRow),
            ("trade_records", TradeRecordRow),
        ):
            counts[name] = int(session.execute(select(func.count()).select_from(model)).scalar_one())
    return counts


__all__ = [
    "BacktestRunRow",
    "DailySignalRow",
    "FillRow",
    "OrderRow",
    "PoolDailyRow",
    "PortfolioDailyRow",
    "PositionDailyRow",
    "ResultsBase",
    "RunPayload",
    "StoredRun",
    "TradeRecordRow",
    "delete_run",
    "ensure_results_schema",
    "list_runs",
    "load_run",
    "make_results_engine",
    "resolve_results_schema",
    "resolve_results_url",
    "save_pool_daily",
    "save_run",
    "table_counts",
]
