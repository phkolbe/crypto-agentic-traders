import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState, useId } from 'react'

import { ApiError, api } from '../api/client'
import { dateTime, money, multiplyDecimal, price, quantity, quoteOf } from '../api/format'
import {
  formularioVazio,
  lancamentoDoFormulario,
  type FormularioDeLancamento,
  type LancamentoManual,
} from '../api/mutacoes'
import { Card, useQuoteCurrency } from '../components/Shared'

export default function ManualTrade() {
  // Rotulo ligado ao campo (leitor de tela e clique no rotulo).
  const uid = useId()
  const queryClient = useQueryClient()
  const moeda = useQuoteCurrency()
  // Inicializador preguicoso: a data padrao e a de quando a tela abre, nao a
  // de quando o app carregou (ver `formularioVazio`).
  const [form, setForm] = useState<FormularioDeLancamento>(() => formularioVazio(new Date()))
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  /**
   * Lancamento em revisao. Registrar era um clique, e o lancamento entra no
   * preco medio que define o stop: um zero a menos no preco derrubava o stop do
   * BTC de ~92 mil para ~51 mil sem aviso. Agora o envio mostra o resumo e so
   * grava no "Sim, registrar".
   */
  const [revisando, setRevisando] = useState<LancamentoManual | null>(null)
  /** Recusa 409 da API: preco longe do ultimo preco conhecido do par. */
  const [foraDoMercado, setForaDoMercado] = useState<string | null>(null)

  const create = useMutation({
    mutationFn: api.createManualTrade,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['trades'] })
      queryClient.invalidateQueries({ queryKey: ['portfolio'] })
      setMessage({ ok: true, text: 'Negociação registrada no histórico.' })
      setForm(formularioVazio(new Date()))
      setRevisando(null)
      setForaDoMercado(null)
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        // Mantem a revisao aberta: o operador corrige o preco ou confirma que
        // ele esta certo (operacao antiga, de outra epoca de preco).
        setForaDoMercado(error.message)
        return
      }
      setRevisando(null)
      setMessage({
        ok: false,
        text: error instanceof ApiError ? error.message : String(error),
      })
    },
  })

  const update = (field: keyof FormularioDeLancamento) => (value: string) => {
    // Mexeu no formulario, a revisao deixou de descrever o que vai ser enviado.
    setRevisando(null)
    setForaDoMercado(null)
    setForm({ ...form, [field]: value })
  }

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    setMessage(null)
    setForaDoMercado(null)
    setRevisando(lancamentoDoFormulario(form, moedaDoPar))
  }

  const registrar = (aceitarPrecoForaDoMercado: boolean) => {
    if (revisando) create.mutate({ lancamento: revisando, aceitarPrecoForaDoMercado })
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
                <label htmlFor={`${uid}-1`}>Data e hora</label>
                <input id={`${uid}-1`}
                  type="datetime-local"
                  value={form.executed_at}
                  onChange={(e) => update('executed_at')(e.target.value)}
                  required
                />
              </div>

              <div className="field">
                <label htmlFor={`${uid}-2`}>Exchange</label>
                <select id={`${uid}-2`} value={form.exchange} onChange={(e) => update('exchange')(e.target.value)}>
                  <option value="binance">Binance</option>
                  {/* "outra" continua disponivel: um lancamento manual pode vir
                      de qualquer lugar, mesmo o sistema operando so na Binance. */}
                  <option value="outra">Outra</option>
                </select>
              </div>

              <div className="field">
                <label htmlFor={`${uid}-3`}>Par</label>
                <input id={`${uid}-3`}
                  value={form.symbol}
                  onChange={(e) => update('symbol')(e.target.value.toUpperCase())}
                  placeholder={moeda ? `BTC/${moeda}` : ''}
                  pattern="[A-Z0-9]{2,12}/[A-Z0-9]{2,12}"
                  required
                />
                <span className="hint">Formato BASE/COTAÇÃO</span>
              </div>

              <div className="field">
                <label htmlFor={`${uid}-4`}>Lado</label>
                <select id={`${uid}-4`} value={form.side} onChange={(e) => update('side')(e.target.value)}>
                  <option value="buy">Compra</option>
                  <option value="sell">Venda</option>
                </select>
              </div>

              <div className="field">
                <label htmlFor={`${uid}-5`}>Quantidade</label>
                <input id={`${uid}-5`}
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
                <label htmlFor={`${uid}-6`}>Preço unitário</label>
                <input id={`${uid}-6`}
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
                <label htmlFor={`${uid}-7`}>Taxa paga</label>
                <input id={`${uid}-7`}
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
                <label htmlFor={`${uid}-8`}>Moeda da taxa</label>
                <input id={`${uid}-8`}
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
              <label htmlFor={`${uid}-9`}>Observações</label>
              <textarea id={`${uid}-9`}
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
              <div className={`form-message ${message.ok ? 'ok' : 'err'}`} role={message.ok ? 'status' : 'alert'}>
                {message.text}
              </div>
            )}

            {revisando && (
              <div className="alert-banner">
                <div>
                  <strong>
                    Registrar a {revisando.side === 'buy' ? 'compra' : 'venda'} de{' '}
                    {quantity(revisando.quantity)} {revisando.symbol} a {price(revisando.price)}{' '}
                    {quoteOf(revisando.symbol)}, em {dateTime(revisando.executed_at)}?
                  </strong>{' '}
                  <span className="muted">
                    O lançamento entra no preço médio da posição e move o stop e o alvo dela.
                    Confira o preço contra o extrato da exchange.
                  </span>
                  {foraDoMercado && (
                    <div className="form-message err" style={{ marginTop: 8 }} role="alert">
                      {foraDoMercado}
                    </div>
                  )}
                </div>
                <div className="row-tight">
                  {!foraDoMercado ? (
                    <button
                      type="button"
                      className="primary"
                      onClick={() => registrar(false)}
                      disabled={create.isPending}
                    >
                      {create.isPending ? 'Registrando…' : 'Sim, registrar'}
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="danger"
                      onClick={() => registrar(true)}
                      disabled={create.isPending}
                    >
                      {create.isPending ? 'Registrando…' : 'O preço está certo, registrar'}
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      setRevisando(null)
                      setForaDoMercado(null)
                    }}
                  >
                    Voltar e corrigir
                  </button>
                </div>
              </div>
            )}

            {!revisando && (
              <div className="row-tight">
                <button type="submit" className="primary">
                  Revisar e registrar
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setForm(formularioVazio(new Date()))
                    setMessage(null)
                  }}
                >
                  Limpar
                </button>
              </div>
            )}
          </form>
        </Card>
      </div>
    </>
  )
}
