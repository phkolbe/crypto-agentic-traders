/**
 * Formatacao para exibicao.
 *
 * Unico lugar onde `Money` (string) vira `number`. A conversao acontece no
 * ultimo instante possivel, so para renderizar.
 */

import type { Money } from './types'

const BRL_LOCALE = 'pt-BR'

export function toNumber(value: Money | null | undefined): number {
  if (value === null || value === undefined || value === '') return 0
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

export function money(value: Money | number | null | undefined, currency = 'USDT'): string {
  const amount = typeof value === 'number' ? value : toNumber(value)
  return `${amount.toLocaleString(BRL_LOCALE, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ${currency}`
}

/** Quantidades de cripto precisam de mais casas: 0.00 esconderia a posicao. */
export function quantity(value: Money | number | null | undefined): string {
  const amount = typeof value === 'number' ? value : toNumber(value)
  const digits = amount !== 0 && Math.abs(amount) < 1 ? 8 : 4
  return amount.toLocaleString(BRL_LOCALE, {
    minimumFractionDigits: 2,
    maximumFractionDigits: digits,
  })
}

export function price(value: Money | number | null | undefined): string {
  const amount = typeof value === 'number' ? value : toNumber(value)
  return amount.toLocaleString(BRL_LOCALE, {
    minimumFractionDigits: 2,
    maximumFractionDigits: amount < 10 ? 6 : 2,
  })
}

export function percent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toLocaleString(BRL_LOCALE, {
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
  return new Date(value).toLocaleString(BRL_LOCALE, {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function timeOnly(value: string): string {
  return new Date(value).toLocaleTimeString(BRL_LOCALE, {
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function shortDate(value: string): string {
  return new Date(value).toLocaleDateString(BRL_LOCALE, { day: '2-digit', month: '2-digit' })
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
