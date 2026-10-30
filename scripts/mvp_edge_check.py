#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 0 MVP：主策略毛超额与 go/no-go 判定（方案 §10）。

用法：
  python scripts/mvp_edge_check.py --start 2016-01-01 --end 2024-12-31 \
      --cache-dir data/cache/swing_bars --out reports/mvp

数据来源：
  1) --data-dir 读取本地 CSV/CSV.GZ 行情（离线、可复现）；
  2) 否则用 BaoStock 历史时点并集发现股票池并抓取（含后来退市股）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.data.bars import quality_report, snapshot_hash, write_quality_report  # noqa: E402
from src.data.fixtures import build_fixture_frames  # noqa: E402
from src.data.loader import load_frames  # noqa: E402
from src.data.market_caps import (  # noqa: E402
    MatchedControlConfig,
    attach_market_caps,
    load_caps_meta,
    load_market_caps,
)
from src.data.pool import build_pool_panel  # noqa: E402
from src.sim.engine import BacktestEngine  # noqa: E402
from src.sim.fees import FeeModel  # noqa: E402
from src.sim.portfolio import PortfolioConfig  # noqa: E402
from src.strategy import build_strategy  # noqa: E402
from src.strategy.benchmarks import (  # noqa: E402
    MatchedControlBench,
    add_buckets,
    basket_forward_return,
    sample_baskets,
)
from src.validation.stats import (  # noqa: E402
    block_bootstrap_ci,
    performance_metrics,
    trade_metrics,
)

logger = logging.getLogger("mvp_edge_check")

MIN_TRADES = 300
MIN_GROSS_EXCESS = 0.006
MIN_CI_LOW = 0.003


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A 股波段策略 MVP 可行性切片")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--strategy", default="trend_pullback_restart")
    parser.add_argument("--out", default="reports/mvp")
    parser.add_argument("--data-dir", default=None, help="本地行情目录（CSV/CSV.GZ）")
    parser.add_argument("--cache-dir", default="data/cache/swing_bars")
    parser.add_argument(
        "--bars-url",
        default=None,
        help="从数据库读行情（V3.0 I-4，如 sqlite:///data/swing_bars.db 或 postgresql://...）；"
        "默认读 CSV 缓存。可用环境变量 SWING_BARS_DATABASE_URL",
    )
    parser.add_argument(
        "--bars-schema",
        default=None,
        help="数据库 schema（PostgreSQL 推荐独立 schema，如 swing_bars）；可用环境变量 SWING_BARS_SCHEMA",
    )
    parser.add_argument(
        "--adj-source",
        default="baostock",
        choices=("baostock", "tushare"),
        help="复权因子来源（仅从库读时生效）：baostock=原始 hfq；tushare=重建后的 adj_factor_aligned",
    )
    parser.add_argument("--codes-file", default=None, help="每行一个 6 位代码")
    parser.add_argument("--limit-codes", type=int, default=0)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--cost-tier", default="base", choices=("base", "higher", "stress"))
    parser.add_argument("--aum", type=float, default=300000.0)
    parser.add_argument("--bench-sims", type=int, default=200)
    parser.add_argument(
        "--b2",
        default="auto",
        choices=("auto", "matched", "legacy"),
        help="B2 口径：matched=同日同市值分档全体均值+留一（V3.0 §4.4，需市值缓存）；"
        "legacy=随机 5 只×N 次中位数（MVP 冻结口径）；auto=有市值缓存用 matched，否则 legacy",
    )
    parser.add_argument("--caps-dir", default="data/cache/market_caps", help="市值缓存目录")
    parser.add_argument(
        "--size-edges",
        default=None,
        help="流通市值分档边界（亿元），逗号分隔，如 20,30,45,70；默认读 config/backtest.yaml",
    )
    parser.add_argument("--offline-fixtures", action="store_true", help="使用合成行情跑通流程")
    parser.add_argument("--portfolio-constrained", action="store_true",
                        help="启用 5 仓/2 笔/日/回撤熔断组合约束（默认信号级评估）")
    return parser.parse_args()


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def attach_benchmark(
    trades: pd.DataFrame,
    frames: Dict[str, pd.DataFrame],
    signal_frames: Dict[str, pd.DataFrame],
    sims: int,
    b2_mode: str = "legacy",
    matched_bench: Optional[MatchedControlBench] = None,
    matched_panel: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    if trades.empty:
        return trades
    panel = build_pool_panel(signal_frames)
    if panel.empty:
        return trades
    panel["date"] = pd.to_datetime(panel["date"])
    panel = panel.drop_duplicates(["date", "code"], keep="last")

    if b2_mode == "matched":
        return _attach_matched_benchmark(trades, frames, matched_bench, matched_panel)

    snapshot_cache: dict[pd.Timestamp, pd.DataFrame] = {}

    def snapshot_for(timestamp: pd.Timestamp) -> pd.DataFrame:
        cached = snapshot_cache.get(timestamp)
        if cached is not None:
            return cached
        subset = panel[panel["date"] == timestamp]
        if subset.empty:
            snapshot_cache[timestamp] = subset
            return subset
        snapshot = subset.set_index("code")[["size", "volatility", "industry"]]
        snapshot = add_buckets(snapshot, size_col="size", vol_col="volatility")
        snapshot_cache[timestamp] = snapshot
        return snapshot

    rows = []
    for _, trade in trades.iterrows():
        record = trade.to_dict()
        signal_date = pd.Timestamp(trade["signal_date"])
        snapshot = snapshot_for(signal_date)
        bench = {"bench_median": None, "bench_sims": 0}
        if not snapshot.empty:
            target = snapshot.loc[trade["code"]] if trade["code"] in snapshot.index else None
            size_bucket = int(target["size_bucket"]) if target is not None else 0
            vol_bucket = int(target["vol_bucket"]) if target is not None else 0
            industry = str(trade.get("industry") or "") or None
            seed = int(hashlib.sha256(f"{trade['code']}-{trade['entry_date']}".encode()).hexdigest()[:8], 16)
            baskets = sample_baskets(
                snapshot,
                size_bucket=size_bucket,
                vol_bucket=vol_bucket,
                industry=industry,
                exclude=[trade["code"]],
                n=5,
                sims=sims,
                seed=seed,
            )
            bench = basket_forward_return(frames, baskets, trade["entry_date"], trade["exit_date"])
        gross_return = float(trade["exit_price"]) / float(trade["entry_price"]) - 1.0
        bench_median = bench.get("median")
        record["gross_return"] = gross_return
        record["bench_return"] = bench_median
        record["bench_market_return"] = None
        record["bench_n"] = bench.get("sims", 0)
        record["size_source"] = "proxy_quantile"
        record["excess_return"] = gross_return - bench_median if bench_median is not None else None
        record["excess_market_return"] = None
        record["bench_sims"] = bench.get("sims", 0)
        record["net_return"] = float(trade["pnl"]) / (
            float(trade["entry_price"]) * float(trade["quantity"])
        )
        rows.append(record)
    return pd.DataFrame(rows)


def _attach_matched_benchmark(
    trades: pd.DataFrame,
    frames: Dict[str, pd.DataFrame],
    matched_bench: Optional[MatchedControlBench],
    matched_panel: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """B2 修正口径：同日同市值分档全体可买股票均值（留一）+ 市场口径对照。"""
    if matched_bench is None or matched_panel is None:
        raise ValueError("matched 口径需要 MatchedControlBench 与分档面板")
    lookup = matched_panel.set_index(["date", "code"])
    rows = []
    for _, trade in trades.iterrows():
        record = trade.to_dict()
        signal_date = pd.Timestamp(trade["signal_date"])
        key = (signal_date, trade["code"])
        size_bucket = -1
        if key in lookup.index:
            row = lookup.loc[key]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[-1]
            size_bucket = int(row["size_bucket"])
        result = matched_bench.benchmark(
            trade["code"],
            signal_date.date(),
            pd.Timestamp(trade["entry_date"]).date(),
            pd.Timestamp(trade["exit_date"]).date(),
            size_bucket,
        )
        gross_return = float(trade["exit_price"]) / float(trade["entry_price"]) - 1.0
        control = result["control_mean"]
        market = result["market_mean"]
        record["gross_return"] = gross_return
        record["bench_return"] = control
        record["bench_market_return"] = market
        record["bench_n"] = result["control_n"]
        record["bench_market_n"] = result["market_n"]
        record["size_source"] = result["size_source"]
        record["excess_return"] = gross_return - control if control is not None else None
        record["excess_market_return"] = gross_return - market if market is not None else None
        record["bench_sims"] = 0
        record["net_return"] = float(trade["pnl"]) / (
            float(trade["entry_price"]) * float(trade["quantity"])
        )
        rows.append(record)
    return pd.DataFrame(rows)


def verdict(summary: dict) -> tuple[str, str]:
    checks = {
        "trades_ge_300": summary["n_trades"] >= MIN_TRADES,
        "mean_excess_ge_0.6pct": summary["mean_excess"] is not None and summary["mean_excess"] >= MIN_GROSS_EXCESS,
        "ci_low_gt_0.3pct": summary["ci_low"] is not None and summary["ci_low"] > MIN_CI_LOW,
        "net_expectancy_positive": summary["net_expectancy"] is not None and summary["net_expectancy"] > 0,
    }
    passed = all(checks.values())
    if passed:
        return "PASS", "冻结 v1，进入阶段 1"
    mean_ok = checks["mean_excess_ge_0.6pct"]
    ci_ok = checks["ci_low_gt_0.3pct"]
    if mean_ok and not ci_ok:
        return "RETEST", "均值达标但 CI 下界不达标：扩大样本区间后重测一次"
    return "STOP", "停止完整系统开发，保留代码作为研究工具"


def write_summary(
    path: Path,
    args: argparse.Namespace,
    result_metadata: dict,
    performance: dict,
    trade_stats: dict,
    summary: dict,
    checks: dict,
    decision: str,
    decision_reason: str,
    code_count: int,
    rejections: dict,
    trades_by_year: dict,
    b2_mode: str = "legacy",
    b2_coverage: Optional[dict] = None,
) -> None:
    b2_label = (
        "修正口径：同日同流通市值分档全体可买股票均值 + 留一剔除（V3.0 §4.4）"
        if b2_mode == "matched"
        else "legacy 口径：同分位桶随机 5 只 × N 次取中位数（MVP 冻结口径）"
    )
    bench_label = (
        "B2 匹配对照每笔收益均值（留一）"
        if b2_mode == "matched"
        else "B2 基准每笔收益中位数均值"
    )
    lines = [
        "# MVP 可行性切片报告",
        "",
        f"- 区间：{args.start} ~ {args.end}",
        f"- 策略：{args.strategy}（成本档 {args.cost_tier}，"
        f"{'AUM ' + format(args.aum, ',.0f') if args.portfolio_constrained else '信号级固定 6 万/笔'}）",
        f"- 股票数：{code_count}；交易日：{result_metadata.get('days')}",
        f"- 评估模式：{'信号级（固定 6 万名义本金，无组合约束）' if not args.portfolio_constrained else '组合约束'}",
        f"- B2 口径：{b2_label}",
        f"- 结论：**{decision}** — {decision_reason}",
        "",
        "## 1. 成本倒推（方案 §6.3）",
        "",
        "| 目标净年化 | 5 日(50 轮) | 7 日(36 轮) | 10 日(25 轮) |",
        "|---|---|---|---|",
        "| 净 10% | 毛 0.51%/笔 | 0.59% | 0.71% |",
        "| 净 20% | 毛 0.71%/笔 | 0.87% | 1.11% |",
        "",
        "主策略按 8 日时间退出估计，需毛超额 ≥0.6%/笔。",
        "",
        "## 2. 相对 B2 匹配基准的 alpha",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 交易笔数 | {summary['n_trades']} |",
        f"| 每笔毛收益均值 | {_pct(summary['mean_gross'])} |",
        f"| {bench_label} | {_pct(summary['mean_bench'])} |",
    ]
    if b2_mode == "matched":
        lines.append(
            f"| B2 市场口径对照均值（同日全池，留一） | {_pct(summary.get('mean_market_bench'))} |"
        )
    lines += [
        f"| 每笔毛超额均值 | {_pct(summary['mean_excess'])} |",
        f"| 超额 t 值 | {summary['t_stat']:.2f} |",
        f"| bootstrap 95% CI | [{_pct(summary['ci_low'])}, {_pct(summary['ci_high'])}] |",
        f"| 扣成本后每笔净期望 | {_pct(summary['net_expectancy'])} |",
        f"| 胜率 / PF | {trade_stats['win_rate']:.1%} / {trade_stats['profit_factor']:.2f} |",
        "",
    ]
    if b2_mode == "matched" and b2_coverage:
        lines += [
            f"> 市值覆盖：行覆盖率 {b2_coverage.get('row_coverage', 0):.1%}，"
            f"全量分档 {b2_coverage.get('dates_full_coverage', 0)}/{b2_coverage.get('dates_total', 0)} 日，"
            f"回退代理分档 {b2_coverage.get('dates_fallback_proxy', 0)} 日"
            f"（市值源区间 {b2_coverage.get('first_caps_date')} ~ {b2_coverage.get('last_caps_date')}）",
            "",
        ]
    lines += [
        "## 3. go/no-go 判据",
        "",
        "| 判据 | 通过 |",
        "|---|---|",
        f"| 笔数 ≥300 | {'是' if checks['trades_ge_300'] else '否'} |",
        f"| 每笔毛超额 ≥0.6% | {'是' if checks['mean_excess_ge_0.6pct'] else '否'} |",
        f"| CI 下界 >0.3% | {'是' if checks['ci_low_gt_0.3pct'] else '否'} |",
        f"| 扣基础成本净期望 >0 | {'是' if checks['net_expectancy_positive'] else '否'} |",
        "",
        "## 4. 执行诊断",
        "",
        f"- 拒单原因：{json.dumps(rejections, ensure_ascii=False)}",
        f"- 分年度交易数：{json.dumps(trades_by_year, ensure_ascii=False)}",
        "",
        "## 5. 组合表现",
        "",
        "> 信号级模式下净值按固定 6 万名义本金逐笔加总，仅用于账务校验，不代表可投资组合收益。",
        "",
        f"- 总收益：{_pct(performance['total_return'])}",
        f"- 年化收益：{_pct(performance['cagr'])}",
        f"- 年化波动：{_pct(performance['annual_vol'])}",
        f"- 夏普：{performance['sharpe']:.2f}",
        f"- 最大回撤：{_pct(performance['max_drawdown'])}",
        "",
        "## 6. 限制与未覆盖项",
        "",
    ]
    if b2_mode == "legacy":
        lines.append(
            "- B2 的 size 代理为 20 日均额（缺少全市场市值数据），行业字段若缺失则退化为全池抽样。"
        )
    else:
        lines.append(
            "- B2 分档市值来自 stock_eod_daily 导出缓存；覆盖率不足的交易日回退 20 日均额代理分位桶（见上方覆盖说明）。"
        )
    lines += [
        "- 未建模送转/现金分红（阶段 3）；BaoStock 后复权已含分红，但持仓期现金分红未入账，组合收益偏保守。",
        "- 未使用官方 stk_limit，涨跌停价按分段规则自算（市值缓存含官方涨跌停价，可用 scripts/fetch_market_caps.py --cross-check 对账）。",
        "- 退市股是否可得取决于数据源实测（见 docs/plans/a-share-swing-data-probe.md）。",
        "- 数据质量详见 data_quality_report.json；当前未自动剔除隔离候选（MVP 阶段）。",
        "",
        "## 7. 文件",
        "",
        "- `trades.csv`：逐笔成交与基准对照",
        "- `daily_equity.csv`：逐日净值",
        "- `performance_metrics.json`",
        "- `data_quality_report.json`",
        "- `run_metadata.json`",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.2f}%"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.offline_fixtures:
        frames = build_fixture_frames()
    else:
        frames = load_frames(
            args.start,
            args.end,
            data_dir=args.data_dir,
            cache_dir=args.cache_dir,
            codes_file=args.codes_file,
            limit_codes=args.limit_codes,
            refresh=args.refresh,
            bars_url=args.bars_url,
            bars_schema=args.bars_schema,
            adj_source=args.adj_source,
        )
    if not frames:
        logger.error("没有可用行情数据")
        return 2
    logger.info("载入 %d 只股票", len(frames))

    strategy = build_strategy(args.strategy)
    fee_model = FeeModel.load(cost_tier=args.cost_tier)
    signal_level = not args.portfolio_constrained
    portfolio_aum = Decimal(str(args.aum)) if not signal_level else Decimal("100000000")
    portfolio_config = PortfolioConfig(aum=portfolio_aum)
    engine = BacktestEngine(
        strategy,
        frames,
        fee_model,
        portfolio_config,
        participation_cap=0.02 if not signal_level else 1.0,
        start=pd.Timestamp(args.start).date(),
        end=pd.Timestamp(args.end).date(),
        constrained=not signal_level,
        fixed_notional=Decimal("60000"),
    )
    result = engine.run()
    logger.info("回测完成: %s", result.metadata)

    quality = [quality_report(frame, code) for code, frame in engine.frames.items()]
    write_quality_report(quality, out_dir / "data_quality_report.json")

    trades = result.trades
    b2_mode = args.b2
    matched_bench = None
    matched_panel = None
    caps_meta: dict = {}
    coverage_dict: Optional[dict] = None
    if b2_mode == "auto":
        try:
            load_market_caps(args.caps_dir)
            b2_mode = "matched"
        except (FileNotFoundError, ValueError):
            logger.warning("市值缓存不可用（%s），B2 回退 legacy 抽样口径", args.caps_dir)
            b2_mode = "legacy"
    if b2_mode == "matched":
        caps = load_market_caps(args.caps_dir)
        caps_meta = load_caps_meta(args.caps_dir)
        control_config = MatchedControlConfig.load()
        if args.size_edges:
            control_config = MatchedControlConfig(
                size_edges_yi=tuple(float(v) for v in args.size_edges.split(",")),
                min_coverage=control_config.min_coverage,
                entry_gap_max=control_config.entry_gap_max,
            )
        panel = build_pool_panel(engine.frames)
        matched_panel, coverage = attach_market_caps(panel, caps, control_config)
        coverage_dict = coverage.to_dict()
        logger.info(
            "B2 修正口径：分档边界 %s 亿；市值行覆盖率 %.1f%%（%d/%d 日全量，%d 日回退代理）",
            list(control_config.size_edges_yi),
            coverage.row_coverage * 100,
            coverage.dates_full_coverage,
            coverage.dates_total,
            coverage.dates_fallback_proxy,
        )
        matched_bench = MatchedControlBench(frames, matched_panel, control_config)
    trades = attach_benchmark(
        trades,
        frames,
        engine.frames,
        sims=args.bench_sims,
        b2_mode=b2_mode,
        matched_bench=matched_bench,
        matched_panel=matched_panel,
    )
    equity_series = pd.Series(
        result.equity["equity"].to_numpy(),
        index=pd.to_datetime(result.equity["trade_date"]),
    )
    performance = performance_metrics(equity_series)
    trade_stats = trade_metrics(trades["pnl"].tolist()) if not trades.empty else trade_metrics([])

    excess = trades["excess_return"].dropna() if not trades.empty else pd.Series(dtype=float)
    mean_excess = float(excess.mean()) if len(excess) else None
    t_stat = 0.0
    if len(excess) > 1 and float(excess.std(ddof=1)) > 0:
        t_stat = float(excess.mean() / (excess.std(ddof=1) / np.sqrt(len(excess))))
    bootstrap = (
        block_bootstrap_ci(excess, lambda series: float(series.mean()), n_boot=2000, block=5, seed=42)
        if len(excess)
        else {"ci_low": None, "ci_high": None}
    )
    summary = {
        "n_trades": int(len(trades)),
        "n_benchmarked": int(len(excess)),
        "mean_gross": float(trades["gross_return"].mean()) if not trades.empty else None,
        "mean_bench": float(trades["bench_return"].dropna().mean()) if not trades.empty else None,
        "mean_market_bench": (
            float(trades["bench_market_return"].dropna().mean())
            if not trades.empty and "bench_market_return" in trades.columns
            else None
        ),
        "mean_excess": mean_excess,
        "t_stat": t_stat,
        "ci_low": bootstrap.get("ci_low"),
        "ci_high": bootstrap.get("ci_high"),
        "net_expectancy": float(trades["net_return"].mean()) if not trades.empty else None,
    }
    checks = {
        "trades_ge_300": summary["n_trades"] >= MIN_TRADES,
        "mean_excess_ge_0.6pct": mean_excess is not None and mean_excess >= MIN_GROSS_EXCESS,
        "ci_low_gt_0.3pct": bootstrap.get("ci_low") is not None and bootstrap["ci_low"] > MIN_CI_LOW,
        "net_expectancy_positive": summary["net_expectancy"] is not None and summary["net_expectancy"] > 0,
    }
    decision, reason = verdict(summary)

    run_metadata = {
        "run_id": pd.Timestamp.now().strftime("mvp-%Y%m%d-%H%M%S"),
        "git_commit": git_commit(),
        "strategy": args.strategy,
        "rule_version": strategy.version,
        "params": strategy.params,
        "cost_tier": args.cost_tier,
        "aum": args.aum,
        "period": [args.start, args.end],
        "data_hash": snapshot_hash(frames),
        "codes": len(engine.frames),
        "engine": result.metadata,
        "bench_sims": args.bench_sims,
        "b2": {
            "caliber": b2_mode,
            "size_edges_yi": list(MatchedControlConfig.load().size_edges_yi),
            "caps_source": caps_meta.get("source"),
            "caps_range": caps_meta.get("range"),
            "coverage": coverage_dict,
        },
        "created_at": pd.Timestamp.now().isoformat(),
    }

    result.equity.to_csv(out_dir / "daily_equity.csv", index=False)
    result.orders.to_csv(out_dir / "orders.csv", index=False)
    result.fills.to_csv(out_dir / "fills.csv", index=False)
    result.signals.to_csv(out_dir / "signals.csv", index=False)
    result.diagnostics.to_csv(out_dir / "diagnostics.csv", index=False)
    if not trades.empty:
        trades.to_csv(out_dir / "trades.csv", index=False)
    (out_dir / "performance_metrics.json").write_text(
        json.dumps({"performance": performance, "trades": trade_stats, "summary": summary, "checks": checks,
                    "decision": decision, "decision_reason": reason}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "run_metadata.json").write_text(
        json.dumps(run_metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rejected = result.orders[result.orders["status"] == "rejected"] if not result.orders.empty else pd.DataFrame()
    rejections = rejected["reject_reason"].value_counts().head(8).to_dict() if not rejected.empty else {}
    trades_by_year = (
        pd.to_datetime(trades["entry_date"]).dt.year.value_counts().sort_index().to_dict()
        if not trades.empty else {}
    )
    write_summary(
        out_dir / "summary.md", args, result.metadata, performance, trade_stats, summary, checks,
        decision, reason, len(engine.frames), rejections, trades_by_year,
        b2_mode=b2_mode, b2_coverage=coverage_dict,
    )
    logger.info("结论: %s — %s", decision, reason)
    logger.info("报告目录: %s", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
