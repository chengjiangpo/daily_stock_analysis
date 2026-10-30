#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""试验预算校验（V3.0 §3.2.2：两仓库累计试验次数 ≤ 200，超预算拒绝运行）。

用法：
  python scripts/check_experiment_budget.py [--ledger reports/experiments/ledger.csv] [--budget 200]

退出码：0 = 预算内；1 = 超预算（应拒绝启动新试验）；2 = 账本缺失/格式错误。
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = REPO_ROOT / "reports" / "experiments" / "ledger.csv"
DEFAULT_BUDGET = 200


def main() -> int:
    parser = argparse.ArgumentParser(description="试验账本预算校验")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    args = parser.parse_args()

    ledger = Path(args.ledger)
    if not ledger.exists():
        print(f"错误：试验账本不存在：{ledger}", file=sys.stderr)
        return 2

    with ledger.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or "experiment_id" not in rows[0]:
        print("错误：账本为空或缺少 experiment_id 列", file=sys.stderr)
        return 2

    seen: set[str] = set()
    for row in rows:
        experiment_id = (row.get("experiment_id") or "").strip()
        if not experiment_id:
            print(f"错误：存在空 experiment_id 行", file=sys.stderr)
            return 2
        if experiment_id in seen:
            print(f"错误：experiment_id 重复：{experiment_id}", file=sys.stderr)
            return 2
        seen.add(experiment_id)

    total = len(rows)
    per_repo = Counter((row.get("repo") or "unknown").strip() for row in rows)
    print(f"试验计数：{total}/{args.budget}")
    for repo, count in sorted(per_repo.items()):
        print(f"  - {repo}: {count}")

    if total > args.budget:
        print(
            f"拒绝：试验次数 {total} 超出预算 {args.budget}（V3.0 §3.2.2）。"
            "超预算需书面说明后方可继续。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
