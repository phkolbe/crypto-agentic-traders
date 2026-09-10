import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, api } from '../api/client'
import { money, percent, price, quoteOf, shortDate, signedPercent } from '../api/format'
import type { BacktestResult, StrategyInfo } from '../api/types'
import { TimeSeriesChart } from '../components/Charts'
import { Card, Empty, Stat, useQuoteCurrency } from '../components/Shared'

const TIMEFRAMES = ['15m', '30m', '1h', '4h', '1d']

export default function Backtest() {
  const moeda = useQuoteCurrency()
  const [form, setForm] = useState({
    // Sem par fixo: `BTC/USDT` era o padrao e deixou de existir na configuracao
    // quando a cotacao virou USDC — o backtest de fabrica rodava num par que o
    // sistema nao negocia. Vazio + placeholder derivado da moeda vigente.
    symbol: '',
    strategy: 'ma_crossover',
    timeframe: '1h',
    days: 90,
    initial_balance: '1000',
  })
  const [error, setError] = useState<string | null>(null)
  const parPadrao = moeda ? `BTC/${moeda}` : ''
  const par = form.symbol.trim() || parPadrao

  const strategies = useQuery<StrategyInfo[]>({
    queryKey: ['strategies'],
    queryFn: api.strategies,
  })

  const run = useMutation<BacktestResult, unknown, void>({
    mutationFn: () =>
      api.backtest({
        symbol: par.toUpperCase(),
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
  // O resultado e do par que FOI rodado, que pode nao ser o da configuracao
  // atual — quem rodou BTC/BRL nao pode ver o resultado rotulado em USDC.
  const moedaDoResultado = quoteOf(summary?.symbol) || moeda
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
              placeholder={parPadrao}
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
            <label>
              Saldo inicial
              {moeda && <span className="faint"> ({moeda})</span>}
            </label>
            <input
              type="number"
              min={1}
              value={form.initial_balance}
              onChange={(e) => setForm({ ...form, initial_balance: e.target.value })}
            />
          </div>
          <button
            className="primary"
            onClick={() => run.mutate()}
            disabled={run.isPending || !par}
          >
            {run.isPending ? 'Rodando…' : 'Rodar backtest'}
          </button>
        </div>

        {error && <div className="form-message err" style={{ marginTop: 12 }}>{error}</div>}
      </Card>

      {!summary ? (
        <BacktestAindaNaoRodado
          pendente={run.isPending}
          estrategias={strategies.data ?? []}
          selecionada={form.strategy}
        />
      ) : (
        <>
          <div className="grid grid-4" style={{ marginTop: 14 }}>
            <Stat
              label="Retorno da estratégia"
              value={signedPercent(summary.total_return_pct)}
              hint={`${money(summary.final_value, moedaDoResultado)} finais`}
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
                <TimeSeriesChart
                  data={curve}
                  height={280}
                  formatValue={(value) => money(value, moedaDoResultado)}
                  valueLabel="capital"
                  minTickGap={50}
                />
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
                        {/* Um backtest e de um unico par, entao aqui a unidade
                            cabe no cabecalho — ao contrario do historico real,
                            que mistura pares e precisa da unidade por linha. */}
                        <th className="right">
                          Preço{moedaDoResultado && ` (${moedaDoResultado})`}
                        </th>
                        <th className="right">
                          Valor{moedaDoResultado && ` (${moedaDoResultado})`}
                        </th>
                        <th className="right">
                          PnL{moedaDoResultado && ` (${moedaDoResultado})`}
                        </th>
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
                          <td className="right">{money(trade.notional, '')}</td>
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

          <div className="disclaimer">{AVISO}</div>
        </>
      )}
    </>
  )
}

const AVISO =
  'Backtest não prova nada sobre o futuro. Antes de ligar o modo real: semanas em simulação, ' +
  'depois testnet, e um teste deliberado do circuit breaker.'

/**
 * O que a tela mede, dito ANTES de existir resultado.
 *
 * Cada item é exatamente um dos cartões que aparecem depois, na mesma ordem e no
 * mesmo grid — o vazio prefigura o cheio em vez de ser um buraco.
 */
const PREVIA: { rotulo: string; descricao: string }[] = [
  {
    rotulo: 'Retorno da estratégia',
    descricao: 'quanto o capital inicial virou no fim do período, já com taxa e slippage simulados',
  },
  {
    rotulo: 'Comprar e segurar',
    descricao: 'o mesmo período sem estratégia nenhuma — o piso que ela precisa superar',
  },
  {
    rotulo: 'Queda máxima',
    descricao: 'a maior distância do pico ao vale, que é o que se sente na hora',
  },
  {
    rotulo: 'Taxa de acerto',
    descricao: 'fração das operações fechadas no lucro, e quantas foram',
  },
]

/**
 * Estado da tela antes do primeiro backtest — o estado em que ela abre sempre.
 *
 * Tudo aqui era `{summary && …}`: da barra do formulário para baixo não havia
 * um único elemento, e sobrava a maior parte do viewport em branco, sem uma
 * linha dizendo o que vai aparecer. Nenhuma das outras telas faz isso; todas
 * têm `<Empty>`.
 */
function BacktestAindaNaoRodado({
  pendente,
  estrategias,
  selecionada,
}: {
  pendente: boolean
  estrategias: StrategyInfo[]
  selecionada: string
}) {
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="empty-title">
        {pendente ? 'Rodando o backtest…' : 'Nenhum backtest rodado nesta sessão'}
      </div>
      <p className="empty-text">
        {pendente
          ? 'Buscando os candles na exchange e passando cada um pela mesma estratégia, mesmo ' +
            'RiskEngine e mesmo PaperBroker que a produção usa.'
          : 'Escolha par, estratégia, timeframe e período acima e clique em Rodar backtest. ' +
            'Nada é gravado no histórico: o resultado vive nesta tela até você rodar outro.'}
      </p>

      <div className="grid grid-4" style={{ marginTop: 16 }}>
        {PREVIA.map((item) => (
          <div key={item.rotulo} className="placeholder">
            <div className="card-title" style={{ marginBottom: 6 }}>{item.rotulo}</div>
            <div className="placeholder-value">—</div>
            <div className="hint">{item.descricao}</div>
          </div>
        ))}
      </div>

      <div className="hint" style={{ marginTop: 16 }}>
        Também aparecem: a curva de capital, cada operação simulada com o motivo, e o painel do
        Risk Manager com quantos sinais ele barrou e por quê.
      </div>

      {/* As estrategias vem da API e ja estao carregadas para preencher o
          seletor acima — mostra-las aqui e de graca, e e a informacao que falta
          para escolher qual rodar. */}
      {estrategias.length > 0 && (
        <div className="watchlist">
          <div className="card-title" style={{ marginBottom: 8 }}>
            Estratégias disponíveis
          </div>
          <div className="stack" style={{ gap: 9 }}>
            {estrategias.map((estrategia) => (
              <div key={estrategia.name} className="strategy-row">
                <div className="row-tight" style={{ gap: 8 }}>
                  <strong>{estrategia.name}</strong>
                  {estrategia.name === selecionada && (
                    <span className="badge badge-accent">selecionada</span>
                  )}
                  {estrategia.active && (
                    <span className="badge badge-positive">ativa em produção</span>
                  )}
                </div>
                <div className="hint">{estrategia.description}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="disclaimer">{AVISO}</div>
    </div>
  )
}
