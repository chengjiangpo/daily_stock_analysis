# -*- coding: utf-8 -*-
"""波段回测 API 契约测试（最小 FastAPI app，不加载其它端点）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.endpoints.swing_backtest import router
from src.services import swing_backtest_service as service


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "run1"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "run_metadata.json",
        {
            "run_id": "run1",
            "strategy": "trend_pullback_restart",
            "cost_tier": "base",
            "period": ["2016-01-01", "2024-12-31"],
            "created_at": "2026-10-26T23:00:00",
            "mode": "signal",
        },
    )
    _write_json(
        run_dir / "performance_metrics.json",
        {
            "performance": {"total_return": -0.1, "cagr": -0.01, "sharpe": -0.5, "max_drawdown": 0.2},
            "trades": {"n_trades": 3, "win_rate": 0.33, "profit_factor": 0.5},
            "summary": {"n_trades": 3, "mean_excess": -0.002, "ci_low": -0.005, "ci_high": 0.001},
            "decision": "STOP",
        },
    )
    (run_dir / "daily_equity.csv").write_text(
        "trade_date,cash,market_value,equity,drawdown\n2024-01-02,100,0,100,0\n",
        encoding="utf-8",
    )
    (run_dir / "trades.csv").write_text(
        "code,signal_date,entry_date,exit_date,entry_price,exit_price,gross_return,exit_reason,hold_days\n"
        "600000,2024-01-01,2024-01-02,2024-01-05,10,11,0.1,take_profit,3\n",
        encoding="utf-8",
    )
    (run_dir / "summary.md").write_text("# 报告\n", encoding="utf-8")

    monkeypatch.setattr(service, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(service, "MVP_RUN_DIR", tmp_path / "mvp")
    monkeypatch.setattr(service, "DEFAULT_CACHE_DIR", tmp_path / "empty_cache")
    monkeypatch.delenv("BACKTEST_CACHE_DIR", raising=False)
    with service._STATUS_LOCK:
        service._STATUS.clear()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/swing-backtest")
    yield TestClient(app)
    with service._STATUS_LOCK:
        service._STATUS.clear()


def test_list_runs(client: TestClient) -> None:
    response = client.get("/api/v1/swing-backtest/runs")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["run_id"] == "run1"
    assert body["items"][0]["decision"] == "STOP"


def test_get_detail_and_status(client: TestClient) -> None:
    detail = client.get("/api/v1/swing-backtest/runs/run1")
    assert detail.status_code == 200
    body = detail.json()
    assert body["n_trades"] == 3
    assert len(body["equity"]) == 1
    assert body["trades"][0]["code"] == "600000"
    assert body["summary_markdown"].startswith("# 报告")

    status = client.get("/api/v1/swing-backtest/runs/run1/status")
    assert status.status_code == 200
    assert status.json()["status"] == "completed"


def test_download_and_traversal(client: TestClient) -> None:
    response = client.get("/api/v1/swing-backtest/runs/run1/files/summary.md")
    assert response.status_code == 200
    assert response.text.startswith("# 报告")

    blocked = client.get("/api/v1/swing-backtest/runs/run1/files/..%2Frun_metadata.json")
    assert blocked.status_code == 404

    missing = client.get("/api/v1/swing-backtest/runs/nope")
    assert missing.status_code == 404


def test_create_run_validation(client: TestClient, tmp_path: Path) -> None:
    invalid = client.post(
        "/api/v1/swing-backtest/runs",
        json={"strategy": "unknown", "start": "2023-01-01", "end": "2023-12-31",
              "cost_tier": "base", "mode": "signal", "max_codes": 10},
    )
    assert invalid.status_code == 400

    no_cache = client.post(
        "/api/v1/swing-backtest/runs",
        json={"strategy": "trend_pullback_restart", "start": "2023-01-01", "end": "2023-12-31",
              "cost_tier": "base", "mode": "signal", "max_codes": 10},
    )
    assert no_cache.status_code == 409
