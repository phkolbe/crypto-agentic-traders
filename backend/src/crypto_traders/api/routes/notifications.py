"""Rotas de notificacao: liga/desliga dos canais, destinatarios e teste de envio."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ...agents.orchestrator import Orchestrator
from ...db.repositories import AuditLogRepository, NotificationConfigRepository
from ...notifications import MultiChannelNotifier
from ..deps import db_session, orchestrator_dep, settings_dep
from ..schemas import (
    NotificationChannelStatus,
    NotificationConfigIn,
    NotificationConfigOut,
    NotificationTestOut,
)

router = APIRouter()


def _channel_status(notifier: MultiChannelNotifier, name: str) -> NotificationChannelStatus:
    channel = notifier.channels[name]
    return NotificationChannelStatus(
        configured=channel.configured, missing_settings=channel.missing_settings()
    )


@router.get("/notifications/config", response_model=NotificationConfigOut)
async def get_notification_config(
    session: AsyncSession = Depends(db_session),
    settings=Depends(settings_dep),
) -> NotificationConfigOut:
    """Preferencias dos canais e o que ainda falta no `.env`.

    Os segredos (senha SMTP, token da Meta) nunca sao devolvidos -- a resposta
    diz apenas se estao presentes e, quando nao, quais variaveis faltam.
    """
    config = await NotificationConfigRepository(session).get_or_create()
    notifier = MultiChannelNotifier(settings)
    return NotificationConfigOut(
        email_enabled=config.email_enabled,
        email_to=config.email_to,
        whatsapp_enabled=config.whatsapp_enabled,
        whatsapp_to=config.whatsapp_to,
        email=_channel_status(notifier, "email"),
        whatsapp=_channel_status(notifier, "whatsapp"),
    )


@router.put("/notifications/config", response_model=NotificationConfigOut)
async def update_notification_config(
    payload: NotificationConfigIn,
    session: AsyncSession = Depends(db_session),
    settings=Depends(settings_dep),
) -> NotificationConfigOut:
    """Atualiza canais e destinatarios.

    Ligar um canal sem destinatario e recusado: um canal "ligado" que nunca
    entrega e pior do que um desligado, porque cria a impressao de cobertura.
    """
    values = payload.model_dump(exclude_none=True)
    repository = NotificationConfigRepository(session)
    current = await repository.get_or_create()

    email_to = values.get("email_to", current.email_to)
    whatsapp_to = values.get("whatsapp_to", current.whatsapp_to)

    if values.get("email_enabled", current.email_enabled) and not email_to:
        raise HTTPException(
            status_code=422, detail="informe o e-mail de destino para ativar o canal"
        )
    if values.get("whatsapp_enabled", current.whatsapp_enabled) and not whatsapp_to:
        raise HTTPException(
            status_code=422, detail="informe o numero de WhatsApp para ativar o canal"
        )

    before = {
        "email_enabled": current.email_enabled,
        "whatsapp_enabled": current.whatsapp_enabled,
    }
    config = await repository.update(values)
    await AuditLogRepository(session).append(
        action="notification_config_updated",
        actor="user",
        target="notification_config",
        before=before,
        after={
            "email_enabled": config.email_enabled,
            "whatsapp_enabled": config.whatsapp_enabled,
        },
    )

    notifier = MultiChannelNotifier(settings)
    return NotificationConfigOut(
        email_enabled=config.email_enabled,
        email_to=config.email_to,
        whatsapp_enabled=config.whatsapp_enabled,
        whatsapp_to=config.whatsapp_to,
        email=_channel_status(notifier, "email"),
        whatsapp=_channel_status(notifier, "whatsapp"),
    )


@router.post("/notifications/test", response_model=NotificationTestOut)
async def send_test_notification(
    orchestrator: Orchestrator = Depends(orchestrator_dep),
) -> NotificationTestOut:
    """Dispara um alerta de teste pelos canais ligados.

    Existe para o operador nao descobrir que a notificacao nao funciona
    justamente no dia em que o circuit breaker dispara.
    """
    notifier = orchestrator.notifier
    if not isinstance(notifier, MultiChannelNotifier):
        raise HTTPException(status_code=503, detail="notificador nao disponivel")

    results = await notifier.send_test()
    if not results:
        return NotificationTestOut(
            results=[],
            detail="Nenhum canal ligado. O alerta foi apenas registrado no log.",
        )
    return NotificationTestOut(
        results=[
            {"channel": r.channel, "ok": r.ok, "detail": r.detail} for r in results
        ],
        detail="",
    )
