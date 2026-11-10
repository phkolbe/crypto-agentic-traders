"""Alertas para eventos criticos.

Circuit breaker acionado, acesso a exchange negado, agente travado. Sem isso, um
sistema que roda sozinho falha sozinho e voce descobre depois.

Dois canais, cada um com liga/desliga proprio na interface: **e-mail** (SMTP) e
**WhatsApp** (Meta Cloud API). Nenhum canal configurado nao e erro -- os alertas
continuam indo para o log, que e a fonte de verdade da auditoria.
"""

from __future__ import annotations

import abc
import asyncio

from ..config import Settings
from ..db.repositories import NotificationConfigRepository
from ..db.session import session_scope
from ..logging_setup import get_logger
from .channels import (
    DeliveryResult,
    EmailChannel,
    NotificationChannel,
    WhatsAppChannel,
    sanitize_template_param,
)

log = get_logger(__name__)

#: Quanto o alerta espera pela leitura dos destinatarios no banco. Postgres que nao
#: responde (conexao presa, nao recusada) seguraria o alerta pelo prazo do driver.
PRAZO_LEITURA_CONFIG_S = 5.0

#: (canal, ligado, destinatario)
Alvo = tuple[str, bool, str]

__all__ = [
    "DeliveryResult",
    "EmailChannel",
    "MultiChannelNotifier",
    "NotificationChannel",
    "Notifier",
    "NullNotifier",
    "WhatsAppChannel",
    "build_notifier",
    "sanitize_template_param",
]


class Notifier(abc.ABC):
    @abc.abstractmethod
    async def send(self, title: str, body: str) -> list[DeliveryResult]: ...


class NullNotifier(Notifier):
    """Sem canal externo: registra e segue."""

    async def send(self, title: str, body: str) -> list[DeliveryResult]:
        log.warning("alert", title=title, body=body)
        return []


class MultiChannelNotifier(Notifier):
    """Dispara para todos os canais ligados.

    A configuracao e lida do banco **a cada envio**, e nao no start: ligar ou
    desligar um canal pela interface passa a valer na hora, sem reiniciar o
    sistema. O custo e uma leitura no banco por alerta, e alertas sao raros.

    Sem o banco, o alerta sai assim mesmo. Antes a leitura levantava ANTES de
    qualquer canal, e justamente o alerta de "sem acesso ao banco" (Postgres
    morto pelo limite de memoria) nao saia, com os segredos e o canal no
    ambiente. Agora a ultima configuracao lida com sucesso fica em memoria; se
    o processo nunca leu (subiu com o banco fora), valem os destinatarios de
    reserva do ambiente (`ALERTA_EMAIL_RESERVA`, `ALERTA_WHATSAPP_RESERVA`).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.channels: dict[str, NotificationChannel] = {
            "email": EmailChannel(settings),
            "whatsapp": WhatsAppChannel(settings),
        }
        self._ultima_config: list[Alvo] | None = None

    async def _ler_config(self) -> list[Alvo]:
        async with session_scope(self._settings) as session:
            config = await NotificationConfigRepository(session).get_or_create()
            return [
                ("email", bool(config.email_enabled), config.email_to or ""),
                ("whatsapp", bool(config.whatsapp_enabled), config.whatsapp_to or ""),
            ]

    def _reserva(self) -> list[Alvo]:
        s = self._settings
        return [
            (nome, True, destino)
            for nome, destino in (
                ("email", s.alerta_email_reserva.strip()),
                ("whatsapp", s.alerta_whatsapp_reserva.strip()),
            )
            if destino
        ]

    async def _alvos(self) -> list[Alvo]:
        try:
            alvos = await asyncio.wait_for(self._ler_config(), PRAZO_LEITURA_CONFIG_S)
        except Exception as exc:
            if self._ultima_config is not None:
                alvos, origem = self._ultima_config, "ultima configuracao lida"
            else:
                alvos = self._reserva()
                origem = "reserva do ambiente" if alvos else "nenhum (so o log)"
            log.error(
                "notification.config_indisponivel",
                error=str(exc) or type(exc).__name__,
                destinatarios=origem,
                detail="banco sem resposta; o alerta segue com os destinatarios conhecidos",
            )
            return alvos
        self._ultima_config = alvos
        return alvos

    async def send(self, title: str, body: str) -> list[DeliveryResult]:
        # O log vem primeiro e sempre: se toda entrega falhar, o evento ainda
        # existe em algum lugar.
        log.warning("alert", title=title, body=body)

        targets = await self._alvos()

        results: list[DeliveryResult] = []
        for name, enabled, destination in targets:
            if not enabled:
                continue
            channel = self.channels[name]
            try:
                results.append(await channel.send(title, body, destination or ""))
            except Exception as exc:
                # Um canal quebrado nao pode impedir o outro de avisar.
                log.error("notification.channel_crashed", channel=name, error=str(exc))
                results.append(DeliveryResult(name, False, str(exc)))
        return results

    async def send_test(self) -> list[DeliveryResult]:
        """Testa os canais ligados, para o operador nao descobrir que o alerta
        nao funciona justamente no dia em que ele importa."""
        return await self.send(
            "Teste de notificacao",
            "Se voce recebeu esta mensagem, o canal esta funcionando. "
            "Alertas reais avisam sobre circuit breaker acionado, perda de acesso "
            "a exchange e agente travado.",
        )


def build_notifier(settings: Settings) -> Notifier:
    return MultiChannelNotifier(settings)
