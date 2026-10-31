#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""I-4：把 BaoStock 行情缓存落库（V3.0 §5.2）。

用法：
  # 全量落库（默认 sqlite:data/swing_bars.db；生产用 --url 指向 PostgreSQL）
  python scripts/ingest_swing_bars.py --cache-dir data/cache/swing_bars_full \
    --url "$SWING_BARS_DATABASE_URL"

  # 冒烟 / 只看规模
  python scripts/ingest_swing_bars.py --cache-dir data/cache/swing_bars_full \
    --limit-codes 50
  python scripts/ingest_swing_bars.py --cache-dir data/cache/swing_bars_full --dry-run

  # 只落指定代码清单
  python scripts/ingest_swing_bars.py --cache-dir data/cache/swing_bars_full \
    --codes-file data/cache/swing_bars_full/missing_codes_now.txt

幂等：同一 `fetched_at` + `rows` 的代码默认跳过；`--refresh` 强制重写。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.data.bars_store import (  # noqa: E402
    ENV_SCHEMA_KEY,
    ENV_URL_KEY,
    ingest_cache,
    mask_url,
    resolve_schema,
    resolve_url,
    table_counts,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="把 BaoStock 行情缓存落库（V3.0 I-4）")
    parser.add_argument("--cache-dir", default="data/cache/swing_bars_full")
    parser.add_argument(
        "--url",
        default=None,
        help=f"数据库 URL；默认环境变量 {ENV_URL_KEY} 或 sqlite:///data/swing_bars.db",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help=f"目标 schema（PostgreSQL 推荐独立 schema，如 swing_bars）；"
        f"默认环境变量 {ENV_SCHEMA_KEY}，不设则用默认 schema",
    )
    parser.add_argument("--start", default=None, help="只落该日期之后的行情（默认全部）")
    parser.add_argument("--end", default=None, help="只落该日期之前的行情（默认全部）")
    parser.add_argument("--codes-file", default=None, help="每行一个 6 位代码（默认扫描缓存全部）")
    parser.add_argument("--limit-codes", type=int, default=0, help="只落前 N 只（冒烟用）")
    parser.add_argument("--batch-codes", type=int, default=50, help="每批提交的股票数")
    parser.add_argument("--refresh", action="store_true", help="忽略已有 fetched_at，强制重写")
    parser.add_argument("--dry-run", action="store_true", help="只统计规模，不建表不写库")
    parser.add_argument(
        "--with-pool",
        action="store_true",
        help="同时落时点股票池到 swing_pool_daily（V3.0 I-4；与 run 无关的共享表）",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    url = resolve_url(args.url)
    schema = resolve_schema(args.schema)

    codes = None
    if args.codes_file:
        codes = [
            line.strip()
            for line in Path(args.codes_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if args.limit_codes and args.limit_codes > 0:
            codes = codes[: args.limit_codes]
    elif args.limit_codes and args.limit_codes > 0:
        root = Path(args.cache_dir)
        codes = sorted(path.name[:-7] for path in root.glob("raw/*.csv.gz"))[: args.limit_codes]

    logging.info(
        "落库目标：%s%s（缓存 %s）",
        mask_url(url),
        f" [schema={schema}]" if schema else "",
        args.cache_dir,
    )
    result = ingest_cache(
        cache_dir=args.cache_dir,
        url=url,
        start=args.start,
        end=args.end,
        codes=codes,
        refresh=args.refresh,
        dry_run=args.dry_run,
        batch_codes=args.batch_codes,
        schema=schema,
    )
    summary = result.summary()
    if args.dry_run:
        logging.info(
            "dry-run：扫描 %d 只，预计写入 %d 行，缺数据 %d 只",
            summary["codes_seen"],
            summary["rows_written"],
            summary["codes_without_data"],
        )
    else:
        logging.info("落库完成：%s；库内现有 %s", summary, table_counts(url, schema))

    if args.with_pool and not args.dry_run:
        logging.info("开始落时点股票池 ...")
        from src.data.loader import load_frames
        from src.data.results_store import save_pool_daily, table_counts as results_table_counts

        frames = load_frames(
            start=args.start or "2016-01-01",
            end=args.end or "2026-12-31",
            cache_dir=args.cache_dir,
            codes_file=args.codes_file,
            limit_codes=args.limit_codes,
        )
        written = save_pool_daily(
            frames, url=url, schema=schema, start=args.start, end=args.end
        )
        logging.info("股票池落库完成：%d 行；结果库现有 %s", written, results_table_counts(url, schema))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
