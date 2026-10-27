/**
 * 波段回测 API 类型（对应 api/v1/schemas/swing_backtest.py）
 */

export interface SwingRunSummary {
  runId: string;
  title: string;
  status: string;
  strategy?: string;
  mode?: string;
  costTier?: string;
  start?: string;
  end?: string;
  createdAt?: string;
  nTrades: number;
  totalReturn?: number;
  cagr?: number;
  sharpe?: number;
  maxDrawdown?: number;
  winRate?: number;
  profitFactor?: number;
  meanExcess?: number;
  ciLow?: number;
  ciHigh?: number;
  netExpectancy?: number;
  decision?: string;
  decisionReason?: string;
  codes: number;
  files: string[];
}

export interface SwingRunsResponse {
  items: SwingRunSummary[];
}

export interface SwingEquityPoint {
  date: string;
  equity?: number;
  drawdown?: number;
  cash?: number;
  marketValue?: number;
}

export interface SwingTradeItem {
  code: string;
  signalDate?: string;
  entryDate?: string;
  exitDate?: string;
  entryPrice?: number;
  exitPrice?: number;
  grossReturn?: number;
  benchReturn?: number;
  excessReturn?: number;
  netReturn?: number;
  pnl?: number;
  rMultiple?: number;
  exitReason?: string;
  holdDays: number;
}

export interface SwingDiagnosticItem {
  conditionName: string;
  total: number;
  failed: number;
  passRate?: number;
}

export interface SwingFileItem {
  name: string;
  size: number;
}

export interface SwingRunDetail extends Omit<SwingRunSummary, 'files'> {
  equity: SwingEquityPoint[];
  trades: SwingTradeItem[];
  rejectionReasons: Record<string, number>;
  diagnostics: SwingDiagnosticItem[];
  files: SwingFileItem[];
  summaryMarkdown: string;
}

export interface SwingRunRequest {
  strategy: string;
  start: string;
  end: string;
  costTier: string;
  mode: 'signal' | 'portfolio';
  maxCodes: number;
  aum?: number;
}

export interface SwingRunStatus {
  runId: string;
  status: string;
  message?: string;
  error?: string;
  createdAt?: string;
  finishedAt?: string;
}
