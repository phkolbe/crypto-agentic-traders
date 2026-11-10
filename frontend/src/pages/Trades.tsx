import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, useState, useId } from 'react'

import { api, ApiError } from '../api/client'
import { MODE_LABEL, dateTime, money, price, quantity, quoteOf, taxa } from '../api/format'
import type { TradePage } from '../api/types'
import {
  Card,
  Empty,
  ErroDeCarga,
  Loading,
  OriginBadge,
  SideBadge,
  useQuoteCurrency,
} from '../components/Shared'

const PAGE_SIZE = 25

export default function Trades() {
  // Rotulo ligado ao campo (leitor de tela e clique no rotulo).
  const uid = useId()
  const queryClient = useQueryClient()
  const moeda = useQuoteCurrency()
  const [offset, setOffset] = useState(0)
  const [origin, setOrigin] = useState('')
  const [symbol, setSymbol] = useState('')
  const [side, setSide] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  /**
   * Lancamento cuja exclusao esta sendo confirmada. Era um clique: um erro de
   * mira na linha vizinha apagava uma compra, e o preco medio (base do stop) e
   * o PnL eram reapurados sem ela.
   */
  const [excluindo, setExcluindo] = useState<string | null>(null)

  const filters = { limit: PAGE_SIZE, offset, origin, symbol, side }
  const trades = useQuery<TradePage>({
    queryKey: ['trades', filters],
    queryFn: () => api.trades(filters),
  })

  const remove = useMutation({
    mutationFn: api.deleteTrade,
    onSuccess: () => {
      setError(null)
      setExcluindo(null)
      queryClient.invalidateQueries({ queryKey: ['trades'] })
      // O lancamento saiu do preco medio: carteira e analise mudam junto.
      queryClient.invalidateQueries({ queryKey: ['portfolio'] })
      queryClient.invalidateQueries({ queryKey: ['portfolioAnalise'] })
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  })

  const resetFilter = (setter: (value: string) => void) => (value: string) => {
    setter(value)
    setOffset(0) // trocar filtro sem voltar para a primeira pagina confundiria
  }

  const total = trades.data?.total ?? 0
  const items = trades.data?.items ?? []

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Negociações</h1>
          <p>Histórico consolidado: operações dos agentes e lançamentos manuais.</p>
        </div>
        <a href={api.exportUrl()} download>
          <button>Exportar CSV</button>
        </a>
      </div>

      <Card>
        <div className="row" style={{ marginBottom: 14 }}>
          <div className="field" style={{ maxWidth: 160 }}>
            <label htmlFor={`${uid}-1`}>Origem</label>
            <select id={`${uid}-1`} value={origin} onChange={(e) => resetFilter(setOrigin)(e.target.value)}>
              <option value="">Todas</option>
              <option value="agent">Agentes</option>
              <option value="manual">Manuais</option>
            </select>
          </div>
          <div className="field" style={{ maxWidth: 160 }}>
            <label htmlFor={`${uid}-2`}>Lado</label>
            <select id={`${uid}-2`} value={side} onChange={(e) => resetFilter(setSide)(e.target.value)}>
              <option value="">Todos</option>
              <option value="buy">Compra</option>
              <option value="sell">Venda</option>
            </select>
          </div>
          <div className="field" style={{ maxWidth: 180 }}>
            <label htmlFor={`${uid}-3`}>Par</label>
            <input id={`${uid}-3`}
              value={symbol}
              onChange={(e) => resetFilter(setSymbol)(e.target.value.toUpperCase())}
              placeholder={moeda ? `BTC/${moeda}` : ''}
            />
          </div>
          {(origin || side || symbol) && (
            <button
              onClick={() => {
                setOrigin('')
                setSide('')
                setSymbol('')
                setOffset(0)
              }}
            >
              Limpar
            </button>
          )}
        </div>

        {error && <div className="form-message err" style={{ marginBottom: 12 }}>{error}</div>}

        {/* Falha ao carregar era "Nenhuma negociação encontrada": o operador
            não distinguia histórico vazio de API fora do ar. */}
        {trades.isError && <ErroDeCarga error={trades.error} oQue="as negociações" />}

        {trades.isLoading ? (
          <Loading />
        ) : trades.isError && !trades.data ? null : items.length === 0 ? (
          <Empty>
            Nenhuma negociação encontrada.
            {total === 0 && !origin && !symbol && !side && (
              <>
                <br />
                Enquanto o sistema roda em simulação, as operações dos agentes aparecem aqui.
              </>
            )}
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Data</th>
                  <th>Par</th>
                  <th>Lado</th>
                  <th className="right">Quantidade</th>
                  <th className="right">Preço</th>
                  <th className="right">Total</th>
                  <th className="right">Taxa</th>
                  <th>Origem</th>
                  <th>Estratégia</th>
                  <th>Modo</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((trade) => (
                  // Fragment com key: a linha de detalhe e irma da principal, e
                  // sem a key o React perderia a identidade ao reordenar.
                  <Fragment key={trade.id}>
                    <tr
                      className="expandable"
                      onClick={() => setExpanded(expanded === trade.id ? null : trade.id)}
                    >
                      <td className="muted">{dateTime(trade.executed_at)}</td>
                      <td><strong>{trade.symbol}</strong></td>
                      <td><SideBadge side={trade.side} /></td>
                      <td className="right">{quantity(trade.quantity)}</td>
                      {/* A unidade sai do par da propria linha: o historico
                          mistura operacoes em BRL (antes da migracao) com
                          operacoes em USDC, e um rotulo unico de coluna
                          mentiria sobre metade das linhas.
                          As DUAS colunas de dinheiro carregam a unidade. Enquanto
                          so o total carregava, o preco unitario (385.275,67)
                          ficava sem unidade em lugar nenhum da tela — e num
                          historico misturado ele e justamente o numero que se
                          confere contra o extrato da exchange. */}
                      <td className="right">
                        {price(trade.price)}{' '}
                        <span className="unit">{quoteOf(trade.symbol)}</span>
                      </td>
                      <td className="right">{money(trade.notional, quoteOf(trade.symbol))}</td>
                      {/* `taxa`: cobrada no ativo base (compra em live), 0,0000012 BTC
                          aparecia como "0,00 BTC". */}
                      <td className="right muted">{taxa(trade.fee, trade.fee_currency, trade.symbol)}</td>
                      <td><OriginBadge origin={trade.origin} /></td>
                      <td className="muted">{trade.strategy ?? '—'}</td>
                      <td className="muted" style={{ fontSize: 12 }}>
                        {MODE_LABEL[trade.mode] ?? trade.mode}
                      </td>
                      <td className="right">
                        {trade.origin === 'manual' && (
                          <button
                            className="small danger"
                            onClick={(event) => {
                              event.stopPropagation()
                              setExcluindo(trade.id)
                            }}
                            disabled={remove.isPending}
                            title="Remover lançamento manual"
                          >
                            excluir
                          </button>
                        )}
                      </td>
                    </tr>
                    {excluindo === trade.id && (
                      <tr>
                        <td colSpan={11}>
                          <div className="row-tight">
                            <span>
                              <strong>
                                Excluir a {trade.side === 'buy' ? 'compra' : 'venda'} de{' '}
                                {quantity(trade.quantity)} {trade.symbol} a {price(trade.price)}{' '}
                                {quoteOf(trade.symbol)}?
                              </strong>{' '}
                              <span className="muted">
                                O lançamento sai do preço médio, e o stop e o alvo da posição são
                                recalculados sem ele. A exclusão fica no log de auditoria.
                              </span>
                            </span>
                            <button
                              className="small danger"
                              onClick={() => remove.mutate(trade.id)}
                              disabled={remove.isPending}
                            >
                              {remove.isPending ? 'Excluindo…' : 'Sim, excluir'}
                            </button>
                            <button className="small" onClick={() => setExcluindo(null)}>
                              Cancelar
                            </button>
                          </div>
                        </td>
                      </tr>
                    )}
                    {expanded === trade.id && (
                      <tr>
                        <td colSpan={11} style={{ background: 'var(--bg)' }}>
                          <div className="detail-box">
                            {`id            ${trade.id}\n`}
                            {`exchange      ${trade.exchange}\n`}
                            {`sinal         ${trade.signal_id ?? '— (lançamento manual)'}\n`}
                            {`pnl realizado ${trade.realized_pnl ?? '—'}\n`}
                            {trade.notes ? `observações   ${trade.notes}` : ''}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="pagination">
          <span>
            {total === 0
              ? 'nenhum registro'
              : `${offset + 1}–${Math.min(offset + PAGE_SIZE, total)} de ${total}`}
          </span>
          <div className="row-tight">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
              Anterior
            </button>
            <button
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Próxima
            </button>
          </div>
        </div>
      </Card>
    </>
  )
}
