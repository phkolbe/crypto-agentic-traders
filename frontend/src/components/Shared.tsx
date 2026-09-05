/** Componentes reutilizados pelas paginas. */

import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { api } from '../api/client'
import { MODE_LABEL, signedPercent } from '../api/format'
import type { Health } from '../api/types'

export function useHealth() {
  return useQuery<Health>({
    queryKey: ['health'],
    queryFn: api.health,
    // Agentes parados (503) sao um estado legitimo do sistema, nao um erro a
    // ser retentado indefinidamente.
    retry: false,
  })
}

export function Card({
  title,
  children,
  action,
}: {
  title?: string
  children: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="card">
      {(title || action) && (
        <div className="row-tight" style={{ justifyContent: 'space-between', marginBottom: 10 }}>
          {title && <div className="card-title" style={{ marginBottom: 0 }}>{title}</div>}
          {action}
        </div>
      )}
      {children}
    </div>
  )
}

export function Stat({
  label,
  value,
  change,
  hint,
}: {
  label: string
  value: string
  change?: number | null
  hint?: string
}) {
  const tone = change === null || change === undefined ? '' : change >= 0 ? 'positive' : 'negative'
  return (
    <div className="card">
      <div className="card-title">{label}</div>
      <div className="stat-value">{value}</div>
      {change !== undefined && (
        <div className={`stat-sub ${tone}`}>{signedPercent(change)}{hint ? ` ${hint}` : ''}</div>
      )}
      {change === undefined && hint && <div className="stat-sub">{hint}</div>}
    </div>
  )
}

/**
 * Faixa permanente com o modo de operacao.
 *
 * Fica visivel em todas as telas de proposito: a diferenca entre simulacao e
 * dinheiro real nunca deve depender de o operador lembrar do que configurou.
 */
export function ModeBanner({ health }: { health: Health | undefined }) {
  if (!health) return null
  const mode = health.mode
  const className =
    mode === 'live' ? 'mode-live' : mode === 'testnet' ? 'mode-testnet' : 'mode-simulated'
  const message =
    mode === 'live'
      ? 'DINHEIRO REAL — ordens estão sendo enviadas à exchange.'
      : mode === 'testnet'
        ? 'Testnet — ordens reais na sandbox da exchange, com dinheiro fictício.'
        : 'Simulação — nenhuma ordem sai da máquina.'

  return (
    <div className={`mode-banner ${className}`}>
      <span>{mode === 'live' ? '⚠' : '●'}</span>
      <span>
        <strong>{MODE_LABEL[mode] ?? mode}</strong> · {message}
      </span>
      <span className="faint" style={{ marginLeft: 'auto', fontSize: 12 }}>
        {health.exchange} · {health.symbols.join(', ')}
      </span>
    </div>
  )
}

export function CircuitBreakerBanner({
  active,
  reason,
  onReset,
  resetting,
}: {
  active: boolean
  reason?: string | null
  onReset?: () => void
  resetting?: boolean
}) {
  if (!active) return null
  return (
    <div className="alert-banner">
      <div>
        <strong>Circuit breaker acionado.</strong>{' '}
        <span className="muted">
          {reason ?? 'Os agentes de decisão estão pausados.'} O rearme é manual e propositalmente
          exige revisão antes de voltar a operar.
        </span>
      </div>
      {onReset && (
        <button className="danger" onClick={onReset} disabled={resetting}>
          {resetting ? 'Rearmando…' : 'Rearmar e retomar'}
        </button>
      )}
    </div>
  )
}

export function AgentsOffline() {
  return (
    <div className="card">
      <div className="empty">
        Os agentes não estão rodando neste processo.
        <br />
        <span className="mono" style={{ display: 'inline-block', marginTop: 8 }}>
          uv run crypto-traders run
        </span>
      </div>
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}

export function Loading() {
  return <div className="loading">Carregando…</div>
}

export function SideBadge({ side }: { side: string }) {
  return (
    <span className={`badge ${side === 'buy' ? 'badge-positive' : 'badge-negative'}`}>
      {side === 'buy' ? 'compra' : 'venda'}
    </span>
  )
}

export function OriginBadge({ origin }: { origin: string }) {
  return (
    <span className={`badge ${origin === 'manual' ? 'badge-warning' : 'badge-accent'}`}>
      {origin === 'manual' ? 'manual' : 'agente'}
    </span>
  )
}
