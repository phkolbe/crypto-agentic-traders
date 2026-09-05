/**
 * WebSocket de tempo real.
 *
 * Invalida as queries afetadas quando o backend anuncia um evento, em vez de
 * fazer polling curto: o dashboard atualiza no instante em que algo acontece,
 * sem bater na API a cada segundo.
 */

import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

export type LiveEvent =
  | { event: 'signal'; data: Record<string, unknown> }
  | { event: 'risk_assessment'; data: Record<string, unknown> }
  | { event: 'order_result'; data: Record<string, unknown> }
  | { event: 'portfolio'; data: Record<string, unknown> }
  | { event: 'alert'; data: Record<string, unknown> }

/** Queries a revalidar por tipo de evento. */
const INVALIDATES: Record<string, string[]> = {
  signal: ['signals'],
  risk_assessment: ['riskEvents'],
  order_result: ['trades', 'orders', 'portfolio'],
  portfolio: ['portfolio', 'portfolioHistory'],
  alert: ['health', 'riskConfig'],
}

const RECONNECT_DELAY_MS = 3000
const MAX_FEED = 50

export function useLiveEvents() {
  const queryClient = useQueryClient()
  const [connected, setConnected] = useState(false)
  const [feed, setFeed] = useState<{ event: string; data: any; at: string }[]>([])
  const socketRef = useRef<WebSocket | null>(null)
  const timerRef = useRef<number | null>(null)

  useEffect(() => {
    let disposed = false

    const connect = () => {
      if (disposed) return
      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const socket = new WebSocket(`${protocol}://${window.location.host}/ws`)
      socketRef.current = socket

      socket.onopen = () => setConnected(true)

      socket.onmessage = (message) => {
        try {
          const payload = JSON.parse(message.data) as LiveEvent
          for (const key of INVALIDATES[payload.event] ?? []) {
            queryClient.invalidateQueries({ queryKey: [key] })
          }
          setFeed((current) =>
            [{ event: payload.event, data: payload.data, at: new Date().toISOString() }, ...current].slice(
              0,
              MAX_FEED,
            ),
          )
        } catch {
          /* mensagem malformada: ignorar em vez de derrubar a conexao */
        }
      }

      socket.onclose = () => {
        setConnected(false)
        // O backend pode estar reiniciando; tentar de novo mantem o dashboard
        // vivo sem exigir refresh manual da pagina.
        if (!disposed) timerRef.current = window.setTimeout(connect, RECONNECT_DELAY_MS)
      }

      socket.onerror = () => socket.close()
    }

    connect()

    return () => {
      disposed = true
      if (timerRef.current) window.clearTimeout(timerRef.current)
      socketRef.current?.close()
    }
  }, [queryClient])

  return { connected, feed }
}
