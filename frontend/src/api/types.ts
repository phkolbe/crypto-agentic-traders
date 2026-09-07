/**
 * Tipos espelhando os schemas da API.
 *
 * Valores monetarios chegam como `string`, nao `number` — de proposito.
 * JSON nao tem decimal e o `number` do JavaScript e float64: `0.1 + 0.2` da
 * `0.30000000000000004`. Numa tela que mostra dinheiro isso vira centavo errado.
 * A conversao acontece apenas na formatacao (`format.ts`), nunca no transporte.
 */

/** Valor monetario. O alias existe para deixar explicito o que e dinheiro. */
export type Money = string

export interface Position {
  asset: string
  quantity: Money
  average_price: Money | null
  current_price: Money | null
  market_value: Money
  unrealized_pnl: Money
}

export interface Portfolio {
  timestamp: string
  total_value: Money
  cash_value: Money
  positions_value: Money
  realized_pnl: Money
  unrealized_pnl: Money
  allocations: Record<string, number>
  positions: Position[]
  mode: string
}

export interface PortfolioSummary {
  current: Portfolio | null
  change_24h_pct: number | null
  change_7d_pct: number | null
  change_since_start_pct: number | null
  trades_today: number
}

export interface EquityPoint {
  timestamp: string
  total_value: Money
}

export interface Trade {
  id: string
  executed_at: string
  exchange: string
  symbol: string
  side: 'buy' | 'sell'
  quantity: Money
  price: Money
  notional: Money
  fee: Money
  fee_currency: string | null
  origin: 'agent' | 'manual'
  strategy: string | null
  signal_id: string | null
  mode: string
  realized_pnl: Money | null
  notes: string | null
}

export interface TradePage {
  items: Trade[]
  total: number
  limit: number
  offset: number
}

export interface Signal {
  id: string
  created_at: string
  exchange: string
  symbol: string
  timeframe: string
  strategy: string
  direction: 'long' | 'short' | 'flat'
  confidence: number
  reason: string
  reference_price: Money
  indicators: Record<string, number | null>
}

export interface RiskEvent {
  id: string
  created_at: string
  event_type: string
  signal_id: string | null
  decision: 'approved' | 'rejected' | null
  reasons: string[]
  approved_quantity: Money | null
  approved_notional: Money | null
  stop_loss: Money | null
  take_profit: Money | null
  snapshot: Record<string, unknown>
}

export interface Order {
  id: string
  created_at: string
  client_order_id: string
  exchange_order_id: string | null
  symbol: string
  side: string
  order_type: string
  quantity: Money
  price: Money | null
  notional: Money
  stop_loss: Money | null
  take_profit: Money | null
  status: string
  filled_quantity: Money
  average_price: Money | null
  strategy: string | null
  mode: string
  error: string | null
}

export interface RiskConfig {
  max_order_notional: Money
  max_order_pct_portfolio: number
  max_asset_exposure_pct: number
  max_open_positions: number
  min_order_notional: Money
  stop_loss_pct: number
  take_profit_pct: number
  daily_loss_limit_pct: number
  weekly_loss_limit_pct: number
  min_signal_confidence: number
  asset_whitelist: string[]
  symbol_whitelist: string[]
  cooldown_seconds: number
  circuit_breaker_active: boolean
  circuit_breaker_reason: string | null
}

export interface AgentStatus {
  state: string
  running: boolean
  paused: boolean
  last_heartbeat: string | null
  stale: boolean
  last_error: string | null
}

export interface NotificationChannelStatus {
  configured: boolean
  missing_settings: string[]
}

export interface NotificationConfig {
  email_enabled: boolean
  email_to: string | null
  whatsapp_enabled: boolean
  whatsapp_to: string | null
  email: NotificationChannelStatus
  whatsapp: NotificationChannelStatus
}

export interface NotificationTestResult {
  channel: string
  ok: boolean
  detail: string
}

export interface Health {
  mode: 'dry_run' | 'testnet' | 'live'
  exchange: string
  symbols: string[]
  /** `configurado` (SYMBOLS no .env) ou `descoberta` (varredura de mercado). */
  symbols_source: string
  strategies: string[]
  started_at: string | null
  circuit_breaker_active: boolean
  /** False quando patrimônio e limites tornam qualquer ordem impossível. */
  sizing_feasible: boolean
  sizing_detail: string | null
  agents: Record<string, AgentStatus>
}

export interface StrategyInfo {
  name: string
  description: string
  active: boolean
}

export interface AuditEntry {
  id: number
  timestamp: string
  actor: string
  action: string
  target: string | null
  detail: string | null
  before: Record<string, unknown>
  after: Record<string, unknown>
}

export interface BacktestResult {
  summary: {
    strategy: string
    symbol: string
    timeframe: string
    start: string
    end: string
    initial_balance: number
    final_value: number
    total_return_pct: number
    buy_and_hold_pct: number
    max_drawdown_pct: number
    trades: number
    closed_trades: number
    win_rate: number
    profit_factor: number
    signals_generated: number
    signals_rejected: number
    top_rejection_reasons: [string, number][]
  }
  equity_curve: { timestamp: string; value: number }[]
  trades: {
    timestamp: string
    side: string
    quantity: number
    price: number
    notional: number
    fee: number
    reason: string
    realized_pnl: number | null
  }[]
}
