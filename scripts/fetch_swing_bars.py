#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按代码清单抓取 BaoStock 行情（支持分片并发）。"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.data.baostock_bars import fetch_many  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 BaoStock 原始+后复权日线")
    parser.add_argument("--codes-file", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--label", default="fetch")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [{args.label}] %(levelname)s %(message)s")
    codes = [line.strip() for line in Path(args.codes_file).read_text(encoding="utf-8").splitlines() if line.strip()]

    def progress(index: int, total: int, code: str) -> None:
        if index % 20 == 0 or index == total:
            logging.info("进度 %d/%d (%s)", index, total, code)

    frames = fetch_many(
        codes,
        args.start,
        args.end,
        cache_dir=args.cache_dir,
        refresh=args.refresh,
        progress=progress,
        sleep_seconds=0.02,
    )
    logging.info("完成：%d/%d 只有数据", len(frames), len(codes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
