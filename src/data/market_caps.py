# -*- coding: utf-8 -*-
"""市值/涨跌停价截面数据（V3.0 §4.5：stock_eod_daily 接入）。

数据源为 fund-analysis 库的 ``public.stock_eod_daily``（TuShare ``daily_basic`` +
``stk_limit`` + ``adj_factor`` 合成，含退市股），由 ``scripts/fetch_market_caps.py``
导出为本地缓存 ``data/cache/market_caps/market_caps.csv.gz``，本模块只读缓存。

单位约定：``circ_mv`` / ``total_mv`` 为**元**；分档边界 ``size_edges_yi`` 为**亿元**。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_SIZE_EDGES_YI: tuple[float, ...] = (20.0, 30.0, 45.0, 70.0)
DEFAULT_MIN_COVERAGE = 0.90
YI = 1e8  # 亿元 → 元

CAPS_COLUMNS = [
    "trade_date",
    "code",
    "circ_mv",
    "total_mv",
    "adj_factor",
    "up_limit",
    "down_limit",
    "is_st",
]


@dataclass(frozen=True)
class MatchedControlConfig:
    """B2 修正口径参数（config/backtest.yaml -> matched_control）。"""

    size_edges_yi: tuple[float, ...] = DEFAULT_SIZE_EDGES_YI
    min_coverage: float = DEFAULT_MIN_COVERAGE
    entry_gap_max: float = 0.03

    @classmethod
    def load(cls, path: Optional[Path] = None, raw: Optional[dict] = None) -> "MatchedControlConfig":
        if raw is None:
            from src.sim.rules import PROJECT_ROOT

            config_path = Path(path) if path else PROJECT_ROOT / "config" / "backtest.yaml"
            import yaml

            with config_path.open("r", encoding="utf-8") as handle:
                raw = (yaml.safe_load(handle) or {}).get("matched_control") or {}
        edges = raw.get("size_edges_yi") or list(DEFAULT_SIZE_EDGES_YI)
        return cls(
            size_edges_yi=tuple(float(edge) for edge in edges),
            min_coverage=float(raw.get("min_coverage", DEFAULT_MIN_COVERAGE)),
            entry_gap_max=float(raw.get("entry_gap_max", 0.03)),
        )


@dataclass
class CoverageReport:
    """市值覆盖情况，必须写入回测报告（V3.0 §4.5 / 方案 §12.3 局限声明）。"""

    total_rows: int = 0
    rows_with_caps: int = 0
    dates_total: int = 0
    dates_full_coverage: int = 0
    dates_fallback_proxy: int = 0
    first_caps_date: Optional[str] = None
    last_caps_date: Optional[str] = None

    @property
    def row_coverage(self) -> float:
        return self.rows_with_caps / self.total_rows if self.total_rows else 0.0

    def to_dict(self) -> dict:
        return {
            "total_rows": self.total_rows,
            "rows_with_caps": self.rows_with_caps,
            "row_coverage": round(self.row_coverage, 4),
            "dates_total": self.dates_total,
            "dates_full_coverage": self.dates_full_coverage,
            "dates_fallback_proxy": self.dates_fallback_proxy,
            "first_caps_date": self.first_caps_date,
            "last_caps_date": self.last_caps_date,
        }


def load_market_caps(cache_dir: str | Path) -> pd.DataFrame:
    """读取市值缓存；不存在或为空时抛出 FileNotFoundError（调用方决定降级路径）。"""
    path = Path(cache_dir) / "market_caps.csv.gz"
    if not path.exists():
        raise FileNotFoundError(
            f"市值缓存不存在：{path}。先运行 scripts/fetch_market_caps.py 导出"
            "（数据源 fund-analysis 库 stock_eod_daily，见 docs/swing-backtest.md）"
        )
    caps = pd.read_csv(
        path,
        dtype={"code": str},
        parse_dates=["trade_date"],
    )
    if caps.empty:
        raise ValueError(f"市值缓存为空：{path}")
    caps["code"] = caps["code"].str.zfill(6)
    return caps


def load_caps_meta(cache_dir: str | Path) -> dict:
    path = Path(cache_dir) / "meta.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def bucket_of_circ_mv(circ_mv: float, edges_yi: Sequence[float] = DEFAULT_SIZE_EDGES_YI) -> int:
    """流通市值（元）→ 分档序号；边界为闭开区间 [edge, next)。"""
    yi = float(circ_mv) / YI
    bucket = 0
    for edge in sorted(edges_yi):
        if yi >= edge:
            bucket += 1
    return bucket


def attach_market_caps(
    panel: pd.DataFrame,
    caps: pd.DataFrame,
    config: Optional[MatchedControlConfig] = None,
) -> tuple[pd.DataFrame, CoverageReport]:
    """把 circ_mv 分档并入池面板（date × code）。

    - panel 需含 ``date``/``code`` 列（``build_pool_panel`` 输出）；
    - 当日 circ_mv 覆盖率 >= config.min_coverage 时，``size_bucket`` 用绝对市值分档
      （``size_source='circ_mv'``）；否则该日回退为 20 日均额代理的截面分位桶
      （``size_source='proxy_quantile'``，与 legacy B2 同口径），并在覆盖报告中披露；
    - 返回 (新面板, 覆盖报告)。新面板新增 ``circ_mv``/``size_bucket``/``size_source`` 列。
    """
    config = config or MatchedControlConfig()
    report = CoverageReport()
    if panel.empty:
        out = panel.copy()
        for column in ("circ_mv", "size_bucket", "size_source"):
            out[column] = pd.Series(dtype=object if column == "size_source" else float)
        return out, report

    out = panel.copy()
    out["date"] = pd.to_datetime(out["date"])
    caps_local = caps[["trade_date", "code", "circ_mv"]].copy()
    caps_local["trade_date"] = pd.to_datetime(caps_local["trade_date"])
    merged = out.merge(
        caps_local,
        how="left",
        left_on=["date", "code"],
        right_on=["trade_date", "code"],
    ).drop(columns=["trade_date"])

    report.total_rows = int(len(merged))
    has_caps = merged["circ_mv"].notna() & (merged["circ_mv"] > 0)
    report.rows_with_caps = int(has_caps.sum())

    buckets = pd.Series(-1, index=merged.index, dtype=int)
    sources = pd.Series("proxy_quantile", index=merged.index, dtype=object)
    per_date = merged.groupby("date", sort=False)
    for date_value, day in per_date:
        day_has_caps = day["circ_mv"].notna() & (day["circ_mv"] > 0)
        coverage = float(day_has_caps.mean()) if len(day) else 0.0
        report.dates_total += 1
        if coverage >= config.min_coverage and day_has_caps.any():
            report.dates_full_coverage += 1
            sources.loc[day.index] = "circ_mv"
            buckets.loc[day.index] = day["circ_mv"].map(
                lambda value: bucket_of_circ_mv(value, config.size_edges_yi)
                if pd.notna(value) and value > 0
                else -1
            )
        else:
            report.dates_fallback_proxy += 1
            # 截面分位桶（与 legacy add_buckets 同口径），代理列缺失时单独成桶 -1
            try:
                quantile = pd.qcut(day["size"].astype(float), q=5, labels=False, duplicates="drop")
            except ValueError:
                quantile = pd.Series(0, index=day.index)
            buckets.loc[day.index] = pd.to_numeric(quantile, errors="coerce").fillna(-1).astype(int)
        if day_has_caps.any():
            first = str(date_value.date())
            last = str(date_value.date())
            if report.first_caps_date is None or first < report.first_caps_date:
                report.first_caps_date = first
            if report.last_caps_date is None or last > report.last_caps_date:
                report.last_caps_date = last

    merged["size_bucket"] = buckets
    merged["size_source"] = sources
    return merged, report
