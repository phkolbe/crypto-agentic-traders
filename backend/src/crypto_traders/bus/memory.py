"""Event bus in-process (asyncio).

Backend padrao enquanto todo o sistema roda em um unico processo na maquina
local. Mesma semantica de fan-out do Redis Streams, sem exigir infraestrutura.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from ..logging_setup import get_logger
from .base import EventBus

log = get_logger(__name__)

#: Sentinela que encerra os iteradores dos assinantes no shutdown.
_STOP = object()


class InMemoryEventBus(EventBus):
    """Fan-out por filas independentes, uma por assinante."""

    def __init__(self, max_queue_size: int = 1000) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[Any]]] = {}
        self._max_queue_size = max_queue_size
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        for queues in self._subscribers.values():
            for queue in queues:
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(_STOP)
        self._subscribers.clear()

    async def publish(self, topic: str, payload: Any) -> None:
        for queue in list(self._subscribers.get(topic, [])):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Um consumidor lento nao pode travar os produtores nem os demais
                # consumidores: descartamos o evento mais antigo e seguimos.
                log.warning("event_bus.queue_full", topic=topic, dropped="oldest")
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(payload)

    async def subscribe(self, topic: str) -> AsyncIterator[Any]:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._max_queue_size)
        self._subscribers.setdefault(topic, []).append(queue)
        try:
            while True:
                item = await queue.get()
                if item is _STOP:
                    break
                yield item
        finally:
            subscribers = self._subscribers.get(topic, [])
            if queue in subscribers:
                subscribers.remove(queue)

    @property
    def subscriber_count(self) -> dict[str, int]:
        return {topic: len(queues) for topic, queues in self._subscribers.items()}
