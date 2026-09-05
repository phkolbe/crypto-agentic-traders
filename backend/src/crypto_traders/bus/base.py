"""Contrato do event bus interno.

Os agentes conversam exclusivamente por este contrato. Trocar o backend
(in-process hoje, Redis Streams quando o Docker entrar) nao toca em nenhum agente.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from ..domain.models import (
    Candle,
    Heartbeat,
    OrderRequest,
    OrderResult,
    PortfolioSnapshot,
    RiskAssessment,
    Signal,
)


class Topics:
    """Nomes dos canais. Constantes para nao espalhar strings soltas pelo codigo."""

    CANDLES = "market.candles"
    TICKERS = "market.tickers"
    SIGNALS = "strategy.signals"
    RISK_ASSESSMENTS = "risk.assessments"
    ORDER_REQUESTS = "risk.order_requests"
    ORDER_RESULTS = "execution.order_results"
    PORTFOLIO_SNAPSHOTS = "portfolio.snapshots"
    HEARTBEATS = "system.heartbeats"
    ALERTS = "system.alerts"


#: Mapeia topico -> classe do payload, para o backend serializar/desserializar
#: sem que os agentes precisem se preocupar com isso.
TOPIC_MODELS: dict[str, type] = {
    Topics.CANDLES: Candle,
    Topics.SIGNALS: Signal,
    Topics.RISK_ASSESSMENTS: RiskAssessment,
    Topics.ORDER_REQUESTS: OrderRequest,
    Topics.ORDER_RESULTS: OrderResult,
    Topics.PORTFOLIO_SNAPSHOTS: PortfolioSnapshot,
    Topics.HEARTBEATS: Heartbeat,
}

Handler = Callable[[Any], Awaitable[None]]


class EventBus(abc.ABC):
    """Pub/sub assincrono com multiplos consumidores por topico."""

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def publish(self, topic: str, payload: Any) -> None: ...

    @abc.abstractmethod
    def subscribe(self, topic: str) -> AsyncIterator[Any]:
        """Fluxo dedicado de eventos do topico.

        Cada chamada cria uma assinatura independente: dois agentes inscritos no
        mesmo topico recebem cada um a sua copia do evento (fan-out), e nao
        metade dos eventos cada um.
        """
        ...
