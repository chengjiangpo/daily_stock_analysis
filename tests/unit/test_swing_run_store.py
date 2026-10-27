# -*- coding: utf-8 -*-
"""波段回测运行库与异步执行服务测试。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.data.fixtures import build_trend_pullback_frame
from src.services import swing_backtest_service as service


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _make_run(root: Path, run_id: str = "run1") -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "run_metadata.json",
        {
            "run_id": run_id,
            "strategy": "trend_pullback_restart",
            "cost_tier": "base",
            "period": ["2016-01-01", "2024-12-31"],
            "created_at": "2026-10-26T23:00:00",
            "codes": 10,
            "mode": "signal",
        },
    )
    _write_json(
        run_dir / "performance_metrics.json",
        {
            "performance": {"total_return": -0.1, "cagr": -0.01, "sharpe": -0.5, "max_drawdown": 0.2},
            "trades": {"n_trades": 3, "win_rate": 0.33, "profit_factor": 0.5},
            "summary": {"n_trades": 3, "mean_excess": -0.002, "ci_low": -0.005, "ci_high": 0.001,
                        "net_expectancy": -0.006},
            "decision": "STOP",
            "decision_reason": "test",
        },
    )
    (run_dir / "daily_equity.csv").write_text(
        "trade_date,cash,market_value,equity,drawdown\n"
        "2024-01-02,100,0,100,0\n"
        "2024-01-03,100,10,110,0\n",
        encoding="utf-8",
    )
    (run_dir / "trades.csv").write_text(
        "code,signal_date,entry_date,exit_date,entry_price,exit_price,gross_return,bench_return,"
        "excess_return,net_return,pnl,r_multiple,exit_reason,hold_days\n"
        "600000,2024-01-01,2024-01-02,2024-01-05,10,11,0.1,0.02,0.08,0.09,90,1.2,take_profit,3\n"
        "600001,2024-02-01,2024-02-02,2024-02-06,10,9,-0.1,-0.02,-0.08,-0.09,-90,-0.8,hard_stop,4\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "status,reject_reason\nrejected,suspended\nrejected,suspended\nrejected,gap_exceeds_limit\nfilled,\n",
        encoding="utf-8",
    )
    (run_dir / "diagnostics.csv").write_text(
        "condition_name,passed\n"
        "trend.close_above_ma20,1\n"
        "trend.close_above_ma20,0\n"
        "restart.volume,0\n",
        encoding="utf-8",
    )
    (run_dir / "summary.md").write_text("# 回测报告\n", encoding="utf-8")
    return run_dir


@pytest.fixture()
def isolated_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runs_root = tmp_path / "runs"
    mvp_dir = tmp_path / "mvp"
    monkeypatch.setattr(service, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(service, "MVP_RUN_DIR", mvp_dir)
    with service._STATUS_LOCK:
        service._STATUS.clear()
    yield tmp_path
    with service._STATUS_LOCK:
        service._STATUS.clear()


def test_list_and_detail_parse_reports(isolated_runs: Path) -> None:
    _make_run(service.RUNS_ROOT)
    items = service.list_runs()
    assert len(items) == 1
    item = items[0]
    assert item["run_id"] == "run1"
    assert item["n_trades"] == 3
    assert item["decision"] == "STOP"
    assert item["mean_excess"] == pytest.approx(-0.002)

    detail = service.get_run_detail("run1")
    assert len(detail["equity"]) == 2
    assert len(detail["trades"]) == 2
    assert detail["trades"][0]["entry_date"] == "2024-02-02"  # 按 entry_date 倒序
    assert detail["rejection_reasons"]["suspended"] == 2
    diagnostic_names = {item["condition_name"] for item in detail["diagnostics"]}
    assert "trend.close_above_ma20" in diagnostic_names
    assert "restart.volume" in diagnostic_names
    assert {item["name"] for item in detail["files"]} >= {"run_metadata.json", "trades.csv"}
    assert detail["summary_markdown"].startswith("# 回测报告")


def test_get_report_file_blocks_traversal_and_unknown_types(isolated_runs: Path) -> None:
    _make_run(service.RUNS_ROOT)
    assert service.get_report_file("run1", "summary.md").name == "summary.md"
    with pytest.raises(service.RunNotFoundError):
        service.get_report_file("run1", "../run_metadata.json")
    with pytest.raises(service.RunNotFoundError):
        service.get_report_file("run1", "script.py")
    with pytest.raises(service.RunNotFoundError):
        service.get_report_file("run1", "missing.csv")
    with pytest.raises(service.RunNotFoundError):
        service.get_run_detail("../run1")


def test_load_cached_frames_does_not_login(isolated_runs: Path) -> None:
    cache = isolated_runs / "cache"
    raw = cache / "raw"
    meta = cache / "meta"
    raw.mkdir(parents=True)
    meta.mkdir(parents=True)
    frame, _ = build_trend_pullback_frame(code="600000")
    frame.index.name = "date"
    frame.to_csv(raw / "600000.csv.gz")
    _write_json(
        meta / "600000.json",
        {"requested_start": "2016-01-01", "requested_end": "2024-12-31"},
    )
    _write_json(cache / "industry.json", {"600000": "银行"})

    frames = service.load_cached_frames(cache, "2023-01-01", "2023-12-31")
    assert list(frames) == ["600000"]
    assert frames["600000"].index.min() >= frame.index.min()
    assert frames["600000"]["industry"].iloc[0] == "银行"


def test_submit_run_rejects_invalid_and_missing_cache(isolated_runs: Path) -> None:
    with pytest.raises(service.InvalidRunRequestError):
        service.submit_run("unknown", "2023-01-01", "2023-12-31", "base", "signal", 10)
    with pytest.raises(service.InvalidRunRequestError):
        service.submit_run("trend_pullback_restart", "2024-01-01", "2023-01-01", "base", "signal", 10)
    with pytest.raises(service.CacheUnavailableError):
        service.submit_run(
            "trend_pullback_restart", "2023-01-01", "2023-12-31", "base", "signal", 10,
            cache_dir=str(isolated_runs / "empty_cache"),
        )


def test_async_run_completes_and_persists(isolated_runs: Path) -> None:
    cache = isolated_runs / "cache"
    raw = cache / "raw"
    meta = cache / "meta"
    raw.mkdir(parents=True)
    meta.mkdir(parents=True)
    frame, _ = build_trend_pullback_frame(code="600000")
    frame.index.name = "date"
    frame.to_csv(raw / "600000.csv.gz")
    _write_json(meta / "600000.json", {"requested_start": "2016-01-01", "requested_end": "2024-12-31"})

    status = service.submit_run(
        "trend_pullback_restart", "2023-01-01", "2023-12-31", "base", "signal", 1,
        cache_dir=str(cache),
    )
    run_id = status["run_id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        current = service.get_run_status(run_id)
        if current["status"] in ("completed", "failed"):
            break
        time.sleep(0.2)
    assert current["status"] == "completed", current
    assert (service.RUNS_ROOT / run_id / "performance_metrics.json").is_file()
    detail = service.get_run_detail(run_id)
    assert detail["status"] == "completed"
    assert detail["n_trades"] >= 1
