#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段 -1 数据源前置实测：BaoStock 退市股/字段/复权/股票池 + TuShare 可用性。

用法::

    .venv/bin/python scripts/probe_data_sources.py --out reports/data_probe

脚本只做只读网络查询，不写入任何 token；所有查询都设置 socket 超时并捕获异常。
输出 ``<out>/data_probe.json``，同时在 stdout 打印摘要。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
    "turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST"
)
REQUIRED_FIELDS = [
    "preclose",
    "isST",
    "tradestatus",
    "turn",
    "pctChg",
    "amount",
    "volume",
]

# 四只不同年份退市的样本；代码来自 BaoStock query_stock_basic 可查范围。
DEFAULT_DELISTED = [
    {"code": "sz.000939", "label": "2020 年退市（凯迪退）"},
    {"code": "sh.600485", "label": "2021 年退市（*ST信威）"},
    {"code": "sh.600093", "label": "2022 年退市（退市易见）"},
    {"code": "sz.300495", "label": "2024 年退市（*ST美尚）"},
]
DEFAULT_NORMAL = "sh.600519"
DEFAULT_ALL_STOCK_DATES = ["2016-12-30", "2019-12-31", "2022-12-30", "2024-12-31"]

A_SHARE_PREFIXES = ("sh.60", "sh.68", "sz.00", "sz.30")
NON_STOCK_INDEX_PREFIXES = ("sz.39",)  # 399xxx 是深证指数，避免误算进股票


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _records(df) -> List[Dict[str, Any]]:  # pandas.DataFrame -> list[dict]
    return json.loads(df.to_json(orient="records", force_ascii=False))


def _is_a_share(code: str) -> bool:
    return code.startswith(A_SHARE_PREFIXES) and not code.startswith(NON_STOCK_INDEX_PREFIXES)


class BaoStockSession:
    """BaoStock 登录/登出与带超时的查询封装。"""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self.bs = None
        self.login_result: Dict[str, Any] = {}
        self.login_history: List[Dict[str, Any]] = []
        self.relogin_count = 0

    def _login(self):
        started = time.time()
        result = self.bs.login()
        self.login_history.append(
            {
                "error_code": result.error_code,
                "error_msg": result.error_msg,
                "elapsed_sec": round(time.time() - started, 3),
            }
        )
        return result

    def __enter__(self) -> "BaoStockSession":
        self._old_timeout = socket.getdefaulttimeout()
        # Baostock 使用阻塞 socket；设置默认超时，避免异常时永久卡住。
        socket.setdefaulttimeout(self.timeout)
        try:
            import baostock as bs  # 延迟导入，便于给出更清晰的错误
        except Exception as exc:  # pragma: no cover - 环境问题
            socket.setdefaulttimeout(self._old_timeout)
            raise RuntimeError(f"无法导入 baostock: {exc}") from exc

        self.bs = bs
        result = self._login()
        self.login_result = self.login_history[-1]
        if result.error_code != "0":
            socket.setdefaulttimeout(self._old_timeout)
            raise RuntimeError(f"BaoStock 登录失败: {result.error_code} {result.error_msg}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self.bs is not None:
                self.bs.logout()
        finally:
            socket.setdefaulttimeout(getattr(self, "_old_timeout", None))

    def query(self, name: str, fn, *args, **kwargs):
        """执行一次查询，返回 (BaostockResult, rows, elapsed)。

        BaoStock 会话偶尔会在长查询中途失效（error_code=10001001），
        或出现 10002007/网络接收错误。这里最多尝试 3 次（含首次），
        每次失败后重登录，不做无限重试。
        """
        last_error: Optional[BaseException] = None
        for attempt in range(3):
            started = time.time()
            try:
                result = fn(*args, **kwargs)
                if result.error_code != "0":
                    raise RuntimeError(f"{result.error_code} {result.error_msg}")
                rows: List[List[str]] = []
                while result.next():
                    rows.append(result.get_row_data())
                if result.error_code != "0":
                    raise RuntimeError(f"{result.error_code} {result.error_msg}")
                return result, rows, round(time.time() - started, 3)
            except Exception as exc:
                last_error = exc
                message = str(exc)
                retryable = (
                    any(
                        token in message
                        for token in (
                            "10001001",
                            "10002007",
                            "未登录",
                            "网络",
                            "接收数据",
                            "timed out",
                            "timeout",
                        )
                    )
                    or isinstance(exc, (OSError, TimeoutError, UnicodeDecodeError))
                )
                if attempt < 2 and retryable:
                    self.relogin_count += 1
                    time.sleep(1.0)
                    try:
                        login = self._login()
                    except Exception as login_exc:
                        last_error = login_exc
                        break
                    if login.error_code != "0":
                        last_error = RuntimeError(
                            f"重登录失败: {login.error_code} {login.error_msg}"
                        )
                        break
                    continue
                break
        raise RuntimeError(f"{name} 查询失败: {last_error}")

    def query_df(self, name: str, fn, *args, **kwargs):
        import pandas as pd

        result, rows, elapsed = self.query(name, fn, *args, **kwargs)
        df = pd.DataFrame(rows, columns=result.fields)
        return df, elapsed


def _history(session: BaoStockSession, code: str, start: str, end: str, adjustflag: str):
    return session.query_df(
        f"history_k_data_plus({code},adj={adjustflag})",
        session.bs.query_history_k_data_plus,
        code,
        FIELDS,
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag=adjustflag,
    )


def _summarize_history(df, fields: Sequence[str]) -> Dict[str, Any]:
    import pandas as pd

    volume = pd.to_numeric(df["volume"], errors="coerce") if len(df) else None
    traded_mask = (
        (df["tradestatus"] == "1") & (volume > 0)
        if len(df)
        else None
    )
    out: Dict[str, Any] = {
        "rows": int(len(df)),
        "fields_returned": list(df.columns),
        "missing_required_fields": [f for f in REQUIRED_FIELDS if f not in df.columns],
        "date_range": [df["date"].iloc[0], df["date"].iloc[-1]] if len(df) else None,
        "tradestatus_1_rows": int((df["tradestatus"] == "1").sum()) if len(df) else 0,
        "tradestatus_0_rows": int((df["tradestatus"] == "0").sum()) if len(df) else 0,
        "traded_days_volume_positive": int(traded_mask.sum()) if len(df) else 0,
        "last3": _records(df.tail(3)) if len(df) else [],
        "last3_traded": _records(df[traded_mask].tail(3)) if len(df) else [],
        "field_non_empty_counts": {
            f: int((df[f] != "").sum()) for f in fields if f in df.columns
        },
        "first_row": _records(df.head(1))[0] if len(df) else None,
    }
    return out


def probe_delisted(session: BaoStockSession, start: str, end: str) -> Dict[str, Any]:
    import pandas as pd

    result, rows, elapsed = session.query(
        "query_stock_basic(all)", session.bs.query_stock_basic
    )
    basic = pd.DataFrame(rows, columns=result.fields)
    type1 = basic[basic["type"] == "1"]
    delisted = type1[type1["status"] == "0"].copy()
    active = type1[type1["status"] == "1"].copy()

    out: Dict[str, Any] = {
        "query_stock_basic": {
            "elapsed_sec": elapsed,
            "rows": int(len(basic)),
            "fields": list(basic.columns),
            "counts_by_type_status": (
                basic.groupby(["type", "status"]).size().unstack(fill_value=0).to_dict()
            ),
            "type1_total": int(len(type1)),
            "type1_active": int(len(active)),
            "type1_delisted": int(len(delisted)),
            "type1_prefix_counts": {
                str(k): int(v) for k, v in type1["code"].str[:5].value_counts().items()
            },
            "type1_delisted_prefix_counts": {
                str(k): int(v) for k, v in delisted["code"].str[:5].value_counts().items()
            },
            "delisted_out_year_counts": {
                str(k): int(v)
                for k, v in delisted["outDate"].str[:4].value_counts().sort_index().items()
            },
            "delisted_sample": _records(delisted.head(5)),
            "delisted_all": _records(delisted),
        },
        "samples": [],
    }

    for item in DEFAULT_DELISTED:
        code = item["code"]
        sample: Dict[str, Any] = {"code": code, "label": item["label"]}
        try:
            # 股票基本资料（含退市日期）
            rs, rows_basic, elapsed_basic = session.query(
                f"query_stock_basic({code})", session.bs.query_stock_basic, code=code
            )
            sample["stock_basic"] = {
                "elapsed_sec": elapsed_basic,
                "fields": list(rs.fields),
                "rows": rows_basic,
            }

            frames = {}
            for adj in ("3", "1"):
                df, elapsed = _history(session, code, start, end, adj)
                frames[adj] = df
                sample[f"adjustflag_{adj}"] = _summarize_history(df, FIELDS.split(","))
                sample[f"adjustflag_{adj}"]["elapsed_sec"] = elapsed

            raw = frames["3"]
            hfq = frames["1"]
            if len(raw) and len(hfq):
                merged = raw[["date", "close", "preclose", "pctChg"]].merge(
                    hfq[["date", "close"]], on="date", suffixes=("_raw", "_hfq")
                )
                merged["close_raw"] = merged["close_raw"].astype(float)
                merged["close_hfq"] = merged["close_hfq"].astype(float)
                merged["ratio"] = merged["close_hfq"] / merged["close_raw"]
                merged["jump"] = merged["ratio"].pct_change()
                sample["hfq_ratio"] = {
                    "first": float(merged["ratio"].iloc[0]),
                    "last": float(merged["ratio"].iloc[-1]),
                    "top_abs_jumps": _records(
                        merged.reindex(merged["jump"].abs().sort_values(ascending=False).index)
                        .head(5)[["date", "close_raw", "close_hfq", "ratio", "jump", "pctChg"]]
                    ),
                }
        except Exception as exc:
            sample["error"] = f"{type(exc).__name__}: {exc}"
            sample["traceback"] = traceback.format_exc(limit=3)
        out["samples"].append(sample)

    # P0-01 字段支持样例：正常股 + 退市股，含 raw/hfq。
    out["field_samples"] = {}
    for code, w_start, w_end in [
        (DEFAULT_NORMAL, "2024-06-17", "2024-06-21"),
        ("sh.600485", "2021-05-25", "2021-06-01"),
    ]:
        item: Dict[str, Any] = {}
        for adj in ("3", "1"):
            try:
                df, elapsed = _history(session, code, w_start, w_end, adj)
                item[f"adjustflag_{adj}"] = {
                    "elapsed_sec": elapsed,
                    "rows": int(len(df)),
                    "fields_returned": list(df.columns),
                    "missing_required_fields": [
                        f for f in REQUIRED_FIELDS if f not in df.columns
                    ],
                    "first_row": _records(df.head(1))[0] if len(df) else None,
                    "last_row": _records(df.tail(1))[0] if len(df) else None,
                }
            except Exception as exc:
                item[f"adjustflag_{adj}"] = {"error": f"{type(exc).__name__}: {exc}"}
        out["field_samples"][code] = item

    return out


def _dividend_windows(session: BaoStockSession, code: str, start: str, end: str):
    raw, raw_elapsed = _history(session, code, start, end, "3")
    hfq, hfq_elapsed = _history(session, code, start, end, "1")
    if not len(raw) or not len(hfq):
        raise RuntimeError(f"{code} 复权对比数据为空")

    merged = raw[["date", "close", "preclose", "pctChg", "tradestatus", "isST"]].merge(
        hfq[["date", "close"]], on="date", suffixes=("_raw", "_hfq")
    )
    import pandas as pd

    for column in ("close_raw", "close_hfq", "preclose", "pctChg"):
        merged[column] = pd.to_numeric(merged[column], errors="coerce")
    # 停牌/退市日的 pctChg/preclose 可能为空；只保留可用于复权比对的交易日。
    merged = merged.dropna(subset=["close_raw", "close_hfq", "preclose", "pctChg"]).copy()
    if not len(merged):
        raise RuntimeError(f"{code} 复权对比无有效交易日")
    merged["ratio"] = merged["close_hfq"] / merged["close_raw"]
    merged["ratio_jump"] = merged["ratio"].pct_change()
    merged["prev_raw_close"] = merged["close_raw"].shift(1)
    merged["implied_cash_per_share"] = merged["prev_raw_close"] - merged["preclose"]
    merged["hfq_return"] = merged["close_hfq"].pct_change()
    merged["raw_total_return_with_cash"] = (
        (merged["close_raw"] + merged["implied_cash_per_share"]) / merged["prev_raw_close"] - 1
    )
    merged["calc_pct"] = (merged["close_raw"] / merged["preclose"] - 1) * 100
    merged["pct_diff_pp"] = merged["calc_pct"] - merged["pctChg"]
    merged["jump_minus_cash_pp"] = (
        merged["ratio_jump"] * 100
        - merged["implied_cash_per_share"] / merged["preclose"] * 100
    )

    jumps = merged[merged["ratio_jump"].abs() > 0.005].copy()
    rows = []
    for _, row in jumps.iterrows():
        rows.append(
            {
                "date": row["date"],
                "prev_raw_close": row["prev_raw_close"],
                "raw_close": row["close_raw"],
                "preclose": row["preclose"],
                "implied_cash_per_share": row["implied_cash_per_share"],
                "ratio_prev": float(merged.loc[merged["date"] < row["date"], "ratio"].iloc[-1])
                if (merged["date"] < row["date"]).any()
                else None,
                "ratio_current": float(row["ratio"]),
                "ratio_jump_pct": float(row["ratio_jump"] * 100),
                "hfq_return_pct": float(row["hfq_return"] * 100),
                "raw_total_return_with_cash_pct": float(row["raw_total_return_with_cash"] * 100),
                "pctChg": float(row["pctChg"]),
            }
        )

    return {
        "code": code,
        "range": [start, end],
        "rows": int(len(merged)),
        "elapsed_sec": {"raw": raw_elapsed, "hfq": hfq_elapsed},
        "ratio_first_last": [float(merged["ratio"].iloc[0]), float(merged["ratio"].iloc[-1])],
        "jumps_gt_0_5pct": rows,
        "max_abs_ratio_jump_pct": float(merged["ratio_jump"].abs().max() * 100),
        "pctchg_vs_close_over_preclose_max_abs_diff_pp": float(
            merged["pct_diff_pp"].abs().max()
        ),
        "ratio_jump_vs_cash_over_preclose_max_abs_diff_pp": float(
            merged["jump_minus_cash_pp"].abs().max()
        ),
        "note": "implied_cash_per_share 由 前收 - 除权参考 preclose 反推；仅对无送转样本等价于每股现金分红。",
    }


def probe_adjustment(session: BaoStockSession, code: str, start: str, end: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"normal_sample": {}, "delisted_sample": {}}
    try:
        out["normal_sample"] = _dividend_windows(session, code, start, end)
    except Exception as exc:
        out["normal_sample"] = {"error": f"{type(exc).__name__}: {exc}"}

    try:
        delisted_code = "sh.600485"
        out["delisted_sample"] = _dividend_windows(
            session, delisted_code, "2016-01-01", "2021-06-01"
        )
    except Exception as exc:
        out["delisted_sample"] = {"error": f"{type(exc).__name__}: {exc}"}

    # 用固定窗口展示除权日前后逐日数值，便于文档引用。
    windows = {}
    for label, code2, w_start, w_end in [
        ("600519_2023_exdiv", code, "2023-06-28", "2023-07-04"),
        ("600519_2024_exdiv", code, "2024-06-17", "2024-06-21"),
    ]:
        try:
            raw, _ = _history(session, code2, w_start, w_end, "3")
            hfq, _ = _history(session, code2, w_start, w_end, "1")
            merged = raw[["date", "close", "preclose", "pctChg"]].merge(
                hfq[["date", "close"]], on="date", suffixes=("_raw", "_hfq")
            )
            merged["ratio"] = merged["close_hfq"].astype(float) / merged["close_raw"].astype(float)
            windows[label] = _records(merged)
        except Exception as exc:
            windows[label] = {"error": f"{type(exc).__name__}: {exc}"}
    out["windows"] = windows
    return out


def probe_all_stock(session: BaoStockSession, days: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"days": [], "union": {}, "delisted_coverage": {}}
    union_codes: set[str] = set()
    for day in days:
        item: Dict[str, Any] = {"day": day}
        try:
            result, rows, elapsed = session.query(
                f"query_all_stock({day})", session.bs.query_all_stock, day=day
            )
            codes = [r[0] for r in rows]
            a_codes = [c for c in codes if _is_a_share(c)]
            statuses = {}
            for r in rows:
                if _is_a_share(r[0]):
                    statuses[r[1]] = statuses.get(r[1], 0) + 1
            item.update(
                {
                    "elapsed_sec": elapsed,
                    "fields": list(result.fields),
                    "total_rows": len(rows),
                    "a_share_rows": len(a_codes),
                    "a_share_unique": len(set(a_codes)),
                    "a_share_tradestatus_counts": statuses,
                    "prefix_counts": _prefix_counts(codes),
                    "contains_samples": {
                        c: c in set(codes)
                        for c in ["sh.600485", "sz.000939", "sh.600093", "sz.300495"]
                    },
                }
            )
            union_codes.update(a_codes)
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
        out["days"].append(item)

    out["union"] = {
        "sample_days": list(days),
        "a_share_union_count": len(union_codes),
        "a_share_union_codes": sorted(union_codes),
        "a_share_union_sample": sorted(union_codes)[:20],
    }
    return out


def _prefix_counts(codes: Iterable[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for code in codes:
        key = code[:5]
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def _coverage_against_delisted(
    all_stock: Dict[str, Any], basic_result: Dict[str, Any]
) -> Dict[str, Any]:
    """用 query_stock_basic 的退市清单校验多时点并集覆盖情况。"""
    delisted = basic_result.get("delisted_all", [])
    union_codes = set(all_stock.get("union", {}).get("a_share_union_codes", []))
    days = all_stock.get("union", {}).get("sample_days", [])
    if not days:
        return {}

    covered = [d for d in delisted if d.get("code") in union_codes]
    missing = [d for d in delisted if d.get("code") not in union_codes]
    first_day, last_day = days[0], days[-1]
    overlap = [d for d in delisted if d.get("outDate", "") >= first_day]
    before = [d for d in delisted if d.get("outDate", "") < first_day]

    def _year_counts(items):
        counts: Dict[str, int] = {}
        for d in items:
            year = (d.get("outDate") or "?")[:4]
            counts[year] = counts.get(year, 0) + 1
        return dict(sorted(counts.items()))

    return {
        "delisted_total": len(delisted),
        "delisted_covered_by_union": len(covered),
        "delisted_missing_from_union": len(missing),
        "delisted_missing_out_year_counts": _year_counts(missing),
        "delisted_missing_sample": missing[:10],
        "delisted_out_before_first_sample": len(before),
        "delisted_out_on_or_after_first_sample": len(overlap),
        "note": "query_all_stock 为时点快照；并集只能覆盖在采样日仍存续的股票。短命股需用 stock_basic 的 ipo/outDate 补齐。",
    }


def probe_tushare() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "token_env_present": bool(
            os.getenv("TUSHARE_TOKEN") or os.getenv("TS_TOKEN") or os.getenv("TUSHARE_TOKEN_ID")
        ),
        "status": "unavailable",
        "error": None,
        "stk_limit": None,
    }
    try:
        import tushare as ts
    except Exception as exc:
        out["error"] = f"无法导入 tushare: {type(exc).__name__}: {exc}"
        return out

    token = os.getenv("TUSHARE_TOKEN") or os.getenv("TS_TOKEN") or os.getenv("TUSHARE_TOKEN_ID")
    try:
        pro = ts.pro_api(token) if token else ts.pro_api()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    if not token:
        out["error"] = "TUSHARE_TOKEN 未设置；tushare.pro_api() 初始化失败，未发起取数请求。"
        return out

    # 有 token 时才按年测试 stk_limit 最早可用年份；不购买、不绕过权限。
    attempts = []
    for year in (2014, 2015, 2016, 2017, 2018):
        start, end = f"{year}0101", f"{year}0131"
        try:
            df = pro.stk_limit(ts_code="600519.SH", start_date=start, end_date=end)
            attempts.append({"year": year, "rows": int(len(df)), "error": None})
            if len(df):
                out["status"] = "available"
                out["earliest_available_year_tested"] = year
                break
        except Exception as exc:
            attempts.append({"year": year, "rows": 0, "error": f"{type(exc).__name__}: {exc}"})
    out["stk_limit"] = attempts
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 -1 数据源前置实测")
    parser.add_argument(
        "--out",
        default="reports/data_probe",
        help="JSON 输出目录（默认 reports/data_probe，已被 .gitignore 覆盖）",
    )
    parser.add_argument("--start", default="2016-01-01", help="退市股日线起始日")
    parser.add_argument(
        "--end", default=date.today().isoformat(), help="退市股日线结束日（默认今天）"
    )
    parser.add_argument(
        "--all-stock-dates",
        default=",".join(DEFAULT_ALL_STOCK_DATES),
        help="query_all_stock 采样日，逗号分隔",
    )
    parser.add_argument("--normal", default=DEFAULT_NORMAL, help="复权对比的正常股票")
    parser.add_argument("--timeout", type=float, default=20.0, help="socket 超时秒数")
    parser.add_argument("--skip-tushare", action="store_true", help="跳过 TuShare 探测")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report: Dict[str, Any] = {
        "generated_at": _now_iso(),
        "cwd": str(Path.cwd()),
        "python": sys.version,
        "params": {
            "start": args.start,
            "end": args.end,
            "normal": args.normal,
            "all_stock_dates": [d.strip() for d in args.all_stock_dates.split(",") if d.strip()],
            "timeout_sec": args.timeout,
        },
        "baostock": {},
        "tushare": None,
        "conclusions": {},
    }

    try:
        with BaoStockSession(timeout=args.timeout) as session:
            report["baostock"]["login"] = session.login_result
            report["baostock"]["login_history"] = session.login_history
            report["baostock"]["relogin_count"] = session.relogin_count

            print("[1/4] P0-01 BaoStock 退市股日线 ...", flush=True)
            delisted = probe_delisted(session, args.start, args.end)
            report["baostock"]["p0_01_delisted"] = delisted
            print(
                "      退市股样本: "
                + ", ".join(
                    f"{s['code']}={s.get('adjustflag_3', {}).get('traded_days_volume_positive', 'ERR')} 交易日"
                    for s in delisted["samples"]
                ),
                flush=True,
            )

            print("[2/4] P0-03 复权口径校准 ...", flush=True)
            report["baostock"]["p0_03_adjustment"] = probe_adjustment(
                session, args.normal, "2022-01-01", "2024-12-31"
            )
            normal = report["baostock"]["p0_03_adjustment"].get("normal_sample", {})
            print(
                f"      {args.normal} 最大复权/原始比值跳变: "
                f"{normal.get('max_abs_ratio_jump_pct')}%",
                flush=True,
            )

            print("[3/4] 补充：退市清单 + query_all_stock 时点池 ...", flush=True)
            dates = [d.strip() for d in args.all_stock_dates.split(",") if d.strip()]
            all_stock = probe_all_stock(session, dates)
            report["baostock"]["support_stock_basic"] = delisted["query_stock_basic"]
            report["baostock"]["support_all_stock"] = all_stock
            report["baostock"]["support_all_stock"]["delisted_coverage"] = (
                _coverage_against_delisted(all_stock, delisted["query_stock_basic"])
            )
            print(
                "      query_all_stock: "
                + ", ".join(
                    f"{d['day']}={d.get('a_share_rows', 'ERR')}"
                    for d in all_stock["days"]
                ),
                flush=True,
            )
            report["baostock"]["login_history"] = session.login_history
            report["baostock"]["relogin_count"] = session.relogin_count
    except Exception as exc:
        report["baostock"]["fatal_error"] = f"{type(exc).__name__}: {exc}"
        report["baostock"]["traceback"] = traceback.format_exc()
        print(f"BaoStock 探测失败: {exc}", file=sys.stderr)

    if not args.skip_tushare:
        print("[4/4] P0-02 TuShare stk_limit 可用性 ...", flush=True)
        report["tushare"] = probe_tushare()
        print(
            f"      TuShare: {report['tushare'].get('status')} / "
            f"error={report['tushare'].get('error')}",
            flush=True,
        )

    report["conclusions"] = _conclusions(report)
    path = out_dir / "data_probe.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON 已写入: {path}", flush=True)
    print("结论摘要:")
    for key, value in report["conclusions"].items():
        print(f"  - {key}: {value}")
    return 0


def _conclusions(report: Dict[str, Any]) -> Dict[str, str]:
    conclusions: Dict[str, str] = {}
    bs = report.get("baostock", {})

    samples = bs.get("p0_01_delisted", {}).get("samples", [])
    ok = 0
    for s in samples:
        if s.get("adjustflag_3", {}).get("traded_days_volume_positive", 0) >= 120 and not s.get(
            "error"
        ):
            ok += 1
    conclusions["P0-01 退市股日线"] = (
        "可用" if samples and ok == len(samples) else f"部分可用（{ok}/{len(samples)}）"
    )

    adjust = bs.get("p0_03_adjustment", {}).get("normal_sample", {})
    max_jump = adjust.get("max_abs_ratio_jump_pct")
    conclusions["P0-03 后复权口径"] = (
        f"后复权含现金分红（实测最大比值跳变 {max_jump}%）"
        if max_jump is not None
        else "未取得有效结果"
    )

    basic = bs.get("support_stock_basic", {})
    conclusions["补充：退市清单"] = (
        f"可用：query_stock_basic 返回 type=1 status=0 共 {basic.get('type1_delisted')} 只，含 outDate"
        if basic.get("type1_delisted") is not None
        else "不可用"
    )

    days = bs.get("support_all_stock", {}).get("days", [])
    conclusions["补充：历史股票池"] = (
        "可用：query_all_stock(day) 返回时点快照，包含后来退市股；"
        "并集需配合 stock_basic 的 ipo/outDate 补齐短命股"
        if days and all("error" not in d for d in days)
        else "部分失败，见 JSON"
    )

    ts = report.get("tushare") or {}
    conclusions["P0-02 TuShare stk_limit"] = (
        "不可用：未配置 TUSHARE_TOKEN" if not ts.get("token_env_present") else f"状态 {ts.get('status')}"
    )
    return conclusions


if __name__ == "__main__":
    raise SystemExit(main())
