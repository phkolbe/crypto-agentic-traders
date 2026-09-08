import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { ApiError, api } from '../api/client'
import type { TradingConfig } from '../api/types'
import { AgentsOffline, Card, Loading } from '../components/Shared'

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

export default function Configuration() {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)

  const config = useQuery<TradingConfig>({
    queryKey: ['tradingConfig'],
    queryFn: api.tradingConfig,
    retry: false,
  })

  useEffect(() => {
    if (!config.data) return
    const inicial: Record<string, string> = {}
    for (const campo of TODOS) inicial[campo.key] = paraTexto(config.data, campo)
    inicial.strategies = config.data.strategies.join(', ')
    setDraft(inicial)
  }, [config.data])

  const save = useMutation({
    mutationFn: api.updateTradingConfig,
    onSuccess: () => {
      setMessage({
        ok: true,
        text: 'Configuração aplicada, já valendo, e registrada no log de auditoria.',
      })
      queryClient.invalidateQueries({ queryKey: ['tradingConfig'] })
      queryClient.invalidateQueries({ queryKey: ['audit'] })
      queryClient.invalidateQueries({ queryKey: ['health'] })
    },
    onError: (error) =>
      setMessage({ ok: false, text: error instanceof ApiError ? error.message : String(error) }),
  })

  if (config.isError) {
    const error = config.error
    if (error instanceof ApiError && error.agentsAreDown) return <AgentsOffline />
  }
  if (config.isLoading || !config.data) return <Loading />

  const atual = config.data
  const estrategiasSelecionadas = splitList(draft.strategies ?? '')

  const alterado =
    TODOS.some((campo) => draft[campo.key] !== undefined && draft[campo.key] !== paraTexto(atual, campo)) ||
    estrategiasSelecionadas.join(',') !== atual.strategies.join(',')

  const submit = () => {
    const payload: Record<string, unknown> = {}
    for (const campo of TODOS) {
      const valor = draft[campo.key]
      if (valor === undefined || valor === paraTexto(atual, campo)) continue
      if (campo.kind === 'list') payload[campo.key] = splitList(valor)
      else if (campo.kind === 'text' || campo.kind === 'money') payload[campo.key] = valor
      else payload[campo.key] = Number(valor)
    }
    if (estrategiasSelecionadas.join(',') !== atual.strategies.join(',')) {
      payload.strategies = estrategiasSelecionadas
    }
    setMessage(null)
    save.mutate(payload)
  }

  const alternarEstrategia = (nome: string) => {
    const atuais = new Set(estrategiasSelecionadas)
    if (atuais.has(nome)) atuais.delete(nome)
    else atuais.add(nome)
    setDraft({ ...draft, strategies: [...atuais].join(', ') })
  }

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

      {message && (
        <div className={`form-message ${message.ok ? 'ok' : 'err'}`} style={{ marginBottom: 14 }}>
          {message.text}
        </div>
      )}

      {GRUPOS.map((grupo) => (
        <Card key={grupo.titulo} title={grupo.titulo} subtitle={grupo.descricao}>
          <div className="form-grid">
            {grupo.campos.map((campo) => (
              <div className="field" key={campo.key}>
                <label>
                  {campo.label}
                  {campo.sufixo && <span className="faint"> ({campo.sufixo})</span>}
                  {campo.kind === 'percent' && <span className="faint"> (0–1)</span>}
                </label>
                <input
                  type={campo.kind === 'int' || campo.kind === 'percent' ? 'number' : 'text'}
                  step={campo.kind === 'int' ? '1' : 'any'}
                  min={campo.kind === 'int' || campo.kind === 'percent' ? '0' : undefined}
                  value={draft[campo.key] ?? ''}
                  onChange={(e) =>
                    setDraft({
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

      <div className="row" style={{ marginTop: 14, gap: 10 }}>
        <button
          className="btn btn-primary"
          disabled={!alterado || save.isPending || estrategiasSelecionadas.length === 0}
          onClick={submit}
        >
          {save.isPending ? 'Aplicando…' : 'Aplicar alterações'}
        </button>
        {alterado && (
          <button
            className="btn"
            onClick={() => {
              const inicial: Record<string, string> = {}
              for (const campo of TODOS) inicial[campo.key] = paraTexto(atual, campo)
              inicial.strategies = atual.strategies.join(', ')
              setDraft(inicial)
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
