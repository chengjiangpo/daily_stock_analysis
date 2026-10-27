import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  AppPage,
  ApiErrorAlert,
  Badge,
  Button,
  Card,
  EmptyState,
  Input,
  PageHeader,
  Select,
  StatCard,
} from '../components/common';
import { getParsedApiError } from '../api/error';
import type { ParsedApiError } from '../api/error';
import { swingBacktestApi } from '../api/swingBacktest';
import type {
  SwingRunDetail,
  SwingRunStatus,
  SwingRunSummary,
} from '../types/swingBacktest';

const STRATEGIES = [
  { value: 'trend_pullback_restart', label: '趋势回调再启动（主策略）' },
  { value: 'box_breakout', label: '箱体突破（对照）' },
];

const COST_TIERS = [
  { value: 'base', label: '基础成本' },
  { value: 'higher', label: '较高成本' },
  { value: 'stress', label: '压力成本' },
];

const MODES = [
  { value: 'signal', label: '信号级（推荐，无组合约束）' },
  { value: 'portfolio', label: '组合约束（5 仓 / 风控）' },
];

function pct(value?: number | null, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '--';
  return `${(value * 100).toFixed(digits)}%`;
}

function num(value?: number | null, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '--';
  return value.toFixed(digits);
}

function decisionMeta(decision?: string): { label: string; variant: 'success' | 'warning' | 'danger' | 'default' } {
  switch (decision) {
    case 'PASS':
    case 'SIMULATE':
      return { label: '通过 / 可进入模拟', variant: 'success' };
    case 'WATCH':
      return { label: '待观察', variant: 'warning' };
    case 'STOP':
    case 'REJECT':
      return { label: '不通过', variant: 'danger' };
    default:
      return { label: '未判定', variant: 'default' };
  }
}

function modeLabel(mode?: string): string {
  return mode === 'portfolio' ? '组合约束' : '信号级';
}

interface EquityChartPoint {
  date: string;
  returnPct: number;
}

const EquityChart: React.FC<{ detail: SwingRunDetail }> = ({ detail }) => {
  const data = useMemo<EquityChartPoint[]>(() => {
    const base = detail.equity.find((point) => (point.equity ?? 0) > 0)?.equity;
    if (!base) return [];
    return detail.equity.map((point) => ({
      date: point.date,
      returnPct: ((point.equity ?? base) / base - 1) * 100,
    }));
  }, [detail.equity]);

  if (data.length === 0) {
    return <EmptyState title="暂无净值曲线" description="该运行没有输出 daily_equity.csv" />;
  }

  const positive = (data[data.length - 1]?.returnPct ?? 0) >= 0;
  const stroke = positive ? 'hsl(var(--success))' : 'hsl(var(--danger))';

  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="swingEquityFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity={0.35} />
              <stop offset="100%" stopColor={stroke} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.35} />
          <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={40} />
          <YAxis tick={{ fontSize: 11 }} tickFormatter={(value: number) => `${value.toFixed(1)}%`} width={52} />
          <Tooltip
            formatter={(value) => `${Number(value).toFixed(2)}%`}
            labelFormatter={(label) => String(label)}
          />
          <Area
            type="monotone"
            dataKey="returnPct"
            stroke={stroke}
            strokeWidth={2}
            fill="url(#swingEquityFill)"
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
};

const RunDetailPanel: React.FC<{ detail: SwingRunDetail }> = ({ detail }) => {
  const meta = decisionMeta(detail.decision);
  const recentTrades = detail.trades.slice(0, 100);
  const summaryFile = detail.files.find((file) => file.name === 'summary.md');

  return (
    <div className="space-y-4">
      <Card
        title={detail.title}
        subtitle={`${modeLabel(detail.mode)} · ${detail.strategy ?? '--'} · ${detail.costTier ?? '--'}`}
        variant="gradient"
      >
        <div className="flex flex-wrap items-center gap-3">
          <Badge variant={meta.variant} size="md" glow>{meta.label}</Badge>
          <span className="text-sm text-secondary-text">
            {detail.start ?? '--'} ~ {detail.end ?? '--'} · {detail.codes} 只股票 · {detail.nTrades} 笔交易
          </span>
          {summaryFile ? (
            <a
              className="ml-auto text-sm text-cyan hover:underline"
              href={swingBacktestApi.fileUrl(detail.runId, summaryFile.name)}
              target="_blank"
              rel="noreferrer"
            >
              下载完整报告
            </a>
          ) : null}
        </div>
        {detail.decisionReason ? (
          <p className="mt-3 text-sm text-secondary-text">{detail.decisionReason}</p>
        ) : null}
      </Card>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
        <StatCard label="交易笔数" value={detail.nTrades} tone="primary" />
        <StatCard label="每笔毛超额" value={pct(detail.meanExcess)} tone={(detail.meanExcess ?? 0) > 0 ? 'success' : 'danger'} hint="相对 B2 匹配基准" />
        <StatCard label="95% CI 下界" value={pct(detail.ciLow)} tone={(detail.ciLow ?? 0) > 0 ? 'success' : 'warning'} />
        <StatCard label="净期望/笔" value={pct(detail.netExpectancy)} tone={(detail.netExpectancy ?? 0) > 0 ? 'success' : 'danger'} />
        <StatCard label="Profit Factor" value={num(detail.profitFactor)} tone={(detail.profitFactor ?? 0) >= 1.2 ? 'success' : 'danger'} hint="基础成本档" />
        <StatCard label="最大回撤" value={pct(detail.maxDrawdown)} tone={(detail.maxDrawdown ?? 0) <= 0.15 ? 'success' : 'danger'} />
      </div>

      <Card title="累计收益曲线" subtitle={detail.mode === 'portfolio' ? '组合净值' : '账务净值（非可投资组合）'}>
        <EquityChart detail={detail} />
        <div className="mt-2 grid gap-1 text-xs text-secondary-text sm:grid-cols-2 lg:grid-cols-4">
          <span>总收益：{pct(detail.totalReturn)}</span>
          <span>年化：{pct(detail.cagr)}</span>
          <span>夏普：{num(detail.sharpe)}</span>
          <span>胜率：{pct(detail.winRate, 1)}</span>
        </div>
      </Card>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="条件失败排行" subtitle="Signal Diagnostics">
          {detail.diagnostics.length === 0 ? (
            <EmptyState title="暂无诊断数据" description="该运行没有输出 diagnostics.csv" />
          ) : (
            <div className="space-y-3">
              {detail.diagnostics.map((item) => {
                const passRate = item.passRate ?? 0;
                return (
                  <div key={item.conditionName}>
                    <div className="flex items-center justify-between gap-3 text-sm">
                      <span className="truncate font-mono text-xs text-secondary-text" title={item.conditionName}>
                        {item.conditionName}
                      </span>
                      <span className="shrink-0 text-xs text-secondary-text">
                        {item.failed}/{item.total} 失败 · {pct(passRate, 1)} 通过
                      </span>
                    </div>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted/40">
                      <div
                        className="h-full rounded-full bg-cyan/70"
                        style={{ width: `${Math.max(2, Math.min(100, passRate * 100))}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </Card>

        <Card title="拒单原因" subtitle="Execution Rejections">
          {Object.keys(detail.rejectionReasons).length === 0 ? (
            <EmptyState title="没有被拒订单" description="可能是信号级运行或全部成交" />
          ) : (
            <div className="space-y-2">
              {Object.entries(detail.rejectionReasons).map(([reason, count]) => (
                <div key={reason} className="flex items-center justify-between rounded-xl border border-subtle/60 px-3 py-2">
                  <span className="font-mono text-xs text-secondary-text">{reason}</span>
                  <Badge variant="warning">{count}</Badge>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <Card title="最近交易" subtitle={`最近 ${recentTrades.length} 笔`}>
        {recentTrades.length === 0 ? (
          <EmptyState title="暂无交易" description="该运行没有产生成交" />
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs uppercase tracking-wide text-secondary-text">
                <tr className="border-b border-border/50">
                  <th className="py-2 pr-4">代码</th>
                  <th className="py-2 pr-4">买入</th>
                  <th className="py-2 pr-4">卖出</th>
                  <th className="py-2 pr-4">持有</th>
                  <th className="py-2 pr-4">毛收益</th>
                  <th className="py-2 pr-4">基准</th>
                  <th className="py-2 pr-4">超额</th>
                  <th className="py-2 pr-4">退出原因</th>
                </tr>
              </thead>
              <tbody>
                {recentTrades.map((trade, index) => (
                  <tr key={`${trade.code}-${trade.entryDate}-${index}`} className="border-b border-border/25">
                    <td className="py-2 pr-4 font-mono text-xs">{trade.code}</td>
                    <td className="py-2 pr-4 text-xs text-secondary-text">{trade.entryDate ?? '--'}</td>
                    <td className="py-2 pr-4 text-xs text-secondary-text">{trade.exitDate ?? '--'}</td>
                    <td className="py-2 pr-4 text-xs">{trade.holdDays} 日</td>
                    <td className={`py-2 pr-4 text-xs ${(trade.grossReturn ?? 0) >= 0 ? 'text-success' : 'text-danger'}`}>
                      {pct(trade.grossReturn)}
                    </td>
                    <td className="py-2 pr-4 text-xs text-secondary-text">{pct(trade.benchReturn)}</td>
                    <td className={`py-2 pr-4 text-xs ${(trade.excessReturn ?? 0) >= 0 ? 'text-success' : 'text-danger'}`}>
                      {pct(trade.excessReturn)}
                    </td>
                    <td className="py-2 pr-4 text-xs text-secondary-text">{trade.exitReason ?? '--'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="报告文件" subtitle="Downloads">
        <div className="flex flex-wrap gap-2">
          {detail.files.map((file) => (
            <a
              key={file.name}
              className="rounded-lg border border-subtle px-3 py-2 text-xs text-secondary-text transition-colors hover:border-cyan/40 hover:text-cyan"
              href={swingBacktestApi.fileUrl(detail.runId, file.name)}
              target="_blank"
              rel="noreferrer"
            >
              {file.name} · {(file.size / 1024).toFixed(0)} KB
            </a>
          ))}
        </div>
      </Card>
    </div>
  );
};

const SwingBacktestPage: React.FC = () => {
  const [runs, setRuns] = useState<SwingRunSummary[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [detail, setDetail] = useState<SwingRunDetail | null>(null);
  const [isLoadingRuns, setIsLoadingRuns] = useState(true);
  const [isLoadingDetail, setIsLoadingDetail] = useState(false);
  const [pageError, setPageError] = useState<ParsedApiError | null>(null);

  const [strategy, setStrategy] = useState('trend_pullback_restart');
  const [start, setStart] = useState('2016-01-01');
  const [end, setEnd] = useState('2024-12-31');
  const [costTier, setCostTier] = useState('base');
  const [mode, setMode] = useState<'signal' | 'portfolio'>('signal');
  const [maxCodes, setMaxCodes] = useState('300');
  const [isStarting, setIsStarting] = useState(false);
  const [activeRun, setActiveRun] = useState<SwingRunStatus | null>(null);
  const pollTimer = useRef<number | null>(null);

  useEffect(() => {
    document.title = '波段回测 - DSA';
    return () => {
      if (pollTimer.current != null) window.clearInterval(pollTimer.current);
    };
  }, []);

  const loadDetail = useCallback(async (runId: string) => {
    setIsLoadingDetail(true);
    try {
      const data = await swingBacktestApi.getRun(runId, 200);
      setDetail(data);
      setPageError(null);
    } catch (error) {
      console.error('加载波段回测详情失败', error);
      setPageError(getParsedApiError(error));
    } finally {
      setIsLoadingDetail(false);
    }
  }, []);

  const refreshRuns = useCallback(async (preferredId?: string) => {
    setIsLoadingRuns(true);
    try {
      const items = await swingBacktestApi.listRuns();
      setRuns(items);
      setSelectedId((current) => {
        const candidate = preferredId || current;
        if (candidate && items.some((item) => item.runId === candidate)) return candidate;
        return items.find((item) => item.runId === 'mvp')?.runId || items[0]?.runId || '';
      });
      setPageError(null);
    } catch (error) {
      console.error('加载波段回测列表失败', error);
      setPageError(getParsedApiError(error));
    } finally {
      setIsLoadingRuns(false);
    }
  }, []);

  useEffect(() => {
    void refreshRuns();
  }, [refreshRuns]);

  useEffect(() => {
    if (selectedId) void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  const stopPolling = () => {
    if (pollTimer.current != null) {
      window.clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  };

  const handleStart = async () => {
    setIsStarting(true);
    setPageError(null);
    try {
      const status = await swingBacktestApi.createRun({
        strategy,
        start,
        end,
        costTier,
        mode,
        maxCodes: Number(maxCodes) || 300,
      });
      setActiveRun(status);
      stopPolling();
      pollTimer.current = window.setInterval(async () => {
        try {
          const latest = await swingBacktestApi.getStatus(status.runId);
          setActiveRun(latest);
          if (latest.status === 'completed') {
            stopPolling();
            await refreshRuns(latest.runId);
          } else if (latest.status === 'failed') {
            stopPolling();
            setPageError({
              title: '回测失败',
              message: latest.error || '回测失败',
              rawMessage: latest.error || '',
              category: 'unknown',
            });
          }
        } catch (error) {
          console.error('轮询回测状态失败', error);
        }
      }, 2500);
    } catch (error) {
      setPageError(getParsedApiError(error));
    } finally {
      setIsStarting(false);
    }
  };

  const running = activeRun?.status === 'running';
  const selectedSummary = runs.find((item) => item.runId === selectedId);

  return (
    <AppPage>
      <div className="space-y-4">
        <PageHeader
          eyebrow="Swing Backtest"
          title="波段回测"
          description="趋势回调再启动 / 箱体突破的信号级或组合级回测；相对同市值同波动匹配组合评估 alpha。"
          actions={(
            <Button variant="secondary" size="sm" onClick={() => void refreshRuns()} isLoading={isLoadingRuns}>
              刷新运行
            </Button>
          )}
        />

        {pageError ? <ApiErrorAlert error={pageError} /> : null}

        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
          <Card title="运行结果" subtitle="Runs">
            {runs.length === 0 && !isLoadingRuns ? (
              <EmptyState
                title="还没有可展示的回测"
                description="先运行 scripts/mvp_edge_check.py 或 scripts/run_backtest.py，或使用右侧“新建回测”。"
              />
            ) : (
              <div className="space-y-3">
                <Select
                  label="选择运行"
                  value={selectedId}
                  onChange={setSelectedId}
                  options={runs.map((item) => ({
                    value: item.runId,
                    label: `${item.title} · ${item.decision ?? item.status} · ${item.nTrades} 笔`,
                  }))}
                  placeholder="选择一次回测"
                  disabled={isLoadingRuns}
                />
                {selectedSummary ? (
                  <div className="flex flex-wrap items-center gap-2 text-xs text-secondary-text">
                    <Badge variant={decisionMeta(selectedSummary.decision).variant}>
                      {decisionMeta(selectedSummary.decision).label}
                    </Badge>
                    <span>{modeLabel(selectedSummary.mode)}</span>
                    <span>{selectedSummary.nTrades} 笔</span>
                    <span>创建于 {selectedSummary.createdAt?.slice(0, 19).replace('T', ' ') ?? '--'}</span>
                  </div>
                ) : null}
                {isLoadingDetail ? (
                  <div className="py-10 text-center text-sm text-secondary-text">加载详情中…</div>
                ) : detail ? (
                  <RunDetailPanel detail={detail} />
                ) : null}
              </div>
            )}
          </Card>

          <Card title="新建回测" subtitle="Local Cache Only">
            <div className="space-y-3">
              <Select label="策略" value={strategy} onChange={setStrategy} options={STRATEGIES} />
              <div className="grid grid-cols-2 gap-3">
                <Input label="开始日期" type="date" value={start} onChange={(event) => setStart(event.target.value)} />
                <Input label="结束日期" type="date" value={end} onChange={(event) => setEnd(event.target.value)} />
              </div>
              <Select label="成本档" value={costTier} onChange={setCostTier} options={COST_TIERS} />
              <Select
                label="评估模式"
                value={mode}
                onChange={(value) => setMode(value as 'signal' | 'portfolio')}
                options={MODES}
              />
              <Input
                label="最多股票数"
                type="number"
                min={1}
                max={800}
                value={maxCodes}
                onChange={(event) => setMaxCodes(event.target.value)}
                hint="只读取服务器本地缓存；全市场回测请用 CLI"
              />
              <Button className="w-full" onClick={() => void handleStart()} isLoading={isStarting || running} glow>
                开始回测
              </Button>
              {activeRun ? (
                <div className="rounded-xl border border-subtle/70 bg-card/50 p-3 text-xs text-secondary-text">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono">{activeRun.runId}</span>
                    <Badge variant={activeRun.status === 'completed' ? 'success' : activeRun.status === 'failed' ? 'danger' : 'info'}>
                      {activeRun.status}
                    </Badge>
                  </div>
                  <p className="mt-1">{activeRun.message ?? '等待中'}</p>
                  {activeRun.error ? <p className="mt-1 text-danger">{activeRun.error}</p> : null}
                </div>
              ) : (
                <p className="text-xs text-secondary-text">
                  触发运行要求 `data/cache/swing_bars_full/raw` 已存在；同一时间只允许一个回测。
                </p>
              )}
            </div>
          </Card>
        </div>
      </div>
    </AppPage>
  );
};

export default SwingBacktestPage;
