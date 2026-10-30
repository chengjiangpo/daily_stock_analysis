# -*- coding: utf-8 -*-
"""行情装载入口（本地 CSV 或 BaoStock）。"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from .baostock_bars import _load_cached, discover_universe, fetch_industry_map, fetch_many
from .bars import normalize_daily_bars

logger = logging.getLogger(__name__)


def load_local_frames(data_dir: str | Path) -> Dict[str, pd.DataFrame]:
    frames: Dict[str, pd.DataFrame] = {}
    for path in sorted(Path(data_dir).glob("*.csv*")):
        try:
            frame = pd.read_csv(path)
            code = str(frame["code"].iloc[0]).zfill(6) if "code" in frame.columns else path.name.split(".")[0]
            frames[code] = normalize_daily_bars(frame, code=code)
        except Exception as error:  # noqa: BLE001
            logger.warning("读取 %s 失败: %s", path, error)
    return frames


def universe_sample_dates(start: str, end: str) -> list[date]:
    dates = []
    start_year = pd.Timestamp(start).year
    end_ts = pd.Timestamp(end)
    for year in range(start_year, end_ts.year + 1):
        for month, day in ((6, 28), (12, 28)):
            candidate = pd.Timestamp(year=year, month=month, day=day)
            if pd.Timestamp(start) <= candidate <= end_ts:
                dates.append(candidate.date())
    return dates


def load_frames(
    start: str,
    end: str,
    data_dir: Optional[str] = None,
    cache_dir: Optional[str] = "data/cache/swing_bars",
    codes_file: Optional[str] = None,
    limit_codes: int = 0,
    refresh: bool = False,
    bars_url: Optional[str] = None,
    bars_schema: Optional[str] = None,
    adj_source: str = "baostock",
    progress_every: int = 20,
) -> Dict[str, pd.DataFrame]:
    if data_dir:
        return load_local_frames(data_dir)

    cache_root = Path(cache_dir) if cache_dir else None
    codes: Optional[list[str]] = None
    if bars_url:
        # I-4：从库读回，输出与 CSV 缓存同构；行业仍从缓存目录补（可缺）
        from .bars_store import load_frames_from_db, mask_url

        if codes_file:
            codes = [
                line.strip() for line in Path(codes_file).read_text(encoding="utf-8").splitlines() if line.strip()
            ]
        logger.info("从数据库装载行情（%s%s）", mask_url(bars_url), f" [schema={bars_schema}]" if bars_schema else "")
        frames = load_frames_from_db(
            url=bars_url,
            start=start,
            end=end,
            codes=codes,
            limit_codes=limit_codes,
            schema=bars_schema,
            adj_source=adj_source,
        )
        logger.info("载入 %d 只股票（数据库）", len(frames))
        industry_file = cache_root / "industry.json" if cache_root is not None else None
        if industry_file is not None and industry_file.exists():
            # 只读本地行业缓存，不从库读路径触发网络登录
            try:
                industry_map = fetch_industry_map(cache_root, refresh=False)
            except Exception as error:  # noqa: BLE001 - 行业缺失不阻塞回测
                logger.warning("行业映射获取失败，B2 将退化为全池抽样: %s", error)
                industry_map = {}
            if industry_map:
                for code, frame in frames.items():
                    frame["industry"] = industry_map.get(code, "UNKNOWN")
        return frames

    if codes_file:
        codes = [line.strip() for line in Path(codes_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        codes = discover_universe(universe_sample_dates(start, end), cache_dir=cache_root, refresh=refresh)
    if limit_codes and limit_codes > 0:
        codes = codes[:limit_codes]
    logger.info("股票池 %d 只，抓取区间 %s ~ %s", len(codes), start, end)

    def progress(index: int, total: int, code: str) -> None:
        if progress_every > 0 and (index % progress_every == 0 or index == total):
            logger.info("抓取进度 %d/%d (%s)", index, total, code)

    frames = fetch_many(
        codes,
        start,
        end,
        cache_dir=cache_root,
        refresh=refresh,
        progress=progress,
        sleep_seconds=0.05,
    )
    try:
        industry_map = fetch_industry_map(cache_root, refresh=refresh)
    except Exception as error:  # noqa: BLE001 - 行业缺失不阻塞回测
        logger.warning("行业映射获取失败，B2 将退化为全池抽样: %s", error)
        industry_map = {}
    if industry_map:
        for code, frame in frames.items():
            if "industry" not in frame.columns:
                frame["industry"] = industry_map.get(code, "UNKNOWN")
            else:
                frame["industry"] = frame["industry"].fillna(industry_map.get(code, "UNKNOWN"))
    return frames


def load_cached_frames(
    cache_dir: str | Path,
    start: str,
    end: str,
    max_codes: int = 0,
    attach_industry: bool = True,
) -> Dict[str, pd.DataFrame]:
    """只读本地缓存，不触发任何网络登录（供 Web/API 使用）。"""
    root = Path(cache_dir)
    if not root.exists():
        return {}
    paths = sorted(root.glob("raw/*.csv.gz"))
    if max_codes and 0 < max_codes < len(paths):
        # 等距抽样，避免只取代码段前部造成样本偏差。
        step = len(paths) / max_codes
        paths = [paths[int(index * step)] for index in range(max_codes)]

    frames: Dict[str, pd.DataFrame] = {}
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    for path in paths:
        code = path.name.split(".")[0]
        frame = _load_cached(root, code, start, end)
        if frame is None:
            continue
        part = frame[(frame.index >= start_ts) & (frame.index <= end_ts)]
        if not part.empty:
            frames[code] = part

    if attach_industry:
        industry_file = root / "industry.json"
        if industry_file.exists():
            import json

            try:
                industry_map = json.loads(industry_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                industry_map = {}
            if industry_map:
                for code, frame in frames.items():
                    frame["industry"] = industry_map.get(code, "UNKNOWN")
    return frames
