"""Watchdog depois da maquina suspensa: o episodio de 28/10/2026.

O notebook suspendeu; na volta os cinco agentes estavam "sem sinal de vida" no
mesmo instante, sem erro. O watchdog reiniciou a execucao tres vezes em tres
minutos e desistiu, pausando o sistema -- que ficou parado sete dias. Dois defeitos:

1. o agente recem-reiniciado continuava medido pela batida de ANTES do reinicio
   (`last_beat or started_at`), entao nascia vencido e era reiniciado de novo;
2. o watchdog nao distinguia "agente travado" de "processo inteiro congelado".

Cada teste ve a protecao agir e tem a contraprova do comportamento antigo.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

from test_orquestrador import AgenteDeEvento, BancadaOrquestrador, _escuta_alertas

from crypto_traders.agents import orchestrator as orch_mod
from crypto_traders.bus import InMemoryEventBus

AGORA = datetime.now(UTC)


def _vencido(agente) -> None:
    """Sem batida e sem subida ha duas horas: o agente travado de verdade."""
    agente.last_beat = datetime.now(UTC) - timedelta(hours=2)
    agente.started_at = datetime.now(UTC) - timedelta(hours=2)


class TestReinicioRenovaOPrazo:
    async def test_recem_reiniciado_nao_e_reiniciado_de_novo(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteDeEvento(bus)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)
        await agente.start()
        _vencido(agente)

        await orquestrador._check_health_once()
        assert agente.restarts == 1, "o agente vencido tem de ser reiniciado"
        batida_antiga = agente.last_beat

        # Os dois ciclos seguintes do watchdog (60 s cada, no sistema real): o
        # agente acabou de subir e tem o timeout inteiro para bater.
        await orquestrador._check_health_once()
        await orquestrador._check_health_once()
        await agente.stop()

        assert agente.restarts == 1, "reiniciou de novo um agente que acabou de subir"
        assert "consumidor" not in orquestrador._given_up
        # Contraprova: pela regra antiga, a batida velha ainda mandava.
        regra_antiga = batida_antiga or agente.started_at
        assert datetime.now(UTC) - regra_antiga > orch_mod.HEARTBEAT_TIMEOUT


class TestMaquinaSuspensa:
    async def test_atraso_do_watchdog_vira_carencia_e_nao_reinicio(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteDeEvento(bus)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)
        alertas, escuta = await _escuta_alertas(bus)
        await agente.start()
        _vencido(agente)

        agora = datetime.now(UTC)
        reconhecida = await orquestrador._observar_relogio(
            agora - timedelta(hours=2), agora, timedelta(seconds=60)
        )
        await orquestrador._check_health_once()
        await asyncio.sleep(0.05)
        escuta.cancel()

        assert reconhecida is True
        assert agente.restarts == 0, "maquina parada nao e agente travado"
        aviso = [a for a in alertas if a["type"] == "host_suspended"]
        assert aviso and "119 min" in aviso[0]["title"]  # 2 h menos o sleep de 60 s

        # Passada a carencia, travamento de verdade continua sendo pego.
        orquestrador._carencia_ate = time.monotonic() - 1
        await orquestrador._check_health_once()
        await agente.stop()
        assert agente.restarts == 1

    async def test_watchdog_no_horario_nao_inventa_suspensao(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        orquestrador = BancadaOrquestrador(settings, {}, bus)
        agora = datetime.now(UTC)
        assert (
            await orquestrador._observar_relogio(
                agora - timedelta(seconds=61), agora, timedelta(seconds=60)
            )
            is False
        )
        assert orquestrador._carencia_ate is None

    async def test_paradas_em_serie_nao_renovam_a_carencia(self, settings):
        """Engasgos de 4 min a cada ciclo (swap, CPU estrangulada) nao sao suspensao
        em serie: uma carencia por hora, e o agente travado de verdade e reiniciado.
        Contraprova: antes, cada engasgo renovava a carencia -- 0 reinicios em 5 h."""
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteDeEvento(bus)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)
        alertas, escuta = await _escuta_alertas(bus)
        await agente.start()
        agora = datetime.now(UTC)
        engasgo = timedelta(minutes=4)
        concedidas = [
            await orquestrador._observar_relogio(agora - engasgo, agora, timedelta(seconds=60))
            for _ in range(5)
        ]
        # A unica carencia concedida acaba; o agente travado tem de ser pego.
        orquestrador._carencia_ate = time.monotonic() - 1
        _vencido(agente)
        await orquestrador._check_health_once()
        await asyncio.sleep(0.05)
        escuta.cancel()
        await agente.stop()

        assert concedidas == [True, False, False, False, False]
        assert agente.restarts == 1
        assert len([a for a in alertas if a["type"] == "host_suspended"]) == 1

    async def test_falso_positivo_nao_apaga_a_contagem_de_reinicios(self, settings):
        """Um agente em laco de queda continua contando para a desistencia."""
        bus = InMemoryEventBus()
        await bus.start()
        orquestrador = BancadaOrquestrador(settings, {}, bus)
        agora = datetime.now(UTC)
        historico = [agora - timedelta(minutes=2), agora - timedelta(minutes=1)]
        orquestrador._restart_history["consumidor"] = list(historico)
        await orquestrador._observar_relogio(
            agora - timedelta(minutes=30), agora, timedelta(seconds=60)
        )
        assert orquestrador._restart_history["consumidor"] == historico

    async def test_prazo_da_carencia_nao_segue_o_relogio_de_parede(self, settings):
        """Prazo monotonico: um relogio de parede adiantado 5 h (e depois corrigido
        pelo NTP) da a mesma carencia de 10 min, e nao 5 h e 10 min."""
        bus = InMemoryEventBus()
        await bus.start()
        orquestrador = BancadaOrquestrador(settings, {}, bus)
        agora = datetime.now(UTC)
        await orquestrador._observar_relogio(
            agora, agora + timedelta(hours=5), timedelta(seconds=60)
        )
        restante = orquestrador._carencia_ate - time.monotonic()
        assert 0 < restante <= orch_mod.HEARTBEAT_TIMEOUT.total_seconds()
