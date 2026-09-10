/** Componentes reutilizados pelas paginas. */

import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { api } from '../api/client'
import { MODE_LABEL, money, rodapeDoStat, toNumber } from '../api/format'
import type { CapitalStatus, Health, TradingConfig } from '../api/types'

export function useHealth() {
  return useQuery<Health>({
    queryKey: ['health'],
    queryFn: api.health,
    // Agentes parados (503) sao um estado legitimo do sistema, nao um erro a
    // ser retentado indefinidamente.
    retry: false,
  })
}

/**
 * Moeda de cotacao vigente, lida da configuracao de negocio (banco).
 *
 * Nenhuma tela pode chutar isso. A migracao de BRL para USDC nao mexeu em
 * codigo nenhum do frontend, e ainda assim o dashboard passou a mentir a
 * moeda — porque `money()` tinha `'USDT'` como padrao de parametro. Enquanto a
 * configuracao nao chegou, devolve `''`: numero sem unidade e incompleto, mas
 * numero com a unidade errada e falso.
 *
 * `retry` NAO e `false` aqui, ao contrario do `useHealth`. A diferenca importa:
 * 503 do health e um estado legitimo do sistema (agentes parados), mas uma falha
 * ao ler a configuracao de negocio e sempre acidente — e o preco dela era alto
 * demais para nao insistir. Com moeda `''`, toda a aplicacao perdia a unidade
 * por cinco minutos (o `staleTime`) sem uma palavra na tela. Tres tentativas com
 * espera crescente cobrem a falha transitoria, que e a comum.
 *
 * (O outro efeito daquele `''` — o caixa reaparecendo como posicao aberta —
 * deixou de depender disto: `posicoesAbertas` filtra por `average_price` nulo,
 * que e estrutural, e nao apenas pelo nome da moeda.)
 */
export function useQuoteCurrency(): string {
  const config = useQuery<TradingConfig>({
    queryKey: ['tradingConfig'],
    queryFn: api.tradingConfig,
    retry: 3,
    retryDelay: (tentativa) => Math.min(1000 * 2 ** tentativa, 8000),
    // Trocar a moeda de cotacao e um gesto raro e deliberado; nao vale
    // revalidar isso a cada montagem de tela.
    staleTime: 5 * 60 * 1000,
  })
  return config.data?.quote_currency ?? ''
}

export function Card({
  title,
  subtitle,
  children,
  action,
}: {
  title?: string
  subtitle?: string
  children: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="card">
      {(title || action) && (
        <div className="row-tight" style={{ justifyContent: 'space-between', marginBottom: 10 }}>
          <div>
            {title && <div className="card-title" style={{ marginBottom: 0 }}>{title}</div>}
            {subtitle && (
              <div className="hint" style={{ marginTop: 3 }}>
                {subtitle}
              </div>
            )}
          </div>
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
  // A decisao de mostrar variacao, dica ou nada mora em `rodapeDoStat`, que e
  // funcao pura e verificada — inclusive a diferenca entre `null` ("a API nao
  // tem essa comparacao") e `undefined` ("esta tela decidiu nao comparar").
  const rodape = rodapeDoStat(change, hint)
  return (
    <div className="card">
      <div className="card-title">{label}</div>
      <div className="stat-value">{value}</div>
      {rodape && <div className={`stat-sub ${rodape.tom}`}>{rodape.texto}</div>}
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
  // O texto NAO repete o rotulo que vem logo antes dele. "Simulação ·
  // Simulação — nenhuma ordem sai da máquina" era o que estava na tela.
  const message =
    mode === 'live'
      ? 'ordens estão sendo enviadas à exchange com dinheiro real.'
      : mode === 'testnet'
        ? 'ordens reais na sandbox da exchange, com dinheiro fictício.'
        : 'nenhuma ordem sai da máquina.'

  const pares = health.symbols.length

  return (
    <div className={`mode-banner ${className}`}>
      <span>{mode === 'live' ? '⚠' : '●'}</span>
      <span>
        <strong>{MODE_LABEL[mode] ?? mode}</strong> · {message}
      </span>
      <span
        className="faint"
        style={{ marginLeft: 'auto', fontSize: 12 }}
        // A lista inteira ocupava duas linhas do banner com 16 pares. A
        // contagem cabe numa linha e o detalhe fica no title.
        title={health.symbols.join(', ')}
      >
        {health.exchange} ·{' '}
        {pares === 0 ? 'sem pares' : pares === 1 ? '1 par' : `${pares} pares`}
        {health.symbols_source === 'descoberta' && ' · descoberta automática'}
      </span>
    </div>
  )
}

/**
 * Aviso de que nenhuma ordem e possivel com o patrimonio e os limites atuais.
 *
 * Fica no topo porque o sintoma sem ele e enganoso: o sistema parece saudavel,
 * coleta dados e gera sinais, mas rejeita todos em silencio.
 */
export function SizingBanner({ health }: { health: Health | undefined }) {
  if (!health || health.sizing_feasible) return null
  return (
    <div className="alert-banner">
      <div>
        <strong>Nenhuma ordem é possível com o patrimônio atual.</strong>{' '}
        <span className="muted">
          {health.sizing_detail} Os agentes continuam gerando sinais, mas todos serão
          rejeitados. Ajuste os limites em <strong>Risco</strong> ou aumente o patrimônio.
        </span>
      </div>
    </div>
  )
}

/**
 * Saldo disponível que o sistema não pode usar até você autorizar.
 *
 * O portão existe porque um depósito não é uma ordem: dinheiro que entra na
 * conta por qualquer motivo não deveria virar exposição sozinho. Mas dinheiro
 * autorizado e parado é o problema oposto — e é por isso que este aviso pede
 * ação em vez de apenas informar.
 */
export function CapitalGateBanner({
  status,
  onAuthorize,
  authorizing,
}: {
  status: CapitalStatus | undefined
  onAuthorize: () => void
  authorizing?: boolean
}) {
  if (!status || !status.gate_active) return null
  if (!(toNumber(status.unauthorized_value) > 0)) return null

  // A moeda vem do proprio status, que e a fonte da verdade do portao.
  const moeda = status.quote_currency

  return (
    <div className="alert-banner">
      <div>
        <strong>
          {money(status.unauthorized_value, moeda)} disponíveis e não autorizados.
        </strong>{' '}
        <span className="muted">
          O patrimônio é {money(status.total_value, moeda)} e o capital autorizado a operar é{' '}
          {money(status.authorized_capital ?? '0', moeda)}. O sistema{' '}
          <strong>não vai usar a diferença</strong> até você autorizar.
        </span>
      </div>
      {/* `primary`, nao `btn btn-primary`: nao existe regra `.btn` nem
          `.btn-primary` no CSS, e este botao — o que libera capital para operar —
          renderizava com a aparencia de um botao secundario qualquer. */}
      <button className="primary" onClick={onAuthorize} disabled={authorizing}>
        {authorizing ? 'Autorizando…' : 'Autorizar todo o saldo'}
      </button>
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
