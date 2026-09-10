import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { ApiError, api } from '../api/client'
import { money, multiplyDecimal, quoteOf } from '../api/format'
import { Card, useQuoteCurrency } from '../components/Shared'

/** `datetime-local` espera o horario LOCAL sem timezone; ISO com Z apareceria deslocado. */
function nowForInput(): string {
  const now = new Date()
  now.setMinutes(now.getMinutes() - now.getTimezoneOffset())
  return now.toISOString().slice(0, 16)
}

/**
 * Formulario vazio.
 *
 * `fee_currency` NAO tem valor de fabrica: era `'USDT'`, e depois da migracao
 * para USDC todo lancamento manual salvo sem tocar no campo gravava a moeda
 * errada no historico. O padrao passa a ser a moeda de cotacao vigente,
 * resolvida em tempo de renderizacao.
 */
const EMPTY = {
  executed_at: nowForInput(),
  exchange: 'binance',
  symbol: '',
  side: 'buy',
  quantity: '',
  price: '',
  fee: '',
  fee_currency: '',
  notes: '',
}

export default function ManualTrade() {
  const queryClient = useQueryClient()
  const moeda = useQuoteCurrency()
  const [form, setForm] = useState(EMPTY)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)

  const create = useMutation({
    mutationFn: api.createManualTrade,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['trades'] })
      queryClient.invalidateQueries({ queryKey: ['portfolio'] })
      setMessage({ ok: true, text: 'Negociação registrada no histórico.' })
      setForm({ ...EMPTY, executed_at: nowForInput() })
    },
    onError: (error) =>
      setMessage({
        ok: false,
        text: error instanceof ApiError ? error.message : String(error),
      }),
  })

  const update = (field: string) => (value: string) => setForm({ ...form, [field]: value })

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    setMessage(null)
    create.mutate({
      // O input entrega horario local; convertemos para ISO com timezone para
      // que o backend nao precise adivinhar o fuso.
      executed_at: new Date(form.executed_at).toISOString(),
      exchange: form.exchange,
      symbol: form.symbol.toUpperCase(),
      side: form.side,
      quantity: form.quantity,
      price: form.price,
      fee: form.fee || '0',
      fee_currency: form.fee_currency || moedaDoPar || null,
      notes: form.notes || null,
    })
  }

  /**
   * Total da operacao, calculado sem float (D7).
   *
   * Era `Number(quantity) * Number(price)`. Com 0,07 x 1,1 o float devolve
   * 0.07700000000000001, e este total e conferido pelo operador contra o que
   * ele viu na exchange: um residuo aqui e uma divergencia que ninguem
   * consegue explicar. `multiplyDecimal` multiplica os digitos em `BigInt`, e
   * devolve `null` quando o campo ainda nao e um decimal valido.
   */
  const total =
    form.quantity && form.price ? multiplyDecimal(form.quantity, form.price) : null
  // Lancamento manual pode ser de um par que o sistema nao negocia mais: a
  // unidade do total vem do par digitado, com a moeda configurada so como
  // ultimo recurso.
  const moedaDoPar = quoteOf(form.symbol) || moeda

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Lançar negociação manual</h1>
          <p>
            Para operações feitas fora do sistema — pelo app da exchange, por exemplo. Elas entram no
            mesmo histórico e contam no cálculo de preço médio.
          </p>
        </div>
      </div>

      <div style={{ maxWidth: 720 }}>
        <Card>
          <form onSubmit={submit} className="stack">
            <div className="form-grid">
              <div className="field">
                <label>Data e hora</label>
                <input
                  type="datetime-local"
                  value={form.executed_at}
                  onChange={(e) => update('executed_at')(e.target.value)}
                  required
                />
              </div>

              <div className="field">
                <label>Exchange</label>
                <select value={form.exchange} onChange={(e) => update('exchange')(e.target.value)}>
                  <option value="binance">Binance</option>
                  {/* "outra" continua disponivel: um lancamento manual pode vir
                      de qualquer lugar, mesmo o sistema operando so na Binance. */}
                  <option value="outra">Outra</option>
                </select>
              </div>

              <div className="field">
                <label>Par</label>
                <input
                  value={form.symbol}
                  onChange={(e) => update('symbol')(e.target.value.toUpperCase())}
                  placeholder={moeda ? `BTC/${moeda}` : ''}
                  pattern="[A-Z0-9]{2,12}/[A-Z0-9]{2,12}"
                  required
                />
                <span className="hint">Formato BASE/COTAÇÃO</span>
              </div>

              <div className="field">
                <label>Lado</label>
                <select value={form.side} onChange={(e) => update('side')(e.target.value)}>
                  <option value="buy">Compra</option>
                  <option value="sell">Venda</option>
                </select>
              </div>

              <div className="field">
                <label>Quantidade</label>
                <input
                  type="number"
                  step="any"
                  min="0"
                  value={form.quantity}
                  onChange={(e) => update('quantity')(e.target.value)}
                  placeholder="0.001"
                  required
                />
              </div>

              <div className="field">
                <label>Preço unitário</label>
                <input
                  type="number"
                  step="any"
                  min="0"
                  value={form.price}
                  onChange={(e) => update('price')(e.target.value)}
                  placeholder="50000.00"
                  required
                />
              </div>

              <div className="field">
                <label>Taxa paga</label>
                <input
                  type="number"
                  step="any"
                  min="0"
                  value={form.fee}
                  onChange={(e) => update('fee')(e.target.value)}
                  placeholder="0.00"
                />
                <span className="hint">Entra no cálculo do preço médio</span>
              </div>

              <div className="field">
                <label>Moeda da taxa</label>
                <input
                  value={form.fee_currency}
                  onChange={(e) => update('fee_currency')(e.target.value.toUpperCase())}
                  placeholder={moedaDoPar}
                />
                <span className="hint">
                  Vazio usa a cotação do par{moedaDoPar ? ` (${moedaDoPar})` : ''}.
                </span>
              </div>
            </div>

            <div className="field">
              <label>Observações</label>
              <textarea
                rows={2}
                value={form.notes}
                onChange={(e) => update('notes')(e.target.value)}
                placeholder="opcional"
                maxLength={500}
              />
            </div>

            {total !== null && (
              <div className="muted" style={{ fontSize: 13 }}>
                Valor total da operação:{' '}
                <strong className="num">{money(total, moedaDoPar)}</strong>
              </div>
            )}

            {message && (
              <div className={`form-message ${message.ok ? 'ok' : 'err'}`}>{message.text}</div>
            )}

            <div className="row-tight">
              <button type="submit" className="primary" disabled={create.isPending}>
                {create.isPending ? 'Registrando…' : 'Registrar negociação'}
              </button>
              <button
                type="button"
                onClick={() => {
                  setForm({ ...EMPTY, executed_at: nowForInput() })
                  setMessage(null)
                }}
              >
                Limpar
              </button>
            </div>
          </form>
        </Card>
      </div>
    </>
  )
}
