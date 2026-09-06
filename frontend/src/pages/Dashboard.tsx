import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import {
  Area,
  AreaChart,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { api } from '../api/client'
import { money, percent, quantity, shortDate, signedPercent, timeOnly } from '../api/format'
import type { EquityPoint, PortfolioSummary, RiskEvent } from '../api/types'
import {
  Card,
  CircuitBreakerBanner,
  Empty,
  Loading,
  ModeBanner,
  Stat,
  useHealth,
} from '../components/Shared'

/** Paleta das fatias de alocacao; cores repetem se houver muitos ativos. */
const SLICE_COLORS = ['#58a6ff', '#3fb950', '#d29922', '#bc8cff', '#f85149', '#39c5cf']

const RANGES = [
  { days: 1, label: '24h' },
  { days: 7, label: '7d' },
  { days: 30, label: '30d' },
  { days: 90, label: '90d' },
]

export default function Dashboard({ feed }: { feed: { event: string; data: any; at: string }[] }) {
  const [days, setDays] = useState(30)
  const queryClient = useQueryClient()
  const health = useHealth()

  const portfolio = useQuery<PortfolioSummary>({ queryKey: ['portfolio'], queryFn: api.portfolio })
  const history = useQuery<EquityPoint[]>({
    queryKey: ['portfolioHistory', days],
    queryFn: () => api.portfolioHistory(days),
  })
  const riskEvents = useQuery<RiskEvent[]>({
    queryKey: ['riskEvents'],
    queryFn: () => api.riskEvents(15),
  })

  const reset = useMutation({
    mutationFn: api.resetCircuitBreaker,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['health'] })
      queryClient.invalidateQueries({ queryKey: ['riskConfig'] })
    },
  })

  const current = portfolio.data?.current
  const curve = (history.data ?? []).map((point) => ({
    time: point.timestamp,
    value: Number(point.total_value),
  }))
  const allocations = Object.entries(current?.allocations ?? {})
    .map(([asset, share]) => ({ asset, share }))
    .sort((a, b) => b.share - a.share)

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Dashboard</h1>
          <p>Patrimônio, alocação e o que os agentes decidiram.</p>
        </div>
      </div>

      <ModeBanner health={health.data} />
      <CircuitBreakerBanner
        active={health.data?.circuit_breaker_active ?? false}
        onReset={() => reset.mutate()}
        resetting={reset.isPending}
      />

      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <Stat
          label="Patrimônio total"
          value={current ? money(current.total_value) : '—'}
          change={portfolio.data?.change_24h_pct}
          hint="em 24h"
        />
        <Stat
          label="Caixa disponível"
          value={current ? money(current.cash_value) : '—'}
          hint={current ? `${money(current.positions_value)} em posições` : undefined}
        />
        <Stat
          label="PnL realizado"
          value={current ? money(current.realized_pnl) : '—'}
          hint={current ? `${money(current.unrealized_pnl)} não realizado` : undefined}
        />
        <Stat
          label="Operações hoje"
          value={String(portfolio.data?.trades_today ?? 0)}
          change={portfolio.data?.change_7d_pct}
          hint="em 7 dias"
        />
      </div>

      <div className="grid grid-2" style={{ marginBottom: 14 }}>
        <Card
          title="Evolução do patrimônio"
          action={
            <div className="row-tight">
              {RANGES.map((range) => (
                <button
                  key={range.days}
                  className={`small ${days === range.days ? 'primary' : ''}`}
                  onClick={() => setDays(range.days)}
                >
                  {range.label}
                </button>
              ))}
            </div>
          }
        >
          {history.isLoading ? (
            <Loading />
          ) : curve.length < 2 ? (
            <Empty>
              Ainda não há histórico suficiente para o gráfico.
              <br />O Portfolio Agent grava um ponto por minuto enquanto o sistema roda.
            </Empty>
          ) : (
            <ResponsiveContainer width="100%" height={260}>
              <AreaChart data={curve} margin={{ top: 6, right: 6, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="equity" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#58a6ff" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="#58a6ff" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <XAxis
                  dataKey="time"
                  tickFormatter={(value) => (days <= 1 ? timeOnly(value) : shortDate(value))}
                  stroke="var(--text-faint)"
                  fontSize={11}
                  tickLine={false}
                  axisLine={false}
                  minTickGap={40}
                />
                <YAxis
                  stroke="var(--text-faint)"
                  fontSize={11}
                  tickLine={false}
                  axisLine={false}
                  width={62}
                  // Escala focada na variacao real: comecar em zero achataria a
                  // curva e esconderia justamente o que interessa observar.
                  domain={['dataMin - dataMin * 0.01', 'dataMax + dataMax * 0.01']}
                  tickFormatter={(value) => Number(value).toFixed(0)}
                />
                <Tooltip
                  contentStyle={{
                    background: 'var(--bg-elevated)',
                    border: '1px solid var(--border)',
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                  labelFormatter={(value) => new Date(value as string).toLocaleString('pt-BR')}
                  formatter={(value) => [money(Number(value)), 'patrimônio']}
                />
                <Area
                  type="monotone"
                  dataKey="value"
                  stroke="#58a6ff"
                  strokeWidth={2}
                  fill="url(#equity)"
                  // O grafico e reavaliado a cada snapshot de portfolio; sem
                  // isso a curva se redesenharia do zero a cada minuto.
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </Card>

        <Card title="Alocação por ativo">
          {allocations.length === 0 ? (
            <Empty>Sem posições apuradas.</Empty>
          ) : (
            <div className="row" style={{ alignItems: 'center', gap: 18 }}>
              {/* PieChart com tamanho fixo em vez de ResponsiveContainer: dentro
                  de um flex, o container responsivo mede 0 e o donut some. */}
              <div style={{ flexShrink: 0 }}>
                <PieChart width={180} height={180}>
                  <Pie
                    data={allocations}
                    dataKey="share"
                    nameKey="asset"
                    innerRadius={48}
                    outerRadius={78}
                    // Sem espacamento quando ha uma fatia so: num circulo
                    // completo o paddingAngle zera o setor.
                    paddingAngle={allocations.length > 1 ? 2 : 0}
                    stroke="none"
                    // Sem animacao de entrada: quando ela nao completa, o setor
                    // fica congelado em um quadro degenerado e o donut some.
                    // Alem disso o dashboard re-renderiza a cada evento do
                    // WebSocket, e a animacao recomecaria do zero toda vez.
                    isAnimationActive={false}
                  >
                    {allocations.map((entry, index) => (
                      <Cell
                        key={entry.asset}
                        fill={SLICE_COLORS[index % SLICE_COLORS.length]}
                      />
                    ))}
                  </Pie>
                  <Tooltip
                    contentStyle={{
                      background: 'var(--bg-elevated)',
                      border: '1px solid var(--border)',
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                    formatter={(value) => percent(Number(value))}
                  />
                </PieChart>
              </div>

              <div style={{ flex: 1, minWidth: 160 }}>
                {allocations.map((entry, index) => (
                  <div
                    key={entry.asset}
                    className="row-tight"
                    style={{ justifyContent: 'space-between', padding: '4px 0' }}
                  >
                    <span className="row-tight" style={{ gap: 7 }}>
                      <span
                        className="dot"
                        style={{ background: SLICE_COLORS[index % SLICE_COLORS.length] }}
                      />
                      {entry.asset}
                    </span>
                    <span className="num muted">{percent(entry.share)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
      </div>

      <div className="grid grid-2">
        <Card title="Posições">
          {!current || current.positions.length === 0 ? (
            <Empty>Nenhuma posição aberta.</Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Ativo</th>
                    <th className="right">Quantidade</th>
                    <th className="right">Preço médio</th>
                    <th className="right">Atual</th>
                    <th className="right">PnL</th>
                  </tr>
                </thead>
                <tbody>
                  {current.positions.map((position) => {
                    const pnl = Number(position.unrealized_pnl)
                    return (
                      <tr key={position.asset}>
                        <td><strong>{position.asset}</strong></td>
                        <td className="right">{quantity(position.quantity)}</td>
                        <td className="right muted">
                          {position.average_price ? money(position.average_price, '') : '—'}
                        </td>
                        <td className="right">
                          {position.current_price ? money(position.current_price, '') : '—'}
                        </td>
                        <td className={`right ${pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : 'muted'}`}>
                          {position.average_price ? money(pnl, '') : '—'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card title="Atividade em tempo real">
          {feed.length === 0 ? (
            <Empty>
              Aguardando eventos.
              <br />
              Sinais, decisões de risco e execuções aparecem aqui assim que acontecem.
            </Empty>
          ) : (
            <div className="feed">
              {feed.map((item, index) => (
                <div key={index} className={`feed-item ${item.event}`}>
                  <span className="feed-time">{timeOnly(item.at)}</span>
                  <span>{describeEvent(item.event, item.data)}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <div style={{ marginTop: 14 }}>
        <Card title="Decisões recentes do Risk Manager">
          {riskEvents.isLoading ? (
            <Loading />
          ) : (riskEvents.data ?? []).length === 0 ? (
            <Empty>Nenhuma decisão registrada ainda.</Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Quando</th>
                    <th>Par</th>
                    <th>Decisão</th>
                    <th>Motivo</th>
                    <th className="right">Valor</th>
                  </tr>
                </thead>
                <tbody>
                  {(riskEvents.data ?? []).map((event) => (
                    <tr key={event.id}>
                      <td className="muted">{timeOnly(event.created_at)}</td>
                      <td>{String(event.snapshot?.symbol ?? '—')}</td>
                      <td>
                        <span
                          className={`badge ${
                            event.decision === 'approved' ? 'badge-positive' : 'badge-neutral'
                          }`}
                        >
                          {event.decision === 'approved' ? 'aprovado' : 'rejeitado'}
                        </span>
                      </td>
                      <td className="muted" style={{ maxWidth: 420 }}>
                        {event.reasons.length > 0 ? event.reasons.join(' · ') : 'dentro de todos os limites'}
                      </td>
                      <td className="right">
                        {event.approved_notional ? money(event.approved_notional) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  )
}

/** Converte o payload cru do WebSocket em uma frase legivel. */
function describeEvent(event: string, data: any): string {
  switch (event) {
    case 'signal':
      return `Sinal ${data.direction} em ${data.symbol} por ${data.strategy} (confiança ${percent(
        data.confidence,
        0,
      )}) — ${data.reason}`
    case 'risk_assessment':
      return data.decision === 'approved'
        ? `Risk Manager aprovou ${money(data.approved_notional ?? '0')}`
        : `Risk Manager rejeitou: ${(data.reasons ?? []).join(' · ')}`
    case 'order_result':
      return `Ordem ${data.status} — ${quantity(data.filled_quantity)} @ ${money(
        data.average_price ?? '0',
        '',
      )}`
    case 'portfolio':
      return `Patrimônio atualizado: ${money(data.total_value)} (${signedPercent(
        Number(data.unrealized_pnl) / Math.max(Number(data.total_value), 1),
      )} não realizado)`
    case 'alert':
      return `ALERTA: ${data.reason ?? data.type}`
    default:
      return event
  }
}
