# -*- coding: utf-8 -*-
"""回测统计模块。

本模块只依赖 numpy / pandas / scipy，提供净值、交易、bootstrap、基准对比、
DSR、PBO 以及按持仓周期拆解盈亏的纯计算函数。所有函数均不修改输入对象。
"""

from __future__ import annotations

import itertools
import math
from typing import Callable, Optional, Sequence

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

TRADING_DAYS = 252
_EULER_GAMMA = 0.5772156649015329


def _finite_series(values: pd.Series) -> pd.Series:
    """返回去除非有限值后的浮点 Series，保留原有索引顺序。"""
    series = pd.Series(values, dtype=float)
    return series.replace([np.inf, -np.inf], np.nan).dropna()


def _zero_performance() -> dict[str, float]:
    return {
        "total_return": 0.0,
        "cagr": 0.0,
        "annual_vol": 0.0,
        "sharpe": 0.0,
        "max_drawdown": 0.0,
        "calmar": 0.0,
        "n_days": 0.0,
    }


def performance_metrics(equity: pd.Series) -> dict[str, float]:
    """计算净值曲线的基础绩效指标。

    Args:
        equity: 按交易日排序的净值序列，值应大于 0；NaN / inf 会被丢弃。

    Returns:
        包含 total_return、cagr、annual_vol、sharpe、max_drawdown、calmar、n_days
        的字典。max_drawdown 以正数表示回撤幅度。空序列或单点序列返回全 0。

    Notes:
        n_days 为有效净值点数；CAGR 使用 n_days - 1 个日收益年化。
    """
    series = _finite_series(equity)
    series = series[series > 0]
    n = int(len(series))
    if n < 2:
        return _zero_performance()

    values = series.to_numpy(dtype=float)
    total_return = float(values[-1] / values[0] - 1.0)
    n_returns = n - 1
    cagr = float((values[-1] / values[0]) ** (TRADING_DAYS / n_returns) - 1.0)

    daily_returns = np.diff(values) / values[:-1]
    mean_return = float(np.mean(daily_returns))
    std_return = float(np.std(daily_returns, ddof=1)) if n_returns > 1 else 0.0
    annual_vol = std_return * math.sqrt(TRADING_DAYS)
    sharpe = mean_return / std_return * math.sqrt(TRADING_DAYS) if std_return > 0 else 0.0

    running_max = np.maximum.accumulate(values)
    drawdown = values / running_max - 1.0
    max_drawdown = float(-np.min(drawdown))
    calmar = cagr / max_drawdown if max_drawdown > 0 else 0.0

    return {
        "total_return": total_return,
        "cagr": cagr,
        "annual_vol": float(annual_vol),
        "sharpe": float(sharpe),
        "max_drawdown": max_drawdown,
        "calmar": float(calmar),
        "n_days": float(n),
    }


def trade_metrics(pnls: Sequence[float], gross_profits: Optional[Sequence[float]] = None) -> dict[str, float]:
    """计算逐笔交易的汇总指标。

    Args:
        pnls: 逐笔净盈亏序列；NaN / inf 会被丢弃。
        gross_profits: 可选的逐笔毛盈利序列；长度与 pnls 一致时，
            profit_factor 使用该序列计算，其余指标仍基于 pnls，长度不一致时忽略。

    Returns:
        包含 n_trades、win_rate、profit_factor、avg_pnl、median_pnl、expectancy、
        max_consecutive_losses 的字典。无交易时全部为 0；无亏损且有盈利时
        profit_factor 为 inf；全亏损时为 0。
    """
    raw = np.asarray(list(pnls), dtype=float)
    finite_mask = np.isfinite(raw)
    values = raw[finite_mask]
    n_trades = int(values.size)

    if n_trades == 0:
        return {
            "n_trades": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "avg_pnl": 0.0,
            "median_pnl": 0.0,
            "expectancy": 0.0,
            "max_consecutive_losses": 0.0,
        }

    wins = values[values > 0]
    win_rate = float(wins.size / n_trades)

    profit_source = values
    if gross_profits is not None:
        gross = np.asarray(list(gross_profits), dtype=float)
        if gross.shape == raw.shape:
            profit_source = gross[finite_mask]
            profit_source = profit_source[np.isfinite(profit_source)]

    gross_profit = float(np.sum(profit_source[profit_source > 0]))
    gross_loss = float(-np.sum(profit_source[profit_source < 0]))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    max_consecutive_losses = 0
    current_losses = 0
    for value in values:
        if value < 0:
            current_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, current_losses)
        else:
            current_losses = 0

    avg_pnl = float(np.mean(values))
    return {
        "n_trades": float(n_trades),
        "win_rate": win_rate,
        "profit_factor": float(profit_factor),
        "avg_pnl": avg_pnl,
        "median_pnl": float(np.median(values)),
        "expectancy": avg_pnl,
        "max_consecutive_losses": float(max_consecutive_losses),
    }


def block_bootstrap_ci(
    returns: pd.Series,
    statistic: Callable[[pd.Series], float],
    n_boot: int = 2000,
    block: int = 20,
    alpha: float = 0.05,
    seed: int = 42,
) -> dict[str, float]:
    """用循环块 bootstrap 估计统计量的置信区间。

    Args:
        returns: 日收益序列；NaN / inf 会被丢弃。
        statistic: 接受 pd.Series 并返回标量的统计函数。
        n_boot: bootstrap 次数。
        block: 块长（交易日）；样本短于块长时退化为单点重采样。
        alpha: 显著性水平，返回 1 - alpha 的置信区间。
        seed: 随机种子，相同输入与 seed 可完全复现。

    Returns:
        包含 estimate、ci_low、ci_high、n_boot、block 的字典。
    """
    series = _finite_series(returns)
    n = int(len(series))
    requested_block = int(block)
    n_boot = int(n_boot)

    if n == 0 or n_boot < 1:
        return {
            "estimate": float("nan"),
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "n_boot": float(n_boot),
            "block": float(requested_block),
        }

    values = series.to_numpy(dtype=float)
    estimate = float(statistic(pd.Series(values)))
    rng = np.random.default_rng(seed)

    effective_block = max(requested_block, 1)
    if n <= effective_block:
        # 样本不足一个块：按单点（有放回）重采样，避免空样本或越界。
        sample_indices = rng.integers(0, n, size=(n_boot, n))
    else:
        n_blocks = int(math.ceil(n / effective_block))
        starts = rng.integers(0, n, size=(n_boot, n_blocks))
        offsets = np.arange(effective_block)
        indices = (starts[:, :, None] + offsets[None, None, :]) % n
        sample_indices = indices.reshape(n_boot, -1)[:, :n]

    boot_stats = np.empty(n_boot, dtype=float)
    for i in range(n_boot):
        boot_stats[i] = float(statistic(pd.Series(values[sample_indices[i]])))

    boot_stats = boot_stats[np.isfinite(boot_stats)]
    if boot_stats.size == 0:
        ci_low = ci_high = float("nan")
    else:
        ci_low, ci_high = np.quantile(boot_stats, [alpha / 2.0, 1.0 - alpha / 2.0])

    return {
        "estimate": estimate,
        "ci_low": float(ci_low),
        "ci_high": float(ci_high),
        "n_boot": float(n_boot),
        "block": float(requested_block),
    }


def alpha_vs_benchmark(strategy_returns: pd.Series, benchmark_returns: pd.Series) -> dict[str, float]:
    """对策略收益与基准收益做 OLS，计算年化 alpha、beta 与信息比率。

    模型：strategy = alpha_daily + beta * benchmark + eps。

    Returns:
        包含 alpha_annual、beta、alpha_t、ir、n_days 的字典。日期按索引内连接对齐。
        ir = 年化超额收益均值 / 年化跟踪误差；跟踪误差为 0 时返回 inf（超额非零）
        或 0（超额为零）。
    """
    strategy = pd.Series(strategy_returns, dtype=float).rename("strategy")
    benchmark = pd.Series(benchmark_returns, dtype=float).rename("benchmark")
    aligned = pd.concat([strategy, benchmark], axis=1, join="inner")
    aligned = aligned.replace([np.inf, -np.inf], np.nan).dropna()
    n = int(len(aligned))

    if n < 2:
        return {
            "alpha_annual": 0.0,
            "beta": 0.0,
            "alpha_t": 0.0,
            "ir": 0.0,
            "n_days": float(n),
        }

    y = aligned["strategy"].to_numpy(dtype=float)
    x = aligned["benchmark"].to_numpy(dtype=float)
    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    x_centered = x - x_mean
    sxx = float(np.sum(x_centered**2))

    if sxx <= 0:
        beta = 0.0
        alpha_daily = y_mean
        residual = y - alpha_daily
    else:
        beta = float(np.sum(x_centered * (y - y_mean)) / sxx)
        alpha_daily = y_mean - beta * x_mean
        residual = y - alpha_daily - beta * x

    if n > 2 and sxx > 0:
        sigma2 = float(np.sum(residual**2) / (n - 2))
        se_alpha = math.sqrt(max(sigma2 * (1.0 / n + x_mean**2 / sxx), 0.0))
        alpha_t = alpha_daily / se_alpha if se_alpha > 0 else 0.0
    else:
        alpha_t = 0.0

    excess = y - x
    excess_mean = float(np.mean(excess))
    excess_std = float(np.std(excess, ddof=1)) if n > 1 else 0.0
    if excess_std > 0:
        ir = excess_mean / excess_std * math.sqrt(TRADING_DAYS)
    elif excess_mean != 0:
        ir = float("inf") if excess_mean > 0 else float("-inf")
    else:
        ir = 0.0

    return {
        "alpha_annual": float(alpha_daily * TRADING_DAYS),
        "beta": beta,
        "alpha_t": float(alpha_t),
        "ir": float(ir),
        "n_days": float(n),
    }


def deflated_sharpe_ratio(
    returns: pd.Series,
    n_trials: int,
    sr_benchmark: float = 0.0,
) -> dict[str, float]:
    """计算 Bailey & López de Prado 的 Deflated Sharpe Ratio（非正态调整版）。

    Args:
        returns: 日收益序列；NaN / inf 会被丢弃。
        n_trials: 参与筛选的独立试验数，必须 >= 1。
        sr_benchmark: 额外的 Sharpe 门槛（日频、非年化），与多重检验期望最大值取较大者。

    Returns:
        包含 sr_observed、sr_expected_max、dsr、n_trials、n_days 的字典。
    """
    n_trials = int(n_trials)
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")

    series = _finite_series(returns)
    n = int(len(series))
    if n < 2:
        return {
            "sr_observed": 0.0,
            "sr_expected_max": 0.0,
            "dsr": 0.0,
            "n_trials": float(n_trials),
            "n_days": float(n),
        }

    values = series.to_numpy(dtype=float)
    mean_return = float(np.mean(values))
    std_return = float(np.std(values, ddof=1))
    if std_return > 0:
        sr_observed = mean_return / std_return
    elif mean_return > 0:
        sr_observed = float("inf")
    elif mean_return < 0:
        sr_observed = float("-inf")
    else:
        sr_observed = 0.0

    if not np.isfinite(sr_observed):
        dsr = 1.0 if sr_observed > 0 else 0.0
        return {
            "sr_observed": float(sr_observed),
            "sr_expected_max": 0.0,
            "dsr": float(dsr),
            "n_trials": float(n_trials),
            "n_days": float(n),
        }

    skew = float(scipy_stats.skew(values, bias=False)) if n > 2 else 0.0
    kurtosis = float(scipy_stats.kurtosis(values, fisher=False, bias=False)) if n > 3 else 3.0
    variance_sr = max(
        (1.0 - skew * sr_observed + (kurtosis - 1.0) / 4.0 * sr_observed**2) / (n - 1),
        0.0,
    )

    if n_trials <= 1:
        sr_expected_max = 0.0
    else:
        z1 = scipy_stats.norm.ppf(1.0 - 1.0 / n_trials)
        z2 = scipy_stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
        sr_expected_max = math.sqrt(variance_sr) * (
            (1.0 - _EULER_GAMMA) * z1 + _EULER_GAMMA * z2
        )

    threshold = max(sr_expected_max, float(sr_benchmark))
    denominator = math.sqrt(variance_sr) if variance_sr > 0 else 0.0
    if denominator > 0:
        z_score = (sr_observed - threshold) * math.sqrt(n - 1) / denominator
        dsr = float(scipy_stats.norm.cdf(z_score))
    else:
        dsr = 1.0 if sr_observed > threshold else 0.0

    return {
        "sr_observed": float(sr_observed),
        "sr_expected_max": float(sr_expected_max),
        "dsr": dsr,
        "n_trials": float(n_trials),
        "n_days": float(n),
    }


def _column_sharpe(block: np.ndarray) -> np.ndarray:
    """按列计算日收益 Sharpe；标准差为 0 时用大常数表示排序。"""
    mean = block.mean(axis=0)
    std = block.std(axis=0, ddof=1)
    sharpes = np.zeros(block.shape[1], dtype=float)
    valid = std > 0
    sharpes[valid] = mean[valid] / std[valid]
    zero_std = ~valid
    sharpes[zero_std] = np.where(
        mean[zero_std] > 0,
        1e6,
        np.where(mean[zero_std] < 0, -1e6, 0.0),
    )
    return sharpes


def probability_of_backtest_overfitting(
    returns_matrix: np.ndarray,
    n_splits: int = 8,
    seed: int = 42,
) -> dict[str, float]:
    """用 CSCV 计算回测过拟合概率（PBO）。

    Args:
        returns_matrix: shape 为 (T, N) 的日收益矩阵，每列一个参数组合。
        n_splits: 时间分段数；建议使用偶数（CSCV 标准做法）。
        seed: 保留参数以统一接口；CSCV 本身是确定性算法。

    Returns:
        包含 pbo、n_splits、n_combos、logit_mean 的字典。样本不足时返回
        pbo=nan 并附带 reason 键。
    """
    del seed  # CSCV 为确定性流程，保留参数仅用于统一调用接口。
    matrix = np.asarray(returns_matrix, dtype=float)
    if matrix.ndim != 2:
        raise ValueError("returns_matrix must be a 2-D array of shape (T, N)")

    finite_rows = np.isfinite(matrix).all(axis=1)
    matrix = matrix[finite_rows]
    n_rows, n_columns = matrix.shape
    n_splits = int(n_splits)

    def _insufficient(reason: str) -> dict[str, float]:
        return {
            "pbo": float("nan"),
            "n_splits": float(n_splits),
            "n_combos": 0.0,
            "logit_mean": float("nan"),
            "reason": reason,
        }

    if n_columns < 2:
        return _insufficient("need at least 2 strategy columns")
    if n_splits < 2:
        return _insufficient("n_splits must be >= 2")
    if n_rows < n_splits * 2:
        return _insufficient("need at least n_splits * 2 observations")

    segments = np.array_split(np.arange(n_rows), n_splits)
    in_sample_size = n_splits // 2
    combinations = list(itertools.combinations(range(n_splits), in_sample_size))
    if not combinations:
        return _insufficient("no valid CSCV combinations")

    logits: list[float] = []
    for in_sample_ids in combinations:
        out_sample_ids = tuple(i for i in range(n_splits) if i not in in_sample_ids)
        if not out_sample_ids:
            continue

        in_sample_rows = np.concatenate([segments[i] for i in in_sample_ids])
        out_sample_rows = np.concatenate([segments[i] for i in out_sample_ids])

        in_sample_scores = _column_sharpe(matrix[in_sample_rows])
        best_column = int(np.argmax(in_sample_scores))
        out_sample_scores = _column_sharpe(matrix[out_sample_rows])

        ranks = scipy_stats.rankdata(-out_sample_scores, method="average")
        rank = float(ranks[best_column])
        logits.append(math.log((n_columns + 1.0 - rank) / rank))

    if not logits:
        return _insufficient("no valid CSCV combinations")

    logit_array = np.asarray(logits, dtype=float)
    return {
        "pbo": float(np.mean(logit_array <= 0)),
        "n_splits": float(n_splits),
        "n_combos": float(len(logits)),
        "logit_mean": float(np.mean(logit_array)),
    }


def decompose_pnl(trades: pd.DataFrame) -> pd.DataFrame:
    """按持仓天数分桶汇总逐笔交易盈亏。

    Args:
        trades: 至少包含 pnl、hold_days 两列；若存在 r_multiple 列则额外汇总均值。

    Returns:
        以 hold_days_bucket 为索引的 DataFrame，包含 n_trades、win_rate、
        avg_pnl、total_pnl（以及可选的 avg_r_multiple）。
    """
    required = {"pnl", "hold_days"}
    missing = required - set(trades.columns)
    if missing:
        raise ValueError(f"trades missing required columns: {sorted(missing)}")

    columns = ["n_trades", "win_rate", "avg_pnl", "total_pnl"]
    if "r_multiple" in trades.columns:
        columns.append("avg_r_multiple")

    if trades.empty:
        return pd.DataFrame(columns=columns, index=pd.Index([], name="hold_days_bucket"), dtype=float)

    frame = trades.copy()
    frame["pnl"] = pd.to_numeric(frame["pnl"], errors="coerce")
    frame["hold_days"] = pd.to_numeric(frame["hold_days"], errors="coerce")
    if "r_multiple" in frame.columns:
        frame["r_multiple"] = pd.to_numeric(frame["r_multiple"], errors="coerce")
    frame = frame.dropna(subset=["pnl", "hold_days"])
    if frame.empty:
        return pd.DataFrame(columns=columns, index=pd.Index([], name="hold_days_bucket"), dtype=float)

    bins = [-np.inf, 1.0, 3.0, 5.0, 10.0, 20.0, np.inf]
    labels = ["1", "2-3", "4-5", "6-10", "11-20", "21+"]
    frame["hold_days_bucket"] = pd.cut(frame["hold_days"], bins=bins, labels=labels, right=True)
    frame = frame.dropna(subset=["hold_days_bucket"])
    frame["_win"] = (frame["pnl"] > 0).astype(float)

    grouped = frame.groupby("hold_days_bucket", observed=True, sort=False)
    result = grouped.agg(
        n_trades=("pnl", "size"),
        win_rate=("_win", "mean"),
        avg_pnl=("pnl", "mean"),
        total_pnl=("pnl", "sum"),
    )
    if "r_multiple" in frame.columns:
        result["avg_r_multiple"] = grouped["r_multiple"].mean()

    result = result.reindex(labels)
    result = result.dropna(subset=["n_trades"])
    result["n_trades"] = result["n_trades"].astype(int)
    result.index.name = "hold_days_bucket"
    return result[columns]
