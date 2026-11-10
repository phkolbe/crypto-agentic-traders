"""Barramento Redis Streams (latente: `EVENT_BUS=memory` e o padrao).

Auditoria de 09/11/2026, orquestracao M2. O backend Redis prometia replay e
entregava no-maximo-uma-vez com lacunas:

* o cursor `$` era mantido enquanto o `XREAD` voltava vazio, e cada `XREAD`
  seguinte reabria o "a partir de agora": um `XADD` entre duas leituras se
  perdia mesmo gravado no stream;
* reassinar depois de queda ou reinicio recomeca em `$` (o intervalo e pulado);
* nenhum consumer group, `XACK` ou `XPENDING` -- o evento em voo de um agente
  que morre nao volta.

A primeira lacuna e corrigida aqui (cursor fixado num ID concreto). As outras
pedem um contrato de confirmacao (`ack`) que atravessa a caixa de entrada de
todo agente; ate existir, o sistema RECUSA subir com Redis em `live`.
"""

from __future__ import annotations

import asyncio

import pytest

from crypto_traders.bus import Topics, build_event_bus
from crypto_traders.bus.redis_streams import RedisStreamsEventBus
from crypto_traders.domain.enums import TradingMode


def _ordem(entry_id: str) -> tuple[int, int]:
    ms, _, seq = entry_id.partition("-")
    return int(ms), int(seq or 0)


class RedisFalso:
    """O minimo do Redis Streams que o barramento usa, com a semantica do `$`.

    `XREAD ... $` so entrega o que for adicionado DURANTE aquela chamada: aqui a
    chamada bloqueia por uma volta do laco e volta vazia, que e o que o servidor
    faz quando o `XADD` cai entre duas leituras.
    """

    def __init__(self) -> None:
        self.entradas: list[tuple[str, dict]] = []
        self.cursores: list[str] = []

    async def xadd(self, topic, fields, maxlen=None, approximate=None):
        entry_id = f"{len(self.entradas) + 1}-0"
        self.entradas.append((entry_id, fields))
        return entry_id

    async def xrevrange(self, topic, max="+", min="-", count=None):
        return list(reversed(self.entradas))[: count or None]

    async def xread(self, streams, count=100, block=5000):
        ((topic, last_id),) = streams.items()
        self.cursores.append(last_id)
        await asyncio.sleep(0)
        if last_id == "$":
            return []
        novos = [(i, f) for i, f in self.entradas if _ordem(i) > _ordem(last_id)]
        return [(topic, novos[:count])] if novos else []


async def _bus_com(redis) -> RedisStreamsEventBus:
    bus = RedisStreamsEventBus("redis://falso:6379/0")
    bus._redis = redis
    return bus


class TestCursorFixado:
    async def test_evento_publicado_entre_duas_leituras_vazias_chega(self):
        redis = RedisFalso()
        bus = await _bus_com(redis)
        assinatura = bus.subscribe(Topics.ALERTS).__aiter__()
        proximo = asyncio.create_task(assinatura.__anext__())

        # Algumas leituras vazias antes do evento: e a janela que perdia.
        while len(redis.cursores) < 3:
            await asyncio.sleep(0)
        await bus.publish(Topics.ALERTS, {"type": "stop_loss", "title": "x"})

        evento = await asyncio.wait_for(proximo, timeout=1)
        assert evento == {"type": "stop_loss", "title": "x"}
        assert "$" not in redis.cursores, "o cursor voltou a reabrir o 'a partir de agora'"

    async def test_o_que_ja_estava_no_stream_antes_da_assinatura_nao_e_reentregue(self):
        """Fixar o cursor nao vira replay: o fan-out continua sendo 'de agora em diante'."""
        redis = RedisFalso()
        bus = await _bus_com(redis)
        await bus.publish(Topics.ALERTS, {"type": "antigo"})

        assinatura = bus.subscribe(Topics.ALERTS).__aiter__()
        proximo = asyncio.create_task(assinatura.__anext__())
        while len(redis.cursores) < 2:
            await asyncio.sleep(0)
        await bus.publish(Topics.ALERTS, {"type": "novo"})

        assert await asyncio.wait_for(proximo, timeout=1) == {"type": "novo"}


class TestRedisNaoSobeEmLive:
    def test_live_com_redis_e_recusado_com_o_motivo(self, settings):
        configurado = settings.model_copy(
            update={"event_bus": "redis", "trading_mode": TradingMode.LIVE}
        )

        with pytest.raises(RuntimeError, match="no-maximo-uma-vez"):
            build_event_bus(configurado)

    @pytest.mark.parametrize("modo", [TradingMode.DRY_RUN, TradingMode.TESTNET])
    def test_fora_do_live_o_redis_continua_disponivel(self, settings, modo):
        configurado = settings.model_copy(update={"event_bus": "redis", "trading_mode": modo})

        assert isinstance(build_event_bus(configurado), RedisStreamsEventBus)

    def test_memoria_em_live_segue_normal(self, settings):
        configurado = settings.model_copy(update={"trading_mode": TradingMode.LIVE})

        assert not isinstance(build_event_bus(configurado), RedisStreamsEventBus)
