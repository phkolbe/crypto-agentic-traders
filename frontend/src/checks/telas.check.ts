/**
 * Verificacoes das telas — rode com `npm run check` no diretorio `frontend`.
 *
 * Existe porque os defeitos consertados aqui passaram pelo `tsc` sem uma
 * reclamacao: eixo com oito rotulos identicos, eixo Y colapsando tres marcacoes
 * em "29", a tela anunciando USDT depois da migracao para USDC, o tooltip
 * afirmando "150,00 USDC" sobre 150 BRL, e todo o texto secundario abaixo de
 * WCAG AA. Tipo certo, tela errada.
 *
 * DUAS REGRAS deste arquivo, aprendidas na reprovacao anterior:
 *
 * 1. Verificacao que casa string de codigo-fonte prova quase nada.
 *    `/event\.decision === null \?/` passa com qualquer logica atras do
 *    ternario. Por isso as decisoes de exibicao foram extraidas para funcoes
 *    puras em `format.ts`, e aqui elas sao CHAMADAS com os dados reais medidos
 *    no sistema. Sobraram tres verificacoes de fonte, cada uma para um defeito
 *    que so existe no texto (moeda fixa em codigo, padrao de parametro,
 *    multiplicacao em float) — e essas estao marcadas.
 *
 * 2. Toda alegacao de conserto precisa de uma verificacao que veja o DEFEITO
 *    tambem. Onde da, cada bloco tem um par: "o defeito existe" (o caminho
 *    antigo erra) e "o conserto age" (o caminho novo acerta).
 *
 * Nao usa framework de teste de proposito: o unico executavel novo seria uma
 * dependencia a mais. O `esbuild` que o Vite ja traz compila este arquivo e o
 * Node roda.
 */

import {
  alteracoesDeNegocio,
  alteracoesDeRisco,
  comparacaoAtravessaTroca,
  fronteiraDaMoeda,
  granularidadeDoIntervalo,
  money,
  multiplyDecimal,
  rotuloDaPagina,
  paginaValida,
  parDoEvento,
  percent,
  posicoesAbertas,
  price,
  quantity,
  quoteOf,
  rodapeDoStat,
  rotuladorDeSerie,
  rotuloDeCampo,
  rotuloDeDecisao,
  serieNaMoedaAtual,
  taxa,
  toNumber,
  unidadeDoEvento,
} from '../api/format'
import {
  chaveDoEvento,
  descreverEvento,
  horaDoFeed,
  instanteDoEvento,
  juntarAtividade,
  type ItemDoFeed,
} from '../api/atividade'
import {
  alocacaoNoTempo,
  chaveDoGrupo,
  comparacaoComBenchmark,
  drawdown,
  retornosPorPeriodo,
} from '../api/analise'
import {
  chaveDoPeriodo,
  erroDoPersonalizado,
  lerPeriodoSalvo,
  parametrosDoPeriodo,
} from '../api/periodo'
import type { AuditEntry, Position } from '../api/types'
import {
  formularioVazio,
  lancamentoDoFormulario,
  pedidos,
  type LancamentoManual,
  type Pedido,
} from '../api/mutacoes'
import contratoDasMutacoes from './contrato-mutacoes.json'
import {
  amostragemTipica,
  casasParaPasso,
  resumoDaSerie,
  niceTicks,
  passosTemporais,
  tickIndices,
} from '../components/Charts'

/*
 * O acesso ao Node e declarado aqui, em vez de importado de `node:fs`.
 *
 * O `tsconfig` deste projeto e o do navegador e nao tem `@types/node` — e nao
 * vale trazer um pacote de tipos inteiro por causa de duas funcoes. Declarar o
 * minimo mantem este arquivo DENTRO do `tsc -b`, que e o ponto: o resto das
 * verificacoes continua sendo checado contra as assinaturas reais do codigo.
 */
declare function require(id: string): unknown
declare const process: { cwd(): string; exit(codigo: number): never }

const { readFileSync } = require('node:fs') as {
  readFileSync: (caminho: string, codificacao: string) => string
}
const { join } = require('node:path') as { join: (...partes: string[]) => string }

let falhas = 0
let verificacoes = 0

function verifica(descricao: string, condicao: boolean, detalhe = ''): void {
  verificacoes += 1
  if (condicao) {
    console.log(`  ok   ${descricao}`)
    return
  }
  falhas += 1
  console.error(`  FALHOU  ${descricao}${detalhe ? `\n          ${detalhe}` : ''}`)
}

function bloco(titulo: string): void {
  console.log(`\n${titulo}`)
}

/** Le um arquivo de `src/` como texto, com fim de linha normalizado. */
function fonte(...partes: string[]): string {
  return readFileSync(join(process.cwd(), 'src', ...partes), 'utf8').replace(/\r\n/g, '\n')
}

/**
 * Mesmo arquivo, sem comentarios.
 *
 * As verificacoes de moeda fixa em codigo procuram literais como `'USDT'`, e um
 * comentario que EXPLICA o defeito antigo cita exatamente esse literal. Sem
 * remover comentario, documentar o conserto reprova o conserto.
 */
function fonteSemComentarios(...partes: string[]): string {
  return fonte(...partes)
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/^[ \t]*\/\/.*$/gm, ' ')
    .replace(/([^:])\/\/.*$/gm, '$1')
}

// ===========================================================================
bloco('Moeda vem da configuracao, nunca de um padrao no codigo')

verifica('moeda explicita aparece no texto', money('29.29', 'USDC') === '29,29 USDC', money('29.29', 'USDC'))
verifica('string vazia = numero sem unidade', money('29.29', '') === '29,29', money('29.29', ''))
verifica(
  'separador decimal pt-BR, com milhar',
  money('1234.5', 'USDC') === '1.234,50 USDC',
  money('1234.5', 'USDC'),
)

// [VERIFICACAO DE FONTE] Um padrao de parametro nao muda comportamento
// nenhum quando o argumento e passado — so quando alguem ESQUECE. Nao existe
// dado de entrada que exponha isso; o defeito e textual.
const fonteFormat = fonte('api', 'format.ts')
verifica(
  'format.ts nao tem moeda como padrao de parametro',
  !/currency\s*=\s*['"]/.test(fonteFormat),
  'um padrao aqui faz a tela afirmar a moeda errada em silencio quando alguem esquece o argumento',
)
verifica(
  'nenhuma constante de locale disfarcada de moeda',
  !/BRL_LOCALE/.test(fonteFormat),
  'pt-BR e formato de numero; a moeda e outro conceito',
)

// [VERIFICACAO DE FONTE] idem: par fixo em codigo.
const paginas = [
  'Dashboard.tsx',
  'Portfolio.tsx',
  'Trades.tsx',
  'ManualTrade.tsx',
  'RiskSettings.tsx',
  'Configuration.tsx',
  'Agents.tsx',
  'Notifications.tsx',
  'Backtest.tsx',
]
for (const pagina of paginas) {
  const codigo = fonteSemComentarios('pages', pagina)
  // 'USDC' tambem entra na proibicao: hoje esta certo e amanha e a proxima
  // migracao. O unico lugar onde um codigo de moeda pode aparecer e como
  // exemplo em placeholder derivado da configuracao.
  const codificadas = codigo.match(/['"`][A-Z]{3,5}\/(USDT|USDC|BRL)['"`]|['"`](USDT|BRL)['"`]/g)
  verifica(
    `${pagina} nao fixa par nem moeda em codigo`,
    codificadas === null,
    codificadas ? `encontrado: ${codificadas.join(', ')}` : '',
  )
}

verifica('quoteOf le a cotacao do par', quoteOf('BTC/USDC') === 'USDC')
verifica('quoteOf reconhece o historico em BRL', quoteOf('BTC/BRL') === 'BRL')
verifica('quoteOf normaliza caixa e espaco', quoteOf(' eth/usdc ') === 'USDC')
verifica('quoteOf recusa par sem cotacao', quoteOf('BTCUSDC') === '')
verifica('quoteOf recusa nulo', quoteOf(null) === '')
verifica('quoteOf recusa cotacao vazia', quoteOf('BTC/') === '')

// ===========================================================================
bloco('Fronteira de moeda na serie de patrimonio (o tooltip que mentia)')

/**
 * Log de auditoria REAL, lido de `GET /api/audit?limit=50` em 10/09/2026.
 *
 * A serie de patrimonio atravessa TRES unidades: USDT ate 08/09 22:36, BRL ate
 * 09/09 19:06, USDC dali em diante. Medido no navegador antes do conserto, o
 * tooltip do grafico afirmava "150,00 USDC" sobre um ponto que valia 150 BRL e
 * "19,29 USDC" sobre um ponto da epoca do USDT.
 */
const AUDIT_REAL: AuditEntry[] = [
  {
    id: 16,
    timestamp: '2026-09-09T19:11:47.810128',
    actor: 'system',
    action: 'system_started',
    target: 'orchestrator',
    detail: null,
    before: {},
    after: { mode: 'dry_run', exchange: 'binance' },
  },
  {
    id: 15,
    timestamp: '2026-09-09T19:06:50.344139',
    actor: 'user',
    action: 'quote_currency_migrated',
    target: 'trading_config,risk_config',
    detail: 'migracao BRL->USDC a 5.1218 USDC/BRL',
    before: { quote_currency: 'BRL', min_order_notional: '15' },
    after: { quote_currency: 'USDC', min_order_notional: '5' },
  },
  {
    id: 9,
    timestamp: '2026-09-09T02:46:39.262380',
    actor: 'user',
    action: 'trading_config_updated',
    target: 'trading_config',
    detail: null,
    before: { paper_initial_balance: '1000' },
    after: { paper_initial_balance: '150' },
  },
  {
    id: 4,
    timestamp: '2026-09-08T22:36:56.660180',
    actor: 'user',
    action: 'trading_config_updated',
    target: 'trading_config',
    detail: null,
    before: { quote_currency: 'USDT', timeframe: '15m' },
    after: { quote_currency: 'BRL', timeframe: '1d' },
  },
  {
    id: 1,
    timestamp: '2026-09-07T20:44:11.055583',
    actor: 'system',
    action: 'system_started',
    target: 'orchestrator',
    detail: null,
    before: {},
    after: { mode: 'dry_run' },
  },
]

/** Amostra da serie real de `GET /api/portfolio/history?days=30`. */
const SERIE_REAL = [
  { timestamp: '2026-09-07T20:44:11.047558', total_value: '19.29' }, // era USDT
  { timestamp: '2026-09-07T20:50:11.501744', total_value: '19.29' }, // era USDT
  { timestamp: '2026-09-09T02:40:25.243537', total_value: '1000' }, // era BRL
  { timestamp: '2026-09-09T02:44:09.427515', total_value: '999.0663748483662630568192' },
  { timestamp: '2026-09-09T02:47:04.753215', total_value: '150' }, // o "150,00 USDC" medido
  { timestamp: '2026-09-09T03:03:46.332079', total_value: '150' },
  { timestamp: '2026-09-09T19:11:47.806870', total_value: '29.29' }, // USDC de verdade
  { timestamp: '2026-09-10T01:42:56.747948', total_value: '29.29' },
]

const fronteira = fronteiraDaMoeda(AUDIT_REAL, 'USDC', SERIE_REAL[0]!.timestamp, true)
verifica(
  'a troca de moeda e localizada no log de auditoria',
  fronteira.tipo === 'trocada' && fronteira.desde === '2026-09-09T19:06:50.344139',
  JSON.stringify(fronteira),
)
verifica(
  'a moeda anterior e a que o log registra, nao um chute',
  fronteira.tipo === 'trocada' && fronteira.anterior === 'BRL',
  JSON.stringify(fronteira),
)

const cortada = serieNaMoedaAtual(SERIE_REAL, fronteira)
verifica(
  'a serie exibida perde TODO ponto anterior a troca',
  cortada.length === 2 && cortada.every((p) => p.timestamp >= '2026-09-09T19:06:50'),
  JSON.stringify(cortada.map((p) => p.timestamp)),
)
verifica(
  'o ponto que produziu o "150,00 USDC" medido nao esta mais na serie',
  !cortada.some((p) => p.total_value === '150'),
  JSON.stringify(cortada),
)
verifica(
  'o ponto de 19,29 da epoca do USDT tambem sai',
  !cortada.some((p) => p.total_value === '19.29'),
  JSON.stringify(cortada),
)
verifica(
  'o defeito existe: sem corte, a serie rotulada em USDC afirmaria 150 e 1000',
  SERIE_REAL.some((p) => p.total_value === '150') && SERIE_REAL.length - cortada.length === 6,
  `${SERIE_REAL.length - cortada.length} pontos cortados`,
)

// Log truncado ou ausente: nao se pode PROVAR unidade nenhuma, e ai a tela nao
// afirma unidade. Degradacao para o lado seguro (regra 3 do projeto).
verifica(
  'sem log de auditoria, a unidade fica desconhecida',
  fronteiraDaMoeda([], 'USDC', SERIE_REAL[0]!.timestamp, true).tipo === 'desconhecida',
)
verifica(
  'log truncado (cheio ate o limite) tambem devolve desconhecida',
  fronteiraDaMoeda(
    // As entradas antigas, SEM a migracao para USDC — e o log veio cheio, ou
    // seja, pode haver mais coisa que nao chegou.
    AUDIT_REAL.filter((e) => e.id <= 9),
    'USDC',
    SERIE_REAL[0]!.timestamp,
    false,
  ).tipo === 'desconhecida',
  'nao achar troca num log truncado nao prova que nao houve troca',
)
verifica(
  'sem moeda carregada, a unidade fica desconhecida',
  fronteiraDaMoeda(AUDIT_REAL, '', SERIE_REAL[0]!.timestamp, true).tipo === 'desconhecida',
)
verifica(
  'log completo que cobre o inicio e sem troca nenhuma: unidade unica',
  fronteiraDaMoeda(
    [AUDIT_REAL[4]!, AUDIT_REAL[2]!],
    'USDC',
    '2026-09-08T00:00:00',
    true,
  ).tipo === 'unica',
)
verifica(
  'log completo que NAO cobre o inicio da serie nao afirma unidade unica',
  fronteiraDaMoeda([AUDIT_REAL[0]!], 'USDC', '2026-09-01T00:00:00', true).tipo === 'desconhecida',
)
verifica(
  'troca para OUTRA moeda que nao a de hoje nao conta como fronteira',
  fronteiraDaMoeda([AUDIT_REAL[3]!], 'USDC', '2026-09-01T00:00:00', true).tipo === 'desconhecida',
  'a entrada 4 e USDT->BRL; ela nao diz nada sobre quando o USDC comecou',
)

// O "+51,84% em 24h" verde: o backend comparou 19,29 (USDT) com 29,29 (USDC).
const AGORA_MEDIDO = Date.parse('2026-09-10T01:48:56')
verifica(
  'a variacao de 24h e suprimida quando a janela atravessa a troca',
  comparacaoAtravessaTroca(fronteira, AGORA_MEDIDO, 24),
  'a troca foi 6h antes da medicao; comparar 24h atras cruza BRL e USDC',
)
verifica(
  'e volta a aparecer quando a janela inteira esta na moeda de hoje',
  !comparacaoAtravessaTroca(fronteira, Date.parse('2026-09-11T00:00:00'), 24),
)
verifica(
  'unidade unica nunca suprime a comparacao',
  !comparacaoAtravessaTroca({ tipo: 'unica' }, AGORA_MEDIDO, 24),
)
verifica(
  'unidade desconhecida sempre suprime a comparacao',
  comparacaoAtravessaTroca({ tipo: 'desconhecida' }, AGORA_MEDIDO, 24),
)
verifica(
  'o rodape do card fica sem percentual quando a comparacao e suprimida',
  rodapeDoStat(undefined, 'sem 24h comparavel')?.texto === 'sem 24h comparavel' &&
    rodapeDoStat(undefined, 'sem 24h comparavel')?.tom === '',
  JSON.stringify(rodapeDoStat(undefined, 'sem 24h comparavel')),
)
verifica(
  'o defeito existe: com o percentual do backend o card sairia verde',
  rodapeDoStat(0.5184033177812338, 'em 24h')?.tom === 'positive' &&
    rodapeDoStat(0.5184033177812338, 'em 24h')?.texto === '+51,84% em 24h',
  JSON.stringify(rodapeDoStat(0.5184033177812338, 'em 24h')),
)

// ===========================================================================
bloco('Eixo X: granularidade sai do intervalo dos DADOS, nao da faixa')

verifica(
  'serie de 6,6 h (a carteira de hoje) e rotulada por hora e minuto',
  granularidadeDoIntervalo(6.6 * 3600e3) === 'minuto',
)
verifica('serie de 2 min ganha segundos', granularidadeDoIntervalo(2 * 60e3) === 'segundo')
verifica('serie de 3 dias ganha dia e hora', granularidadeDoIntervalo(3 * 86400e3) === 'dia_hora')
verifica('serie de 90 dias (o backtest) e rotulada por dia', granularidadeDoIntervalo(90 * 86400e3) === 'dia')
verifica('serie de 400 dias vira mes e ano', granularidadeDoIntervalo(400 * 86400e3) === 'mes')
verifica('intervalo zero nao quebra', granularidadeDoIntervalo(0) === 'minuto')

/**
 * A CARTEIRA PLANA REAL: 404 pontos de um minuto cobrindo um unico dia.
 *
 * Este e o estado que o dono vai olhar pelos proximos dias, e era onde o eixo
 * degenerava. Medido no componente real antes do conserto: `eixoX = ['09/09']`
 * — UM rotulo, encostado na borda direita, num grafico sem eixo de tempo.
 */
const inicioPlano = Date.parse('2026-09-09T19:11:00')
const instantesPlanos = Array.from({ length: 404 }, (_, i) => inicioPlano + i * 60e3)
const isoPlanos = instantesPlanos.map((ms) => new Date(ms).toISOString())
const larguraReal = 469.5 // 537,5 px do card menos as margens do grafico

// O defeito: rotular por dia uma serie que cabe num dia.
const rotulosPorDia = isoPlanos.map(() => '09/09')
verifica(
  'o defeito existe: rotulos "dd/mm" numa serie de um dia colapsam o eixo',
  tickIndices(rotulosPorDia, larguraReal, 40, instantesPlanos).length === 1,
  JSON.stringify(tickIndices(rotulosPorDia, larguraReal, 40, instantesPlanos)),
)

// O conserto: o rotulador sai da propria serie.
const rotularPlano = rotuladorDeSerie(isoPlanos)
const rotulosPlanos = isoPlanos.map(rotularPlano)
const ticksPlanos = tickIndices(rotulosPlanos, larguraReal, 40, instantesPlanos)
const textosPlanos = ticksPlanos.map((i) => rotulosPlanos[i]!)
verifica(
  'a serie plana de um dia produz VARIAS marcacoes, nao uma',
  ticksPlanos.length >= 4,
  JSON.stringify(textosPlanos),
)
verifica(
  'e todos os rotulos sao textos DIFERENTES',
  new Set(textosPlanos).size === textosPlanos.length,
  JSON.stringify(textosPlanos),
)
verifica(
  'os rotulos dizem hora e minuto, porque a serie cabe num dia',
  textosPlanos.every((t) => /^\d{2}:\d{2}$/.test(t)),
  JSON.stringify(textosPlanos),
)
verifica(
  'o ultimo ponto continua ancorando a serie',
  ticksPlanos[ticksPlanos.length - 1] === isoPlanos.length - 1,
)

// ===========================================================================
bloco('Eixo X: passo uniforme e em grade de tempo redonda')

/** Passos entre marcacoes consecutivas. */
function passosDe(indices: number[]): number[] {
  return indices.slice(1).map((indice, i) => indice - indices[i]!)
}

verifica(
  'a grade da serie plana tem passo constante',
  new Set(passosDe(ticksPlanos)).size === 1,
  JSON.stringify(passosDe(ticksPlanos)),
)

/** O backtest medido: 90 dias em candles de 1 h, 2.160 pontos. */
const inicioBacktest = Date.parse('2026-06-11T00:00:00')
const instantesBacktest = Array.from({ length: 2160 }, (_, i) => inicioBacktest + i * 3600e3)
const isoBacktest = instantesBacktest.map((ms) => new Date(ms).toISOString())
const rotularBacktest = rotuladorDeSerie(isoBacktest)
const rotulosBacktest = isoBacktest.map(rotularBacktest)
const ticksBacktest = tickIndices(rotulosBacktest, 700, 50, instantesBacktest)

verifica(
  'o backtest de 90d/1h tem passo de indice constante',
  new Set(passosDe(ticksBacktest)).size === 1,
  JSON.stringify(passosDe(ticksBacktest)),
)
verifica(
  'o defeito existe: o passo medido saia 18/08, 19/08, 21/08 — 1 dia num ritmo de 2',
  // O passo em DIAS entre rotulos consecutivos tem que ser sempre o mesmo. Era
  // o que quebrava: `semRotulosRepetidos` removia um indice e os sobreviventes
  // mantinham a posicao original.
  new Set(
    ticksBacktest
      .slice(1)
      .map((indice, i) => Math.round((instantesBacktest[indice]! - instantesBacktest[ticksBacktest[i]!]!) / 86400e3)),
  ).size === 1,
  JSON.stringify(ticksBacktest.map((i) => rotulosBacktest[i])),
)
verifica(
  'e o passo cai numa unidade de tempo redonda (multiplo de dia)',
  (instantesBacktest[ticksBacktest[1]!]! - instantesBacktest[ticksBacktest[0]!]!) % 86400e3 === 0,
  `${(instantesBacktest[ticksBacktest[1]!]! - instantesBacktest[ticksBacktest[0]!]!) / 3600e3} h`,
)

verifica(
  'passosTemporais so oferece passos redondos, do menor aceitavel para cima',
  JSON.stringify(passosTemporais(51, instantesPlanos, 403)) === JSON.stringify([60, 120, 180, 360]),
  JSON.stringify(passosTemporais(51, instantesPlanos, 403)),
)
verifica(
  'serie de um unico instante nao gera passo nenhum',
  passosTemporais(1, [1, 1, 1], 2).length === 0,
)

// A cadencia real do Portfolio Agent: um ponto por minuto, com retratos
// duplicados a milissegundos de distancia quando o sistema sobe.
// Retrato duplicado na subida (medido: 19:11:47.806 e 19:11:47.810), depois um
// ponto por minuto.
const comDuplicados = [0, 4, 60_004, 120_004, 180_004]
verifica(
  'a cadencia e a mediana dos intervalos de verdade, nao a media com duplicados',
  amostragemTipica(comDuplicados) === 60_000,
  String(amostragemTipica(comDuplicados)),
)
verifica(
  'o defeito existe: a media dessa serie erra a cadencia em 25%',
  Math.round(comDuplicados[comDuplicados.length - 1]! / (comDuplicados.length - 1)) === 45_001,
  `media 45001 ms contra cadencia real de ${amostragemTipica(comDuplicados)} ms`,
)
verifica(
  'serie com TODOS os intervalos sub-segundo usa a propria cadencia',
  amostragemTipica([0, 100, 200, 300]) === 100,
)
// Um retrato duplicado por minuto: metade dos intervalos e sub-segundo, e sem
// descarta-los a mediana desaba para 4 ms e o eixo perde a nocao de escala.
const duplicadoTodoMinuto = [0, 4, 60_004, 60_008, 120_008, 120_012, 180_012, 180_016]
verifica(
  'metade dos intervalos sub-segundo nao derruba a cadencia',
  amostragemTipica(duplicadoTodoMinuto) === 60_000,
  String(amostragemTipica(duplicadoTodoMinuto)),
)
verifica(
  'o defeito existe: a mediana crua dessa serie daria 4 ms',
  (() => {
    const d: number[] = []
    for (let i = 1; i < duplicadoTodoMinuto.length; i += 1) {
      d.push(duplicadoTodoMinuto[i]! - duplicadoTodoMinuto[i - 1]!)
    }
    d.sort((a, b) => a - b)
    return d[Math.floor(d.length / 2)] === 4
  })(),
)

// ===========================================================================
bloco('Eixo X: folga real entre CAIXAS de rotulo, com as ancoras do componente')

const LARGURA_CARACTERE = 6.1
const FOLGA_MINIMA = 12

/**
 * Reproduz a caixa de cada rotulo como o componente a desenha.
 *
 * A medicao que reprovou a rodada anterior media o espacamento entre TICKS, nao
 * a folga entre ROTULOS — grandeza errada, que subestimava o aperto em ~6x. O
 * primeiro tick usa `text-anchor="start"` e o ultimo `"end"`, e essas caixas
 * ficam inteiras de um lado do tick.
 */
function caixasDeRotulo(indices: number[], rotulos: string[], plotW: number) {
  const total = rotulos.length
  return indices.map((i) => {
    const x = total === 1 ? plotW / 2 : (i / (total - 1)) * plotW
    const largura = rotulos[i]!.length * LARGURA_CARACTERE
    const esquerda = i === 0 ? x : i === total - 1 ? x - largura : x - largura / 2
    return { texto: rotulos[i]!, esquerda, direita: esquerda + largura }
  })
}

function folgasEntreRotulos(indices: number[], rotulos: string[], plotW: number): number[] {
  const caixas = caixasDeRotulo(indices, rotulos, plotW)
  return caixas.slice(1).map((caixa, i) => caixa.esquerda - caixas[i]!.direita)
}

for (const caso of [
  { nome: 'carteira plana de hoje (hh:mm)', indices: ticksPlanos, rotulos: rotulosPlanos, w: larguraReal },
  { nome: 'backtest 90d/1h (dd/mm)', indices: ticksBacktest, rotulos: rotulosBacktest, w: 700 },
]) {
  const folgas = folgasEntreRotulos(caso.indices, caso.rotulos, caso.w)
  const minima = Math.min(...folgas)
  verifica(
    `${caso.nome}: nenhuma folga abaixo de ${FOLGA_MINIMA} px`,
    minima >= FOLGA_MINIMA,
    `folgas: ${folgas.map((f) => f.toFixed(1)).join(', ')}`,
  )
  verifica(
    `${caso.nome}: a folga menor nao e uma fracao das outras`,
    minima >= Math.max(...folgas) * 0.5,
    `min ${minima.toFixed(1)} px contra max ${Math.max(...folgas).toFixed(1)} px`,
  )
}

// O defeito medido: `maisLargo + 14` supoe rotulo centrado, e o primeiro tick
// nao e. Com dd/mm (30,5 px) a folga contra o vizinho caia para 6,9 px.
const gapAntigo = 5 * LARGURA_CARACTERE + 14
verifica(
  'o defeito existe: a folga antiga contra um rotulo ancorado no inicio dava ~7 px',
  Math.abs(gapAntigo - 1.5 * (5 * LARGURA_CARACTERE)) < 8,
  `passo ${gapAntigo.toFixed(1)} px menos 1,5 x 30,5 px = ${(gapAntigo - 45.75).toFixed(1)} px de folga`,
)

// Series com rotulos todos distintos nao devem perder marcacao nenhuma.
const distintos = Array.from({ length: 30 }, (_, i) => `${String(i + 1).padStart(2, '0')}/09`)
const indicesDistintos = tickIndices(distintos, 600, 40)
verifica(
  'serie com rotulos distintos mantem varias marcacoes',
  indicesDistintos.length >= 5,
  JSON.stringify(indicesDistintos.map((i) => distintos[i])),
)
verifica(
  'e ainda respeita o espacamento minimo',
  Math.min(...folgasEntreRotulos(indicesDistintos, distintos, 600)) >= FOLGA_MINIMA,
  JSON.stringify(folgasEntreRotulos(indicesDistintos, distintos, 600).map((f) => f.toFixed(1))),
)

// Casos degenerados: um ponto so, e serie inteira num mesmo instante.
verifica('um ponto so devolve uma marcacao', tickIndices(['09/09'], 400, 40).length === 1)
verifica(
  'serie inteira no mesmo instante devolve uma marcacao, nao duas iguais',
  tickIndices(['09/09', '09/09'], 400, 40).length === 1,
  'duas marcacoes com o mesmo texto nao medem nada',
)
verifica('serie vazia nao gera marcacao', tickIndices([], 400, 40).length === 0)

// ===========================================================================
bloco('Eixo Y: a carteira de hoje, 29,29 em caixa e curva plana')

/** Reproduz a folga que o grafico aplica a uma serie constante. */
function eixoDeSerieConstante(valor: number): { ticks: number[]; rotulos: string[] } {
  const folga = Math.abs(valor) * 0.01 || 1
  const min = valor - folga
  const max = valor + folga
  const ticks = niceTicks(min, max, 5)
  const passo = ticks.length > 1 ? Math.abs(ticks[1]! - ticks[0]!) : Math.abs(max - min)
  const casas = casasParaPasso(passo)
  const rotulos = ticks.map((t) =>
    t.toLocaleString('pt-BR', { minimumFractionDigits: casas, maximumFractionDigits: casas }),
  )
  return { ticks, rotulos }
}

const eixoHoje = eixoDeSerieConstante(29.29)
verifica(
  'toFixed(0) realmente colapsaria este eixo (o defeito existe)',
  new Set(eixoHoje.ticks.map((t) => t.toFixed(0))).size < eixoHoje.ticks.length,
  `com toFixed(0): ${JSON.stringify(eixoHoje.ticks.map((t) => t.toFixed(0)))}`,
)
verifica(
  'rotulos do eixo sao todos distintos',
  new Set(eixoHoje.rotulos).size === eixoHoje.rotulos.length,
  JSON.stringify(eixoHoje.rotulos),
)
verifica(
  'rotulos usam virgula decimal, como o resto da tela',
  eixoHoje.rotulos.every((r) => !r.includes('.') || r.includes(',')),
  JSON.stringify(eixoHoje.rotulos),
)

const eixoGrande = niceTicks(180, 1010, 5)
const passoGrande = Math.abs(eixoGrande[1]! - eixoGrande[0]!)
verifica(
  'eixo de centenas nao ganha decimais',
  casasParaPasso(passoGrande) === 0,
  `passo ${passoGrande} -> ${casasParaPasso(passoGrande)} casas`,
)
verifica('passo pequeno ganha casa', casasParaPasso(0.2) === 1, String(casasParaPasso(0.2)))
verifica('passo minusculo ganha mais casas', casasParaPasso(0.0005) === 4, String(casasParaPasso(0.0005)))
verifica('passo invalido nao gera NaN de casas', casasParaPasso(0) === 2)

const ticksApertados = niceTicks(1000, 1150, 4)
verifica(
  'niceTicks respeita o alvo de marcacoes',
  ticksApertados.length <= 6,
  JSON.stringify(ticksApertados),
)

// ===========================================================================
bloco('Estado vazio: o caixa nao e posicao, e a falha de rede nao o traz de volta')

/** Payload real de `GET /api/portfolio` em 10/09/2026: carteira toda em caixa. */
const CAIXA_USDC: Position = {
  asset: 'USDC',
  quantity: '29.29',
  average_price: null,
  current_price: '1',
  market_value: '29.29',
  unrealized_pnl: '0',
}
const POSICAO_REAL: Position = {
  asset: 'BTC',
  quantity: '0.00005611',
  average_price: '401128.46',
  current_price: '385275.67',
  market_value: '21.62',
  unrealized_pnl: '-0.89',
}

verifica(
  'o caixa em USDC nao aparece como posicao aberta',
  posicoesAbertas([CAIXA_USDC], 'USDC').length === 0,
)
verifica(
  'e continua fora quando a configuracao NAO carregou (moeda vazia)',
  posicoesAbertas([CAIXA_USDC], '').length === 0,
  'era por aqui que uma falha em /api/trading/config ressuscitava o defeito',
)
verifica(
  'o defeito existe: filtrar so por nome com moeda vazia nao filtra nada',
  [CAIXA_USDC].filter((p) => p.asset !== '').length === 1,
)
verifica(
  'posicao de verdade continua na tabela',
  posicoesAbertas([CAIXA_USDC, POSICAO_REAL], 'USDC').length === 1 &&
    posicoesAbertas([CAIXA_USDC, POSICAO_REAL], 'USDC')[0]!.asset === 'BTC',
)
verifica(
  'e continua na tabela mesmo sem a moeda carregada',
  posicoesAbertas([CAIXA_USDC, POSICAO_REAL], '').length === 1,
)
verifica('carteira vazia devolve lista vazia', posicoesAbertas([], 'USDC').length === 0)

verifica('percent de nulo e travessao', percent(null) === '—')
// Estas duas exigiam "0,00 USDC" para nulo: era o defeito (telas.md, low
// "toNumber transforma null em 0") — um zero que ninguem mediu, com unidade.
verifica('money de nulo e travessao', money(null, 'USDC') === '—', money(null, 'USDC'))
verifica('money de string vazia e travessao', money('', 'USDC') === '—', money('', 'USDC'))
verifica(
  'price e quantity de nulo ou lixo sao travessao',
  price(null) === '—' && quantity(undefined) === '—' && price('abc' as never) === '—',
)
verifica('zero de verdade continua zero', money('0', 'USDC') === '0,00 USDC' && quantity('0') === '0,00')
verifica('toNumber trata nulo como zero, sem NaN na tela', toNumber(null) === 0)
verifica('toNumber trata texto invalido como zero', toNumber('abc' as never) === 0)

// Rodape do cartao de indicador: null, undefined e numero sao tres casos.
verifica(
  'change nulo cai na dica sozinha (a API nao tem essa comparacao)',
  rodapeDoStat(null, 'nenhuma desde a meia-noite')?.texto === 'nenhuma desde a meia-noite',
)
verifica(
  'o defeito existe: tratar nulo como numero renderizaria "— em 7 dias"',
  percent(null) === '—',
)
verifica('sem change e sem dica, nao existe rodape', rodapeDoStat(undefined, undefined) === null)
verifica('change negativo pinta de vermelho', rodapeDoStat(-0.031, 'em 24h')?.tom === 'negative')
verifica('change zero conta como nao-negativo', rodapeDoStat(0, 'em 24h')?.tom === 'positive')

// ===========================================================================
bloco('Log de decisoes de risco nao inventa categoria nem unidade')

// Eventos reais medidos em 09/09/2026: um circuit breaker das 02:47 (decision
// null) e uma aprovacao das 02:44 num par BTC/BRL.
verifica(
  'circuit breaker disparado NAO e rotulado como sinal rejeitado',
  rotuloDeDecisao({ decision: null, event_type: 'circuit_breaker_tripped' }).texto ===
    'circuit breaker',
  JSON.stringify(rotuloDeDecisao({ decision: null, event_type: 'circuit_breaker_tripped' })),
)
verifica(
  'rearme de circuit breaker tem rotulo proprio',
  rotuloDeDecisao({ decision: null, event_type: 'circuit_breaker_reset' }).texto === 'rearme',
)
verifica(
  'alteracao de limites tem rotulo proprio',
  rotuloDeDecisao({ decision: null, event_type: 'limits_updated' }).texto === 'limites alterados',
)
verifica(
  'evento sem decisao e sem rotulo conhecido mostra o tipo cru, nao "rejeitado"',
  rotuloDeDecisao({ decision: null, event_type: 'coisa_nova' }).texto === 'coisa_nova',
)
verifica(
  'os tres eventos sem decisao usam a etiqueta de aviso, nao a de rejeicao',
  ['circuit_breaker_tripped', 'circuit_breaker_reset', 'limits_updated'].every(
    (tipo) => rotuloDeDecisao({ decision: null, event_type: tipo }).classe === 'badge-warning',
  ),
)
verifica(
  'aprovacao e rejeicao continuam distintas e destacadas',
  rotuloDeDecisao({ decision: 'approved', event_type: 'signal_evaluated' }).classe ===
    'badge-positive' &&
    rotuloDeDecisao({ decision: 'rejected', event_type: 'signal_evaluated' }).classe ===
      'badge-negative' &&
    rotuloDeDecisao({ decision: 'rejected', event_type: 'signal_evaluated' }).texto === 'rejeitado',
)

verifica(
  'a unidade do valor aprovado vem do par do proprio evento',
  unidadeDoEvento({ snapshot: { symbol: 'BTC/BRL' } }) === 'BRL',
  'os 22,50 aprovados as 02:44 sao reais, nao USDC',
)
verifica(
  'o valor aprovado sai com a unidade do evento',
  money('22.50', unidadeDoEvento({ snapshot: { symbol: 'BTC/BRL' } })) === '22,50 BRL',
)
verifica(
  'evento sem par no snapshot nao ganha unidade inventada',
  unidadeDoEvento({ snapshot: {} }) === '' && money('22.50', unidadeDoEvento({ snapshot: {} })) === '22,50',
)
// A mesma regra vale para a tabela de Negociacoes, que mistura pares em BRL e
// em USDC. [VERIFICACAO DE FONTE]: a unidade e escolhida no JSX de cada celula,
// e o defeito e passar a variavel errada — nao ha entrada que o exponha.
const fonteTrades = fonteSemComentarios('pages', 'Trades.tsx')
verifica(
  'nenhuma celula de Negociacoes e rotulada com a moeda de HOJE',
  !/money\([^)]*,\s*moeda\s*\)/.test(fonteTrades),
  'rotular a coluna inteira com a moeda vigente mente sobre as linhas em BRL',
)
verifica(
  'as DUAS colunas de dinheiro usam a cotacao do par da propria linha',
  (fonteTrades.match(/quoteOf\(trade\.symbol\)/g) ?? []).length >= 2,
  'preco unitario tambem: sem unidade em lugar nenhum era o defeito medido',
)
verifica(
  'a unidade de uma linha BTC/BRL e BRL, e o total sai com ela',
  money('21.62', quoteOf('BTC/BRL')) === '21,62 BRL',
)

verifica('snapshot nulo nao quebra', parDoEvento({ snapshot: null }) === '')
verifica(
  'snapshot com symbol que nao e texto nao quebra',
  parDoEvento({ snapshot: { symbol: 42 } }) === '',
)

// ===========================================================================
bloco('Campo de dinheiro sem unidade e numero que nao se confere')

verifica(
  'piso de liquidez ganha a moeda de cotacao',
  rotuloDeCampo({ label: 'Piso de liquidez em 24h', kind: 'money' }, 'USDC') ===
    'Piso de liquidez em 24h (USDC)',
  rotuloDeCampo({ label: 'Piso de liquidez em 24h', kind: 'money' }, 'USDC'),
)
verifica(
  'saldo inicial simulado ganha a moeda de cotacao',
  rotuloDeCampo({ label: 'Saldo inicial simulado', kind: 'money' }, 'USDC') ===
    'Saldo inicial simulado (USDC)',
)
verifica(
  'o defeito existe: sem a regra, os dois campos sairiam sem unidade',
  rotuloDeCampo({ label: 'Piso de liquidez em 24h', kind: 'text' }, 'USDC') ===
    'Piso de liquidez em 24h',
)
verifica(
  'sem moeda carregada, o campo fica sem unidade em vez de inventar uma',
  rotuloDeCampo({ label: 'Saldo inicial simulado', kind: 'money' }, '') ===
    'Saldo inicial simulado',
)
verifica(
  'percentual mostra a faixa, nao uma moeda',
  rotuloDeCampo({ label: 'Taxa simulada', kind: 'percent' }, 'USDC') === 'Taxa simulada (0–1)',
)
verifica(
  'sufixo explicito manda (segundos, candles, horas)',
  rotuloDeCampo({ label: 'Intervalo de coleta', kind: 'int', sufixo: 's' }, 'USDC') ===
    'Intervalo de coleta (s)',
)

// TODO campo `money` das telas de configuracao tem que passar por aqui: a
// verificacao anterior ("Configuration.tsx nao fixa moeda em codigo") passava
// vazia, porque o arquivo simplesmente nunca menciona moeda.
const fonteConfig = fonte('pages', 'Configuration.tsx')
verifica(
  'Configuration.tsx monta o rotulo pela funcao verificada, nao a mao',
  /rotuloDeCampo\(campo,/.test(fonteConfig),
  'sem isso, um campo novo de dinheiro volta a sair sem unidade',
)
verifica(
  'os dois campos de dinheiro da tela existem e estao declarados como money',
  /key: 'discovery_min_quote_volume_24h'[\s\S]{0,240}kind: 'money'/.test(fonteConfig) &&
    /key: 'paper_initial_balance'[\s\S]{0,240}kind: 'money'/.test(fonteConfig),
)

// ===========================================================================
bloco('Dinheiro nao passa por float em conta nenhuma (D7)')

// GRAVIDADE MEDIDA, nao suposta: com 0,07 x 1,1 a tela renderiza "0,08" pelos
// DOIS caminhos, porque `money` arredonda em duas casas, e o total do
// lancamento manual nao entra no payload. A troca e conformidade com D7 —
// nao conserto de valor errado visivel.
const totalFloat = Number('0.07') * Number('1.1')
verifica(
  'float erra o produto (o residuo existe)',
  totalFloat !== 0.077,
  `Number('0.07') * Number('1.1') = ${totalFloat}`,
)
verifica(
  'e a tela NAO mudava por causa disso: money arredonda em 2 casas',
  money(String(totalFloat), 'BRL') === money('0.077', 'BRL'),
  `float: ${money(String(totalFloat), 'BRL')} | exato: ${money('0.077', 'BRL')}`,
)
verifica(
  'o residuo esta no NUMERO, so nao chegava a tela',
  String(totalFloat) === '0.07700000000000001' && String(0.077) === '0.077',
  `float: ${String(totalFloat)} | exato: ${String(multiplyDecimal('0.07', '1.1'))}`,
)
verifica(
  'multiplyDecimal e exato',
  multiplyDecimal('0.07', '1.1') === '0.077',
  String(multiplyDecimal('0.07', '1.1')),
)
verifica(
  'multiplyDecimal preserva escala grande (satoshi x preco)',
  multiplyDecimal('0.00000001', '112345.67') === '0.0011234567',
  String(multiplyDecimal('0.00000001', '112345.67')),
)
verifica('multiplyDecimal com inteiros', multiplyDecimal('3', '4') === '12', String(multiplyDecimal('3', '4')))
verifica('multiplyDecimal recusa lixo em vez de inventar', multiplyDecimal('1,5', '2') === null)
verifica('multiplyDecimal recusa vazio', multiplyDecimal('', '2') === null)
verifica('multiplyDecimal propaga sinal', multiplyDecimal('-0.5', '3') === '-1.5', String(multiplyDecimal('-0.5', '3')))

// [VERIFICACAO DE FONTE] uma multiplicacao em float compila e da o valor quase
// certo; nao ha entrada que a exponha na tela. O defeito e textual.
const fonteManual = fonteSemComentarios('pages', 'ManualTrade.tsx')
verifica(
  'ManualTrade nao multiplica dinheiro com Number()',
  !/Number\(\s*form\.(quantity|price)\s*\)\s*\*/.test(fonteManual),
)
const fonteDashboard = fonteSemComentarios('pages', 'Dashboard.tsx')
verifica(
  'Dashboard nao divide dinheiro por dinheiro em float',
  !/Number\(data\.unrealized_pnl\)\s*\//.test(fonteDashboard),
)
verifica(
  'o corte da serie e feito por TIMESTAMP, nao comparando dinheiro',
  !/total_value\s*[<>]/.test(fonteDashboard) && !/toNumber\([^)]*\)\s*[<>]\s*toNumber/.test(fonteDashboard),
  'filtrar a serie comparando valores em float seria conta de dinheiro em number',
)

// ===========================================================================
bloco('Periodo dos graficos: atalhos e intervalo personalizado ate 1 ano')

const hojeFixo = new Date(2026, 9, 25, 15, 30) // 25/10/2026 15h30, hora local
verifica('um dia so (de = ate) vale', erroDoPersonalizado('2026-10-01', '2026-10-01', hojeFixo) === null)
verifica(
  '366 dias valem (o limite do backend)',
  erroDoPersonalizado('2025-10-25', '2026-10-25', hojeFixo) === null,
  String(erroDoPersonalizado('2025-10-25', '2026-10-25', hojeFixo)),
)
verifica(
  '367 dias sao recusados ANTES de ir a API',
  (erroDoPersonalizado('2025-10-24', '2026-10-25', hojeFixo) ?? '').includes('1 ano'),
)
verifica('inicio depois do fim e recusado', erroDoPersonalizado('2026-10-10', '2026-10-01', hojeFixo) !== null)
verifica('fim no futuro e recusado', erroDoPersonalizado('2026-10-01', '2026-10-26', hojeFixo) !== null)
{
  const p = parametrosDoPeriodo({ tipo: 'personalizado', de: '2026-10-20', ate: '2026-10-25' }, hojeFixo)
  verifica(
    'o fim de um intervalo que inclui hoje e AGORA, nao 23h59 do futuro',
    p.end === hojeFixo.toISOString(),
    p.end,
  )
  verifica(
    'o inicio e a meia-noite LOCAL do dia escolhido',
    p.start === new Date(2026, 9, 20).toISOString(),
    p.start,
  )
}
{
  const p = parametrosDoPeriodo({ tipo: 'atalho', dias: 7 }, hojeFixo)
  verifica(
    '7d sao 7 dias exatos ate agora',
    Date.parse(p.end) - Date.parse(p.start) === 7 * 24 * 60 * 60 * 1000,
  )
}
verifica(
  'a chave de cache de um atalho nao muda com o relogio',
  chaveDoPeriodo({ tipo: 'atalho', dias: 30 }) === chaveDoPeriodo({ tipo: 'atalho', dias: 30 }) &&
    chaveDoPeriodo({ tipo: 'atalho', dias: 30 }) !== chaveDoPeriodo({ tipo: 'atalho', dias: 90 }),
)
verifica(
  'periodo salvo corrompido ou fora do limite volta ao padrao',
  lerPeriodoSalvo('{lixo', { tipo: 'atalho', dias: 30 }, hojeFixo).tipo === 'atalho' &&
    lerPeriodoSalvo(
      JSON.stringify({ tipo: 'personalizado', de: '2020-01-01', ate: '2026-10-01' }),
      { tipo: 'atalho', dias: 30 },
      hojeFixo,
    ).tipo === 'atalho',
)

// ===========================================================================
bloco('Analise do portfolio: drawdown, retornos, comparacao e alocacao')

{
  const dd = drawdown([100, 120, 90, 130, 117])
  verifica('drawdown mede contra o pico ANTERIOR', Math.abs(dd.serie[2]! - -0.25) < 1e-12, String(dd.serie))
  verifica('a maior queda e a de 120 para 90', Math.abs(dd.maior - -0.25) < 1e-12 && dd.indiceDoPico === 1 && dd.indiceDoVale === 2)
  verifica('novo pico zera a queda', dd.serie[3] === 0)
  verifica('serie so subindo nao tem queda', drawdown([1, 2, 3]).maior === 0)
}
{
  const fechamentos = [
    { dia: '2026-10-11', valor: 100 }, // base: dia anterior ao periodo
    { dia: '2026-10-12', valor: 110 }, // segunda
    { dia: '2026-10-13', valor: 99 },
    { dia: '2026-10-19', valor: 120 }, // segunda seguinte
  ]
  const dias = retornosPorPeriodo(fechamentos, 'dia')
  verifica('o dia base nao vira barra', dias.length === 3 && dias[0]!.chave === '2026-10-12')
  verifica('retorno diario contra o fechamento anterior', Math.abs(dias[0]!.retorno - 0.1) < 1e-12)
  verifica(
    'semana comeca na segunda (domingo e da semana anterior)',
    chaveDoGrupo('2026-10-11', 'semana') === '2026-10-05' && chaveDoGrupo('2026-10-13', 'semana') === '2026-10-12',
  )
  const semanas = retornosPorPeriodo(fechamentos, 'semana')
  verifica(
    'retorno semanal: ultimo fechamento da semana contra o da semana anterior',
    semanas.length === 2 && Math.abs(semanas[0]!.retorno - -0.01) < 1e-12,
    JSON.stringify(semanas),
  )
}
{
  const c = comparacaoComBenchmark(
    [
      { dia: '2026-10-02', valor: 50 },
      { dia: '2026-10-03', valor: 55 },
    ],
    [
      { dia: '2026-10-01', valor: 1000 },
      { dia: '2026-10-02', valor: 1100 },
      { dia: '2026-10-03', valor: 990 },
    ],
  )
  verifica(
    'a comparacao comeca no primeiro dia COMUM (nada de semana de vantagem)',
    c.dias[0] === '2026-10-02' && c.carteira[0] === 0 && c.benchmark[0] === 0,
    JSON.stringify(c),
  )
  verifica('carteira +10% e BTC -10% desde a base', Math.abs(c.carteira[1]! - 0.1) < 1e-12 && Math.abs(c.benchmark[1]! - -0.1) < 1e-12)
}
{
  const a = alocacaoNoTempo(
    [
      { allocations: { USDC: 0.5, BTC: 0.2, ETH: 0.1, SOL: 0.1, ADA: 0.05, XRP: 0.05 } },
      { allocations: { USDC: 1 } },
    ],
    3,
  )
  verifica('os maiores pela media e o resto em Outros', a.ativos.join() === 'USDC,BTC,ETH,Outros', a.ativos.join())
  verifica(
    'cada ponto soma 100% (nada some no agrupamento)',
    [0, 1].every((i) => Math.abs(a.series.reduce((s, serie) => s + serie[i]!, 0) - 1) < 1e-12),
  )
}

// ===========================================================================
bloco('Contraste WCAG AA, calculado sobre as cores REAIS do styles.css')

const css = fonte('styles.css')

/** Tokens `--nome: #rrggbb` de um trecho de CSS. */
function tokens(trecho: string): Record<string, string> {
  const mapa: Record<string, string> = {}
  for (const achado of trecho.matchAll(/(--[a-z-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;/g)) {
    mapa[achado[1]!] = achado[2]!
  }
  return mapa
}

const blocoClaro = /@media \(prefers-color-scheme: light\)\s*\{([\s\S]*?)\n\}/.exec(css)
const tokensEscuro = tokens(css.slice(0, blocoClaro?.index ?? css.length))
const tokensClaro = { ...tokensEscuro, ...tokens(blocoClaro?.[1] ?? '') }
verifica(
  'os dois temas foram lidos do CSS',
  Object.keys(tokensEscuro).length > 8 && tokensClaro['--text-faint'] !== tokensEscuro['--text-faint'],
  `escuro: ${tokensEscuro['--text-faint']} | claro: ${tokensClaro['--text-faint']}`,
)

type Cor = { r: number; g: number; b: number; a: number }

function doHex(hex: string): Cor {
  return {
    r: parseInt(hex.slice(1, 3), 16),
    g: parseInt(hex.slice(3, 5), 16),
    b: parseInt(hex.slice(5, 7), 16),
    a: 1,
  }
}

/** `background` de uma regra do CSS, quando e rgba(). */
function fundoDaRegra(seletor: string): Cor | null {
  const regra = new RegExp(
    `${seletor.replace(/\./g, '\\.')}\\s*\\{[^}]*background:\\s*rgba?\\(([^)]+)\\)`,
  ).exec(css)
  if (!regra) return null
  const partes = regra[1]!.split(',').map((p) => parseFloat(p.trim()))
  return { r: partes[0]!, g: partes[1]!, b: partes[2]!, a: partes[3] ?? 1 }
}

/** Camada semitransparente sobre um fundo opaco. */
function compor(frente: Cor, fundo: Cor): Cor {
  return {
    r: frente.a * frente.r + (1 - frente.a) * fundo.r,
    g: frente.a * frente.g + (1 - frente.a) * fundo.g,
    b: frente.a * frente.b + (1 - frente.a) * fundo.b,
    a: 1,
  }
}

function canal(v: number): number {
  const c = v / 255
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

function luminancia(cor: Cor): number {
  return 0.2126 * canal(cor.r) + 0.7152 * canal(cor.g) + 0.0722 * canal(cor.b)
}

function contraste(frente: Cor, fundo: Cor): number {
  const a = luminancia(frente)
  const b = luminancia(fundo)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

// Sanidade do calculo contra a medicao do navegador: #818b98 sobre branco deu
// 3,45 no DevTools, e #656d76 sobre branco deu 5,25.
verifica(
  'a conta de contraste bate com a medida no navegador',
  Math.abs(contraste(doHex('#818b98'), doHex('#ffffff')) - 3.45) < 0.02 &&
    Math.abs(contraste(doHex('#656d76'), doHex('#ffffff')) - 5.25) < 0.02,
  `${contraste(doHex('#818b98'), doHex('#ffffff')).toFixed(2)} e ${contraste(doHex('#656d76'), doHex('#ffffff')).toFixed(2)}`,
)

/** AA para texto abaixo de 18 px. Todo texto secundario deste projeto e. */
const AA = 4.5

const tintaModo = fundoDaRegra('.mode-simulated')
const tintaAlerta = fundoDaRegra('.alert-banner')
const tintaPositiva = fundoDaRegra('.badge-positive')
const tintaNegativa = fundoDaRegra('.badge-negative')
const tintaAmbar = fundoDaRegra('.badge-warning')
verifica(
  'as tintas semitransparentes foram lidas do CSS',
  [tintaModo, tintaAlerta, tintaPositiva, tintaNegativa, tintaAmbar].every((t) => t !== null),
)

for (const tema of [
  { nome: 'escuro', t: tokensEscuro },
  { nome: 'claro', t: tokensClaro },
]) {
  const cor = (nome: string) => doHex(tema.t[nome]!)
  const fundo = cor('--bg')
  const cartao = cor('--bg-elevated')
  const sutil = cor('--bg-subtle')

  const pares: { onde: string; frente: Cor; atras: Cor }[] = [
    { onde: 'cabecalho de tabela / rotulo de eixo / titulo de cartao (faint no cartao)', frente: cor('--text-faint'), atras: cartao },
    { onde: 'faint sobre o fundo da pagina', frente: cor('--text-faint'), atras: fundo },
    { onde: 'faint sobre fundo sutil', frente: cor('--text-faint'), atras: sutil },
    { onde: 'estado vazio / nota do grafico / dica (muted no cartao)', frente: cor('--text-muted'), atras: cartao },
    { onde: 'chip e linha de estrategia (muted em fundo sutil)', frente: cor('--text-muted'), atras: sutil },
    { onde: 'texto principal no cartao', frente: cor('--text'), atras: cartao },
    { onde: 'detalhe da faixa de modo (faint sobre a tinta azul)', frente: cor('--text-faint'), atras: compor(tintaModo!, fundo) },
    { onde: 'faixa de modo (accent sobre a tinta azul)', frente: cor('--accent'), atras: compor(tintaModo!, fundo) },
    { onde: 'faixa de alerta (negative sobre a tinta vermelha)', frente: cor('--negative'), atras: compor(tintaAlerta!, fundo) },
    { onde: 'etiqueta aprovado', frente: cor('--positive'), atras: compor(tintaPositiva!, cartao) },
    { onde: 'etiqueta rejeitado', frente: cor('--negative'), atras: compor(tintaNegativa!, cartao) },
    { onde: 'etiqueta circuit breaker', frente: cor('--warning'), atras: compor(tintaAmbar!, cartao) },
    { onde: 'PnL positivo na tabela', frente: cor('--positive'), atras: cartao },
    { onde: 'PnL negativo na tabela', frente: cor('--negative'), atras: cartao },
  ]

  for (const par of pares) {
    const razao = contraste(par.frente, par.atras)
    verifica(
      `[${tema.nome}] ${par.onde}`,
      razao >= AA,
      `razao ${razao.toFixed(2)} — AA pede ${AA} para texto menor que 18 px`,
    )
  }
}

// O defeito existe: as cores que estavam no arquivo reprovavam.
verifica(
  'o defeito existe: o faint antigo do tema escuro dava 3,77 sobre o cartao',
  Math.abs(contraste(doHex('#6e7681'), doHex(tokensEscuro['--bg-elevated']!)) - 3.77) < 0.03,
  contraste(doHex('#6e7681'), doHex(tokensEscuro['--bg-elevated']!)).toFixed(2),
)
verifica(
  'o defeito existe: o faint antigo do tema claro dava 3,04 sobre a faixa de modo',
  Math.abs(
    contraste(doHex('#818b98'), compor(tintaModo!, doHex(tokensClaro['--bg']!))) - 3.04,
  ) < 0.03,
  contraste(doHex('#818b98'), compor(tintaModo!, doHex(tokensClaro['--bg']!))).toFixed(2),
)

// ===========================================================================
bloco('Nenhuma tela manda configurar negocio no .env (D15)')

/**
 * O `.env` so tem AMBIENTE. Dizer ao usuario para por variavel de negocio nele
 * e contradizer a fronteira que faz o backend recusar subir.
 *
 * A verificacao anterior procurava uma frase especifica ("estrategias ativas
 * vem de"), entao um texto NOVO mandando editar o .env passaria. Esta procura o
 * PADRAO: `.env` perto de qualquer nome de variavel de negocio.
 */
const NEGOCIO = [
  'STRATEGIES',
  'SYMBOLS',
  'TIMEFRAME',
  'QUOTE_CURRENCY',
  'PAPER_',
  'DISCOVERY_',
  'MAX_ORDER',
  'MIN_ORDER',
  'STOP_LOSS',
  'TAKE_PROFIT',
  'DAILY_LOSS',
  'CANDLE_HISTORY',
]

/**
 * Marcas de que a frase PROIBE, em vez de instruir.
 *
 * Documentar a fronteira ("nao coloque STRATEGIES no .env, o sistema recusa
 * subir") e exatamente o que o projeto quer na tela; o que nao pode e MANDAR
 * fazer. A negacao precisa vir ANTES da chave de negocio — foi o que separou os
 * dois casos: uma justificativa depois ("...faz o sistema recusar subir")
 * acompanha tanto a proibicao quanto a instrucao, e absolvia as duas.
 */
const NEGACOES = /\b(n[ãa]o|nunca|jamais|proib)/i

function mandaConfigurarNegocioNoEnv(texto: string): string[] {
  const achados: string[] = []
  for (const ocorrencia of texto.matchAll(/\.env/g)) {
    const de = Math.max(0, ocorrencia.index! - 240)
    const janela = texto.slice(de, ocorrencia.index! + 240)
    for (const chave of NEGOCIO) {
      const onde = janela.indexOf(chave)
      if (onde < 0) continue
      if (NEGACOES.test(janela.slice(0, onde))) continue
      achados.push(`${chave} perto de .env sem negacao antes`)
    }
  }
  return achados
}

// A verificacao se auto-testa nos tres casos que importam.
verifica(
  'a verificacao pega um texto que manda por STRATEGIES no .env',
  mandaConfigurarNegocioNoEnv('Coloque STRATEGIES no arquivo .env do servidor.').length > 0,
)
verifica(
  'pega tambem um texto NOVO, com outra chave de negocio',
  mandaConfigurarNegocioNoEnv('Ajuste MAX_ORDER_NOTIONAL editando o .env e reinicie.').length > 0,
  'a verificacao antiga procurava uma frase especifica e um texto novo passaria',
)
verifica(
  'e absolve a frase que PROIBE o mesmo gesto',
  mandaConfigurarNegocioNoEnv(
    'Não coloque STRATEGIES no .env: variável de negócio nesse arquivo faz o sistema recusar subir.',
  ).length === 0,
)
verifica(
  'pega a instrucao mesmo quando ela vem seguida da justificativa da proibicao',
  mandaConfigurarNegocioNoEnv(
    'Ajuste STRATEGIES no .env: variável de negócio nesse arquivo faz o sistema recusar subir.',
  ).length > 0,
  'foi assim que a versao anterior desta verificacao deixou passar a mutacao',
)
// Sem comentarios: a regra e sobre o que a TELA diz. Um comentario que explica
// o defeito antigo ("o texto anterior mandava editar STRATEGIES no .env") cita
// exatamente o padrao proibido, e nao chega a olho nenhum.
for (const pagina of paginas) {
  const achados = mandaConfigurarNegocioNoEnv(fonteSemComentarios('pages', pagina))
  verifica(`${pagina} nao manda configurar negocio no .env`, achados.length === 0, achados.join('; '))
}

// ===========================================================================
bloco('Classes de CSS que as telas usam existem de verdade')

/**
 * `className="btn btn-primary"` compila, passa no lint e renderiza um botao
 * secundario: nao existe regra `.btn` neste CSS. Era o botao que aplica a
 * configuracao de negocio e o que autoriza capital.
 */
const CLASSES_IGNORADAS = new Set([
  'active', // react-router
])
const arquivosComClasse = [
  ['App.tsx'],
  ['components', 'Shared.tsx'],
  ['components', 'Charts.tsx'],
  ...paginas.map((p) => ['pages', p]),
]
const ausentes: string[] = []
for (const partes of arquivosComClasse) {
  const codigo = fonte(...partes)
  for (const achado of codigo.matchAll(/className="([a-z0-9 _-]+)"/g)) {
    for (const classe of achado[1]!.split(/\s+/).filter(Boolean)) {
      if (CLASSES_IGNORADAS.has(classe)) continue
      // `button.primary`, `.card`, `td.right` — qualquer forma de declaracao.
      if (!new RegExp(`[.\\s]${classe}[\\s,:.{]`).test(css)) {
        ausentes.push(`${partes.join('/')}: .${classe}`)
      }
    }
  }
}
verifica(
  'toda classe estatica usada nas telas tem regra no styles.css',
  ausentes.length === 0,
  ausentes.join('; '),
)
verifica(
  'o defeito existe: .btn e .btn-primary nunca foram declarados',
  !/\.btn[\s,:.{]/.test(css) && !/\.btn-primary/.test(css),
)

// ===========================================================================
bloco('Contrato das mutacoes: o que a tela envia e o que a API exige')

// Rearmar e Autorizar capital mandaram POST sem corpo de 10/09 a 09/11/2026 e a
// API respondeu 400 o tempo todo. O `contrato-mutacoes.json` e repetido contra
// a API em `backend/tests/test_api.py` (TestContratoDasTelas); aqui se confere
// que as funcoes que o `client.ts` usa montam EXATAMENTE aqueles pedidos.

/** JSON com as chaves em ordem: comparar objeto sem depender da ordem de montagem. */
function canonico(valor: unknown): string {
  if (Array.isArray(valor)) return `[${valor.map(canonico).join(',')}]`
  if (valor !== null && typeof valor === 'object') {
    const obj = valor as Record<string, unknown>
    return `{${Object.keys(obj)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonico(obj[k])}`)
      .join(',')}}`
  }
  return JSON.stringify(valor)
}

function comoNoContrato(pedido: { metodo?: string; caminho?: string; corpo?: unknown }) {
  return {
    metodo: pedido.metodo,
    caminho: pedido.caminho,
    ...(pedido.corpo !== undefined ? { corpo: pedido.corpo } : {}),
  }
}

const lancamentoDoContrato: LancamentoManual = {
  executed_at: '2026-11-09T15:00:00.000Z',
  exchange: 'binance',
  symbol: 'BTC/USDT',
  side: 'buy',
  quantity: '0.01',
  price: '95000',
  fee: '0',
  fee_currency: 'USDT',
  notes: null,
}
const montados: Record<string, Pedido> = {
  rearmar_circuit_breaker: pedidos.rearmarCircuitBreaker(),
  autorizar_capital: pedidos.autorizarCapital('5000'),
  alterar_limites: pedidos.alterarLimites({ cooldown_seconds: 300 }),
  alterar_negocio: pedidos.alterarNegocio({ timeframe: '4h' }),
  lancar_manual: pedidos.lancarManual(lancamentoDoContrato, false),
  lancar_manual_preco_fora_sem_aceitar: pedidos.lancarManual(
    { ...lancamentoDoContrato, price: '9500' },
    false,
  ),
  lancar_manual_preco_fora_aceito: pedidos.lancarManual(
    { ...lancamentoDoContrato, price: '9500' },
    true,
  ),
  excluir_lancamento: pedidos.excluirLancamento('ID_DO_LANCAMENTO'),
}

const pedidosDoContrato: { nome: string; metodo: string; caminho: string; corpo?: unknown }[] =
  contratoDasMutacoes.pedidos
for (const esperado of pedidosDoContrato) {
  const montado = montados[esperado.nome]
  const daTela = montado ? canonico(comoNoContrato(montado)) : 'nenhum'
  const doContrato = canonico(comoNoContrato(esperado))
  verifica(
    `${esperado.nome}: a tela monta o pedido que a API aceita`,
    daTela === doContrato,
    `tela: ${daTela}\n          contrato: ${doContrato}`,
  )
}
verifica(
  'toda mutacao montada tem par no contrato (e e repetida contra a API)',
  Object.keys(montados).every((nome) => pedidosDoContrato.some((p) => p.nome === nome)),
)

// O par do conserto: o pedido antigo (POST sem corpo) nao casa com o contrato.
const contratoRearme = pedidosDoContrato.find((p) => p.nome === 'rearmar_circuit_breaker')
verifica(
  'o defeito existe: o rearme sem corpo, como saia ate 09/11, nao e o que a API exige',
  contratoRearme !== undefined &&
    canonico({ metodo: 'POST', caminho: '/risk/circuit-breaker/reset' }) !==
      canonico(comoNoContrato(contratoRearme)),
)
verifica(
  'autorizar leva o patrimonio que a tela mostrou (o 409 de deposito no meio passa a valer)',
  pedidos.autorizarCapital('1234.56').corpo?.expected_total_value === '1234.56',
)

// [VERIFICACAO DE FONTE] O `client.ts` nao pode montar estes pedidos por conta
// propria: se montar, o contrato acima confere uma funcao que ninguem usa.
const fonteClient = fonteSemComentarios('api', 'client.ts')
const caminhosDiretos = [
  '/risk/circuit-breaker/reset',
  '/risk/capital/authorize',
  '/risk/config',
  '/trading/config',
  '/trades/manual',
  '/trades/${',
].filter((caminho) => {
  // As leituras (GET) de `/risk/config` e `/trading/config` continuam no client.
  const usos = fonteClient.split(caminho).length - 1
  const leituras = caminho === '/risk/config' || caminho === '/trading/config' ? 1 : 0
  return usos > leituras
})
verifica(
  'client.ts envia as mutacoes so pelo que sai de mutacoes.ts',
  caminhosDiretos.length === 0,
  caminhosDiretos.join(', '),
)

// ===========================================================================
bloco('Lancamento manual: data do formulario e fuso')

const abertoAs9 = formularioVazio(new Date(2026, 10, 9, 9, 0))
const abertoAs15 = formularioVazio(new Date(2026, 10, 9, 15, 0))
verifica(
  'a data padrao e a hora em que o formulario abre',
  abertoAs9.executed_at === '2026-11-09T09:00' && abertoAs15.executed_at === '2026-11-09T15:00',
  `${abertoAs9.executed_at} / ${abertoAs15.executed_at}`,
)
const enviado = lancamentoDoFormulario(
  { ...abertoAs15, symbol: 'btc/usdc', quantity: '1', price: '2' },
  'USDC',
)
verifica(
  'o envio leva o horario com fuso (a API recusa data sem fuso)',
  enviado.executed_at.endsWith('Z') &&
    Date.parse(enviado.executed_at) === new Date(2026, 10, 9, 15, 0).getTime(),
  enviado.executed_at,
)
verifica(
  'moeda da taxa vazia usa a cotacao do par',
  enviado.fee_currency === 'USDC' && enviado.symbol === 'BTC/USDC',
  `${enviado.fee_currency} ${enviado.symbol}`,
)
// [VERIFICACAO DE FONTE] O defeito era textual: uma constante de modulo com a
// data, avaliada uma vez quando o app carregava.
const fonteManualTrade = fonteSemComentarios('pages', 'ManualTrade.tsx')
verifica(
  'nenhuma data de formulario e calculada no carregamento do modulo',
  !/^const\s+EMPTY\b/m.test(fonteManualTrade) && !/^\s*executed_at:\s*nowForInput\(\)/m.test(fonteManualTrade),
)

// ===========================================================================
bloco('Preco de ativo barato e taxa no ativo base')

// O defeito: Posicoes formatava preco com `money()`, 2 casas.
verifica(
  'o defeito existe: PEPE a 0,0000095 com money() vira 0,00',
  money('0.0000095', '') === '0,00',
  money('0.0000095', ''),
)
verifica('price mostra o PEPE com casas significativas', price('0.0000095') === '0,0000095', price('0.0000095'))
verifica('price mostra o SHIB inteiro', price('0.00001234') === '0,00001234', price('0.00001234'))
verifica('price mantem 6 casas abaixo de 10', price('0.17345') === '0,17345', price('0.17345'))
verifica('price de BTC continua com 2 casas', price('65432.1') === '65.432,10', price('65432.1'))
verifica('price de zero nao inventa casas', price('0') === '0,00', price('0'))

// O defeito: taxa de compra em live, cobrada no ativo base.
verifica(
  'o defeito existe: 0,0000012 BTC com money() vira 0,00 BTC',
  money('0.0000012', 'BTC') === '0,00 BTC',
  money('0.0000012', 'BTC'),
)
verifica(
  'taxa no ativo base sai com casas de quantidade e a moeda',
  taxa('0.0000012', 'BTC', 'BTC/USDC') === '0,0000012 BTC',
  taxa('0.0000012', 'BTC', 'BTC/USDC'),
)
verifica(
  'taxa na cotacao sai como dinheiro',
  taxa('0.05', 'USDC', 'BTC/USDC') === '0,05 USDC',
  taxa('0.05', 'USDC', 'BTC/USDC'),
)
verifica(
  'taxa sem moeda gravada usa a cotacao do par',
  taxa('0.05', null, 'BTC/BRL') === '0,05 BRL',
  taxa('0.05', null, 'BTC/BRL'),
)
verifica('sem taxa: travessao, e nao 0,00', taxa('0', 'USDC', 'BTC/USDC') === '—' && taxa(null, null, null) === '—')
// [VERIFICACAO DE FONTE] O defeito era escolher o formatador errado no JSX.
verifica(
  'Posicoes nao formata preco com money()',
  !/money\(position\.(average|current)_price/.test(fonteDashboard),
)
verifica(
  'Negociacoes formata a taxa com taxa()',
  /taxa\(trade\.fee, trade\.fee_currency, trade\.symbol\)/.test(fonteSemComentarios('pages', 'Trades.tsx')),
)
// [VERIFICACAO DE FONTE] Barras de "Resultado por par" com a moeda do par.
verifica(
  'Resultado por par: barras na moeda de cada par',
  /formatValue=\{\(v, i\) => money\(v, pares\[i\]\?\.moeda/.test(fonteSemComentarios('pages', 'Portfolio.tsx')),
)

// ===========================================================================
bloco('Configuracoes: a confirmacao diz o que muda')

const CONFIG_ATUAL = {
  quote_currency: 'USDC',
  symbols: ['BTC/USDC', 'ETH/USDC'],
  timeframe: '1h',
  strategies: ['ma_crossover'],
}
const CAMPOS = [
  { key: 'quote_currency', label: 'Moeda de cotação' },
  { key: 'symbols', label: 'Pares negociados' },
  { key: 'timeframe', label: 'Timeframe' },
]
const apagarPares = alteracoesDeNegocio(CAMPOS, CONFIG_ATUAL, { symbols: [] })
verifica(
  'apagar os pares diz que liga a descoberta automatica',
  apagarPares.length === 1 &&
    apagarPares[0]!.campo === 'Pares negociados' &&
    apagarPares[0]!.antes === 'BTC/USDC, ETH/USDC' &&
    apagarPares[0]!.depois === '(vazio)' &&
    /descoberta automática/.test(apagarPares[0]!.aviso ?? ''),
  JSON.stringify(apagarPares),
)
const trocarMoeda = alteracoesDeNegocio(CAMPOS, CONFIG_ATUAL, { quote_currency: 'USDT', timeframe: '4h' })
verifica(
  'trocar a moeda avisa que nada e convertido; cada campo vira uma linha',
  trocarMoeda.length === 2 &&
    trocarMoeda[0]!.antes === 'USDC' &&
    trocarMoeda[0]!.depois === 'USDT' &&
    /nenhum valor é convertido/.test(trocarMoeda[0]!.aviso ?? '') &&
    trocarMoeda[1]!.campo === 'Timeframe' &&
    trocarMoeda[1]!.aviso === undefined,
  JSON.stringify(trocarMoeda),
)
const voltarAosPares = alteracoesDeNegocio(CAMPOS, { ...CONFIG_ATUAL, symbols: [] }, { symbols: ['BTC/USDC'] })
verifica(
  'listar pares com a descoberta ligada diz que a desliga',
  /desliga a descoberta/.test(voltarAosPares[0]!.aviso ?? ''),
  JSON.stringify(voltarAosPares),
)
// [VERIFICACAO DE FONTE] O defeito era o botao chamar `save.mutate` direto. O
// pedido enviado tem de ser o congelado no primeiro passo, de onde o resumo saiu.
const fonteConfiguracao = fonteSemComentarios('pages', 'Configuration.tsx')
const chamadasDeSalvar = fonteConfiguracao.match(/save\.mutate\([^)]*\)/g) ?? []
verifica(
  'Configuracoes so envia o pedido confirmado',
  chamadasDeSalvar.length === 1 && chamadasDeSalvar[0] === 'save.mutate(pendente.pedido)',
  chamadasDeSalvar.join(', '),
)
verifica(
  '"Aplicar alterações" abre a confirmacao, nao aplica',
  /onClick=\{pedirConfirmacao\}/.test(fonteConfiguracao),
)

// Risco tinha a mesma frase fixa para tudo (telas.md, low): apertar o stop e
// esvaziar o "Capital autorizado" — que libera o patrimonio inteiro — pediam a
// mesma confirmacao generica.
const CAMPOS_DE_RISCO = [
  { key: 'stop_loss_pct', label: 'Stop-loss' },
  { key: 'authorized_capital', label: 'Capital autorizado a operar', nullable: true },
  { key: 'max_order_notional', label: 'Valor máximo por ordem', nullable: true },
]
const BASE_DE_RISCO = { stop_loss_pct: '0.03', authorized_capital: '29.29', max_order_notional: '' }
const liberarCapital = alteracoesDeRisco(CAMPOS_DE_RISCO, BASE_DE_RISCO, {
  clear_authorized_capital: true,
  stop_loss_pct: 0.05,
})
verifica(
  'esvaziar o capital autorizado diz que libera o patrimonio e os depositos futuros',
  liberarCapital.length === 2 &&
    liberarCapital[0]!.campo === 'Capital autorizado a operar' &&
    liberarCapital[0]!.antes === '29.29' &&
    liberarCapital[0]!.depois === '(sem limite)' &&
    /depósitos futuros/.test(liberarCapital[0]!.aviso ?? ''),
  JSON.stringify(liberarCapital),
)
verifica(
  'mudar o stop mostra antes e depois, sem aviso extra',
  liberarCapital[1]!.campo === 'Stop-loss' &&
    liberarCapital[1]!.antes === '0.03' &&
    liberarCapital[1]!.depois === '0.05' &&
    liberarCapital[1]!.aviso === undefined,
  JSON.stringify(liberarCapital[1]),
)
const porTeto = alteracoesDeRisco(CAMPOS_DE_RISCO, BASE_DE_RISCO, { max_order_notional: '50' })
verifica(
  'campo que aceita vazio mostra "(sem limite)" como valor anterior',
  porTeto[0]!.antes === '(sem limite)' && porTeto[0]!.depois === '50',
  JSON.stringify(porTeto),
)
const fonteRisco = fonteSemComentarios('pages', 'RiskSettings.tsx')
const salvarRisco = fonteRisco.match(/save\.mutate\([^)]*\)/g) ?? []
verifica(
  'Risco so envia o pedido confirmado, de onde o resumo saiu',
  salvarRisco.length === 1 &&
    salvarRisco[0] === 'save.mutate(pendente.pedido)' &&
    /alteracoesDeRisco\(/.test(fonteRisco),
  salvarRisco.join(', '),
)

// ===========================================================================
bloco('Nenhuma tela de dados confunde falha com vazio')

// [VERIFICACAO DE FONTE] Cada `const x = useQuery(...)` de pagina precisa olhar
// `x.isError` em algum lugar: sem isso, a falha da API cai no ramo "vazio"
// ("Nenhuma negociação encontrada", "falta configurar"). Nao prova que o ramo
// esta certo — prova que ninguem esqueceu a consulta, que era o defeito.
for (const pagina of paginas) {
  const codigo = fonteSemComentarios('pages', pagina)
  const consultas = [...codigo.matchAll(/const (\w+) = useQuery\b/g)].map((m) => m[1]!)
  const semTratamento = consultas.filter((nome) => !codigo.includes(`${nome}.isError`))
  verifica(
    `${pagina}: toda consulta trata erro`,
    semTratamento.length === 0,
    semTratamento.length ? `sem isError: ${semTratamento.join(', ')}` : '',
  )
  if (consultas.length > 0 || /useHealth\(\)/.test(codigo)) {
    verifica(`${pagina}: a falha aparece com ErroDeCarga`, codigo.includes('<ErroDeCarga'))
  }
}

// ===========================================================================
bloco('Faixa de modo em todas as telas; trava respeitada por agente')

// [VERIFICACAO DE FONTE] A faixa mora no layout. Se uma pagina voltar a desenhar
// a sua, aparecem duas; se o layout perder a dele, quatro telas de acao ficam
// sem saber se e dinheiro real.
const fonteApp = fonteSemComentarios('App.tsx')
verifica('App desenha a faixa de modo acima das rotas', /<ModeBanner[^>]*\/>\s*<Routes>/.test(fonteApp))
const paginasComFaixa = paginas.filter((pagina) =>
  /<ModeBanner\b/.test(fonteSemComentarios('pages', pagina)),
)
verifica('nenhuma pagina desenha a propria faixa', paginasComFaixa.length === 0, paginasComFaixa.join(', '))
const fonteAgentes = fonteSemComentarios('pages', 'Agents.tsx')
verifica(
  '"retomar" de cada agente fica indisponivel com a trava acionada, com o motivo',
  /disabled=\{status\.paused && travado\}/.test(fonteAgentes) &&
    /title=\{status\.paused && travado \? motivoDaTrava/.test(fonteAgentes),
)

// ===========================================================================
bloco('Celular de 360 px, rotulos e reconexao')

// [VERIFICACAO DE FONTE] Coluna minima fixa maior que a tela: com 360 px de
// largura e 14 px de margem dos dois lados sobram 332 px, e `minmax(340px, 1fr)`
// fazia o grid-2 rolar de lado.
const fonteCss = fonte('styles.css')
const colunasLargas = [...fonteCss.matchAll(/minmax\((\d+)px/g)].filter((m) => Number(m[1]) > 300)
verifica(
  'nenhuma coluna de grid exige mais de 300 px fixos',
  colunasLargas.length === 0,
  colunasLargas.map((m) => m[0]).join(', '),
)
// Rotulo sem `htmlFor` nao nomeia o campo para o leitor de tela. Rotulo que
// ENVOLVE o campo (checkbox) nomeia sozinho e nao entra aqui.
const rotuloSolto = /<label>(?:(?!<\/label>)[\s\S])*?<\/label>\s*<(input|select|textarea)/
const paginasComRotuloSolto = paginas.filter((pagina) => rotuloSolto.test(fonte('pages', pagina)))
verifica(
  'todo rotulo seguido de campo aponta para ele (htmlFor)',
  paginasComRotuloSolto.length === 0,
  paginasComRotuloSolto.join(', '),
)
// [VERIFICACAO DE FONTE] O socket nao guarda historico: o que aconteceu na
// queda so chega revalidando.
const fonteLive = fonteSemComentarios('hooks', 'useLiveEvents.ts')
verifica(
  'reconectar o WebSocket revalida as consultas',
  /onopen = \(\) => \{[\s\S]*?invalidateQueries\(\)[\s\S]*?\}/.test(fonteLive),
)

// ===========================================================================
bloco('Feed de atividade: historico do banco + tempo real, sem repetir')

// Payloads com a forma REAL das duas fontes: `GET /api/atividade`
// (`routes/atividade.py`, conferida em `tests/test_atividade.py`) e a mensagem
// do WebSocket (`routes/ws.py`, `model_dump(mode="json")` do modelo de dominio).
const SINAL_ID = '6f1c2a9e0b7d4e3f8a5c1d2e3f4a5b6c'
const DECISAO_ID = '0a1b2c3d4e5f60718293a4b5c6d7e8f9'
const ORDEM_ID = 'ffeeddccbbaa99887766554433221100'
const HISTORICO_REAL: ItemDoFeed[] = [
  {
    event: 'alert',
    at: '2026-11-10T03:12:40.512000+00:00',
    data: {
      type: 'circuit_breaker',
      title: 'Circuit breaker acionado',
      reason: 'perda diaria de 5,2% acima do limite de 5%',
      target: 'risk_config',
      timestamp: '2026-11-10T03:12:40.512000+00:00',
    },
  },
  {
    event: 'order_result',
    at: '2026-11-10T02:44:11.020000+00:00',
    data: {
      id: ORDEM_ID,
      order_request_id: ORDEM_ID,
      symbol: 'BTC/BRL',
      side: 'buy',
      status: 'filled',
      filled_quantity: '0.00004',
      average_price: '562340.12',
      error: null,
      timestamp: '2026-11-10T02:44:11.020000+00:00',
    },
  },
  {
    event: 'risk_assessment',
    at: '2026-11-10T02:44:10.900000+00:00',
    data: {
      id: DECISAO_ID,
      decision: 'approved',
      reasons: [],
      approved_notional: '22.50',
      snapshot: { symbol: 'BTC/BRL' },
    },
  },
  {
    event: 'signal',
    at: '2026-11-10T02:44:10.100000+00:00',
    data: {
      id: SINAL_ID,
      created_at: '2026-11-10T02:44:10.100000',
      symbol: 'BTC/BRL',
      strategy: 'ma_crossover',
      direction: 'long',
      confidence: 0.71,
      reason: 'media curta cruzou a longa',
      reference_price: '562300.00',
    },
  },
  {
    event: 'system',
    at: '2026-11-09T22:14:03.000000+00:00',
    data: { type: 'system_started', actor: 'system', target: 'orchestrator', mode: 'dry_run' },
  },
]
// O WebSocket entregou o mesmo sinal e o mesmo alerta (com 30 ms de diferenca do
// registro no banco), mais tres retratos de portfolio.
const chegada = new Date('2026-11-10T03:20:00Z')
const AO_VIVO: ItemDoFeed[] = [
  {
    event: 'portfolio',
    data: { timestamp: '2026-11-10T03:15:00+00:00', total_value: '29.29', unrealized_pnl: '0' },
  },
  {
    event: 'alert',
    data: {
      type: 'circuit_breaker',
      title: 'Circuit breaker acionado',
      message: 'perda diaria de 5,2% acima do limite de 5%\n\nOs agentes de decisao foram pausados.',
      reason: 'perda diaria de 5,2% acima do limite de 5%',
      timestamp: '2026-11-10T03:12:40.542000+00:00',
    },
  },
  {
    event: 'portfolio',
    data: { timestamp: '2026-11-10T03:14:00+00:00', total_value: '29.30', unrealized_pnl: '0' },
  },
  {
    event: 'portfolio',
    data: { timestamp: '2026-11-10T03:13:00+00:00', total_value: '29.31', unrealized_pnl: '0' },
  },
  {
    event: 'signal',
    data: { ...HISTORICO_REAL[3]!.data, created_at: '2026-11-10T02:44:10.100000+00:00' },
  },
].map((m) => ({ ...m, at: instanteDoEvento(m.data, chegada) }))

const juntado = juntarAtividade(HISTORICO_REAL, AO_VIVO, 50)
const chaves = juntado.map(chaveDoEvento)
const concatenado = [...AO_VIVO, ...HISTORICO_REAL]
verifica(
  'o defeito existe: so concatenar repete o sinal e o alerta',
  concatenado.filter((i) => i.event === 'signal').length === 2 &&
    concatenado.filter((i) => i.event === 'alert').length === 2,
)
verifica(
  'ao abrir a tela, o historico aparece mesmo sem nada ao vivo',
  juntarAtividade(HISTORICO_REAL, [], 50).length === HISTORICO_REAL.length,
)
verifica(
  'o mesmo sinal (mesmo id) aparece uma vez',
  juntado.filter((i) => i.event === 'signal').length === 1,
)
verifica(
  'o mesmo alerta (mesmo tipo, 30 ms de diferenca) aparece uma vez, e e o do tempo real',
  juntado.filter((i) => i.event === 'alert').length === 1 &&
    typeof juntado.find((i) => i.event === 'alert')!.data.message === 'string',
)
verifica('nenhuma chave repetida (a key do React)', new Set(chaves).size === chaves.length, chaves.join(', '))
verifica(
  'retratos de portfolio: so o mais recente',
  juntado.filter((i) => i.event === 'portfolio').length === 1 &&
    juntado.find((i) => i.event === 'portfolio')!.data.total_value === '29.29',
)
const instantesJuntados = juntado.map((i) => Date.parse(i.at))
verifica(
  'do mais novo para o mais antigo, historico e tempo real intercalados',
  instantesJuntados.every((ms, i) => i === 0 || ms <= instantesJuntados[i - 1]!) &&
    juntado[0]!.event === 'portfolio' &&
    juntado[juntado.length - 1]!.event === 'system',
  juntado.map((i) => `${i.event}@${i.at}`).join(' | '),
)
verifica('o maximo e respeitado', juntarAtividade(HISTORICO_REAL, AO_VIVO, 3).length === 3)
const outroDisparo: ItemDoFeed = {
  ...HISTORICO_REAL[0]!,
  at: '2026-11-10T04:12:40.512000+00:00',
  data: { ...HISTORICO_REAL[0]!.data, timestamp: '2026-11-10T04:12:40.512000+00:00' },
}
verifica(
  'dois disparos do circuit breaker com 1 h de distancia sao dois eventos',
  juntarAtividade([outroDisparo, ...HISTORICO_REAL], [], 50).filter((i) => i.event === 'alert')
    .length === 2,
)
const mesmaOrdemAberta: ItemDoFeed = {
  event: 'order_result',
  at: '2026-11-10T02:44:10.990000+00:00',
  data: { order_request_id: ORDEM_ID, status: 'open', filled_quantity: '0' },
}
verifica(
  'a mesma ordem em dois estados (aberta, executada) sao dois eventos',
  juntarAtividade(HISTORICO_REAL, [mesmaOrdemAberta], 50).filter((i) => i.event === 'order_result')
    .length === 2,
)
verifica(
  'instante do tempo real vem do servidor, nao da chegada',
  AO_VIVO[1]!.at === '2026-11-10T03:12:40.542000+00:00',
  AO_VIVO[1]!.at,
)
verifica(
  'sem instante no payload, vale a chegada',
  instanteDoEvento({ total_value: '1' }, chegada) === chegada.toISOString(),
)

const textoSubida = descreverEvento('system', HISTORICO_REAL[4]!.data, 'USDC')
verifica('marco de subida diz o modo', textoSubida === 'Sistema iniciado em simulação', textoSubida)
const textoParada = descreverEvento(
  'system',
  { type: 'system_stopped', detail: 'sinal de parada (SIGTERM)' },
  'USDC',
)
verifica('marco de parada diz o motivo', textoParada === 'Sistema parado: sinal de parada (SIGTERM)', textoParada)
const textoAlerta = descreverEvento('alert', HISTORICO_REAL[0]!.data, 'USDC')
verifica(
  'alerta do historico mostra titulo e motivo',
  textoAlerta === 'ALERTA: Circuit breaker acionado — perda diaria de 5,2% acima do limite de 5%',
  textoAlerta,
)
const textoSemMotivo = descreverEvento(
  'alert',
  { type: 'unclean_shutdown', title: 'A execucao anterior nao desligou -- ela foi derrubada' },
  'USDC',
)
verifica(
  'alerta sem motivo mostra o titulo, nao o tipo cru',
  textoSemMotivo.includes('foi derrubada') && !textoSemMotivo.includes('unclean_shutdown'),
  textoSemMotivo,
)
const textoDecisao = descreverEvento('risk_assessment', HISTORICO_REAL[2]!.data, 'USDC')
verifica(
  'decisao do historico usa a moeda do PAR, nao a de hoje',
  textoDecisao === 'Risk Manager aprovou em BTC/BRL 22,50 BRL',
  textoDecisao,
)
verifica(
  'hora do feed: evento de ontem leva a data',
  horaDoFeed('2026-11-09T12:00:00', new Date('2026-11-10T12:00:00')).includes('09/11') &&
    !horaDoFeed('2026-11-10T09:00:00', new Date('2026-11-10T12:00:00')).includes('/'),
)
// [VERIFICACAO DE FONTE] A tela le o historico e junta com o tempo real; a
// linha do feed e identificada pelo evento, nao pela posicao.
const fonteDoFeed = fonteSemComentarios('pages', 'Dashboard.tsx')
verifica(
  'Dashboard le /atividade e junta com o tempo real',
  /api\.atividade\(/.test(fonteDoFeed) && /juntarAtividade\(/.test(fonteDoFeed),
)
verifica('linha do feed com key do evento', /key=\{chaveDoEvento\(item\)\}/.test(fonteDoFeed))

// ===========================================================================
bloco('Negociacoes: paginacao depois de excluir, linha por teclado, avisos anunciados')

// Excluir o unico item da ultima pagina (26 lancamentos, pagina 2) deixava a
// tela em "26–25 de 25" com uma pagina vazia.
const rotuloAntigo = (offset: number, tamanho: number, total: number) =>
  total === 0 ? 'nenhum registro' : `${offset + 1}–${Math.min(offset + tamanho, total)} de ${total}`
verifica('o defeito existe: a faixa antiga dizia "26–25 de 25"', rotuloAntigo(25, 25, 25) === '26–25 de 25')
verifica(
  'depois de excluir, a pagina volta para a ultima que existe',
  paginaValida(25, 25, 25) === 0 && rotuloDaPagina(25, 25, 25) === '1–25 de 25',
  rotuloDaPagina(25, 25, 25),
)
verifica(
  'pagina valida nao muda; vazio e "nenhum registro"',
  paginaValida(25, 25, 26) === 25 &&
    rotuloDaPagina(25, 25, 26) === '26–26 de 26' &&
    rotuloDaPagina(0, 25, 0) === 'nenhum registro',
)
// [VERIFICACAO DE FONTE] Acessibilidade e um defeito do texto do JSX.
const fonteNegociacoes = fonteSemComentarios('pages', 'Trades.tsx')
// Ate o `>` sozinho na linha que fecha a tag: os `=>` das funcoes ficam dentro.
const linhaExpansivel = fonteNegociacoes.match(/<tr\s+className="expandable"[\s\S]*?\n\s*>/)?.[0] ?? ''
verifica(
  'linha que expande abre pelo teclado e diz se esta aberta',
  /tabIndex=\{0\}/.test(linhaExpansivel) &&
    /onKeyDown=/.test(linhaExpansivel) &&
    /aria-expanded=/.test(linhaExpansivel),
  linhaExpansivel,
)
const botaoDentroDeLink = paginas.filter((pagina) =>
  /<a\b[^>]*>\s*<button/.test(fonteSemComentarios('pages', pagina)),
)
verifica('nenhum botao dentro de link', botaoDentroDeLink.length === 0, botaoDentroDeLink.join(', '))
verifica('filtro "Par" espera a digitacao parar', /setTimeout\(/.test(fonteNegociacoes))
const avisosMudos: string[] = []
for (const partes of arquivosComClasse) {
  const codigo = fonteSemComentarios(...partes)
  for (const tag of codigo.matchAll(/<div\s+className=(?:"form-message[^"]*"|\{`form-message[^`]*`\})[^>]*>/g)) {
    if (!/\brole=/.test(tag[0])) avisosMudos.push(`${partes.join('/')}: ${tag[0].slice(0, 60)}`)
  }
}
verifica(
  'toda mensagem de resultado e anunciada ao leitor de tela (role)',
  avisosMudos.length === 0,
  avisosMudos.join('; '),
)
verifica('o rodape "reconectando…" e anunciado', /sidebar-estado"[^>]*role="status"/.test(fonteApp))

// ===========================================================================
bloco('Graficos com nome para o leitor de tela')

verifica(
  'resumo da serie: primeiro e ultimo valor medidos, buraco ignorado',
  resumoDaSerie([null, 29.29, 30, null, 31.02, null], (v) => money(v, 'USDC')) ===
    'de 29,29 USDC a 31,02 USDC',
  resumoDaSerie([null, 29.29, 30, null, 31.02, null], (v) => money(v, 'USDC')),
)
verifica('serie sem nenhum valor diz "sem dados"', resumoDaSerie([null, null], String) === 'sem dados')
// [VERIFICACAO DE FONTE] `<svg>` sem papel nem nome: o leitor de tela pulava o
// grafico, e o valor so existia no tooltip de mouse.
const fonteGraficos = fonteSemComentarios('components', 'Charts.tsx')
const svgsSemNome = [...fonteGraficos.matchAll(/<svg\b[\s\S]*?\n?\s*>/g)]
  .map((m) => m[0])
  .filter((tag) => !/role="img"/.test(tag) || !/aria-label=/.test(tag))
verifica(
  'todo <svg> de grafico tem role="img" e aria-label',
  svgsSemNome.length === 0 && fonteGraficos.includes('<svg'),
  svgsSemNome.map((t) => t.slice(0, 50)).join(' | '),
)

// ===========================================================================
console.log(
  `\n${verificacoes - falhas}/${verificacoes} verificacoes passaram` +
    (falhas > 0 ? ` — ${falhas} FALHA(S)` : ''),
)
process.exit(falhas > 0 ? 1 : 0)
