# 试验账本（V2.0 §5.5 / V3.0 §3.2）

**用途**：登记每一次「假设检验型」回测/事件研究试验，防止多重检验产生假阳性。任何新策略假设进入回测前，必须先在本账本登记（V3.0 §3.2 准入条件）。

## 规则

1. **预算**：两仓库（`fund-analysis` + `daily_stock_analysis`）试验次数合并计数，累计 **≤ 200**；超预算需书面说明。校验：`python scripts/check_experiment_budget.py`。
2. **每试验必记**（V2.0 §5.5 字段 + 跨仓扩展）：
   - `experiment_id`：稳定唯一 ID（建议 `假设编号-口径-仓库缩写`）；
   - `registered_date`：试验登记日（扩展字段，V3.0 §6.3 跨仓合并计数需要）；
   - `repo`：`daily_stock_analysis` 或 `fund-analysis`（扩展字段，同上）；
   - `strategy_version` / `parameter_set` / `periods` / `cost_model` / `code_commit` / `metrics` / `decision` / `decision_reason`：V2.0 §5.5 原字段。
3. **禁止**：看结果后改规则再声称「样本外」；同一测试区间重复计算收益。
4. **统计**：试验数 > 20 时报告必须附 DSR；> 50 时附 PBO（V2.0 §5.5）。
5. 负先验登记册见 [fund-analysis/docs/a-share-swing-backtest-v3-plan.md §2.4](https://github.com/chengjiangpo/fund-analysis/blob/main/docs/a-share-swing-backtest-v3-plan.md)（N1–N3）；新假设登记前先查该登记册与本账本。

## 当前计数

| repo | 试验数 |
|---|---|
| fund-analysis | 2（N1、N2 锚点；子试验明细见其报告） |
| daily_stock_analysis | 2（N3 主口径 + 诊断口径） |
| **合计** | **4 / 200** |
