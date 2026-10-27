# -*- coding: utf-8 -*-
"""BaoStock 原始+后复权日线适配（方案 §4.1/§4.4）。

说明：回测子系统直接调用 baostock，避免导入 data_provider 包时连带初始化
其它可选数据源（efinance/pytdx/yfinance/tickflow 等）。行情口径与字段仍在
本模块统一为回测约定，不复用 get_daily_data() 的前复权输出。
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Callable, Generator, Iterable, Optional

import pandas as pd

from src.sim.rules import Board, classify_board

from .bars import normalize_daily_bars

logger = logging.getLogger(__name__)

FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"


def _login(bs: object) -> object:
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败: {login.error_msg}")
    return bs


def _relogin(bs: object) -> object:
    try:
        bs.logout()
    except Exception:  # noqa: BLE001
        pass
    return _login(bs)


def _is_login_error(error: Exception) -> bool:
    text = str(error)
    return "未登录" in text or "not login" in text.lower() or "login" in text.lower()


@contextmanager
def baostock_session() -> Generator[object, None, None]:
    import baostock as bs

    _login(bs)
    try:
        yield bs
    finally:
        try:
            bs.logout()
        except Exception:  # noqa: BLE001
            pass


def _baostock_code(code: str) -> str:
    normalized = str(code).strip()
    if len(normalized) != 6 or not normalized.isdigit():
        raise ValueError(f"非法 A 股代码: {code!r}")
    if normalized.startswith(("6",)):
        return f"sh.{normalized}"
    return f"sz.{normalized}"


def _query_daily(
    bs: object,
    code: str,
    start: str,
    end: str,
    adjustflag: str,
    fields: str = FIELDS,
) -> pd.DataFrame:
    rs = bs.query_history_k_data_plus(
        code=_baostock_code(code),
        fields=fields,
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag=adjustflag,
    )
    if rs.error_code != "0":
        raise RuntimeError(f"BaoStock 查询失败 {code}: {rs.error_msg}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame(columns=fields.split(","))
    return pd.DataFrame(rows, columns=fields.split(","))


def _cache_path(root: Path, code: str) -> Path:
    return root / "raw" / f"{code}.csv.gz"


def _meta_path(root: Path, code: str) -> Path:
    return root / "meta" / f"{code}.json"


def _load_cached(root: Path, code: str, start: str, end: str) -> Optional[pd.DataFrame]:
    path = _cache_path(root, code)
    meta_file = _meta_path(root, code)
    if not path.exists() or not meta_file.exists():
        return None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if meta.get("requested_start", "") > start or meta.get("requested_end", "") < end:
        return None
    frame = pd.read_csv(path, parse_dates=["date"], index_col="date")
    return frame if not frame.empty else None


def _save_cache(root: Path, code: str, start: str, end: str, frame: pd.DataFrame) -> None:
    path = _cache_path(root, code)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path)
    meta_file = _meta_path(root, code)
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    meta_file.write_text(
        json.dumps(
            {
                "code": code,
                "requested_start": start,
                "requested_end": end,
                "first_date": str(frame.index.min().date()),
                "last_date": str(frame.index.max().date()),
                "rows": int(len(frame)),
                "fetched_at": pd.Timestamp.now().isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def fetch_stock_bars(
    code: str,
    start: str,
    end: str,
    cache_dir: str | Path | None = None,
    refresh: bool = False,
    retries: int = 1,
    session: object | None = None,
) -> pd.DataFrame:
    """返回单票日线：真实价 + adj_factor（后复权/不复权）。"""
    root = Path(cache_dir) if cache_dir else None
    if root is not None and not refresh:
        cached = _load_cached(root, code, start, end)
        if cached is not None:
            return cached

    last_error: Optional[Exception] = None
    raw = hfq = None
    for attempt in range(retries + 1):
        try:
            if session is not None:
                raw = _query_daily(session, code, start, end, "3")
                hfq = _query_daily(session, code, start, end, "1", fields="date,close")
            else:
                with baostock_session() as bs:
                    raw = _query_daily(bs, code, start, end, "3")
                    hfq = _query_daily(bs, code, start, end, "1", fields="date,close")
            break
        except Exception as error:  # noqa: BLE001 - 数据源异常需重试/跳过
            last_error = error
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    if raw is None or hfq is None:
        raise RuntimeError(f"{code} 获取失败: {last_error}")
    if raw.empty:
        return pd.DataFrame()

    numeric_columns = ["open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg"]
    raw = raw.copy()
    raw["date"] = pd.to_datetime(raw["date"])
    for column in numeric_columns:
        raw[column] = pd.to_numeric(raw[column], errors="coerce")
    hfq = hfq.copy()
    hfq["date"] = pd.to_datetime(hfq["date"])
    hfq["close"] = pd.to_numeric(hfq["close"], errors="coerce")
    merged = raw.merge(
        hfq[["date", "close"]].rename(columns={"close": "hfq_close"}),
        on="date",
        how="left",
    )
    merged["adj_factor"] = merged["hfq_close"] / merged["close"].replace(0, pd.NA)
    merged["adj_factor"] = merged["adj_factor"].fillna(1.0)
    frame = normalize_daily_bars(merged, code=code)

    if root is not None:
        _save_cache(root, code, start, end, frame)
    return frame


def fetch_many(
    codes: Iterable[str],
    start: str,
    end: str,
    cache_dir: str | Path | None = None,
    refresh: bool = False,
    progress: Optional[Callable[[int, int, str], None]] = None,
    sleep_seconds: float = 0.0,
) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    code_list = list(codes)
    total = len(code_list)
    with baostock_session() as bs:
        for index, code in enumerate(code_list, start=1):
            frame = pd.DataFrame()
            for attempt in range(2):
                try:
                    frame = fetch_stock_bars(
                        code,
                        start,
                        end,
                        cache_dir=cache_dir,
                        refresh=refresh,
                        session=bs,
                    )
                    break
                except Exception as error:  # noqa: BLE001
                    if attempt == 0 and _is_login_error(error):
                        logger.warning("会话失效，重新登录后重试 %s", code)
                        _relogin(bs)
                        continue
                    logger.warning("跳过 %s: %s", code, error)
                    break
            if not frame.empty:
                frames[code] = frame
            if progress is not None:
                progress(index, total, code)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    return frames


def discover_universe(
    sample_dates: Iterable[date],
    cache_dir: str | Path | None = None,
    refresh: bool = False,
    board: Board = Board.MAIN,
) -> list[str]:
    """用多个历史时点的「当日有交易股票」并集近似历史全市场（含后来退市股）。"""
    root = Path(cache_dir) if cache_dir else None
    cache_file = root / "universe.json" if root else None
    date_keys = [str(day) for day in sample_dates]
    if cache_file is not None and cache_file.exists() and not refresh:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("sample_dates") == date_keys:
            return cached.get("codes", [])

    codes: set[str] = set()
    with baostock_session() as bs:
        for day in date_keys:
            try:
                rs = bs.query_all_stock(day=day)
                if rs.error_code != "0":
                    raise RuntimeError(rs.error_msg)
                while rs.next():
                    row = rs.get_row_data()
                    raw_code = str(row[0]).strip().lower()
                    if raw_code.startswith("sh."):
                        code = raw_code[3:]
                        # 沪市股票：主板 60x / 科创板 688、689；排除 sh.000xxx 指数
                        if code.startswith(("60", "68")):
                            codes.add(code)
                    elif raw_code.startswith("sz."):
                        code = raw_code[3:]
                        # 深市股票：主板 00x / 创业板 30x；排除 sz.399xxx 指数
                        if code.startswith(("00", "30")):
                            codes.add(code)
            except Exception as error:  # noqa: BLE001
                logger.warning("获取 %s 股票列表失败: %s", day, error)

    filtered = []
    for code in sorted(codes):
        try:
            if classify_board(code) is board:
                filtered.append(code)
        except ValueError:
            continue

    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"sample_dates": date_keys, "codes": filtered}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return filtered


def fetch_industry_map(
    cache_dir: str | Path | None = None,
    refresh: bool = False,
) -> dict[str, str]:
    """获取代码 → 行业映射（BaoStock 证监会行业分类，仅当前口径）。"""
    root = Path(cache_dir) if cache_dir else None
    cache_file = root / "industry.json" if root else None
    if cache_file is not None and cache_file.exists() and not refresh:
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    mapping: dict[str, str] = {}
    with baostock_session() as bs:
        rs = bs.query_stock_industry()
        if rs.error_code != "0":
            raise RuntimeError(f"BaoStock query_stock_industry 失败: {rs.error_msg}")
        while rs.next():
            row = rs.get_row_data()
            raw_code = str(row[1]).strip().lower()
            industry = str(row[3]).strip()
            if raw_code.startswith(("sh.", "sz.")) and industry:
                mapping[raw_code[3:]] = industry

    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    return mapping
