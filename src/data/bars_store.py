# -*- coding: utf-8 -*-
"""行情落库（V3.0 §5.2 I-4）：把 BaoStock 日线缓存装入关系库并支持从库读回。

设计要点：
- 后端无关：默认 SQLite，生产建议 PostgreSQL（与 fund-analysis `stock_eod_daily` 同库便于对账）；
  写路径只依赖 `ON CONFLICT`（PostgreSQL 与 SQLite ≥3.24 均支持）；
- 幂等：同一 `fetched_at` 的代码默认跳过，`--refresh` 强制重写；
- 读路径输出与 CSV 缓存同构的 `Dict[code, DataFrame]`，回测代码零改动。

表：
- `swing_bars_daily`：不复权真实价 OHLC + pre_close/volume/amount/turn/pct_chg/tradestatus/is_st，
  后复权 hfq_close 与 adj_factor（信号层用），adjustflag 保留原始抓取口径；
  主键 (code, trade_date)。
- `swing_bars_fetch_meta`：抓取溯源（requested_start/end、first_date、last_date、rows、fetched_at），
  等价于原 `meta/*.json`，用于判断缓存是否覆盖请求窗口。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, Optional

import pandas as pd
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

logger = logging.getLogger(__name__)

DEFAULT_URL = "sqlite:///data/swing_bars.db"
ENV_URL_KEY = "SWING_BARS_DATABASE_URL"

# 缓存 CSV 的列 → 落库列（缺列时留空）
PRICE_COLUMNS = ("open", "high", "low", "close", "pre_close")
NUMERIC_COLUMNS = ("volume", "amount", "turn", "pct_chg", "hfq_close", "adj_factor")


class Base(DeclarativeBase):
    pass


class SwingBarDaily(Base):
    __tablename__ = "swing_bars_daily"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Optional[float]] = mapped_column(Float)
    high: Mapped[Optional[float]] = mapped_column(Float)
    low: Mapped[Optional[float]] = mapped_column(Float)
    close: Mapped[Optional[float]] = mapped_column(Float)
    pre_close: Mapped[Optional[float]] = mapped_column(Float)
    volume: Mapped[Optional[float]] = mapped_column(Float)
    amount: Mapped[Optional[float]] = mapped_column(Float)
    turn: Mapped[Optional[float]] = mapped_column(Float)
    pct_chg: Mapped[Optional[float]] = mapped_column(Float)
    tradestatus: Mapped[Optional[int]] = mapped_column(Integer)
    is_st: Mapped[Optional[bool]] = mapped_column(Boolean)
    hfq_close: Mapped[Optional[float]] = mapped_column(Float)
    adj_factor: Mapped[Optional[float]] = mapped_column(Float)
    adjustflag: Mapped[Optional[int]] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_swing_bars_daily_date", "trade_date"),
    )


class SwingBarFetchMeta(Base):
    __tablename__ = "swing_bars_fetch_meta"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    requested_start: Mapped[Optional[str]] = mapped_column(String(10))
    requested_end: Mapped[Optional[str]] = mapped_column(String(10))
    first_date: Mapped[Optional[date]] = mapped_column(Date)
    last_date: Mapped[Optional[date]] = mapped_column(Date)
    rows: Mapped[Optional[int]] = mapped_column(Integer)
    fetched_at: Mapped[Optional[str]] = mapped_column(String(32))
    ingested_at: Mapped[Optional[object]] = mapped_column(DateTime(timezone=False))


@dataclass
class IngestResult:
    """一次落库的统计结果。"""

    codes_seen: int = 0
    codes_written: int = 0
    codes_skipped: int = 0
    rows_written: int = 0
    codes_without_data: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "codes_seen": self.codes_seen,
            "codes_written": self.codes_written,
            "codes_skipped": self.codes_skipped,
            "rows_written": self.rows_written,
            "codes_without_data": len(self.codes_without_data),
        }


def resolve_url(url: Optional[str] = None) -> str:
    """解析数据库 URL：显式参数 > 环境变量 > 默认 sqlite 文件。"""
    if url:
        return url
    import os

    return os.environ.get(ENV_URL_KEY) or DEFAULT_URL


def make_engine(url: Optional[str] = None) -> Engine:
    resolved = resolve_url(url)
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if resolved.startswith("sqlite"):
        # SQLite 批量写入需要关闭同名线程检查
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(resolved, **kwargs)
    if engine.dialect.name == "sqlite":
        _apply_sqlite_pragmas(engine)
    return engine


def _apply_sqlite_pragmas(engine: Engine) -> None:
    """SQLite 批量写优化：默认 2MB page cache 在大表上会造成随机页抖动。

    - WAL：读写不互斥，落库期间仍可查询；
    - synchronous=NORMAL：WAL 下仅少一次 fsync，崩溃安全仍由 WAL 保证；
    - cache_size=-262144：256MB page cache（负数=KB）；
    - temp_store=MEMORY：避免临时表落盘。
    """
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA cache_size=-262144",
            "PRAGMA temp_store=MEMORY",
        ):
            cursor.execute(pragma)
        cursor.close()


def ensure_schema(engine: Engine) -> None:
    """建表（幂等）。生产库建议改用迁移脚本，这里保证可离线自举。"""
    Base.metadata.create_all(engine)


def ensure_indexes(engine: Engine) -> None:
    """建二级索引（幂等）。

    批量落库时故意把二级索引建在最后：先写满数据再建索引，10 万行级/批的
    随机页插入不会逐行维护非聚簇索引（实测可把全量落库从 >40 分钟降到十几分钟）。
    """
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            index.create(bind=engine, checkfirst=True)


def drop_secondary_indexes(engine: Engine) -> None:
    """落库前丢掉二级索引（主键/唯一约束索引不动）。"""
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            try:
                index.drop(bind=engine, checkfirst=True)
            except Exception as error:  # noqa: BLE001 - 索引可能尚不存在
                logger.debug("删除索引 %s 失败（忽略）: %s", index.name, error)


def _dialect_insert(engine: Engine, table):
    """返回带 ON CONFLICT 能力的 insert 构造器（PG / SQLite）。"""
    name = engine.dialect.name
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return pg_insert(table)
    if name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert(table)
    return None


@dataclass
class _CodePayload:
    code: str
    rows: list[dict]
    meta: dict


def _frame_rows(code: str, frame: pd.DataFrame) -> list[dict]:
    """把单只股票日线 DataFrame 转成行字典（索引为 date）。"""
    rows: list[dict] = []
    for trade_date, row in frame.iterrows():
        item = {
            "code": code,
            "trade_date": pd.Timestamp(trade_date).date(),
            "tradestatus": (
                int(row["tradestatus"]) if "tradestatus" in frame.columns and pd.notna(row.get("tradestatus")) else None
            ),
            "is_st": (
                bool(row["is_st"]) if "is_st" in frame.columns and pd.notna(row.get("is_st")) else None
            ),
            "adjustflag": (
                int(row["adjustflag"]) if "adjustflag" in frame.columns and pd.notna(row.get("adjustflag")) else None
            ),
        }
        for column in PRICE_COLUMNS + NUMERIC_COLUMNS:
            if column in frame.columns:
                value = row.get(column)
                item[column] = None if pd.isna(value) else float(value)
        rows.append(item)
    return rows


def _load_cache_payload(root: Path, code: str, start: Optional[str], end: Optional[str]) -> Optional[_CodePayload]:
    """读取单只缓存（CSV + meta），窗口不覆盖请求区间时返回 None。"""
    csv_path = root / "raw" / f"{code}.csv.gz"
    meta_path = root / "meta" / f"{code}.json"
    if not csv_path.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        logger.warning("读取 %s 的 meta 失败: %s", code, error)
        return None
    if start and meta.get("requested_start", "") > start:
        return None
    if end and meta.get("requested_end", "") < end:
        return None
    try:
        frame = pd.read_csv(csv_path, parse_dates=["date"], index_col="date")
    except Exception as error:  # noqa: BLE001
        logger.warning("读取 %s 的行情缓存失败: %s", code, error)
        return None
    if frame.empty:
        return None
    if start:
        frame = frame[frame.index >= pd.Timestamp(start)]
    if end:
        frame = frame[frame.index <= pd.Timestamp(end)]
    if frame.empty:
        return None
    return _CodePayload(code=code, rows=_frame_rows(code, frame), meta=meta)


def _write_payloads(session: Session, engine: Engine, payloads: list[_CodePayload], now) -> int:
    """写一批股票（先删后插，保证重抓后的行集合不残留旧行）。"""
    written = 0
    insert_stmt = _dialect_insert(engine, SwingBarDaily.__table__)
    meta_insert = _dialect_insert(engine, SwingBarFetchMeta.__table__)
    for payload in payloads:
        # 重抓可能缩短历史（补正/退市），先按 code 清除旧行再整段写入
        session.execute(delete(SwingBarDaily).where(SwingBarDaily.code == payload.code))
        written += len(payload.rows)
        if insert_stmt is not None:
            for start_at in range(0, len(payload.rows), 1000):
                chunk = payload.rows[start_at : start_at + 1000]
                statement = insert_stmt.values(chunk)
                statement = statement.on_conflict_do_update(
                    index_elements=["code", "trade_date"],
                    set_={
                        key: statement.excluded[key]
                        for key in chunk[0].keys()
                        if key not in {"code", "trade_date"}
                    },
                )
                session.execute(statement)
        else:  # 兜底：不支持 ON CONFLICT 的方言走普通插入
            session.bulk_insert_mappings(SwingBarDaily, payload.rows)

        meta = payload.meta
        meta_row = {
            "code": payload.code,
            "requested_start": meta.get("requested_start"),
            "requested_end": meta.get("requested_end"),
            "first_date": pd.Timestamp(meta["first_date"]).date() if meta.get("first_date") else None,
            "last_date": pd.Timestamp(meta["last_date"]).date() if meta.get("last_date") else None,
            "rows": int(meta.get("rows") or len(payload.rows)),
            "fetched_at": meta.get("fetched_at"),
            "ingested_at": now,
        }
        if meta_insert is not None:
            statement = meta_insert.values(meta_row)
            statement = statement.on_conflict_do_update(
                index_elements=["code"],
                set_={key: statement.excluded[key] for key in meta_row.keys() if key != "code"},
            )
            session.execute(statement)
        else:
            session.merge(SwingBarFetchMeta(**meta_row))
    return written


def _existing_meta(session: Session) -> Dict[str, tuple[Optional[str], Optional[int]]]:
    rows = session.execute(select(SwingBarFetchMeta.code, SwingBarFetchMeta.fetched_at, SwingBarFetchMeta.rows)).all()
    return {row[0]: (row[1], row[2]) for row in rows}


def ingest_cache(
    cache_dir: str | Path,
    url: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    codes: Optional[Iterable[str]] = None,
    refresh: bool = False,
    dry_run: bool = False,
    batch_codes: int = 50,
    progress_every: int = 50,
) -> IngestResult:
    """把行情缓存落库（幂等）。

    - `codes` 为空时扫描 cache_dir/raw 下全部代码；
    - 同一 `fetched_at` + `rows` 的代码默认跳过（`refresh=True` 强制重写）；
    - `dry_run=True` 只统计规模，不建表不写库。
    """
    root = Path(cache_dir)
    if not root.exists():
        raise FileNotFoundError(f"行情缓存目录不存在：{root}")
    if codes is None:
        codes = sorted(path.name[:-7] for path in root.glob("raw/*.csv.gz"))
    codes = [str(code).zfill(6) for code in codes]

    result = IngestResult()
    if dry_run:
        for code in codes:
            payload = _load_cache_payload(root, code, start, end)
            result.codes_seen += 1
            if payload is None:
                result.codes_without_data.append(code)
            else:
                result.rows_written += len(payload.rows)
        return result

    engine = make_engine(url)
    ensure_schema(engine)
    factory = sessionmaker(bind=engine, future=True)
    now = pd.Timestamp.now().to_pydatetime()
    batch: list[_CodePayload] = []

    # 先丢二级索引再落库（写完重建），避免逐行维护非聚簇索引导致尾部严重退化
    drop_secondary_indexes(engine)
    try:
        with factory() as session:
            existing = {} if refresh else _existing_meta(session)
            session.commit()

        for index, code in enumerate(codes, start=1):
            payload = _load_cache_payload(root, code, start, end)
            result.codes_seen += 1
            if payload is None:
                result.codes_without_data.append(code)
            elif not refresh and existing.get(code) == (
                payload.meta.get("fetched_at"),
                int(payload.meta.get("rows") or 0),
            ):
                result.codes_skipped += 1
            else:
                batch.append(payload)

            should_flush = len(batch) >= batch_codes or index == len(codes)
            if should_flush and batch:
                with factory() as session:
                    result.rows_written += _write_payloads(session, engine, batch, now)
                    session.commit()
                result.codes_written += len(batch)
                batch = []
            if progress_every > 0 and (index % progress_every == 0 or index == len(codes)):
                logger.info(
                    "落库进度 %d/%d：写入 %d 只 / %d 行，跳过 %d 只",
                    index,
                    len(codes),
                    result.codes_written,
                    result.rows_written,
                    result.codes_skipped,
                )
    finally:
        ensure_indexes(engine)
    return result


def table_counts(url: Optional[str] = None) -> dict:
    engine = make_engine(url)
    ensure_schema(engine)
    with Session(engine) as session:
        bars = session.execute(select(func.count()).select_from(SwingBarDaily)).scalar_one()
        metas = session.execute(select(func.count()).select_from(SwingBarFetchMeta)).scalar_one()
        return {"bars": int(bars), "codes": int(metas)}


def load_frames_from_db(
    url: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    codes: Optional[Iterable[str]] = None,
    limit_codes: int = 0,
) -> Dict[str, pd.DataFrame]:
    """从库读回行情，输出与 CSV 缓存同构的 ``Dict[code, DataFrame]``（索引 date）。"""
    engine = make_engine(url)
    ensure_schema(engine)
    statement = select(SwingBarDaily)
    if start:
        statement = statement.where(SwingBarDaily.trade_date >= pd.Timestamp(start).date())
    if end:
        statement = statement.where(SwingBarDaily.trade_date <= pd.Timestamp(end).date())
    if codes is not None:
        wanted = [str(code).zfill(6) for code in codes]
        if limit_codes and limit_codes > 0:
            wanted = wanted[:limit_codes]
        if not wanted:
            return {}
        statement = statement.where(SwingBarDaily.code.in_(wanted))
    statement = statement.order_by(SwingBarDaily.code, SwingBarDaily.trade_date)

    parts_by_code: Dict[str, list[pd.DataFrame]] = {}
    with engine.connect() as connection:
        for chunk in pd.read_sql(statement, connection, chunksize=200_000):
            for code, part in chunk.groupby("code", sort=True):
                part = part.drop(columns=["code"]).set_index("trade_date")
                part.index = pd.to_datetime(part.index)
                part.index.name = "date"
                parts_by_code.setdefault(str(code), []).append(part)

    merged: Dict[str, pd.DataFrame] = {}
    for code, parts in parts_by_code.items():
        frame = pd.concat(parts).sort_index()
        frame.insert(0, "code", code)
        if "is_st" in frame.columns:
            frame["is_st"] = frame["is_st"].fillna(False).astype(bool)
        if "tradestatus" in frame.columns:
            frame["tradestatus"] = frame["tradestatus"].fillna(0).astype(int)
        merged[code] = frame
    if limit_codes and limit_codes > 0:
        merged = dict(sorted(merged.items())[:limit_codes])
    return merged
