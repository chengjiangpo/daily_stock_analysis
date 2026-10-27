#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组合级回测 CLI（方案附录 C）。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.data.loader import load_frames  # noqa: E402
from src.services.backtest_run_service import run_backtest  # noqa: E402
from src.validation.stats import performance_metrics  # noqa: E402
from src.validation.walk_forward import assert_non_overlapping, generate_windows  # noqa: E402

logger = logging.getLogger("run_backtest")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A 股组合级回测")
    parser.add_argument("--strategy", default="trend_pullback_restart")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default="2022-12-31")
    parser.add_argument("--cost-tier", default="base", choices=("base", "higher", "stress"))
    parser.add_argument("--aum", type=float, default=None)
    parser.add_argument("--out", default="reports/backtest/latest")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--cache-dir", default="data/cache/swing_bars")
    parser.add_argument("--codes-file", default=None)
    parser.add_argument("--limit-codes", type=int, default=0)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--dev-years", type=int, default=3)
    parser.add_argument("--val-years", type=int, default=1)
    parser.add_argument("--test-months", type=int, default=6)
    parser.add_argument("--step-months", type=int, default=6)
    parser.add_argument("--no-diagnostics", action="store_true")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    start, end = pd.Timestamp(args.start).date(), pd.Timestamp(args.end).date()
    if start >= end:
        logger.error("start 必须早于 end")
        return 2

    frames = load_frames(
        args.start,
        args.end,
        data_dir=args.data_dir,
        cache_dir=args.cache_dir,
        codes_file=args.codes_file,
        limit_codes=args.limit_codes,
        refresh=args.refresh,
    )
    if not frames:
        logger.error("没有可用行情")
        return 2
    logger.info("载入 %d 只股票", len(frames))

    if not args.walk_forward:
        outputs = run_backtest(
            args.strategy,
            frames,
            start,
            end,
            cost_tier=args.cost_tier,
            aum=args.aum,
            out_dir=args.out,
            record_diagnostics=not args.no_diagnostics,
        )
        logger.info("回测完成: %s", outputs.metadata["engine"])
        logger.info("报告目录: %s", outputs.run_dir)
        return 0

    windows = generate_windows(
        start,
        end,
        dev_years=args.dev_years,
        val_years=args.val_years,
        test_months=args.test_months,
        step_months=args.step_months,
    )
    assert_non_overlapping(windows)
    if not windows:
        logger.error("区间不足以生成任何滚动窗口")
        return 2

    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    records = []
    for index, window in enumerate(windows, start=1):
        logger.info("窗口 %d/%d: test %s ~ %s", index, len(windows), window.test_start, window.test_end)
        outputs = run_backtest(
            args.strategy,
            frames,
            window.test_start,
            window.test_end,
            cost_tier=args.cost_tier,
            aum=args.aum,
            out_dir=root / "windows" / f"{index:02d}",
            record_diagnostics=not args.no_diagnostics,
        )
        equity = pd.read_csv(outputs.run_dir / "daily_equity.csv", parse_dates=["trade_date"])
        metrics = performance_metrics(pd.Series(equity["equity"].to_numpy(), index=equity["trade_date"]))
        record = window.to_dict()
        record.update(
            {
                "test_return": metrics["total_return"],
                "test_sharpe": metrics["sharpe"],
                "test_max_drawdown": metrics["max_drawdown"],
                "trades": outputs.metadata["engine"].get("trades", 0),
            }
        )
        records.append(record)

    pd.DataFrame(records).to_csv(root / "walk_forward_results.csv", index=False)
    (root / "walk_forward_metadata.json").write_text(
        json.dumps({"strategy": args.strategy, "cost_tier": args.cost_tier, "windows": len(records)},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("滚动前推完成：%d 个窗口，结果 %s", len(records), root / "walk_forward_results.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
