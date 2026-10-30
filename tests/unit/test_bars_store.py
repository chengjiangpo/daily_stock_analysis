# -*- coding: utf-8 -*-
"""行情落库（I-4）测试：幂等写入、读回同构、窗口过滤、重抓收敛。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.data.bars_store import (
    ingest_cache,
    load_frames_from_db,
    mask_url,
    resolve_url,
    table_counts,
)


def test_mask_url_hides_password() -> None:
    masked = mask_url("postgresql+psycopg2://fund:secret%40pass@localhost:5432/fund")
    assert masked == "postgresql+psycopg2://fund:***@localhost:5432/fund"
    assert "secret" not in masked
    # 无凭据/无密码的 URL 原样返回
    assert mask_url("sqlite:///data/swing_bars.db") == "sqlite:///data/swing_bars.db"
    assert mask_url("postgresql://localhost:5432/fund") == "postgresql://localhost:5432/fund"


def test_resolve_url_normalizes_postgres_driver() -> None:
    resolved = resolve_url("postgres://fund:pw@localhost:5432/fund")
    assert resolved.startswith("postgresql+psycopg")


def _write_cache(root: Path, code: str, days: int = 5, rows: int | None = None) -> None:
    """造一份最小可用缓存（CSV + meta），复刻 baostock_bars._save_cache 结构。"""
    index = pd.bdate_range("2024-01-02", periods=days)
    frame = pd.DataFrame(
        {
            "date": index.strftime("%Y-%m-%d"),
            "code": code,
            "open": [10.0 + i * 0.1 for i in range(days)],
            "high": [10.2 + i * 0.1 for i in range(days)],
            "low": [9.9 + i * 0.1 for i in range(days)],
            "close": [10.1 + i * 0.1 for i in range(days)],
            "pre_close": [10.0 + i * 0.1 for i in range(days)],
            "volume": [1_000_000 + i for i in range(days)],
            "amount": [10_000_000.0 + i for i in range(days)],
            "adjustflag": [3] * days,
            "turn": [0.5] * days,
            "tradestatus": [1] * days,
            "pct_chg": [1.0] * days,
            "is_st": [False] * days,
            "hfq_close": [100.0 + i for i in range(days)],
            "adj_factor": [9.9] * days,
        }
    )
    (root / "raw").mkdir(parents=True, exist_ok=True)
    (root / "meta").mkdir(parents=True, exist_ok=True)
    frame.to_csv(root / "raw" / f"{code}.csv.gz", index=False)
    meta = {
        "code": code,
        "requested_start": "2016-01-01",
        "requested_end": "2024-12-31",
        "first_date": str(index.min().date()),
        "last_date": str(index.max().date()),
        "rows": int(rows if rows is not None else len(frame)),
        "fetched_at": "2026-10-30T13:00:00",
    }
    (root / "meta" / f"{code}.json").write_text(json.dumps(meta), encoding="utf-8")


@pytest.fixture()
def cache_dir(tmp_path: Path) -> Path:
    root = tmp_path / "cache"
    for code in ("000001", "600000", "300750"):
        _write_cache(root, code)
    return root


@pytest.fixture()
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'swing_bars.db'}"


def test_ingest_writes_bars_and_meta(cache_dir: Path, db_url: str) -> None:
    result = ingest_cache(cache_dir, url=db_url)
    assert result.codes_seen == 3
    assert result.codes_written == 3
    assert result.rows_written == 15
    assert table_counts(db_url) == {"bars": 15, "codes": 3}

    frames = load_frames_from_db(db_url)
    assert set(frames) == {"000001", "300750", "600000"}
    frame = frames["000001"]
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.index.name == "date"
    assert len(frame) == 5
    assert {"open", "high", "low", "close", "pre_close", "volume", "amount", "hfq_close", "adj_factor"} <= set(
        frame.columns
    )
    assert frame["close"].iloc[0] == pytest.approx(10.1)
    assert frame["hfq_close"].iloc[-1] == pytest.approx(104.0)
    assert frame["is_st"].dtype == bool
    assert frame["tradestatus"].dtype.kind in "iu"


def test_ingest_is_idempotent(cache_dir: Path, db_url: str) -> None:
    ingest_cache(cache_dir, url=db_url)
    second = ingest_cache(cache_dir, url=db_url)
    assert second.codes_written == 0
    assert second.codes_skipped == 3
    assert second.rows_written == 0
    assert table_counts(db_url) == {"bars": 15, "codes": 3}


def test_refresh_rewrites_without_duplicating(cache_dir: Path, db_url: str) -> None:
    ingest_cache(cache_dir, url=db_url)
    third = ingest_cache(cache_dir, url=db_url, refresh=True)
    assert third.codes_written == 3
    assert table_counts(db_url) == {"bars": 15, "codes": 3}


def test_refetch_shrinking_history_does_not_leave_stale_rows(cache_dir: Path, db_url: str) -> None:
    """重抓后行数变少时，旧行必须被清掉（先删后插）。"""
    ingest_cache(cache_dir, url=db_url)
    _write_cache(cache_dir, "000001", days=3)
    ingest_cache(cache_dir, url=db_url, codes=["000001"], refresh=True)
    frames = load_frames_from_db(db_url, codes=["000001"])
    assert len(frames["000001"]) == 3
    assert table_counts(db_url)["bars"] == 13


def test_window_filter_and_limit(cache_dir: Path, db_url: str) -> None:
    ingest_cache(cache_dir, url=db_url)
    frames = load_frames_from_db(db_url, start="2024-01-04", end="2024-01-05")
    assert len(frames) == 3
    # 2024-01-02/03 起头，5 个交易日 → 只保留 01-04、01-05
    assert len(frames["600000"]) == 2
    assert frames["600000"].index.strftime("%Y-%m-%d").tolist() == ["2024-01-04", "2024-01-05"]

    limited = load_frames_from_db(db_url, limit_codes=2)
    assert len(limited) == 2


def test_dry_run_reports_scale_without_writing(cache_dir: Path, db_url: str) -> None:
    result = ingest_cache(cache_dir, url=db_url, dry_run=True)
    assert result.rows_written == 15
    assert result.codes_written == 0
    assert not Path(db_url.replace("sqlite:///", "")).exists()


def test_meta_window_mismatch_is_skipped(tmp_path: Path, db_url: str) -> None:
    """缓存只覆盖 2024 年后半段时，请求 2016 起的历史窗口应判为无数据。"""
    root = tmp_path / "cache"
    _write_cache(root, "000001")
    meta_path = root / "meta" / "000001.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["requested_start"] = "2024-07-01"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    result = ingest_cache(root, url=db_url, start="2016-01-01")
    assert result.codes_written == 0
    assert result.codes_without_data == ["000001"]


def test_loader_prefers_db_when_bars_url_given(cache_dir: Path, db_url: str) -> None:
    from src.data.loader import load_frames

    ingest_cache(cache_dir, url=db_url)
    frames = load_frames("2024-01-01", "2024-12-31", cache_dir=str(cache_dir), bars_url=db_url)
    assert set(frames) == {"000001", "300750", "600000"}
    assert len(frames["000001"]) == 5


def test_adj_source_tushare_prefers_aligned_factor(cache_dir: Path, db_url: str) -> None:
    """重建因子优先：adj_factor_aligned 存在时用于 hfq_close=close*aligned。"""
    from sqlalchemy import update

    from src.data.bars_store import Session, SwingBarDaily, make_engine

    ingest_cache(cache_dir, url=db_url)
    engine = make_engine(db_url)
    with Session(engine) as session:
        # 人为把 2024-01-02 那天的因子放大 1.2 倍，模拟「重建后修正」
        session.execute(
            update(SwingBarDaily)
            .where(SwingBarDaily.code == "000001", SwingBarDaily.trade_date == pd.Timestamp("2024-01-02").date())
            .values(adj_factor_aligned=9.9 * 1.2, adj_factor_ts=1.0)
        )
        session.commit()

    base = load_frames_from_db(db_url, codes=["000001"])
    rebuilt = load_frames_from_db(db_url, codes=["000001"], adj_source="tushare")
    assert base["000001"]["adj_factor"].iloc[0] == pytest.approx(9.9)
    assert rebuilt["000001"]["adj_factor"].iloc[0] == pytest.approx(9.9 * 1.2)
    assert rebuilt["000001"]["hfq_close"].iloc[0] == pytest.approx(rebuilt["000001"]["close"].iloc[0] * 9.9 * 1.2)
    # 未回填的行回退原始因子
    assert rebuilt["000001"]["adj_factor"].iloc[1] == pytest.approx(9.9)


def test_adj_source_rejects_unknown(cache_dir: Path, db_url: str) -> None:
    with pytest.raises(ValueError):
        load_frames_from_db(db_url, adj_source="wind")
