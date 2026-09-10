import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { ApiError, api } from '../api/client'
import { dateTime, percent, relativeTime, timeOnly } from '../api/format'
import type { Signal, StrategyInfo } from '../api/types'
import {
  AgentsOffline,
  Card,
  CircuitBreakerBanner,
  Empty,
  Loading,
  ModeBanner,
  SizingBanner,
  useHealth,
} from '../components/Shared'

const AGENT_ROLES: Record<string, string> = {
  market_data: 'Coleta candles e preços. Não tem credenciais nem poder de gastar.',
  strategy: 'Calcula indicadores e emite sinais. Nunca executa ordens.',
  risk_manager: 'Guardião: valida todo sinal e anexa stop-loss e take-profit.',
  execution: 'Único componente autorizado a enviar ordens à exchange.',
  portfolio: 'Apura saldos, PnL e grava os snapshots do gráfico.',
}

export default function Agents() {
  const queryClient = useQueryClient()
  const [message, setMessage] = useState<string | null>(null)
  const health = useHealth()

  const strategies = useQuery<StrategyInfo[]>({
    queryKey: ['strategies'],
    queryFn: api.strategies,
  })
  const signals = useQuery<Signal[]>({ queryKey: ['signals'], queryFn: () => api.signals(20) })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['health'] })
  const onError = (error: unknown) =>
    setMessage(error instanceof ApiError ? error.message : String(error))

  const pause = useMutation({ mutationFn: api.pauseAgent, onSuccess: invalidate, onError })
  const resume = useMutation({ mutationFn: api.resumeAgent, onSuccess: invalidate, onError })
  const pauseAll = useMutation({ mutationFn: api.pauseAll, onSuccess: invalidate, onError })
  const resumeAll = useMutation({ mutationFn: api.resumeAll, onSuccess: invalidate, onError })
  const resetBreaker = useMutation({
    mutationFn: api.resetCircuitBreaker,
    onSuccess: invalidate,
    onError,
  })

  if (health.isError) {
    const error = health.error
    if (error instanceof ApiError && error.agentsAreDown) return <AgentsOffline />
  }
  if (health.isLoading) return <Loading />

  const agents = Object.entries(health.data?.agents ?? {})
  const anyPaused = agents.some(([, status]) => status.paused)

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Agentes</h1>
          <p>Estado de cada agente, controle de pausa e os sinais mais recentes.</p>
        </div>
        <div className="row-tight">
          {anyPaused ? (
            <button
              className="primary"
              onClick={() => resumeAll.mutate()}
              disabled={resumeAll.isPending || health.data?.circuit_breaker_active}
              title={
                health.data?.circuit_breaker_active
                  ? 'Rearme o circuit breaker antes de retomar'
                  : undefined
              }
            >
              Retomar todos
            </button>
          ) : (
            <button className="danger" onClick={() => pauseAll.mutate()} disabled={pauseAll.isPending}>
              Pausar decisões
            </button>
          )}
        </div>
      </div>

      <ModeBanner health={health.data} />
      <SizingBanner health={health.data} />
      <CircuitBreakerBanner
        active={health.data?.circuit_breaker_active ?? false}
        onReset={() => resetBreaker.mutate()}
        resetting={resetBreaker.isPending}
      />

      {message && <div className="form-message err" style={{ marginBottom: 14 }}>{message}</div>}

      <div className="grid grid-2">
        <Card title="Estado dos agentes">
          {agents.map(([name, status]) => (
            <div className="agent-row" key={name}>
              <div style={{ minWidth: 0 }}>
                <div className="row-tight">
                  <span className="agent-name">{name}</span>
                  <span
                    className={`badge ${
                      status.stale || status.state === 'error'
                        ? 'badge-negative'
                        : status.paused
                          ? 'badge-warning'
                          : 'badge-positive'
                    }`}
                  >
                    {status.state === 'error'
                      ? 'erro'
                      : status.stale
                        ? 'sem heartbeat'
                        : status.paused
                          ? 'pausado'
                          : 'rodando'}
                  </span>
                </div>
                <div className="faint" style={{ fontSize: 12, marginTop: 2 }}>
                  {AGENT_ROLES[name] ?? ''}
                </div>
                <div className="faint" style={{ fontSize: 11, marginTop: 2 }}>
                  último sinal de vida: {relativeTime(status.last_heartbeat)}
                  {status.last_error ? ` · erro: ${status.last_error}` : ''}
                </div>
              </div>
              <button
                className="small"
                onClick={() =>
                  status.paused ? resume.mutate(name) : pause.mutate(name)
                }
              >
                {status.paused ? 'retomar' : 'pausar'}
              </button>
            </div>
          ))}
          <div className="faint" style={{ fontSize: 12, marginTop: 12 }}>
            Pausar as decisões mantém o Market Data Agent rodando de propósito: sem preço
            atualizado o dashboard congela justamente quando você mais precisa olhar para ele.
          </div>
        </Card>

        <Card title="Estratégias">
          {strategies.isLoading ? (
            <Loading />
          ) : (
            <div className="stack" style={{ gap: 10 }}>
              {(strategies.data ?? []).map((strategy) => (
                <div key={strategy.name} className="row-tight" style={{ alignItems: 'flex-start' }}>
                  <span className={`badge ${strategy.active ? 'badge-positive' : 'badge-neutral'}`}>
                    {strategy.active ? 'ativa' : 'inativa'}
                  </span>
                  <div style={{ minWidth: 0 }}>
                    <div className="mono">{strategy.name}</div>
                    <div className="faint" style={{ fontSize: 12 }}>{strategy.description}</div>
                  </div>
                </div>
              ))}
              {/* O texto anterior mandava editar STRATEGIES no `.env`. Alem de
                  desatualizado, era instrucao para quebrar o sistema: variavel de
                  negocio no arquivo de ambiente faz o backend RECUSAR SUBIR
                  (D15). O lugar certo e a tela de Configuracoes, e vale na hora. */}
              <div className="faint" style={{ fontSize: 12, marginTop: 4 }}>
                As estratégias ativas moram no banco e são editadas em{' '}
                <Link to="/config">Configurações</Link> — valem no próximo candle fechado,
                sem reiniciar. Não coloque <span className="mono">STRATEGIES</span> no{' '}
                <span className="mono">.env</span>: variável de negócio nesse arquivo faz o
                sistema recusar subir.
              </div>
            </div>
          )}
        </Card>
      </div>

      <div style={{ marginTop: 14 }}>
        <Card title="Sinais recentes">
          {signals.isLoading ? (
            <Loading />
          ) : (signals.data ?? []).length === 0 ? (
            <Empty>
              Nenhum sinal gerado ainda. As estratégias só decidem sobre candles fechados, e não
              operar é o resultado normal na maioria deles.
            </Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Quando</th>
                    <th>Par</th>
                    <th>Estratégia</th>
                    <th>Direção</th>
                    <th className="right">Confiança</th>
                    <th>Motivo</th>
                  </tr>
                </thead>
                <tbody>
                  {(signals.data ?? []).map((signal) => (
                    <tr key={signal.id} title={dateTime(signal.created_at)}>
                      <td className="muted">{timeOnly(signal.created_at)}</td>
                      <td><strong>{signal.symbol}</strong></td>
                      <td className="mono">{signal.strategy}</td>
                      <td>
                        <span
                          className={`badge ${
                            signal.direction === 'long' ? 'badge-positive' : 'badge-neutral'
                          }`}
                        >
                          {signal.direction === 'long' ? 'comprar' : 'fechar'}
                        </span>
                      </td>
                      <td className="right num">{percent(signal.confidence, 0)}</td>
                      <td className="muted" style={{ maxWidth: 460 }}>{signal.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  )
}
