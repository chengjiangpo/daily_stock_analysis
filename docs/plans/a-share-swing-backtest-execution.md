# A 股短线波段系统 · 可执行任务拆分（Execution Backlog）

- 来源：[a-share-swing-backtest-plan.md](./a-share-swing-backtest-plan.md)（V2.0）
- 拆分日期：2026-10-26
- 用途：把方案章节转成**可认领、可验收、可追踪**的任务。方案仍是规则真源；本文只负责排序、边界与验收。
- 状态标记：`[ ]` 未开始 · `[~]` 进行中 · `[x]` 已完成 · `[!]` 阻塞

---

## 0. 使用规则

1. 任务编号自包含，交付物必须能对上方案章节。
2. 严格按阶段推进：**阶段 -1 → 阶段 0（go/no-go）→ 阶段 1**。阶段 0 未通过，不开发完整引擎。
3. 每个任务完成时补三样证据：改动文件、验证命令、结论（通过/不通过/数据不足）。
4. 任何阈值改动必须登记到 `reports/experiments/ledger.csv`（方案 §5.5），禁止「看结果再改条件」。

---

## 1. 本会话执行范围（阶段 0 垂直切片）

目标：不依赖完整引擎，先跑通「规则 → 信号 → 撮合 → 组合 → 统计」的最小闭环，并产出可判定结论。

| 编号 | 任务 | 交付物 | 验收 |
|---|---|---|---|
| S-01 | 配置与规则表 | `config/market_rules.yaml`、`config/fees.yaml`、`config/backtest.yaml`，`src/sim/rules.py` 加载与日期分段 | `[x]` 单测覆盖 10%/20%/30%/ST 分段、印花税 2023-08-28 切换 |
| S-02 | 撮合核心 | `src/sim/matcher.py`：Decimal 涨跌停、一字板、开盘涨停、跌停可卖、参与率上限 | `[x]` 方案 §12.1 撮合用例全过 |
| S-03 | 费用与滑点 | `src/sim/fees.py`：佣金最低 5 元、印花税分段、过户/经手/证管费、tick 滑点 | `[x]` 手工算例逐项对齐 |
| S-04 | 组合与风控 | `src/sim/portfolio.py`：30 万 AUM、最多 5 仓、25% 单票、1% 风险预算、单日 2 笔、回撤熔断、连亏降档 | `[x]` 仓位/整手/风控拒绝用例 |
| S-05 | 主策略 v1 | `src/strategy/`：指标、趋势回调再启动、条件诊断、评分 | `[x]` 合成行情可稳定触发，18 条诊断逐条输出 |
| S-06 | 对照策略与基准 | `box_breakout`、`momentum`、B3 动量基准；B2 匹配组合抽样的可复现 RNG | `[x]` 同 seed 抽样一致 |
| S-07 | 回测循环 | `src/sim/engine.py`：T 收盘信号 → T+1 开盘执行，卖出资金次日可用，收盘估值 | `[x]` 合成数据逐日净值断言 |
| S-08 | 验证统计 | `src/validation/`：切分、滚动窗口、区块 bootstrap、PF/IR/回撤、DSR/PBO | `[x]` 13 项统计测试 |
| S-09 | MVP CLI | `scripts/mvp_edge_check.py`：BaoStock（含退市股）→ 每笔毛超额 vs B2 → 报告 | `[x]` 全市场 3328 只、1511 笔已完成 |
| S-10 | 交付报告 | `reports/mvp/` 下 `summary.md` + `trades.csv` + `daily_equity.csv` | `[x]` 已完成，结论 STOP |

本会话不做的部分：阶段 1 完整落库（SQLAlchemy 新表）、阶段 5 全量滚动前推、阶段 6 模拟实盘。相关任务已列在下方，待阶段 0 通过后启动。

---

## 2. 阶段 -1：前置实测（必须先做）

| 编号 | 任务 | 方法 | 通过标准 | 状态 |
|---|---|---|---|---|
| P0-01 | BaoStock 退市股日线 | 取 3 只不同年份退市股，查 2016 起日线 | 能取到退市前 ≥120 个交易日，字段含 `isST/tradestatus/preclose/turn` | `[x]` 4 只退市股均可用；字段齐全 |
| P0-02 | TuShare `stk_limit` 起始年 | 按年抽样 | 明确最早可用日期；不可用则全面自算 | `[!]` 无 TUSHARE_TOKEN，全面自算 |
| P0-03 | 复权口径校准 | 高分红股，比对除权日 `hfq/raw` 跳变 | 跳变可由分红送转解释；明确是否含分红 | `[x]` **BaoStock 后复权含现金分红**（与方案原文相反） |
| P0-04 | 盘后固定价成交量 | 抽样 30 只 | 给出占全日成交比例区间 | `[ ]` 未做（实验组后置） |
| P0-05 | 北交所退市整理期规则 | 交易所原文 | 记录规则条款 | `[ ]` 未做（北交所后置） |

> P0-01 不通过 → 方案 §10 结论视为不可信，MVP 停止。P0-03 不通过 → 信号序列不可用。

---

## 3. 阶段 0：MVP 垂直切片（6–8 人日）

范围冻结：主板、2016–2024、BaoStock 日线、主策略 v1、T+1 开盘、8 日时间退出或结构止损、B2 匹配基准。

| 编号 | 任务 | 依赖 | 交付物 | 状态 |
|---|---|---|---|---|
| M0-01 | 规则/费用配置（= S-01） | P0-03 | `config/*.yaml`、`src/sim/rules.py` | `[x]` |
| M0-02 | 数据读取与标准化 | P0-01 | `src/data/bars.py`、`src/data/baostock_bars.py` | `[x]` 真实 BaoStock 已验证 |
| M0-03 | 时点股票池（主板简化版） | M0-02 | `src/data/pool.py`：上市 120 日、非 ST、正常交易、20 日均额 ≥5000 万、收盘 ≥2 元 | `[x]` |
| M0-04 | 主策略信号 + 诊断 | M0-01 | `src/strategy/trend_pullback.py`、`diagnostics.py` | `[x]` |
| M0-05 | 简化撮合与成本 | M0-01 | `src/sim/matcher.py`、`fees.py` | `[x]` |
| M0-06 | 仓位与退出 | M0-05 | `src/sim/portfolio.py`、`src/sim/engine.py` | `[x]` |
| M0-07 | 回测循环与净值 | M0-04~06 | `src/sim/engine.py`（净值并入引擎） | `[x]` |
| M0-08 | B2 匹配基准 | M0-03 | `src/strategy/benchmarks.py`：同市值/波动分位 + 同行业，随机 5 只等权 ×200 次取中位数 | `[x]` 市值以 20 日均额代理 |
| M0-09 | 超额与显著性 | M0-07/08 | 每笔毛超额、t 值、区块 bootstrap 95% CI | `[x]` 1511 笔，CI [-0.48%, 0.11%] |
| M0-10 | go/no-go 报告 | M0-09 | `reports/mvp/summary.md` | `[x]` STOP（见 §13） |

**验收（方案 §10.2）**：笔数 ≥300；每笔毛超额 ≥0.6%；bootstrap 95% CI 下界 >0.3%；扣基础成本净期望 >0。

---

## 4. 阶段 1：完整数据层（15–25 人日）

| 编号 | 任务 | 交付物 | 验收 |
|---|---|---|---|
| D1-01 | 表结构 + 迁移 | `src/storage.py` 增量模型：`bars_daily`、`stock_status_daily`、`pool_daily`、`daily_signals`、`signal_diagnostics`、`orders`、`fills`、`positions_daily`、`portfolio_daily`、`trade_records`、`backtest_runs` | 建表幂等；旧表行为不变 |
| D1-02 | 数据快照与哈希 | `src/data/snapshot.py` | 同输入同哈希；写入 `run_metadata` |
| D1-03 | 十条质量校验 | `src/data/quality.py` | 每条校验有失败样例；§4.5 全过 |
| D1-04 | 时点池完整版 | `src/data/pool.py` | 每日成分数 vs 交易所上市数偏差 ≤2% 告警 |
| D1-05 | 复权三口径 | `src/data/adjust.py` | 除权日阈值重算、送转股数、分红入账单测 |
| D1-06 | 退市/ST/停牌历史 | 数据源适配 + 隔离清单 | 退市股退市前区间可入池 |
| D1-07 | 行业历史成员 | `src/data/industry.py` | 无历史时报告声明局限 |
| D1-08 | 涨跌停价落库 | `limit_up/limit_down` + 交叉校验 | 与 `high/low` 反向校验通过率记录 |

---

## 5. 阶段 2：策略层（5–10 人日）

| 编号 | 任务 | 交付物 | 验收 |
|---|---|---|---|
| S2-01 | 指标库 | `src/strategy/indicators.py`：MA/ATR/缩量比/收盘位置/摆动高低点 | `high==low` 不除零；边界用例 |
| S2-02 | 主策略 v1 冻结配置 | `config/strategies/trend_pullback_restart.yaml` | 参数与方案附录 A 一致 |
| S2-03 | 箱体突破 | `box_breakout.py` | 与主策略共用撮合/资金 |
| S2-04 | 动量基准 | `momentum.py` | 20 日收益前 10%，等权 5 只 |
| S2-05 | 诊断落表 | `diagnostics.py` + `signal_diagnostics` | 每条件含 observed/threshold/failure_reason |
| S2-06 | 评分 | `scoring.py` | 同日多信号按 score 排序，禁止未来收益 |
| S2-07 | 选股日报 | CLI `scripts/daily_signals.py` | 输出可解释报告 |

---

## 6. 阶段 3：撮合/组合（20–30 人日）

| 编号 | 任务 | 交付物 | 验收 |
|---|---|---|---|
| S3-01 | 日循环时钟 | `src/sim/clock.py` | 顺序固定，交易日历无空洞 |
| S3-02 | 成交判据全量 | `matcher.py` | §6.2 八种场景单测 |
| S3-03 | 参与率与容量 | `matcher.py`、`capacity.csv` | 30/100/500 万三档估算 |
| S3-04 | 公司行为 | `src/sim/corporate_actions.py` | 10 送 3 + 派息用例 |
| S3-05 | 持仓与可卖数量 | `portfolio.py` | 当日买入不可卖 |
| S3-06 | 资金时序 | `execution.py` | 卖出资金不用于同日开盘买入 |
| S3-07 | 净值与账务重建 | `equity.py` | equity 可由 fills+positions+分红 重建 |
| S3-08 | 盘后固定价实验组 | `execution.py` | 单列成交率/容量 |

---

## 7. 阶段 4：回测与对照（8–12 人日）

| 编号 | 任务 | 交付物 | 验收 |
|---|---|---|---|
| B4-01 | 运行编排 | `src/services/backtest_run_service.py` | 一次调用产出 §13 全部文件 |
| B4-02 | CLI | `scripts/run_backtest.py`（`--strategy/--period/--cost-tier/--out`） | 三策略可跑 |
| B4-03 | 报告层 | `src/reports/backtest_report.py` | 首页顺序固定 |
| B4-04 | 复现测试 | `tests/integration/` | 同哈希+参数+commit 逐日一致 1e-9 |
| B4-05 | 试验账本 | `reports/experiments/ledger.csv` + 校验脚本 | 超 200 次拒绝运行 |

---

## 8. 阶段 5：验证与稳健性（15–20 人日）

| 编号 | 任务 | 交付物 | 验收 |
|---|---|---|---|
| V5-01 | 时间切分 | `src/validation/split.py` | 开发/验证/样本外边界断言 |
| V5-02 | 滚动前推 | `src/validation/walk_forward.py` | 窗口不重叠，拼接净值 |
| V5-03 | 参数敏感性 | `src/validation/sensitivity.py` | 输出参数-收益曲面 |
| V5-04 | 成本三档 | 同上 | 基础/较高/压力 |
| V5-05 | 统计检验 | `src/validation/stats.py` | 区块 bootstrap、DSR、PBO |
| V5-06 | 五因子归因 | `attribution.py` | size/vol/industry/momentum/turnover 暴露 + 残差 alpha |
| V5-07 | 验收判定 | `src/validation/verdict.py` | §8.4 A/B/C 分级自动输出 |

---

## 9. 阶段 6：模拟实盘（12 周）

| 编号 | 任务 | 交付物 | 验收 |
|---|---|---|---|
| L6-01 | 每日信号与模拟订单 | `scripts/sim_trade.py` | 每日落库 |
| L6-02 | 偏差监控 | `src/validation/live_deviation.py` | 成交率偏差 ≤10pp；滑点 ≤1.5× 模型 |
| L6-03 | 周报 | `reports/sim/` | 信号数 ≥历史同期 60% |

---

## 10. 关键路径与工作量

```text
P0-01/03 ──► M0-01 ──► M0-04 ──► M0-07 ──► M0-09 ──► go/no-go
   │            │         │          ▲
   └► M0-02 ────┴► M0-03 ─┘          │
                        M0-05 ──► M0-06
M0-08 ────────────────────────────────┘
```

- 阶段 -1：2–3 人日
- 阶段 0：6–8 人日
- 阶段 1–5：63–94 人日
- 阶段 6：持续 12 周

---

## 11. 会话完成记录

| 日期 | 完成任务 | 证据 |
|---|---|---|
| 2026-10-26 | 文档拆分、工程骨架 | 见本文件与 git diff |

---

## 12. 已落地模块与命令（阶段 0 骨架）

### 12.1 模块

| 层 | 模块 | 说明 |
|---|---|---|
| 配置 | `config/market_rules.yaml`、`config/fees.yaml`、`config/backtest.yaml` | 规则/费用/组合/策略参数；支持 `BACKTEST_*` 环境变量覆盖 |
| 策略 | `src/strategy/` | 指标、主策略、箱体突破、动量、过滤、诊断、评分、B2 基准 |
| 撮合 | `src/sim/` | Decimal 涨跌停、费用分段、T+1、参与率、风控、日循环 |
| 数据 | `src/data/` | BaoStock 原始+后复权、字段标准化、质量校验、快照哈希、时点池、行情装载 |
| 验证 | `src/validation/` | 时间切分、滚动窗口、bootstrap、DSR/PBO、绩效指标 |
| 服务 | `src/services/backtest_run_service.py` | 一次运行 + 报告落盘 |
| 服务 | `src/services/swing_backtest_service.py` | 运行库扫描、详情聚合、文件下载、缓存异步运行 |
| API | `api/v1/endpoints/swing_backtest.py` | `/api/v1/swing-backtest` 运行列表/详情/状态/触发/下载 |
| Web | `apps/dsa-web/src/pages/SwingBacktestPage.tsx` | `/swing-backtest`：go/no-go、净值、交易、诊断、运行表单 |
| CLI | `scripts/mvp_edge_check.py`、`scripts/run_backtest.py` | MVP go/no-go、组合回测、滚动前推 |

### 12.2 命令

```bash
# 依赖（一次性）
python3.12 -m venv .venv
.venv/bin/pip install pandas numpy sqlalchemy pyyaml pytest baostock tushare scipy tenacity exchange-calendars

# 离线冒烟（无需网络）
.venv/bin/python scripts/mvp_edge_check.py --offline-fixtures \
  --start 2023-01-01 --end 2023-12-31 --out reports/mvp_smoke

# MVP（BaoStock，自动发现历史股票池）
.venv/bin/python scripts/mvp_edge_check.py --start 2016-01-01 --end 2024-12-31 \
  --cache-dir data/cache/swing_bars --out reports/mvp

# 全量抓取可 4 分片并行（每个进程独立 BaoStock 会话，写同一缓存目录）
.venv/bin/python scripts/fetch_swing_bars.py --codes-file data/cache/chunks/chunk_0.txt \
  --start 2016-01-01 --end 2024-12-31 --cache-dir data/cache/swing_bars --label w0

# 单次组合回测
.venv/bin/python scripts/run_backtest.py --strategy trend_pullback_restart \
  --start 2016-01-01 --end 2022-12-31 --cost-tier base --out reports/backtest/dev

# 滚动前推
.venv/bin/python scripts/run_backtest.py --walk-forward \
  --start 2016-01-01 --end 2026-09-30 --out reports/backtest/wf
```

### 12.2.1 Web / API

```bash
# 页面（开发）
cd apps/dsa-web && npm run dev
# 打开 http://localhost:5173/swing-backtest

# 只读 API
curl http://127.0.0.1:8000/api/v1/swing-backtest/runs
curl http://127.0.0.1:8000/api/v1/swing-backtest/runs/mvp
```

说明见 [docs/swing-backtest.md](../../docs/swing-backtest.md)。

### 12.3 已知限制（必须在报告中声明）

1. **B2 的 size 代理**：无全市场市值数据时用 20 日均额代替市值分位；接入 TuShare `daily_basic` 后替换。
2. **行业字段**：BaoStock 日线不含申万行业，当前为空时 B2 退化为全池抽样。
3. **公司行为未建模**：送转/现金分红在阶段 3 实现；当前用 `adj_factor` 仅做信号换算。
   - 实测修正（见 [a-share-swing-data-probe.md](./a-share-swing-data-probe.md)）：BaoStock 后复权**包含现金分红**，因此「后复权算信号 + 不复权算成交」成立；但持仓期间现金分红未入账，组合收益会**低估**分红部分。MVP 结论按偏保守解读。
4. **官方涨跌停价**：无 TuShare `stk_limit` 时按分段规则自算。
5. **退市股**：P0-01 已验证可用；`query_all_stock` 历史时点并集可覆盖窗口内退市股（2016–2024 并集 5320 只）。
6. **未落库**：阶段 1 的 SQLAlchemy 表尚未创建，当前报告直接写 CSV/JSON。
7. **MVP 评估口径**：脚本默认信号级（固定 6 万名义本金、无组合约束），以匹配 §10.1；`--portfolio-constrained` 才启用 5 仓/2 笔/日/熔断，组合版结果不作为 MVP 判据。
8. **回撤熔断的恢复规则（实现补充）**：方案 §7 只写「回撤 >18% 停止新开仓」，未定义恢复方式。若按字面永久停止，2018 年后将不再交易，样本被截断。当前实现为「首次触发进入 20 个交易日冷却；冷却结束后以半风险恢复开仓，回撤回到 12% 以下再解除熔断」。该规则已写入 `config/backtest.yaml` 的 `drawdown_cooldown_days`，需在冻结 v1 前确认。

### 12.4 验证记录

```text
.venv/bin/python -m pytest tests/unit/test_swing_stats.py \
  tests/unit/test_swing_core.py tests/unit/test_swing_data.py \
  tests/unit/test_swing_validation.py tests/integration/test_swing_engine.py -q
# 48 passed
```


---

## 13. 阶段 0 结论（2026-10-26）

### 13.1 运行口径

- 数据：BaoStock 2016-01-04～2024-12-31，主板 3328 只（含窗口内退市股），3324 只有日线。
- 评估：**信号级**（方案 §10.1），固定 6 万名义本金/笔，无 5 仓、2 笔/日、回撤熔断等组合约束。
- 成交：T+1 开盘，跳空上限 3%，一字/开盘涨停不买，停牌不买；退出按 v1 规则次日开盘。
- 基准：B2 同市值分位（20 日均额代理）+ 同波动分位 + 行业，随机 5 只等权 ×200 次取中位数。
- 成本：基础档（佣金 0.025%/最低 5 元，印花税按日期分段，1 tick 滑点）。

### 13.2 结果

| 判据 | 实测 | 门槛 | 通过 |
|---|---|---|---|
| 交易笔数 | 1511 | ≥300 | 是 |
| 每笔毛超额均值 | **-0.20%** | ≥0.6% | 否 |
| bootstrap 95% CI 下界 | **-0.48%** | >0.3% | 否 |
| 扣基础成本后每笔净期望 | **-0.68%** | >0 | 否 |

补充：每笔毛收益 -0.52%，B2 基准 -0.32%；胜率 28.3%，PF 0.73；退出分布为 trend_break 1129、time_exit 263、take_profit 79、hard_stop 40。分年度交易数 65–214，样本覆盖完整。

### 13.3 结论

按方案 §10.3：**STOP**。不进入阶段 1–6 的完整系统开发，保留现有代码作为研究工具。

### 13.4 解读与限制

1. 负超额主要来自 `trend_break`（收盘 < MA10）退出：1129 笔、均值 -2.73%；时间退出与止盈为正，但不足以覆盖。说明 v1 的「回调后再启动」在 A 股 2016–2024 上没有稳定边际。
2. 这是**对冻结 v1 的检验结果**，不得据此直接调参再声称样本外；若继续研究，必须先登记新假设与试验预算（§5.5）。
3. 未计入现金分红、未建公司行为、行业为当前口径、size 用 20 日均额代理，均会带来偏差；但毛超额为负且 CI 上界仅 0.11%，修正这些口径不足以支持 0.6% 门槛。
4. 组合约束版（5 仓/2 笔/日/熔断）单独跑出 834 笔、总收益 -33.2%、最大回撤 40.4%，用于识别执行与风控问题，不作为 MVP 判据。
5. 数据质量：3321/3328 只通过 §4.5 可自动判定项；7 只隔离候选（5 只复权因子单调性、2 只 OHLC/量额），仅涉及 2 笔交易，未改变结论。
6. 复现命令：`.venv/bin/python scripts/mvp_edge_check.py --start 2016-01-01 --end 2024-12-31 --cache-dir data/cache/swing_bars_full --out reports/mvp`。

---

## 14. I-1 MVP 复现验证（2026-10-30）

### 14.1 目的与口径

按 V3.0 方案 §5.2 的 I-1，验证 §13 的 STOP 结论能否在当前 HEAD 与重建后的数据缓存上复现，
排除「结论依赖一次性环境或一次性缓存」的可能。

- **必须是 MVP 冻结口径**：`--b2 legacy`（同日 20 日均额代理分位桶 + 同波动分位 + 行业，
  随机 5 只 ×200 次取中位数）。用 `--b2 auto` 时市值缓存存在会自动切到 `matched`，
  差异来自口径切换而非漂移，**不能作为复现判据**。
- 数据：BaoStock 2016-01-04～2024-12-31，主板 3328 只（含窗口内退市股），缓存补至 3328/3328。
- 基线：§13.2 与试验账本 `N3-mvp-signal-level`（run_id `mvp-20261026-233203`）。

### 14.2 执行记录

| 步骤 | 命令要点 | 实测 |
|---|---|---|
| 补抓缺失 85 只（全为 6004xx 段，上轮会话中断遗留） | `scripts/fetch_swing_bars.py --codes-file data/cache/swing_bars_full/missing_codes_now.txt --start 2016-01-01 --end 2024-12-31 --cache-dir data/cache/swing_bars_full` | 85/85 有数据，**6m53s**（≈12 只/分钟，单会话；并发会触发限流） |
| 全窗口复现 | `scripts/mvp_edge_check.py --start 2016-01-01 --end 2024-12-31 --cache-dir data/cache/swing_bars_full --b2 legacy --out reports/mvp_reproduction/run-legacy` | **7m31s**，3328 只 / 2187 交易日 / 1511 笔 |
| 比对判级 | `scripts/verify_mvp_reproduction.py --run-dir reports/mvp_reproduction/run-legacy` | **MATCH** |

复现 run_id `mvp-20261030-133041` @ commit `07b2bad`。

### 14.3 比对结果

| 指标 | 已发布（§13.2） | 复现 | 差值 | 容差内 |
|---|---|---|---|---|
| 交易笔数 | 1511 | 1511 | 0 | 是 |
| 每笔毛收益 | -0.52% | -0.52% | -0.00pp | 是 |
| B2 基准 | -0.32% | -0.32% | -0.00pp | 是 |
| 每笔毛超额 | -0.20% | -0.20% | +0.00pp | 是 |
| bootstrap 95% CI | [-0.48%, +0.11%] | [-0.48%, +0.11%] | 0 | 是 |
| 扣基础成本净期望 | -0.68% | -0.68% | -0.00pp | 是 |
| 胜率 | 28.30% | 28.33% | +0.03pp | 是 |
| PF | 0.73 | 0.73 | +0.39pp | 是 |
| 退出分布 | 1129/263/79/40 | 1129/263/79/40 | 0 | 是 |

### 14.4 结论

**MATCH**：STOP 结论与全部关键数字可复现，仅胜率/PF 存在千分位舍入级差异。§13 的证伪
不是单次环境或缓存偶然所致，冻结 v1 的负结论成立。

### 14.5 留痕与边界

1. 复现产物在 `reports/mvp_reproduction/run-legacy/`（gitignored，8.3MB），`comparison.json`
   由 `verify_mvp_reproduction.py` 重生成；长期留痕以本节数字 + run_id + commit 为准。
2. 2026-10-28 曾产出一次 `DRIFT` 比对，**作废**：那次只跑 2023 单年、1106 只、37 笔，
   样本口径与发布结论不同，不构成漂移证据。
3. 数据完整性核对：161 只在窗口内提前结束（退市，正常），78 只行数 <500（2023 年后
   上市新股），无缺失代码（3328/3328）。
4. **本复现不计入试验账本**（复现验证 ≠ 新假设）；若要用 `matched` 口径重跑同一冻结规则
   做对照检验，属新试验，须先登记 `reports/experiments/ledger.csv` 再跑。
5. 行情仍在文件缓存（`data/` 被 gitignore），**未落库**；SQLAlchemy 落库见 §15（I-4）。

---

## 15. I-4 行情落库（SQLAlchemy）

阶段 0 的已知限制第 6 条「未落库」在本阶段收口：把 `data/cache/swing_bars_full` 的
BaoStock 日线装入关系库，使回测不再依赖单一机器上的 CSV 缓存。

### 15.1 设计

| 表 | 主键 | 说明 |
|---|---|---|
| `swing_bars_daily` | `(code, trade_date)` | 不复权真实价 OHLC + `pre_close`/`volume`/`amount`/`turn`/`pct_chg`/`tradestatus`/`is_st`，后复权 `hfq_close` 与 `adj_factor`（信号层用），`adjustflag` 保留原始抓取口径 |
| `swing_bars_fetch_meta` | `code` | 抓取溯源：`requested_start/end`、`first_date`、`last_date`、`rows`、`fetched_at`，用于判断缓存是否覆盖请求窗口（等价于原 `meta/*.json`） |

- 后端无关：默认 `sqlite:///data/swing_bars.db`；生产用 PostgreSQL，**推荐独立 schema**
  （`SWING_BARS_SCHEMA=swing_bars`，通过连接层 `search_path` 生效）：与 `fund-analysis`
  的 `public.stock_eod_daily` 同库不同 schema，既能互不干扰、又能跨 schema 对账。
  URL 由 `SWING_BARS_DATABASE_URL` 或 `--url` 指定；裸 `postgres://` 会按实际安装的驱动
  自动改写为 `postgresql+psycopg2://`（本仓库依赖 psycopg2）。
- 幂等写入：按主键 `ON CONFLICT DO UPDATE`（PostgreSQL / SQLite ≥3.24 均支持）；
  同一 `fetched_at` 的代码默认跳过，`--refresh` 强制重写。
- 读路径：`load_frames(..., bars_url=..., bars_schema=...)` 与
  `mvp_edge_check.py --bars-url/--bars-schema`，输出与 CSV 缓存同构的
  `Dict[code, DataFrame]`，回测代码零改动。
- 日志安全：连接串在日志里经 `mask_url()` 掩掉密码。

### 15.2 命令

```bash
# 落库（PostgreSQL，独立 schema swing_bars；同脚本可用 --url 换成 sqlite）
SWING_BARS_DATABASE_URL=postgresql://user:pass@host:5432/fund \
SWING_BARS_SCHEMA=swing_bars \
.venv/bin/python scripts/ingest_swing_bars.py --cache-dir data/cache/swing_bars_full

# 只落 50 只做冒烟 / 只看规模不写库
.venv/bin/python scripts/ingest_swing_bars.py --cache-dir data/cache/swing_bars_full --limit-codes 50
.venv/bin/python scripts/ingest_swing_bars.py --cache-dir data/cache/swing_bars_full --dry-run

# 从库读回跑回测（不再依赖 CSV 缓存目录）
.venv/bin/python scripts/mvp_edge_check.py --start 2016-01-01 --end 2024-12-31 \
  --bars-url "$SWING_BARS_DATABASE_URL" --bars-schema swing_bars \
  --b2 legacy --out reports/mvp_reproduction/run-legacy-pg
```

### 15.3 执行结果（2026-10-30）

| 项 | 结果 |
|---|---|
| 全量落库 | **6,371,456 行 / 3328 只**（与 CSV 缓存行数完全相等），`swing_bars_fetch_meta` 3328 行 |
| 目标库（生产） | PostgreSQL 16.13 `/fund` 的 **`swing_bars` schema**（表 `swing_bars_daily` 1140MB、`swing_bars_fetch_meta` 480kB），与 `public.stock_eod_daily` 同库不同 schema；全量入库 **18m54s**（写入 3278 只 / 6,268,632 行 + 跳过 50 只，后者为冒烟已存的验证案例） |
| 目标库（本地） | `sqlite:///data/swing_bars.db`，**1.07GB**（`quick_check` = ok） |
| 从库读回等价性 | 两条库路径全窗口重跑均 1511 笔、`final_equity` 99,363,299.99；`trades.csv` / `performance_metrics.json` / `signals.csv` / `orders.csv` / `fills.csv` 与 CSV 路径 **逐字节相同** |
| 复现判级 | PG 读回再跑 `verify_mvp_reproduction`：**MATCH**（run `mvp-20261030-200939` @ `af22038`），耗时 3m10s |
| 幂等验证 | 同一命令重跑：`codes_written=0`、全部 `codes_skipped`，库内行数不变 |

落库实测与优化（重要，已并入代码）：

1. 首轮全量落库在尾部出现 **10 倍级退化**（50 只/24s → 50 只/>16min）。原因为 SQLite 默认
   page cache 仅 2MB，在 1GB+ 表上按主键随机插页造成抖动，叠加大表上逐行维护非聚簇索引。
2. 修正：SQLite 连接固定 `journal_mode=WAL` / `synchronous=NORMAL` / `cache_size=256MB` /
   `temp_store=MEMORY`；二级索引改为「落库前删、写完后建」；删除与主键重复的
   `ix_swing_bars_daily_code_date`。
3. 中断可续：写入幂等（同 `fetched_at`+`rows` 跳过），实测用 `--codes-file` 只补最后 78 只
   **15s** 完成（6.29M 行已就位时），无需全量重扫。
4. 环境提醒：本轮 macOS swap 已用 7.97G（`mediaanalysisd` 占 300%+ CPU）时，任何
   读 6.37M 行的任务都会明显变慢（从库读回 9m05s vs CSV 2m08s 的直接差异主要来自此）；
   性能比较必须在机器不 swap 时做。

### 15.4 待办

- **对账入口**：两套数据现在同库，可写一条 SQL 做抽检（`swing_bars.swing_bars_daily`
  JOIN `public.stock_eod_daily` ON code/trade_date），后续可接成脚本。
- **维护**：`swing_bars` schema 的备份/分区（按年）尚未做；当前单表 1140MB，
  9 年数据量下无性能压力，按年分区属可选优化。
- ℹ️ `adj_factor` 重建已于同日完成，见 §16。

---

## 16. 复权因子重建与 v1 受控复核（2026-10-30）

### 16.1 动机

`docs/swing-backtest.md` §4 已登记：两源复权因子绝对水平不可比（累积基准日不同），
而 BaoStock hfq 在部分除权事件上跳变与交易所不一致（配股类漏调整等）。v1 的信号层
（MA/趋势结构）跑在 `hfq_close` 上，若该序列存在不连续，会直接制造假趋势突破/假破位。
本步用同库 `public.stock_eod_daily.adj_factor` 重建因子，并做**只换因子源**的受控复核。

### 16.2 方法（锚点对齐，保留溯源）

```text
aligned(t) = ts_factor(t) / ts_factor(anchor) * bao_factor(anchor)
anchor = 该股票两源都有数据的第一天（stock_eod_daily 自 2017-05-26 起）
```

- 原始 `adj_factor` / `hfq_close` 列**不改写**（BaoStock 溯源保留）；新增
  `adj_factor_ts`、`adj_factor_aligned`、`adj_rebuilt_at` 三列，由纯 SQL 回填；
- 锚点之前（2016-01-01~2017-05-25）无 TuShare 覆盖，`aligned` 留空，读路径回退原始因子；
  锚点处 `aligned == bao_factor`，拼接处无人工跳变；
- 读路径开关：`mvp_edge_check.py --adj-source {baostock,tushare}`，`tushare` 时
  `hfq_close = close * aligned`；命令：`scripts/rebuild_adj_factor.py`（幂等，可重复执行）。

### 16.3 结果

| 项 | 数值 |
|---|---|
| 锚点覆盖 | 3326 / 3328 只（2 只在 TuShare 侧无覆盖） |
| 有 TuShare 因子行数 | 5,473,997 行 |
| 实质修正（相对偏离 >0.5%） | **18,107 行 / 27 只** |
| 单日因子比例跳变 >2% 的事件 | **37 个**（含 1 天错位型与持续水平偏移型） |

典型事件（`step_pct` = 重建后/原始因子比例的当日变化）：

| 日期 | 代码 | 跳变 | 性质 |
|---|---|---|---|
| 2019-12-26 | 002466 | +18.85% | 配股除权，**已知样本**（BaoStock 跳到 2020-01-02 才生效） |
| 2020-12-31 | 000001 | +20.22% | **BaoStock 自身数据错误**：原始价连续（pre_close 19.20→close 19.34）却 hfq 从 2303.2 掉到 1929.9，TuShare 无事件 |
| 2019-06-06 | 001914 | -83.96% | 持续水平偏移（后续 1820 行全部受影响） |
| 2018-12-26 | 001872 | -71.64% | 持续水平偏移（1642 行） |
| 2022-02-25 | 000981 | +66.67% | 1 天错位（02-28 回补 -40.00%） |
| 2024-03-27 | 002492 | +228.39% | 单次巨大偏移（187 行） |

结论修正一条既有认知：BaoStock hfq 的问题**不只是「配股 9 例」**，全量扫描得到 27 只 / 37 个
显著跳变，其中包含与公司行为无关的纯数据错误（000001）与持续水平偏移（001914、001872）。

### 16.4 对 v1 结论的影响（受控对照）

同一冻结 v1、同一 legacy B2、同一数据窗口，**只把复权源从 baostock 换成 tushare**：

| 指标 | baostock | tushare 重建 | 差 |
|---|---|---|---|
| 交易笔数 | 1511 | 1507 | -4 |
| 每笔毛收益 | -0.5212% | -0.5377% | -1.6bp |
| B2 基准 | -0.3232% | -0.3440% | -2.1bp |
| 每笔毛超额 | -0.1980% | **-0.1937%** | **+0.4bp** |
| bootstrap 95% CI | [-0.4825%, +0.1113%] | [-0.4820%, +0.1193%] | 微幅上移 |
| 扣成本净期望 | -0.6801% | -0.6965% | -1.6bp |
| 胜率 / PF | 28.33% / 0.734 | 28.27% / 0.728 | 微降 |
| 退出分布 | 1129/263/79/40 | 1127/263/77/40 | -2/-2 |
| 判定 | **STOP** | **STOP** | 不变 |

三条判据（毛超额 ≥0.6%、CI 下界 >0.3%、净期望 >0）仍然全部未过，且差距达数十个 bp 量级：
**STOP 结论不依赖复权源**。该复核已登记为 `N3-mvp-adjts-recheck`（账本 5/200），标注为
数据修正复核而非新假设，避免日后以「数据修正」为名重复试口径。

### 16.5 边界与后续

1. 2016-01-01~2017-05-25 无 TuShare 覆盖，该段仍用 BaoStock 因子，异常无法检测（约占窗口 16%）；
2. 重建只修正**跳变一致性**，不重算分红现金流入账（阶段 3 范畴），持仓期现金分红仍未入账；
3. `aligned` 是否作为默认读源：建议下一轮策略研究默认 `tushare`，但**本次不改冻结口径**
   （否则与 I-1 复现基线不可比）；该切换应作为新阶段起点并重新登记的基准。

---

## 17. I-5 官方 stk_limit 全量对账与起始年确认（2026-10-31）

### 17.1 目的

收口方案 P0-02 遗留：「TuShare `stk_limit` 的可用起始年不明」。V3.0 §8 此前只有抽样对账
（1436 对比 0 不匹配）。本步**不需要 TuShare token**：官方涨跌停价已经由 `stock_eod_daily`
落库，直接查表 + 与该库内的 `swing_bars.swing_bars_daily` 内连接即可全量对账。

### 17.2 命令

```bash
# 起始年与按年覆盖度（只读，秒级）
.venv/bin/python scripts/crosscheck_limit_prices.py --coverage-only

# 全量对账（5,473,997 行内连接，约 8 分钟）
.venv/bin/python scripts/crosscheck_limit_prices.py --start 2016-01-01 --end 2026-12-31 \
  --out reports/i5-limit-crosscheck --emit-no-limit-days
```

### 17.3 起始年结论（P0-02 关闭）

| 项 | 结果 |
|---|---|
| `stock_eod_daily` 总行数 | 10,354,468 |
| 含 `up_limit` / `down_limit` | 10,280,241 行 |
| 有效（非哨兵）涨跌停价首次出现 | **2017-05-26** → **起始年 2017** |
| 有效涨跌停价最后一日 | 2026-10-26 |
| 有有效涨跌停价的代码 | 5,829 / 5,830 |

**结论：2017-05-26 之前（含 MVP 窗口的 2016-01-01~2017-05-25，约占窗口 16%）不可依赖官方
`stk_limit`，必须使用 `config/market_rules.yaml` 的分段规则自算。** 这正好覆盖 MVP 的回测起点
之后不久，因此冻结口径「全面自算涨跌停价」的选择是必要的而非保守。

### 17.4 全量对账结果

差异按性质分类（脚本内置分类，只有 `both_limited_differ` / `computed_no_limit_only` 才是真错）：

| 分类 | 涨停价 | 跌停价 | 是否真错 |
|---|---|---|---|
| `both_limited_equal`（双方都有限价且一致） | 5,470,644 | 5,470,643 | — |
| `both_limited_differ`（双方都有限价但不符） | **0** | **0** | 是 |
| `first_day_regime_differ`（上市首日口径不同） | 2 | 3 | 否（见下） |
| `both_no_limit`（双方都不设限） | 245 | 245 | — |
| `official_no_limit_only`（官方不设限、自算给价） | 62 | 62 | 否（见下） |
| `computed_no_limit_only`（自算不设限、官方给价） | **0** | **0** | 是 |
| **真差异合计** | **0** | **0** | — |

**判定：PASS。双方均有限价的 10,941,287 行全部一致（容差 0.01 元，差异率 0）。**
自算分段规则可完全替代官方 `stk_limit`。

非真错的 129 处全部集中在「涨跌停制度本身不适用」的日子，分两类：

**（a）124 处「官方不设限 / 自算按常规幅度给价」** — 交易所当日明确不设涨跌幅：

| 类型 | 样例 |
|---|---|
| 退市整理期首日 | 000038 @2023-06-19、000502 @2022-06-06、000606 @2023-06-13 |
| 恢复上市首日 | 000792 盐湖股份 @2021-08-10、000670 盈方微 @2022-08-22、600610 中毅达 @2020-08-17 |

`rules.limit_rule(is_delisting=True, delisting_day=1)` 已支持该语义，但回测侧**没有「退市整理/
恢复上市日历」**，因此无法传参。

**（b）5 处「双方都有价但幅度不同」** — 均为**非 IPO 的上市首日**：

| 代码 | 日期 | 官方 | 自算 | 性质 |
|---|---|---|---|---|
| 001965 招商公路 | 2017-12-25 | ±10% | ±44%/−36% | 换股吸收合并上市首日 |
| 601598 中国外运 | 2019-01-18 | ±10% | ±44%/−36% | 换股吸收合并上市首日 |
| 601975 招商南油 | 2019-01-08 | −44.08% | −36% | 重新上市首日（跌停口径不同） |

交易所对 **IPO / 换股吸收合并 / 重新上市 / 分拆** 的首日申报区间各不相同，规则引擎只建模了
最常见的 IPO 口径（errata E3）。脚本按 `listed_days == 1` 单独归类，不计入真错。

**影响方向：保守。** 回测把「不设限」当成「有限价」，只会把本可买到的信号误判为「开盘即涨停」
而放弃，**不会制造虚假成交**。且这几类日子在股票池过滤（非 ST + 20 日均额 ≥5000 万 + 上市满
120 日）下基本不进入交易。故本轮**不修改规则引擎**，只登记为已知口径缺口；全部明细留在
`reports/i5-limit-crosscheck/no_limit_days.csv`（63 行去重明细）与 `limit_crosscheck.json`，
供将来做 rules overlay 时直接消费。

### 17.5 对账过程中修掉的三个审计脚本缺陷（规则引擎本身无错）

首轮全量对账报出 1295 处差异，逐层排查并四轮迭代后确认**全部是审计脚本自身的错**，
规则引擎无错（每一轮跑完后剩余差异都会换一种形态，直到收敛到 0）：

| # | 缺陷 | 表现 | 修正 |
|---|---|---|---|
| 1 | `listed_days` 用自然日折算（`(trade_date-first).days*5/7+1`） | 上市第 2 个交易日被算成第 1 个，误套「核准制首日 +44%/−36%」段，1048 处「官方 ±10% vs 自算 ±44%」 | 改为流式扫描中按 `(code, trade_date)` 递增计数，取**真实交易日序号** |
| 2 | 新股判定用硬编码 `date(2016,1,1)` 作数据起点 | 2016-01-04 才是有数据的第一天，导致**全部老股**被当成新股（每只 1 天） | 改为 `dataset_start = min(首根 bar)`，严格大于才视为真实新上市 |
| 3 | `listed_days` 用**内连接后**的行计数，而「是否新股」取自行情表 | 官方涨跌停价自 2017-05-26 才有，连接前的交易日被漏计；002774 的上市第 43 个交易日被算成第 1 个（581 处假差异） | 改为从行情表用窗口函数 `ROW_NUMBER() PARTITION BY code` 取真实序号 |

另修正哨兵值归一：TuShare 表达「不设限」存在 **1000000 / 100000 / 10000 三种写法**
（第三种的样例如 600145 @2022-03-30、600610 @2020-08-17），原先只识别第一种，把 148+2 处
「双方都不设限」误报成真差异。现统一按「A 股历史最高价约 2600 元，涨跌停价不可能 ≥10000」
判定哨兵。

迭代轨迹（便于复现排查方法）：1295 → 1196 → 581 → 7 → **0**。

> 教训：**审计脚本的错误方向决定结论可信度**。首轮 1295 处差异看似「规则有 0.02% 的错」，
> 实际是 0；若直接采信会去做无谓的规则修补。

---

## 18. I-6 事件研究工具移植（2026-10-31）

### 18.1 目的

把 `fund-analysis` 已验证的 §4.4 事件研究口径移植到本仓库，作为 V3.0 §3.2
「再出发准入条件 3：事件研究先行」的执行载体——**任何新策略假设在进入组合回测前，
必须先证明其入场信号层有超越同日同市值对照的毛超额**。

### 18.2 模块

| 文件 | 内容 |
|---|---|
| `src/validation/event_study.py` | 纯函数：事件累加器、留一对照池、聚类稳健 t、聚类 bootstrap、初筛门槛 7 项 |
| `src/strategy/events.py` | 事件定义：复用已注册策略的 `entry` 信号 / 条件表达式 / 随机阴性对照 |
| `scripts/event_study.py` | CLI：控制台表格 + 门槛逐项判定 + JSON/CSV 落盘 |
| `tests/unit/test_event_study.py` | 40 项单测（含与 scipy 的 p 值对齐） |

### 18.3 口径（与 fund-analysis `event-study.ts` 逐项对齐）

| 项 | 规则 |
|---|---|
| 收益 | 信号日 T 收盘确认 → **T+1 开盘买入 → 第 T+h 日收盘卖出**（h=1 即 T+1 收盘）；收益 = `(close_exit×adj_exit)/(open_entry×adj_entry)−1` |
| 入场拦截 | 8 类：次日无行情 / 缺价 / 缺开盘 / 无成交 / 缺涨停价 / **开盘即涨停** / 缺复权因子 / **高开 >8%**；被拦截的信号不计入收益，只计数 |
| 对照组（B2 matched） | 同一信号日、同一流通市值分档（<20/20–30/30–45/45–70/≥70 亿）内**所有可买股票**均值，对事件股票**留一剔除**；等价于无限次随机抽样的期望，无抽样噪声、无需种子 |
| 市场口径对照 | 同一信号日全池可买股票均值（留一） |
| 显著性 | 按「信号日所在的 h 日区块」聚类稳健标准误；p 值用 **t 分布（自由度 = 聚类数−1）**，不用正态近似 |
| bootstrap | 按**聚类**（信号日区块）重采样，默认 2000 次（§4.6 P-3） |
| 成本 | 默认 40.2bp 往返，仅用于 `扣费%` 与门槛的「净期望」项；超额本身不受影响（对照组同口径） |

p 值实现为纯 Python（Lanczos + 不完全 Beta 连分式），**不引入 scipy 运行时依赖**；
单测用 scipy 交叉验证，误差 < 1e-6。

### 18.4 初筛门槛（7 项全过才允许进入组合回测）

1. 样本数 ≥ 200
2. 聚类数 ≥ 30（t 值可靠）
3. 平均超额 > 0
4. 聚类 t ≥ 2
5. 剔除超额最高 5 笔后仍 > 0（防右尾依赖）
6. 扣往返成本后绝对收益 > 0
7. 超额为正的年份占比 ≥ 60%（年样本 ≥ 20 的那几年里）

参数字段：`EventGateOptions(min_samples, min_t_stat, min_clusters, min_positive_year_ratio, min_year_samples)`。

### 18.5 事件定义

| 名称 | 说明 |
|---|---|
| `trend_pullback_entry` | N3/R-1：v1 主策略入场信号（**不含任何退出规则**），直接复用 `TrendPullbackRestart.generate_signals` 的 `entry` 列 |
| `box_breakout_entry` | N1：箱体突破入场信号（已封存，仅作负先验复核） |
| `random` | 阴性对照：每个信号日以 p 概率触发，用于校准门槛本身 |

`strategy_entry_detector` 默认 `warmup=120` 个交易日（覆盖 MA60 等滚动窗口），
数据不足的股票不产生任何事件，避免把预热期 NaN 当信号。

### 18.6 阴性对照验证（关键）

`--events random` 跑 40 只 × 2019–2021：

| h | 样本 | 事件均值 | 对照均值 | 超额 | 聚类 t |
|---|---|---|---|---|---|
| 1 | 395 | 0.209% | 0.209% | **0.000%** | 0.27 |
| 3 | 395 | -1.194% | -1.185% | **-0.000%** | -0.99 |
| 5 | 395 | 0.247% | 0.240% | **-0.000%** | -0.64 |
| 10 | 395 | 0.845% | 0.826% | **0.000%** | 0.56 |
| 20 | 395 | 2.874% | 2.825% | **-0.000%** | -1.55 |

随机信号的超额在所有周期上都精确为 0.000%——对照池与留一剔除的实现正确，
门槛不会因对照口径偏差而产生假阳性。门槛判定也如预期**未通过**（超额不 > 0）。

### 18.7 命令

```bash
# 研究 v1 入场信号（R-1 的第一步）
.venv/bin/python scripts/event_study.py --start 2016-01-01 --end 2024-12-31 \
  --events trend_pullback_entry --primary-horizon 10 --out reports/event-study

# 阴性对照（校准门槛）
.venv/bin/python scripts/event_study.py --start 2018-01-01 --end 2024-12-31 \
  --events random --primary-horizon 5

# 免 CSV 缓存：直接从 PostgreSQL 读行情
.venv/bin/python scripts/event_study.py --bars-url "$SWING_BARS_DATABASE_URL" \
  --bars-schema swing_bars --adj-source tushare --events trend_pullback_entry
```

### 18.8 边界

1. 市值缓存 `data/cache/market_caps` 当前覆盖 **2017-05-26 ~ 2024-12-31**；更早（2016–2017-05）
   无 `circ_mv`，这些日子的事件会被跳过，报告须披露；
2. 持有期内停牌/退市导致缺退出日行情的样本被剔除（轻微乐观），报告单独给出剔除数；
3. 退出按收盘价成交，不模拟跌停无法卖出；不含冲击成本与容量约束（与 fund-analysis 一致）；
4. **本模块只回答「入场信号有没有 alpha」，不构成任何采纳结论**——采纳仍须走
   §3.2 的确认段一次 + 前向预注册。

### 18.9 工具冒烟（**非研究结论**）

为验证 CLI 与两个事件定义都能跑通，在 **400 只**（随机取前 400 只，非全市场）与
2018–2024 上各跑了一遍。结果仅用于确认工具正确性，**不构成任何 R-1 采纳证据**：

| 事件 | 样本 | 聚类 | h=10 超额 | 聚类 t | 去 Top5 | 扣费后 | 门槛 |
|---|---|---|---|---|---|---|---|
| `trend_pullback_entry`（v1 入场） | 201 | 95 | +0.079% | 0.14 | -0.644% | -0.252% | **未通过**（t、去右尾、净收益三项不过） |
| `random`（阴性对照） | 5,476 | 14 | **0.000%** | 1.10 | -0.079% | -0.584% | 未通过（预期） |

两点观察（不作为结论）：

1. 阴性对照在 1/3/5/10/20 全部五个周期上的超额都精确为 `0.000%`，说明对照池与留一剔除
   在全市场尺度上同样正确；
2. v1 入场信号在 400 只样本上的超额为正但 t 仅 0.14、去掉最好的 5 笔后转负——这与 N3
   的既有结论（右尾依赖、无稳定 alpha）方向一致，但**样本量只有全市场的 12%**，
   必须全量重跑才能下判断。

> 全量试跑（需全市场市值缓存与完整预热期）应作为 R-1 预登记后的第一步，而不是现在做：
> §3.2 要求先用书面假设登记锁定「试了几个口径」，再跑，否则这次试跑本身就会消耗试验额度。

---

---

## 19. I-4 结果落库（剩余 8 张表，2026-10-31）

### 19.1 目的

§15 只落了「输入侧」两张表（行情 + 抓取溯源）。本步补齐「输出侧」，使回测产物可查询、
可跨机器复核，并作为 §3.2 试验账本（≤200 次预算）的机器可读底账。

### 19.2 表结构

表名沿用 §15.1 已确立的 `swing_` 前缀，与 V3.0 §5.2 清单的对应关系：

| V3.0 §5.2 | 实际表 | 主键 | 说明 |
|---|---|---|---|
| `bars_daily` | `swing_bars_daily` | `(code, trade_date)` | §15 已落 |
| `pool_daily` | `swing_pool_daily` | `(trade_date, code)` | **运行无关的共享表**，见 §19.3 |
| `daily_signals` | `swing_daily_signals` | `(run_id, signal_date, code)` | 信号（含未通过的） |
| `orders` | `swing_orders` | `(run_id, order_id)` | 委托（含 reject 与原因） |
| `fills` | `swing_fills` | 自增 `id` + `(run_id, order_id)` 索引 | 引擎侧无天然唯一键 |
| `positions_daily` | `swing_positions_daily` | `(run_id, trade_date, code)` | 每日持仓 |
| `portfolio_daily` | `swing_portfolio_daily` | `(run_id, trade_date)` | 每日净值/现金/回撤 |
| `trade_records` | `swing_trade_records` | `(run_id, trade_id)` | 已平仓交易 |
| `backtest_runs` | `swing_backtest_runs` | `run_id` | 元数据 + 绩效指标（含 params/metadata 全量 JSON） |

`swing_backtest_runs` 额外抽出可索引的标量列（`total_return`/`sharpe`/`max_drawdown`/
`n_trades`/`win_rate`/`profit_factor`/`git_commit`/`data_hash`），同时保留
`params_json`/`metadata_json`/`performance_json`/`trade_metrics_json` 全量快照。

### 19.3 `swing_pool_daily` 为什么不分 run

股票池（`in_pool`/`reason`）是「行情 + `PoolConfig`」的**纯函数**，与策略参数、区间、AUM 无关。
若按 run 存，3300 只 × 1900 日 ≈ 630 万行/次，每次回测重写一遍既浪费又无信息量。
因此它与 `swing_bars_daily` 同属数据层，由 `scripts/ingest_swing_bars.py --with-pool`
一次性落库（按日期区间先删后写，幂等），各 run 直接按日期关联即可。

### 19.4 接入方式

落库是**可选且失败不阻断**的：未配置 URL 时只写 CSV/JSON（研究阶段的主产物）。

```bash
# 配置后自动落库（两者都未设则不落库）
export SWING_RESULTS_DATABASE_URL="$SWING_BARS_DATABASE_URL"
export SWING_RESULTS_SCHEMA=swing_bars

# 或按次显式指定
.venv/bin/python scripts/run_backtest.py ... --results-url "$URL" --results-schema swing_bars
.venv/bin/python scripts/mvp_edge_check.py ... --results-url "$URL" --results-schema swing_bars

# 股票池（共享表，一次性）
.venv/bin/python scripts/ingest_swing_bars.py --cache-dir data/cache/swing_bars_full --with-pool
```

`run_metadata.json` 会追加 `results_db` 字段记录各表写入行数，便于与 CSV 行数核对。

### 19.5 验证

| 项 | 结果 |
|---|---|
| 单元测试 | `tests/unit/test_results_store.py` 11 项全过（含 9 表建表、幂等重写、读回、删 run、env 解析优先级） |
| `run_backtest.py` 冒烟 | 20 只 × 2023–2024：`portfolio_daily` 484 行与 `daily_equity.csv` 行数一致、末值一致 |
| `mvp_edge_check.py` 端到端 | 300 只 × 2023–2024：signals 29 / orders 56 / fills 55 / trades 27 / positions 123 / equity 484，**6 张表与对应 CSV 行数逐表相等** |
| 幂等 | 同一 `run_id` 重跑：先按 `run_id` 删除旧行再写，行数不累积；行集合变短时旧行不残留 |

### 19.6 边界

1. `diagnostics` 表未单独建（列随策略条件变化，属动态 schema），当前仍只写 CSV；
   若将来要 SQL 侧按条件归因，需先固定条件列集合；
2. 结果库不存行情副本，跨库复核仍依赖 `swing_bars_daily` 或 `data_hash`；
3. `swing_*` 的备份/分区策略与 §15.4 同（当前单表规模下无性能压力，按年分区属可选优化）。
