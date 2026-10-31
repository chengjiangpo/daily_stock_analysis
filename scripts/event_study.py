#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件研究 CLI（V3.0 §4.4 / I-6）。

在做组合回测之前，先用信号级事件研究回答「这个信号本身有没有 alpha」。
口径与 ``fund-analysis`` 的 ``npm run event-study`` 完全一致，两仓库可直接比对结果。

用法：
    # 研究 v1 主策略入场信号（R-1 的第一步）
    .venv/bin/python scripts/event_study.py --start 2016-01-01 --end 2024-12-31 \
        --events trend_pullback_entry --primary-horizon 10

    # 阴性对照（校准门槛本身）
    .venv/bin/python scripts/event_study.py --start 2018-01-01 --end 2024-12-31 \
        --events random --primary-horizon 5

    # 从 PostgreSQL 读行情 + 官方市值（免 CSV 缓存）
    .venv/bin/python scripts/event_study.py --bars-url "$SWING_BARS_DATABASE_URL" \
        --bars-schema swing_bars --start 2018-01-01 --end 2024-12-31 \
        --events trend_pullback_entry --primary-horizon 10

    # 结果写 JSON/CSV（供试验账本 §3.2 引用）
    .venv/bin/python scripts/event_study.py --events trend_pullback_entry --out reports/event-study

输出：控制台表格 + 初筛门槛逐项判定（7 项全过才允许进入组合回测）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.data.loader import load_frames  # noqa: E402
from src.sim.rules import MarketRules  # noqa: E402
from src.strategy.events import build_detectors, list_detectors  # noqa: E402
from src.validation.event_study import (  # noqa: E402
    DEFAULT_EVENT_HORIZONS,
    DEFAULT_ROUND_TRIP_COST_BPS,
    DEFAULT_SIZE_EDGES_YI,
    EventGateConfig,
    EventGateOptions,
    EventStudyAccumulator,
    EventStudyOptions,
    build_calendar,
    evaluate_event_gate,
)
from src.validation.event_study import EventHorizonStats  # noqa: E402

logger = logging.getLogger("event_study")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="信号级事件研究（组合回测前置关卡）")
    # 区间与行情
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--cache-dir", default="data/cache/swing_bars_full")
    parser.add_argument("--data-dir", default=None, help="改用本地 CSV 目录而不是 BaoStock 缓存")
    parser.add_argument("--bars-url", default=None, help="从 PostgreSQL 读行情（替代 CSV 缓存）")
    parser.add_argument("--bars-schema", default=None, help="行情所在 schema（如 swing_bars）")
    parser.add_argument("--adj-source", default="baostock", choices=("baostock", "tushare"),
                        help="复权口径；tushare 用重建因子（I-4b），用于修复配股类除权漏调整")
    parser.add_argument("--codes-file", default=None)
    parser.add_argument("--limit-codes", type=int, default=0, help="只跑前 N 只（冒烟）")
    parser.add_argument("--caps-cache", default="data/cache/market_caps",
                        help="市值/官方涨跌停价缓存目录（scripts/fetch_market_caps.py 产出）")
    # 事件
    parser.add_argument("--events", default="trend_pullback_entry",
                        help=f"逗号分隔的事件定义，可用: {','.join(list_detectors())}")
    parser.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_EVENT_HORIZONS))
    parser.add_argument("--primary-horizon", type=int, default=10, help="门槛判定的主周期")
    # 口径
    parser.add_argument("--size-edges", default=",".join(str(edge) for edge in DEFAULT_SIZE_EDGES_YI),
                        help="流通市值分档边界（亿元）")
    parser.add_argument("--max-entry-gap-pct", type=float, default=8.0)
    parser.add_argument("--max-circ-mv-yi", type=float, default=None,
                        help="股票池流通市值上限（亿元）；缺省不设上限")
    parser.add_argument(
        "--universe",
        default="circ-mv",
        choices=("circ-mv", "v1-pool"),
        help="股票池口径：circ-mv=主板+非ST+(可选)流通市值上限（N1/N2 口径）；"
        "v1-pool=主板+非ST+上市满120交易日+20日均额≥5000万+收盘≥2元（与组合回测 PoolConfig 一致，R-1 冻结口径）",
    )
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_ROUND_TRIP_COST_BPS)
    parser.add_argument("--bootstrap", type=int, default=2000,
                        help="按信号日聚类 bootstrap 次数（0 关闭）")
    parser.add_argument("--seed", type=int, default=42)
    # 门槛
    parser.add_argument("--min-samples", type=int, default=200)
    parser.add_argument("--min-t-stat", type=float, default=2.0)
    parser.add_argument("--min-clusters", type=int, default=30)
    # 输出
    parser.add_argument("--out", default=None, help="结果目录；缺省只打印不落盘")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def _parse_int_list(text: str) -> list[int]:
    return [int(item) for item in text.split(",") if item.strip()]


def _parse_float_list(text: str) -> list[float]:
    return [float(item) for item in text.split(",") if item.strip()]


def _load_caps(cache_dir: str) -> Optional[pd.DataFrame]:
    """读取市值缓存（含 circ_mv / up_limit）。缺失时返回 None，调用方须披露降级。"""
    from src.data.market_caps import load_market_caps

    path = Path(cache_dir)
    if not path.exists():
        return None
    try:
        return load_market_caps(path)
    except Exception as error:  # noqa: BLE001
        logger.warning("读取市值缓存失败（%s）：%s", path, error)
        return None


def attach_event_columns(
    frame: pd.DataFrame,
    code: str,
    caps_by_date: Optional[pd.DataFrame],
    rules: MarketRules,
) -> pd.DataFrame:
    """给行情补上事件研究需要的 ``circ_mv`` / ``limit_up`` / ``is_st`` 列。

    ``limit_up`` 优先用官方 ``stk_limit``（I-5 已全量验证与自算一致）；
    缺失时按 ``MarketRules`` + ``pre_close`` 即时计算（与引擎同口径）。
    两者皆缺时留空，事件研究会把信号计入 ``up_limit_missing``（口径降级须披露）。
    """
    from src.sim.matcher import compute_limit_prices

    out = frame.copy()
    out["code"] = code
    if "circ_mv" not in out.columns:
        out["circ_mv"] = float("nan")
    if "limit_up" not in out.columns:
        out["limit_up"] = float("nan")
    if caps_by_date is not None and not caps_by_date.empty:
        joined = caps_by_date.reindex(out.index)
        if "circ_mv" in joined.columns:
            out["circ_mv"] = joined["circ_mv"].to_numpy()
        if "up_limit" in joined.columns:
            official = pd.to_numeric(joined["up_limit"], errors="coerce")
            # 官方哨兵值（1000000）表示不设限 → 置 NaN 并视为「官方已给出且不设限」
            official = official.where(official < 1e6)
            out["limit_up"] = official.to_numpy()
        if "is_st" in joined.columns:
            out["is_st"] = joined["is_st"].fillna(False).to_numpy()

    missing_mask = out["limit_up"].isna()
    if missing_mask.any():
        computed: list[float] = []
        for timestamp, row in out.loc[missing_mask].iterrows():
            pre_close = row.get("pre_close")
            if pre_close is None or pd.isna(pre_close) or float(pre_close) <= 0:
                computed.append(float("nan"))
                continue
            listed_days = None
            listed_value = row.get("listed_days")
            if listed_value is not None and not pd.isna(listed_value):
                listed_days = int(listed_value)
            is_st = bool(row.get("is_st", False)) if not pd.isna(row.get("is_st", False)) else False
            try:
                rule = rules.limit_rule(code, pd.Timestamp(timestamp).date(), is_st=is_st,
                                        listed_days=listed_days)
                up, _ = compute_limit_prices(pre_close, rule, rules.tick_size)
            except ValueError:
                up = None
            computed.append(float(up) if up is not None else float("nan"))
        out.loc[missing_mask, "limit_up"] = computed
    return out


def detect_events(
    detectors,
    frames: Dict[str, pd.DataFrame],
    progress_every: int = 50,
) -> Dict[str, Dict[str, set]]:
    """对全部股票跑一遍事件检测，返回 ``{code: {event_name: {dates}}}``。"""
    result: Dict[str, Dict[str, set]] = {}
    total = len(frames)
    for index, (code, frame) in enumerate(frames.items(), start=1):
        per_stock: Dict[str, set] = {}
        for detector in detectors:
            per_stock[detector.name] = detector.detect(code, frame)
        result[code] = per_stock
        if progress_every > 0 and (index % progress_every == 0 or index == total):
            logger.info("事件检测进度 %d/%d", index, total)
    return result


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def print_horizon_table(stats: list[EventHorizonStats], cost_bps: float) -> None:
    header = (
        f"  {'h':>3} {'样本':>6} {'信号日':>6} {'均值%':>8} {'中位%':>8} {'胜率':>6} "
        f"{'扣费%':>8} {'对照%':>8} {'超额%':>8} {'聚类t':>7} {'p':>7} {'聚类':>5} "
        f"{'去Top5%':>9} {'市场超额%':>10}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for item in stats:
        print(
            f"  {item.horizon:>3} {item.n:>6} {item.date_count:>6} "
            f"{_fmt(item.mean_pct):>8} {_fmt(item.median_pct):>8} "
            f"{_fmt(item.win_rate):>6} {_fmt(item.net_mean_pct):>8} "
            f"{_fmt(item.control_mean_pct):>8} {_fmt(item.excess_mean_pct):>8} "
            f"{_fmt(item.excess_t_stat, 2):>7} {_fmt(item.excess_p_value):>7} "
            f"{item.cluster_count:>5} {_fmt(item.excess_ex_top5_mean_pct):>9} "
            f"{_fmt(item.market_excess_mean_pct):>10}"
        )
    print(f"  （扣费% 已扣除 {cost_bps}bp 往返成本；超额% 为事件收益减同日同市值分档对照组均值）")


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    horizons = _parse_int_list(args.horizons)
    size_edges = _parse_float_list(args.size_edges)
    event_names = [item.strip() for item in args.events.split(",") if item.strip()]
    detectors = build_detectors(event_names)

    print("=== 事件研究 ===")
    print(f"区间：{args.start} ~ {args.end}")
    print(f"事件定义：{', '.join(f'{d.name}（{d.label}）' for d in detectors)}")
    print(f"持有周期：{horizons}；主周期 {args.primary_horizon}")
    print(f"股票池口径：{args.universe}；市值分档（亿元）：{size_edges}；往返成本 {args.cost_bps}bp")

    frames = load_frames(
        start=args.start,
        end=args.end,
        data_dir=args.data_dir,
        cache_dir=args.cache_dir,
        codes_file=args.codes_file,
        limit_codes=args.limit_codes,
        bars_url=args.bars_url,
        bars_schema=args.bars_schema,
        adj_source=args.adj_source,
    )
    if not frames:
        print("无行情数据，退出。")
        return 2
    logger.info("行情：%d 只股票", len(frames))

    caps = _load_caps(args.caps_cache)
    if caps is None:
        print(f"⚠ 未找到市值缓存 {args.cache_cache if False else args.caps_cache}："
              "circ_mv 缺失 → 所有信号会被跳过；请先跑 scripts/fetch_market_caps.py")
        return 2
    caps["code"] = caps["code"].astype(str).str.zfill(6)
    caps_by_code = {
        code: group.set_index(pd.to_datetime(group["trade_date"]))
        for code, group in caps.groupby("code")
    }
    rules = MarketRules.load()

    calendar = build_calendar(frames)
    print(f"交易日历：{len(calendar)} 天（{calendar[0].date()} ~ {calendar[-1].date()}）")

    pool_config = None
    universe_fn = None
    if args.universe == "v1-pool":
        from src.strategy.filters import PoolConfig

        pool_config = PoolConfig()

        def universe_fn(code, row):  # noqa: ANN001 - 与 event_study 的约定一致
            value = row.get("in_pool") if hasattr(row, "get") else None
            if value is None:
                return False
            try:
                return bool(value) and not pd.isna(value)
            except (TypeError, ValueError):
                return False

    options = EventStudyOptions(
        calendar=calendar,
        start=args.start,
        end=args.end,
        horizons=horizons,
        size_edges_yi=size_edges,
        gate=EventGateConfig(max_entry_gap_pct=args.max_entry_gap_pct),
        cost_bps=args.cost_bps,
        universe=universe_fn,
        max_circ_mv_yi=args.max_circ_mv_yi if universe_fn is None else None,
        bootstrap=args.bootstrap,
        seed=args.seed,
    )
    accumulator = EventStudyAccumulator(options, [detector.name for detector in detectors])

    logger.info("开始逐票扫描 ...")
    total = len(frames)
    for index, (code, frame) in enumerate(frames.items(), start=1):
        enriched = attach_event_columns(frame, code, caps_by_code.get(code), rules)
        if pool_config is not None:
            from src.data.pool import prepare_pool_frame

            enriched = prepare_pool_frame(enriched, pool_config)
        event_dates = {detector.name: detector.detect(code, frame) for detector in detectors}
        accumulator.add_stock(enriched, event_dates)
        if index % 100 == 0 or index == total:
            logger.info("扫描进度 %d/%d（对照池观测 %d）", index, total, accumulator.pool_observations)

    result = accumulator.finalize()
    print(
        f"\n股票池观测：{result.pool_observations:,} 次（股票-信号日-可买）"
        f"；扫描 {result.stock_count} 只"
    )

    gate_options = EventGateOptions(
        min_samples=args.min_samples,
        min_t_stat=args.min_t_stat,
        min_clusters=args.min_clusters,
    )
    payload: dict = {"meta": {
        "start": args.start,
        "end": args.end,
        "horizons": horizons,
        "primaryHorizon": args.primary_horizon,
        "sizeEdgesYi": size_edges,
        "costBps": args.cost_bps,
        "maxEntryGapPct": args.max_entry_gap_pct,
        "maxCircMvYi": args.max_circ_mv_yi,
        "universe": args.universe,
        "adjSource": args.adj_source,
        "codes": len(frames),
        "calendarDays": len(calendar),
    }, "events": []}

    for event in result.events:
        detector = next(item for item in detectors if item.name == event.name)
        print(f"\n=== {detector.label} ===")
        print(
            f"  信号 {event.detected_count:,} 条，可买 {event.tradable_count:,} 条，"
            f"拦截 {event.blocked_count:,} 条"
        )
        blocked = [(key, value) for key, value in event.blocked_by_reason.items() if value > 0]
        if blocked:
            print("  拦截原因：" + "，".join(f"{key}={value:,}" for key, value in blocked))
        print_horizon_table(list(event.horizons), args.cost_bps)

        primary = next(
            (item for item in event.horizons if item.horizon == args.primary_horizon),
            event.horizons[-1],
        )
        print(f"\n  主周期 {primary.horizon} 日分年超额：")
        for year in primary.by_year:
            print(
                f"    {year['year']}: n={year['n']:>5}  均值 {_fmt(year['meanPct'])}%  "
                f"超额 {_fmt(year['excessMeanPct'])}%"
            )
        if primary.dropped_no_exit:
            print(f"  （因缺少退出日行情剔除 {primary.dropped_no_exit} 笔，口径轻微乐观）")
        print("  * 超额中位数为「事件收益 − 对照组均值」，收益右偏时天然偏负，仅参考、不作判定依据")

        gate = evaluate_event_gate(primary, gate_options)
        print(f"\n  初筛门槛（主周期 {primary.horizon} 日）：{'通过' if gate.passed else '未通过'}")
        for check in gate.checks:
            print(f"    [{'✓' if check.passed else '✗'}] {check.label}  → {check.actual}")
        if not gate.passed:
            print("  → 未通过：不得进入组合回测（V3.0 §3.2 准入条件 3）")

        payload["events"].append(
            {
                "name": event.name,
                "label": detector.label,
                "hypothesis": detector.hypothesis,
                **event.to_dict(),
                "gate": gate.to_dict(),
            }
        )

    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "event_study.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        rows = []
        for event in payload["events"]:
            for item in event["horizons"]:
                rows.append(
                    {
                        "event": event["name"],
                        "horizon": item["horizon"],
                        "n": item["n"],
                        "dateCount": item["dateCount"],
                        "meanPct": item["meanPct"],
                        "excessMeanPct": item["excessMeanPct"],
                        "excessTStat": item["excessTStat"],
                        "excessPValue": item["excessPValue"],
                        "clusterCount": item["clusterCount"],
                        "netMeanPct": item["netMeanPct"],
                        "excessExTop5MeanPct": item["excessExTop5MeanPct"],
                        "excessCiLowPct": item["excessCiLowPct"],
                        "excessCiHighPct": item["excessCiHighPct"],
                    }
                )
        pd.DataFrame(rows).to_csv(out_dir / "event_study_horizons.csv", index=False, encoding="utf-8")
        logger.info("结果已写入 %s", out_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
