#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出 fund-analysis 库 stock_eod_daily 的市值/涨跌停价截面（V3.0 §4.5 / I-2）。

用法：
  # 导出（默认读 MARKET_CAPS_DATABASE_URL / DATABASE_URL 环境变量）
  python scripts/fetch_market_caps.py --start 2016-01-01 --end 2024-12-31 \
      --cache-dir data/cache/market_caps

  # 导出后抽样交叉校验（对照 BaoStock 行情缓存：adj_factor 互验 + 涨跌停价对账）
  python scripts/fetch_market_caps.py --start 2016-01-01 --end 2024-12-31 \
      --cache-dir data/cache/market_caps --cross-check --bars-cache data/cache/swing_bars_full

数据源：fund-analysis PostgreSQL public.stock_eod_daily
（TuShare daily + daily_basic + moneyflow + adj_factor + stk_limit 合成，含退市股；
circ_mv/total_mv 单位为元）。跨库迁移见 fund-analysis/docs/eod-pg-transfer.md。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.data.baostock_bars import _load_cached  # noqa: E402
from src.data.market_caps import CAPS_COLUMNS  # noqa: E402
from src.sim.matcher import compute_limit_prices  # noqa: E402
from src.sim.rules import MarketRules, classify_board  # noqa: E402

logger = logging.getLogger("fetch_market_caps")

ADJ_FACTOR_WARN_THRESHOLD = 0.005  # 0.5%（V3.0 §4.5 对账告警线）
LIMIT_PRICE_TOLERANCE = Decimal("0.01")  # 涨跌停价允许 1 分钱舍入差


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出市值/涨跌停价截面缓存")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--cache-dir", default="data/cache/market_caps")
    parser.add_argument("--codes-file", default=None, help="可选：仅导出清单内代码（每行一个 6 位代码）")
    parser.add_argument("--url", default=None, help="PostgreSQL 连接串（默认取环境变量）")
    parser.add_argument("--chunk-size", type=int, default=500_000)
    parser.add_argument("--cross-check", action="store_true", help="导出后抽样交叉校验（需 --bars-cache）")
    parser.add_argument("--bars-cache", default="data/cache/swing_bars_full", help="BaoStock 行情缓存目录")
    parser.add_argument("--sample", type=int, default=200, help="交叉校验抽样股票数（每只再抽 ≤5 日对账涨跌停价）")
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def database_url(args: argparse.Namespace) -> str:
    url = args.url or os.getenv("MARKET_CAPS_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit(
            "缺少数据库连接串：请用 --url 或设置 MARKET_CAPS_DATABASE_URL / DATABASE_URL"
            "（指向 fund-analysis 库，见 docs/swing-backtest.md）"
        )
    # SQLAlchemy 2.1 对裸 `postgresql://` 默认选 psycopg3，而本仓库依赖是 psycopg2；
    # 直接贴 fund-analysis 的 DATABASE_URL 会报 ModuleNotFoundError: psycopg。
    # 复用 bars_store 的驱动补全逻辑，两处口径保持一致。
    from src.data.bars_store import _normalize_url

    return _normalize_url(url)


def export_caps(args: argparse.Namespace) -> pd.DataFrame:
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url(args))
    query = text(
        "SELECT trade_date, code, circ_mv, total_mv, adj_factor, up_limit, down_limit, is_st "
        "FROM stock_eod_daily WHERE trade_date >= :start AND trade_date <= :end"
    )
    params: dict = {"start": args.start, "end": args.end}
    if args.codes_file:
        codes = [line.strip() for line in Path(args.codes_file).read_text(encoding="utf-8").splitlines() if line.strip()]
        params["codes"] = tuple(codes)
        query = text(
            "SELECT trade_date, code, circ_mv, total_mv, adj_factor, up_limit, down_limit, is_st "
            "FROM stock_eod_daily WHERE trade_date >= :start AND trade_date <= :end "
            "AND code IN :codes"
        )

    chunks: list[pd.DataFrame] = []
    with engine.connect().execution_options(stream_results=True, max_row_buffer=args.chunk_size) as conn:
        for chunk in pd.read_sql(query, conn, params=params, chunksize=args.chunk_size):
            chunks.append(chunk)
    if not chunks:
        raise SystemExit(f"区间 {args.start}~{args.end} 无数据，请检查连接与日期")
    caps = pd.concat(chunks, ignore_index=True)
    caps["code"] = caps["code"].astype(str).str.zfill(6)
    caps = caps.sort_values(["trade_date", "code"]).reset_index(drop=True)
    return caps


def cross_check(args: argparse.Namespace, caps: pd.DataFrame) -> dict:
    """抽样对账（V3.0 §4.5）。

    1) adj_factor：两源复权因子的**绝对水平不可比**（TuShare 自上市日累积，
       BaoStock hfq 自其数据基准重置，恒定倍数差）。有效口径是**窗口内除权日跳变一致性**：
       对每个除权日比较两源因子比值 (factor_t / factor_{t-1})，偏差 > 0.5% 告警；
    2) 涨跌停价：官方 stk_limit 与分段规则自算（Decimal ROUND_HALF_UP）逐日对账。
    """
    rules = MarketRules.load()
    sample_codes = caps["code"].drop_duplicates().sample(
        n=min(args.sample, caps["code"].nunique()), random_state=args.seed
    )
    bars_root = Path(args.bars_cache)
    jump_total = jump_dev = 0
    jump_worst = 0.0
    ratio_checked = ratio_constant = 0
    limit_total = limit_mismatch = 0
    limit_examples: list[str] = []

    caps_by_code = {
        code: group.set_index(pd.to_datetime(group["trade_date"]))
        for code, group in caps.groupby("code")
    }
    for code in sample_codes:
        try:
            classify_board(code)
        except ValueError:
            continue
        cap_rows = caps_by_code.get(code)
        frame = _load_cached(bars_root, code, args.start, args.end)
        if cap_rows is None or frame is None or frame.empty:
            continue
        joined = frame[["adj_factor", "pre_close", "close"]].join(
            cap_rows["adj_factor"].rename("caps_factor"), how="inner"
        ).dropna()
        if len(joined) < 3:
            continue

        # 0) 恒定倍数差检查（基准日不同的披露证据）
        ratio = (joined["caps_factor"] / joined["adj_factor"]).abs()
        if (ratio > 0).all():
            ratio_checked += 1
            if ratio.max() / ratio.min() - 1 < 1e-3:
                ratio_constant += 1

        # 1) 除权日跳变一致性
        ex_mask = joined["pre_close"].shift(1).notna() & (
            (joined["pre_close"] - joined["close"].shift(1)).abs() > 0.001
        )
        for ts in joined.index[ex_mask]:
            prev = joined.loc[:ts].index[-2]
            bars_jump = joined.loc[ts, "adj_factor"] / joined.loc[prev, "adj_factor"]
            caps_jump = joined.loc[ts, "caps_factor"] / joined.loc[prev, "caps_factor"]
            if bars_jump > 0 and caps_jump > 0:
                jump_total += 1
                deviation = abs(caps_jump / bars_jump - 1.0)
                jump_worst = max(jump_worst, deviation)
                if deviation > ADJ_FACTOR_WARN_THRESHOLD:
                    jump_dev += 1

        # 2) 涨跌停价对账（抽该股最多 5 个样本日，覆盖不同年份）
        #    listed_days 近似：窗口内才出现首根 bar 的股票视为新股（跨窗老股不可能处于
        #    上市初期规则段）。TuShare 对「不设限」用哨兵值 up=1000000 / down=0.01。
        is_st_series = cap_rows["is_st"] if "is_st" in cap_rows.columns else None
        new_listing = frame.index[0] > pd.Timestamp(args.start)
        pick = cap_rows.iloc[:: max(1, len(cap_rows) // 5)]
        sentinel_up = Decimal("1000000")
        sentinel_down = Decimal("0.01")
        for ts, row in pick.iterrows():
            if ts not in frame.index:
                continue
            bar = frame.loc[ts]
            is_st = bool(row.get("is_st", False)) if pd.notna(row.get("is_st")) else bool(bar.get("is_st", False))
            listed_days = int(frame.index.get_loc(ts)) + 1 if new_listing else None
            try:
                rule = rules.limit_rule(code, ts.date(), is_st=is_st, listed_days=listed_days)
            except ValueError:
                continue
            limit_up, limit_down = compute_limit_prices(bar["pre_close"], rule, rules.tick_size)
            for official, computed, name in (
                (row["up_limit"], limit_up, "up_limit"),
                (row["down_limit"], limit_down, "down_limit"),
            ):
                if pd.isna(official):
                    continue
                official_dec = Decimal(str(official)).quantize(Decimal("0.01"))
                # 双方都表达「不设限」（官方哨兵 vs 自算 None）→ 一致
                if computed is None:
                    if (name == "up_limit" and official_dec >= sentinel_up) or (
                        name == "down_limit" and official_dec <= sentinel_down
                    ):
                        limit_total += 1
                    continue
                limit_total += 1
                if abs(official_dec - computed) > LIMIT_PRICE_TOLERANCE:
                    limit_mismatch += 1
                    if len(limit_examples) < 5:
                        limit_examples.append(
                            f"{code}@{ts.date()} {name}: 官方 {official} vs 自算 {computed}（pre_close={bar['pre_close']}，listed_days={listed_days}）"
                        )

    result = {
        "sampled_codes": int(len(sample_codes)),
        "adj_factor": {
            "note": "绝对水平不可比（两源累积基准日不同，恒定倍数差）；按除权日跳变一致性对账",
            "codes_ratio_constant": f"{ratio_constant}/{ratio_checked}",
            "ex_date_jumps_compared": jump_total,
            "jump_dev_over_0_5pct": jump_dev,
            "worst_jump_deviation": round(jump_worst, 6),
        },
        "limit_prices": {
            "compared": limit_total,
            "mismatch": limit_mismatch,
            "examples": limit_examples,
        },
    }
    logger.info(
        "交叉校验：因子恒定倍数 %d/%d 只（基准日差异佐证）；除权跳变 %d 对比 / %d 超 0.5%%（最差 %.4f%%）；"
        "涨跌停价 %d 对比 / %d 不匹配",
        ratio_constant, ratio_checked, jump_total, jump_dev, jump_worst * 100, limit_total, limit_mismatch,
    )
    for example in limit_examples:
        logger.warning("涨跌停价不匹配：%s", example)
    if jump_dev and jump_total:
        logger.warning(
            "除权日因子跳变偏差超 0.5%% 的占比 %.2f%%（V3.0 §4.5 对账告警线）",
            jump_dev / jump_total * 100,
        )
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    caps = export_caps(args)
    out_path = cache_dir / "market_caps.csv.gz"
    caps.to_csv(out_path, index=False, compression="gzip")
    by_year = (
        caps.assign(year=pd.to_datetime(caps["trade_date"]).dt.year)
        .groupby("year")["code"].count().to_dict()
    )
    meta = {
        "source": "fund-analysis public.stock_eod_daily",
        "range": [args.start, args.end],
        "rows": int(len(caps)),
        "codes": int(caps["code"].nunique()),
        "rows_by_year": {int(year): int(count) for year, count in by_year.items()},
        "columns": CAPS_COLUMNS,
        "units": {"circ_mv": "元", "total_mv": "元", "size_edges": "亿元"},
        "exported_at": pd.Timestamp.now().isoformat(),
    }

    if args.cross_check:
        meta["cross_check"] = cross_check(args, caps)

    (cache_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "导出完成：%d 行 / %d 只 → %s（%.1f MB）",
        len(caps), caps["code"].nunique(), out_path, out_path.stat().st_size / 1e6,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
