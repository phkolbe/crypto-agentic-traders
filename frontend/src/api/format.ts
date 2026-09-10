/**
 * Formatacao e decisoes de exibicao.
 *
 * Unico lugar onde `Money` (string) vira `number`. A conversao acontece no
 * ultimo instante possivel, so para renderizar.
 *
 * Tambem mora aqui toda decisao de exibicao que precisa de PROVA: que unidade
 * afirmar, que rotulo dar a um evento, que ponto da serie e comparavel. Elas
 * saem do JSX de proposito — dentro de um ternario a unica verificacao possivel
 * e casar a string do codigo-fonte, que passa com qualquer logica atras; como
 * funcao pura, a verificacao ve a decisao acontecer.
 */

import type { AuditEntry, Money, Position } from './types'

/**
 * Locale de NUMERO, nao de moeda.
 *
 * Define separador decimal e de milhar em pt-BR ("29,29"). A moeda e outra
 * coisa e vem sempre da configuracao — confundir as duas foi exatamente o que
 * fez a tela anunciar "USDT" depois da migracao para USDC.
 */
const LOCALE_PT_BR = 'pt-BR'

export function toNumber(value: Money | null | undefined): number {
  if (value === null || value === undefined || value === '') return 0
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

/**
 * Valor monetario formatado, com a moeda passada explicitamente.
 *
 * `currency` e OBRIGATORIO de proposito. Enquanto havia um padrao (`'USDT'`),
 * esquecer o argumento nao dava erro nenhum: dava uma tela que afirmava com
 * confianca a moeda errada. Sem padrao, o `tsc` acha toda chamada que esqueceu.
 * Para numero sem unidade (coluna que ja tem a moeda no cabecalho), passe `''`.
 */
export function money(value: Money | number | null | undefined, currency: string): string {
  const amount = typeof value === 'number' ? value : toNumber(value)
  const formatted = amount.toLocaleString(LOCALE_PT_BR, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  return currency ? `${formatted} ${currency}` : formatted
}

/**
 * Multiplicacao exata de dois decimais em string, sem passar por float (D7).
 *
 * Honestidade sobre o dano MEDIDO: com 0,07 x 1,1 a tela renderiza "0,08" pelos
 * dois caminhos, porque `money()` arredonda em duas casas, e o total do
 * lancamento manual e apenas exibido — nao entra no payload. Ou seja, o residuo
 * do float NAO chegou a mentir na tela; a troca aqui e conformidade com D7, nao
 * conserto de um valor errado visivel. Foi medido no navegador antes de trocar,
 * e classificar isso de "grave" seria supor dano em vez de medir.
 *
 * O que a exatidao compra de concreto e o futuro: no instante em que alguem
 * exibir mais casas, somar dois totais ou enviar este produto ao backend, o
 * residuo passa a aparecer. `BigInt` sobre os digitos nao tem residuo — o
 * produto e exato e a escala e a soma das escalas — e custa nada.
 *
 * Devolve `null` quando qualquer um dos lados nao e decimal valido — o estado
 * seguro aqui e nao mostrar total, nunca mostrar um total inventado.
 */
export function multiplyDecimal(a: string, b: string): Money | null {
  const parse = (raw: string): { digits: bigint; scale: number } | null => {
    const text = raw.trim()
    if (!/^-?\d+(\.\d+)?$/.test(text)) return null
    const [inteira, fracao = ''] = text.split('.')
    return { digits: BigInt(`${inteira}${fracao}`), scale: fracao.length }
  }

  const left = parse(a)
  const right = parse(b)
  if (!left || !right) return null

  const produto = left.digits * right.digits
  const escala = left.scale + right.scale
  if (escala === 0) return produto.toString()

  const negativo = produto < 0n
  const absoluto = (negativo ? -produto : produto).toString().padStart(escala + 1, '0')
  const corte = absoluto.length - escala
  return `${negativo ? '-' : ''}${absoluto.slice(0, corte)}.${absoluto.slice(corte)}`
}

/**
 * Moeda de cotacao de um par: `BTC/USDC` -> `USDC`.
 *
 * A moeda de UMA operacao nao e a moeda configurada hoje. O historico deste
 * sistema tem operacoes em BRL de antes da migracao para USDC, e rotular a
 * coluna inteira com a moeda vigente afirmaria que aqueles 385.275,67 sao
 * USDC. Cada linha carrega a sua propria unidade, tirada do proprio par.
 *
 * Devolve `''` para par irreconhecivel — sem unidade e melhor que unidade
 * errada.
 */
export function quoteOf(symbol: string | null | undefined): string {
  if (!symbol) return ''
  const partes = symbol.split('/')
  return partes.length === 2 && partes[1] ? partes[1].trim().toUpperCase() : ''
}

/** Quantidades de cripto precisam de mais casas: 0.00 esconderia a posicao. */
export function quantity(value: Money | number | null | undefined): string {
  const amount = typeof value === 'number' ? value : toNumber(value)
  const digits = amount !== 0 && Math.abs(amount) < 1 ? 8 : 4
  return amount.toLocaleString(LOCALE_PT_BR, {
    minimumFractionDigits: 2,
    maximumFractionDigits: digits,
  })
}

export function price(value: Money | number | null | undefined): string {
  const amount = typeof value === 'number' ? value : toNumber(value)
  return amount.toLocaleString(LOCALE_PT_BR, {
    minimumFractionDigits: 2,
    maximumFractionDigits: amount < 10 ? 6 : 2,
  })
}

export function percent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toLocaleString(LOCALE_PT_BR, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}%`
}

/** Percentual com sinal explicito: `+3,20%` le melhor que `3,20%` num card de PnL. */
export function signedPercent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return '—'
  const formatted = percent(value, digits)
  return value > 0 ? `+${formatted}` : formatted
}

export function dateTime(value: string): string {
  return new Date(value).toLocaleString(LOCALE_PT_BR, {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function timeOnly(value: string): string {
  return new Date(value).toLocaleTimeString(LOCALE_PT_BR, {
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function shortDate(value: string): string {
  return new Date(value).toLocaleDateString(LOCALE_PT_BR, { day: '2-digit', month: '2-digit' })
}

// ---------------------------------------------------------------------------
// Granularidade do eixo de tempo
// ---------------------------------------------------------------------------

/** Grandeza que distingue dois instantes de uma serie. */
export type Granularidade = 'segundo' | 'minuto' | 'dia_hora' | 'dia' | 'mes'

const MINUTO = 60 * 1000
const HORA = 60 * MINUTO
const DIA = 24 * HORA

/**
 * Granularidade tirada do INTERVALO REAL dos dados, nunca da faixa escolhida.
 *
 * Este era o defeito: o dashboard trocava "dd/mm" por "hh:mm" quando o botao
 * `24h` estava apertado (`days <= 1`), e a faixa padrao e 30d. Com a carteira
 * toda em caixa e a serie inteira dentro de um dia — o estado real de hoje —
 * todos os rotulos "dd/mm" saiam iguais, o desempate colapsava o eixo para UM
 * rotulo na borda direita, e o grafico ficava sem eixo de tempo.
 *
 * A faixa e o que se PEDE; o intervalo e o que se TEM. Quem manda no rotulo e o
 * segundo: uma serie de seis horas dentro de uma faixa de 30 dias continua uma
 * serie de seis horas.
 */
export function granularidadeDoIntervalo(spanMs: number): Granularidade {
  if (!Number.isFinite(spanMs) || spanMs <= 0) return 'minuto'
  if (spanMs <= 3 * MINUTO) return 'segundo'
  if (spanMs <= 26 * HORA) return 'minuto'
  if (spanMs <= 5 * DIA) return 'dia_hora'
  if (spanMs <= 300 * DIA) return 'dia'
  return 'mes'
}

/** Formatador de rotulo para uma granularidade. */
export function rotuloDaGranularidade(g: Granularidade): (value: string) => string {
  switch (g) {
    case 'segundo':
      return (value) =>
        new Date(value).toLocaleTimeString(LOCALE_PT_BR, {
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        })
    case 'minuto':
      return timeOnly
    case 'dia_hora':
      return (value) =>
        new Date(value).toLocaleString(LOCALE_PT_BR, {
          day: '2-digit',
          month: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
        })
    case 'dia':
      return shortDate
    case 'mes':
      return (value) =>
        new Date(value).toLocaleDateString(LOCALE_PT_BR, { month: '2-digit', year: '2-digit' })
  }
}

/**
 * Rotulador de uma serie temporal concreta.
 *
 * Passa a ser o UNICO caminho: o grafico deriva o rotulo dos proprios dados, e
 * nenhuma tela pode mais passar um formatador tirado de outro critério.
 */
export function rotuladorDeSerie(times: string[]): (value: string) => string {
  if (times.length === 0) return rotuloDaGranularidade('minuto')
  let menor = Infinity
  let maior = -Infinity
  for (const t of times) {
    const ms = Date.parse(t)
    if (!Number.isFinite(ms)) continue
    if (ms < menor) menor = ms
    if (ms > maior) maior = ms
  }
  if (!Number.isFinite(menor) || !Number.isFinite(maior)) {
    return rotuloDaGranularidade('minuto')
  }
  return rotuloDaGranularidade(granularidadeDoIntervalo(maior - menor))
}

// ---------------------------------------------------------------------------
// Fronteira de unidade da serie de patrimonio
// ---------------------------------------------------------------------------

/**
 * O que se sabe sobre a unidade em que a serie de patrimonio foi medida.
 *
 * `unica` — nenhuma troca de moeda de cotacao no log, e o log cobre o inicio da
 * serie; a unidade pode ser afirmada.
 * `trocada` — houve troca em `desde`, vindo de `anterior`; antes disso os
 * valores estao em outra unidade e NAO sao comparaveis.
 * `desconhecida` — nao se pode provar nem uma coisa nem a outra.
 */
export type FronteiraDeMoeda =
  | { tipo: 'unica' }
  | { tipo: 'trocada'; desde: string; anterior: string }
  | { tipo: 'desconhecida' }

/** Instante comparavel de um ISO-8601; `null` quando nao da para ler. */
function instante(iso: string | null | undefined): number | null {
  if (!iso) return null
  const ms = Date.parse(iso)
  return Number.isFinite(ms) ? ms : null
}

/**
 * Onde a serie de patrimonio passou a ser medida na moeda de HOJE.
 *
 * O `EquityPoint` do backend nao carrega moeda — so `timestamp` e `total_value`.
 * Medido no navegador em 10/09/2026: o tooltip afirmava "150,00 USDC" sobre um
 * ponto que valia 150 BRL, e "19,29 USDC" sobre um ponto de quando a cotacao
 * era USDT. A serie real atravessa TRES unidades.
 *
 * Gravar a moeda no snapshot e trabalho de backend. Mas AFIRMAR a unidade e
 * decisao do frontend, e afirmar errado e pior que nao afirmar (regra 3 do
 * projeto: diante de duvida, nao arrisca). O log de auditoria e append-only e
 * registra a troca com `before.quote_currency`/`after.quote_currency` — entao a
 * fronteira e derivavel aqui, sem inventar nada.
 *
 * `logCompleto` importa: nao achar troca nenhuma so prova unidade unica se o
 * log em maos comeca ANTES da serie. Log truncado devolve `desconhecida`, e a
 * tela deixa de afirmar a unidade — a degradacao aponta para o lado seguro.
 */
export function fronteiraDaMoeda(
  entradas: AuditEntry[],
  moedaAtual: string,
  inicioDaSerie: string | null,
  logCompleto: boolean,
): FronteiraDeMoeda {
  if (!moedaAtual) return { tipo: 'desconhecida' }

  let ultima: { desde: string; anterior: string; ms: number } | null = null
  let maisAntiga: number | null = null

  for (const entrada of entradas) {
    const ms = instante(entrada.timestamp)
    if (ms !== null && (maisAntiga === null || ms < maisAntiga)) maisAntiga = ms

    const de = entrada.before?.quote_currency
    const para = entrada.after?.quote_currency
    if (typeof de !== 'string' || typeof para !== 'string' || ms === null) continue

    const deNorm = de.trim().toUpperCase()
    const paraNorm = para.trim().toUpperCase()
    if (paraNorm !== moedaAtual.toUpperCase() || deNorm === paraNorm) continue
    if (ultima === null || ms > ultima.ms) ultima = { desde: entrada.timestamp, anterior: deNorm, ms }
  }

  if (ultima) return { tipo: 'trocada', desde: ultima.desde, anterior: ultima.anterior }

  const inicio = instante(inicioDaSerie)
  if (logCompleto && maisAntiga !== null && inicio !== null && maisAntiga <= inicio) {
    return { tipo: 'unica' }
  }
  return { tipo: 'desconhecida' }
}

/**
 * A comparacao de `janelaHoras` atras atravessa uma troca de moeda?
 *
 * O card "Patrimonio total" mostrava "+51,84% em 24h" em verde, medido pelo
 * backend entre 19,29 (era USDT) e 29,29 (USDC) — dois numeros de unidades
 * diferentes — ao lado de um grafico que desenhava uma queda. Percentual entre
 * unidades diferentes nao e percentual; e a razao de duas coisas distintas.
 */
export function comparacaoAtravessaTroca(
  fronteira: FronteiraDeMoeda,
  agoraMs: number,
  janelaHoras: number,
): boolean {
  if (fronteira.tipo === 'unica') return false
  if (fronteira.tipo === 'desconhecida') return true
  const desde = instante(fronteira.desde)
  if (desde === null) return true
  return desde > agoraMs - janelaHoras * HORA
}

/** Pontos da serie que estao dentro da unidade vigente. */
export function serieNaMoedaAtual<T extends { timestamp: string }>(
  pontos: T[],
  fronteira: FronteiraDeMoeda,
): T[] {
  if (fronteira.tipo !== 'trocada') return pontos
  const corte = instante(fronteira.desde)
  if (corte === null) return pontos
  return pontos.filter((ponto) => {
    const ms = instante(ponto.timestamp)
    return ms !== null && ms >= corte
  })
}

// ---------------------------------------------------------------------------
// Posicoes
// ---------------------------------------------------------------------------

/**
 * Posicoes de verdade: o caixa nao e uma delas.
 *
 * O backend devolve o saldo em moeda de cotacao dentro de `positions`, e a
 * tabela o mostrava como posicao aberta — com preco medio "—" e PnL "—", porque
 * caixa nao tem preco de entrada. No estado de hoje (zero posicoes) a tela
 * dizia que havia uma, e o "Nenhuma posicao aberta" nunca aparecia.
 *
 * Os DOIS critérios existem de proposito. Comparar com a moeda seria suficiente
 * se a moeda estivesse sempre carregada — e nao esta: `useQuoteCurrency`
 * devolve `''` enquanto `/api/trading/config` nao responde, e com `''` o filtro
 * por nome nao filtra nada e o defeito volta por falha de rede. `average_price`
 * nulo e estrutural: quem foi comprado tem preco de entrada.
 */
export function posicoesAbertas(posicoes: Position[], moeda: string): Position[] {
  return posicoes.filter(
    (posicao) =>
      posicao.average_price !== null &&
      (moeda === '' || posicao.asset.toUpperCase() !== moeda.toUpperCase()),
  )
}

// ---------------------------------------------------------------------------
// Decisoes de exibicao extraidas do JSX
// ---------------------------------------------------------------------------
//
// As tres funcoes abaixo eram ternarios dentro do JSX das paginas. Enquanto
// eram, a unica prova possivel era casar a string do codigo-fonte — e
// `/event\.decision === null \?/` passa com QUALQUER logica atras do ternario.
// Extraidas, a verificacao ve a decisao acontecer com dados reais.

/**
 * Eventos do Risk Manager que NAO sao avaliacao de sinal.
 *
 * So `signal_evaluated` traz `decision`. Os outros tres chegam com
 * `decision: null`, e a tabela renderizava `decision === 'approved' ? ... :
 * 'rejeitado'` — ou seja, afirmava "rejeitado" sobre um circuit breaker
 * disparado e sobre um REARME de circuit breaker. Medido no dashboard em
 * 09/09/2026: o evento `circuit_breaker_tripped` das 02:47 aparecia como um
 * sinal rejeitado. O log de decisoes de risco e onde se vai procurar o que o
 * sistema fez; ele nao pode inventar categoria.
 */
const RISK_EVENT_LABEL: Record<string, string> = {
  circuit_breaker_tripped: 'circuit breaker',
  circuit_breaker_reset: 'rearme',
  limits_updated: 'limites alterados',
}

/** Etiqueta de uma linha do log de decisoes de risco. */
export function rotuloDeDecisao(event: {
  decision: 'approved' | 'rejected' | null
  event_type: string
}): { texto: string; classe: string } {
  if (event.decision === null) {
    return { texto: RISK_EVENT_LABEL[event.event_type] ?? event.event_type, classe: 'badge-warning' }
  }
  if (event.decision === 'approved') return { texto: 'aprovado', classe: 'badge-positive' }
  return { texto: 'rejeitado', classe: 'badge-negative' }
}

/** Par do evento de risco, `''` quando o snapshot nao traz um. */
export function parDoEvento(event: { snapshot: Record<string, unknown> | null }): string {
  const symbol = event.snapshot?.symbol
  return typeof symbol === 'string' ? symbol : ''
}

/**
 * Unidade de um valor aprovado: vem do PAR DO EVENTO, nao da moeda de hoje.
 *
 * A aprovacao de 22,50 das 02:44 e de um par `BTC/BRL`. Rotula-la com a moeda
 * vigente afirmaria 22,50 USDC sobre um numero que sao 22,50 reais.
 */
export function unidadeDoEvento(event: { snapshot: Record<string, unknown> | null }): string {
  return quoteOf(parDoEvento(event))
}

/**
 * Rodape de um cartao de indicador: variacao com sinal, ou so a dica.
 *
 * `null` de `change` e "a API ainda nao tem essa comparacao" (7 dias num sistema
 * com 2 dias de historico), nao "variou zero" — e `undefined` e "esta tela
 * decidiu nao comparar". Os dois casos precisam cair na dica sozinha; o
 * rodape "— em 7 dias" que a tela mostrava nao informa nada.
 */
export function rodapeDoStat(
  change: number | null | undefined,
  hint?: string,
): { texto: string; tom: string } | null {
  if (change !== undefined && change !== null) {
    return {
      texto: `${signedPercent(change)}${hint ? ` ${hint}` : ''}`,
      tom: change >= 0 ? 'positive' : 'negative',
    }
  }
  return hint ? { texto: hint, tom: '' } : null
}

/**
 * Rotulo de um campo de configuracao, com a unidade quando ele e dinheiro.
 *
 * "Piso de liquidez em 24h = 9760000" e "Saldo inicial simulado = 29.29"
 * estavam sem unidade nenhuma na tela de Configuracoes — o mesmo defeito que
 * levou a por "(USDC)" nos limites de Risco, porque cinco sem unidade e cinco
 * de que? Estes dois sao medidos na moeda de cotacao vigente.
 */
export function rotuloDeCampo(
  campo: { label: string; kind: string; sufixo?: string },
  moeda: string,
): string {
  if (campo.sufixo) return `${campo.label} (${campo.sufixo})`
  if (campo.kind === 'money' && moeda) return `${campo.label} (${moeda})`
  if (campo.kind === 'percent') return `${campo.label} (0–1)`
  return campo.label
}

export function relativeTime(value: string | null): string {
  if (!value) return 'nunca'
  const seconds = Math.floor((Date.now() - new Date(value).getTime()) / 1000)
  if (seconds < 60) return `${seconds}s atras`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}min atras`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h atras`
  return `${Math.floor(seconds / 86400)}d atras`
}

export const MODE_LABEL: Record<string, string> = {
  dry_run: 'Simulação',
  testnet: 'Testnet',
  live: 'Dinheiro real',
  manual: 'Manual',
}
