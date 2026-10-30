#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""I-4 后续：用同库 `public.stock_eod_daily.adj_factor` 重建复权因子。

背景（见 docs/swing-backtest.md §4）：BaoStock hfq 对**配股类除权**存在漏调整
（全量扫描 9 例），而 TuShare 因子正确跳变。两源因子绝对水平不可比（累积基准日不同，
每股差一个恒定倍数），因此不能直接替换，只能**按锚点对齐**：

    aligned(t) = tu_share_factor(t) / tu_share_factor(anchor) * bao_factor(anchor)

anchor = 该股票两源都有数据的第一天（stock_eod_daily 自 2017-05-26 起）。
锚点之前没有 TuShare 数据，`aligned` 留空，读路径回退原始 BaoStock 因子（连续性不变，
因为 anchor 处 aligned == bao_factor）。

用法：
  python scripts/rebuild_adj_factor.py --report reports/adj_factor_rebuild.json
  python scripts/rebuild_adj_factor.py --dry-run           # 只出统计，不写库
  python scripts/rebuild_adj_factor.py --refresh-scope     # 只重算子集（--limit-codes）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.data.bars_store import (  # noqa: E402
    ENV_SCHEMA_KEY,
    ENV_URL_KEY,
    mask_url,
    resolve_schema,
    resolve_url,
)

logger = logging.getLogger(__name__)

DEFAULT_SOURCE_TABLE = "public.stock_eod_daily"
ADD_COLUMNS_SQL = """
ALTER TABLE {target}
    ADD COLUMN IF NOT EXISTS adj_factor_ts double precision,
    ADD COLUMN IF NOT EXISTS adj_factor_aligned double precision,
    ADD COLUMN IF NOT EXISTS adj_rebuilt_at timestamp without time zone
"""

# 锚点：每只票两源都有数据的第一天（两源因子都必须有效）
ANCHOR_SQL = """
CREATE TEMP TABLE tmp_anchor AS
SELECT b.code AS code, min(b.trade_date) AS anchor_date
FROM {target} b
JOIN {source} s ON s.code = b.code AND s.trade_date = b.trade_date
WHERE s.adj_factor IS NOT NULL AND s.adj_factor > 0
  AND b.adj_factor IS NOT NULL AND b.adj_factor > 0
GROUP BY b.code
"""

BASE_SQL = """
CREATE TEMP TABLE tmp_base AS
SELECT a.code AS code,
       s.adj_factor::double precision AS ts_anchor,
       b.adj_factor::double precision AS bao_anchor
FROM tmp_anchor a
JOIN {source} s ON s.code = a.code AND s.trade_date = a.anchor_date
JOIN {target} b ON b.code = a.code AND b.trade_date = a.anchor_date
"""

BASE_INDEX_SQL = "CREATE INDEX ON tmp_base (code)"

UPDATE_SQL = """
UPDATE {target} b
SET adj_factor_ts = s.adj_factor::double precision,
    adj_factor_aligned = (s.adj_factor::double precision / base.ts_anchor) * base.bao_anchor,
    adj_rebuilt_at = now()
FROM {source} s
JOIN tmp_base base ON base.code = s.code
WHERE s.code = b.code AND s.trade_date = b.trade_date
  AND s.adj_factor IS NOT NULL AND s.adj_factor > 0
"""""

STATS_SQL = """
SELECT
    count(*) FILTER (WHERE adj_factor_ts IS NOT NULL) AS rows_with_ts,
    count(DISTINCT code) FILTER (WHERE adj_factor_ts IS NOT NULL) AS codes_with_ts,
    count(*) FILTER (
        WHERE adj_factor_aligned IS NOT NULL
          AND abs(adj_factor_aligned / NULLIF(adj_factor, 0) - 1) > 1e-6
    ) AS rows_changed_any,
    count(DISTINCT code) FILTER (
        WHERE adj_factor_aligned IS NOT NULL
          AND abs(adj_factor_aligned / NULLIF(adj_factor, 0) - 1) > 1e-6
    ) AS codes_changed_any,
    count(*) FILTER (
        WHERE adj_factor_aligned IS NOT NULL
          AND abs(adj_factor_aligned / NULLIF(adj_factor, 0) - 1) > 0.005
    ) AS rows_corrected_material,
    count(DISTINCT code) FILTER (
        WHERE adj_factor_aligned IS NOT NULL
          AND abs(adj_factor_aligned / NULLIF(adj_factor, 0) - 1) > 0.005
    ) AS codes_corrected_material,
    count(*) AS rows_total
FROM {target}
"""

# 实质性修正事件（因子比例单日跳变 >2%，即真存在漏调整/多调整类不连续）
EVENTS_SQL = """
WITH x AS (
    SELECT code, trade_date, adj_factor_aligned / NULLIF(adj_factor, 0) AS ratio
    FROM {target}
    WHERE adj_factor_ts IS NOT NULL
), y AS (
    SELECT code, trade_date, ratio,
           lag(ratio) OVER (PARTITION BY code ORDER BY trade_date) AS prev
    FROM x
)
SELECT trade_date, code, prev AS ratio_before, ratio AS ratio_after,
       (ratio / NULLIF(prev, 0) - 1) AS step
FROM y
WHERE prev IS NOT NULL AND abs(ratio / prev - 1) > 0.02
ORDER BY abs(ratio / prev - 1) DESC
"""

TOP_CODES_SQL = """
SELECT code,
       count(*) AS rows_corrected,
       max(abs(adj_factor_aligned / NULLIF(adj_factor, 0) - 1)) AS max_dev
FROM {target}
WHERE adj_factor_aligned IS NOT NULL
  AND abs(adj_factor_aligned / NULLIF(adj_factor, 0) - 1) > 0.005
GROUP BY code
ORDER BY max_dev DESC
LIMIT 15
"""

# 漏调整典型样本：002466 2019-12-26 配股除权（BaoStock 未跳变，TuShare ×1.19）
KNOWN_CASES = (
    ("002466", "2019-12-20", "2020-01-08"),
)


def _engine(url: str) -> Engine:
    resolved = url.replace("postgres://", "postgresql+psycopg2://", 1)
    if resolved.startswith("postgresql://"):
        resolved = resolved.replace("postgresql://", "postgresql+psycopg2://", 1)
    options = os.environ.get("PGOPTIONS", "")
    connect_args = {"options": f"-cstatement_timeout=1800000 {options}".strip()}
    return create_engine(resolved, future=True, connect_args=connect_args)


def rebuild(
    url: str,
    schema: str,
    source_table: str = DEFAULT_SOURCE_TABLE,
    dry_run: bool = False,
) -> dict:
    target = f'"{schema}".swing_bars_daily'
    sql_target = target
    source = source_table
    engine = _engine(url)

    with engine.begin() as connection:
        connection.execute(text(ADD_COLUMNS_SQL.format(target=sql_target)))
        logger.info("列已就绪（adj_factor_ts / adj_factor_aligned / adj_rebuilt_at）")
        connection.execute(text(ANCHOR_SQL.format(target=sql_target, source=source)))
        connection.execute(text(BASE_SQL.format(target=sql_target, source=source)))
        connection.execute(text(BASE_INDEX_SQL))
        anchors = connection.execute(text("SELECT count(*) FROM tmp_anchor")).scalar_one()
        logger.info("锚点已建：%d 只", anchors)

        if dry_run:
            logger.info("dry-run：跳过 UPDATE")
        else:
            result = connection.execute(
                text(UPDATE_SQL.format(target=sql_target, source=source))
            )
            logger.info("回填完成：%s 行", result.rowcount)

        stats = connection.execute(text(STATS_SQL.format(target=sql_target))).mappings().one()
        events = [
            {
                "trade_date": str(row["trade_date"]),
                "code": row["code"],
                "step_pct": float(row["step"]) * 100,
            }
            for row in connection.execute(text(EVENTS_SQL.format(target=sql_target))).mappings()
        ]
        top_codes = [
            dict(row)
            for row in connection.execute(text(TOP_CODES_SQL.format(target=sql_target))).mappings()
        ]
        known = {}
        for code, start, end in KNOWN_CASES:
            rows = connection.execute(
                text(
                    f"""SELECT trade_date, adj_factor, adj_factor_ts, adj_factor_aligned
                        FROM {sql_target}
                        WHERE code = :code AND trade_date BETWEEN :start AND :end
                        ORDER BY trade_date"""
                ),
                {"code": code, "start": start, "end": end},
            ).mappings()
            known[code] = [dict(row) for row in rows]

    return {
        "target": f"{schema}.swing_bars_daily",
        "source": source,
        "dry_run": dry_run,
        "anchors": int(anchors),
        "stats": {key: int(value) for key, value in stats.items()},
        "material_events": events,
        "top_corrected_codes": [
            {**row, "max_dev": float(row["max_dev"])} for row in top_codes
        ],
        "known_cases": {
            code: [
                {
                    **{k: (str(v) if isinstance(v, (pd.Timestamp,)) else v) for k, v in row.items()},
                }
                for row in rows
            ]
            for code, rows in known.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="用 public.stock_eod_daily 重建 swing_bars 复权因子")
    parser.add_argument("--url", default=None, help=f"PostgreSQL URL（默认环境变量 {ENV_URL_KEY}）")
    parser.add_argument("--schema", default=None, help=f"目标 schema（默认环境变量 {ENV_SCHEMA_KEY}）")
    parser.add_argument("--source-table", default=DEFAULT_SOURCE_TABLE, help="因子来源表（同库跨 schema）")
    parser.add_argument("--dry-run", action="store_true", help="只建列与统计，不回填")
    parser.add_argument("--report", default=None, help="把校验报告写成 JSON（如 reports/adj_factor_rebuild.json）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    url = resolve_url(args.url)
    schema = resolve_schema(args.schema) or "public"
    if url.startswith("sqlite"):
        logger.error("复权重建需要 PostgreSQL 源表（%s）；sqlite 落库无跨 schema 因子数据", args.source_table)
        return 2

    logger.info("目标 %s.swing_bars_daily ← %s（%s）", schema, args.source_table, mask_url(url))
    report = rebuild(url, schema, args.source_table, dry_run=args.dry_run)
    stats = report["stats"]
    logger.info(
        "统计：总计 %d 行；有 TuShare 因子 %d 行 / %d 只；相对差异>1e-6 的 %d 行 / %d 只（含数值噪声）；"
        "实质性修正(>0.5%%) %d 行 / %d 只；单日跳变>2%% 的事件 %d 个",
        stats["rows_total"],
        stats["rows_with_ts"],
        stats["codes_with_ts"],
        stats["rows_changed_any"],
        stats["codes_changed_any"],
        stats["rows_corrected_material"],
        stats["codes_corrected_material"],
        len(report["material_events"]),
    )
    for event in report["material_events"][:15]:
        logger.info("  事件 %s %s 单日因子跳变 %+.2f%%", event["trade_date"], event["code"], event["step_pct"])
    for row in report["top_corrected_codes"][:10]:
        logger.info("  修正 %s：%d 行，最大偏离 %.2f%%", row["code"], row["rows_corrected"], row["max_dev"] * 100)
    for code, rows in report["known_cases"].items():
        changed = [r for r in rows if r["adj_factor_aligned"] is not None and r["adj_factor"]]
        jumps = [
            (str(r["trade_date"]), r["adj_factor"], r["adj_factor_aligned"]) for r in changed
        ]
        logger.info("  样本 %s：%s", code, jumps[:5])

    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        logger.info("报告已写入 %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
