"""Event bus sobre Redis Streams.

Ainda nao e o backend padrao, mas implementa o mesmo contrato `EventBus`:

    EVENT_BUS=redis
    REDIS_URL=redis://localhost:6379/0

**Entrega no-maximo-uma-vez**, e por isso recusado em `live`
(`bus.build_event_bus`). O docstring anterior prometia "replay para reconstruir o
estado apos um crash", e nada disso existia (auditoria de 09/11/2026,
orquestracao M2): nao ha consumer group, `XACK` nem `XPENDING`, entao o evento em
voo de um agente que morre nao volta, e uma assinatura refeita depois de queda
ou reinicio comeca do "agora" -- o que foi publicado no intervalo esta no stream
e nunca e lido. Para um `OrderRequest` de venda de protecao isso e um stop que
nao acontece.

O que este modulo garante: dentro de UMA assinatura viva, nenhum evento se perde
entre duas leituras (o cursor e fixado num ID concreto na primeira leitura; ver
`subscribe`). Entrega pelo menos uma vez exigiria um grupo por agente, `XACK`
depois do `handled` da caixa de entrada (`agents/base._Inbox`) e
idempotencia em todo consumidor -- o Execution tem (`client_order_id`), o Risk
e a Strategy nao deduplicam sinal reavaliado.

Lacuna que continua: o cursor so e fixado no primeiro `anext`, e
`_Inbox.ready` e marcado antes dele -- o que for publicado nessa janela da
subida nao e entregue (no bus em memoria a assinatura nasce sem `await`).
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
        self._notificar_observadores(topic, payload)

    async def subscribe(self, topic: str) -> AsyncIterator[Any]:
        """Le o stream a partir de agora, em fan-out.

        Cada assinante mantem seu proprio cursor local, o que reproduz a
        semantica do bus in-process: todos recebem todos os eventos.

        O "agora" e FIXADO num ID concreto (o ultimo do stream, ou `0-0` se ele
        ainda nao existe) antes da primeira leitura. Antes o cursor era `$` ate
        o primeiro evento chegar, e `$` e reavaliado a cada `XREAD`: um `XADD`
        que caisse entre duas leituras vazias (o bloqueio de 5 s expirou, o laco
        voltou) era gravado no stream e nunca entregue.
        """
        if self._redis is None:
            raise RuntimeError("EventBus nao iniciado: chame start() antes de subscribe()")

        model = TOPIC_MODELS.get(topic)
        ultimos = await self._redis.xrevrange(topic, count=1)
        last_id = ultimos[0][0] if ultimos else "0-0"
        while True:
            response = await self._redis.xread({topic: last_id}, count=100, block=5000)
            if not response:
                continue
            for _stream, entries in response:
                for entry_id, fields in entries:
                    last_id = entry_id
                    raw = json.loads(fields["data"])
                    yield model.model_validate(raw) if model else raw
