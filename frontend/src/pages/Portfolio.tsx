import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import {
  type Agrupamento,
  alocacaoNoTempo,
  comparacaoComBenchmark,
  diaUtc,
  drawdown,
  motivoLegivel,
  retornosPorPeriodo,
} from '../api/analise'
import { api } from '../api/client'
import {
  fronteiraDaMoeda,
  money,
  percent,
  serieNaMoedaAtual,
  signedPercent,
  toNumber,
} from '../api/format'
import { chaveDoPeriodo, parametrosDoPeriodo, rotuloDoPeriodo } from '../api/periodo'
import type { AnalisePortfolio, AuditEntry, EquityPoint } from '../api/types'
import { BarChart, HorizontalBars, MultiSeriesChart } from '../components/Charts'
import { SeletorDePeriodo, usePeriodo } from '../components/Periodo'
import { Card, Empty, Loading, Stat, useQuoteCurrency } from '../components/Shared'

/** Cores das series; repetem se houver muitos ativos. */
const CORES = ['#58a6ff', '#3fb950', '#d29922', '#bc8cff', '#f85149', '#39c5cf']

/** Quantas entradas de auditoria pedir para localizar a troca de moeda (igual ao Dashboard). */
const AUDIT_LIMITE = 50

const AGRUPAMENTOS: { valor: Agrupamento; rotulo: string }[] = [
  { valor: 'dia', rotulo: 'Dia' },
  { valor: 'semana', rotulo: 'Semana' },
  { valor: 'mes', rotulo: 'Mês' },
]

export default function Portfolio() {
  const [periodo, setPeriodo] = usePeriodo('portfolio', { tipo: 'atalho', dias: 90 })
  const [agrupamento, setAgrupamento] = useState<Agrupamento>('dia')
  const moeda = useQuoteCurrency()
  const chave = chaveDoPeriodo(periodo)

  const historico = useQuery<EquityPoint[]>({
    queryKey: ['portfolioHistory', chave],
    queryFn: () => api.portfolioHistory(parametrosDoPeriodo(periodo, new Date())),
  })
  const analise = useQuery<AnalisePortfolio>({
    queryKey: ['portfolioAnalise', chave],
    queryFn: () => api.portfolioAnalise(parametrosDoPeriodo(periodo, new Date())),
    refetchInterval: 5 * 60 * 1000,
  })
  // A mesma prova de unidade do Dashboard: ponto medido em outra moeda de
  // cotacao nao entra em razao nenhuma (drawdown, retorno, comparacao).
  const audit = useQuery<AuditEntry[]>({
    queryKey: ['audit', AUDIT_LIMITE],
    queryFn: () => api.audit(AUDIT_LIMITE),
  })

  if (historico.isLoading || analise.isLoading || audit.isLoading) {
    return (
      <>
        <Cabecalho periodo={periodo} onChange={setPeriodo} />
        <Loading />
      </>
    )
  }

  const pontos = historico.data ?? []
  const entradas = audit.data ?? []
  const fronteira = fronteiraDaMoeda(
    entradas,
    moeda,
    pontos[0]?.timestamp ?? null,
    entradas.length < AUDIT_LIMITE,
  )
  const unidade = fronteira.tipo === 'desconhecida' ? '' : moeda
  const serie = serieNaMoedaAtual(pontos, fronteira)
  const instantes = serie.map((p) => p.timestamp)
  const totais = serie.map((p) => toNumber(p.total_value))
  const dd = drawdown(totais)
  const retornoDoPeriodo =
    totais.length >= 2 && totais[0]! > 0 ? totais[totais.length - 1]! / totais[0]! - 1 : null

  const dados = analise.data
  const diarios = serieNaMoedaAtual(dados?.fechamentos_diarios ?? [], fronteira).map((p) => ({
    dia: diaUtc(p.timestamp),
    valor: toNumber(p.total_value),
  }))
  const retornos = retornosPorPeriodo(diarios, agrupamento)
  const btc = (dados?.benchmark?.fechamentos ?? []).map((f) => ({
    dia: diaUtc(f.timestamp),
    valor: toNumber(f.close),
  }))
  const comparacao = comparacaoComBenchmark(diarios, btc)
  const ultimo = (lista: (number | null)[]) => [...lista].reverse().find((v) => v !== null) ?? null
  const carteiraVsBtc = {
    carteira: ultimo(comparacao.carteira),
    btc: ultimo(comparacao.benchmark),
  }

  const pares = dados?.pares ?? []
  const ganhos = pares.reduce((s, p) => s + p.ganhos, 0)
  const perdas = pares.reduce((s, p) => s + p.perdas, 0)
  const alocacao = alocacaoNoTempo(serie.map((p) => ({ allocations: p.allocations ?? {} })))
  const risco = dados?.risco

  return (
    <>
      <Cabecalho periodo={periodo} onChange={setPeriodo} />

      {fronteira.tipo === 'trocada' && pontos.length > serie.length && (
        <div className="chart-note" style={{ marginBottom: 14 }}>
          A cotação passou a {moeda} dentro deste período: os pontos anteriores, em{' '}
          {fronteira.anterior}, ficam fora de todos os gráficos de patrimônio.
        </div>
      )}

      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <Stat
          label="Retorno no período"
          value={signedPercent(retornoDoPeriodo)}
          hint={
            totais.length >= 2
              ? `${money(totais[0]!, unidade)} → ${money(totais[totais.length - 1]!, unidade)}`
              : 'histórico insuficiente'
          }
        />
        <Stat
          label="Maior queda"
          value={totais.length >= 2 ? percent(dd.maior) : '—'}
          hint={
            dd.maior < 0
              ? `do pico em ${new Date(instantes[dd.indiceDoPico]!).toLocaleDateString('pt-BR')}`
              : 'sem queda no período'
          }
        />
        <Stat
          label={`Carteira × ${dados?.benchmark?.simbolo ?? 'BTC'}`}
          value={
            carteiraVsBtc.carteira !== null && carteiraVsBtc.btc !== null
              ? // Diferenca de dois percentuais: pontos percentuais, nao %.
                signedPercent(carteiraVsBtc.carteira - carteiraVsBtc.btc, 1).replace('%', ' p.p.')
              : '—'
          }
          hint={
            carteiraVsBtc.carteira !== null && carteiraVsBtc.btc !== null
              ? `carteira ${signedPercent(carteiraVsBtc.carteira)} · BTC ${signedPercent(carteiraVsBtc.btc)}`
              : 'sem dias em comum para comparar'
          }
        />
        <Stat
          label="Taxa de acerto"
          value={ganhos + perdas > 0 ? percent(ganhos / (ganhos + perdas), 0) : '—'}
          hint={
            ganhos + perdas > 0
              ? `${ganhos} de ${ganhos + perdas} vendas com lucro`
              : 'nenhuma venda com base de custo no período'
          }
        />
      </div>

      <div className="grid grid-2" style={{ marginBottom: 14 }}>
        <Card
          title="Queda desde o pico (drawdown)"
          subtitle="Quanto o patrimônio está abaixo do maior valor já atingido no período."
        >
          {totais.length < 2 ? (
            <Empty>Ainda não há histórico suficiente no período.</Empty>
          ) : (
            <MultiSeriesChart
              instantes={instantes}
              series={[{ nome: 'abaixo do pico', cor: 'var(--negative)', valores: dd.serie }]}
              area
              incluirZero
              formatValue={(v) => percent(v)}
              formatY={(v) => percent(v, 1)}
            />
          )}
        </Card>

        <Card
          title={`Carteira × ${dados?.benchmark?.simbolo ?? 'BTC'}`}
          subtitle="Variação desde o primeiro dia em comum: a estratégia contra comprar BTC e segurar."
        >
          {comparacao.dias.length < 2 ? (
            <Empty>
              {dados?.benchmark
                ? 'Sem dias suficientes em comum entre a carteira e o BTC no período.'
                : `Não há candles diários de BTC/${moeda} gravados para comparar.`}
            </Empty>
          ) : (
            <MultiSeriesChart
              instantes={comparacao.dias.map((d) => `${d}T00:00:00Z`)}
              series={[
                { nome: 'carteira', cor: 'var(--accent)', valores: comparacao.carteira },
                { nome: 'BTC', cor: 'var(--warning)', valores: comparacao.benchmark },
              ]}
              incluirZero
              formatValue={(v) => signedPercent(v)}
              formatY={(v) => percent(v, 0)}
            />
          )}
        </Card>

        <Card
          title="Resultado acumulado"
          subtitle="Realizado (já virou dinheiro) × não realizado (posições abertas, a preço de mercado)."
        >
          {serie.length < 2 ? (
            <Empty>Ainda não há histórico suficiente no período.</Empty>
          ) : (
            <MultiSeriesChart
              instantes={instantes}
              series={[
                {
                  nome: 'realizado',
                  cor: 'var(--positive)',
                  valores: serie.map((p) => toNumber(p.realized_pnl ?? '0')),
                },
                {
                  nome: 'não realizado',
                  cor: 'var(--accent)',
                  valores: serie.map((p) => toNumber(p.unrealized_pnl ?? '0')),
                },
              ]}
              incluirZero
              formatValue={(v) => money(v, unidade)}
            />
          )}
        </Card>

        <Card
          title="Retorno por período"
          subtitle="Fechamento de cada período contra o do anterior (dias UTC)."
          action={
            <div className="row-tight">
              {AGRUPAMENTOS.map((a) => (
                <button
                  key={a.valor}
                  className={`small ${agrupamento === a.valor ? 'primary' : ''}`}
                  onClick={() => setAgrupamento(a.valor)}
                >
                  {a.rotulo}
                </button>
              ))}
            </div>
          }
        >
          {retornos.length === 0 ? (
            <Empty>Ainda não há dois fechamentos para comparar neste agrupamento.</Empty>
          ) : (
            <BarChart
              itens={retornos.map((r) => ({ rotulo: r.rotulo, valor: r.retorno }))}
              formatValue={(v) => signedPercent(v)}
              formatY={(v) => percent(v, 1)}
            />
          )}
        </Card>

        <Card
          title="Caixa × posições"
          subtitle="Quanto do patrimônio esteve exposto ao mercado e quanto parado em caixa."
        >
          {serie.length < 2 ? (
            <Empty>Ainda não há histórico suficiente no período.</Empty>
          ) : (
            <MultiSeriesChart
              instantes={instantes}
              series={[
                {
                  nome: `caixa${unidade ? ` (${unidade})` : ''}`,
                  cor: '#8b949e',
                  valores: serie.map((p) => toNumber(p.cash_value ?? '0')),
                },
                {
                  nome: 'posições',
                  cor: 'var(--accent)',
                  valores: serie.map((p) => toNumber(p.positions_value ?? '0')),
                },
              ]}
              empilhado
              formatValue={(v) => money(v, unidade)}
            />
          )}
        </Card>

        <Card title="Alocação no tempo" subtitle="Fração do patrimônio em cada ativo (o caixa incluído).">
          {serie.length < 2 || alocacao.ativos.length === 0 ? (
            <Empty>Ainda não há histórico suficiente no período.</Empty>
          ) : (
            <MultiSeriesChart
              instantes={instantes}
              series={alocacao.ativos.map((ativo, i) => ({
                nome: ativo,
                cor: CORES[i % CORES.length]!,
                valores: alocacao.series[i]!,
              }))}
              empilhado
              formatValue={(v) => percent(v)}
              formatY={(v) => percent(v, 0)}
            />
          )}
        </Card>
      </div>

      <div className="stack">
        <Card
          title="Resultado por par"
          subtitle="PnL realizado no período, por custo médio (a mesma conta do patrimônio)."
        >
          {pares.length === 0 ? (
            <Empty>Nenhuma compra ou venda no período.</Empty>
          ) : (
            <div className="pares-layout">
              <HorizontalBars
                itens={pares.map((p) => ({ rotulo: p.simbolo, valor: toNumber(p.realizado) }))}
                formatValue={(v) => money(v, moeda)}
              />
              <div className="table-wrap">
                <table className="nowrap">
                  <thead>
                    <tr>
                      <th>Par</th>
                      <th className="right">Realizado</th>
                      <th className="right">Acerto</th>
                      <th className="right">Vendas</th>
                      <th className="right">Compras</th>
                      <th className="right">Taxas</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pares.map((p) => {
                      const valor = toNumber(p.realizado)
                      const comBase = p.ganhos + p.perdas
                      return (
                        <tr key={p.simbolo}>
                          <td>
                            <strong>{p.simbolo}</strong>
                          </td>
                          <td className={`right ${valor > 0 ? 'positive' : valor < 0 ? 'negative' : 'muted'}`}>
                            {money(p.realizado, p.moeda)}
                          </td>
                          <td className="right">
                            {comBase > 0 ? `${p.ganhos}/${comBase}` : '—'}
                          </td>
                          <td className="right">
                            {p.vendas}
                            {p.vendas_sem_base > 0 && (
                              <span className="faint" title="venda sem base de custo: fora do acerto">
                                {' '}
                                ({p.vendas_sem_base} s/ base)
                              </span>
                            )}
                          </td>
                          <td className="right">{p.compras}</td>
                          <td className="right muted">{money(p.taxas, p.moeda)}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </Card>

        <Card
          title="Decisões do Risk Manager"
          subtitle="Aprovadas × rejeitadas no período, e o que mais segurou as ordens."
        >
          {!risco || risco.aprovadas + risco.rejeitadas === 0 ? (
            <Empty>Nenhuma decisão sobre sinal no período.</Empty>
          ) : (
            <div className="grid grid-2" style={{ alignItems: 'start' }}>
              <HorizontalBars
                itens={[
                  { rotulo: 'aprovadas', valor: risco.aprovadas },
                  { rotulo: 'rejeitadas', valor: -risco.rejeitadas },
                ]}
                formatValue={(v) => String(Math.abs(v))}
              />
              {risco.motivos.length > 0 && (
                <div>
                  <div className="card-title">Motivos de rejeição</div>
                  <HorizontalBars
                    itens={risco.motivos.map((m) => ({
                      rotulo: motivoLegivel(m.motivo),
                      valor: m.total,
                      detalhe: `ex.: ${m.exemplo}`,
                    }))}
                    formatValue={(v) => String(v)}
                    corNeutra="var(--warning)"
                    rotulosLongos
                  />
                </div>
              )}
            </div>
          )}
        </Card>
      </div>
    </>
  )
}

function Cabecalho({
  periodo,
  onChange,
}: {
  periodo: Parameters<typeof rotuloDoPeriodo>[0]
  onChange: (p: Parameters<typeof rotuloDoPeriodo>[0]) => void
}) {
  return (
    <div className="page-header">
      <div>
        <h1>Portfólio</h1>
        <p>Evolução, risco e resultado da carteira — {rotuloDoPeriodo(periodo)}.</p>
      </div>
      <SeletorDePeriodo periodo={periodo} onChange={onChange} />
    </div>
  )
}
