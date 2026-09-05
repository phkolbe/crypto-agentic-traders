import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { ApiError, api } from '../api/client'
import { dateTime } from '../api/format'
import type { AuditEntry, RiskConfig } from '../api/types'
import {
  AgentsOffline,
  Card,
  CircuitBreakerBanner,
  Empty,
  Loading,
} from '../components/Shared'

/**
 * Campos editáveis, com a explicação do que cada limite protege.
 *
 * O texto fica junto do campo de propósito: um número sem contexto convida a
 * ser afrouxado sem que a consequência apareça.
 */
const FIELDS: {
  key: keyof RiskConfig
  label: string
  help: string
  kind: 'money' | 'percent' | 'int'
}[] = [
  {
    key: 'max_order_notional',
    label: 'Valor máximo por ordem',
    help: 'Teto absoluto de uma única operação. Limita o estrago de um erro de cálculo.',
    kind: 'money',
  },
  {
    key: 'max_order_pct_portfolio',
    label: 'Máximo por ordem (% do portfólio)',
    help: 'Vale o menor entre este e o teto absoluto. Acompanha o crescimento da carteira.',
    kind: 'percent',
  },
  {
    key: 'min_order_notional',
    label: 'Valor mínimo por ordem',
    help: 'Abaixo disso a taxa come o resultado — e a exchange costuma recusar.',
    kind: 'money',
  },
  {
    key: 'max_asset_exposure_pct',
    label: 'Exposição máxima por ativo',
    help: 'Evita concentração acidental depois de vários sinais na mesma direção.',
    kind: 'percent',
  },
  {
    key: 'max_open_positions',
    label: 'Máximo de posições abertas',
    help: 'Aumentar uma posição já existente não conta como nova.',
    kind: 'int',
  },
  {
    key: 'stop_loss_pct',
    label: 'Stop-loss',
    help: 'Anexado pelo Risk Manager a toda posição aberta por um agente.',
    kind: 'percent',
  },
  {
    key: 'take_profit_pct',
    label: 'Take-profit',
    help: 'Precisa ser maior que o stop-loss, senão a estratégia tem esperança negativa.',
    kind: 'percent',
  },
  {
    key: 'daily_loss_limit_pct',
    label: 'Circuit breaker diário',
    help: 'Queda no dia que pausa todos os agentes de decisão. Rearme é manual.',
    kind: 'percent',
  },
  {
    key: 'weekly_loss_limit_pct',
    label: 'Circuit breaker semanal',
    help: 'Precisa ser maior ou igual ao limite diário.',
    kind: 'percent',
  },
  {
    key: 'min_signal_confidence',
    label: 'Confiança mínima do sinal',
    help: 'Filtra ruído da estratégia antes de virar ordem.',
    kind: 'percent',
  },
  {
    key: 'cooldown_seconds',
    label: 'Cooldown por par (segundos)',
    help: 'Intervalo mínimo entre ordens do mesmo par. Não bloqueia fechamento de posição.',
    kind: 'int',
  },
]

export default function RiskSettings() {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const [confirming, setConfirming] = useState(false)

  const config = useQuery<RiskConfig>({
    queryKey: ['riskConfig'],
    queryFn: api.riskConfig,
    retry: false,
  })
  const audit = useQuery<AuditEntry[]>({ queryKey: ['audit'], queryFn: () => api.audit(20) })

  useEffect(() => {
    if (!config.data) return
    const initial: Record<string, string> = {}
    for (const field of FIELDS) initial[field.key] = String(config.data[field.key])
    initial.asset_whitelist = config.data.asset_whitelist.join(', ')
    initial.symbol_whitelist = config.data.symbol_whitelist.join(', ')
    setDraft(initial)
  }, [config.data])

  const save = useMutation({
    mutationFn: api.updateRiskConfig,
    onSuccess: () => {
      setMessage({ ok: true, text: 'Limites atualizados e registrados no log de auditoria.' })
      setConfirming(false)
      queryClient.invalidateQueries({ queryKey: ['riskConfig'] })
      queryClient.invalidateQueries({ queryKey: ['audit'] })
    },
    onError: (error) =>
      setMessage({ ok: false, text: error instanceof ApiError ? error.message : String(error) }),
  })

  const reset = useMutation({
    mutationFn: api.resetCircuitBreaker,
    onSuccess: () => {
      setMessage({ ok: true, text: 'Circuit breaker rearmado e agentes retomados.' })
      queryClient.invalidateQueries({ queryKey: ['riskConfig'] })
      queryClient.invalidateQueries({ queryKey: ['health'] })
    },
    onError: (error) =>
      setMessage({ ok: false, text: error instanceof ApiError ? error.message : String(error) }),
  })

  if (config.isError) {
    const error = config.error
    if (error instanceof ApiError && error.agentsAreDown) return <AgentsOffline />
  }
  if (config.isLoading) return <Loading />

  const changed = FIELDS.some(
    (field) => draft[field.key] !== undefined && draft[field.key] !== String(config.data?.[field.key]),
  ) ||
    draft.asset_whitelist !== config.data?.asset_whitelist.join(', ') ||
    draft.symbol_whitelist !== config.data?.symbol_whitelist.join(', ')

  const submit = () => {
    const payload: Record<string, unknown> = {}
    for (const field of FIELDS) {
      const value = draft[field.key]
      if (value === undefined || value === String(config.data?.[field.key])) continue
      payload[field.key] = field.kind === 'money' ? value : Number(value)
    }
    const assets = splitList(draft.asset_whitelist)
    const symbols = splitList(draft.symbol_whitelist)
    if (assets.join(',') !== config.data?.asset_whitelist.join(',')) payload.asset_whitelist = assets
    if (symbols.join(',') !== config.data?.symbol_whitelist.join(','))
      payload.symbol_whitelist = symbols

    setMessage(null)
    save.mutate(payload)
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Limites de risco</h1>
          <p>
            Toda ordem passa por estas regras antes de existir. Alterações são aplicadas
            imediatamente e ficam registradas no log de auditoria.
          </p>
        </div>
      </div>

      <CircuitBreakerBanner
        active={config.data?.circuit_breaker_active ?? false}
        reason={config.data?.circuit_breaker_reason}
        onReset={() => reset.mutate()}
        resetting={reset.isPending}
      />

      {message && (
        <div className={`form-message ${message.ok ? 'ok' : 'err'}`} style={{ marginBottom: 14 }}>
          {message.text}
        </div>
      )}

      <Card>
        <div className="form-grid">
          {FIELDS.map((field) => (
            <div className="field" key={field.key}>
              <label>
                {field.label}
                {field.kind === 'percent' && <span className="faint"> (0–1)</span>}
              </label>
              <input
                type="number"
                step={field.kind === 'int' ? '1' : 'any'}
                min="0"
                value={draft[field.key] ?? ''}
                onChange={(e) => setDraft({ ...draft, [field.key]: e.target.value })}
              />
              <span className="hint">{field.help}</span>
            </div>
          ))}
        </div>

        <div className="form-grid" style={{ marginTop: 14 }}>
          <div className="field">
            <label>Ativos permitidos</label>
            <input
              value={draft.asset_whitelist ?? ''}
              onChange={(e) => setDraft({ ...draft, asset_whitelist: e.target.value.toUpperCase() })}
            />
            <span className="hint">Separados por vírgula. Evita tokens ilíquidos ou desconhecidos.</span>
          </div>
          <div className="field">
            <label>Pares permitidos</label>
            <input
              value={draft.symbol_whitelist ?? ''}
              onChange={(e) => setDraft({ ...draft, symbol_whitelist: e.target.value.toUpperCase() })}
            />
            <span className="hint">Nenhuma ordem é enviada para um par fora desta lista.</span>
          </div>
        </div>

        <div className="row-tight" style={{ marginTop: 18 }}>
          {!confirming ? (
            <button className="primary" disabled={!changed} onClick={() => setConfirming(true)}>
              Salvar alterações
            </button>
          ) : (
            <>
              <span className="muted" style={{ fontSize: 13 }}>
                Estes limites controlam quanto dinheiro real o sistema pode comprometer. Confirmar?
              </span>
              <button className="danger" onClick={submit} disabled={save.isPending}>
                {save.isPending ? 'Aplicando…' : 'Sim, aplicar'}
              </button>
              <button onClick={() => setConfirming(false)}>Cancelar</button>
            </>
          )}
          {changed && !confirming && <span className="muted" style={{ fontSize: 12 }}>alterações não salvas</span>}
        </div>
      </Card>

      <div style={{ marginTop: 14 }}>
        <Card title="Log de auditoria">
          {(audit.data ?? []).length === 0 ? (
            <Empty>Nenhuma ação administrativa registrada.</Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Quando</th>
                    <th>Quem</th>
                    <th>Ação</th>
                    <th>Alvo</th>
                    <th>Detalhe</th>
                  </tr>
                </thead>
                <tbody>
                  {(audit.data ?? []).map((entry) => (
                    <tr key={entry.id}>
                      <td className="muted">{dateTime(entry.timestamp)}</td>
                      <td>{entry.actor}</td>
                      <td className="mono">{entry.action}</td>
                      <td className="muted">{entry.target ?? '—'}</td>
                      <td className="muted" style={{ maxWidth: 380 }}>{entry.detail ?? '—'}</td>
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

function splitList(value: string | undefined): string[] {
  return (value ?? '')
    .split(',')
    .map((item) => item.trim().toUpperCase())
    .filter(Boolean)
}
