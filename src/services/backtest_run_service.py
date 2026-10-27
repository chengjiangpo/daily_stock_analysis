# -*- coding: utf-8 -*-
"""一次完整回测运行的编排与报告落盘（方案 §13）。"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import yaml

from src.data.bars import snapshot_hash
from src.sim.engine import BacktestEngine, BacktestResult
from src.sim.fees import FeeModel
from src.sim.portfolio import PortfolioConfig
from src.sim.rules import PROJECT_ROOT
from src.strategy import build_strategy
from src.strategy.filters import PoolConfig
from src.validation.stats import performance_metrics, trade_metrics

DEFAULT_BACKTEST_PATH = PROJECT_ROOT / "config" / "backtest.yaml"


@dataclass
class RunOutputs:
    run_dir: Path
    metadata: dict[str, Any]


def load_portfolio_config(path: str | Path | None = None, aum: Optional[float] = None) -> PortfolioConfig:
    config_path = Path(path) if path else DEFAULT_BACKTEST_PATH
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    env_aum = os.getenv("BACKTEST_AUM")
    return PortfolioConfig(
        aum=Decimal(str(aum if aum is not None else env_aum or raw.get("aum", 300000))),
        max_positions=int(os.getenv("BACKTEST_MAX_POSITIONS", raw.get("max_positions", 5))),
        max_position_pct=float(os.getenv("BACKTEST_MAX_POSITION_PCT", raw.get("max_position_pct", 0.25))),
        max_gross_exposure=float(raw.get("max_gross_exposure", 0.95)),
        risk_per_trade=float(os.getenv("BACKTEST_RISK_PER_TRADE", raw.get("risk_per_trade", 0.01))),
        max_new_positions_per_day=int(raw.get("max_new_positions_per_day", 2)),
        max_per_industry=int(raw.get("max_per_industry", 2)),
        lot_size=int(raw.get("lot_size", 100)),
        drawdown_halve_at=float(raw.get("drawdown_halve_at", 0.12)),
        drawdown_stop_at=float(raw.get("drawdown_stop_at", 0.18)),
        loss_streak_threshold=int(raw.get("loss_streak_threshold", 5)),
        loss_streak_risk=float(raw.get("loss_streak_risk", 0.005)),
        drawdown_cooldown_days=int(raw.get("drawdown_cooldown_days", 20)),
    )


def load_pool_config(path: str | Path | None = None) -> PoolConfig:
    config_path = Path(path) if path else DEFAULT_BACKTEST_PATH
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return PoolConfig(
        min_listed_days=int(raw.get("min_listed_days", 120)),
        min_avg_amount_20d=float(raw.get("min_avg_amount_20d", 50_000_000)),
        min_price=float(raw.get("min_price", 2.0)),
    )


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def run_backtest(
    strategy_name: str,
    frames: Dict[str, pd.DataFrame],
    start: date,
    end: date,
    cost_tier: str = "base",
    aum: Optional[float] = None,
    out_dir: str | Path = "reports/backtest/latest",
    record_diagnostics: bool = True,
) -> RunOutputs:
    strategy = build_strategy(strategy_name)
    portfolio_config = load_portfolio_config(aum=aum)
    pool_config = load_pool_config()
    engine = BacktestEngine(
        strategy,
        frames,
        FeeModel.load(cost_tier=cost_tier),
        portfolio_config,
        pool_config,
        participation_cap=float(os.getenv("BACKTEST_PARTICIPATION_CAP", 0.02)),
        start=start,
        end=end,
        record_diagnostics=record_diagnostics,
    )
    result = engine.run()
    return persist_result(
        result,
        out_dir=out_dir,
        strategy=strategy_name,
        cost_tier=cost_tier,
        start=start,
        end=end,
        data_hash=snapshot_hash(frames),
        extra={"codes": len(engine.frames), "params": strategy.params},
    )


def persist_result(
    result: BacktestResult,
    out_dir: str | Path,
    strategy: str,
    cost_tier: str,
    start: date,
    end: date,
    data_hash: str,
    extra: Optional[dict] = None,
) -> RunOutputs:
    run_dir = Path(out_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    result.equity.to_csv(run_dir / "daily_equity.csv", index=False)
    result.orders.to_csv(run_dir / "orders.csv", index=False)
    result.fills.to_csv(run_dir / "fills.csv", index=False)
    result.trades.to_csv(run_dir / "trades.csv", index=False)
    result.signals.to_csv(run_dir / "signals.csv", index=False)
    result.diagnostics.to_csv(run_dir / "diagnostics.csv", index=False)
    result.positions.to_csv(run_dir / "daily_positions.csv", index=False)

    equity_series = pd.Series(
        result.equity["equity"].to_numpy(dtype=float),
        index=pd.to_datetime(result.equity["trade_date"]),
    )
    performance = performance_metrics(equity_series)
    trade_stats = trade_metrics(result.trades["pnl"].tolist()) if not result.trades.empty else trade_metrics([])

    monthly = _period_returns(equity_series, "M")
    yearly = _period_returns(equity_series, "Y")
    monthly.to_csv(run_dir / "monthly_returns.csv", header=["return"])
    yearly.to_csv(run_dir / "yearly_returns.csv", header=["return"])

    metadata = {
        "run_id": pd.Timestamp.now().strftime("bt-%Y%m%d-%H%M%S"),
        "strategy": strategy,
        "cost_tier": cost_tier,
        "period": [str(start), str(end)],
        "data_hash": data_hash,
        "git_commit": git_commit(),
        "created_at": pd.Timestamp.now().isoformat(),
        "engine": result.metadata,
    }
    if extra:
        metadata.update(extra)
    (run_dir / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                                               encoding="utf-8")
    (run_dir / "performance_metrics.json").write_text(
        json.dumps({"performance": performance, "trades": trade_stats}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "summary.md").write_text(
        _summary_markdown(metadata, performance, trade_stats), encoding="utf-8"
    )
    return RunOutputs(run_dir=run_dir, metadata=metadata)


def _period_returns(equity: pd.Series, freq: str) -> pd.Series:
    if equity.empty:
        return pd.Series(dtype=float)
    if freq == "Y":
        grouped = equity.groupby(equity.index.year)
    else:
        grouped = equity.groupby([equity.index.year, equity.index.month])
    returns = grouped.last() / grouped.first() - 1.0
    return returns.dropna()


def _summary_markdown(metadata: dict, performance: dict, trade_stats: dict) -> str:
    return "\n".join(
        [
            "# 回测报告",
            "",
            f"- 策略：{metadata['strategy']}（成本档 {metadata['cost_tier']}）",
            f"- 区间：{metadata['period'][0]} ~ {metadata['period'][1]}",
            f"- 数据哈希：`{metadata['data_hash'][:16]}`；commit：`{metadata['git_commit'][:8]}`",
            "",
            "| 指标 | 值 |",
            "|---|---|",
            f"| 总收益 | {performance['total_return']:.2%} |",
            f"| 年化收益 | {performance['cagr']:.2%} |",
            f"| 年化波动 | {performance['annual_vol']:.2%} |",
            f"| 夏普 | {performance['sharpe']:.2f} |",
            f"| 最大回撤 | {performance['max_drawdown']:.2%} |",
            f"| 交易笔数 | {trade_stats['n_trades']} |",
            f"| 胜率 | {trade_stats['win_rate']:.1%} |",
            f"| Profit Factor | {trade_stats['profit_factor']:.2f} |",
            "",
            "> 本报告不构成策略有效性结论；有效性须按 §8.4 在拼接后的样本外区间判定。",
        ]
    ) + "\n"
