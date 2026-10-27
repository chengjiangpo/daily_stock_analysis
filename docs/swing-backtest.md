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
```

未设置时默认使用仓库下 `data/cache/swing_bars_full`。

## 4. 已知限制

- 信号级运行的净值按固定 6 万名义本金加总，仅用于账务校验；不以它为可投资组合收益。
- 尚未建模现金分红入账与公司行为（阶段 3）；BaoStock 后复权已含分红，但持仓期现金分红未补记。
- B2 的 size 口径为 20 日均额代理，行业为当前口径。
- 运行触发只使用缓存，不负责补数据。
