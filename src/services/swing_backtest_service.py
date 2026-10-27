# -*- coding: utf-8 -*-
"""波段回测运行库与异步执行服务。

- 只读扫描 ``reports/backtest/*`` 与 ``reports/mvp``，供 Web/API 查询；
- 触发运行时只使用本地 BaoStock 缓存，不发起网络抓取；
- 同一进程内串行执行，避免多个回测同时抢占 CPU/内存。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from src.data.loader import load_cached_frames
from src.services.backtest_run_service import run_backtest
from src.sim.rules import PROJECT_ROOT
from src.strategy import STRATEGY_CLASSES

logger = logging.getLogger(__name__)

REPORTS_ROOT = PROJECT_ROOT / "reports"
RUNS_ROOT = REPORTS_ROOT / "backtest"
MVP_RUN_DIR = REPORTS_ROOT / "mvp"
DEFAULT_CACHE_DIR = Path(os.getenv("BACKTEST_CACHE_DIR", PROJECT_ROOT / "data/cache/swing_bars_full"))

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
ALLOWED_FILE_SUFFIXES = {".csv", ".json", ".md"}
MAX_TRADES = 500
MAX_EQUITY_POINTS = 1200


class SwingBacktestError(Exception):
    """Base error for swing backtest API/service."""


class RunNotFoundError(SwingBacktestError):
    pass


class CacheUnavailableError(SwingBacktestError):
    pass


class RunBusyError(SwingBacktestError):
    pass


class InvalidRunRequestError(SwingBacktestError):
    pass


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.match(run_id or ""):
        raise RunNotFoundError(f"非法运行 ID: {run_id!r}")
    return run_id


def _run_dir(run_id: str) -> Path:
    validate_run_id(run_id)
    if run_id == "mvp":
        target = MVP_RUN_DIR
    else:
        target = RUNS_ROOT / run_id
    if not target.is_dir():
        raise RunNotFoundError(f"运行不存在: {run_id}")
    return target


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except (pd.errors.ParserError, OSError):
        return pd.DataFrame()


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _summary_from_dir(run_id: str, run_dir: Path) -> dict:
    metadata = _read_json(run_dir / "run_metadata.json")
    metrics = _read_json(run_dir / "performance_metrics.json")
    performance = metrics.get("performance", {}) or {}
    trade_stats = metrics.get("trades", {}) or {}
    signal_summary = metrics.get("summary", {}) or {}

    mode = metadata.get("mode") or ("signal" if signal_summary.get("mean_excess") is not None else "portfolio")
    period = metadata.get("period") or []
    created_at = metadata.get("created_at")
    if not created_at:
        mtime = max((path.stat().st_mtime for path in run_dir.glob("*")), default=0)
        created_at = datetime.fromtimestamp(mtime).isoformat() if mtime else None

    strategy = metadata.get("strategy") or "trend_pullback_restart"
    title = "MVP 可行性切片" if run_id == "mvp" else f"{strategy} · {run_id}"
    files = sorted(path.name for path in run_dir.iterdir() if path.is_file())
    decision = metrics.get("decision")
    if decision is None:
        checks = metrics.get("checks") or {}
        if checks:
            decision = "PASS" if all(checks.values()) else "STOP"

    return {
        "run_id": run_id,
        "title": title,
        "status": "completed",
        "strategy": strategy,
        "mode": mode,
        "cost_tier": metadata.get("cost_tier"),
        "start": str(period[0]) if len(period) > 0 else None,
        "end": str(period[1]) if len(period) > 1 else None,
        "created_at": created_at,
        "n_trades": _int_or_zero(signal_summary.get("n_trades", trade_stats.get("n_trades", 0))),
        "total_return": _float_or_none(performance.get("total_return")),
        "cagr": _float_or_none(performance.get("cagr")),
        "sharpe": _float_or_none(performance.get("sharpe")),
        "max_drawdown": _float_or_none(performance.get("max_drawdown")),
        "win_rate": _float_or_none(trade_stats.get("win_rate")),
        "profit_factor": _float_or_none(trade_stats.get("profit_factor")),
        "mean_excess": _float_or_none(signal_summary.get("mean_excess")),
        "ci_low": _float_or_none(signal_summary.get("ci_low")),
        "ci_high": _float_or_none(signal_summary.get("ci_high")),
        "net_expectancy": _float_or_none(signal_summary.get("net_expectancy")),
        "decision": decision,
        "decision_reason": metrics.get("decision_reason"),
        "codes": _int_or_zero(metadata.get("codes", (metadata.get("engine") or {}).get("codes", 0))),
        "files": files,
    }


def list_runs() -> List[dict]:
    items: List[dict] = []
    if RUNS_ROOT.is_dir():
        for child in RUNS_ROOT.iterdir():
            if child.is_dir() and (child / "performance_metrics.json").is_file():
                try:
                    items.append(_summary_from_dir(child.name, child))
                except Exception as error:  # noqa: BLE001
                    logger.warning("跳过回测运行 %s: %s", child, error)
    if MVP_RUN_DIR.is_dir() and (MVP_RUN_DIR / "performance_metrics.json").is_file():
        try:
            items.append(_summary_from_dir("mvp", MVP_RUN_DIR))
        except Exception as error:  # noqa: BLE001
            logger.warning("跳过 MVP 运行: %s", error)

    with _STATUS_LOCK:
        running = [dict(item) for item in _STATUS.values() if item.get("status") == "running"]

    persisted_ids = {item["run_id"] for item in items}
    items.extend(item for item in running if item["run_id"] not in persisted_ids)
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items


def get_run_summary(run_id: str) -> dict:
    validate_run_id(run_id)
    with _STATUS_LOCK:
        status = _STATUS.get(run_id)
    if status and status.get("status") in ("running", "failed"):
        result = {
            "run_id": run_id,
            "title": status.get("title", run_id),
            "status": status["status"],
            "strategy": status.get("strategy"),
            "mode": status.get("mode"),
            "cost_tier": status.get("cost_tier"),
            "start": status.get("start"),
            "end": status.get("end"),
            "created_at": status.get("created_at"),
            "n_trades": 0,
            "decision": None,
            "decision_reason": status.get("error"),
            "files": [],
        }
        return result
    run_dir = _run_dir(run_id)
    return _summary_from_dir(run_id, run_dir)


def _downsample(df: pd.DataFrame, limit: int = MAX_EQUITY_POINTS) -> pd.DataFrame:
    if len(df) <= limit:
        return df
    step = max(1, math.ceil(len(df) / limit))
    sampled = df.iloc[::step]
    if sampled.index[-1] != df.index[-1]:
        sampled = pd.concat([sampled, df.iloc[[-1]]])
    return sampled


def get_run_detail(run_id: str, trade_limit: int = 200) -> dict:
    run_dir = _run_dir(run_id)
    summary = _summary_from_dir(run_id, run_dir)
    summary["status"] = "completed"

    equity = _read_csv(run_dir / "daily_equity.csv")
    equity_items = []
    if not equity.empty:
        equity = _downsample(equity)
        for _, row in equity.iterrows():
            equity_items.append(
                {
                    "date": str(row.get("trade_date", ""))[:10],
                    "equity": _float_or_none(row.get("equity")),
                    "drawdown": _float_or_none(row.get("drawdown")),
                    "cash": _float_or_none(row.get("cash")),
                    "market_value": _float_or_none(row.get("market_value")),
                }
            )

    trades = _read_csv(run_dir / "trades.csv")
    trade_items = []
    if not trades.empty:
        if "entry_date" in trades.columns:
            trades = trades.sort_values("entry_date", ascending=False)
        for _, row in trades.head(max(1, min(trade_limit, MAX_TRADES))).iterrows():
            trade_items.append(
                {
                    "code": str(row.get("code") or ""),
                    "signal_date": str(row.get("signal_date", ""))[:10] or None,
                    "entry_date": str(row.get("entry_date", ""))[:10] or None,
                    "exit_date": str(row.get("exit_date", ""))[:10] or None,
                    "entry_price": _float_or_none(row.get("entry_price")),
                    "exit_price": _float_or_none(row.get("exit_price")),
                    "gross_return": _float_or_none(row.get("gross_return")),
                    "bench_return": _float_or_none(row.get("bench_return")),
                    "excess_return": _float_or_none(row.get("excess_return")),
                    "net_return": _float_or_none(row.get("net_return")),
                    "pnl": _float_or_none(row.get("pnl")),
                    "r_multiple": _float_or_none(row.get("r_multiple")),
                    "exit_reason": row.get("exit_reason"),
                    "hold_days": _int_or_zero(row.get("hold_days")),
                }
            )

    rejections: dict[str, int] = {}
    orders = _read_csv(run_dir / "orders.csv")
    if not orders.empty and "status" in orders.columns:
        rejected = orders[orders["status"] == "rejected"]
        if "reject_reason" in rejected.columns:
            rejections = {
                str(key): int(value)
                for key, value in rejected["reject_reason"].fillna("unknown").value_counts().head(10).items()
            }

    diagnostics = []
    diag_df = _read_csv(run_dir / "diagnostics.csv")
    if not diag_df.empty and "condition_name" in diag_df.columns and "passed" in diag_df.columns:
        grouped = diag_df.groupby("condition_name")["passed"].agg(["count", "sum"])
        grouped["failed"] = grouped["count"] - grouped["sum"]
        grouped["pass_rate"] = grouped["sum"] / grouped["count"]
        top = grouped.sort_values("failed", ascending=False).head(12)
        diagnostics = [
            {
                "condition_name": str(name),
                "total": int(row["count"]),
                "failed": int(row["failed"]),
                "pass_rate": _float_or_none(row["pass_rate"]),
            }
            for name, row in top.iterrows()
        ]

    files = []
    for path in sorted(run_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in ALLOWED_FILE_SUFFIXES:
            files.append({"name": path.name, "size": path.stat().st_size})

    summary_markdown = ""
    summary_path = run_dir / "summary.md"
    if summary_path.is_file():
        summary_markdown = summary_path.read_text(encoding="utf-8")[:50000]

    summary.update(
        {
            "equity": equity_items,
            "trades": trade_items,
            "rejection_reasons": rejections,
            "diagnostics": diagnostics,
            "files": files,
            "summary_markdown": summary_markdown,
        }
    )
    return summary


def get_report_file(run_id: str, filename: str) -> Path:
    if not filename or filename != Path(filename).name or ".." in filename:
        raise RunNotFoundError("非法文件名")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_FILE_SUFFIXES:
        raise RunNotFoundError("不允许下载该文件类型")
    run_dir = _run_dir(run_id).resolve()
    target = (run_dir / filename).resolve()
    if target.parent != run_dir or not target.is_file():
        raise RunNotFoundError("文件不存在")
    return target


# ---------------- 异步运行 ----------------

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="swing_backtest")
_STATUS: Dict[str, dict] = {}
_STATUS_LOCK = threading.Lock()


def _set_status(run_id: str, **fields: Any) -> None:
    with _STATUS_LOCK:
        current = _STATUS.setdefault(run_id, {})
        current["run_id"] = run_id
        current.update(fields)
        current["updated_at"] = datetime.now().isoformat()
        if len(_STATUS) > 30:
            finished = [key for key, item in _STATUS.items() if item.get("status") != "running"]
            for key in finished[: max(0, len(_STATUS) - 30)]:
                _STATUS.pop(key, None)


def get_run_status(run_id: str) -> dict:
    validate_run_id(run_id)
    with _STATUS_LOCK:
        if run_id in _STATUS:
            return dict(_STATUS[run_id])
    summary = get_run_summary(run_id)
    return {
        "run_id": run_id,
        "status": summary.get("status", "completed"),
        "message": "已完成",
        "error": None,
        "created_at": summary.get("created_at"),
        "finished_at": summary.get("created_at"),
    }


def submit_run(
    strategy: str,
    start: str,
    end: str,
    cost_tier: str,
    mode: str,
    max_codes: int,
    cache_dir: Optional[str] = None,
    aum: Optional[float] = None,
) -> dict:
    if strategy not in STRATEGY_CLASSES:
        raise InvalidRunRequestError(f"未知策略: {strategy}")
    if cost_tier not in ("base", "higher", "stress"):
        raise InvalidRunRequestError(f"未知成本档: {cost_tier}")
    if mode not in ("signal", "portfolio"):
        raise InvalidRunRequestError(f"未知评估模式: {mode}")
    try:
        start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as error:
        raise InvalidRunRequestError("日期格式必须为 YYYY-MM-DD") from error
    if start_date >= end_date:
        raise InvalidRunRequestError("开始日期必须早于结束日期")
    if not 1 <= max_codes <= 800:
        raise InvalidRunRequestError("max_codes 必须在 1..800 之间")

    cache_root = Path(cache_dir or os.getenv("BACKTEST_CACHE_DIR", DEFAULT_CACHE_DIR))
    if not (cache_root / "raw").is_dir():
        raise CacheUnavailableError(
            f"本地行情缓存不存在: {cache_root}。请先运行 scripts/fetch_swing_bars.py 或 CLI 回测。"
        )

    run_id = f"swing-{datetime.now():%Y%m%d-%H%M%S}"
    created_at = datetime.now().isoformat()
    with _STATUS_LOCK:
        if any(item.get("status") == "running" for item in _STATUS.values()):
            raise RunBusyError("已有回测在运行，请稍后重试")
        # 原子占位，避免并发请求同时通过上面的检查。
        _STATUS[run_id] = {
            "run_id": run_id,
            "title": f"{strategy} · {run_id}",
            "status": "running",
            "message": "准备回测",
            "error": None,
            "strategy": strategy,
            "mode": mode,
            "cost_tier": cost_tier,
            "start": str(start),
            "end": str(end),
            "created_at": created_at,
            "finished_at": None,
            "updated_at": created_at,
        }
    _EXECUTOR.submit(
        _execute_run,
        run_id,
        strategy,
        start_date,
        end_date,
        cost_tier,
        mode,
        int(max_codes),
        cache_root,
        aum,
    )
    return get_run_status(run_id)


def _execute_run(
    run_id: str,
    strategy: str,
    start: date,
    end: date,
    cost_tier: str,
    mode: str,
    max_codes: int,
    cache_root: Path,
    aum: Optional[float],
) -> None:
    try:
        _set_status(run_id, message="读取本地行情缓存")
        frames = load_cached_frames(cache_root, str(start), str(end), max_codes=max_codes)
        if not frames:
            raise CacheUnavailableError("缓存中没有可用行情，请检查区间或重新抓取")
        _set_status(run_id, message=f"回测中：{len(frames)} 只股票")
        run_backtest(
            strategy,
            frames,
            start,
            end,
            cost_tier=cost_tier,
            aum=aum,
            out_dir=RUNS_ROOT / run_id,
            constrained=(mode == "portfolio"),
            run_id=run_id,
        )
        _set_status(
            run_id,
            status="completed",
            message="回测完成",
            finished_at=datetime.now().isoformat(),
        )
    except Exception as error:  # noqa: BLE001
        logger.error("波段回测运行失败 %s: %s", run_id, error, exc_info=True)
        _set_status(
            run_id,
            status="failed",
            message="回测失败",
            error=str(error),
            finished_at=datetime.now().isoformat(),
        )
