import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useState } from 'react'

import { ApiError, api } from '../api/client'
import { type Alteracao, alteracoesDeNegocio, MODE_LABEL, rotuloDeCampo } from '../api/format'
import type { TradingConfig } from '../api/types'
import {
  AgentsOffline,
  Card,
  ErroDeCarga,
  Loading,
  mensagemDeErro,
  useHealth,
} from '../components/Shared'

/**
 * Configuração de negócio: o que negociar e com que cadência.
 *
 * Esta tela é a contrapartida da regra "o `.env` só tem ambiente". Se as
 * variáveis de negócio saíram do arquivo, precisa existir um lugar de verdade
 * para editá-las — e é aqui. Cada campo traz o que ele muda no comportamento,
 * porque um número sem contexto convida a ser mexido sem que a consequência
 * apareça.
 */
type Campo = {
  key: keyof TradingConfig
  label: string
  help: string
  kind: 'text' | 'int' | 'money' | 'percent' | 'list'
  sufixo?: string
  upper?: boolean
  /** Codigos de ativo sao maiusculos; `timeframe` ("15m") nao e. */
}

const UNIVERSO: Campo[] = [
  {
    key: 'quote_currency',
    upper: true,
    label: 'Moeda de cotação',
    help:
      'Define o universo de pares e a moeda em que o saldo é medido. Você precisa ter ' +
      'saldo NESTA moeda na exchange para comprar qualquer coisa.',
    kind: 'text',
  },
  {
    key: 'symbols',
    upper: true,
    label: 'Pares negociados',
    help:
      'Separados por vírgula. DEIXE VAZIO para ligar a descoberta automática — o sistema ' +
      'escolhe os pares mais líquidos pelos critérios abaixo.',
    kind: 'list',
  },
  {
    key: 'timeframe',
    label: 'Timeframe',
    help: 'Tamanho do candle que dispara as estratégias: 15m, 1h, 4h, 1d.',
    kind: 'text',
  },
]

const CADENCIA: Campo[] = [
  {
    key: 'candle_history_limit',
    label: 'Histórico por par',
    help:
      'Quantos candles buscar. Define o aquecimento dos indicadores: valor baixo faz as ' +
      'médias longas nunca ficarem prontas.',
    kind: 'int',
    sufixo: 'candles',
  },
  {
    key: 'market_data_interval_seconds',
    label: 'Intervalo de coleta',
    help: 'De quanto em quanto tempo buscar preços. Consome rate limit da exchange.',
    kind: 'int',
    sufixo: 's',
  },
  {
    key: 'portfolio_interval_seconds',
    label: 'Intervalo do portfólio',
    help: 'Frequência do retrato da carteira, que é a base das decisões de risco.',
    kind: 'int',
    sufixo: 's',
  },
  {
    key: 'signal_batch_window_seconds',
    label: 'Janela de agrupamento de sinais',
    help:
      'Zero desliga. Acima de zero, sinais concorrentes esperam esta janela e o Risk ' +
      'Manager decide por confiança. Medido: não sustentou ganho — deixe em zero até ' +
      'validar que a confiança da estratégia prediz resultado.',
    kind: 'int',
    sufixo: 's',
  },
]

const DESCOBERTA: Campo[] = [
  {
    key: 'discovery_min_quote_volume_24h',
    label: 'Piso de liquidez em 24h',
    help: 'Par que move menos que isso é descartado. Liquidez baixa vira slippage.',
    kind: 'money',
  },
  {
    key: 'discovery_max_symbols',
    label: 'Máximo de pares descobertos',
    help: 'Teto de pares monitorados. Mais pares = mais escolhas, e mais chamadas por ciclo.',
    kind: 'int',
  },
  {
    key: 'discovery_exclude_assets',
    upper: true,
    label: 'Ativos excluídos',
    help:
      'Além das stablecoins, já excluídas por padrão. Separados por vírgula.',
    kind: 'list',
  },
  {
    key: 'discovery_refresh_hours',
    label: 'Intervalo entre varreduras',
    help: 'De quanto em quanto tempo revarrer o mercado em busca de pares.',
    kind: 'int',
    sufixo: 'h',
  },
]

const SIMULACAO: Campo[] = [
  {
    key: 'paper_initial_balance',
    label: 'Saldo inicial simulado',
    help: 'Carteira fictícia usada em dry_run e no backtest. Não afeta a conta real.',
    kind: 'money',
  },
  {
    key: 'paper_fee_pct',
    label: 'Taxa simulada',
    help: '0,001 = 0,1%, a taxa spot padrão da Binance. Subestimar aqui infla o backtest.',
    kind: 'percent',
  },
  {
    key: 'paper_slippage_pct',
    label: 'Slippage simulado',
    help: 'Diferença entre o preço visto e o preço obtido. Zero aqui é otimismo, não neutralidade.',
    kind: 'percent',
  },
]

const GRUPOS: { titulo: string; descricao: string; campos: Campo[] }[] = [
  {
    titulo: 'O que negociar',
    descricao: 'Define o universo. Trocar a moeda de cotação troca todos os pares.',
    campos: UNIVERSO,
  },
  {
    titulo: 'Cadência',
    descricao: 'Com que frequência o sistema olha o mercado e a carteira.',
    campos: CADENCIA,
  },
  {
    titulo: 'Descoberta automática',
    descricao: 'Só vale quando a lista de pares está vazia.',
    campos: DESCOBERTA,
  },
  {
    titulo: 'Simulação',
    descricao: 'Usado em dry_run e no backtest. Nunca toca dinheiro real.',
    campos: SIMULACAO,
  },
]

const TODOS = GRUPOS.flatMap((g) => g.campos)

function paraTexto(config: TradingConfig, campo: Campo): string {
  const valor = config[campo.key]
  return Array.isArray(valor) ? valor.join(', ') : String(valor)
}

function splitList(value: string): string[] {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

/** Os valores do servidor como o formulario os mostra. */
function rascunhoDe(config: TradingConfig): Record<string, string> {
  const rascunho: Record<string, string> = {}
  for (const campo of TODOS) rascunho[campo.key] = paraTexto(config, campo)
  rascunho.strategies = config.strategies.join(', ')
  return rascunho
}

export default function Configuration() {
  const queryClient = useQueryClient()
  const idBase = useId()
  const health = useHealth()
  const [draft, setDraft] = useState<Record<string, string>>({})
  /**
   * Os valores do servidor de que o rascunho partiu — o mesmo conserto da tela
   * Risco. O rascunho era recopiado da query a cada refetch: uma edição sumia em
   * silêncio quando a configuração recarregava, e um campo que o servidor mudou
   * durante a edição voltaria ao valor velho no envio.
   */
  const [base, setBase] = useState<Record<string, string> | null>(null)
  /** O pedido congelado no primeiro clique, com o que ele muda; `null` sem confirmação aberta. */
  const [pendente, setPendente] = useState<{
    pedido: Record<string, unknown>
    alteracoes: Alteracao[]
  } | null>(null)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)

  const config = useQuery<TradingConfig>({
    queryKey: ['tradingConfig'],
    queryFn: api.tradingConfig,
    retry: false,
  })

  useEffect(() => {
    if (!config.data) return
    const novo = rascunhoDe(config.data)
    if (base === null) {
      setDraft(novo)
      setBase(novo)
      return
    }
    // O que o operador NAO tocou segue o servidor; o que ele tocou fica.
    const tocados = Object.keys(draft).filter((k) => (draft[k] ?? '') !== (base[k] ?? ''))
    const rebaseado = { ...novo }
    for (const chave of tocados) rebaseado[chave] = draft[chave] ?? ''
    setDraft(rebaseado)
    setBase(novo)
    // `draft` e `base` fora das dependencias de proposito: so dado novo do
    // servidor decide.
  }, [config.data])

  const editar = (novo: Record<string, string>) => {
    setDraft(novo)
    // Mudou depois de abrir a confirmacao: o resumo que estava na tela ja nao
    // e o pedido. Fecha e pede o primeiro passo de novo.
    setPendente(null)
  }

  const save = useMutation({
    mutationFn: api.updateTradingConfig,
    onSuccess: (atualizado) => {
      setMessage({
        ok: true,
        text: 'Configuração aplicada, já valendo, e registrada no log de auditoria.',
      })
      setPendente(null)
      const novo = rascunhoDe(atualizado)
      setDraft(novo)
      setBase(novo)
      queryClient.invalidateQueries({ queryKey: ['tradingConfig'] })
      queryClient.invalidateQueries({ queryKey: ['audit'] })
      queryClient.invalidateQueries({ queryKey: ['health'] })
    },
    onError: (error) => setMessage({ ok: false, text: mensagemDeErro(error) }),
  })

  if (config.isError && !config.data) {
    const error = config.error
    if (error instanceof ApiError && error.agentsAreDown) return <AgentsOffline />
    // Qualquer outra falha era "Carregando…" para sempre.
    return <ErroDeCarga error={error} oQue="a configuração de negócio" />
  }
  if (config.isLoading || !config.data) return <Loading />

  const atual = config.data
  const estrategiasSelecionadas = splitList(draft.strategies ?? '')
  // Os valores em dinheiro na tela estao na moeda SALVA, nao na que esta sendo
  // digitada: enquanto a alteracao nao e aplicada, 29.29 continua sendo 29,29
  // da moeda antiga.
  const unidadeSalva = atual.quote_currency
  const moedaEmTroca =
    draft.quote_currency !== undefined &&
    draft.quote_currency.trim() !== '' &&
    draft.quote_currency.trim().toUpperCase() !== unidadeSalva.toUpperCase()

  const alterado =
    TODOS.some((campo) => draft[campo.key] !== undefined && draft[campo.key] !== paraTexto(atual, campo)) ||
    estrategiasSelecionadas.join(',') !== atual.strategies.join(',')

  const montarPedido = (): Record<string, unknown> => {
    const payload: Record<string, unknown> = {}
    for (const campo of TODOS) {
      const valor = draft[campo.key]
      if (valor === undefined || valor === paraTexto(atual, campo)) continue
      if (campo.kind === 'list') payload[campo.key] = splitList(valor)
      // `percent` aqui sao `paper_fee_pct`/`paper_slippage_pct`, tipados `Money`:
      // vao como texto, sem passar por float (D7). So `int` vira numero.
      else if (campo.kind === 'int') payload[campo.key] = Number(valor)
      else payload[campo.key] = valor
    }
    if (estrategiasSelecionadas.join(',') !== atual.strategies.join(',')) {
      payload.strategies = estrategiasSelecionadas
    }
    return payload
  }

  /** Primeiro passo: congela o pedido e mostra o que ele muda. Nada sai daqui. */
  const pedirConfirmacao = () => {
    const pedido = montarPedido()
    setMessage(null)
    if (Object.keys(pedido).length === 0) {
      setMessage({ ok: false, text: 'Nada a aplicar: nenhum campo difere do que está valendo.' })
      return
    }
    setPendente({
      pedido,
      alteracoes: alteracoesDeNegocio(TODOS, atual as unknown as Record<string, unknown>, pedido),
    })
  }

  const alternarEstrategia = (nome: string) => {
    const atuais = new Set(estrategiasSelecionadas)
    if (atuais.has(nome)) atuais.delete(nome)
    else atuais.add(nome)
    editar({ ...draft, strategies: [...atuais].join(', ') })
  }
  const modo = health.data?.mode

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Configurações</h1>
          <p>
            O que o sistema negocia e com que cadência. Estes valores moram no banco, não no
            arquivo de ambiente: alterações valem imediatamente, sem reiniciar, e ficam no log
            de auditoria.
          </p>
        </div>
      </div>

      <div className="info-banner" style={{ marginBottom: 14 }}>
        <strong>Modo de operação, credenciais e exchange não estão aqui.</strong> São
        configuração de ambiente e ficam no <code>.env</code> do servidor, deliberadamente fora
        do alcance do navegador — ligar dinheiro real exige editar o arquivo e reiniciar o
        processo.
      </div>

      {atual.discovery_enabled && (
        <div className="info-banner" style={{ marginBottom: 14 }}>
          <strong>Descoberta automática ativa.</strong> A lista de pares está vazia, então o
          sistema escolhe os pares mais líquidos cotados em {atual.quote_currency} pelos
          critérios da seção “Descoberta automática”.
        </div>
      )}

      {/* Trocar a moeda de cotacao NAO reconverte valor nenhum. A migracao de
          BRL para USDC exigiu reescalar limites e capital autorizado a mao, na
          cotacao do dia — e um campo de dinheiro que continua com o numero
          antigo passa a significar outra coisa em silencio. */}
      {moedaEmTroca && (
        <div className="alert-banner" style={{ marginBottom: 14 }}>
          <div>
            <strong>
              Trocar a cotação de {unidadeSalva} para {draft.quote_currency} não converte os valores.
            </strong>{' '}
            <span className="muted">
              Os campos em dinheiro desta tela e todos os limites em <strong>Risco</strong>
              {' '}continuam com o número que você vê agora, medido em {unidadeSalva}. Reescale-os
              na mesma alteração ou logo depois — e confira o mínimo por ordem da exchange na moeda
              nova.
            </span>
          </div>
        </div>
      )}

      {config.isError && (
        <ErroDeCarga error={config.error} oQue="a versão mais nova da configuração" />
      )}

      {message && (
        <div
          className={`form-message ${message.ok ? 'ok' : 'err'}`}
          style={{ marginBottom: 14 }}
          role={message.ok ? 'status' : 'alert'}
        >
          {message.text}
        </div>
      )}

      {GRUPOS.map((grupo) => (
        <Card key={grupo.titulo} title={grupo.titulo} subtitle={grupo.descricao}>
          <div className="form-grid">
            {grupo.campos.map((campo) => (
              <div className="field" key={campo.key}>
                {/* A unidade e parte do rotulo, nao enfeite: "Piso de liquidez
                    em 24h = 9760000" nao se confere sem saber de que. Vem de
                    `rotuloDeCampo`, que e verificada. */}
                <label htmlFor={`${idBase}-${campo.key}`}>{rotuloDeCampo(campo, unidadeSalva)}</label>
                <input
                  id={`${idBase}-${campo.key}`}
                  type={campo.kind === 'int' || campo.kind === 'percent' ? 'number' : 'text'}
                  step={campo.kind === 'int' ? '1' : 'any'}
                  min={campo.kind === 'int' || campo.kind === 'percent' ? '0' : undefined}
                  value={draft[campo.key] ?? ''}
                  onChange={(e) =>
                    editar({
                      ...draft,
                      [campo.key]: campo.upper ? e.target.value.toUpperCase() : e.target.value,
                    })
                  }
                />
                <span className="hint">{campo.help}</span>
              </div>
            ))}
          </div>
        </Card>
      ))}

      <Card
        title="Estratégias ativas"
        subtitle="Cada candle fechado passa por todas as marcadas. Ao menos uma é obrigatória."
      >
        <div className="form-grid">
          {Object.entries(atual.available_strategies).map(([nome, descricao]) => (
            <label key={nome} className="field" style={{ cursor: 'pointer' }}>
              <span className="row-tight">
                <input
                  type="checkbox"
                  checked={estrategiasSelecionadas.includes(nome)}
                  onChange={() => alternarEstrategia(nome)}
                  style={{ width: 'auto', marginRight: 8 }}
                />
                <strong>{nome}</strong>
              </span>
              <span className="hint">{descricao}</span>
            </label>
          ))}
        </div>
        {estrategiasSelecionadas.length === 0 && (
          <div className="form-message err" style={{ marginTop: 10 }}>
            Sem nenhuma estratégia o sistema fica de pé gerando zero sinais — parece saudável e
            nunca opera.
          </div>
        )}
      </Card>

      {/* Segundo passo, no padrao da tela Risco: o que muda, campo a campo, e
          em que modo. O pedido enviado e o que esta congelado em `pendente` —
          o mesmo de onde as linhas foram montadas. */}
      {pendente && (
        <div className="alert-banner" style={{ marginTop: 14 }} role="alertdialog">
          <div>
            <strong>
              Aplicar {pendente.alteracoes.length === 1 ? 'esta alteração' : 'estas alterações'}
              {modo ? ` em ${MODE_LABEL[modo] ?? modo}` : ''}? Vale na hora, sem reiniciar.
            </strong>
            <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>
              {pendente.alteracoes.map((a) => (
                <li key={a.campo} style={{ marginBottom: 4 }}>
                  <strong>{a.campo}:</strong>{' '}
                  <span className="muted">{a.antes}</span> → <span>{a.depois}</span>
                  {a.aviso && <div className="muted" style={{ fontSize: 12 }}>{a.aviso}</div>}
                </li>
              ))}
            </ul>
          </div>
          <div className="row-tight">
            <button
              className="danger"
              onClick={() => save.mutate(pendente.pedido)}
              disabled={save.isPending}
            >
              {save.isPending ? 'Aplicando…' : 'Sim, aplicar'}
            </button>
            <button onClick={() => setPendente(null)} disabled={save.isPending}>
              Cancelar
            </button>
          </div>
        </div>
      )}

      <div className="row" style={{ marginTop: 14, gap: 10 }}>
        {/* `primary`: nao existe `.btn` nem `.btn-primary` no CSS deste projeto,
            e o botao que aplica a configuracao de negocio saia com a aparencia
            de um botao secundario. */}
        <button
          className="primary"
          disabled={
            !alterado || save.isPending || estrategiasSelecionadas.length === 0 || pendente !== null
          }
          onClick={pedirConfirmacao}
        >
          Aplicar alterações
        </button>
        {alterado && (
          <button
            onClick={() => {
              editar(rascunhoDe(atual))
              setMessage(null)
            }}
          >
            Descartar
          </button>
        )}
      </div>
    </>
  )
}
