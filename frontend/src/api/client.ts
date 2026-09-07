/** Cliente HTTP da API. */

import type {
  AuditEntry,
  BacktestResult,
  EquityPoint,
  Health,
  NotificationConfig,
  NotificationTestResult,
  Order,
  PortfolioSummary,
  RiskConfig,
  RiskEvent,
  Signal,
  StrategyInfo,
  TradePage,
} from './types'

const BASE = '/api'

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }

  /** 503 significa "agentes parados", nao "sistema quebrado". */
  get agentsAreDown(): boolean {
    return this.status === 503
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })

  if (!response.ok) {
    // O FastAPI devolve o motivo em `detail`; propagar isso e o que permite
    // mostrar "cooldown ativo" em vez de "erro 400".
    let detail = response.statusText
    try {
      const body = await response.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* resposta sem corpo JSON */
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<Health>('/health'),

  portfolio: () => request<PortfolioSummary>('/portfolio'),
  portfolioHistory: (days: number) => request<EquityPoint[]>(`/portfolio/history?days=${days}`),

  trades: (params: Record<string, string | number | undefined>) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== '') query.set(key, String(value))
    }
    return request<TradePage>(`/trades?${query}`)
  },
  createManualTrade: (payload: Record<string, unknown>) =>
    request<unknown>('/trades/manual', { method: 'POST', body: JSON.stringify(payload) }),
  deleteTrade: (id: string) => request<void>(`/trades/${id}`, { method: 'DELETE' }),
  exportUrl: () => `${BASE}/trades/export`,

  signals: (limit = 50) => request<Signal[]>(`/signals?limit=${limit}`),
  orders: (limit = 50) => request<Order[]>(`/orders?limit=${limit}`),
  riskEvents: (limit = 50) => request<RiskEvent[]>(`/risk/events?limit=${limit}`),
  audit: (limit = 50) => request<AuditEntry[]>(`/audit?limit=${limit}`),

  riskConfig: () => request<RiskConfig>('/risk/config'),
  updateRiskConfig: (payload: Record<string, unknown>) =>
    request<RiskConfig>('/risk/config', {
      method: 'PUT',
      // `confirm` e obrigatorio no backend: alterar limites afeta dinheiro real.
      body: JSON.stringify({ ...payload, confirm: true }),
    }),
  resetCircuitBreaker: () =>
    request<RiskConfig>('/risk/circuit-breaker/reset', { method: 'POST' }),

  strategies: () => request<StrategyInfo[]>('/strategies'),
  pauseAgent: (name: string) => request<unknown>(`/agents/${name}/pause`, { method: 'POST' }),
  resumeAgent: (name: string) => request<unknown>(`/agents/${name}/resume`, { method: 'POST' }),
  pauseAll: () => request<unknown>('/agents/pause-all', { method: 'POST' }),
  resumeAll: () => request<unknown>('/agents/resume-all', { method: 'POST' }),

  notificationConfig: () => request<NotificationConfig>('/notifications/config'),
  updateNotificationConfig: (payload: Record<string, unknown>) =>
    request<NotificationConfig>('/notifications/config', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  testNotification: () =>
    request<{ results: NotificationTestResult[]; detail: string }>('/notifications/test', {
      method: 'POST',
    }),

  backtest: (payload: Record<string, unknown>) =>
    request<BacktestResult>('/backtest', { method: 'POST', body: JSON.stringify(payload) }),
}
