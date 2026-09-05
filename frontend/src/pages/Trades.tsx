import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, useState } from 'react'

import { api, ApiError } from '../api/client'
import { MODE_LABEL, dateTime, money, price, quantity } from '../api/format'
import type { TradePage } from '../api/types'
import { Card, Empty, Loading, OriginBadge, SideBadge } from '../components/Shared'

const PAGE_SIZE = 25

export default function Trades() {
  const queryClient = useQueryClient()
  const [offset, setOffset] = useState(0)
  const [origin, setOrigin] = useState('')
  const [symbol, setSymbol] = useState('')
  const [side, setSide] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const filters = { limit: PAGE_SIZE, offset, origin, symbol, side }
  const trades = useQuery<TradePage>({
    queryKey: ['trades', filters],
    queryFn: () => api.trades(filters),
  })

  const remove = useMutation({
    mutationFn: api.deleteTrade,
    onSuccess: () => {
      setError(null)
      queryClient.invalidateQueries({ queryKey: ['trades'] })
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
            <label>Origem</label>
            <select value={origin} onChange={(e) => resetFilter(setOrigin)(e.target.value)}>
              <option value="">Todas</option>
              <option value="agent">Agentes</option>
              <option value="manual">Manuais</option>
            </select>
          </div>
          <div className="field" style={{ maxWidth: 160 }}>
            <label>Lado</label>
            <select value={side} onChange={(e) => resetFilter(setSide)(e.target.value)}>
              <option value="">Todos</option>
              <option value="buy">Compra</option>
              <option value="sell">Venda</option>
            </select>
          </div>
          <div className="field" style={{ maxWidth: 180 }}>
            <label>Par</label>
            <input
              value={symbol}
              onChange={(e) => resetFilter(setSymbol)(e.target.value.toUpperCase())}
              placeholder="BTC/USDT"
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

        {trades.isLoading ? (
          <Loading />
        ) : items.length === 0 ? (
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
                      <td className="right">{price(trade.price)}</td>
                      <td className="right">{money(trade.notional)}</td>
                      <td className="right muted">
                        {Number(trade.fee) > 0 ? money(trade.fee, trade.fee_currency ?? '') : '—'}
                      </td>
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
                              remove.mutate(trade.id)
                            }}
                            disabled={remove.isPending}
                            title="Remover lançamento manual"
                          >
                            excluir
                          </button>
                        )}
                      </td>
                    </tr>
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
