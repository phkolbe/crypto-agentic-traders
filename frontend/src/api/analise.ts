/**
 * Contas da tela Portfolio, como funcoes puras (verificadas em `telas.check.ts`).
 *
 * Tudo aqui trabalha sobre series JA cortadas na moeda atual (`serieNaMoedaAtual`):
 * drawdown ou retorno entre um ponto em BRL e outro em USDC nao e uma razao, e um
 * numero entre unidades diferentes. As razoes sao feitas em `number` porque viram
 * percentual de grafico, nunca dinheiro exibido.
 */

export type ResultadoDoDrawdown = {
  /** Fracao abaixo do maior valor ate o ponto (0 no pico, -0,12 = 12% abaixo). */
  serie: number[]
  /** A maior queda do periodo (fracao <= 0). */
  maior: number
  indiceDoPico: number
  indiceDoVale: number
}

/** Quanto cada ponto esta abaixo do pico mais alto ANTERIOR a ele. */
export function drawdown(valores: number[]): ResultadoDoDrawdown {
  const serie: number[] = []
  let pico = -Infinity
  let indicePico = 0
  let maior = 0
  let indiceDoPico = 0
  let indiceDoVale = 0
  valores.forEach((valor, i) => {
    if (valor > pico) {
      pico = valor
      indicePico = i
    }
    const queda = pico > 0 ? valor / pico - 1 : 0
    serie.push(queda)
    if (queda < maior) {
      maior = queda
      indiceDoPico = indicePico
      indiceDoVale = i
    }
  })
  return { serie, maior, indiceDoPico, indiceDoVale }
}

export type Agrupamento = 'dia' | 'semana' | 'mes'

export type RetornoDoPeriodo = { chave: string; rotulo: string; inicio: string; retorno: number }

const MESES = ['jan', 'fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez']

/** Chave do grupo de um dia UTC `YYYY-MM-DD`: o proprio dia, a segunda-feira da semana ou o mes. */
export function chaveDoGrupo(dia: string, agrupamento: Agrupamento): string {
  if (agrupamento === 'dia') return dia
  if (agrupamento === 'mes') return dia.slice(0, 7)
  const data = new Date(`${dia}T00:00:00Z`)
  const recuo = (data.getUTCDay() + 6) % 7 // segunda = 0
  data.setUTCDate(data.getUTCDate() - recuo)
  return data.toISOString().slice(0, 10)
}

function rotuloDoGrupo(chave: string, agrupamento: Agrupamento): string {
  if (agrupamento === 'mes') {
    const [a, m] = chave.split('-')
    return `${MESES[Number(m) - 1]}/${a!.slice(2)}`
  }
  const [, m, d] = chave.split('-')
  return agrupamento === 'semana' ? `sem ${d}/${m}` : `${d}/${m}`
}

/**
 * Retorno de cada dia, semana ou mes: o ultimo fechamento do grupo contra o
 * ultimo fechamento do grupo ANTERIOR.
 *
 * `fechamentos` vem em ordem, um por dia UTC, e o primeiro e o dia anterior ao
 * periodo -- ele e so a base do primeiro retorno, e nao vira barra. Grupo sem
 * base (o primeiro, se faltar o dia anterior) fica de fora, em vez de inventar 0%.
 */
export function retornosPorPeriodo(
  fechamentos: { dia: string; valor: number }[],
  agrupamento: Agrupamento,
): RetornoDoPeriodo[] {
  const grupos: { chave: string; ultimo: number; primeiroDia: string }[] = []
  for (const f of fechamentos) {
    const chave = chaveDoGrupo(f.dia, agrupamento)
    const atual = grupos[grupos.length - 1]
    if (atual && atual.chave === chave) atual.ultimo = f.valor
    else grupos.push({ chave, ultimo: f.valor, primeiroDia: f.dia })
  }
  const retornos: RetornoDoPeriodo[] = []
  for (let i = 1; i < grupos.length; i += 1) {
    const base = grupos[i - 1]!.ultimo
    if (!(base > 0)) continue
    const g = grupos[i]!
    retornos.push({
      chave: g.chave,
      rotulo: rotuloDoGrupo(g.chave, agrupamento),
      inicio: g.primeiroDia,
      retorno: g.ultimo / base - 1,
    })
  }
  return retornos
}

export type Comparacao = { dias: string[]; carteira: (number | null)[]; benchmark: (number | null)[] }

/**
 * Carteira e benchmark em % desde o PRIMEIRO dia em que os dois existem.
 *
 * A base comum e o ponto todo: comparar a carteira desde 07/09 com o BTC desde
 * 01/09 daria a um dos dois uma semana de vantagem. Dia sem um dos lados fica
 * `null` (a linha falha ali), em vez de repetir o valor anterior.
 */
export function comparacaoComBenchmark(
  carteira: { dia: string; valor: number }[],
  benchmark: { dia: string; valor: number }[],
): Comparacao {
  const c = new Map(carteira.map((p) => [p.dia, p.valor]))
  const b = new Map(benchmark.map((p) => [p.dia, p.valor]))
  const dias = [...new Set([...c.keys(), ...b.keys()])].sort()
  const baseDia = dias.find((d) => (c.get(d) ?? 0) > 0 && (b.get(d) ?? 0) > 0)
  if (baseDia === undefined) return { dias: [], carteira: [], benchmark: [] }
  const baseC = c.get(baseDia)!
  const baseB = b.get(baseDia)!
  const usados = dias.filter((d) => d >= baseDia)
  return {
    dias: usados,
    carteira: usados.map((d) => (c.has(d) ? c.get(d)! / baseC - 1 : null)),
    benchmark: usados.map((d) => (b.has(d) ? b.get(d)! / baseB - 1 : null)),
  }
}

export type AlocacaoNoTempo = { ativos: string[]; series: number[][] }

/**
 * Fracao de cada ativo em cada ponto, com os `maximo` maiores (pela media no
 * periodo) e o resto somado em "Outros". A moeda de cotacao (o caixa) entra como
 * ativo: e a parte da carteira que nao esta exposta.
 */
export function alocacaoNoTempo(
  pontos: { allocations: Record<string, number> }[],
  maximo = 5,
): AlocacaoNoTempo {
  const somas = new Map<string, number>()
  for (const p of pontos) {
    for (const [ativo, fracao] of Object.entries(p.allocations ?? {})) {
      somas.set(ativo, (somas.get(ativo) ?? 0) + fracao)
    }
  }
  const ordenados = [...somas.entries()].sort((a, b) => b[1] - a[1]).map(([a]) => a)
  const principais = ordenados.slice(0, maximo)
  const temOutros = ordenados.length > maximo
  const ativos = temOutros ? [...principais, 'Outros'] : principais
  const series = ativos.map((ativo) =>
    pontos.map((p) => {
      const alocacao = p.allocations ?? {}
      if (ativo !== 'Outros') return alocacao[ativo] ?? 0
      return Object.entries(alocacao)
        .filter(([a]) => !principais.includes(a))
        .reduce((s, [, f]) => s + f, 0)
    }),
  )
  return { ativos, series }
}

/** Dia UTC `YYYY-MM-DD` de um instante ISO. */
export function diaUtc(instante: string): string {
  return new Date(instante).toISOString().slice(0, 10)
}

/** "<ativo>" e "<n>" do motivo canonico, para leitura: "não há posição em [ativo]". */
export function motivoLegivel(motivo: string): string {
  return motivo.replace(/<ativo>/g, '[ativo]').replace(/<par>/g, '[par]').replace(/<n>/g, 'N')
}
