# 波段回测：Web 页面与 API

- 实现方案：[a-share-swing-backtest-plan.md](./plans/a-share-swing-backtest-plan.md)
- 任务拆分与结论：[a-share-swing-backtest-execution.md](./plans/a-share-swing-backtest-execution.md)
- 页面路由：`/swing-backtest`（侧边栏「波段回测」）
- API 前缀：`/api/v1/swing-backtest`

## 1. 页面

页面包含：

- 运行选择与 go/no-go 判定（PASS / WATCH / STOP）；
- 交易笔数、每笔毛超额、CI 下界、净期望、PF、最大回撤；
- 累计收益曲线（信号级为账务净值，不代表可投资组合）；
- 条件失败排行、拒单原因、最近交易、报告文件下载；
- 「新建回测」表单：策略、区间、成本档、信号级/组合约束、最多股票数。

触发运行只读取服务器本地缓存 `data/cache/swing_bars_full/raw`，不会在请求内抓取网络行情。全市场抓取仍使用 CLI：

```bash
.venv/bin/python scripts/fetch_swing_bars.py \
  --codes-file data/cache/chunks/chunk_0.txt \
  --start 2016-01-01 --end 2024-12-31 \
  --cache-dir data/cache/swing_bars_full --label w0
```

## 2. API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/swing-backtest/runs` | 列出 `reports/backtest/*` 与 `reports/mvp` 运行 |
| GET | `/api/v1/swing-backtest/runs/{run_id}` | 运行详情（净值、交易、诊断、拒单、文件） |
| GET | `/api/v1/swing-backtest/runs/{run_id}/status` | 运行状态（running/completed/failed） |
| POST | `/api/v1/swing-backtest/runs` | 基于本地缓存触发回测 |
| GET | `/api/v1/swing-backtest/runs/{run_id}/files/{filename}` | 下载 `.csv/.json/.md` 报告文件 |

触发示例：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/swing-backtest/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "strategy": "trend_pullback_restart",
    "start": "2016-01-01",
    "end": "2024-12-31",
    "cost_tier": "base",
    "mode": "signal",
    "max_codes": 300
  }'
```

约束与行为：

- `max_codes` 1–800，默认 300；全市场请用 CLI。
- 同一进程同时只允许一个回测；重复提交返回 `409`。
- 缓存不存在返回 `409`，提示先抓取行情。
- 运行在后台线程执行，页面每 2.5 秒轮询状态。
- 认证开启时，所有 `/api/v1/*` 请求沿用现有 Cookie 认证中间件。
- 报告目录固定为 `reports/backtest/{run_id}`；`reports/mvp` 作为 `run_id=mvp` 展示。

## 3. 环境变量

```text
BACKTEST_CACHE_DIR=./data/cache/swing_bars_full
MARKET_CAPS_DATABASE_URL=postgresql://user:pass@host:5432/db
SWING_BARS_DATABASE_URL=postgresql://user:pass@host:5432/db
SWING_BARS_SCHEMA=swing_bars
```

未设置时行情缓存默认仓库下 `data/cache/swing_bars_full`；`MARKET_CAPS_DATABASE_URL`
供 `scripts/fetch_market_caps.py` 导出市值缓存（密码等特殊字符需 percent-encode，
如 `@` 写作 `%40`）。`SWING_BARS_DATABASE_URL` + `SWING_BARS_SCHEMA` 供
`scripts/ingest_swing_bars.py` 落库与 `mvp_edge_check.py --bars-url/--bars-schema` 读回
（不设 URL 则默认 `sqlite:///data/swing_bars.db`，不设 schema 则用数据库默认 schema）。

## 4. B2 口径与市值缓存（V3.0 §4.4/§4.5）

两套 B2 口径（`scripts/mvp_edge_check.py --b2 {auto,matched,legacy}`）：

| 口径 | 定义 | 用途 |
|---|---|---|
| `legacy` | 同分位桶（20 日均额代理 size）随机抽 5 只 × 200 次取中位数 | 复现 MVP 冻结结果（run `mvp-20261026-233203`） |
| `matched` | 同一信号日、同一流通市值分档（默认 <20/20–30/30–45/45–70/≥70 亿，`--size-edges` 可调）内**所有可买股票**的均值收益，对事件股票留一剔除；同日全池「市场口径」对照；对照股票套用与主策略一致的入场拦截（停牌/开盘涨停/跳空 ≤3%） | V3.0 起的标准口径，与 `fund-analysis` 事件研究统一；无抽样噪声、无需种子 |
| `auto`（默认） | 有市值缓存时用 `matched`，否则回退 `legacy`（日志与 run_metadata 均记录实际口径） | — |

分档参数在 `config/backtest.yaml` 的 `matched_control` 段；当日市值覆盖率
< `min_coverage`（默认 0.9）时该日回退 20 日均额代理分位桶，并在报告披露。

### 导出市值缓存

数据源为 `fund-analysis` 库 `public.stock_eod_daily`（TuShare `daily_basic` +
`stk_limit` + `adj_factor` 合成，含退市股；`circ_mv` 单位为元）。导出与抽样对账：

```bash
.venv/bin/python scripts/fetch_market_caps.py \
  --start 2016-01-01 --end 2024-12-31 \
  --cache-dir data/cache/market_caps \
  --codes-file data/cache/swing_bars_full/universe_codes.txt \
  --url "$MARKET_CAPS_DATABASE_URL" \
  --cross-check --bars-cache data/cache/swing_bars_full
```

交叉校验结论（2026-10-28，主板 3328 只池、300 只抽样）：

- **涨跌停价**：官方 `stk_limit` 与分段规则自算（Decimal ROUND_HALF_UP）1436 对比
  **0 不匹配**（含核准制新股首日 +44%/−36% 与注册制前 5 日不设限哨兵值）；
- **复权因子**：两源绝对水平不可比（TuShare 自上市日累积、BaoStock hfq 基准日不同，
  每股恒定倍数差，121/122 只佐证）；有效口径为**除权日跳变一致性**，552 对比 1 超 0.5%；
- 已知数据缺口：BaoStock hfq 对**配股类除权**存在漏调整（全量扫描 9 例，如 002466
  2019-12-26 配股除权日 BaoStock 因子未跳变、TuShare 正确跳变 ×1.19，官方涨跌停价
  佐证交易所确实重置了 pre_close）。涉及股票的 BaoStock hfq 信号序列在事件日有跳崖，
  量级约 0.5% 股票 × 单日，不足以动摇 MVP STOP 结论；未来信号层可改用市值缓存的
  `adj_factor` 重建复权序列（待 I-4 落库后实施）。

## 5. 试验治理

- 试验账本：`reports/experiments/ledger.csv`（V2.0 §5.5 字段 + repo/日期扩展），
  两仓库累计试验预算 200 次；校验：`python scripts/check_experiment_budget.py`；
- 负先验登记册（N1 箱体突破 / N2 冲高回落 / N3 趋势回调）见
  `fund-analysis/docs/a-share-swing-backtest-v3-plan.md` §2.4；
- 新假设登记与准入门槛见该方案 §3.2（先登记账本 → 事件研究筛选 → 确认段一次 → 前向预注册）。

## 6. 已知限制

- 信号级运行的净值按固定 6 万名义本金加总，仅用于账务校验；不以它为可投资组合收益。
- 尚未建模现金分红入账与公司行为（阶段 3）；BaoStock 后复权已含分红，但持仓期现金分红未补记。
- 市值缓存自 2017-05-26 起（stock_eod_daily 起点）；更早区间 B2 回退 20 日均额代理分位桶。
- BaoStock hfq 的配股除权漏调整见上节；legacy 口径的 size 为 20 日均额代理、行业为当前口径。
- 运行触发只使用缓存，不负责补数据。
- 行情全量入库：本地 sqlite（`data/swing_bars.db`）与 PostgreSQL `swing_bars` schema 均已落齐；
  入库后的 `adj_factor` 仍来自 BaoStock hfq，配股除权漏调整问题需按 §4 的交叉校验口径另行重建。

## 7. 行情落库（V3.0 I-4）

行情不再只依赖单机 CSV 缓存：`scripts/ingest_swing_bars.py` 把
`data/cache/swing_bars_full` 装入关系库，`mvp_edge_check.py --bars-url` 可直接从库读回，
输出与 CSV 缓存同构（已验证 trades/绩效逐字节一致）。

| 表 | 主键 | 内容 |
|---|---|---|
| `swing_bars_daily` | `(code, trade_date)` | 不复权 OHLC + pre_close/volume/amount/turn/pct_chg/tradestatus/is_st；后复权 `hfq_close` 与 `adj_factor`（信号层用）；`adjustflag` 保留抓取口径 |
| `swing_bars_fetch_meta` | `code` | 抓取溯源：requested_start/end、first_date、last_date、rows、fetched_at、ingested_at |

```bash
# 落库（默认 sqlite:///data/swing_bars.db；生产用 --url 指向 PostgreSQL）
.venv/bin/python scripts/ingest_swing_bars.py \
  --cache-dir data/cache/swing_bars_full --url "$SWING_BARS_DATABASE_URL"

# 只看规模 / 小样本冒烟
.venv/bin/python scripts/ingest_swing_bars.py --cache-dir data/cache/swing_bars_full --dry-run
.venv/bin/python scripts/ingest_swing_bars.py --cache-dir data/cache/swing_bars_full --limit-codes 50

# 从库读回跑回测
.venv/bin/python scripts/mvp_edge_check.py --start 2016-01-01 --end 2024-12-31 \
  --bars-url "$SWING_BARS_DATABASE_URL" --b2 legacy --out reports/mvp_reproduction/run-legacy-db
```

语义说明：

- **幂等**：同一 `fetched_at` + `rows` 的代码默认跳过；`--refresh` 强制重写；
- **重抓收敛**：写入前按 code 清除旧行，「重抓后历史变短」不会残留旧数据；
- **窗口校验**：缓存的 `requested_start/end` 不覆盖请求区间时判为无数据（与 CSV 读路径一致）；
- **从库读**只补本地 `industry.json`（存在时），不会触发网络登录；
- **SQLite 批量写优化**：连接固定 `WAL`/`synchronous=NORMAL`/`cache_size=256MB`；
  二级索引「落库前删、写完后建」（大表逐行维护非聚簇索引是尾部退化的主因）；
- 单测：`tests/unit/test_bars_store.py`（幂等、重抓收敛、窗口过滤、读回同构）。

已验证（2026-10-30）：全量 **6,371,456 行 / 3328 只**入本地 sqlite（1.07GB，`quick_check` ok）
与 PostgreSQL **`swing_bars` schema**（表 1140MB，全量入库 18m54s）；两条库路径读回重跑均
1511 笔，`trades.csv`/`performance_metrics.json`/`signals.csv`/`orders.csv`/`fills.csv`
与 CSV 路径**逐字节相同**，`verify_mvp_reproduction` 判 **MATCH**（PG 读回 3m10s）。

选用独立 schema 而不是独立库的理由：与 `public.stock_eod_daily` 同库可跨 schema JOIN 对账
（下一步重建 `adj_factor` 直接用得上），同时表名/权限与业务表完全隔离。连接串在日志中
经 `mask_url()` 掩掉密码；裸 `postgres://` 会自动改写为 `postgresql+psycopg2://`。
落库命令：`SWING_BARS_DATABASE_URL=... SWING_BARS_SCHEMA=swing_bars python scripts/ingest_swing_bars.py
--cache-dir data/cache/swing_bars_full`（幂等，重跑会全部跳过）。
