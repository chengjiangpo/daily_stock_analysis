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
