# -*- coding: utf-8 -*-
"""行情标准化、数据质量校验与快照哈希（方案 §4.4/§4.5）。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume", "amount")


def normalize_daily_bars(df: pd.DataFrame, code: Optional[str] = None) -> pd.DataFrame:
    """把数据源日线统一成回测约定格式。

    输出索引为 DatetimeIndex；价格为不复权真实价；adj_factor = 后复权价 / 真实价。
    """
    out = df.copy()
    out.columns = [str(column).strip().lower() for column in out.columns]
    renames = {
        "trade_date": "date",
        "preclose": "pre_close",
        "pre_close_price": "pre_close",
        "pctchg": "pct_chg",
        "isst": "is_st",
        "trade_status": "tradestatus",
    }
    out = out.rename(columns={key: value for key, value in renames.items() if key in out.columns})

    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out = out.dropna(subset=["date"]).set_index("date")
    if not isinstance(out.index, pd.DatetimeIndex):
        raise TypeError("日线数据缺少 date 列或 DatetimeIndex")
    out.index = pd.to_datetime(out.index)
    out.index.name = "date"

    for column in REQUIRED_COLUMNS:
        if column not in out.columns:
            raise ValueError(f"日线数据缺少必要列: {column}")
        out[column] = pd.to_numeric(out[column], errors="coerce")

    if "pre_close" not in out.columns:
        out["pre_close"] = out["close"].shift(1)
    else:
        out["pre_close"] = pd.to_numeric(out["pre_close"], errors="coerce")
        out["pre_close"] = out["pre_close"].fillna(out["close"].shift(1))

    status = out["tradestatus"] if "tradestatus" in out.columns else pd.Series(1, index=out.index)
    out["tradestatus"] = pd.to_numeric(status, errors="coerce").fillna(0).astype(int)
    is_st = out["is_st"] if "is_st" in out.columns else pd.Series(0, index=out.index)
    if is_st.dtype == object:
        out["is_st"] = is_st.astype(str).str.strip().isin({"1", "1.0", "true", "True", "是", "ST"})
    else:
        out["is_st"] = pd.to_numeric(is_st, errors="coerce").fillna(0).astype(bool)
    factor = out["adj_factor"] if "adj_factor" in out.columns else pd.Series(1.0, index=out.index)
    out["adj_factor"] = pd.to_numeric(factor, errors="coerce").fillna(1.0)

    if "code" not in out.columns:
        if code is None:
            raise ValueError("日线数据缺少 code 列且未传入 code")
        out["code"] = code
    else:
        out["code"] = out["code"].astype(str)

    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def mark_limit_prices(
    df: pd.DataFrame,
    code: str,
    rules: Any,
    tick_size: Any = None,
) -> pd.DataFrame:
    """按交易日分段计算涨跌停价（逐行，适合中小样本；全市场离线可预计算落库）。"""
    from src.sim.matcher import compute_limit_prices
    from src.sim.rules import quantize_price, to_decimal

    out = df.copy()
    tick = tick_size or getattr(rules, "tick_size", None) or quantize_price(Decimal("0.01"))
    ups, downs = [], []
    for timestamp, row in out.iterrows():
        pre_close = row.get("pre_close")
        if pd.isna(pre_close) or float(pre_close) <= 0:
            ups.append(np.nan)
            downs.append(np.nan)
            continue
        is_st = bool(row.get("is_st", False))
        listed_days = row.get("listed_days")
        listed_days = int(listed_days) if pd.notna(listed_days) else None
        is_delisting = bool(row.get("is_delisting", False))
        delisting_day = row.get("delisting_day")
        delisting_day = int(delisting_day) if pd.notna(delisting_day) else None
        rule = rules.limit_rule(
            code,
            timestamp.date(),
            is_st=is_st,
            listed_days=listed_days,
            is_delisting=is_delisting,
            delisting_day=delisting_day,
        )
        up, down = compute_limit_prices(to_decimal(pre_close), rule, tick)
        ups.append(float(up) if up is not None else np.nan)
        downs.append(float(down) if down is not None else np.nan)
    out["limit_up"] = ups
    out["limit_down"] = downs
    return out


def snapshot_hash(frames: dict[str, pd.DataFrame]) -> str:
    """对行情快照做稳定哈希，写入 run_metadata（方案 §4.5 #10）。"""
    digest = hashlib.sha256()
    for code in sorted(frames):
        frame = frames[code]
        digest.update(code.encode("utf-8"))
        columns = [column for column in ("open", "high", "low", "close", "volume", "amount", "adj_factor")
                   if column in frame.columns]
        payload = frame[columns].round(6).to_csv(float_format="%.6f").encode("utf-8")
        digest.update(payload)
    return digest.hexdigest()


@dataclass
class QualityCheck:
    check_id: int
    name: str
    passed: bool
    failures: int = 0
    details: str = ""

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "name": self.name,
            "passed": self.passed,
            "failures": self.failures,
            "details": self.details,
        }


def quality_report(
    df: pd.DataFrame,
    code: str,
    calendar: Optional[Iterable[pd.Timestamp]] = None,
) -> dict:
    """执行方案 §4.5 中可由单票数据判定的校验（#1–#7、#9、#10）。"""
    checks: list[QualityCheck] = []

    duplicates = int(df.index.duplicated().sum())
    checks.append(QualityCheck(1, "unique_code_date", duplicates == 0, duplicates, "重复交易日"))

    ohlc = df[["open", "high", "low", "close"]]
    bad_ohlc = ~(
        (ohlc > 0).all(axis=1)
        & (df["low"] <= df["open"])
        & (df["open"] <= df["high"])
        & (df["low"] <= df["close"])
        & (df["close"] <= df["high"])
    )
    checks.append(QualityCheck(2, "ohlc_bounds", int(bad_ohlc.sum()) == 0, int(bad_ohlc.sum()), "OHLC 越界"))

    negative = (df["volume"] < 0) | (df["amount"] < 0)
    zero_mismatch = ((df["volume"] == 0) & (df["amount"] > 0)) | ((df["amount"] == 0) & (df["volume"] > 0))
    bad_volume = negative | zero_mismatch
    checks.append(QualityCheck(3, "volume_amount_non_negative", int(bad_volume.sum()) == 0,
                               int(bad_volume.sum()), "成交量/额异常"))

    if calendar is not None:
        expected = set(pd.to_datetime(list(calendar)))
        actual = set(df.index)
        missing = expected - actual
        checks.append(QualityCheck(4, "calendar_complete", not missing, len(missing), "交易日缺口"))
    else:
        checks.append(QualityCheck(4, "calendar_complete", True, 0, "未提供交易日历，跳过"))

    factor = pd.to_numeric(df.get("adj_factor", 1.0), errors="coerce")
    factor_ratio = (factor / factor.shift(1)).replace([np.inf, -np.inf], np.nan)
    # 后复权因子应单调不降；正向跳变通常来自分红送转，需公告核对但不直接判失败。
    suspicious = (factor <= 0) | (factor_ratio < 0.80)
    jumps = (factor_ratio - 1).abs() > 0.20
    checks.append(
        QualityCheck(
            5,
            "adj_factor_monotonic",
            int(suspicious.sum()) == 0,
            int(suspicious.sum()),
            f"{int(jumps.sum())} 次 >20% 正向跳变待公告核对",
        )
    )

    has_status = "tradestatus" in df.columns
    checks.append(QualityCheck(6, "tradestatus_present", has_status,
                               0 if has_status else len(df), "缺少 tradestatus"))

    if "limit_up" in df.columns and "limit_down" in df.columns:
        tol = 0.0051
        upper = df["limit_up"].notna()
        lower = df["limit_down"].notna()
        bad_limit = (upper & (df["high"] > df["limit_up"] + tol)) | (lower & (df["low"] < df["limit_down"] - tol))
        checks.append(QualityCheck(7, "limit_consistent", int(bad_limit.sum()) == 0, int(bad_limit.sum()),
                                   "价格越过涨跌停价"))
    else:
        checks.append(QualityCheck(7, "limit_consistent", True, 0, "未计算涨跌停价，跳过"))

    # #9 无未来信息：索引严格递增且由数据源日期构成，信号层再做逐行断言
    monotonic = df.index.is_monotonic_increasing
    checks.append(QualityCheck(9, "no_future_rows", bool(monotonic), 0 if monotonic else 1, "索引未按日递增"))

    checks.append(QualityCheck(10, "snapshot_hash", True, 0, snapshot_hash({code: df})))

    return {
        "code": code,
        "rows": int(len(df)),
        "start": str(df.index.min().date()) if len(df) else None,
        "end": str(df.index.max().date()) if len(df) else None,
        "passed": all(check.passed for check in checks),
        "checks": [check.to_dict() for check in checks],
    }


def write_quality_report(reports: list[dict], path: Any) -> None:
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
