import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { ApiError, api } from '../api/client'
import type { NotificationConfig, NotificationTestResult } from '../api/types'
import { Card, Loading } from '../components/Shared'

/** O que cada canal precisa no `.env` antes de poder ser ligado. */
const CHANNEL_HELP: Record<string, { title: string; setup: string }> = {
  email: {
    title: 'E-mail',
    setup:
      'Preencha SMTP_HOST, SMTP_USERNAME e SMTP_PASSWORD no .env. ' +
      'Com Gmail, use uma "senha de app" — não a senha da conta.',
  },
  whatsapp: {
    title: 'WhatsApp',
    setup:
      'Preencha WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN e ' +
      'WHATSAPP_TEMPLATE_NAME no .env, com um template aprovado pela Meta.',
  },
}

export default function Notifications() {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Partial<NotificationConfig>>({})
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const [results, setResults] = useState<NotificationTestResult[] | null>(null)

  const config = useQuery<NotificationConfig>({
    queryKey: ['notificationConfig'],
    queryFn: api.notificationConfig,
  })

  useEffect(() => {
    if (config.data) setDraft(config.data)
  }, [config.data])

  const save = useMutation({
    mutationFn: api.updateNotificationConfig,
    onSuccess: () => {
      setMessage({ ok: true, text: 'Canais atualizados.' })
      setResults(null)
      queryClient.invalidateQueries({ queryKey: ['notificationConfig'] })
      queryClient.invalidateQueries({ queryKey: ['audit'] })
    },
    onError: (error) =>
      setMessage({ ok: false, text: error instanceof ApiError ? error.message : String(error) }),
  })

  const test = useMutation({
    mutationFn: api.testNotification,
    onSuccess: (data) => {
      setResults(data.results)
      setMessage(data.detail ? { ok: false, text: data.detail } : null)
    },
    onError: (error) =>
      setMessage({ ok: false, text: error instanceof ApiError ? error.message : String(error) }),
  })

  if (config.isLoading) return <Loading />

  const current = config.data
  const changed =
    current &&
    (draft.email_enabled !== current.email_enabled ||
      draft.email_to !== current.email_to ||
      draft.whatsapp_enabled !== current.whatsapp_enabled ||
      draft.whatsapp_to !== current.whatsapp_to)

  const submit = () =>
    save.mutate({
      email_enabled: draft.email_enabled ?? false,
      email_to: draft.email_to || null,
      whatsapp_enabled: draft.whatsapp_enabled ?? false,
      whatsapp_to: draft.whatsapp_to || null,
    })

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Notificações</h1>
          <p>
            Para onde vão os alertas críticos: circuit breaker acionado, perda de acesso à
            exchange e agente travado.
          </p>
        </div>
        <button onClick={() => test.mutate()} disabled={test.isPending}>
          {test.isPending ? 'Enviando…' : 'Enviar teste'}
        </button>
      </div>

      {message && (
        <div className={`form-message ${message.ok ? 'ok' : 'err'}`} style={{ marginBottom: 14 }}>
          {message.text}
        </div>
      )}

      {results && results.length > 0 && (
        <Card title="Resultado do teste">
          <div className="stack" style={{ gap: 8 }}>
            {results.map((result) => (
              <div key={result.channel} className="row-tight">
                <span className={`badge ${result.ok ? 'badge-positive' : 'badge-negative'}`}>
                  {result.ok ? 'entregue' : 'falhou'}
                </span>
                <strong>{CHANNEL_HELP[result.channel]?.title ?? result.channel}</strong>
                <span className="muted" style={{ fontSize: 12 }}>
                  {result.detail}
                </span>
              </div>
            ))}
          </div>
        </Card>
      )}

      <div className="grid grid-2" style={{ marginTop: results ? 14 : 0 }}>
        <ChannelCard
          name="email"
          status={current?.email}
          enabled={draft.email_enabled ?? false}
          onToggle={(value) => setDraft({ ...draft, email_enabled: value })}
          label="E-mail de destino"
          placeholder="voce@exemplo.com"
          value={draft.email_to ?? ''}
          onChange={(value) => setDraft({ ...draft, email_to: value })}
          type="email"
        />

        <ChannelCard
          name="whatsapp"
          status={current?.whatsapp}
          enabled={draft.whatsapp_enabled ?? false}
          onToggle={(value) => setDraft({ ...draft, whatsapp_enabled: value })}
          label="Número de WhatsApp"
          placeholder="5511999999999"
          value={draft.whatsapp_to ?? ''}
          onChange={(value) => setDraft({ ...draft, whatsapp_to: value.replace(/\D/g, '') })}
          hint="Formato internacional, só dígitos: 55 + DDD + número."
        />
      </div>

      <div className="row-tight" style={{ marginTop: 16 }}>
        <button className="primary" onClick={submit} disabled={!changed || save.isPending}>
          {save.isPending ? 'Salvando…' : 'Salvar'}
        </button>
        {changed && <span className="muted" style={{ fontSize: 12 }}>alterações não salvas</span>}
      </div>

      <div style={{ marginTop: 18 }}>
        <Card title="Por que as senhas não ficam aqui">
          <p className="muted" style={{ margin: 0, fontSize: 13 }}>
            Senha de SMTP e token da Meta ficam apenas no arquivo <span className="mono">.env</span>,
            nunca no banco. Um backup do banco é um arquivo que circula — se ele carregasse
            credenciais, um histórico de negociações vazado viraria também um acesso vazado ao seu
            e-mail e ao seu WhatsApp. Esta tela controla o que <em>não</em> é segredo: ligar cada
            canal e para onde enviar.
          </p>
        </Card>
      </div>
    </>
  )
}

function ChannelCard({
  name,
  status,
  enabled,
  onToggle,
  label,
  placeholder,
  value,
  onChange,
  hint,
  type = 'text',
}: {
  name: string
  status?: { configured: boolean; missing_settings: string[] }
  enabled: boolean
  onToggle: (value: boolean) => void
  label: string
  placeholder: string
  value: string
  onChange: (value: string) => void
  hint?: string
  type?: string
}) {
  const help = CHANNEL_HELP[name]
  const ready = status?.configured ?? false

  return (
    <Card>
      <div className="row-tight" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <h2>{help?.title ?? name}</h2>
        <span className={`badge ${ready ? 'badge-positive' : 'badge-warning'}`}>
          {ready ? 'pronto' : 'falta configurar'}
        </span>
      </div>

      {!ready && (
        <div className="form-message err" style={{ marginBottom: 12, fontSize: 12 }}>
          {help?.setup}
          {status && status.missing_settings.length > 0 && (
            <>
              <br />
              <span className="mono">Faltando: {status.missing_settings.join(', ')}</span>
            </>
          )}
        </div>
      )}

      <label className="row-tight" style={{ cursor: ready ? 'pointer' : 'not-allowed', gap: 8 }}>
        <input
          type="checkbox"
          checked={enabled}
          disabled={!ready}
          onChange={(e) => onToggle(e.target.checked)}
          style={{ width: 'auto' }}
        />
        <span>Enviar alertas por este canal</span>
      </label>

      <div className="field" style={{ marginTop: 12 }}>
        <label>{label}</label>
        <input
          type={type}
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
        {hint && <span className="hint">{hint}</span>}
      </div>
    </Card>
  )
}
