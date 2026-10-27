# -*- coding: utf-8 -*-
"""波段回测 API schemas."""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class SwingRunRequest(BaseModel):
    strategy: str = Field("trend_pullback_restart", description="策略名")
    start: str = Field("2016-01-01", description="开始日期 YYYY-MM-DD")
    end: str = Field("2024-12-31", description="结束日期 YYYY-MM-DD")
    cost_tier: str = Field("base", description="成本档 base/higher/stress")
    mode: str = Field("signal", description="signal（信号级）或 portfolio（组合约束）")
    max_codes: int = Field(300, ge=1, le=800, description="最多使用的缓存股票数")
    aum: Optional[float] = Field(None, gt=0, description="组合模式下的 AUM；信号级忽略")


class SwingRunSummary(BaseModel):
    run_id: str
    title: str
    status: str
    strategy: Optional[str] = None
    mode: Optional[str] = None
    cost_tier: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    created_at: Optional[str] = None
    n_trades: int = 0
    total_return: Optional[float] = None
    cagr: Optional[float] = None
    sharpe: Optional[float] = None
    max_drawdown: Optional[float] = None
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None
    mean_excess: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    net_expectancy: Optional[float] = None
    decision: Optional[str] = None
    decision_reason: Optional[str] = None
    codes: int = 0
    files: List[str] = Field(default_factory=list)


class SwingRunsResponse(BaseModel):
    items: List[SwingRunSummary] = Field(default_factory=list)


class SwingFileItem(BaseModel):
    name: str
    size: int


class SwingEquityPoint(BaseModel):
    date: str
    equity: Optional[float] = None
    drawdown: Optional[float] = None
    cash: Optional[float] = None
    market_value: Optional[float] = None


class SwingTradeItem(BaseModel):
    code: str
    signal_date: Optional[str] = None
    entry_date: Optional[str] = None
    exit_date: Optional[str] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    gross_return: Optional[float] = None
    bench_return: Optional[float] = None
    excess_return: Optional[float] = None
    net_return: Optional[float] = None
    pnl: Optional[float] = None
    r_multiple: Optional[float] = None
    exit_reason: Optional[str] = None
    hold_days: int = 0


class SwingDiagnosticItem(BaseModel):
    condition_name: str
    total: int
    failed: int
    pass_rate: Optional[float] = None


class SwingRunDetail(SwingRunSummary):
    equity: List[SwingEquityPoint] = Field(default_factory=list)
    trades: List[SwingTradeItem] = Field(default_factory=list)
    rejection_reasons: Dict[str, int] = Field(default_factory=dict)
    diagnostics: List[SwingDiagnosticItem] = Field(default_factory=list)
    files: List[SwingFileItem] = Field(default_factory=list)
    summary_markdown: str = ""


class SwingRunStatus(BaseModel):
    run_id: str
    status: str
    message: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    finished_at: Optional[str] = None
