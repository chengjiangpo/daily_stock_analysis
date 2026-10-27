import apiClient from './index';
import { toCamelCase } from './utils';
import { API_BASE_URL } from '../utils/constants';
import type {
  SwingRunDetail,
  SwingRunRequest,
  SwingRunStatus,
  SwingRunSummary,
} from '../types/swingBacktest';

export const swingBacktestApi = {
  listRuns: async (): Promise<SwingRunSummary[]> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/swing-backtest/runs');
    const data = toCamelCase<{ items: SwingRunSummary[] }>(response.data);
    return data.items || [];
  },

  getRun: async (runId: string, tradeLimit = 200): Promise<SwingRunDetail> => {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/swing-backtest/runs/${encodeURIComponent(runId)}`,
      { params: { trade_limit: tradeLimit } },
    );
    return toCamelCase<SwingRunDetail>(response.data);
  },

  getStatus: async (runId: string): Promise<SwingRunStatus> => {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/swing-backtest/runs/${encodeURIComponent(runId)}/status`,
    );
    return toCamelCase<SwingRunStatus>(response.data);
  },

  createRun: async (request: SwingRunRequest): Promise<SwingRunStatus> => {
    const payload: Record<string, unknown> = {
      strategy: request.strategy,
      start: request.start,
      end: request.end,
      cost_tier: request.costTier,
      mode: request.mode,
      max_codes: request.maxCodes,
    };
    if (request.aum != null) payload.aum = request.aum;
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/swing-backtest/runs', payload);
    return toCamelCase<SwingRunStatus>(response.data);
  },

  fileUrl: (runId: string, filename: string): string =>
    `${API_BASE_URL}/api/v1/swing-backtest/runs/${encodeURIComponent(runId)}/files/${encodeURIComponent(filename)}`,
};
