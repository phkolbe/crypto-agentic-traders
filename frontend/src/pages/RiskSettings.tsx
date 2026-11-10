import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useState } from 'react'

import { ApiError, api } from '../api/client'
import { dateTime, money } from '../api/format'
import type { AuditEntry, CapitalStatus, RiskConfig } from '../api/types'
import {
  AgentsOffline,
  CapitalGateBanner,
  Card,
  CircuitBreakerBanner,
  Empty,
  ErroDeCarga,
  Loading,
  mensagemDeErro,
  useQuoteCurrency,
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
  /** Campo que aceita vazio como "sem limite". */
  nullable?: boolean
}[] = [
  {
    key: 'max_order_notional',
    label: 'Valor máximo por ordem',
    help:
      'Teto absoluto de uma única operação. DEIXE VAZIO para não ter teto: aí o ' +
      'percentual manda sozinho e a ordem acompanha a carteira a cada aporte, sem ' +
      'você reconfigurar nada. Com teto fixo, o sistema para de crescer quando o ' +
      'patrimônio passa dele.',
    kind: 'money',
    nullable: true,
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
    help:
      'Aumentar uma posição já existente não conta como nova. DEIXE VAZIO para não ' +
      'limitar: quem limita passa a ser o caixa, porque cada ordem consome dinheiro ' +
      'e a próxima só sai se sobrar acima da ordem mínima.',
    kind: 'int',
    nullable: true,
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
  {
    key: 'authorized_capital',
    label: 'Capital autorizado a operar',
    help:
      'Teto de dinheiro que o sistema pode pôr para trabalhar. Saldo acima disto fica ' +
      'parado e gera aviso pedindo autorização — um depósito não é uma ordem. DEIXE ' +
      'VAZIO para liberar todo o patrimônio, inclusive depósitos futuros, sem novo aval.',
    kind: 'money',
    nullable: true,
  },
  {
    key: 'mvrv_max_percentile',
    label: 'Filtro de regime MVRV',
    help:
      '1 desliga. 0,40 = não abrir posição quando o mercado estiver mais caro que 40% ' +
      'dos dias já observados. ATENÇÃO: medido em 0,40, o filtro bloqueou TODAS as ' +
      'entradas em dois de três períodos de um ano — o sistema simplesmente não ' +
      'operou. Ver docs/SEGURANCA.md seção 10. Nunca bloqueia fechamento.',
    kind: 'percent',
  },
]

/** Os valores do servidor como o formulário os mostra (texto, `null` = vazio). */
function rascunhoDe(config: RiskConfig): Record<string, string> {
  const rascunho: Record<string, string> = {}
  for (const field of FIELDS) {
    const valor = config[field.key]
    // `null` significa "sem limite": mostrar a palavra "null" num campo
    // numérico convidaria a digitar por cima sem entender o que havia ali.
    rascunho[field.key] = valor === null || valor === undefined ? '' : String(valor)
  }
  rascunho.asset_whitelist = config.asset_whitelist.join(', ')
  rascunho.symbol_whitelist = config.symbol_whitelist.join(', ')
  return rascunho
}

function mesmoRascunho(a: Record<string, string>, b: Record<string, string>): boolean {
  const chaves = new Set([...Object.keys(a), ...Object.keys(b)])
  return [...chaves].every((chave) => (a[chave] ?? '') === (b[chave] ?? ''))
}

export default function RiskSettings() {
  const queryClient = useQueryClient()
  const moeda = useQuoteCurrency()
  // Rotulo ligado ao campo (leitor de tela e clique no rotulo).
  const uid = useId()
  const [draft, setDraft] = useState<Record<string, string>>({})
  /**
   * Os valores do servidor de que o rascunho partiu.
   *
   * O rascunho era recopiado da query a CADA refetch — e o `alert` do tempo
   * real e o "Autorizar" recarregam a query. Medido: mudar o stop e, antes de
   * salvar, autorizar capital apagava a edição em silêncio; e com "Sim,
   * aplicar" já na tela, um refetch esvaziava o pedido e a tela dizia "Limites
   * atualizados" sem nada ter mudado. Agora o rascunho só segue o servidor
   * enquanto não há edição, e o pedido leva só o que o operador mudou em
   * relação a esta base — nunca um valor velho por cima de um novo.
   */
  const [base, setBase] = useState<Record<string, string> | null>(null)
  /** Campos editados aqui que o servidor também mudou durante a edição. */
  const [conflitos, setConflitos] = useState<string[]>([])
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const [confirming, setConfirming] = useState(false)

  const config = useQuery<RiskConfig>({
    queryKey: ['riskConfig'],
    queryFn: api.riskConfig,
    retry: false,
  })
  const audit = useQuery<AuditEntry[]>({ queryKey: ['audit'], queryFn: () => api.audit(20) })
  const capital = useQuery<CapitalStatus>({
    queryKey: ['capital'],
    queryFn: api.capitalStatus,
    retry: false,
  })

  const authorize = useMutation({
    mutationFn: api.authorizeCapital,
    onSuccess: (novo) => {
      setMessage({
        ok: true,
        text:
          `Capital autorizado: ${money(novo.authorized_capital, novo.quote_currency)}. ` +
          'O saldo passa a ser usado no próximo ciclo, e o registro foi para o log de auditoria.',
      })
      queryClient.invalidateQueries({ queryKey: ['capital'] })
      queryClient.invalidateQueries({ queryKey: ['riskConfig'] })
      queryClient.invalidateQueries({ queryKey: ['audit'] })
    },
    onError: (error) => setMessage({ ok: false, text: mensagemDeErro(error) }),
  })

  const editando = base !== null && !mesmoRascunho(draft, base)

  useEffect(() => {
    if (!config.data) return
    const novo = rascunhoDe(config.data)
    if (base === null) {
      setDraft(novo)
      setBase(novo)
      return
    }
    // Dado novo do servidor no meio da edição: o que o operador NÃO tocou passa
    // ao valor novo; o que ele tocou fica como digitou. Se o servidor mudou
    // justamente um campo tocado, o aviso abaixo diz qual.
    const tocados = Object.keys(draft).filter((k) => (draft[k] ?? '') !== (base[k] ?? ''))
    const rebaseado = { ...novo }
    for (const chave of tocados) rebaseado[chave] = draft[chave] ?? ''
    const novos = tocados.filter((k) => (novo[k] ?? '') !== (base[k] ?? ''))
    setConflitos((antes) => [...new Set([...antes, ...novos])])
    setDraft(rebaseado)
    setBase(novo)
    // `draft` e `base` ficam fora das dependências de propósito: só a chegada
    // de dados novos do servidor decide.
  }, [config.data])

  const save = useMutation({
    mutationFn: api.updateRiskConfig,
    onSuccess: (atualizado) => {
      setMessage({ ok: true, text: 'Limites atualizados e registrados no log de auditoria.' })
      setConfirming(false)
      const novo = rascunhoDe(atualizado)
      setDraft(novo)
      setBase(novo)
      setConflitos([])
      queryClient.invalidateQueries({ queryKey: ['riskConfig'] })
      queryClient.invalidateQueries({ queryKey: ['audit'] })
      // O capital autorizado tambem e um limite: o banner do portao e o
      // `sizing_feasible` do health mudam com ele.
      queryClient.invalidateQueries({ queryKey: ['capital'] })
      queryClient.invalidateQueries({ queryKey: ['health'] })
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
    // Qualquer outra falha era um formulário vazio e nenhum banner: parecia que
    // não havia trava acionada nem limite configurado.
    if (!config.data) return <ErroDeCarga error={error} oQue="os limites de risco" />
  }
  if (config.isLoading) return <Loading />

  const anterior = (chave: string) => base?.[chave] ?? ''
  const changed = editando
  const conflitosVigentes = conflitos.filter((k) => (draft[k] ?? '') !== anterior(k))
  const rotuloDoCampo = (chave: string) =>
    FIELDS.find((field) => field.key === chave)?.label ??
    (chave === 'asset_whitelist' ? 'Ativos permitidos' : 'Pares permitidos')

  const submit = () => {
    const payload: Record<string, unknown> = {}
    for (const field of FIELDS) {
      const value = draft[field.key]
      // Só o que o operador mudou em relação à base: um campo que mudou no
      // servidor enquanto ele editava (o capital, depois de "Autorizar") não
      // pode voltar ao valor velho por estar no rascunho.
      if (value === undefined || value === anterior(field.key)) continue
      if (value === '' && field.nullable) {
        // Num PUT parcial `null` significa "não enviei", então apagar um limite
        // precisa de um gesto próprio no contrato da API.
        payload[`clear_${field.key}`] = true
        continue
      }
      if (value === '') continue
      payload[field.key] = field.kind === 'money' ? value : Number(value)
    }
    const assets = splitList(draft.asset_whitelist)
    const symbols = splitList(draft.symbol_whitelist)
    if (assets.join(',') !== splitList(anterior('asset_whitelist')).join(','))
      payload.asset_whitelist = assets
    if (symbols.join(',') !== splitList(anterior('symbol_whitelist')).join(','))
      payload.symbol_whitelist = symbols

    if (Object.keys(payload).length === 0) {
      // Antes isto ia para a API como `{confirm: true}` e a tela dizia
      // "Limites atualizados" sem nada ter mudado.
      setConfirming(false)
      setMessage({ ok: false, text: 'Nada a aplicar: nenhum campo difere do que está valendo.' })
      return
    }
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

      {/* Sem isto, falha ao ler o capital sumia com o banner do portao: parecia
          que nao havia saldo esperando autorizacao. */}
      {capital.isError && !capital.data && (
        <ErroDeCarga error={capital.error} oQue="o capital autorizado" />
      )}

      <CapitalGateBanner
        status={capital.data}
        onAuthorize={(patrimonioVisto) => authorize.mutate(patrimonioVisto)}
        authorizing={authorize.isPending}
      />

      <CircuitBreakerBanner
        active={config.data?.circuit_breaker_active ?? false}
        reason={config.data?.circuit_breaker_reason}
        onReset={() => reset.mutate()}
        resetting={reset.isPending}
      />

      {config.isError && <ErroDeCarga error={config.error} oQue="a versão mais nova dos limites" />}

      {message && (
        <div className={`form-message ${message.ok ? 'ok' : 'err'}`} style={{ marginBottom: 14 }}>
          {message.text}
        </div>
      )}

      {conflitosVigentes.length > 0 && base && (
        <div className="form-message err" style={{ marginBottom: 14 }}>
          Enquanto você editava, o servidor também mudou:{' '}
          {conflitosVigentes.map(rotuloDoCampo).join(', ')}. Se aplicar, vale o que você digitou.{' '}
          <button
            className="small"
            onClick={() => {
              setDraft(base)
              setConflitos([])
              setConfirming(false)
            }}
          >
            Descartar minhas edições
          </button>
        </div>
      )}

      <Card>
        <div className="form-grid">
          {FIELDS.map((field) => (
            <div className="field" key={field.key}>
              <label htmlFor={`${uid}-${field.key}`}>
                {field.label}
                {/* Limite de dinheiro sem unidade e um numero que nao se pode
                    conferir: "5" e cinco de que? A moeda vem da configuracao. */}
                {field.kind === 'money' && moeda && <span className="faint"> ({moeda})</span>}
                {field.kind === 'percent' && <span className="faint"> (0–1)</span>}
                {field.nullable && <span className="faint"> — vazio = sem limite</span>}
              </label>
              <input
                id={`${uid}-${field.key}`}
                type="number"
                step={field.kind === 'int' ? '1' : 'any'}
                min="0"
                placeholder={field.nullable ? 'vazio = sem limite' : undefined}
                value={draft[field.key] ?? ''}
                onChange={(e) => setDraft({ ...draft, [field.key]: e.target.value })}
              />
              <span className="hint">{field.help}</span>
            </div>
          ))}
        </div>

        <div className="form-grid" style={{ marginTop: 14 }}>
          <div className="field">
            <label htmlFor={`${uid}-ativos`}>Ativos permitidos</label>
            <input
              id={`${uid}-ativos`}
              value={draft.asset_whitelist ?? ''}
              onChange={(e) => setDraft({ ...draft, asset_whitelist: e.target.value.toUpperCase() })}
            />
            <span className="hint">Separados por vírgula. Evita tokens ilíquidos ou desconhecidos.</span>
          </div>
          <div className="field">
            <label htmlFor={`${uid}-pares`}>Pares permitidos</label>
            <input
              id={`${uid}-pares`}
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
          {audit.isError && <ErroDeCarga error={audit.error} oQue="o log de auditoria" />}
          {audit.isError && !audit.data ? null : (audit.data ?? []).length === 0 ? (
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
