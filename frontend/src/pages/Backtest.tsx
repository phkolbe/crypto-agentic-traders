import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import { ApiError, api } from '../api/client'
import { money, percent, price, shortDate, signedPercent } from '../api/format'
import type { BacktestResult, StrategyInfo } from '../api/types'
import { Card, Empty, Stat } from '../components/Shared'

const TIMEFRAMES = ['15m', '30m', '1h', '4h', '1d']

export default function Backtest() {
  const [form, setForm] = useState({
    symbol: 'BTC/USDT',
    strategy: 'ma_crossover',
    timeframe: '1h',
    days: 90,
    initial_balance: '1000',
  })
  const [error, setError] = useState<string | null>(null)

  const strategies = useQuery<StrategyInfo[]>({
    queryKey: ['strategies'],
    queryFn: api.strategies,
  })

  const run = useMutation<BacktestResult, unknown, void>({
    mutationFn: () =>
      api.backtest({
        symbol: form.symbol.toUpperCase(),
        strategy: form.strategy,
        timeframe: form.timeframe,
        days: Number(form.days),
        initial_balance: form.initial_balance,
      }),
    onMutate: () => setError(null),
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  })

  const result = run.data
  const summary = result?.summary
  const curve = (result?.equity_curve ?? []).map((point) => ({
    time: point.timestamp,
    value: point.value,
  }))

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Backtest</h1>
          <p>
            Roda a estratégia sobre dados históricos reais usando o <strong>mesmo</strong> Risk
            Manager da produção — os limites vigentes valem aqui também.
          </p>
        </div>
      </div>

      <Card>
        <div className="row">
          <div className="field" style={{ maxWidth: 160 }}>
            <label>Par</label>
            <input
              value={form.symbol}
              onChange={(e) => setForm({ ...form, symbol: e.target.value.toUpperCase() })}
            />
          </div>
          <div className="field" style={{ maxWidth: 220 }}>
            <label>Estratégia</label>
            <select
              value={form.strategy}
              onChange={(e) => setForm({ ...form, strategy: e.target.value })}
            >
              {(strategies.data ?? []).map((strategy) => (
                <option key={strategy.name} value={strategy.name}>
                  {strategy.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field" style={{ maxWidth: 120 }}>
            <label>Timeframe</label>
            <select
              value={form.timeframe}
              onChange={(e) => setForm({ ...form, timeframe: e.target.value })}
            >
              {TIMEFRAMES.map((timeframe) => (
                <option key={timeframe} value={timeframe}>
                  {timeframe}
                </option>
              ))}
            </select>
          </div>
          <div className="field" style={{ maxWidth: 110 }}>
            <label>Dias</label>
            <input
              type="number"
              min={1}
              max={365}
              value={form.days}
              onChange={(e) => setForm({ ...form, days: Number(e.target.value) })}
            />
          </div>
          <div className="field" style={{ maxWidth: 140 }}>
            <label>Saldo inicial</label>
            <input
              type="number"
              min={1}
              value={form.initial_balance}
              onChange={(e) => setForm({ ...form, initial_balance: e.target.value })}
            />
          </div>
          <button className="primary" onClick={() => run.mutate()} disabled={run.isPending}>
            {run.isPending ? 'Rodando…' : 'Rodar backtest'}
          </button>
        </div>

        {error && <div className="form-message err" style={{ marginTop: 12 }}>{error}</div>}
      </Card>

      {summary && (
        <>
          <div className="grid grid-4" style={{ marginTop: 14 }}>
            <Stat
              label="Retorno da estratégia"
              value={signedPercent(summary.total_return_pct)}
              hint={`${money(summary.final_value)} finais`}
            />
            <Stat
              label="Comprar e segurar"
              value={signedPercent(summary.buy_and_hold_pct)}
              hint={
                summary.total_return_pct >= summary.buy_and_hold_pct
                  ? 'estratégia superou o mercado'
                  : 'estratégia ficou abaixo do mercado'
              }
            />
            <Stat
              label="Queda máxima"
              value={percent(summary.max_drawdown_pct)}
              hint="do pico até o vale"
            />
            <Stat
              label="Taxa de acerto"
              value={percent(summary.win_rate, 1)}
              hint={`${summary.closed_trades} operações fechadas`}
            />
          </div>

          {summary.total_return_pct < summary.buy_and_hold_pct && (
            <div className="form-message err" style={{ marginTop: 14 }}>
              A estratégia rendeu menos do que simplesmente comprar e segurar no período. Retorno
              positivo abaixo do mercado ainda é destruição de valor.
            </div>
          )}

          <div style={{ marginTop: 14 }}>
            <Card title={`Curva de capital — ${summary.strategy} em ${summary.symbol} (${summary.timeframe})`}>
              {curve.length < 2 ? (
                <Empty>Sem pontos suficientes.</Empty>
              ) : (
                <ResponsiveContainer width="100%" height={280}>
                  <LineChart data={curve} margin={{ top: 6, right: 6, left: 0, bottom: 0 }}>
                    <XAxis
                      dataKey="time"
                      tickFormatter={shortDate}
                      stroke="var(--text-faint)"
                      fontSize={11}
                      tickLine={false}
                      axisLine={false}
                      minTickGap={50}
                    />
                    <YAxis
                      stroke="var(--text-faint)"
                      fontSize={11}
                      tickLine={false}
                      axisLine={false}
                      width={62}
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
                      formatter={(value) => [money(Number(value)), 'capital']}
                    />
                    <Line
                      type="monotone"
                      dataKey="value"
                      stroke="#58a6ff"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              )}
            </Card>
          </div>

          <div className="grid grid-2" style={{ marginTop: 14 }}>
            <Card title="Operações simuladas">
              {result.trades.length === 0 ? (
                <Empty>Nenhuma operação foi aprovada no período.</Empty>
              ) : (
                <div className="table-wrap" style={{ maxHeight: 320, overflowY: 'auto' }}>
                  <table>
                    <thead>
                      <tr>
                        <th>Data</th>
                        <th>Lado</th>
                        <th className="right">Preço</th>
                        <th className="right">Valor</th>
                        <th className="right">PnL</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.trades.map((trade, index) => (
                        <tr key={index} title={trade.reason}>
                          <td className="muted">{shortDate(trade.timestamp)}</td>
                          <td>
                            <span
                              className={`badge ${
                                trade.side === 'buy' ? 'badge-positive' : 'badge-negative'
                              }`}
                            >
                              {trade.side === 'buy' ? 'compra' : 'venda'}
                            </span>
                          </td>
                          <td className="right">{price(trade.price)}</td>
                          <td className="right">{money(trade.notional)}</td>
                          <td
                            className={`right ${
                              trade.realized_pnl === null
                                ? 'muted'
                                : trade.realized_pnl > 0
                                  ? 'positive'
                                  : 'negative'
                            }`}
                          >
                            {trade.realized_pnl === null ? '—' : money(trade.realized_pnl, '')}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>

            <Card title="O que o Risk Manager barrou">
              <div className="stack" style={{ gap: 8 }}>
                <div className="row-tight" style={{ justifyContent: 'space-between' }}>
                  <span className="muted">Sinais gerados</span>
                  <strong className="num">{summary.signals_generated}</strong>
                </div>
                <div className="row-tight" style={{ justifyContent: 'space-between' }}>
                  <span className="muted">Rejeitados pelo risco</span>
                  <strong className="num">{summary.signals_rejected}</strong>
                </div>
                <div className="row-tight" style={{ justifyContent: 'space-between' }}>
                  <span className="muted">Fator de lucro</span>
                  <strong className="num">
                    {Number.isFinite(summary.profit_factor)
                      ? summary.profit_factor.toFixed(2)
                      : '∞'}
                  </strong>
                </div>

                {summary.top_rejection_reasons.length > 0 && (
                  <>
                    <div className="card-title" style={{ marginTop: 8, marginBottom: 4 }}>
                      Principais motivos
                    </div>
                    {summary.top_rejection_reasons.map(([reason, count]) => (
                      <div
                        key={reason}
                        className="row-tight"
                        style={{ justifyContent: 'space-between' }}
                      >
                        <span className="muted" style={{ fontSize: 12 }}>{reason}</span>
                        <span className="num faint">{count}×</span>
                      </div>
                    ))}
                  </>
                )}
              </div>
            </Card>
          </div>

          <div className="faint" style={{ marginTop: 14, fontSize: 12 }}>
            Backtest não prova nada sobre o futuro. Antes de ligar o modo real: semanas em
            simulação, depois testnet, e um teste deliberado do circuit breaker.
          </div>
        </>
      )}
    </>
  )
}
