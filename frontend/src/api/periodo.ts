/**
 * Periodo dos graficos: atalhos (24h a 1 ano) ou um intervalo de datas escolhido.
 *
 * Funcoes puras, verificadas em `checks/telas.check.ts`. O backend recusa janela
 * maior que 366 dias (422); a tela valida antes, para o erro aparecer no campo e
 * nao como grafico vazio.
 */

export type Periodo =
  | { tipo: 'atalho'; dias: number }
  | { tipo: 'personalizado'; de: string; ate: string } // YYYY-MM-DD, datas locais

export const ATALHOS = [
  { dias: 1, rotulo: '24h' },
  { dias: 7, rotulo: '7d' },
  { dias: 30, rotulo: '30d' },
  { dias: 90, rotulo: '90d' },
  { dias: 365, rotulo: '1 ano' },
] as const

/** O limite do backend: 1 ano, com folga para ano bissexto. */
export const DIAS_MAXIMOS = 366

const DIA_MS = 24 * 60 * 60 * 1000

/** `YYYY-MM-DD` de uma data, no fuso LOCAL (o do `<input type="date">`). */
export function dataLocal(data: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${data.getFullYear()}-${p(data.getMonth() + 1)}-${p(data.getDate())}`
}

function inicioDoDia(texto: string): Date {
  const [a, m, d] = texto.split('-').map(Number)
  return new Date(a!, m! - 1, d!, 0, 0, 0, 0)
}

/**
 * O erro do intervalo personalizado, ou `null` se ele vale.
 *
 * "Ate" inclui o dia inteiro: de 01/10 a 01/10 e um dia, nao zero. Por isso o
 * limite compara `ate + 1 dia - de` com 366 dias.
 */
export function erroDoPersonalizado(de: string, ate: string, hoje: Date): string | null {
  if (!de || !ate) return 'escolha as duas datas'
  const inicio = inicioDoDia(de)
  const fim = inicioDoDia(ate)
  if (Number.isNaN(inicio.getTime()) || Number.isNaN(fim.getTime())) return 'data inválida'
  if (inicio > fim) return 'a data inicial é depois da final'
  if (fim > inicioDoDia(dataLocal(hoje))) return 'a data final é depois de hoje'
  const dias = Math.round((fim.getTime() + DIA_MS - inicio.getTime()) / DIA_MS)
  if (dias > DIAS_MAXIMOS) return `o período vai até 1 ano (escolhidos ${dias} dias)`
  return null
}

/** Os parametros `start`/`end` (ISO, UTC) que a API recebe. */
export function parametrosDoPeriodo(periodo: Periodo, agora: Date): { start: string; end: string } {
  if (periodo.tipo === 'atalho') {
    return {
      start: new Date(agora.getTime() - periodo.dias * DIA_MS).toISOString(),
      end: agora.toISOString(),
    }
  }
  const fim = new Date(inicioDoDia(periodo.ate).getTime() + DIA_MS - 1)
  return {
    start: inicioDoDia(periodo.de).toISOString(),
    // O dia de hoje ainda nao acabou: o fim e agora, nao 23:59 do futuro.
    end: (fim > agora ? agora : fim).toISOString(),
  }
}

/** Texto curto do periodo, para titulos e legendas. */
export function rotuloDoPeriodo(periodo: Periodo): string {
  if (periodo.tipo === 'atalho') {
    return ATALHOS.find((a) => a.dias === periodo.dias)?.rotulo ?? `${periodo.dias}d`
  }
  const br = (t: string) => t.split('-').reverse().join('/')
  return `${br(periodo.de)} a ${br(periodo.ate)}`
}

/**
 * Chave de cache estavel para o React Query.
 *
 * Um atalho NAO pode usar o `start`/`end` calculado: eles mudam a cada
 * milissegundo, e cada renderizacao viraria uma consulta nova.
 */
export function chaveDoPeriodo(periodo: Periodo): string {
  return periodo.tipo === 'atalho' ? `d${periodo.dias}` : `${periodo.de}_${periodo.ate}`
}

/** Le o periodo salvo (por tela), aceitando so formas validas. */
export function lerPeriodoSalvo(texto: string | null, padrao: Periodo, hoje: Date): Periodo {
  if (!texto) return padrao
  try {
    const valor = JSON.parse(texto) as Periodo
    if (valor?.tipo === 'atalho' && ATALHOS.some((a) => a.dias === valor.dias)) return valor
    if (valor?.tipo === 'personalizado' && erroDoPersonalizado(valor.de, valor.ate, hoje) === null) {
      return valor
    }
  } catch {
    // Valor corrompido no armazenamento local: o padrao vale.
  }
  return padrao
}
