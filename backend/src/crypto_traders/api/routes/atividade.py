"""Historico do feed "Atividade em tempo real".

O feed do Dashboard era so o que o WebSocket entregava com a tela aberta: ao
recarregar, sumia, e com o sistema em `dry_run` sem sinais o operador via
"Aguardando eventos" sem saber se o sistema tinha subido, parado ou disparado o
circuit breaker de madrugada. Esta rota junta o que ja esta gravado -- sinais,
decisoes de risco, ordens com resultado e o `audit_log` (alertas que deixam
rastro e os marcos de subida/parada/pausa) -- na forma da mensagem do WebSocket,
e a tela poe o tempo real por cima.

So leitura, pelos repositorios existentes; o portao de acesso vale como para
qualquer rota (`app.PortaoTailscale` envolve o app inteiro).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.repositories import (
    AuditLogRepository,
    OrderRepository,
    RiskEventRepository,
    SignalRepository,
)
from ...domain.enums import OrderStatus, RiskEventType
from ..deps import db_session
from ..schemas import AtividadeOut, OrderOut, RiskEventOut, SignalOut

router = APIRouter()

#: Acoes do `audit_log` que viram ALERTA no feed, com o `type` do alerta que o
#: agente publica no barramento no mesmo episodio e o titulo dele. O `type` igual
#: e o que deixa a tela reconhecer o mesmo episodio vindo pelo WebSocket (o
#: alerta) e por aqui (o registro). A maioria dos alertas nao deixa registro
#: nenhum no banco -- so estes tem o que mostrar depois de recarregar.
ALERTAS_DO_AUDIT: dict[str, tuple[str, str]] = {
    "circuit_breaker_tripped": ("circuit_breaker", "Circuit breaker acionado"),
    "circuit_breaker_reset": ("circuit_breaker_reset", "Circuit breaker rearmado"),
    "opening_refused": ("opening_refused", "Abertura de posicao recusada -- trava ativa"),
    "opening_without_stop_loss": ("opening_without_stop_loss", "Abertura sem stop-loss"),
    "order_outcome_unknown": ("order_outcome_unknown", "Resultado de ordem desconhecido"),
    "position_trapped": ("position_trapped", "Posicao presa: a exchange recusa a venda"),
    "order_below_exchange_filter": (
        "order_below_exchange_filter",
        "Ordem abaixo do filtro da exchange",
    ),
    "protective_exit_emitted": ("protective_exit", "Saida de protecao emitida"),
    "incomplete_approval_blocked": (
        "incomplete_approval_blocked",
        "Aprovacao sem stop ou alvo bloqueada",
    ),
}

#: Marcos do sistema: subida, parada e pausas. Nao passam pelo barramento, entao
#: so existem no feed por esta rota.
MARCOS_DO_SISTEMA = frozenset(
    {
        "system_started",
        "system_stopped",
        "all_agents_paused",
        "all_agents_resumed",
        "agent_paused",
        "agent_resumed",
        "execution_openings_blocked",
    }
)

#: Quantas entradas recentes do `audit_log` varrer atras de alertas e marcos. O
#: repositorio so lista por tempo (nao filtra por acao), e a trilha mistura acoes
#: administrativas e avisos do razao do Portfolio. 500 e o teto das outras
#: listas da API; marco mais antigo que isso ja nao e "atividade recente".
JANELA_DO_AUDIT = 500


def _utc(valor: datetime) -> datetime:
    """SQLite devolve sem fuso; sem isto a tela leria o horario como local."""
    return valor if valor.tzinfo else valor.replace(tzinfo=UTC)


def _sinal(row) -> AtividadeOut:
    return AtividadeOut(
        event="signal",
        at=_utc(row.created_at),
        data=SignalOut.model_validate(row).model_dump(mode="json"),
    )


def _decisao(row) -> AtividadeOut:
    data = RiskEventOut.model_validate(row).model_dump(mode="json")
    # O retrato guarda o estado inteiro da carteira na hora da decisao; a tela
    # so usa o par (`format.parDoEvento`). Cinquenta retratos inteiros pesariam
    # a resposta sem nada aparecer a mais.
    simbolo = (row.snapshot or {}).get("symbol")
    data["snapshot"] = {"symbol": simbolo} if simbolo else {}
    return AtividadeOut(event="risk_assessment", at=_utc(row.created_at), data=data)


def _ordem(row) -> AtividadeOut:
    data = OrderOut.model_validate(row).model_dump(mode="json")
    # `order_request_id` e o nome do id no `OrderResult` do WebSocket.
    data["order_request_id"] = row.id
    instante = _utc(row.updated_at or row.created_at)
    data["timestamp"] = instante.isoformat()
    return AtividadeOut(event="order_result", at=instante, data=data)


def _do_audit(row) -> AtividadeOut | None:
    instante = _utc(row.timestamp)
    if row.action in ALERTAS_DO_AUDIT:
        tipo, titulo = ALERTAS_DO_AUDIT[row.action]
        data: dict[str, Any] = {
            "type": tipo,
            "title": titulo,
            "reason": row.detail,
            "target": row.target,
            "timestamp": instante.isoformat(),
        }
        return AtividadeOut(event="alert", at=instante, data=data)
    if row.action in MARCOS_DO_SISTEMA:
        data = {
            "type": row.action,
            "actor": row.actor,
            "target": row.target,
            "detail": row.detail,
            "mode": (row.after or {}).get("mode"),
            "timestamp": instante.isoformat(),
        }
        return AtividadeOut(event="system", at=instante, data=data)
    return None


@router.get("/atividade", response_model=list[AtividadeOut])
async def list_atividade(
    limite: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(db_session),
) -> list[AtividadeOut]:
    """Os ultimos eventos do feed, do mais novo para o mais antigo.

    Cada fonte contribui com ate `limite` itens e o corte final fica com os
    `limite` mais recentes do conjunto. Ficam de fora: retratos de portfolio (um
    por minuto; o tempo real ja mostra o atual), ordens ainda pendentes (o
    WebSocket so publica resultado) e eventos de risco que nao sao decisao sobre
    sinal (o circuit breaker chega como alerta, pelo `audit_log`).
    """
    itens: list[AtividadeOut] = []
    itens.extend(_sinal(row) for row in await SignalRepository(session).list(limit=limite))
    itens.extend(
        _decisao(row)
        for row in await RiskEventRepository(session).list(limit=limite)
        if row.event_type == str(RiskEventType.SIGNAL_EVALUATED)
    )
    itens.extend(
        _ordem(row)
        for row in await OrderRepository(session).list(limit=limite)
        if row.status != str(OrderStatus.PENDING)
    )
    for row in await AuditLogRepository(session).list(limit=JANELA_DO_AUDIT):
        item = _do_audit(row)
        if item is not None:
            itens.append(item)

    itens.sort(key=lambda item: item.at, reverse=True)
    return itens[:limite]
