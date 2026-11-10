/** Cliente HTTP da API. */

import type {
  AnalisePortfolio,
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
  CapitalStatus,
  TradePage,
  TradingConfig,
  Money,
} from './types'
import type { ItemDoFeed } from './atividade'
import { pedidos, type LancamentoManual, type Pedido } from './mutacoes'
import { RAIZ } from './raiz'

const BASE = `${RAIZ}/api`

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

/** Envia um pedido montado em `mutacoes.ts` — o unico jeito de mudar estado daqui. */
function enviar<T>(pedido: Pedido): Promise<T> {
  return request<T>(pedido.caminho, {
    method: pedido.metodo,
    ...(pedido.corpo !== undefined ? { body: JSON.stringify(pedido.corpo) } : {}),
  })
}

export const api = {
  health: () => request<Health>('/health'),

  portfolio: () => request<PortfolioSummary>('/portfolio'),
  portfolioHistory: (periodo: { start: string; end: string }) =>
    request<EquityPoint[]>(`/portfolio/history?${new URLSearchParams(periodo)}`),
  portfolioAnalise: (periodo: { start: string; end: string }) =>
    request<AnalisePortfolio>(`/portfolio/analise?${new URLSearchParams(periodo)}`),

  trades: (params: Record<string, string | number | undefined>) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== '') query.set(key, String(value))
    }
    return request<TradePage>(`/trades?${query}`)
  },
  // As mutacoes abaixo levam `confirm: true` (ver `mutacoes.ts`): so podem ser
  // chamadas depois do passo de confirmacao na tela, que diz o que vai mudar.
  createManualTrade: ({
    lancamento,
    aceitarPrecoForaDoMercado,
  }: {
    lancamento: LancamentoManual
    aceitarPrecoForaDoMercado: boolean
  }) => enviar<unknown>(pedidos.lancarManual(lancamento, aceitarPrecoForaDoMercado)),
  deleteTrade: (id: string) => enviar<void>(pedidos.excluirLancamento(id)),
  exportUrl: () => `${BASE}/trades/export`,

  signals: (limit = 50) => request<Signal[]>(`/signals?limit=${limit}`),
  orders: (limit = 50) => request<Order[]>(`/orders?limit=${limit}`),
  riskEvents: (limit = 50) => request<RiskEvent[]>(`/risk/events?limit=${limit}`),
  audit: (limit = 50) => request<AuditEntry[]>(`/audit?limit=${limit}`),
  /** Historico do feed do Dashboard, na forma da mensagem do WebSocket. */
  atividade: (limite = 50) => request<ItemDoFeed[]>(`/atividade?limite=${limite}`),

  riskConfig: () => request<RiskConfig>('/risk/config'),
  updateRiskConfig: (payload: Record<string, unknown>) =>
    enviar<RiskConfig>(pedidos.alterarLimites(payload)),
  capitalStatus: () => request<CapitalStatus>('/risk/capital'),
  /** `patrimonioVisto`: o total que a tela mostrou no dialogo de confirmacao. */
  authorizeCapital: (patrimonioVisto: Money) =>
    enviar<CapitalStatus>(pedidos.autorizarCapital(patrimonioVisto)),

  resetCircuitBreaker: () => enviar<RiskConfig>(pedidos.rearmarCircuitBreaker()),

  tradingConfig: () => request<TradingConfig>('/trading/config'),
  updateTradingConfig: (payload: Record<string, unknown>) =>
    enviar<TradingConfig>(pedidos.alterarNegocio(payload)),

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
