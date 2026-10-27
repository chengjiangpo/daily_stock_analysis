# A 股短线波段 · 阶段 -1 数据源前置实测

- 实测日期：2026-10-26
- 方案依据：[a-share-swing-backtest-plan.md](./a-share-swing-backtest-plan.md) §4.4、§14、附录 D
- 任务依据：[a-share-swing-backtest-execution.md](./a-share-swing-backtest-execution.md) P0-01～P0-03
- 探测脚本：[scripts/probe_data_sources.py](../../scripts/probe_data_sources.py)
- 完整输出：[reports/data_probe/data_probe.json](../../reports/data_probe/data_probe.json)
- 环境：`.venv/bin/python`，Python 3.12.13、pandas 3.1.0、numpy 2.5.4、baostock 00.9.40、tushare 1.4.29

**证据标记**：

- `[实测]`：本次脚本真实 API 返回，数字可在 JSON 中复查。
- `[公告]`：公开分红/除权公告核对，链接见 §3。
- `[推断]`：由数值关系推导，但没有官方口径文档直接确认。
- `[未验证]`：本次没有取得证据，不作结论。

**复现命令**：

```bash
.venv/bin/python scripts/probe_data_sources.py --out reports/data_probe
```

脚本会 `bs.login()` / `bs.logout()`，对每个查询单独捕获异常，socket 超时默认 20 秒，不写任何 token。`reports/` 已被 `.gitignore` 覆盖，JSON 不进入版本控制。

---

## 0. 结论速览

| 任务 | 结论 | 对 MVP 的影响 |
|---|---|---|
| P0-01 BaoStock 退市股日线 | **可用（通过）** | 退市前日线可取，存活偏差的 R1 可解除 |
| P0-02 TuShare `stk_limit` 起始年 | **不可用（无 token，未验证）** | 涨跌停价按方案 §4.2 全面自算，`stk_limit` 仅作后续交叉校验 |
| P0-03 复权口径校准 | **可用（通过），但方案口径需修正** | BaoStock 后复权**包含现金分红**，不能按“漏掉分红”处理；收益账与信号序列要分开 |
| 补充 A：退市股清单 | **可用** | `query_stock_basic` 可拿 340 只退市股及 `outDate` |
| 补充 B：历史股票池 | **可用（近似）** | `query_all_stock(day)` 是时点快照；多时点并集可近似，但应以 `stock_basic` 的 `ipoDate/outDate` 为主 |

> **最重要的修正**：方案 §4.4 写“BaoStock 涨跌幅复权法，不参加分配 → 漏掉现金分红”。本次实测 600519 六个除权日的 `hfq/raw` 比值跳变与公告每股分红逐项吻合，说明 BaoStock `adjustflag=1` **已经包含现金分红**。详见 §3。

---

## 1. P0-01：BaoStock 退市股日线

### 结论

**可用（通过）**。

取 2020、2021、2022、2024 年退市股各 1 只，查询区间 `2016-01-01` 至退市日，均能返回退市前日线；四只股票退市前有成交量的交易日分别为 685、355、1484、1956 个，均 ≥120。

### 证据

查询参数：`frequency="d"`，`adjustflag=3`（不复权）；字段为 18 个完整字段。返回行数包含停牌日/退市日占位行，不等于有成交的交易日。

| 代码 | 名称 | IPO | 退市日 `outDate` | 实际区间 | 返回行 | 有量交易日 | 最后有量日 |
|---|---|---|---|---|---|---|---|
| sz.000939 | 凯迪退 | 1999-09-23 | 2020-12-17 | 2016-01-04～2020-12-17 | 1208 | 685 | 2020-12-16 |
| sh.600485 | *ST信威 | 2003-08-07 | 2021-06-01 | 2016-01-04～2021-06-01 | 1316 | 355 | 2020-04-20 |
| sh.600093 | 退市易见 | 1997-06-26 | 2022-06-23 | 2016-01-04～2022-06-23 | 1573 | 1484 | 2022-06-16 |
| sz.300495 | *ST美尚 | 2015-12-22 | 2024-09-20 | 2016-01-04～2024-09-20 | 2120 | 1956 | 2024-05-08 |

`[实测]` 退市日来自 `bs.query_stock_basic(code=...)` 的 `outDate`、`status=0`；`有量交易日` 定义为 `tradestatus=1 且 volume>0`。

### 最后 3 行样例（`adjustflag=3`，不复权）

**sz.000939（凯迪退）**

```text
2020-12-15  close 0.1500  vol 77,295,300  amount 11,737,259.00  turn 2.5412  tradestatus 1  pctChg -6.2500  isST 0
2020-12-16  close 0.1500  vol 92,169,890  amount 14,006,538.36  turn 3.0302  tradestatus 1  pctChg  0.0000  isST 0
2020-12-17  close 0.1500  vol ''          amount ''             turn ''      tradestatus 0  pctChg  ''      isST 0
```

**sh.600485（*ST信威）**

```text
2021-05-28  close 1.3900  vol 0   amount 0.00  turn ''  tradestatus 0  pctChg 0.0000  isST 1
2021-05-31  close 1.3900  vol 0   amount 0.00  turn ''  tradestatus 0  pctChg 0.0000  isST 1
2021-06-01  close 1.3900  vol ''  amount ''    turn ''  tradestatus 0  pctChg ''      isST 0
```

**sh.600093（退市易见）**

```text
2022-06-21  close 0.5000  vol ''  amount ''  turn ''  tradestatus 0  pctChg ''  isST 0
2022-06-22  close 0.5000  vol ''  amount ''  turn ''  tradestatus 0  pctChg ''  isST 0
2022-06-23  close 0.5000  vol ''  amount ''  turn ''  tradestatus 0  pctChg ''  isST 0
```

**sz.300495（*ST美尚）**

```text
2024-09-18  close 0.1300  vol ''  amount ''  turn ''  tradestatus 0  pctChg ''  isST 1
2024-09-19  close 0.1300  vol ''  amount ''  turn ''  tradestatus 0  pctChg ''  isST 1
2024-09-20  close 0.1300  vol ''  amount ''  turn ''  tradestatus 0  pctChg ''  isST 0
```

### 字段支持与 raw/hfq 样例

`[实测]` 返回字段（18 个）：

```text
date, code, open, high, low, close, preclose, volume, amount, adjustflag,
turn, tradestatus, pctChg, peTTM, pbMRQ, psTTM, pcfNcfTTM, isST
```

P0-01 要求的 `preclose, isST, tradestatus, turn, pctChg, amount, volume` 全部存在；停牌/退市日会出现空字符串，不能当成 0 值直接参与计算。

| 股票/日期 | adjustflag | close | preclose | volume | amount | turn | tradestatus | pctChg | isST |
|---|---|---|---|---|---|---|---|---|---|
| sh.600519 2024-06-17 | 3（raw） | 1541.50 | 1555.00 | 4,831,816 | 7,400,883,242.87 | 0.3846 | 1 | -0.8682 | 0 |
| sh.600519 2024-06-17 | 1（hfq） | 10743.1929065 | 10837.278605 | 4,831,816 | 7,400,883,242.87 | 0.3846 | 1 | -0.8682 | 0 |
| sh.600485 2021-05-25 | 3（raw） | 1.3900 | 1.3900 | 0 | 0.00 | '' | 0 | 0.0000 | 1 |
| sh.600485 2021-05-25 | 1（hfq） | 2.8059082100 | 2.8059082100 | 0 | 0.00 | '' | 0 | 0.0000 | 1 |

- 600519 当日 `hfq/raw = 10743.1929065 / 1541.50 = 6.969311`。
- 600485 当日 `hfq/raw = 2.80590821 / 1.39 = 2.018639`。
- `volume/amount/turn/pctChg/isST` 在 raw 与 hfq 两种模式下相同；只有 OHLC/preclose 被复权。
- `[实测]` 600519 在 2022–2024 年全部交易日的 `pctChg` 与 `close/preclose-1` 最大差 `4.998e-05` 个百分点；`ratio` 跳变与“反推现金/preclose”最大差 `1.110e-05` 个百分点，均属于价格舍入，不是口径差异。

### 对 MVP 的影响

1. **R1 可以解除**：BaoStock 能取到退市前日线，MVP 不需要先购买 TuShare 2000 分权限。
2. **不能把“返回行”当“交易日”**：600485 返回 1316 行，但只有 355 个有量交易日；它从 2020-04-20 后长期停牌，直到 2021-06-01 退市。若按行数判断流动性会严重高估。
3. **退市日/停牌日要单独处理**：`volume=''`、`amount=''`、`pctChg=''`、`tradestatus=0`；`isST` 在 `outDate` 当天还可能从 1 翻成 0（600485、300495 都出现），不能把退市日的 `isST=0` 当成“非 ST”。
4. **最后 3 行可能是停牌占位**：应使用 `last3_traded`（JSON 中已单独输出）或按 `tradestatus=1 且 volume>0` 过滤，不能用 `tail(3)` 直接算最后收益。

### 建议口径

- 读取区间固定为 `2016-01-01` 至 `outDate`；若做 2016 年前回测，需另行实测更早年份。
- 退市股入池条件：退市前有量交易日 ≥120，且最后有量日距 `outDate` 不超过策略允许的停牌窗口；600485 这类长期停牌股应被流动性条件自然剔除。
- 保留 `tradestatus` 与原始 `volume` 空值语义：空串 = 无数据/退市占位，0 = 停牌或无成交；不要统一填 0 后混用。
- 解析时同时保留 raw 与 hfq 两套 OHLC，并写入 `adjustflag` 来源字段。

---

## 2. P0-02：TuShare `stk_limit` 起始年

### 结论

**不可用（无 token，未验证起始年）**。

仓库运行环境没有配置 `TUSHARE_TOKEN`；`tushare.pro_api()` 在初始化阶段直接失败，未发出 `stk_limit` 请求。没有尝试购买权限或绕过权限。

### 证据

`[实测]`：

```text
TUSHARE_TOKEN present: False
pro_api/stk_limit failed: Exception api init error.
```

补充检查：

- 进程环境变量中没有 `TUSHARE_TOKEN` / `TS_TOKEN`。
- `.env` 文件存在，但按 key 名过滤后没有任何 `TUSHARE_TOKEN=` 配置行（只按 key 名检查，未打印任何值）。
- `[未验证]` `stk_limit` 最早可用年份；本次无法给出 2014/2015/2016 等年份的可用性结论。
- `[未验证]` TuShare 旧版免 token 接口；本任务只验证 `stk_limit` 依赖的 Pro API，未把旧接口作为替代源。

### 对 MVP 的影响

- 不能把 `stk_limit` 作为 MVP 的硬依赖；按方案 §4.2 用前收 + 板块/ST/日期分段规则自算涨跌停价。
- `stk_limit` 后续若拿到 token，只作为交叉校验源；用 `high/low` 与自算价反向校验，不能反过来让官方价掩盖规则错误。
- P0-02 仍处于阻塞状态；“全面自算”可以作为 MVP 的默认路径，但“最早可用日期”这个问题仍未回答。

### 建议口径

- `config/market_rules.yaml` 实现按日期分段的涨跌停规则；撮合层只消费自算价。
- 数据质量校验增加：`limit_up/limit_down` 与 `high/low` 的反向校验通过率；异常样本单独隔离。
- 若后续配置 token，探测脚本会自动按 2014–2018 年 1 月抽样 `stk_limit`，记录最早有数据的年份；当前脚本已预留该分支。

---

## 3. P0-03：后复权与不复权的关系

### 结论

**可用（通过），但方案 §4.4 对 BaoStock 的描述需要修正：`adjustflag=1` 的后复权包含现金分红。**

判定依据不是“看价格像不像”，而是逐除权日核对：

```text
ratio = hfq_close / raw_close
ratio_t / ratio_(t-1) = prev_raw_close / preclose_t
ratio_jump = (prev_raw_close - preclose_t) / preclose_t = 每股现金 / preclose_t
```

其中 `preclose_t` 是除权日的除权参考价。若后复权完全不含分红，`ratio` 在除权日应保持不变；实际它按“每股现金 / 除权参考价”跳变，且与公告分红逐项吻合。

### 证据：600519 贵州茅台

`[实测]` 取 2022-01-01～2024-12-31 的 raw（`adjustflag=3`）与 hfq（`adjustflag=1`），合并后计算比值跳变。全部 6 个 >0.5% 的跳变如下：

| 除权日 | 前收（raw） | 除权参考 `preclose` | 反推每股现金 | ratio 前→后 | 比值跳变 | 公告（元/股） |
|---|---|---|---|---|---|---|
| 2022-06-30 | 2030.00 | 2008.33 | 21.67 | 6.628762→6.700286 | +1.0790% | 21.675 `[公告]` |
| 2022-12-27 | 1742.06 | 1720.15 | 21.91 | 6.700286→6.785630 | +1.2737% | 21.910 `[公告]` |
| 2023-06-30 | 1713.71 | 1687.80 | 25.91 | 6.785630→6.889798 | +1.5351% | 25.911 `[公告]` |
| 2023-12-20 | 1675.00 | 1655.89 | 19.11 | 6.889798→6.969311 | +1.1541% | 19.106 `[公告]` |
| 2024-06-19 | 1521.50 | 1490.62 | 30.88 | 6.969311→7.113689 | +2.0716% | 30.876 `[公告]` |
| 2024-12-20 | 1551.01 | 1527.13 | 23.88 | 7.113689→7.224927 | +1.5637% | 23.882 `[公告]` |

公告来源：[搜狐证券 600519 分红记录](https://q.stock.sohu.com/cn/600519/fhsp.shtml)；2024-06-19 另见[新京报报道](https://www.bjnews.com.cn/detail/1718783668168440.html)（每股现金红利 30.876 元）。

`[公告]` 该记录在 2022–2024 年只列现金分红，未列送转；因此 `preclose` 差额可直接视为每股现金分红。`[实测]` 全区间 `ratio` 跳变与“反推现金/preclose”最大差 `1.110e-05` 个百分点，`pctChg` 与 `close/preclose-1` 最大差 `4.998e-05` 个百分点，均属舍入。

以 2024-06-19 为例：

```text
前收 1521.50，除权参考 1490.62 → 反推现金 30.88
比值 6.969311 → 7.113689，跳变 +2.0716%
30.88 / 1490.62 = 2.0716%
公告 30.876 元/股（10 派 308.76 元）
```

以 2023-06-30 为例：

```text
前收 1713.71，除权参考 1687.80 → 反推现金 25.91
比值 6.785630 → 6.889798，跳变 +1.5351%
25.91 / 1687.80 = 1.5351%
公告 25.911 元/股（10 派 259.11 元）
```

同日逐行样例（600519）：

```text
2023-06-28  raw close 1728.38  preclose 1711.05  hfq close 11728.1471794  ratio 6.785630
2023-06-29  raw close 1713.71  preclose 1728.38  hfq close 11628.6019873  ratio 6.785630
2023-06-30  raw close 1691.00  preclose 1687.80  hfq close 11650.6484180  ratio 6.889798
2023-07-03  raw close 1724.10  preclose 1691.00  hfq close 11878.7007318  ratio 6.889798
```

### 旁证：000651 格力电器

`[实测]` 2022-08-05：

```text
前收 32.16，除权参考 30.19 → 反推现金 1.97
ratio 139.253850 → 148.340661，跳变 +6.5254%
公告 2.00 元/股（10 派 20 元）[公告]
```

公告来源：[搜狐证券 000651 分红记录](https://q.stock.sohu.com/cn/000651/fhsp.shtml)。

这里反推现金 1.97 与公告 2.00 相差 0.03 元，原因尚未核实（可能是除权参考价舍入或 BaoStock 的 `preclose` 口径差异）。因此 000651 只作为旁证，**主证据使用 600519 的六个除权日**。

### 对“是否含分红”的判断

- `[实测]` 600519 的 6 个除权日，`hfq/raw` 比值跳变全部与公告每股分红/除权参考价吻合；如果不是含分红，比值应保持常数。
- `[推断]` BaoStock 的 `adjustflag=1` 是“按除权参考价计算涨跌幅并累乘”的后复权序列，经济含义上把现金分红计入了价格路径；它不等同于 TuShare `adj_factor` 那种独立复权因子，也不建议直接称为“分红再投”。
- `[未验证]` BaoStock 官方文档对 `adjustflag` 的精确定义；本次结论来自数值反推，建议后续补官方文档或更多股票样本。

### 对 MVP 的影响

1. **信号序列**：使用 `adjustflag=1` 的后复权序列是可行的，除权日不会出现价格断层；这比方案原先假设的“不含分红”更有利于信号连续性。
2. **成交模拟**：必须继续使用 `adjustflag=3` 的真实价；不能用 hfq 价当成交价。
3. **持仓/收益**：若用 raw 价 + 独立现金分红账，不能再用 hfq 收益重复计分红；若用 hfq 收益算 P&L，则不要再加现金分红。两种口径必须二选一，并在代码中显式标注。
4. **阈值换算**：`factor_ratio = hfq_close / raw_close` 可以用于 `real = adj / factor_ratio`；但该比值在除权日跳变，阈值必须在除权日重算，不能沿用昨日因子。
5. **数据质量断言**：相邻除权日 `ratio` 跳变 >0.5% 时，必须能由该股分红送转方案解释；600519 的 6 个跳变是正例，000651 的 0.03 元差异是待解释项。

### 建议口径

- 数据层同时落 `raw_close` 与 `hfq_close`，并派生 `factor_ratio`；不要把 hfq 当“真实价”写入成交表。
- 信号、均线、止损/止盈阈值全部在 hfq 序列上计算；成交与持仓估值用 raw；现金分红单独入账。
- 在 `corporate_actions` / 数据质量模块中记录每次除权日的 `preclose`、反推每股现金、公告值、跳变比例；无法解释的跳变隔离该股。
- 方案 §4.4 的 BaoStock 行应改为：“涨跌幅复权，**含现金分红**；与 TuShare 复权因子口径不同，禁止混用。” 该修订不在本次任务范围内，留给方案维护者处理。

---

## 4. 补充 A：退市股清单如何获取（支撑 P0-01）

### 结论

**可用**。

`bs.query_stock_basic()` 无参数调用返回全部证券基础资料，包含 `code, code_name, ipoDate, outDate, type, status`；`type=1` 且 `status=0` 即退市股票，`outDate` 给出退市日。

### 证据

`[实测]` 2026-10-26 返回：

| 指标 | 数值 |
|---|---|
| 总记录 | 9022 |
| `type=1` 股票 | 5564 |
| 其中 `status=1`（在市） | 5224 |
| 其中 `status=0`（退市） | 340 |
| 退市股 `outDate` 覆盖 | 1999～2026 |

`type=1` 的代码前缀分布：`sh.60` 1846、`sz.00` 1643、`sz.30` 1454、`sh.68` 621；本次快照中没有 `sh.90`/`sz.20` 等 B 股前缀，但仍建议在池构造时显式过滤 A 股前缀。

退市股按 `outDate` 年份（节选）：2016:1、2017:5、2018:6、2019:11、2020:16、2021:20、2022:43、2023:45、2024:52、2025:30、2026:22；首个采样日 2016-12-30 之前合计 90 只。

单只查询样例：

```text
bs.query_stock_basic(code="sh.600485")
→ ['sh.600485', '*ST信威', '2003-08-07', '2021-06-01', '1', '0']
```

可用调用方式：

```python
lg = bs.login()
rs = bs.query_stock_basic()          # 全量，约 13 秒
rows = []
while rs.error_code == "0" and rs.next():
    rows.append(rs.get_row_data())
# rows 的字段顺序: code, code_name, ipoDate, outDate, type, status
# 退市股: type == "1" and status == "0"
```

或按代码查单只：

```python
rs = bs.query_stock_basic(code="sh.600485")
```

### 限制

- `query_stock_basic` 的签名只支持 `code`/`code_name`，没有 `status`/`outDate` 的服务端过滤；必须全量拉取后本地过滤。
- `status` 的 0/1 语义来自数据观察（0=退市、1=在市），没有在本次任务中核对官方文档；`[推断]`。
- 退市清单是“当前时点”的完整清单，不是每日状态；若需要每日 `list_status`，仍需结合日线 `tradestatus` 或 TuShare 的 `stock_basic(list_status='D')`。
- 全量查询约 13 秒，适合启动时缓存，不适合每次回测循环调用。

### 对 MVP 的影响

- 退市股清单可直接用于构造历史池，不需要 TuShare 的 `stock_basic(list_status='D')` 作为硬依赖。
- `outDate` 可用于决定退市股在池中的最后可交易日；退市日当天通常无成交，应按“最后有量日”处理。
- 340 只退市股的数量足以做存活偏差校验；不需要先购买 TuShare 权限。

### 建议口径

- 以 `query_stock_basic()` 的 `type=1` 全量清单为股票宇宙，过滤 A 股前缀（`sh.60/sh.68/sz.00/sz.30`）。
- 入池区间：`ipoDate <= date < outDate`；`date == outDate` 当天不作为可交易日。
- 缓存 `code_name/ipoDate/outDate/status`，每天只增量检查；全量刷新放在启动或周任务。

---

## 5. 补充 B：股票池历史构造（支撑 P0-01/P0-03）

### 结论

**可用（近似）**。

`bs.query_all_stock(day=...)` 在历史日期能返回当日的时点证券快照，包含后来退市的股票；但它也包含指数、ETF、基金，且包含停牌证券。用“多个时点快照取并集”可以近似历史全市场，但**不能单独保证覆盖所有短命股**；应以 `query_stock_basic` 的 `ipoDate/outDate` 为主，`query_all_stock` 作为时点校验。

### 证据

`[实测]` `query_all_stock(day)` 返回字段：`code, tradeStatus, code_name`。A 股股票计数按 `sh.60/sh.68/sz.00/sz.30` 过滤，并排除 `sz.39`（深证指数）。

| 采样日 | 返回总数 | A 股股票 | `tradeStatus=1` | `tradeStatus=0` | 样本股是否在列 |
|---|---:|---:|---:|---:|---|
| 2016-12-30 | 3757 | 3037 | 2834 | 203 | 600485/000939/600093/300495 全在 |
| 2019-12-31 | 4569 | 3761 | 3742 | 19 | 四只全在 |
| 2022-12-30 | 6200 | 4906 | 4899 | 7 | 仅 300495 在 |
| 2024-12-31 | 6695 | 5122 | 5108 | 14 | 四只均不在 |

- 600485 在 2016/2019 快照中存在，2021-06-01 退市后从 2022 快照消失。
- 000939 在 2016/2019 存在，2020-12-17 退市后消失。
- 600093 在 2016/2019 存在，2022-06-23 退市后从 2024 快照消失。
- 300495 在 2016/2019/2022 存在，2024-09-20 退市后从 2024-12-31 快照消失。

`[实测]` 四个采样日 A 股并集为 5320 只；与 `query_stock_basic` 的 340 只退市股对比：

| 分组 | 数量 | 说明 |
|---|---:|---|
| 退市股总数 | 340 | `type=1, status=0` |
| 并集覆盖 | 250 | `outDate >= 2016-12-30` 的退市股全部覆盖 |
| 并集遗漏 | 90 | 全部为首个采样日 2016-12-30 之前退市的股票 |
| 遗漏年份 | 1999～2016 | 与窗口外预期一致 |

因此，对 **2016–2024 窗口**，四个端点快照的并集覆盖了该窗口内所有 250 只退市股；首个采样日前退市的 90 只不属于该窗口的股票池。

### 限制

- `query_all_stock` 返回的是“当日快照”，不是“当日有成交的股票”：2016-12-30 有 203 只 `tradeStatus=0`（停牌），需要按 `tradeStatus=1` 过滤才是严格“有交易”。
- 快照包含指数/ETF/基金；2016 年 `sz.39` 指数 345 只、`sh.00` 指数 229 只、`sh.51`/`sz.15` 基金等，不能直接当股票池。
- 多时点并集的固有缺口：如果某股票在两个采样日之间上市并退市，且采样日都不在其存续期内，就会被漏掉。本次 4 个端点没有出现这种漏网（250/250），但这是数据巧合，不是逻辑保证。
- `query_all_stock` 单次约 2–10 秒，不建议在 10 年回测中逐日调用；逐日状态应从日线 `tradestatus/isST` 读取。

### 对 MVP 的影响

- 可以用 `query_all_stock` 做时点池的交叉校验：例如某日池中出现了不应存在的退市股，或漏掉了当日应存在的股票。
- 主股票池应以 `query_stock_basic` 的 `ipoDate/outDate` 构造，再按日线 `tradestatus`、`isST`、成交额/价格阈值过滤；这样不会依赖采样频率。
- 如果只允许使用 `query_all_stock`，则应至少按季度采样并取并集；本次只测了 4 个端点，不能宣称任意频率都完整。

### 建议口径

- 主路径：`stock_basic(type=1)` + `ipoDate/outDate` + 日线 `tradestatus/isST` + 流动性/价格过滤。
- 校验路径：季度或年度 `query_all_stock` 快照，检查池成分数偏差；异常日单独告警。
- 严格区分“上市/在市”“停牌”“有成交”：`tradeStatus=1` 也只代表当日可交易状态，不等于有成交量；最终以日线 `volume>0` 为准。
- 过滤指数/基金：只保留 `sh.60/sh.68/sz.00/sz.30`，排除 `sz.39`。

---

## 6. 对 MVP 的建议口径汇总

| 主题 | 建议 |
|---|---|
| 退市股 | 读 2016-01-01 至 `outDate`；过滤 `tradestatus=1 且 volume>0`；要求退市前 ≥120 个有量交易日 |
| 字段 | 使用 18 字段；`preclose/isST/tradestatus/turn/pctChg/amount/volume` 均可用；空串保留为缺失 |
| 复权 | 信号用 `adjustflag=1`；成交/估值用 `adjustflag=3`；派生 `factor_ratio=hfq/raw`；除权日重算阈值 |
| 收益 | raw 价 + 独立现金分红账；禁止在 hfq 收益之外重复加分红 |
| 股票池 | `stock_basic(type=1)` 的 `ipoDate/outDate` 为主；`query_all_stock` 做时点校验 |
| 退市清单 | `query_stock_basic()` 全量过滤 `type=1, status=0`；缓存 `outDate` |
| 涨跌停 | 按 §4.2 自算；`stk_limit` 仅作后续交叉校验 |
| TuShare | 不作为 MVP 硬依赖；P0-02 未通过前不引入 `stk_limit` |
| 数据质量 | 退市日/停牌日空值、`isST` 翻转、复权比值跳变都要有断言/隔离规则 |

---

## 7. 复现与输出

```bash
cd /Users/jepsoncheng/Documents/github/daily_stock_analysis
.venv/bin/python scripts/probe_data_sources.py --out reports/data_probe
```

可选参数：

```text
--out DIR                  JSON 输出目录，默认 reports/data_probe
--start YYYY-MM-DD         退市股查询起始日，默认 2016-01-01
--end YYYY-MM-DD           退市股查询结束日，默认今天
--all-stock-dates D1,D2     query_all_stock 采样日，逗号分隔
--normal CODE              复权对比正常股票，默认 sh.600519
--timeout SEC              socket 超时，默认 20
--skip-tushare             跳过 TuShare 探测
```

输出 JSON 的顶层键：

- `baostock.p0_01_delisted`：退市股日线、字段样例、raw/hfq 比值
- `baostock.p0_03_adjustment`：600519 全部 >0.5% 的比值跳变、逐日窗口
- `baostock.support_stock_basic`：全量基础资料统计、340 只退市清单
- `baostock.support_all_stock`：各采样日计数、并集、退市股覆盖
- `baostock.login_history` / `baostock.relogin_count`：登录与重登录记录
- `tushare`：token 是否存在、`pro_api` 错误、`stk_limit` 尝试记录
- `conclusions`：脚本自动生成的结论摘要

脚本行为：

- 所有网络调用前设置 socket 默认超时；`bs.login()`/`bs.logout()` 在上下文管理器中执行。
- 单个股票/单日查询失败只记录 `error`，不中断其他查询；BaoStock 登录失败会写 `fatal_error` 并继续 TuShare 探测。
- 检测到 `10001001`（未登录）、`10002007`（网络接收错误）、超时或解码错误时，最多重登录并重试 3 次（含首次）；不做无限重试。本次运行初始登录 1 次、重登录 1 次（网络抖动后恢复），最终 JSON 完整，记录见 `login_history`。
- 不读取、不打印、不写入任何 token 值；只记录 `token_env_present` 布尔值。

---

## 8. 未解决项与剩余不确定性

1. **P0-02 仍阻塞**：没有 `TUSHARE_TOKEN`，`stk_limit` 最早可用年份未知；本次只能确认 Pro API 不可用。
2. **BaoStock 后复权官方定义未核对**：结论来自 600519 六个除权日的数值反推；建议补官方文档或再用 2–3 只高分红股复核。当前证据足以推翻“后复权不含分红”的假设，但“等价于分红再投”只是 `[推断]`。
3. **000651 的 0.03 元差异未解释**：公告 2.00 元/股，BaoStock `preclose` 反推 1.97 元；可能是舍入或除权参考价口径差异，未核实。
4. **多时点并集的短命股缺口**：本次 4 个端点覆盖了 2016–2024 全部 250 只退市股，但未测试“上市后两个采样日之间又退市”的极端样本；若只依赖 `query_all_stock`，需提高采样频率。
5. **`status` 语义**：`status=0/1` 是数据观察结果，未核对 BaoStock 官方字段说明；`type=1` 本次只出现 A 股前缀，未发现 B 股。
6. **退市日 `isST` 翻转**：600485/300495 在 `outDate` 当天 `isST` 从 1 变 0；这是数据行为，原因未核实，代码中不能依赖退市日 `isST`。
7. **范围外未测**：北交所、2016 年前日线、2026 年 ST 5%→10% 规则、盘后固定价成交量（P0-04）、北交所退市整理期规则（P0-05）均未在本次任务中实测。

---

## 9. 证据文件

| 文件 | 说明 |
|---|---|
| [scripts/probe_data_sources.py](../../scripts/probe_data_sources.py) | 可重复执行的探测脚本（`--out` 参数化） |
| [reports/data_probe/data_probe.json](../../reports/data_probe/data_probe.json) | 本次完整 JSON（`reports/` 已被 gitignore） |
| [a-share-swing-backtest-plan.md](./a-share-swing-backtest-plan.md) | 方案真源；§4.4 的 BaoStock 口径需要修订 |
| [a-share-swing-backtest-execution.md](./a-share-swing-backtest-execution.md) | P0-01～P0-03 任务与验收标准 |

本次只新增/修改上述脚本和文档；未修改方案、执行清单、配置或其他源码，未提交 commit。
