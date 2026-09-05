"""Event bus sobre Redis Streams.

Ainda nao e o backend padrao (o Docker nao esta instalado na maquina local),
mas implementa o mesmo contrato `EventBus`. Para migrar basta subir o
`docker compose` e trocar duas linhas no `.env`:

    EVENT_BUS=redis
    REDIS_URL=redis://localhost:6379/0

Nenhum agente muda. A vantagem sobre o bus in-process e o replay: os eventos
ficam no stream e podem ser reprocessados para auditoria ou para reconstruir o
estado apos um crash.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..logging_setup import get_logger
from .base import TOPIC_MODELS, EventBus

log = get_logger(__name__)

#: Numero maximo aproximado de eventos retidos por stream (~ alguns dias de operacao).
MAX_STREAM_LENGTH = 100_000


class RedisStreamsEventBus(EventBus):
    def __init__(self, url: str, consumer_group: str = "crypto-traders") -> None:
        self._url = url
        self._group = consumer_group
        self._redis: Any = None

    async def start(self) -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - caminho de configuracao
            raise RuntimeError(
                "EVENT_BUS=redis exige a dependencia opcional: pip install -e '.[redis]'"
            ) from exc

        self._redis = Redis.from_url(self._url, decode_responses=True)
        await self._redis.ping()
        log.info("event_bus.redis.connected", url=self._url)

    async def stop(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def publish(self, topic: str, payload: Any) -> None:
        if self._redis is None:
            raise RuntimeError("EventBus nao iniciado: chame start() antes de publish()")
        body = (
            payload.model_dump_json()
            if hasattr(payload, "model_dump_json")
            else json.dumps(payload)
        )
        await self._redis.xadd(topic, {"data": body}, maxlen=MAX_STREAM_LENGTH, approximate=True)

    async def subscribe(self, topic: str) -> AsyncIterator[Any]:
        """Le o stream a partir de agora (`$`), em fan-out.

        Cada assinante mantem seu proprio cursor local, o que reproduz a
        semantica do bus in-process: todos recebem todos os eventos.
        """
        if self._redis is None:
            raise RuntimeError("EventBus nao iniciado: chame start() antes de subscribe()")

        model = TOPIC_MODELS.get(topic)
        last_id = "$"
        while True:
            response = await self._redis.xread({topic: last_id}, count=100, block=5000)
            if not response:
                continue
            for _stream, entries in response:
                for entry_id, fields in entries:
                    last_id = entry_id
                    raw = json.loads(fields["data"])
                    yield model.model_validate(raw) if model else raw
