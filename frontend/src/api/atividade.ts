/**
 * Feed "Atividade em tempo real": historico do banco + WebSocket, sem repetir.
 *
 * O feed era so o que o WebSocket entregava com a tela aberta: ao recarregar,
 * sumia, e com o sistema em `dry_run` sem sinais o operador via "Aguardando
 * eventos" sem saber se o sistema tinha subido, parado ou disparado a trava de
 * madrugada. Agora a tela le `GET /api/atividade` (mesma forma da mensagem do
 * WebSocket) e poe o tempo real por cima.
 *
 * O mesmo evento chega pelos dois caminhos com frequencia: o historico e
 * revalidado a cada 30 s e a cada reconexao, e tudo que o WebSocket entregou
 * desde a ultima leitura ja esta no banco. Por isso cada evento tem uma CHAVE
 * que nao depende do caminho por onde veio (`chaveDoEvento`).
 *
 * Funcoes puras de proposito: `checks/telas.check.ts` as chama com os payloads
 * reais das duas fontes.
 */

import { money, percent, price, quantity, quoteOf } from './format'

export interface ItemDoFeed {
  event: string
  data: any
  /** Instante do evento (ISO, com fuso), do servidor quando ele informa. */
  at: string
}

/**
 * Distancia maxima entre o alerta do WebSocket e o registro do mesmo episodio
 * no `audit_log`. O alerta nao tem id e o registro nao guarda o horario do
 * alerta: os dois sao gravados no mesmo trecho de codigo, com milissegundos de
 * diferenca. Dez segundos cobrem um banco lento sem juntar dois episodios
 * reais (o mesmo alerta nao se repete em segundos).
 */
export const JANELA_DO_MESMO_ALERTA_MS = 10_000

/**
 * Instante de um evento recebido pelo WebSocket.
 *
 * Era o relogio do navegador na chegada. Misturado com o historico, que traz o
 * horario do servidor, a ordem do feed passaria a depender do atraso da rede e
 * do relogio da maquina. O payload ja carrega o instante (`created_at` em sinal
 * e decisao, `timestamp` em ordem, retrato e alerta); a chegada e so o reserva.
 */
export function instanteDoEvento(data: any, chegada: Date): string {
  const candidato = data?.created_at ?? data?.timestamp
  if (typeof candidato === 'string' && !Number.isNaN(Date.parse(candidato))) return candidato
  return chegada.toISOString()
}

/** Chave que identifica o evento venha ele do historico ou do WebSocket. */
export function chaveDoEvento(item: ItemDoFeed): string {
  const { event, data } = item
  switch (event) {
    case 'signal':
    case 'risk_assessment':
      if (data?.id) return `${event}:${data.id}`
      break
    case 'order_result':
      // Uma ordem pode ter mais de um resultado (aberta, depois executada):
      // cada estado e um evento.
      if (data?.order_request_id) return `${event}:${data.order_request_id}:${data.status}`
      break
    case 'portfolio':
      // Um retrato por minuto: so o mais recente interessa (ver juntarAtividade).
      return 'portfolio'
    case 'alert':
    case 'system':
      return `${event}:${data?.type}:${item.at}`
  }
  return `${event}:${item.at}:${JSON.stringify(data)}`
}

/**
 * Junta o historico e o que chegou ao vivo: mais novo primeiro, sem repetir.
 *
 * - O que veio ao vivo tem preferencia (o alerta do WebSocket traz a mensagem
 *   inteira; o registro do banco, so o motivo).
 * - Alerta repetido e o mesmo `type` a menos de `JANELA_DO_MESMO_ALERTA_MS`.
 * - Retrato de portfolio: so o mais recente. Chega um por minuto e, guardado
 *   como os outros, empurrava o historico para fora do feed em 50 minutos.
 */
export function juntarAtividade(
  historico: ItemDoFeed[],
  aoVivo: ItemDoFeed[],
  maximo: number,
): ItemDoFeed[] {
  const vistos = new Set<string>()
  const alertas: { tipo: unknown; ms: number }[] = []
  let retrato: ItemDoFeed | null = null
  const juntos: ItemDoFeed[] = []

  for (const item of [...aoVivo, ...historico]) {
    if (item.event === 'portfolio') {
      if (!retrato || Date.parse(item.at) > Date.parse(retrato.at)) retrato = item
      continue
    }
    const chave = chaveDoEvento(item)
    if (vistos.has(chave)) continue
    if (item.event === 'alert') {
      const ms = Date.parse(item.at)
      const repetido = alertas.some(
        (outro) =>
          outro.tipo === item.data?.type && Math.abs(outro.ms - ms) <= JANELA_DO_MESMO_ALERTA_MS,
      )
      if (repetido) continue
      alertas.push({ tipo: item.data?.type, ms })
    }
    vistos.add(chave)
    juntos.push(item)
  }
  if (retrato) juntos.push(retrato)

  return juntos.sort((a, b) => Date.parse(b.at) - Date.parse(a.at)).slice(0, maximo)
}

/**
 * Hora do item no feed: so a hora quando e de hoje, data e hora quando nao.
 *
 * Com o feed so ao vivo tudo era de hoje. O historico traz a parada de ontem a
 * noite, e "22:14" sozinho diria que foi hoje.
 */
export function horaDoFeed(at: string, agora: Date): string {
  const instante = new Date(at)
  const mesmoDia = instante.toDateString() === agora.toDateString()
  return instante.toLocaleString('pt-BR', {
    ...(mesmoDia ? {} : { day: '2-digit', month: '2-digit' }),
    hour: '2-digit',
    minute: '2-digit',
  })
}

const ROTULO_DO_MODO: Record<string, string> = {
  dry_run: 'simulação',
  testnet: 'testnet',
  live: 'dinheiro real',
}

/** Converte o payload (do WebSocket ou do historico) em uma frase legivel. */
export function descreverEvento(event: string, data: any, moeda: string): string {
  switch (event) {
    case 'signal':
      return `Sinal ${data.direction} em ${data.symbol} por ${data.strategy} (confiança ${percent(
        data.confidence,
        0,
      )}) — ${data.reason}`
    case 'risk_assessment': {
      // O par mora no retrato (`format.parDoEvento`); sem ele, a moeda vigente.
      const par = typeof data.snapshot?.symbol === 'string' ? data.snapshot.symbol : ''
      const em = par ? ` em ${par}` : ''
      return data.decision === 'approved'
        ? `Risk Manager aprovou${em} ${money(data.approved_notional ?? '0', quoteOf(par) || moeda)}`
        : `Risk Manager rejeitou${em}: ${(data.reasons ?? []).join(' · ')}`
    }
    case 'order_result': {
      const par = data.symbol ? ` ${data.symbol}` : ''
      return `Ordem ${data.status}${par} — ${quantity(data.filled_quantity)} @ ${
        data.average_price ? price(data.average_price) : '—'
      }${data.error ? ` (${data.error})` : ''}`
    }
    case 'portfolio':
      // O percentual que estava aqui era `unrealized_pnl / max(total_value, 1)`:
      // uma DIVISAO de dinheiro por dinheiro em float (proibida por D7) e,
      // pior, com o denominador travado em 1 — numa carteira abaixo de 1 a
      // conta devolvia o proprio PnL disfarcado de percentual. O backend nao
      // manda essa razao neste evento, entao a frase mostra o que ele manda.
      return `Patrimônio atualizado: ${money(data.total_value, moeda)} · ${money(
        data.unrealized_pnl ?? '0',
        moeda,
      )} não realizado`
    case 'alert': {
      // `title` e o que todo alerta traz; o `type` cru ("unclean_shutdown") era
      // o que a tela mostrava quando o alerta nao tinha `reason`.
      const titulo = data.title ?? data.type
      return data.reason && data.reason !== titulo
        ? `ALERTA: ${titulo} — ${data.reason}`
        : `ALERTA: ${titulo}`
    }
    case 'system':
      return descreverMarco(data)
    default:
      return event
  }
}

function descreverMarco(data: any): string {
  const motivo = data.detail ? `: ${data.detail}` : ''
  const quem = data.actor && data.actor !== 'system' ? ` (${data.actor})` : ''
  switch (data.type) {
    case 'system_started':
      return `Sistema iniciado${data.mode ? ` em ${ROTULO_DO_MODO[data.mode] ?? data.mode}` : ''}`
    case 'system_stopped':
      return `Sistema parado${motivo}`
    case 'all_agents_paused':
      return `Todos os agentes pausados${quem}${motivo}`
    case 'all_agents_resumed':
      return `Todos os agentes retomados${quem}`
    case 'agent_paused':
      return `Agente ${data.target} pausado${quem}${motivo}`
    case 'agent_resumed':
      return `Agente ${data.target} retomado${quem}`
    case 'execution_openings_blocked':
      return `Aberturas de posição bloqueadas${quem}${motivo}`
    default:
      return `Sistema: ${data.type}`
  }
}
