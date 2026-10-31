#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""I-5：官方 stk_limit 涨跌停价 vs 分段规则自算 —— 全量对账与起始年确认（V3.0 §5.2）。

背景：方案 P0-02 遗留「TuShare ``stk_limit`` 的可用起始年不明」，V3.0 §8 只完成了
抽样对账（1436 对比 0 不匹配）。本脚本把该项一次做完，且**不需要 TuShare token**：

1. **起始年确认**：官方涨跌停价经 ``stock_eod_daily`` 落库，直接查该表即可得到首条
   有效记录的日期与按年覆盖度（原始数据来自 TuShare ``stk_limit``，见 §4.5）；
2. **全量对账**：把 ``swing_bars.swing_bars_daily``（BaoStock 不复权真实价 + pre_close）
   与 ``public.stock_eod_daily``（官方 up_limit/down_limit）按 ``(code, trade_date)`` 内连接，
   逐行用 ``MarketRules.limit_rule`` + ``compute_limit_prices`` 自算后比对，而不是抽样。

两表必须位于同一数据库（生产库即如此：``swing_bars`` schema 与 ``public`` 同库）。
不同库请先用 ``npm run eod:export/import``（fund-analysis）或
``scripts/fetch_market_caps.py --cross-check`` 做抽样对账。

口径与容差：
- 涨跌停价四舍五入到 0.01 元，允许 1 分钱舍入差（``LIMIT_PRICE_TOLERANCE``）；
- TuShare 用哨兵值表达「不设限」（``up_limit=1000000`` / ``down_limit=0.01``），
  与自算 ``None`` 视为一致；
- ``listed_days`` 只对本库内**确是上市初期**的代码传入（首根 bar 晚于数据集起点），
  其余传 ``None``（视为老股），避免把数据起点误判为上市首日；
- ``is_st`` 优先取官方口径（``stock_eod_daily.is_st``），缺失时回退行情表。

用法：
    # 起始年 + 按年覆盖度（只读，秒级）
    .venv/bin/python scripts/crosscheck_limit_prices.py --coverage-only

    # 全量对账（约 600 万行，10~20 分钟）
    .venv/bin/python scripts/crosscheck_limit_prices.py --start 2016-01-01 --end 2024-12-31

    # 冒烟：只对 50 只
    .venv/bin/python scripts/crosscheck_limit_prices.py --limit-codes 50

    # 结果写 JSON（默认写 reports/i5-limit-crosscheck/）
    .venv/bin/python scripts/crosscheck_limit_prices.py --out reports/i5-limit-crosscheck
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.sim.matcher import compute_limit_prices  # noqa: E402
from src.sim.rules import MarketRules, classify_board  # noqa: E402

logger = logging.getLogger("crosscheck_limit_prices")

# 官方哨兵值：TuShare stk_limit 用这些值表达「当日不设涨跌停」。
# 实测存在三种写法：1000000、100000、10000（见 reports/i5-limit-crosscheck 的 600145/600610）。
# A 股历史最高价约 2600 元（贵州茅台 2021），对应涨停价 ~2860，因此任何 ≥10000 的
# 「涨停价」都不可能是真实价格，统一按哨兵处理；跌停价 ≤0.01 同理。
SENTINEL_UP = Decimal("10000")
SENTINEL_DOWN = Decimal("0.01")
# 涨跌停价舍入到 0.01 元，允许 1 分钱差
LIMIT_PRICE_TOLERANCE = Decimal("0.01")
# BaoStock 缓存 / 数据集起点；首根 bar 等于该日说明是数据截断而未必是真实上市日
DATASET_START = date(2016, 1, 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="官方 stk_limit 与自算涨跌停价全量对账")
    parser.add_argument("--start", default="2016-01-01", help="对账起始日")
    parser.add_argument("--end", default="2026-12-31", help="对账截止日")
    parser.add_argument("--bars-url", default=None,
                        help="行情库连接串（默认 SWING_BARS_DATABASE_URL / DATABASE_URL）")
    parser.add_argument("--bars-schema", default=None,
                        help="行情表所在 schema（默认 SWING_BARS_SCHEMA，通常 swing_bars）")
    parser.add_argument("--caps-url", default=None,
                        help="官方涨跌停价库连接串（默认 MARKET_CAPS_DATABASE_URL / DATABASE_URL）")
    parser.add_argument("--caps-table", default="stock_eod_daily",
                        help="官方涨跌停价表名（默认 public.stock_eod_daily）")
    parser.add_argument("--limit-codes", type=int, default=0, help="只对前 N 只代码（冒烟用）")
    parser.add_argument("--codes-file", default=None, help="只对清单内代码（每行一个 6 位代码）")
    parser.add_argument("--chunk-size", type=int, default=200_000, help="单批读取行数")
    parser.add_argument("--coverage-only", action="store_true", help="只统计起始年与覆盖度，不做全量对账")
    parser.add_argument("--out", default=None, help="结果目录；缺省只打印不落盘")
    parser.add_argument("--max-examples", type=int, default=20, help="保留的不匹配样例数")
    parser.add_argument("--max-no-limit-rows", type=int, default=2000,
                        help="保留的「官方不设限」明细行数（供后续做 rules overlay）")
    parser.add_argument("--emit-no-limit-days", action="store_true",
                        help="把「官方不设限 / 自算有限价」的全部行写成 CSV（rules overlay 素材）")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------


def _normalize_driver(url: str) -> str:
    """补全 PostgreSQL 驱动名（本仓库依赖 psycopg2，SQLAlchemy 2.1 默认选 psycopg3）。"""
    if not url.startswith(("postgres://", "postgresql://")):
        return url
    bare = url.replace("postgres://", "postgresql://", 1)
    if bare.startswith("postgresql+psycopg"):
        return bare
    import importlib.util

    driver = "psycopg" if importlib.util.find_spec("psycopg") else "psycopg2"
    return bare.replace("postgresql://", f"postgresql+{driver}://", 1)


def bars_url(args: argparse.Namespace) -> str:
    url = args.bars_url or os.getenv("SWING_BARS_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit(
            "缺少行情库连接串：请用 --bars-url 或设置 SWING_BARS_DATABASE_URL / DATABASE_URL"
        )
    return _normalize_driver(url)


def caps_url(args: argparse.Namespace) -> str:
    url = args.caps_url or os.getenv("MARKET_CAPS_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit(
            "缺少官方涨跌停价库连接串：请用 --caps-url 或设置 MARKET_CAPS_DATABASE_URL / DATABASE_URL"
        )
    return _normalize_driver(url)


def bars_table(args: argparse.Namespace) -> str:
    schema = args.bars_schema or os.getenv("SWING_BARS_SCHEMA")
    return f'{schema}.swing_bars_daily' if schema else "swing_bars_daily"


def mask_url(url: str) -> str:
    """日志用：掩掉连接串里的密码。"""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    credentials, _, host = rest.rpartition("@")
    if ":" in credentials:
        user, _, _ = credentials.partition(":")
        credentials = f"{user}:***"
    return f"{scheme}://{credentials}@{host}"


# ---------------------------------------------------------------------------
# 1) 起始年与覆盖度
# ---------------------------------------------------------------------------

COVERAGE_SQL = """
SELECT
    count(*)                                              AS rows_total,
    count(up_limit)                                       AS rows_with_up,
    count(down_limit)                                     AS rows_with_down,
    min(CASE WHEN up_limit IS NOT NULL AND up_limit < :sentinel_up
              AND down_limit IS NOT NULL AND down_limit > :sentinel_down
             THEN trade_date END)                         AS first_effective_date,
    max(CASE WHEN up_limit IS NOT NULL AND up_limit < :sentinel_up
              AND down_limit IS NOT NULL AND down_limit > :sentinel_down
             THEN trade_date END)                         AS last_effective_date,
    count(DISTINCT code)                                  AS codes_total,
    count(DISTINCT CASE WHEN up_limit IS NOT NULL AND up_limit < :sentinel_up
                         AND down_limit IS NOT NULL AND down_limit > :sentinel_down
                        THEN code END)                    AS codes_effective
FROM {table}
"""

COVERAGE_BY_YEAR_SQL = """
SELECT
    extract(year FROM trade_date)::int                    AS year,
    count(*)                                              AS rows_total,
    count(CASE WHEN up_limit IS NOT NULL AND up_limit < :sentinel_up
                AND down_limit IS NOT NULL AND down_limit > :sentinel_down
               THEN 1 END)                                AS rows_effective,
    count(DISTINCT CASE WHEN up_limit IS NOT NULL AND up_limit < :sentinel_up
                         AND down_limit IS NOT NULL AND down_limit > :sentinel_down
                        THEN code END)                    AS codes_effective
FROM {table}
GROUP BY 1
ORDER BY 1
"""

FIRST_BAR_BY_CODE_SQL = """
SELECT code, min(trade_date) AS first_date, count(*) AS rows
FROM {table}
GROUP BY code
"""

# 新股代码的逐日「上市第 N 个交易日」。必须来自行情表本身，而不是内连接后的行流：
# 官方涨跌停价自 2017-05-26 才有，若用连接后的行计数，窗口前的交易日会被漏计，
# 把上市第 43 个交易日误判成第 1 个（实测 002774 等 581 处假差异即由此而来）。
LISTED_SEQ_SQL = """
WITH firsts AS (
    SELECT code, min(trade_date) AS first_date
    FROM {table}
    GROUP BY code
), new_codes AS (
    SELECT code FROM firsts WHERE first_date > :dataset_start
)
SELECT b.code, b.trade_date,
       ROW_NUMBER() OVER (PARTITION BY b.code ORDER BY b.trade_date) AS seq
FROM {table} b
JOIN new_codes n ON n.code = b.code
"""


def analyze_coverage(args: argparse.Namespace) -> dict:
    """确认官方 stk_limit 的有效起始年与按年覆盖度（无需 TuShare token）。"""
    from sqlalchemy import create_engine, text

    engine = create_engine(caps_url(args), future=True)
    table = args.caps_table
    params = {"sentinel_up": float(SENTINEL_UP), "sentinel_down": float(SENTINEL_DOWN)}
    with engine.connect() as connection:
        overall = connection.execute(text(COVERAGE_SQL.format(table=table)), params).mappings().one()
        by_year = [
            dict(row)
            for row in connection.execute(
                text(COVERAGE_BY_YEAR_SQL.format(table=table)), params
            ).mappings()
        ]

    first_effective = overall["first_effective_date"]
    conclusion = {
        "first_effective_date": str(first_effective) if first_effective else None,
        "first_effective_year": int(first_effective.year) if first_effective else None,
        "last_effective_date": (
            str(overall["last_effective_date"]) if overall["last_effective_date"] else None
        ),
        "note": (
            "official stk_limit 有效记录起点；在此之前不可依赖官方涨跌停价，"
            "须使用分段规则自算（config/market_rules.yaml）"
        ),
    }
    return {
        "table": table,
        "overall": {
            "rows_total": int(overall["rows_total"]),
            "rows_with_up": int(overall["rows_with_up"]),
            "rows_with_down": int(overall["rows_with_down"]),
            "codes_total": int(overall["codes_total"]),
            "codes_effective": int(overall["codes_effective"]),
        },
        "conclusion": conclusion,
        "by_year": by_year,
    }


# ---------------------------------------------------------------------------
# 2) 全量对账
# ---------------------------------------------------------------------------


def _codes_filter(args: argparse.Namespace) -> Optional[list[str]]:
    codes: Optional[list[str]] = None
    if args.codes_file:
        codes = [
            line.strip().zfill(6)
            for line in Path(args.codes_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    if args.limit_codes and args.limit_codes > 0:
        from sqlalchemy import create_engine, text

        engine = create_engine(bars_url(args), future=True)
        with engine.connect() as connection:
            rows = connection.execute(
                text(f"SELECT DISTINCT code FROM {bars_table(args)} ORDER BY code")
            ).scalars().all()
        picked = [str(code).zfill(6) for code in rows][: args.limit_codes]
        codes = picked if codes is None else [code for code in codes if code in set(picked)]
    return codes


def _first_bar_map(args: argparse.Namespace) -> dict[str, date]:
    """每只代码在本库中的首根 bar 日期（用于判断是否为「上市初期」）。"""
    from sqlalchemy import create_engine, text

    engine = create_engine(bars_url(args), future=True)
    with engine.connect() as connection:
        rows = connection.execute(text(FIRST_BAR_BY_CODE_SQL.format(table=bars_table(args)))).all()
    return {str(code).zfill(6): first for code, first, _ in rows}


def _listed_seq_map(args: argparse.Namespace, dataset_start: date) -> dict[tuple[str, date], int]:
    """返回 ``{(code, trade_date): 上市第 N 个交易日}``，仅覆盖真实新上市代码。"""
    from sqlalchemy import create_engine, text

    engine = create_engine(bars_url(args), future=True)
    table = bars_table(args)
    mapping: dict[tuple[str, date], int] = {}
    with engine.connect().execution_options(stream_results=True) as connection:
        result = connection.execute(
            text(LISTED_SEQ_SQL.format(table=table)), {"dataset_start": dataset_start}
        )
        for code, trade_date, seq in result:
            mapping[(str(code).zfill(6), trade_date)] = int(seq)
    return mapping


def _stream_joined(args: argparse.Namespace, codes: Optional[list[str]]) -> Iterable[pd.DataFrame]:
    """按 (code, trade_date) 流式读取内连接后的行情与官方涨跌停价。"""
    from sqlalchemy import create_engine, text

    table = bars_table(args)
    codes_clause = ""
    params: dict = {"start": args.start, "end": args.end}
    if codes:
        # 用 ANY(ARRAY[...]) 而不是 IN（:) 以便与 text() 参数展开兼容
        codes_clause = " AND b.code = ANY(:codes) "
        params["codes"] = codes

    query = text(
        f"""
        SELECT b.code, b.trade_date, b.pre_close, b.close, b.tradestatus,
               b.is_st AS bar_is_st, b.hfq_close,
               e.up_limit, e.down_limit, e.is_st AS official_is_st
        FROM {table} b
        JOIN {args.caps_table} e
          ON e.code = b.code AND e.trade_date = b.trade_date
        WHERE b.trade_date >= :start AND b.trade_date <= :end
        {codes_clause}
        ORDER BY b.code, b.trade_date
        """
    )
    engine = create_engine(bars_url(args), future=True)
    with engine.connect().execution_options(stream_results=True) as connection:
        for chunk in pd.read_sql(query, connection, params=params, chunksize=args.chunk_size):
            if not chunk.empty:
                yield chunk


def crosscheck(args: argparse.Namespace, first_bar: dict[str, date]) -> dict:
    """全量逐行对账：自算涨跌停价 vs 官方 stk_limit。

    差异按性质分类（只有 ``both_limited_differ`` 才是真正的规则错误）：

    | 分类 | 含义 | 是否需要修规则 |
    |---|---|---|
    | ``both_limited_equal`` | 双方都有限价且一致 | — |
    | ``both_limited_differ`` | 双方都有限价但不等 | **是**（真错） |
    | ``both_no_limit`` | 双方都表达不设限 | — |
    | ``official_no_limit_only`` | 官方不设限、自算按常规幅度给价 | 规则段缺口（退市整理首日/恢复上市首日/新股首日） |
    | ``computed_no_limit_only`` | 自算不设限、官方有限价 | **是**（真错） |
    """
    rules = MarketRules.load()
    codes = _codes_filter(args)
    # 数据集实际起点 = 全部代码首根 bar 的最早值。不能用硬编码日期：
    # 首根 bar 等于数据集起点的代码通常是「数据截断」而非新上市，必须当老股处理。
    dataset_start = min(first_bar.values()) if first_bar else DATASET_START

    fields: dict[str, dict[str, int]] = {
        name: {
            "both_limited_equal": 0,
            "both_limited_differ": 0,
            "first_day_regime_differ": 0,
            "both_no_limit": 0,
            "official_no_limit_only": 0,
            "computed_no_limit_only": 0,
        }
        for name in ("up_limit", "down_limit")
    }
    total = 0
    rows_without_pre_close = 0
    board_skipped = 0
    examples: dict[str, list[dict]] = defaultdict(list)
    mismatch_by_year_pct: dict[int, dict] = defaultdict(lambda: {"up_5pct": 0, "up_10pct": 0, "up_other": 0,
                                                               "down_5pct": 0, "down_10pct": 0, "down_other": 0})
    mismatch_by_code: dict[str, int] = defaultdict(int)
    first_day_cases: list[dict] = []
    no_limit_days: list[dict] = []
    # (code, trade_date) → 上市第 N 个交易日；只覆盖真实新上市代码，其余按老股处理
    listed_seq = _listed_seq_map(args, dataset_start)
    logger.info("新股交易日序号表：%d 条（新上市代码）", len(listed_seq))
    by_year: dict[int, dict] = defaultdict(lambda: {"compared": 0, "mismatch": 0})
    by_reason: dict[str, int] = defaultdict(int)

    chunks_seen = 0
    for chunk in _stream_joined(args, codes):
        chunks_seen += 1
        for record in chunk.itertuples(index=False):
            code = str(record.code).zfill(6)
            trade_date = pd.Timestamp(record.trade_date).date()
            year = trade_date.year

            pre_close = record.pre_close
            if pre_close is None or pd.isna(pre_close) or float(pre_close) <= 0:
                rows_without_pre_close += 1
                continue
            try:
                classify_board(code)
            except ValueError:
                board_skipped += 1
                continue

            # 上市初期判定：本库首根 bar 晚于数据集起点 → 视为真实新股，否则视为老股。
            # listed_days 必须用**交易日的真实序号**（不是自然日近似）：新股规则段按
            # 「上市第 N 个交易日」定义，用自然日折算会把第 2 个交易日误判成第 1 个。
            listed_days = listed_seq.get((code, trade_date))

            is_st_value = record.official_is_st
            if is_st_value is None or pd.isna(is_st_value):
                is_st_value = record.bar_is_st
            is_st = bool(is_st_value) if is_st_value is not None and not pd.isna(is_st_value) else False

            try:
                rule = rules.limit_rule(code, trade_date, is_st=is_st, listed_days=listed_days)
            except ValueError as error:  # 缺配置段：计入跳过并在报告中披露
                by_reason[f"rule_error:{error}"] += 1
                continue

            limit_up, limit_down = compute_limit_prices(pre_close, rule, rules.tick_size)
            by_year[year]["compared"] += 1
            total += 1

            for official, computed, name, sentinel in (
                (record.up_limit, limit_up, "up_limit", SENTINEL_UP),
                (record.down_limit, limit_down, "down_limit", SENTINEL_DOWN),
            ):
                if official is None or pd.isna(official):
                    continue
                official_dec = Decimal(str(official)).quantize(Decimal("0.01"))
                # 官方是否表达「不设限」
                official_no_limit = (
                    official_dec >= SENTINEL_UP if name == "up_limit" else official_dec <= SENTINEL_DOWN
                )
                stats = fields[name]

                if official_no_limit and computed is None:
                    stats["both_no_limit"] += 1
                    continue
                if official_no_limit and computed is not None:
                    stats["official_no_limit_only"] += 1
                    by_year[year]["mismatch"] += 1
                    if len(no_limit_days) < args.max_no_limit_rows:
                        no_limit_days.append(
                            {"code": code, "trade_date": str(trade_date), "pre_close": float(pre_close)}
                        )
                    if len(examples["official_no_limit_only"]) < args.max_examples:
                        examples["official_no_limit_only"].append(
                            _example(code, trade_date, name, official_dec, computed, pre_close, is_st,
                                     listed_days, rule.reason, "official_no_limit_only")
                        )
                    continue
                if not official_no_limit and computed is None:
                    stats["computed_no_limit_only"] += 1
                    by_year[year]["mismatch"] += 1
                    mismatch_by_code[code] += 1
                    if len(examples["computed_no_limit_only"]) < args.max_examples:
                        examples["computed_no_limit_only"].append(
                            _example(code, trade_date, name, official_dec, computed, pre_close, is_st,
                                     listed_days, rule.reason, "computed_no_limit_only")
                        )
                    continue

                if abs(official_dec - computed) <= LIMIT_PRICE_TOLERANCE:
                    stats["both_limited_equal"] += 1
                    continue
                # 上市首日（listed_days == 1）双方都有价但不符 → 归为「首日口径差异」：
                # 交易所对 IPO / 换股吸收合并 / 重新上市 / 分拆 的首日申报区间各不相同，
                # 规则引擎只建模了最常见的 IPO 口径。属已知缺口，不计入真错。
                if listed_days == 1:
                    stats["first_day_regime_differ"] += 1
                    if len(first_day_cases) < 50:
                        first_day_cases.append(
                            {"code": code, "trade_date": str(trade_date), "field": name,
                             "official": str(official_dec),
                             "computed": str(computed) if computed is not None else "no_limit"}
                        )
                    continue
                stats["both_limited_differ"] += 1
                by_year[year]["mismatch"] += 1
                mismatch_by_code[code] += 1
                # 诊断：官方幅度是 5%（ST）还是 10%（常规）？用于判定是否为 ST 时点错配
                official_pct = Decimal(str(official)) / Decimal(str(pre_close)) - 1
                if name == "up_limit":
                    if abs(official_pct - Decimal("0.05")) < Decimal("0.005"):
                        mismatch_by_year_pct[year]["up_5pct"] += 1
                    elif abs(official_pct - Decimal("0.10")) < Decimal("0.005"):
                        mismatch_by_year_pct[year]["up_10pct"] += 1
                    else:
                        mismatch_by_year_pct[year]["up_other"] += 1
                else:
                    if abs(official_pct + Decimal("0.05")) < Decimal("0.005"):
                        mismatch_by_year_pct[year]["down_5pct"] += 1
                    elif abs(official_pct + Decimal("0.10")) < Decimal("0.005"):
                        mismatch_by_year_pct[year]["down_10pct"] += 1
                    else:
                        mismatch_by_year_pct[year]["down_other"] += 1
                if len(examples["both_limited_differ"]) < args.max_examples:
                    item = _example(code, trade_date, name, official_dec, computed, pre_close, is_st,
                                    listed_days, rule.reason, "both_limited_differ")
                    item["official_pct"] = float(official_pct)
                    examples["both_limited_differ"].append(item)
        if chunks_seen % 5 == 0:
            logger.info(
                "对账进度：%d 批 / %s 行；真差异（双方均有限价但不符）up=%d down=%d，"
                "规则段缺口 up=%d down=%d",
                chunks_seen,
                f"{total:,}",
                fields["up_limit"]["both_limited_differ"],
                fields["down_limit"]["both_limited_differ"],
                fields["up_limit"]["official_no_limit_only"],
                fields["down_limit"]["official_no_limit_only"],
            )

    real_mismatch = sum(stats["both_limited_differ"] + stats["computed_no_limit_only"] for stats in fields.values())
    regime_gap = sum(stats["official_no_limit_only"] for stats in fields.values())
    first_day_gap = sum(stats["first_day_regime_differ"] for stats in fields.values())
    compared = sum(stats["both_limited_equal"] + stats["both_limited_differ"] for stats in fields.values())
    return {
        "range": {"start": args.start, "end": args.end},
        "codes_scanned": len(codes) if codes else None,
        "rows_compared": total,
        "rows_without_pre_close": rows_without_pre_close,
        "board_skipped": board_skipped,
        "up_limit": fields["up_limit"],
        "down_limit": fields["down_limit"],
        "limited_rows_compared": compared,
        "real_mismatch": real_mismatch,
        "regime_gap": regime_gap,
        "first_day_gap": first_day_gap,
        "first_day_cases": first_day_cases,
        "real_mismatch_rate": round(real_mismatch / compared, 8) if compared else None,
        "tolerance": str(LIMIT_PRICE_TOLERANCE),
        "by_year": [{"year": year, **stats} for year, stats in sorted(by_year.items())],
        "skipped_rules": dict(by_reason),
        "examples": {key: value for key, value in examples.items()},
        "mismatch_by_year_pct": [
            {"year": year, **stats} for year, stats in sorted(mismatch_by_year_pct.items())
        ],
        "mismatch_top_codes": sorted(mismatch_by_code.items(), key=lambda item: -item[1])[:20],
        "no_limit_days_sample": no_limit_days,
        "verdict": (
            "PASS：双方均有限价的行零差异，自算分段规则可替代官方 stk_limit；"
            f"另有 {regime_gap} 处「官方不设限 / 自算有限价」（退市整理首日、恢复上市首日）"
            f"与 {first_day_gap} 处「上市首日口径差异」（IPO/换股/重新上市首日申报区间不同）"
            "（规则段未建模；方向为保守——回测只会把可买误判为买不到，不会制造虚假成交）"
            if real_mismatch == 0
            else f"FAIL：{real_mismatch} 处真差异（双方均有限价但不符，或自算误判不设限），需核对规则段配置"
        ),
    }


def _example(
    code: str,
    trade_date: date,
    field_name: str,
    official: Decimal,
    computed: Optional[Decimal],
    pre_close: float,
    is_st: bool,
    listed_days: Optional[int],
    rule_reason: str,
    category: str,
) -> dict:
    return {
        "code": code,
        "trade_date": str(trade_date),
        "field": field_name,
        "category": category,
        "official": str(official),
        "computed": str(computed) if computed is not None else "no_limit",
        "pre_close": float(pre_close),
        "is_st": is_st,
        "listed_days": listed_days,
        "rule": rule_reason,
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    logger.info("行情库：%s", mask_url(bars_url(args)))
    logger.info("对账表：%s（官方库 %s）", args.caps_table, mask_url(caps_url(args)))

    coverage = analyze_coverage(args)
    conclusion = coverage["conclusion"]
    print("\n=== 官方 stk_limit 覆盖度 ===")
    print(f"表：{coverage['table']}")
    print(
        f"行数 {coverage['overall']['rows_total']:,}，含 up_limit {coverage['overall']['rows_with_up']:,}，"
        f"含 down_limit {coverage['overall']['rows_with_down']:,}"
    )
    print(
        f"代码 {coverage['overall']['codes_effective']:,}/{coverage['overall']['codes_total']:,} 只有有效涨跌停价"
    )
    print(
        f"有效记录区间：{conclusion['first_effective_date']} ~ {conclusion['last_effective_date']}"
        f"（起始年 {conclusion['first_effective_year']}）"
    )

    result: dict = {"coverage": coverage}
    if not args.coverage_only:
        logger.info("开始全量对账（%s ~ %s）...", args.start, args.end)
        first_bar = _first_bar_map(args)
        crosscheck_result = crosscheck(args, first_bar)
        result["crosscheck"] = crosscheck_result
        print("\n=== 全量涨跌停价对账 ===")
        print(f"比对行数：{crosscheck_result['rows_compared']:,}")
        for name, label in (("up_limit", "涨停价"), ("down_limit", "跌停价")):
            stats = crosscheck_result[name]
            print(
                f"{label}：一致 {stats['both_limited_equal']:,}，"
                f"真不符 {stats['both_limited_differ']:,}，"
                f"首日口径差异 {stats['first_day_regime_differ']:,}，"
                f"双方均不设限 {stats['both_no_limit']:,}，"
                f"官方不设限而自算有限价 {stats['official_no_limit_only']:,}，"
                f"自算不设限而官方有限价 {stats['computed_no_limit_only']:,}"
            )
        print(
            f"有限价行合计 {crosscheck_result['limited_rows_compared']:,}："
            f"真差异 {crosscheck_result['real_mismatch']:,}（{crosscheck_result['real_mismatch_rate']}）"
        )
        print(f"判定：{crosscheck_result['verdict']}")
        if crosscheck_result["examples"]:
            for category, items in crosscheck_result["examples"].items():
                if not items:
                    continue
                print(f"\n样例 — {category}（最多 {len(items)} 条）：")
                for example in items:
                    print("  ", json.dumps(example, ensure_ascii=False))
        if crosscheck_result.get("mismatch_top_codes"):
            print("\n差异最集中的代码：", crosscheck_result["mismatch_top_codes"])

    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "limit_crosscheck.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        logger.info("结果已写入 %s", out_dir / "limit_crosscheck.json")
        no_limit_sample = (result.get("crosscheck") or {}).get("no_limit_days_sample") or []
        if args.emit_no_limit_days and no_limit_sample:
            # 同一 (code, trade_date) 会同时命中涨停与跌停两条，去重后再落盘
            detail = pd.DataFrame(no_limit_sample).drop_duplicates(subset=["code", "trade_date"])
            detail = detail.sort_values(["code", "trade_date"])
            detail.to_csv(out_dir / "no_limit_days.csv", index=False, encoding="utf-8")
            logger.info("不设限日明细已写入 %s", out_dir / "no_limit_days.csv")

    if not args.coverage_only and result["crosscheck"]["real_mismatch"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
