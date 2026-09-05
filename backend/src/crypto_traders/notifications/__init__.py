"""Alertas para eventos criticos.

Circuit breaker acionado, agente travado, falha de execucao. Sem isso, um
sistema que roda sozinho falha sozinho e voce descobre depois.

O Telegram e opcional: sem token configurado, os alertas so vao para o log
(`NullNotifier`) e nada quebra.
"""

from __future__ import annotations

import abc

import httpx

from ..config import Settings
from ..logging_setup import get_logger

log = get_logger(__name__)


class Notifier(abc.ABC):
    @abc.abstractmethod
    async def send(self, title: str, body: str) -> None: ...


class NullNotifier(Notifier):
    """Sem canal externo configurado: registra e segue."""

    async def send(self, title: str, body: str) -> None:
        log.warning("alert", title=title, body=body)


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    async def send(self, title: str, body: str) -> None:
        log.warning("alert", title=title, body=body)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    self._url,
                    json={"chat_id": self._chat_id, "text": f"*{title}*\n\n{body}",
                          "parse_mode": "Markdown"},
                )
        except Exception as exc:
            # Falha ao notificar nunca pode derrubar o sistema de trading: o
            # alerta ja esta no log, que e a fonte de verdade da auditoria.
            log.error("alert.delivery_failed", error=str(exc))


def build_notifier(settings: Settings) -> Notifier:
    if settings.telegram_bot_token and settings.telegram_chat_id:
        return TelegramNotifier(
            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id
        )
    return NullNotifier()


__all__ = ["Notifier", "NullNotifier", "TelegramNotifier", "build_notifier"]
