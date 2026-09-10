import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api/client'
import {
  comparacaoAtravessaTroca,
  dateTime,
  fronteiraDaMoeda,
  money,
  parDoEvento,
  percent,
  posicoesAbertas,
  quantity,
  quoteOf,
  rotuloDeDecisao,
  serieNaMoedaAtual,
  timeOnly,
  toNumber,
  unidadeDoEvento,
} from '../api/format'
import type { AuditEntry, EquityPoint, PortfolioSummary, RiskEvent } from '../api/types'
import { CompositionBar, DonutChart, TimeSeriesChart } from '../components/Charts'
import {
  Card,
  CircuitBreakerBanner,
  Empty,
  Loading,
  ModeBanner,
  SizingBanner,
  Stat,
  useHealth,
  useQuoteCurrency,
} from '../components/Shared'

/** Paleta das fatias de alocacao; cores repetem se houver muitos ativos. */
const SLICE_COLORS = ['#58a6ff', '#3fb950', '#d29922', '#bc8cff', '#f85149', '#39c5cf']

const RANGES = [
  { days: 1, label: '24h' },
  { days: 7, label: '7d' },
  { days: 30, label: '30d' },
  { days: 90, label: '90d' },
]

/** Quantas entradas de auditoria pedir para localizar a troca de moeda. */
const AUDIT_LIMITE = 50

export default function Dashboard({ feed }: { feed: { event: string; data: any; at: string }[] }) {
  const [days, setDays] = useState(30)
  const queryClient = useQueryClient()
  const health = useHealth()
  const moeda = useQuoteCurrency()

  const portfolio = useQuery<PortfolioSummary>({ queryKey: ['portfolio'], queryFn: api.portfolio })
  const history = useQuery<EquityPoint[]>({
    queryKey: ['portfolioHistory', days],
    queryFn: () => api.portfolioHistory(days),
  })
  const riskEvents = useQuery<RiskEvent[]>({
    queryKey: ['riskEvents'],
    queryFn: () => api.riskEvents(15),
  })
  // O log de auditoria e append-only e registra a troca de moeda de cotacao com
  // `before.quote_currency`/`after.quote_currency`. E a unica fonte que permite
  // ao frontend saber ATE QUANDO a serie de patrimonio esta em outra unidade —
  // o `EquityPoint` da API traz so timestamp e valor.
  const audit = useQuery<AuditEntry[]>({
    queryKey: ['audit', AUDIT_LIMITE],
    queryFn: () => api.audit(AUDIT_LIMITE),
  })

  const reset = useMutation({
    mutationFn: api.resetCircuitBreaker,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['health'] })
      queryClient.invalidateQueries({ queryKey: ['riskConfig'] })
    },
  })

  const current = portfolio.data?.current

  const pontos = history.data ?? []
  const entradas = audit.data ?? []
  const fronteira = fronteiraDaMoeda(
    entradas,
    moeda,
    pontos[0]?.timestamp ?? null,
    entradas.length < AUDIT_LIMITE,
  )
  const serie = serieNaMoedaAtual(pontos, fronteira)
  const omitidos = pontos.length - serie.length
  // A unidade so e afirmada quando se PROVA que a serie inteira esta nela.
  // Sem prova, o grafico mostra numero sem unidade — regra 3 do projeto: diante
  // de duvida, nao arrisca. Medido antes: o tooltip lia "150,00 USDC" sobre um
  // ponto de 150 BRL e "19,29 USDC" sobre um ponto da epoca do USDT.
  const moedaDaSerie = fronteira.tipo === 'desconhecida' ? '' : moeda
  // `toNumber` e a fronteira de renderizacao: dinheiro chega como string e so
  // vira `number` aqui, porque o SVG desenha em pixels. Nenhuma conta e feita
  // sobre esses numeros — a serie e plotada como veio do backend.
  const curve = serie.map((point) => ({
    time: point.timestamp,
    value: toNumber(point.total_value),
  }))

  // O "+51,84% em 24h" verde vinha do backend comparando 19,29 (era USDT) com
  // 29,29 (USDC) — dois numeros de unidades diferentes — ao lado de um grafico
  // que desenhava uma queda. Razao entre unidades diferentes nao e percentual.
  const travessia24h = comparacaoAtravessaTroca(fronteira, Date.now(), 24)

  const allocations = Object.entries(current?.allocations ?? {})
    .map(([asset, share]) => ({ asset, share }))
    .sort((a, b) => b.share - a.share)
  const fatias = allocations.map((entry, index) => ({
    label: entry.asset,
    value: entry.share,
    color: SLICE_COLORS[index % SLICE_COLORS.length]!,
  }))

  const posicoes = posicoesAbertas(current?.positions ?? [], moeda)
  const observados = health.data?.symbols ?? []
  const carregandoGrafico = history.isLoading || audit.isLoading

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Dashboard</h1>
          <p>Patrimônio, alocação e o que os agentes decidiram.</p>
        </div>
      </div>

      <ModeBanner health={health.data} />
      <SizingBanner health={health.data} />
      <CircuitBreakerBanner
        active={health.data?.circuit_breaker_active ?? false}
        onReset={() => reset.mutate()}
        resetting={reset.isPending}
      />

      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <Stat
          label="Patrimônio total"
          value={current ? money(current.total_value, moeda) : '—'}
          change={travessia24h ? undefined : portfolio.data?.change_24h_pct}
          hint={
            travessia24h
              ? fronteira.tipo === 'trocada'
                ? `sem 24h comparável: a cotação virou ${moeda} em ${dateTime(fronteira.desde)}`
                : 'sem 24h comparável: a unidade da série antiga não é conhecida'
              : 'em 24h'
          }
        />
        <Stat
          label="Caixa disponível"
          value={current ? money(current.cash_value, moeda) : '—'}
          hint={current ? `${money(current.positions_value, moeda)} em posições` : undefined}
        />
        <Stat
          label="PnL realizado"
          value={current ? money(current.realized_pnl, moeda) : '—'}
          hint={current ? `${money(current.unrealized_pnl, moeda)} não realizado` : undefined}
        />
        <Stat
          label="Operações hoje"
          value={String(portfolio.data?.trades_today ?? 0)}
          // Sem `change`: pendurar a variacao do PATRIMONIO em 7 dias embaixo de
          // uma CONTAGEM de operacoes do dia lia como se a contagem tivesse
          // variado — e com historico curto o campo vinha nulo e a tela
          // mostrava "— em 7 dias".
          hint={
            (portfolio.data?.trades_today ?? 0) === 0
              ? 'nenhuma desde a meia-noite'
              : 'desde a meia-noite'
          }
        />
      </div>

      <div className="grid grid-2" style={{ marginBottom: 14 }}>
        <Card
          title="Evolução do patrimônio"
          action={
            <div className="row-tight">
              {RANGES.map((range) => (
                <button
                  key={range.days}
                  className={`small ${days === range.days ? 'primary' : ''}`}
                  onClick={() => setDays(range.days)}
                >
                  {range.label}
                </button>
              ))}
            </div>
          }
        >
          {carregandoGrafico ? (
            <Loading />
          ) : curve.length < 2 ? (
            <Empty>
              Ainda não há histórico suficiente para o gráfico.
              <br />O Portfolio Agent grava um ponto por minuto enquanto o sistema roda.
              {omitidos > 0 && fronteira.tipo === 'trocada' && (
                <>
                  <br />
                  {omitidos} ponto(s) desta faixa são anteriores à troca de moeda e estão em{' '}
                  {fronteira.anterior}.
                </>
              )}
            </Empty>
          ) : (
            <>
              <TimeSeriesChart
                data={curve}
                height={260}
                fill
                formatValue={(value) => money(value, moedaDaSerie)}
                valueLabel="patrimônio"
              />
              {fronteira.tipo === 'trocada' && omitidos > 0 && (
                <div className="chart-note">
                  Série cortada em <strong>{dateTime(fronteira.desde)}</strong>, quando a cotação
                  passou a {moeda}. Ficaram fora {omitidos}{' '}
                  {omitidos === 1 ? 'ponto anterior' : 'pontos anteriores'}, medidos em{' '}
                  {fronteira.anterior}: patrimônio em unidades diferentes não forma uma curva.
                </div>
              )}
              {fronteira.tipo === 'desconhecida' && (
                <div className="chart-note">
                  Valores <strong>sem unidade</strong> de propósito. O log de auditoria consultado
                  não cobre o início desta série, então não há como provar em que moeda os pontos
                  antigos foram medidos — e afirmar a moeda de hoje sobre eles seria mentir.
                </div>
              )}
            </>
          )}
        </Card>

        <Card
          title="Alocação por ativo"
          subtitle={`Sobre o patrimônio total, incluindo o caixa${moeda ? ` em ${moeda}` : ''}.`}
        >
          {fatias.length === 0 ? (
            <Empty>Sem patrimônio apurado ainda.</Empty>
          ) : fatias.length === 1 ? (
            // Fatia unica: um anel de 180 px para dizer "100,00%" gasta um quarto
            // do card e nao informa nada. E o estado real de hoje — carteira toda
            // em caixa — entao ele precisa ser o estado BEM resolvido, nao a
            // degeneracao de um grafico feito para varios ativos.
            <div className="stack" style={{ gap: 12 }}>
              <CompositionBar data={fatias} />
              <div className="row-tight" style={{ justifyContent: 'space-between' }}>
                <span className="row-tight" style={{ gap: 7 }}>
                  <span className="dot" style={{ background: fatias[0]!.color }} />
                  <strong>{fatias[0]!.label}</strong>
                </span>
                <span className="num muted">{percent(fatias[0]!.value)}</span>
              </div>
              <div className="hint">
                Um único ativo no patrimônio: nada comprado ainda.
                {health.data?.sizing_detail && (
                  <>
                    {' '}
                    Quando o primeiro sinal for aprovado, {health.data.sizing_detail}.
                  </>
                )}
              </div>
            </div>
          ) : (
            <div className="row" style={{ alignItems: 'center', gap: 18 }}>
              {/* Tamanho fixo, nao medido: dentro de um flex um container
                  responsivo mede 0 na primeira renderizacao e o donut some. */}
              <div style={{ flexShrink: 0 }}>
                <DonutChart data={fatias} formatValue={(value) => percent(value)} />
              </div>

              <div style={{ flex: 1, minWidth: 160 }}>
                {fatias.map((fatia) => (
                  <div
                    key={fatia.label}
                    className="row-tight"
                    style={{ justifyContent: 'space-between', padding: '4px 0' }}
                  >
                    <span className="row-tight" style={{ gap: 7 }}>
                      <span className="dot" style={{ background: fatia.color }} />
                      {fatia.label}
                    </span>
                    <span className="num muted">{percent(fatia.value)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
      </div>

      <div className="grid grid-2">
        <Card title="Posições">
          {posicoes.length === 0 ? (
            // O estado vazio de HOJE, e o que o dono vai olhar por dias. Ele
            // responde a pergunta seguinte — "esperando o quê, em quê?" — em vez
            // de so constatar a ausencia.
            <Empty>
              Nenhuma posição aberta.
              <br />
              {moeda
                ? `O patrimônio está todo em caixa (${moeda}) esperando o primeiro sinal aprovado.`
                : 'O patrimônio está todo em caixa esperando o primeiro sinal aprovado.'}
              {observados.length > 0 && (
                <div className="watchlist">
                  <div className="card-title" style={{ marginBottom: 8 }}>
                    Observando {observados.length} {observados.length === 1 ? 'par' : 'pares'}
                  </div>
                  <div className="chip-row">
                    {observados.map((par) => (
                      <span key={par} className="chip">{par}</span>
                    ))}
                  </div>
                </div>
              )}
            </Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Ativo</th>
                    <th className="right">Quantidade</th>
                    <th className="right">Preço médio{moeda && ` (${moeda})`}</th>
                    <th className="right">Atual{moeda && ` (${moeda})`}</th>
                    <th className="right">PnL{moeda && ` (${moeda})`}</th>
                  </tr>
                </thead>
                <tbody>
                  {posicoes.map((position) => {
                    // `toNumber` so decide a COR; o valor exibido continua vindo
                    // da string original, sem passar por float.
                    const pnl = toNumber(position.unrealized_pnl)
                    return (
                      <tr key={position.asset}>
                        <td><strong>{position.asset}</strong></td>
                        <td className="right">{quantity(position.quantity)}</td>
                        <td className="right muted">{money(position.average_price, '')}</td>
                        <td className="right">
                          {position.current_price ? money(position.current_price, '') : '—'}
                        </td>
                        <td className={`right ${pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : 'muted'}`}>
                          {money(position.unrealized_pnl, '')}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card title="Atividade em tempo real">
          {feed.length === 0 ? (
            <Empty>
              Aguardando eventos.
              <br />
              Sinais, decisões de risco e execuções aparecem aqui assim que acontecem.
            </Empty>
          ) : (
            <div className="feed">
              {feed.map((item, index) => (
                <div key={index} className={`feed-item ${item.event}`}>
                  <span className="feed-time">{timeOnly(item.at)}</span>
                  <span>{describeEvent(item.event, item.data, moeda)}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <div style={{ marginTop: 14 }}>
        <Card title="Decisões recentes do Risk Manager">
          {riskEvents.isLoading ? (
            <Loading />
          ) : (riskEvents.data ?? []).length === 0 ? (
            <Empty>Nenhuma decisão registrada ainda.</Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Quando</th>
                    <th>Par</th>
                    <th>Decisão</th>
                    <th>Motivo</th>
                    <th className="right">Valor</th>
                  </tr>
                </thead>
                <tbody>
                  {(riskEvents.data ?? []).map((event) => {
                    const par = parDoEvento(event)
                    const decisao = rotuloDeDecisao(event)
                    return (
                      <tr key={event.id}>
                        <td className="muted">{timeOnly(event.created_at)}</td>
                        <td>{par || '—'}</td>
                        <td>
                          <span className={`badge ${decisao.classe}`}>{decisao.texto}</span>
                        </td>
                        <td className="muted" style={{ maxWidth: 420 }}>
                          {event.reasons.length > 0
                            ? event.reasons.join(' · ')
                            : event.decision === 'approved'
                              ? 'dentro de todos os limites'
                              : '—'}
                        </td>
                        <td className="right">
                          {event.approved_notional
                            ? money(event.approved_notional, unidadeDoEvento(event))
                            : '—'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  )
}

/** Converte o payload cru do WebSocket em uma frase legivel. */
function describeEvent(event: string, data: any, moeda: string): string {
  switch (event) {
    case 'signal':
      return `Sinal ${data.direction} em ${data.symbol} por ${data.strategy} (confiança ${percent(
        data.confidence,
        0,
      )}) — ${data.reason}`
    case 'risk_assessment':
      return data.decision === 'approved'
        ? `Risk Manager aprovou ${money(data.approved_notional ?? '0', quoteOf(data.symbol) || moeda)}`
        : `Risk Manager rejeitou: ${(data.reasons ?? []).join(' · ')}`
    case 'order_result':
      return `Ordem ${data.status} — ${quantity(data.filled_quantity)} @ ${money(
        data.average_price ?? '0',
        '',
      )}`
    case 'portfolio':
      // O percentual que estava aqui era `unrealized_pnl / max(total_value, 1)`:
      // uma DIVISAO de dinheiro por dinheiro em float (proibida por D7) e,
      // pior, com o denominador travado em 1 — numa carteira abaixo de 1 a
      // conta devolvia o proprio PnL disfarcado de percentual. O backend nao
      // manda essa razao neste evento, entao a frase mostra o que ele manda.
      return `Patrimônio atualizado: ${money(data.total_value, moeda)} · ${money(
        data.unrealized_pnl ?? '0',
        moeda,
      )} não realizado`
    case 'alert':
      return `ALERTA: ${data.reason ?? data.type}`
    default:
      return event
  }
}
