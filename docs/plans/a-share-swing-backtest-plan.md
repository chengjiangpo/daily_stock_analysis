# A 股短线波段选股与回测系统 · 可执行实施方案

| 项 | 值 |
|---|---|
| 版本 | V2.0（原方案 V1.0 的可执行化重写） |
| 生效日期 | 2026-10-26 |
| 适用市场 | A 股（沪深主板 → 创业板/科创板 → 北交所分批复用） |
| 主策略 | 趋势回调再启动 |
| 对照策略 | 箱体突破、基准动量、匹配基准组合 |
| 所在仓库 | `daily_stock_analysis` |
| 文档类型 | 实施方案（可直接据此开发与验收） |

---

## 0. 本文档怎么用

1. **本文档自包含**：不依赖原 V1.0 的表述，直接给出可判定的规则、阈值、字段、判据和验收标准。V1.0 章节的去向见 [附录 E](#附录-e原方案章节去向索引)；V1.0 的可行性评审与依据见 [a-share-swing-backtest-plan-review.md](./a-share-swing-backtest-plan-review.md)。
2. **§10 MVP 是第一步**：在写完整引擎之前必须先跑通 2 周垂直切片，并过 go/no-go。未通过就停止，不进入后续阶段。
3. **所有阈值都是配置项，不是代码常量**；唯一一次冻结发生在通过 MVP 之后（§5.5）。
4. **任何规则变更必须走试验账本**（§5.5），不允许「先看结果再改条件」。

---

## 1. 目标与边界

### 1.1 目标

1. 每日收盘后自动筛选候选股，输出可解释的条件诊断；
2. 用可复现的组合级回测评估策略，给出**相对匹配基准的 alpha**；
3. 在冻结参数与代码的前提下做滚动前推与样本外验收；
4. 通过验收后进入模拟实盘，比对回测与真实成交差异。

### 1.2 明确不做（第一阶段）

- 自动实盘下单；逐笔/盘口预测；AI 涨跌概率预测；
- 无约束参数暴力搜索；根据回测结果自动改规则；
- 分钟级策略（仅在 §6.5 实验组中用盘后固定价，不引入分钟撮合）。

### 1.3 三条硬约束（项目的成败判据）

| 约束 | 数值 | 来源 |
|---|---|---|
| 成本门槛 | 每笔**毛超额 ≥ 0.6%**（净 10% 年化所需） | §6.3 |
| 统计功效 | 单窗口 6 个月无法判定有效性；验收看拼接后 ≥4 年 / ≥300 笔 | §8.3 |
| alpha 定义 | 必须相对**同市值同波动匹配组合**，而不是宽基指数 | §5.3 |

三条任一不满足，项目应停止或降级为「研究工具」，不得进入模拟实盘。

---

## 2. 与现有仓库的衔接

### 2.1 复用（不重写）

| 现有资产 | 用途 | 注意 |
|---|---|---|
| `data_provider/base.py` `DataFetcherManager.get_daily_data()` | 日线获取 + 多源降级 | 已含重试、限频、清洗、指标计算 |
| `data_provider/baostock_fetcher.py` | 日线 | 源支持 `isST`/`tradestatus`，但现有 `fields` 未取，需扩展；复权**不含分红**，见 §4.4 |
| `data_provider/tushare_fetcher.py` | `adj_factor`/`stk_limit`/`suspend_d`/成员表 | 需 2000 分档 |
| `src/core/trading_calendar.py` | 交易日历 | 窗口生成统一走它 |
| `src/storage.py` `DatabaseManager` | SQLAlchemy + 会话 | 新表挂同一 Base |
| `src/repositories/*` | 仓储层风格 | 新增 `strategy_repo.py`、`sim_repo.py` |

### 2.2 新建（不复用 `backtest_engine.py`）

`src/core/backtest_engine.py` 是**推荐意见评估器**（判断 LLM 建议的方向对错、是否触发止损止盈），不是组合级撮合引擎。二者语义不同，**不要合并、不要改动它的行为**。

| 新增模块 | 职责 |
|---|---|
| `src/strategy/` | 信号计算、条件诊断、评分 |
| `src/sim/` | 撮合、费用、持仓、组合、净值 |
| `src/validation/` | 切分、滚动、敏感性、统计检验 |
| `src/services/backtest_run_service.py` | 编排一次完整回测运行 |
| `scripts/run_backtest.py` | CLI 入口（`--strategy --period --cost-tier --out`） |

### 2.3 不碰

`apps/`、`bot/`、`api/v1/` 的现有行为；本次只新增后端模块与文档。若后续要暴露 API，按 `api/v1/router.py` 现有模式追加。

---

## 3. 总体架构

```
数据层    raw_bars → adjusted_bars(视图) → status/limit/rules → 时点股票池
            ↓
策略层    trend/pullback/volume/restart 四组布尔条件 + 诊断 + 评分
            ↓
撮合层    T+1 开盘成交判定（涨停/停牌/参与率）→ 费用 → 成交回报
            ↓
组合层    资金分配、风险预算、并发持仓、行业/风格上限
            ↓
验证层    时间切分、滚动前推、成本三档、敏感性、bootstrap、DSR/PBO
            ↓
报告层    run_metadata / equity / orders / fills / trades / metrics / 诊断
```

目录约定（遵循 `AGENTS.md` 的目录边界）：

```
src/strategy/{base,filters,trend_pullback,box_breakout,momentum,scoring,diagnostics}.py
src/sim/{clock,matcher,fees,portfolio,execution,equity}.py
src/validation/{split,walk_forward,sensitivity,stats}.py
src/repositories/{strategy_repo,sim_repo}.py
tests/{unit,integration}/...
tests/fixtures/synthetic_bars/*.csv
```

---

## 4. 数据层

### 4.1 数据源分工与降级链

| 数据 | 主源 | 备源 | 缺失处理 |
|---|---|---|---|
| 日线 OHLCV | BaoStock（含 `isST`/`tradestatus`） | TuShare `daily` | 两源都缺 → 该股当日剔除，记 `data_gap` |
| 复权因子 | TuShare `adj_factor` | 自算（除权除息方案） | 无法解释 → 隔离 |
| 涨跌停价 | TuShare `stk_limit` | 按 §4.2 规则自算 | 自算为默认，`stk_limit` 仅作交叉校验 |
| 停牌 | BaoStock `tradestatus` | TuShare `suspend_d` | 区分「停牌」与「缺数据」 |
| 证券状态/退市 | TuShare `stock_basic(list_status='D')` | BaoStock `query_stock_basic(status,outDate)` | 缺失 → 该股不进入历史池 |
| 行业历史成员 | TuShare `index_member_all` | 当前行业（标注局限） | 无历史则报告中声明 |
| 指数 | TuShare `index_daily` | BaoStock | — |

> **硬要求 1**：日线、状态、涨跌停价三者必须来自「同一时点可解释」的口径；不允许用今天的股票列表回溯历史池（见 §4.6）。
>
> **硬要求 2**：现有 `BaostockFetcher._fetch_raw_data()` 的 `fields` 仅 `date,open,high,low,close,volume,amount,pctChg`，需补 `preclose,isST,tradestatus,turn` 后才能满足 §4.5/§4.6；该 fetcher 明确不支持北交所，北交所需改用其他源。

### 4.2 分段规则配置（`config/market_rules.yaml`）

按日期区间配置，禁止硬编码单一比例。

| 规则 | 取值 | 生效区间 |
|---|---|---|
| 主板普通涨跌幅 | 10% | 全历史 |
| 主板 ST/*ST | 5% | …～2026-07-05 |
| 主板 ST/*ST | **10%** | 2026-07-06～ |
| 创业板 | 10% | …～2020-08-23 |
| 创业板 | 20%（ST 亦 20%） | 2020-08-24～ |
| 科创板 | 20%（ST 亦 20%） | 2019-07-22～ |
| 北交所 | 30% | 2021-11-15～ |
| 主板新股首日 | 有效申报 +44%/−36%（非无限制） | …～2023-04-09 |
| 主板注册制新股 | 前 5 日不设限 | 2023-04-10～ |
| 创业板/科创板新股 | 前 5 日不设限 | 各自开板起 |
| 北交所新股 | 首日不设限 | 2021-11-15～ |
| 退市整理期 | 15 个交易日；首日不设限，其后主板 10%、双创 20% | 全历史 |

**涨跌停价计算**（`src/sim/matcher.py` 必须用 `Decimal`）：

```python
limit_up  = quantize_half_up(pre_close * (1 + pct), 0.01)
limit_down = quantize_half_up(pre_close * (1 - pct), 0.01)
# 除权日 pre_close 用除权参考价；结果 < 0.01 时取 0.01
```

禁止 `round()`（银行家舍入）——边界股票会系统性错一个价位。

### 4.3 费用分段（`config/fees.yaml`）

| 费项 | 取值 | 生效区间 | 方向 |
|---|---|---|---|
| 佣金 | 0.025%（默认），最低 5 元 | 全历史（可配） | 双向 |
| 印花税 | 0.1% | …～2023-08-27 | 卖出 |
| 印花税 | 0.05% | 2023-08-28～ | 卖出 |
| 过户费 | 0.001% | 2022-04-29～ | 双向 |
| 过户费（沪） | 0.002% | …～2022-04-28 | 双向 |
| 经手费 | 0.00487% | …～2023-08-27 | 双向 |
| 经手费 | 0.00341% | 2023-08-28～ | 双向 |
| 证管费 | 0.002% | 全历史 | 双向 |
| 滑点 | 1 tick（基础）/ 2 / 3 tick（压力） | 配置 | 双向 |

`tick = 0.01 元`。滑点按 tick 数建模，不按固定百分比——10 元股 1 tick = 0.1%，5 元股 = 0.2%，20 元股 = 0.05%。

### 4.4 复权三口径（最容易出错的一节）

| 用途 | 口径 | 禁止 |
|---|---|---|
| 信号计算 | 后复权序列（或复权因子） | **禁止前复权**（锚定当前价，含未来信息） |
| 成交模拟 | 不复权真实价 | 禁止用复权价当成交价 |
| 持仓/收益 | 不复权价 + 独立现金流账 | 禁止用复权价算收益后又记分红 |

**源间口径差异（会导致策略比较失效）**：

| 源 | 口径 | 直接算收益的后果 |
|---|---|---|
| TuShare `pro_bar` | 分红再投 | 已隐含分红再投资；再记现金分红 = 重复计算 |
| BaoStock | 涨跌幅复权法，不参加分配 | 漏掉现金分红 |
| AkShare（东财 qfq/hfq） | 未披露 | 使用前必须先校准 |

> **实测修正（2026-10-26）**：阶段 -1 实测显示 BaoStock `adjustflag=1` 后复权**包含现金分红**，与上表「不参加分配、漏掉分红」的表述相反；证据见 [a-share-swing-data-probe.md](./a-share-swing-data-probe.md) §3。因此「后复权算信号 + 不复权算成交 + 分红单独入账」仍然成立，但持仓期现金分红必须在组合层补记，否则收益低估。

**落地规则**：

1. 信号一律在复权序列上计算；
2. 阈值（止损价、止盈价、均线）在复权序列上算出，**当日换算回真实价**：`real = adj / factor_ratio`；
3. 除权日必须重算（因子跳变当日，阈值按新因子重算，不沿用昨日真实价）；
4. 持仓调整：送股/转增按比例改 `quantity`，现金分红单独入 `cash_dividend` 账；
5. 断言：相邻除权日的因子跳变必须能被该股分红送转方案解释，否则隔离。

### 4.5 数据质量校验（每次导入都跑）

| # | 校验 | 失败处理 |
|---|---|---|
| 1 | `(code, trade_date)` 唯一 | 拒绝导入 |
| 2 | `low ≤ open,close ≤ high` 且均 > 0 | 隔离该行 |
| 3 | 成交量/成交额非负，且「量=0 则额=0」 | 隔离 |
| 4 | 交易日历完整（区间内无空洞） | 拒绝导入 |
| 5 | 复权因子单调跳变可解释 | 隔离该股 |
| 6 | 停牌日与缺数据可区分（`tradestatus`） | 标记 `data_gap` |
| 7 | 涨跌幅与当日规则一致（允许 0.5bp 容差） | 告警 + 抽检 |
| 8 | **每日池成分数 vs 交易所历史上市公司数**偏差 > 2% | 告警（防存活偏差） |
| 9 | 无未来信息：信号日字段全部 ≤ 信号日 | 单测断言 |
| 10 | 数据快照哈希写入 `run_metadata` | 必填 |

### 4.6 时点股票池（防存活偏差）

每日重新计算，条件全部基于「当日已知」：

```text
include if:
  board in {主板}                       # 阶段 1 先只做主板
  hist_listed_days >= 120               # 上市满 120 个交易日
  not is_st                              # 当日 isST
  tradestatus == 1                       # 当日正常交易
  amount_20d_mean >= 50_000_000          # 20 日均额 ≥ 5000 万
  close >= 2.0                           # 排除极低价股（tick 成本过高）
  not in 退市整理期
```

**退市股必须保留**：`stock_basic(list_status='D')` 的股票在其退市前区间必须进入池。若免费源取不到退市股日线，**MVP 结论视为不可信**，需先解决（见 §14 风险 R1）。

---

## 5. 策略层

### 5.1 主策略：趋势回调再启动（冻结配置，基线 v1）

所有条件在复权序列上计算，`T` = 信号日，信号在 `T` 收盘后生成，`T+1` 开盘执行。

**A. 趋势过滤（全部满足）**

| 条件 | 表达式 |
|---|---|
| 收盘在 MA20 上 | `close > MA20` |
| 中期多头 | `MA20 > MA60` |
| 长期未走坏 | `(MA60 - MA60.shift(10)) / MA60.shift(10) >= -0.02` |
| 中期收益非负 | `(close / close.shift(20) - 1) >= -0.05` |
| 未过度偏离 | `close / MA20 - 1 <= 0.08` |

**B. 回调识别（全部满足）**

| 条件 | 表达式 |
|---|---|
| 波段高点 | `swing_high = max(high[T-15:T])`（不含 T） |
| 回调幅度 | `0.04 <= (swing_high - close) / swing_high <= 0.12` |
| 低点未崩 | `min(low[T-15:T]) >= swing_high * 0.85` |
| 未破中期结构 | `close >= MA60 * 0.98` |

**C. 回调质量（全部满足）**

| 条件 | 表达式 |
|---|---|
| 缩量 | `0.50 <= mean(vol[T-3:T]) / mean(vol[T-13:T-3]) <= 0.90` |
| 无放量长阴 | 回调期内不存在 `(close/open - 1) <= -0.06 且 vol > 1.5 * vol_ma20` |
| 无连续跳空下杀 | 回调期内累计向下跳空 < 2 次（`low > prev_low` 判据见诊断） |

**D. 再启动（全部满足）**

| 条件 | 表达式 |
|---|---|
| 收涨 | `close > prev_close` |
| 突破局部高点 | `close > max(high[T-4:T])` |
| 放量 | `vol > 1.2 * mean(vol[T-13:T-3])` |
| 收在高位 | `(close - low) / (high - low) >= 0.60`（`high == low` 时该条件判 false） |
| 涨幅合理 | `0.005 <= close/prev_close - 1 <= 0.05` |
| 非一字涨停 | `not (open == high == low == close == limit_up)` |

> 若 `close == limit_up`（收盘涨停但非一字），**允许入选但单独分组统计**，成交按 §6.2 判定。

**E. 入场**

| 规则 | 值 |
|---|---|
| 执行时点 | `T+1` 开盘 |
| 追高上限 | `T+1 open / T close - 1 <= 0.03`，超过放弃 |
| 不成交情形 | 停牌、`T+1 open == limit_up`、数据异常、资金不足、风险上限 |
| 成交价 | `open + slippage_ticks * 0.01`（买入方向） |

**F. 退出（主规则：一律「收盘信号 → 次日开盘执行」）**

| 优先级 | 规则 | 说明 |
|---|---|---|
| 1 | 硬止损 | 收盘价 ≤ `stop_price` → 次日开盘卖 |
| 2 | 止盈 | 收盘价 ≥ `entry + 2R` → 次日开盘卖 |
| 3 | 趋势破坏 | 收盘价 < `MA10` → 次日开盘卖 |
| 4 | 时间退出 | 持有 ≥ 8 个交易日 → 次日开盘卖 |

- `R = entry_price - stop_price`
- `stop_price` 主规则（结构止损）：`max(min(low[T-15:T]) * 0.995, entry * 0.92)`，且不高于 `entry * 0.97`（保证 R 在 3%–8%）
- **盘中止损（`low ≤ stop` 当日成交）只作实验组**，不进主规则：多数券商条件单有延迟，散户无法稳定复制该执行假设
- 同日多条件触发时按上表优先级；开盘跳空穿越止损价，按开盘价 + 滑点成交，不按止损价成交

### 5.2 对照策略：箱体突破（同一套撮合与资金规则）

| 条件 | 表达式 |
|---|---|
| 箱体 | `box_high = max(high[T-10:T])`，`box_low = min(low[T-10:T])`（不含 T） |
| 振幅 | `(box_high - box_low) / box_low <= 0.10` |
| 无漂移 | `abs(close[T-1] / close[T-10] - 1) <= 0.08` |
| 突破 | `close > box_high` |
| 放量 | `vol >= 1.5 * mean(vol[T-10:T])` 且 `vol <= 5 * mean(vol[T-10:T])` |
| 收在高位 | `(close - low)/(high - low) >= 0.7` |
| 涨幅合理 | `0.01 <= close/prev_close - 1 <= 0.07` |
| 趋势不向下 | `close > MA20` 且 `MA20 >= MA60 * 0.98` |

入场、退出、成本、仓位与主策略**完全一致**，否则比较的是资金管理而非信号。

### 5.3 基准（三件套，缺一不可）

| 基准 | 定义 | 作用 |
|---|---|---|
| B1 宽基 | 沪深 300 / 中证 1000 / 中证全指 | 市场 beta |
| B2 匹配组合 | 每日按「同市值分位 + 同波动分位 + 同行业」从池中随机抽 5 只等权，蒙特卡洛 200 次取中位数 | **真正的 alpha 基准** |
| B3 动量 | `close > MA20` 且 20 日收益排名前 10%，等权持有 5 只 | 判断复杂规则是否有增量 |

**收益归因**：对每笔交易做 `size / volatility / industry / momentum / turnover` 五因子回归，输出暴露与残差 alpha。

### 5.4 条件诊断输出

每个条件统一结构，落表 `signal_diagnostics`：

```text
condition_name, passed, observed_value, threshold_low, threshold_high,
failure_reason, rule_version
```

入选股额外输出：趋势强度、回调幅度、缩量比、启动强度、距止损空间、预期持有天数。**不输出未校准的概率数字。**

### 5.5 参数实验账本（防多重检验）

| 项 | 规则 |
|---|---|
| 主配置冻结 | 通过 MVP 后，`strategy_definitions` 写入 v1 主配置并加锁（`frozen_at`） |
| 实验预算 | 阶段 4 前总试验次数 ≤ 200，超预算需书面说明 |
| 每试验必记 | `experiment_id, strategy_version, parameter_set, periods, cost_model, code_commit, metrics, decision, decision_reason` |
| 禁止 | 看结果后改规则再声称「样本外」；同一测试区间重复计算收益 |
| 统计 | 试验数 > 20 时，报告必须附 DSR；> 50 时附 PBO |

评分（同日多信号抢资金时使用，**禁止用未来收益排序**）：

```text
score = 0.3*norm(缩量程度) + 0.3*norm(启动强度) + 0.2*norm(回调深度适配度)
      + 0.2*norm(距止损空间/R)
```

评分公式本身也是一个自由参数，必须纳入 §8.4 的敏感性测试。

---

## 6. 撮合与成本（引擎核心）

### 6.1 日循环（严格顺序，禁止重排）

```
09:00  载入账户快照（昨日收盘后的现金/持仓/可卖数量）
09:00  应用公司行为：送转改股数、现金分红入账、除权日重算阈值
09:25  判断 T 日开盘成交（用当日 open/limit/停牌状态）
09:25  执行卖出单（昨日收盘信号产生的）
09:25  执行买入单（资金 = 昨日可用现金，不含当日卖出所得）
15:00  用当日 close 计算净值、持仓估值（停牌用最后可得价）
15:30  计算当日指标，生成 T+1 候选信号
15:35  落库：orders / fills / positions / equity / signals
```

两条显式约束：

1. **当日买入的股票当日不可卖出**（`sellable_quantity` 初始为 0）；
2. **当日卖出所得不用于当日开盘买入**（券商预检查不可依赖）。这条是保守假设，必须写进报告。

### 6.2 成交判据（日线可判，写死）

| 场景 | 判据 | 结果 |
|---|---|---|
| 一字涨停 | `open == high == low == close == limit_up` | 买不到 |
| 开盘涨停 | `open == limit_up` | 买不到 |
| 收盘涨停（非一字） | `close == limit_up` 且 `low < limit_up` | 可买，单独分组统计 |
| 一字跌停 | `high == low == close == limit_down` | 卖不出 |
| 跌停但盘中有成交 | `low == limit_down` 且 `high > limit_down` | **可卖**（常见错误：误判为卖不出） |
| 停牌 | `tradestatus == 0` | 不能买卖，净值沿用最后价 |
| 开盘跳空穿越止损 | `open < stop_price` | 按 `open - slippage` 成交 |
| 参与率超限 | `order_value > min(0.02*amount, 0.02*volume*price)` | 部分成交（按上限），余量取消 |

**参与率上限**：`min(当日成交额 × 2%, 当日成交量 × 2%)`，且不超过该股 20 日均额的 2%。这是容量约束，缺失会让一字板/小盘股收益虚高。

### 6.3 成本模型（三档）

| 档 | 佣金 | 滑点 | 用途 |
|---|---|---|---|
| 基础 | 0.025%，最低 5 元 | 1 tick | 主报告 |
| 较高 | 0.03% | 2 tick | 稳健性 |
| 压力 | 0.05% | 3 tick | 是否仍有正期望 |

**成本倒推**（必须写进报告首页）：

| 目标净年化 | 5 日持仓(50 轮) | 7 日(36 轮) | 10 日(25 轮) |
|---|---|---|---|
| 净 10% | 毛 0.51%/笔 | 0.59% | 0.71% |
| 净 20% | 毛 0.71%/笔 | 0.87% | 1.11% |

（当前费率 + 1 tick 滑点 ≈ 0.313%/轮；历史印花税口径 ≈ 0.363%/轮。）

### 6.4 净值与持仓

- 每日收盘估值，停牌用最后可得收盘价；
- 现金分红单独入账，不从复权价重复取；
- 净值 = 现金 + Σ(股数 × 收盘价)；
- 气隙：`equity_curve` 必须能由 `fills + positions + 公司行为` 完全重建（§12 一致性测试）。

### 6.5 实验组：收盘价成交（2026-07-06 起新选项）

盘后固定价格交易已由双创扩至全部 A 股（15:05–15:30，按收盘价，时间优先）：

| 项 | 规则 |
|---|---|
| 用途 | 独立实验组，与「T+1 开盘」对照，不替代主规则 |
| 容量约束 | 单笔 ≤ 该股当日盘后固定价成交量的 10%（该数据可能取不到 → 用当日成交额的 0.5% 保守替代） |
| 报告 | 单列成交率与容量估计；若纳入后收益主要来自该组，说明主策略不可扩容 |

---

## 7. 组合与风险预算（补齐原方案的空白数值）

| 项 | 基线值 | 说明 |
|---|---|---|
| AUM | 30 万元 | 低于 10 万会被 5 元最低佣金显著抬高成本 |
| 最大持仓数 | 5 | |
| 单票上限 | 25% 净值 | |
| 总仓位上限 | 95% | 留 5% 现金 |
| 单笔风险预算 | 1.0% 净值 | `qty = floor(risk_budget / R / 100) * 100` |
| 单日新建仓 | ≤ 2 | |
| 行业集中度 | ≤ 2 只 / 单一申万一级 | |
| 风格暴露 | 小市值分位 ≤ 0.7 的持仓占比 ≤ 60% | 防「一篮子微盘」 |
| 最大回撤熔断 | 净值回撤 > 12% → 新开仓减半；> 18% → 停止新开仓 | |
| 连续亏损 | 连续 5 笔亏损 → 单笔风险降至 0.5% | |

---

## 8. 数据集划分与验证

### 8.1 时间切分（2026-10 视角，已重划）

| 区间 | 用途 | 备注 |
|---|---|---|
| 2016-01-01 ～ 2022-12-31 | 开发集 | 含 2018 熊市、2020 结构牛 |
| 2023-01-01 ～ 2024-12-31 | 验证集 | 含 2024-01 微盘股流动性危机 |
| 2025-01-01 ～ 2026-09-30 | 封存样本外 | 21 个月；**2026-07-06 制度断点须单列** |
| 2026-10-01 起 | 前向观察 | 模拟实盘 |

> 若 2025–2026 区间已被查看过，它不再是样本外，必须重新划界或改用前向数据。

### 8.2 滚动前推

| 参数 | 值 |
|---|---|
| 开发窗 | 3 年 |
| 验证窗 | 1 年 |
| 测试窗 | 6 个月 |
| 步进 | 6 个月 |
| 约束 | `dev_end < val_start < test_start`，测试窗不重叠 |
| 输出 | 拼接净值曲线 + 逐轮元数据（参数、成本档、commit、数据哈希） |

### 8.3 样本量与功效（写进报告，防止误判）

| 检验目标 | 所需样本 |
|---|---|
| 6 个月窗口判断年化夏普 0.8 | t = 0.55 → **无判定力** |
| 期望 t = 2（约 50% 功效） | 6.2 年 |
| 80% 功效 | 约 12 年 |
| PF 1.2（单笔信噪比 0.09） | 约 400–500 笔 |

因此：

- **单窗口只判执行异常与灾难亏损**（回撤超预算、成交率异常、无法退出）；
- 有效性只看**拼接后的 ≥4 年样本外**，且笔数 ≥300；
- 「2/3 窗口为正」仅作描述性统计（零假设误通过 14.5%–34%，真有效时误杀 23%–41%，不能作门槛）。

### 8.4 验收标准（V2，可复现）

**A. 执行层（每轮回测必过）**

| 项 | 标准 |
|---|---|
| 数据质量 | §4.5 十条全通过 |
| 未来函数 | 单测：信号日字段 `max(date) <= T` |
| 复现性 | 相同数据哈希 + 参数 + commit 重跑，净值逐日一致（容差 1e-9） |
| 账务一致性 | `equity` 可由 `fills + positions + 分红` 重建 |
| 成交合理性 | 无一字板成交；未成交原因分布可解释 |
| 容量 | 报告含 AUM=30 万/100 万/500 万三档容量估算 |

**B. 有效性层（仅看拼接后样本外）**

| 指标 | 门槛 |
|---|---|
| 相对 B2 匹配组合的年化 alpha | > 0，且 bootstrap 95% CI 下界 > 0 |
| 信息比率（IR） | ≥ 0.5 |
| Profit Factor | ≥ 1.2（压力成本档） |
| 最大回撤 | ≤ 15% |
| 分年度超额为正 | ≥ 2/3（描述性） |
| 五因子残差 alpha | > 0（不能被 size/vol 解释） |

**C. 结论分级**

| 级别 | 条件 |
|---|---|
| 不通过 | alpha 不显著为正 / 主要收益来自一字板或不可成交价 / PF < 1.1 |
| 待观察 | alpha 为正但 CI 下界 < 0，或笔数 < 300 |
| 进入模拟实盘 | A+B 全通过，参数与代码冻结 |

### 8.5 统计方法

- 区间 bootstrap（按交易日区块，块长 20 日）估计 alpha、IR、最大回撤的 CI；
- 策略配对比较：同一测试期、同资金、同成本，比较净收益差与回撤差；
- 试验数 > 20 时计算 DSR；> 50 时计算 PBO。

---

## 9. 模拟实盘（阶段 6）

| 项 | 要求 |
|---|---|
| 最短时长 | 12 周 |
| 最短样本 | ≥80 笔，且期间至少覆盖一次指数 ≥5% 回撤 |
| 每日记录 | 信号、理论入场价、模拟成交价、可成交性、滑点、费用、退出原因、理论与实际差异 |
| 通过条件 | 成交率偏差 ≤ 10pp；滑点中位数 ≤ 模型值 1.5 倍；信号数 ≥ 历史同期 60% |
| 失败处理 | 先定位原因（数据/撮合/信号），**禁止直接调参修复** |

---

## 10. MVP 垂直切片（阶段 0，2 周，go/no-go）

**目的**：在投入完整引擎之前，用最小成本回答唯一重要的问题——信号是否提供 ≥0.6%/笔的毛超额。

### 10.1 范围

```
BaoStock 日线（含退市股样本）→ 冻结主规则 v1 → T+1 开盘买入
→ 持有 8 日或触发退出 → 收益分布（相对 B2 匹配基准）
```

必须包含的三个保守过滤（否则结论无效）：

1. `open == limit_up` 不买；
2. `tradestatus == 0` 不买；
3. 20 日均额 < 5000 万不买。

### 10.2 判据

| 项 | 门槛 |
|---|---|
| 交易笔数 | ≥ 300（跨 2016–2024） |
| 每笔毛超额均值 | ≥ 0.6% |
| bootstrap 95% CI 下界 | > 0.3% |
| 扣基础成本后每笔净期望 | > 0 |

### 10.3 结论处理

| 结果 | 动作 |
|---|---|
| 全通过 | 冻结 v1，进入阶段 1（完整数据层） |
| 均值达标但 CI 下界不达标 | 扩大样本区间后重测一次；仍不达标 → 停止 |
| 不达标 | **停止完整系统开发**；保留代码作为研究工具 |

> 这一步是本文档对原 V1.0 最重要的顺序修改：把「先建完再验证」改成「先验证再建完」。

---

## 11. 分期实施计划

| 阶段 | 内容 | 产出 | 验收 | 人日 |
|---|---|---|---|---|
| 0 | MVP 切片 | `scripts/mvp_edge_check.py` + 报告 | §10.2 判据 | 6–8 |
| 1 | 数据层 | `bars/status/limit/rules` 表 + 校验 + 池 | §4.5 十条通过 | 15–25 |
| 2 | 策略层 | `src/strategy/*` + 诊断 + 选股报告 | 同参数可复现、逐条可解释 | 5–10 |
| 3 | 撮合/组合 | `src/sim/*` + 订单/成交/持仓/净值 | 人工用例全过 | 20–30 |
| 4 | 基础回测与对照 | 三策略 + 三基准 + 明细 | 结果可重建、账务自洽 | 8–12 |
| 5 | 验证与稳健性 | 切分/滚动/敏感性/成本/统计 | §8.4 B 层 | 15–20 |
| 6 | 模拟实盘 | 每日选股 + 模拟订单 + 偏差监控 | §9 | 持续 12 周 |

总计约 **70–105 人日**（不含模拟实盘观察期）。

### 11.1 需要新增的配置项（同步 `.env.example`）

```text
BACKTEST_AUM=300000
BACKTEST_COMMISSION_RATE=0.00025
BACKTEST_MIN_COMMISSION=5
BACKTEST_SLIPPAGE_TICKS=1
BACKTEST_MAX_POSITIONS=5
BACKTEST_MAX_POSITION_PCT=0.25
BACKTEST_RISK_PER_TRADE=0.01
BACKTEST_PARTICIPATION_CAP=0.02
BACKTEST_DATA_SNAPSHOT_DIR=./data/snapshots
BACKTEST_RULES_PATH=./config/market_rules.yaml
BACKTEST_FEES_PATH=./config/fees.yaml
```

---

## 12. 测试清单

### 12.1 单元测试（`tests/unit/`）

| 模块 | 用例 |
|---|---|
| 指标 | MA/ATR/缩量比/收盘位置；`high == low` 不除零 |
| 涨跌停价 | 10%/5%/20%/30%/ST 分段；`ROUND_HALF_UP` 边界（如 10.05×1.1=11.055→11.06） |
| 复权 | 除权日阈值换算；送转股数调整；分红入账 |
| 费用 | 印花税按日期切换；5 元最低佣金；tick 滑点 |
| 撮合 | 一字板买不到；跌停但 `high > limit_down` 可卖；跳空穿越止损按开盘价 |
| 组合 | 整手取整；现金不足；风控拒绝；最大持仓 |
| 净值 | 停牌估值；分红时点；回撤计算 |

### 12.2 集成测试（`tests/integration/`，合成数据）

`tests/fixtures/synthetic_bars/` 构造 6 个场景，每个场景断言逐日净值：

1. T 信号 → T+1 正常成交 → 8 日时间退出；
2. T+1 一字涨停 → 未成交；
3. 持仓期间停牌 3 日 → 净值冻结、复牌跳空结算；
4. 开盘跳空 −9% 穿越止损 → 按开盘价成交，记录跳空损失；
5. 同日 6 个信号、仅 2 个仓位 → 按 score 排序，资金不足者拒绝；
6. 持仓期间 10 送 3 + 派息 → 股数与现金同时正确。

### 12.3 一致性测试

- 相同 `data_hash + params + commit` 重跑，`daily_equity.csv` 逐位一致；
- 单票独立回测的成交与组合回测中该票成交一致；
- `orders → fills → positions → equity` 四表可相互核对；
- 策略参数不可跨策略共享（防意外污染）。

---

## 13. 报告与交付物

每次运行输出到 `reports/backtest/{run_id}/`：

```text
run_metadata.json        # 含数据快照哈希、commit、参数、成本档
daily_equity.csv
daily_positions.csv
orders.csv
fills.csv
trades.csv
signals.csv
diagnostics.csv
monthly_returns.csv
yearly_returns.csv
performance_metrics.json
cost_sensitivity.csv
parameter_sensitivity.csv
walk_forward_results.csv
attribution.csv          # 五因子暴露与残差 alpha
capacity.csv             # 30万/100万/500万
data_quality_report.json
```

报告首页固定顺序：**成本倒推 → 相对 B2 的 alpha 与 CI → 分年度稳定性 → 容量 → 限制与未覆盖项**。

---

## 14. 风险登记册

| # | 风险 | 影响 | 应对 | 状态 |
|---|---|---|---|---|
| R1 | 退市股历史日线取不到 | 存活偏差，MVP 结论失效 | 优先实测 BaoStock；不行则购 TuShare 2000 分或换源 | **待实测（最高优先）** |
| R2 | `stk_limit` 历史起始不明（示例最早 2019） | 长回测无官方涨跌停价 | 自算 + 用 `high/low` 反向校验 | 待实测 |
| R3 | 2026-07-06 ST 5%→10% 断点 | 样本外与开发集分布不同 | 分段统计、单列 2026H2 | 已覆盖（§4.2） |
| R4 | 双源复权口径冲突 | 策略间比较失真 | 统一后复权 + 因子断言 | 已覆盖（§4.4） |
| R5 | 分钟数据缺失 | 「触板后打开」不可判 | 只用一字板保守判据，接受高估 | 已接受 |
| R6 | 盘后固定价成交量极小 | 实验组容量不足 | 单列容量估计 | 已覆盖（§6.5） |
| R7 | 盘中止损不可复制 | 回测优于实盘 | 主规则改为收盘信号 | 已修正（§5.1-F） |
| R8 | 多重检验（试验过多） | 最优结果由随机产生 | 试验账本 + DSR/PBO | 已覆盖（§5.5） |
| R9 | 策略 alpha 实为小市值/高波动暴露 | 风格切换即失效 | B2 匹配基准 + 五因子归因 | 已覆盖（§5.3） |
| R10 | 免费源接口不稳定（AkShare） | 数据中断 | 仅作补数，不入主链 | 已覆盖（§4.1） |

---

## 15. 与 V1.0 的关键差异（评审后修正项）

| 修正 | V1.0 | V2.0 |
|---|---|---|
| 实施顺序 | 阶段一→六顺序推进 | 先 2 周 MVP + go/no-go（§10） |
| 验收指标 | 绝对夏普 > 0.8 | 相对匹配基准 alpha + IR（§8.4） |
| 2/3 窗口规则 | 参考门槛 | 降为描述性统计（§8.3） |
| 样本外区间 | 2025 起 | 2025-01～2026-09，且单列制度断点 |
| 止损执行 | high/low 触发 | 收盘信号→次日开盘（§5.1-F） |
| 成交容量 | 无 | 参与率 2% 上限（§6.2） |
| 跌停判据 | 未定 | 一字板 `high=low=close`（§6.2） |
| ST 涨跌幅 | 隐含 5% | 分段，2026-07-06 起 10%（§4.2） |
| 收盘价成交 | 禁止 | 实验组（盘后固定价，§6.5） |
| 风险预算 | 无数值 | 补齐（§7） |
| alpha 基准 | 宽基指数 | + 同市值同波动匹配组合（§5.3） |
| 参数治理 | 「有限搜索」 | 冻结 + 试验预算 + 账本（§5.5） |

---

## 附录 A：主策略参数表（v1 基线）

```yaml
strategy: trend_pullback_restart
version: v1
trend:
  ma_fast: 20
  ma_slow: 60
  ma_slope_window: 10
  ma_slope_min: -0.02
  ret20_min: -0.05
  deviation_max: 0.08
pullback:
  swing_window: 15
  depth_min: 0.04
  depth_max: 0.12
  low_guard: 0.85
  ma60_guard: 0.98
volume:
  shrink_window: 3
  base_window: 10
  shrink_min: 0.50
  shrink_max: 0.90
restart:
  breakout_window: 4
  vol_mult: 1.2
  close_pos_min: 0.60
  ret_min: 0.005
  ret_max: 0.05
entry:
  gap_max: 0.03
exit:
  stop_mode: structural      # structural | fixed | atr（实验组）
  stop_buffer: 0.995
  stop_max_pct: 0.08
  stop_min_pct: 0.03
  take_profit_r: 2.0
  trend_exit_ma: 10
  max_hold_days: 8
```

## 附录 B：字段字典（核心表）

| 表 | 关键字段 |
|---|---|
| `bars_daily` | `code, trade_date, open, high, low, close, volume, amount, pre_close, is_st, tradestatus, limit_up, limit_down, adj_factor, source, data_version` |
| `stock_status_daily` | `code, trade_date, is_st, is_delisting, board, listed_days` |
| `pool_daily` | `trade_date, code, in_pool, reason` |
| `daily_signals` | `signal_date, code, strategy, rule_version, score, passed, entry_plan` |
| `signal_diagnostics` | `signal_date, code, condition_name, passed, observed_value, threshold_low, threshold_high, failure_reason` |
| `orders` | `order_id, run_id, strategy, code, signal_date, order_date, side, quantity, ref_price, fill_price, fill_qty, fees, slippage, status, reject_reason` |
| `positions_daily` | `run_id, trade_date, code, quantity, sellable_qty, avg_cost, close, market_value` |
| `portfolio_daily` | `run_id, trade_date, cash, market_value, equity, drawdown` |
| `trade_records` | `trade_id, run_id, code, entry_date, entry_price, exit_date, exit_price, r_multiple, fees, pnl, exit_reason, hold_days` |
| `backtest_runs` | `run_id, data_hash, code_commit, params_json, cost_tier, periods, created_at` |

## 附录 C：CLI 约定

```bash
# MVP 可行性切片
python scripts/mvp_edge_check.py --start 2016-01-01 --end 2024-12-31 \
  --strategy trend_pullback_restart --out reports/mvp/

# 单次回测
python scripts/run_backtest.py --strategy trend_pullback_restart \
  --start 2016-01-01 --end 2022-12-31 --cost-tier base --aum 300000

# 滚动前推
python scripts/run_backtest.py --walk-forward --dev 3y --val 1y --test 6m --step 6m

# 成本压力
python scripts/run_backtest.py --strategy box_breakout --cost-tier stress
```

## 附录 D：待实测清单（阶段 0 之前完成）

| 项 | 验证方法 | 影响 |
|---|---|---|
| BaoStock 退市股 K 线 | 取 3 只已退市股（不同年份）验证 | R1 |
| TuShare `stk_limit` 起始年 | 按年抽样查询 | R2 |
| 复权因子是否含分红 | 取一只高分红股，比对除权日跳变 | §4.4 |
| 盘后固定价成交量 | 抽样统计 30 只股票的该时段成交占比 | §6.5 |
| 北交所退市整理期规则 | 查交易所规则原文 | 仅影响北交所扩展 |

## 附录 E：原方案章节去向索引

| V1.0 章节 | 本文档 | 变化 |
|---|---|---|
| 一 目标与原则 | §1 | 保留，补三条硬约束 |
| 二 架构与技术栈 | §3、§2 | 改为与现有仓库衔接；Parquet/DuckDB → SQLAlchemy/现有 DB |
| 三 数据体系 | §4 | 补分段规则表、复权换算规则、存活偏差校验 |
| 四 趋势回调策略 | §5.1 | 阈值全部冻结；止损执行方式修正 |
| 五 箱体突破 | §5.2 | 补齐并强制与主策略共用撮合 |
| 六 信号诊断 | §5.4 | 结构保留，加 rule_version |
| 七 回测引擎 | §6 | 补成交判据、参与率、T+1 资金时序 |
| 八 组合级回测 | §6.1、§7 | 补数值与风格约束 |
| 九 数据集划分 | §8.1 | 区间按 2026-10 重划 |
| 十 滚动前推 | §8.2 | 保留，明确拼接与不重叠 |
| 十一 参数优化 | §5.5 | 补试验预算与冻结机制 |
| 十二 绩效评估 | §8.4、§13 | 指标改为相对基准 alpha/IR，补归因 |
| 十三 市场状态归因 | §5.3、§8.4 | 并入归因体系 |
| 十四 统计显著性 | §8.3、§8.5 | 补功效计算与双向误判 |
| 十五 验收标准 | §8.4 | 重写为执行层 + 有效性层 |
| 十六 模拟实盘 | §9 | 补最短样本与通过条件 |
| 十七 存储设计 | 附录 B | 落到现有 SQLAlchemy |
| 十八 报告设计 | §13 | 固定首页顺序 |
| 十九 自动化测试 | §12 | 全部具体化 |
| 二十 实施计划 | §10、§11 | 插入阶段 0（MVP） |
| 二十一 交付物 | §11、§13 | 与仓库目录对齐 |
| 二十二 实施原则 | §1.3、§10 | 保留精神，改为可判定 |
