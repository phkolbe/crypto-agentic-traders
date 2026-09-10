/**
 * Gráficos em SVG: série temporal e rosca.
 *
 * Substituem o Recharts, que custava **60% do bundle** (618 kB de fonte, mais
 * lodash, d3-*, decimal.js-light e react-smooth arrastados junto) para desenhar
 * três gráficos. É a mesma troca que o projeto já fez nos indicadores e no
 * backtester: código próprio, testável e sem caixa-preta, em vez de uma
 * biblioteca genérica da qual se usa 2%.
 *
 * O que se ganha além do tamanho: o bug do donut que sumia — a animação de
 * entrada congelava num quadro degenerado e o setor virava uma linha — deixa de
 * ser possível, porque não existe animação nem estado interno aqui. O dashboard
 * re-renderiza a cada evento do WebSocket, e esses componentes são funções puras
 * dos dados.
 */

import { useEffect, useRef, useState } from 'react'

import { rotuladorDeSerie } from '../api/format'

/** Ponto de uma série temporal. `time` é ISO-8601; `value` já em unidades reais. */
export type SeriesPoint = { time: string; value: number }

const AXIS = 'var(--text-faint)'
const LINE = 'var(--accent)'

// -------------------------------------------------------------------------
// Interpolação
// -------------------------------------------------------------------------

/**
 * Tangentes de Fritsch–Carlson: o que o Recharts chamava de `type="monotone"`.
 *
 * Uma spline cúbica comum inventa oscilação entre pontos — numa curva de
 * patrimônio isso desenharia prejuízo onde não houve. A interpolação monotônica
 * garante que o traço só sobe onde os dados sobem, ao custo de não ser tão
 * suave. Para dinheiro, essa é a troca certa.
 */
function monotoneTangents(xs: number[], ys: number[]): number[] {
  const n = xs.length
  if (n < 2) return [0]

  const dxs: number[] = []
  const slopes: number[] = []
  for (let i = 0; i < n - 1; i += 1) {
    const dx = xs[i + 1]! - xs[i]!
    dxs.push(dx)
    // dx zero acontece quando dois pontos têm o mesmo instante; tratar como
    // patamar evita divisão por zero virar NaN e apagar o gráfico inteiro.
    slopes.push(dx === 0 ? 0 : (ys[i + 1]! - ys[i]!) / dx)
  }

  const tangents: number[] = [slopes[0]!]
  for (let i = 0; i < n - 2; i += 1) {
    const m = slopes[i]!
    const next = slopes[i + 1]!
    if (m * next <= 0) {
      // Troca de direção: tangente zero fixa o extremo local no próprio ponto.
      tangents.push(0)
    } else {
      const dx = dxs[i]!
      const dxNext = dxs[i + 1]!
      const common = dx + dxNext
      tangents.push((3 * common) / ((common + dxNext) / m + (common + dx) / next))
    }
  }
  tangents.push(slopes[n - 2]!)
  return tangents
}

/** Caminho SVG cúbico monotônico pelos pontos já em coordenadas de tela. */
function monotonePath(xs: number[], ys: number[]): string {
  const n = xs.length
  if (n === 0) return ''
  if (n === 1) return `M ${xs[0]} ${ys[0]}`

  const tangents = monotoneTangents(xs, ys)
  let path = `M ${xs[0]} ${ys[0]}`
  for (let i = 0; i < n - 1; i += 1) {
    const dx = (xs[i + 1]! - xs[i]!) / 3
    path +=
      ` C ${xs[i]! + dx} ${ys[i]! + tangents[i]! * dx}` +
      ` ${xs[i + 1]! - dx} ${ys[i + 1]! - tangents[i + 1]! * dx}` +
      ` ${xs[i + 1]} ${ys[i + 1]}`
  }
  return path
}

// -------------------------------------------------------------------------
// Escalas e eixos
// -------------------------------------------------------------------------

/**
 * Marcações "redondas" para o eixo Y: passos de 1, 2, 5 ou 10 vezes potência de 10.
 *
 * Sem isso os rótulos sairiam em valores como 1037,4183 — tecnicamente certos e
 * ilegíveis.
 *
 * O passo é arredondado **para cima**, e essa direção é o ponto todo: arredondar
 * para baixo satisfaz o "valor redondo" e estoura a contagem de marcações. Numa
 * série de 1.000 a 1.150 com alvo de 4, o passo bruto de 42,75 arredondado para
 * baixo dá 20 — nove marcações onde caberiam quatro, e num gráfico de 110 px de
 * altura elas saíam empilhadas ("1160 1150 1140 1130...").
 */
export function niceTicks(min: number, max: number, alvo = 4): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return [min]
  const passoBruto = (max - min) / Math.max(1, alvo)
  const magnitude = 10 ** Math.floor(Math.log10(passoBruto))
  const normalizado = passoBruto / magnitude
  const escala = [1, 2, 5, 10].find((candidato) => candidato >= normalizado - 1e-9) ?? 10
  const passo = magnitude * escala

  const ticks: number[] = []
  // A tolerância evita perder o último tick por erro de ponto flutuante.
  for (let v = Math.ceil(min / passo) * passo; v <= max + passo * 1e-9; v += passo) {
    ticks.push(v)
  }
  return ticks.length > 0 ? ticks : [min]
}

/**
 * Casas decimais necessárias para dois ticks vizinhos não virarem o mesmo texto.
 *
 * O padrão anterior era `toFixed(0)`, e ele é correto exatamente enquanto o
 * patrimônio é grande. Com os 29,29 USDC de hoje e a carteira toda em caixa, a
 * série é constante, o passo calculado é 0,2 e os três rótulos do eixo saem
 * "29", "29", "29" — um eixo que não mede nada. O número de casas tem que vir
 * do PASSO, não de um palpite sobre a ordem de grandeza dos valores.
 */
export function casasParaPasso(passo: number): number {
  if (!Number.isFinite(passo) || passo <= 0) return 2
  // Uma casa a mais que a primeira significativa do passo cobre 0,2 -> "29,2"
  // sem inflar 200 -> "200,0".
  const casas = Math.ceil(-Math.log10(passo))
  return Math.min(8, Math.max(0, casas))
}

/** Altura mínima confortável para um rótulo de eixo, em pixels. */
const ALTURA_POR_ROTULO = 30

/** Largura aproximada de um rótulo de 11px, em pixels. */
const LARGURA_POR_CARACTERE = 6.1

/** Folga horizontal mínima entre duas caixas de rótulo vizinhas, em pixels. */
const FOLGA_ENTRE_ROTULOS = 12

/**
 * Espaço que um rótulo pede à sua volta, medido em pixels de tick a tick.
 *
 * A conta anterior era `maisLargo + 14`, e ela vale só para rótulo **centrado**:
 * duas caixas centradas nos ticks deixam `passo − largura` de folga. Mas o
 * primeiro tick usa `text-anchor="start"` e o último `"end"`, e essas caixas
 * ficam inteiras de um lado do tick — a folga contra o vizinho centrado cai para
 * `passo − 1,5 × largura`. Medido com `getBBox` no backtest antes do conserto:
 * folgas `[8,9  22,9  22,9 …]`, o primeiro par com um terço da folga dos demais;
 * no dashboard, com "07/09 09/09", a folga real era **6,9 px** e os dois rótulos
 * liam como um borrão.
 *
 * Reservar `1,5 × largura` cobre os três arranjos possíveis (start→middle,
 * middle→middle, middle→end) com a mesma folga mínima.
 */
function espacoPorRotulo(maisLargo: number): number {
  return maisLargo * 1.5 + FOLGA_ENTRE_ROTULOS
}

const MINUTO_MS = 60 * 1000
const HORA_MS = 60 * MINUTO_MS
const DIA_MS = 24 * HORA_MS

/**
 * Passos de tempo que um humano lê como redondos.
 *
 * Um eixo de tempo tem que andar em unidades de tempo, não no que sobrar da
 * divisão. Sem isto, uma grade perfeitamente uniforme em pixels sai
 * `03/08 05/08 08/08 11/08 13/08` — passo real constante de 60 horas, que
 * arredondado para dia alterna 2 e 3 e se lê como escala irregular. Snapando o
 * passo para 3 dias, os rótulos andam de 3 em 3.
 */
const PASSOS_DE_TEMPO = [
  MINUTO_MS,
  2 * MINUTO_MS,
  5 * MINUTO_MS,
  10 * MINUTO_MS,
  15 * MINUTO_MS,
  30 * MINUTO_MS,
  HORA_MS,
  2 * HORA_MS,
  3 * HORA_MS,
  6 * HORA_MS,
  12 * HORA_MS,
  DIA_MS,
  2 * DIA_MS,
  3 * DIA_MS,
  7 * DIA_MS,
  14 * DIA_MS,
  30 * DIA_MS,
  90 * DIA_MS,
  180 * DIA_MS,
  365 * DIA_MS,
]

/**
 * Amostragem típica da série: a MEDIANA dos intervalos, não a média.
 *
 * A média é enganada pelos dois desvios que a série real de patrimônio tem: o
 * Portfolio Agent grava dois pontos a milissegundos de distância quando o
 * sistema sobe, e há buracos de horas entre uma execução e a seguinte. Medido na
 * série de hoje, a média dava ~205 s onde a cadência de verdade é 60 s. A
 * mediana ignora os dois extremos e acerta a cadência.
 */
export function amostragemTipica(instantes: number[]): number {
  const todas: number[] = []
  for (let i = 1; i < instantes.length; i += 1) {
    const d = instantes[i]! - instantes[i - 1]!
    if (Number.isFinite(d) && d > 0) todas.push(d)
  }
  // Intervalo abaixo de um segundo é retrato duplicado (o agente grava um ponto
  // ao subir e outro no primeiro tique do laço), não cadência. Deixá-los na
  // conta puxava a mediana para 30 s numa série gravada de minuto em minuto, e
  // o eixo perdia metade das marcações. Se TUDO for sub-segundo, aí é a
  // cadência de verdade e vale.
  const cadencia = todas.filter((d) => d >= 1000)
  const amostras = cadencia.length > 0 ? cadencia : todas
  if (amostras.length === 0) return 0
  amostras.sort((a, b) => a - b)
  const meio = Math.floor(amostras.length / 2)
  return amostras.length % 2 === 1
    ? amostras[meio]!
    : (amostras[meio - 1]! + amostras[meio]!) / 2
}

/**
 * Passos de índice que caem em passos de tempo redondos, do menor aceitável
 * para cima.
 *
 * O eixo é indexado por AMOSTRA, não por instante — é o que mantém o passo em
 * pixels constante. Converter o passo de tempo pela cadência da série é o que
 * aproxima os dois: para série regular (um candle por hora no backtest) fica
 * exato, e para série com buracos fica no melhor possível sem furar a grade.
 */
export function passosTemporais(
  passoMinimo: number,
  instantes: number[],
  maximoIndice: number,
): number[] {
  const n = instantes.length
  if (n < 2) return []
  const porIndice = amostragemTipica(instantes)
  if (!(porIndice > 0)) return []

  const passos: number[] = []
  for (const candidato of PASSOS_DE_TEMPO) {
    const passo = Math.round(candidato / porIndice)
    if (passo < passoMinimo || passo > maximoIndice) continue
    if (passos[passos.length - 1] !== passo) passos.push(passo)
  }
  return passos
}

/**
 * Índices do eixo X: grade de passo CONSTANTE, ancorada no último ponto.
 *
 * Duas coisas que a versão anterior errava, as duas medidas no navegador:
 *
 * 1. **Colapso.** Ela montava a grade e depois removia os índices cujo rótulo
 *    repetia o vizinho. Com a série real de hoje (404 pontos dentro de um dia,
 *    rótulos "dd/mm" todos iguais) a remoção comia o eixo inteiro e sobrava
 *    **um** rótulo, encostado na borda direita — um gráfico sem eixo de tempo.
 *    Aqui o desempate é no PASSO: ele cresce até os rótulos da grade serem
 *    distintos, em vez de furar a grade. (A causa raiz do rótulo repetido é a
 *    granularidade, resolvida em `rotuladorDeSerie`; isto é a rede de proteção.)
 *
 * 2. **Passo irregular.** Remover um índice deixava os sobreviventes nas
 *    posições originais, e o eixo saía `18/08, 19/08, 21/08` — um salto de 1 dia
 *    no meio de um ritmo de 2 dias. Eixo de tempo com passo irregular mente
 *    sobre a escala. Ancorar no fim e caminhar para trás com passo fixo dá uma
 *    grade uniforme e mantém o último ponto rotulado, que é o que ancora a
 *    leitura ("até quando vai a série").
 *
 * A largura do rótulo é estimada por contagem de caracteres em vez de medida no
 * DOM: medir exigiria renderizar para depois decidir o que renderizar, e o erro
 * de uma fonte proporcional é absorvido pela folga.
 */
export function tickIndices(
  rotulos: string[],
  largura: number,
  gapMinimo: number,
  instantes?: number[],
): number[] {
  const total = rotulos.length
  if (total === 0) return []
  if (total === 1) return [0]

  const maisLargo = Math.max(...rotulos.map((r) => r.length)) * LARGURA_POR_CARACTERE
  const gap = Math.max(gapMinimo, espacoPorRotulo(maisLargo))
  const maximo = Math.max(1, Math.floor(largura / gap))
  const passoInicial = Math.max(1, Math.ceil((total - 1) / maximo))

  const grade = (passo: number): number[] => {
    const indices: number[] = []
    for (let i = total - 1; i >= 0; i -= passo) indices.push(i)
    return indices.reverse()
  }
  const rotulosDistintos = (indices: number[]): boolean =>
    indices.every((indice, i) => i === 0 || rotulos[indice] !== rotulos[indices[i - 1]!])

  // Passos redondos de tempo primeiro; só se nenhum deles separar os textos é
  // que se aceita um passo qualquer (que ainda é uniforme, só não é redondo).
  const candidatos = instantes ? passosTemporais(passoInicial, instantes, total - 1) : []
  for (let passo = passoInicial; passo <= total - 1; passo += 1) candidatos.push(passo)

  for (const passo of candidatos) {
    const indices = grade(passo)
    if (indices.length < 2) continue
    if (rotulosDistintos(indices)) return indices
  }

  // Nenhum passo separa os textos: a série inteira cai no mesmo rótulo (dois
  // pontos no mesmo instante, por exemplo). Duas marcações com o mesmo texto
  // não medem nada, então sobra a que ancora o fim da série.
  return [total - 1]
}

/** Largura disponível do container, observada de verdade (sem polling). */
function useLarguraMedida(): [React.RefObject<HTMLDivElement>, number] {
  const ref = useRef<HTMLDivElement>(null)
  const [largura, setLargura] = useState(0)

  useEffect(() => {
    const alvo = ref.current
    if (!alvo) return
    // `ResizeObserver` em vez de evento de resize da janela: o gráfico está
    // dentro de um grid que muda de largura sem a janela mudar de tamanho.
    const observador = new ResizeObserver((entradas) => {
      const nova = entradas[0]?.contentRect.width ?? 0
      setLargura((atual) => (Math.abs(nova - atual) > 0.5 ? nova : atual))
    })
    observador.observe(alvo)
    setLargura(alvo.clientWidth)
    return () => observador.disconnect()
  }, [])

  return [ref, largura]
}

// -------------------------------------------------------------------------
// Série temporal
// -------------------------------------------------------------------------

export function TimeSeriesChart({
  data,
  height = 260,
  fill = false,
  formatY,
  formatValue,
  valueLabel,
  minTickGap = 40,
}: {
  data: SeriesPoint[]
  height?: number
  /** Preenche a área sob a curva com um gradiente. */
  fill?: boolean
  /** Omitido: as casas decimais saem do passo do eixo (ver `casasParaPasso`). */
  formatY?: (value: number) => string
  formatValue: (value: number) => string
  valueLabel: string
  minTickGap?: number
}) {
  const [ref, largura] = useLarguraMedida()
  const [ativo, setAtivo] = useState<number | null>(null)

  const margem = { top: 6, right: 6, bottom: 22, left: 62 }
  const plotW = Math.max(0, largura - margem.left - margem.right)
  const plotH = Math.max(0, height - margem.top - margem.bottom)

  // Sem largura medida ainda: reserva o espaço para não haver salto de layout.
  if (largura === 0 || data.length === 0) {
    return <div ref={ref} style={{ width: '100%', height }} />
  }

  const valores = data.map((p) => p.value)
  const bruto = { min: Math.min(...valores), max: Math.max(...valores) }
  // Escala focada na variação real: começar em zero achataria a curva e
  // esconderia justamente o que interessa observar.
  let min = bruto.min - Math.abs(bruto.min) * 0.01
  let max = bruto.max + Math.abs(bruto.max) * 0.01
  if (max === min) {
    // Série constante: sem esta folga a divisão pelo intervalo seria por zero e
    // a linha desapareceria em vez de aparecer reta no meio.
    const folga = Math.abs(max) * 0.01 || 1
    min -= folga
    max += folga
  }

  const escalaX = (i: number) =>
    margem.left + (data.length === 1 ? plotW / 2 : (i / (data.length - 1)) * plotW)
  const escalaY = (v: number) => margem.top + plotH - ((v - min) / (max - min)) * plotH

  const xs = data.map((_, i) => escalaX(i))
  const ys = data.map((p) => escalaY(p.value))
  const caminho = monotonePath(xs, ys)
  const gradiente = `equity-${fill ? 'area' : 'line'}`

  const ticksY = niceTicks(min, max, Math.min(5, Math.max(2, Math.floor(plotH / ALTURA_POR_ROTULO))))
  // O passo real do eixo, não o intervalo dos dados, é o que decide as casas.
  const passoY = ticksY.length > 1 ? Math.abs(ticksY[1]! - ticksY[0]!) : Math.abs(max - min)
  const casasY = casasParaPasso(passoY)
  const rotularY =
    formatY ??
    ((valor: number) =>
      // Separador decimal em pt-BR, igual ao resto da tela: um eixo com "29.2"
      // ao lado de um card com "29,29" parece dado de outra origem.
      valor.toLocaleString('pt-BR', {
        minimumFractionDigits: casasY,
        maximumFractionDigits: casasY,
      }))
  // A granularidade do eixo X sai do INTERVALO REAL da série, não de uma faixa
  // escolhida em outro lugar da tela. Não existe mais prop `formatX`: enquanto
  // ela existia, o dashboard passava "dd/mm" para uma série de seis horas
  // porque o botão de faixa estava em 30d, e o eixo colapsava para um rótulo.
  const rotularX = rotuladorDeSerie(data.map((p) => p.time))
  const rotulosX = data.map((p) => rotularX(p.time))
  const instantes = data.map((p) => Date.parse(p.time))
  const ticksX = tickIndices(rotulosX, plotW, minTickGap, instantes)

  const aoMover = (evento: React.MouseEvent<SVGSVGElement>) => {
    const caixa = evento.currentTarget.getBoundingClientRect()
    const x = evento.clientX - caixa.left
    if (data.length === 1) return setAtivo(0)
    const proporcao = (x - margem.left) / plotW
    const indice = Math.round(proporcao * (data.length - 1))
    setAtivo(Math.min(data.length - 1, Math.max(0, indice)))
  }

  const ponto = ativo === null ? null : data[ativo]

  return (
    <div ref={ref} style={{ position: 'relative', width: '100%' }}>
      <svg
        width={largura}
        height={height}
        onMouseMove={aoMover}
        onMouseLeave={() => setAtivo(null)}
        style={{ display: 'block', overflow: 'visible' }}
      >
        {fill && (
          <defs>
            <linearGradient id={gradiente} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={LINE} stopOpacity={0.35} />
              <stop offset="100%" stopColor={LINE} stopOpacity={0} />
            </linearGradient>
          </defs>
        )}

        {/* Linhas de grade horizontais: dão ao olho uma régua para comparar dois
            instantes distantes da série sem seguir a curva com o dedo. É o que
            todo terminal de mercado desenha, e o custo aqui é um <line>. */}
        {ticksY.map((valor) => (
          <line
            key={`grade-${valor}`}
            x1={margem.left}
            x2={margem.left + plotW}
            y1={escalaY(valor)}
            y2={escalaY(valor)}
            stroke="var(--border)"
            strokeWidth={1}
            opacity={0.55}
            shapeRendering="crispEdges"
          />
        ))}

        {ticksY.map((valor) => (
          <text
            key={valor}
            x={margem.left - 8}
            y={escalaY(valor)}
            textAnchor="end"
            dominantBaseline="middle"
            fontSize={11}
            fill={AXIS}
          >
            {rotularY(valor)}
          </text>
        ))}

        {/* Grade vertical nas mesmas marcações do eixo X: é o que permite ler
            "quanto valia às 21h" sem descer o dedo da curva até o rótulo. */}
        {ticksX.map((i) => (
          <line
            key={`grade-x-${i}`}
            x1={escalaX(i)}
            x2={escalaX(i)}
            y1={margem.top}
            y2={margem.top + plotH}
            stroke="var(--border)"
            strokeWidth={1}
            opacity={0.4}
            shapeRendering="crispEdges"
          />
        ))}

        {ticksX.map((i) => (
          <text
            key={i}
            x={escalaX(i)}
            y={height - 6}
            textAnchor={i === 0 ? 'start' : i === data.length - 1 ? 'end' : 'middle'}
            fontSize={11}
            fill={AXIS}
          >
            {rotulosX[i]}
          </text>
        ))}

        {fill && caminho && (
          <path
            d={`${caminho} L ${xs[xs.length - 1]} ${margem.top + plotH} L ${xs[0]} ${
              margem.top + plotH
            } Z`}
            fill={`url(#${gradiente})`}
          />
        )}
        <path d={caminho} fill="none" stroke={LINE} strokeWidth={2} />
        {data.length === 1 && <circle cx={xs[0]} cy={ys[0]} r={3} fill={LINE} />}

        {ativo !== null && (
          <>
            <line
              x1={xs[ativo]}
              x2={xs[ativo]}
              y1={margem.top}
              y2={margem.top + plotH}
              stroke="var(--border)"
              strokeWidth={1}
            />
            <circle
              cx={xs[ativo]}
              cy={ys[ativo]}
              r={3.5}
              fill={LINE}
              stroke="var(--bg-elevated)"
              strokeWidth={2}
            />
          </>
        )}
      </svg>

      {ponto && (
        <div
          className="chart-tooltip"
          style={{
            // Ancora à esquerda ou à direita do cursor conforme a metade em que
            // ele está, para a caixa nunca sair do card.
            left: xs[ativo!]! < largura / 2 ? xs[ativo!]! + 12 : undefined,
            right: xs[ativo!]! >= largura / 2 ? largura - xs[ativo!]! + 12 : undefined,
            top: 6,
          }}
        >
          <div className="chart-tooltip-label">
            {new Date(ponto.time).toLocaleString('pt-BR')}
          </div>
          <div>
            <span className="num">{formatValue(ponto.value)}</span>{' '}
            <span className="faint">{valueLabel}</span>
          </div>
        </div>
      )}
    </div>
  )
}

// -------------------------------------------------------------------------
// Rosca
// -------------------------------------------------------------------------

export type DonutSlice = { label: string; value: number; color: string }

/**
 * Barra de composição: a alternativa honesta ao donut de fatia única.
 *
 * Um anel de 180 px de lado para informar "100,00%" gasta um quarto da largura
 * do card e não diz nada que o número ao lado já não diga — e é exatamente o que
 * a tela mostra no estado real de hoje, com o patrimônio todo em caixa. Uma
 * barra cheia comunica a mesma coisa em 8 px de altura e deixa o resto do card
 * para o que varia.
 */
export function CompositionBar({ data }: { data: DonutSlice[] }) {
  const total = data.reduce((soma, s) => soma + s.value, 0)
  if (data.length === 0 || total <= 0) return null
  return (
    <div className="composition-bar">
      {data.map((slice) => (
        <span
          key={slice.label}
          title={slice.label}
          style={{ background: slice.color, width: `${(slice.value / total) * 100}%` }}
        />
      ))}
    </div>
  )
}

/** Ponto na circunferência. Ângulo em radianos, medido a partir do topo. */
function naBorda(cx: number, cy: number, raio: number, angulo: number) {
  return [cx + raio * Math.sin(angulo), cy - raio * Math.cos(angulo)]
}

export function DonutChart({
  data,
  size = 180,
  innerRadius = 48,
  outerRadius = 78,
  formatValue,
}: {
  data: DonutSlice[]
  size?: number
  innerRadius?: number
  outerRadius?: number
  formatValue: (value: number) => string
}) {
  const [ativo, setAtivo] = useState<number | null>(null)

  const total = data.reduce((soma, s) => soma + s.value, 0)
  if (data.length === 0 || total <= 0) return null

  const centro = size / 2
  // Espaçamento só faz sentido com mais de uma fatia: num círculo completo ele
  // abriria uma fenda no próprio setor.
  const vao = data.length > 1 ? 0.03 : 0

  let inicio = 0
  const setores = data.map((slice, indice) => {
    const varredura = (slice.value / total) * Math.PI * 2
    const de = inicio + vao / 2
    const ate = inicio + varredura - vao / 2
    inicio += varredura

    // Uma fatia única cobre o círculo inteiro, e um arco de 360° tem começo e
    // fim no mesmo ponto — o caminho fica degenerado e nada é pintado. Dois
    // anéis concêntricos resolvem sem caso especial no desenho.
    if (data.length === 1) {
      return { slice, indice, d: anelCompleto(centro, innerRadius, outerRadius) }
    }

    const [x1, y1] = naBorda(centro, centro, outerRadius, de)
    const [x2, y2] = naBorda(centro, centro, outerRadius, ate)
    const [x3, y3] = naBorda(centro, centro, innerRadius, ate)
    const [x4, y4] = naBorda(centro, centro, innerRadius, de)
    const maior = ate - de > Math.PI ? 1 : 0

    return {
      slice,
      indice,
      d:
        `M ${x1} ${y1} A ${outerRadius} ${outerRadius} 0 ${maior} 1 ${x2} ${y2}` +
        ` L ${x3} ${y3} A ${innerRadius} ${innerRadius} 0 ${maior} 0 ${x4} ${y4} Z`,
    }
  })

  const emFoco = ativo === null ? null : data[ativo]

  return (
    <div style={{ position: 'relative', width: size, height: size }}>
      <svg width={size} height={size} style={{ display: 'block' }}>
        {setores.map(({ slice, indice, d }) => (
          <path
            key={slice.label}
            d={d}
            fill={slice.color}
            opacity={ativo === null || ativo === indice ? 1 : 0.45}
            onMouseEnter={() => setAtivo(indice)}
            onMouseLeave={() => setAtivo(null)}
          />
        ))}
      </svg>

      {emFoco && (
        <div
          className="chart-tooltip"
          style={{ left: '50%', top: '50%', transform: 'translate(-50%, -50%)' }}
        >
          <div className="chart-tooltip-label">{emFoco.label}</div>
          <div className="num">{formatValue(emFoco.value)}</div>
        </div>
      )}
    </div>
  )
}

/** Anel fechado, para o caso de fatia única (arco de 360° não desenha). */
function anelCompleto(centro: number, interno: number, externo: number): string {
  return (
    `M ${centro} ${centro - externo}` +
    ` A ${externo} ${externo} 0 1 1 ${centro} ${centro + externo}` +
    ` A ${externo} ${externo} 0 1 1 ${centro} ${centro - externo} Z` +
    ` M ${centro} ${centro - interno}` +
    ` A ${interno} ${interno} 0 1 0 ${centro} ${centro + interno}` +
    ` A ${interno} ${interno} 0 1 0 ${centro} ${centro - interno} Z`
  )
}
