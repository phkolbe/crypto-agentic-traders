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
function niceTicks(min: number, max: number, alvo = 4): number[] {
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

/** Altura mínima confortável para um rótulo de eixo, em pixels. */
const ALTURA_POR_ROTULO = 30

/** Largura aproximada de um rótulo de 11px, em pixels. */
const LARGURA_POR_CARACTERE = 6.1

/**
 * Índices do eixo X que cabem sem os rótulos se encavalarem.
 *
 * O espaçamento mínimo pedido é um piso, não a regra: quem manda é a largura do
 * **rótulo mais largo**. Ignorar isso foi o primeiro erro visível desta
 * substituição — com 500 pontos e datas como "08 de set.", um `minTickGap` de 50
 * px deixava os rótulos a 50 px de distância e 55 px de largura, e o eixo saía
 * como "08 de set.10 de set.".
 *
 * A largura é estimada por contagem de caracteres em vez de medida no DOM: medir
 * exigiria renderizar para depois decidir o que renderizar, e o erro de uma fonte
 * proporcional é absorvido pela folga.
 */
function tickIndices(rotulos: string[], largura: number, gapMinimo: number): number[] {
  const total = rotulos.length
  if (total === 0) return []
  if (total === 1) return [0]

  const maisLargo = Math.max(...rotulos.map((r) => r.length)) * LARGURA_POR_CARACTERE
  const gap = Math.max(gapMinimo, maisLargo + 14)
  const maximo = Math.max(1, Math.floor(largura / gap))
  const passo = Math.max(1, Math.ceil((total - 1) / maximo))

  const indices: number[] = []
  for (let i = 0; i < total; i += passo) indices.push(i)

  // O último ponto ancora a leitura ("até quando vai a série"), então entra
  // sempre. Se o rótulo anterior ficaria colado nele, esse anterior sai.
  //
  // O critério é em PIXELS, não em passos: o resto da divisão faz a distância
  // até o último ponto ser qualquer coisa entre um passo e zero, e medir isso em
  // "meio passo" ainda deixava "27 de set." grudado em "29 de set.".
  const ultimo = total - 1
  if (indices[indices.length - 1] !== ultimo) {
    const distanciaEmPixels = ((ultimo - indices[indices.length - 1]!) / (total - 1)) * largura
    if (distanciaEmPixels < gap) indices.pop()
    indices.push(ultimo)
  }
  return indices
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
  formatX,
  formatY = (v) => v.toFixed(0),
  formatValue,
  valueLabel,
  minTickGap = 40,
}: {
  data: SeriesPoint[]
  height?: number
  /** Preenche a área sob a curva com um gradiente. */
  fill?: boolean
  formatX: (time: string) => string
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
  const ticksX = tickIndices(
    data.map((p) => formatX(p.time)),
    plotW,
    minTickGap,
  )

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
            {formatY(valor)}
          </text>
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
            {formatX(data[i]!.time)}
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
