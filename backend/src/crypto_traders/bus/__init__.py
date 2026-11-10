"""Event bus interno: contrato unico, backends intercambiaveis."""

from __future__ import annotations

from ..config import Settings
from ..domain.enums import TradingMode
from .base import EventBus, Topics
from .memory import InMemoryEventBus

__all__ = ["EventBus", "InMemoryEventBus", "Topics", "build_event_bus"]


def build_event_bus(settings: Settings) -> EventBus:
    """Escolhe o backend conforme `EVENT_BUS` no `.env`.

    Redis em `live` e RECUSADO: aquele backend entrega no-maximo-uma-vez (sem
    consumer group nem `XACK`; ver `bus/redis_streams.py`), e o barramento em
    memoria com a caixa de entrada duravel dos agentes da uma garantia melhor
    para quem opera dinheiro de verdade. Recusar na subida, com o motivo, e
    melhor que descobrir na primeira venda de protecao perdida numa reconexao.
    """
    if settings.event_bus == "redis":
        if settings.trading_mode is TradingMode.LIVE:
            raise RuntimeError(
                "EVENT_BUS=redis nao e aceito em TRADING_MODE=live: o backend Redis "
                "Streams entrega no-maximo-uma-vez (sem consumer group nem XACK), "
                "entao um evento publicado durante uma reconexao ou reinicio de "
                "agente -- inclusive uma venda de protecao -- e perdido. Use "
                "EVENT_BUS=memory em live."
            )
        from .redis_streams import RedisStreamsEventBus

        return RedisStreamsEventBus(settings.redis_url)
    return InMemoryEventBus()
