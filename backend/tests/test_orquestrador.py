"""Orquestrador: ciclo de vida, heartbeat e watchdog.

Estes testes existem por causa de D25. O ensaio em `dry_run` mostrou o watchdog
reiniciando o agente `strategy` **17 vezes em 16 minutos**, sempre com
`crashed=False error=None stale=True`: o agente estava legitimamente ocioso
esperando o proximo candle diario, e o watchdog nao sabia distinguir "ocioso" de
"travado".

Os dois testes que importam estao lado a lado de proposito -- `TestOciosoOuTravado`.
Um deles ve o agente ocioso NAO ser reiniciado; o outro ve o agente travado SER
reiniciado. Um sozinho nao prova nada: passar o primeiro e facil desligando o
watchdog, e passar o segundo e facil afrouxando o timeout.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from helpers import make_candles

from crypto_traders.agents import base as base_mod
from crypto_traders.agents import orchestrator as orch_mod
from crypto_traders.agents.base import BaseAgent
from crypto_traders.agents.orchestrator import Orchestrator
from crypto_traders.agents.strategy import StrategyAgent
from crypto_traders.bus import InMemoryEventBus, Topics
from crypto_traders.db.repositories import AuditLogRepository
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import AgentState
from crypto_traders.strategies import get_strategy


# ---------------------------------------------------------------------------
# Bancada
# ---------------------------------------------------------------------------
class BancadaOrquestrador(Orchestrator):
    """Orquestrador com o conjunto de agentes escolhido pelo teste.

    O `Orchestrator.start()` real precisa de exchange, banco e descoberta. O que
    esta em julgamento aqui e a vigilancia de saude, entao o teste monta os
    agentes que quer vigiar e chama o watchdog na mao -- sem esperar os 60s do
    `WATCHDOG_INTERVAL_SECONDS`.
    """

    def __init__(self, settings, agentes: dict[str, BaseAgent], bus: InMemoryEventBus) -> None:
        super().__init__(settings)
        self.bus = bus
        self._agentes = agentes

    @property
    def agents(self) -> dict[str, BaseAgent]:
        return self._agentes


class AgenteDeEvento(BaseAgent):
    """Consumidor de `Topics.CANDLES` com o mesmo desenho do agente real.

    `strategy`, `risk_manager` e `execution` sao todos assim: ficam pendurados
    numa assinatura do bus e so trabalham quando um evento chega.
    """

    name = "consumidor"

    def __init__(self, bus, *, trava: asyncio.Event | None = None) -> None:
        super().__init__(bus)
        self.recebidos: list = []
        self.trava = trava
        """Se presente, o agente fica pendurado no processamento ate ser liberado."""

    async def _run(self) -> None:
        async for evento in self.bus.subscribe(Topics.CANDLES):
            await self.wait_if_paused()
            self.recebidos.append(evento)
            if self.trava is not None:
                await self.trava.wait()
            await self.heartbeat()


class AgenteQueMorre(BaseAgent):
    name = "suicida"

    def __init__(self, bus) -> None:
        super().__init__(bus)
        self.subidas = 0

    async def _run(self) -> None:
        self.subidas += 1
        raise RuntimeError("morri na subida")


class AgenteQueNuncaBate(BaseAgent):
    """Sobe, fica ocupado e nunca bate heartbeat nem entra em espera."""

    name = "mudo"

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(0.01)


async def _escuta_alertas(bus: InMemoryEventBus) -> tuple[list[dict], asyncio.Task]:
    alertas: list[dict] = []

    async def escuta() -> None:
        async for alerta in bus.subscribe(Topics.ALERTS):
            alertas.append(alerta)

    tarefa = asyncio.create_task(escuta())
    await asyncio.sleep(0)
    return alertas, tarefa


@pytest.fixture
def relogio_apertado(monkeypatch):
    """Encolhe os prazos do watchdog para caberem num teste.

    Nada de logica muda: o que era "10 minutos sem bater" passa a ser "0,3s sem
    bater", e a batida de vitalidade sai a cada 0,05s em vez de 60s.
    """
    monkeypatch.setattr(orch_mod, "HEARTBEAT_TIMEOUT", timedelta(seconds=0.3))
    monkeypatch.setattr(base_mod, "IDLE_PULSE_SECONDS", 0.05)


# ---------------------------------------------------------------------------
class TestOciosoOuTravado:
    """O nucleo de D25: qual e o sinal correto de vitalidade."""

    async def test_agente_ocioso_nao_e_reiniciado(self, settings, relogio_apertado):
        """Candle de 1 dia: o agente fica horas sem trabalho e isso e normal.

        Foi este o caso que o watchdog diagnosticou errado 17 vezes seguidas.
        """
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteDeEvento(bus)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)
        alertas, escuta = await _escuta_alertas(bus)

        await agente.start()
        await asyncio.sleep(0.05)

        # Um evento no inicio, como na subida real (o priming publica candles),
        # e depois o silencio legitimo do timeframe diario.
        await bus.publish(Topics.CANDLES, make_candles([100.0])[0])
        await asyncio.sleep(0.05)
        assert agente.recebidos, "o agente precisa ter recebido o primeiro evento"

        tarefa_original = agente._task
        primeira_batida = agente.last_beat

        # Muito mais que o timeout: com o diagnostico antigo o watchdog teria
        # reiniciado o agente cinco vezes nesta janela.
        await asyncio.sleep(0.5)
        await orquestrador._check_health_once()
        await asyncio.sleep(0.5)
        await orquestrador._check_health_once()

        await asyncio.sleep(0.05)  # deixa o alerta chegar a quem escuta
        escuta.cancel()
        tarefa_final = agente._task
        ocioso_no_fim = agente.is_idle
        await agente.stop()

        assert tarefa_final is tarefa_original, "agente ocioso foi reiniciado"
        assert agente.restarts == 0
        assert [a for a in alertas if a["type"] == "agent_restarted"] == []
        # E a prova de que a protecao AGE: quem manteve o agente vivo aos olhos
        # do watchdog foi a batida de ocioso, nao um timeout afrouxado.
        assert agente.last_beat > primeira_batida
        assert ocioso_no_fim, "o agente deveria estar esperando, nao processando"

    async def test_agente_travado_e_reiniciado(self, settings, relogio_apertado):
        """Mesmo cenario, uma diferenca: o agente esta pendurado no processamento.

        Este e o travamento de verdade -- a razao pela qual o watchdog existe --
        e ele precisa continuar sendo detectado depois da correcao do ocioso.
        """
        bus = InMemoryEventBus()
        await bus.start()
        trava = asyncio.Event()
        agente = AgenteDeEvento(bus, trava=trava)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)
        alertas, escuta = await _escuta_alertas(bus)

        await agente.start()
        await asyncio.sleep(0.05)
        await bus.publish(Topics.CANDLES, make_candles([100.0])[0])
        await asyncio.sleep(0.05)

        tarefa_original = agente._task
        assert not agente.is_idle, "pendurado no processamento nao e ocioso"

        await asyncio.sleep(0.5)
        await orquestrador._check_health_once()

        await asyncio.sleep(0.05)  # deixa o alerta chegar a quem escuta
        escuta.cancel()
        tarefa_final = agente._task
        trava.set()
        await agente.stop()

        assert tarefa_final is not tarefa_original, "agente travado NAO foi reiniciado"
        assert agente.restarts == 1
        reinicios = [a for a in alertas if a["type"] == "agent_restarted"]
        assert len(reinicios) == 1

    async def test_agente_que_nunca_bateu_e_detectado(self, settings, relogio_apertado):
        """`last_beat is None` era eternidade aos olhos do watchdog.

        A regra antiga exigia `agent.last_beat is not None` para considerar o
        agente velho. Um agente que sobe e nunca bate -- o pior caso -- passava
        despercebido para sempre.
        """
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteQueNuncaBate(bus)
        orquestrador = BancadaOrquestrador(settings, {"mudo": agente}, bus)

        await agente.start()
        assert agente.last_beat is None
        await asyncio.sleep(0.5)

        await orquestrador._check_health_once()
        await agente.stop()

        assert agente.restarts == 1


# ---------------------------------------------------------------------------
class TestPerdaDeMensagem:
    """O dano real de D25: `stop()` + `start()` deixa uma janela sem assinatura."""

    async def test_evento_publicado_durante_o_reinicio_nao_e_perdido(
        self, settings, relogio_apertado
    ):
        """A rajada dos 16 pares caindo exatamente na janela do reinicio.

        Candle e publicado uma unica vez -- fechado e inedito. Com `stop()`
        seguido de `start()`, tudo que fosse publicado entre os dois sumia sem
        uma linha no log. Aqui a publicacao acontece dentro do reinicio, no pior
        instante possivel, e nada pode ser descartado.
        """
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteDeEvento(bus)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)

        await agente.start()
        await asyncio.sleep(0.05)

        rajada = make_candles([100.0 + i for i in range(16)])
        subida_original = agente.start

        async def sobe_publicando_no_meio() -> None:
            # Janela exata: a tarefa antiga ja morreu, a nova ainda nao existe.
            for candle in rajada:
                await bus.publish(Topics.CANDLES, candle)
            await subida_original()

        agente.start = sobe_publicando_no_meio
        agente.last_beat = datetime.now(UTC) - timedelta(hours=1)

        await orquestrador._check_health_once()
        await asyncio.sleep(0.2)

        await agente.stop()

        assert agente.restarts == 1, "o teste depende de o reinicio ter acontecido"
        assert len(agente.recebidos) == 16, (
            f"perdeu candle no reinicio: recebeu {len(agente.recebidos)} de 16"
        )

    async def test_stop_seguido_de_start_perde_tudo_na_janela(self, settings):
        """A medicao do defeito, mantida como fronteira.

        `stop()` solta a assinatura de proposito -- e desligamento completo, nao
        reciclagem. O que este teste fixa e o tamanho do dano: 16 candles
        publicados na janela, 16 candles perdidos. E por isso que o watchdog usa
        `restart()`, e o teste acima e que prova que ele usa.
        """
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteDeEvento(bus)

        await agente.start()
        await asyncio.sleep(0.05)

        await agente.stop()
        for candle in make_candles([100.0 + i for i in range(16)]):
            await bus.publish(Topics.CANDLES, candle)
        await agente.start()
        await asyncio.sleep(0.2)
        await agente.stop()

        assert agente.recebidos == []

    async def test_evento_interrompido_no_meio_e_registrado_em_alto(
        self, settings, relogio_apertado
    ):
        """O que a caixa de entrada nao consegue salvar tem que aparecer no log.

        Um evento ja entregue e em processamento quando a tarefa e morta esta
        perdido de verdade -- reentrega-lo poderia duplicar efeito. Silencio
        sobre isso e a mesma familia do stop-loss que nao disparava.
        """
        bus = InMemoryEventBus()
        await bus.start()
        trava = asyncio.Event()
        agente = AgenteDeEvento(bus, trava=trava)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)
        alertas, escuta = await _escuta_alertas(bus)

        await agente.start()
        await asyncio.sleep(0.05)
        await bus.publish(Topics.CANDLES, make_candles([100.0], symbol="ETH/USDT")[0])
        await asyncio.sleep(0.05)

        await asyncio.sleep(0.5)
        await orquestrador._check_health_once()

        await asyncio.sleep(0.05)  # deixa o alerta chegar a quem escuta
        escuta.cancel()
        trava.set()
        await agente.stop()

        assert agente.lost_events == 1
        reinicio = next(a for a in alertas if a["type"] == "agent_restarted")
        assert "ETH/USDT" in reinicio["message"]
        assert "perdido" in reinicio["message"].lower()

    async def test_a_assinatura_sobrevive_ao_reinicio(self, settings, relogio_apertado):
        """Depois do reinicio existe UMA assinatura, nao duas nem zero.

        Duas assinaturas fariam o agente processar cada candle em dobro; zero o
        deixariam de pe e surdo, que e o pior dos dois.
        """
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteDeEvento(bus)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)

        await agente.start()
        await asyncio.sleep(0.05)
        assert bus.subscriber_count[Topics.CANDLES] == 1

        agente.last_beat = datetime.now(UTC) - timedelta(hours=1)
        await orquestrador._check_health_once()
        await asyncio.sleep(0.05)

        assert bus.subscriber_count[Topics.CANDLES] == 1

        await bus.publish(Topics.CANDLES, make_candles([100.0])[0])
        await asyncio.sleep(0.05)
        assert len(agente.recebidos) == 1, "evento processado em dobro ou nao processado"

        await agente.stop()
        assert bus.subscriber_count.get(Topics.CANDLES, 0) == 0, "assinatura vazou no stop"


# ---------------------------------------------------------------------------
class TestRuidoDeAlerta:
    """D19 aplicado ao watchdog: um alerta que chega sempre deixa de ser lido."""

    async def test_um_alerta_por_episodio_e_nao_por_tentativa(
        self, settings, relogio_apertado
    ):
        """Foram 17 alertas em 16 minutos -- 1.440 por dia no ritmo do watchdog."""
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteQueMorre(bus)
        orquestrador = BancadaOrquestrador(settings, {"suicida": agente}, bus)
        alertas, escuta = await _escuta_alertas(bus)

        await agente.start()
        await asyncio.sleep(0.05)

        for _ in range(6):
            await orquestrador._check_health_once()
            await asyncio.sleep(0.02)

        await asyncio.sleep(0.05)  # deixa o alerta chegar a quem escuta
        escuta.cancel()

        reinicios = [a for a in alertas if a["type"] == "agent_restarted"]
        assert len(reinicios) == 1, f"{len(reinicios)} alertas do mesmo episodio"

    async def test_desiste_depois_de_tentativas_repetidas_e_para_de_operar(
        self, settings, relogio_apertado
    ):
        """Reiniciar em laco nao consertou nada em D25: so produziu ruido.

        Depois do limite de tentativas o sistema para de tentar e recusa operar
        -- o estado seguro nao e "arrisca menos", e "nao arrisca".
        """
        bus = InMemoryEventBus()
        await bus.start()
        morto = AgenteQueMorre(bus)
        vivo = AgenteDeEvento(bus)
        orquestrador = BancadaOrquestrador(
            settings, {"suicida": morto, "strategy": vivo, "risk_manager": vivo,
                       "execution": vivo}, bus
        )
        alertas, escuta = await _escuta_alertas(bus)

        await morto.start()
        await vivo.start()
        await asyncio.sleep(0.05)

        for _ in range(8):
            await orquestrador._check_health_once()
            await asyncio.sleep(0.02)

        await asyncio.sleep(0.05)  # deixa o alerta chegar a quem escuta
        escuta.cancel()
        pausado_no_fim = vivo.is_paused
        await vivo.stop()

        assert morto.subidas == 1 + orch_mod.MAX_RESTART_ATTEMPTS, (
            "o watchdog nao parou de reiniciar um agente que nao volta"
        )
        assert len(
            [a for a in alertas if a["type"] == "agent_unrecoverable"]
        ) == 1
        assert pausado_no_fim, "o sistema continuou operando sem um agente da cadeia"

    async def test_rearma_o_alerta_so_depois_da_janela_inteira_sem_reinicio(
        self, settings, relogio_apertado
    ):
        """Um episodio novo tem que avisar de novo, senao a segunda falha e muda.

        O que fecha o episodio e a JANELA passar limpa, e nao um ciclo em que o
        agente pareceu bem. Essa distincao e o defeito medido: logo depois de um
        reinicio o agente sempre parece bem, porque `_is_stale` mede a partir de
        `started_at` e ele acabou de ser renovado. Contando ciclos consecutivos,
        a contagem zerava a cada reinicio e a desistencia nunca armava.
        """
        bus = InMemoryEventBus()
        await bus.start()
        trava = asyncio.Event()
        agente = AgenteDeEvento(bus, trava=trava)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)
        alertas, escuta = await _escuta_alertas(bus)

        await agente.start()
        await asyncio.sleep(0.05)

        # Primeiro episodio: travado no processamento.
        await bus.publish(Topics.CANDLES, make_candles([100.0])[0])
        await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        await orquestrador._check_health_once()
        trava.set()

        # Um ciclo saudavel logo depois NAO fecha o episodio.
        await asyncio.sleep(0.2)
        await orquestrador._check_health_once()
        assert agente.restarts == 1
        assert "consumidor" in orquestrador._restart_alerted, (
            "um unico ciclo bom logo apos o reinicio nao pode rearmar o alerta"
        )

        # A janela inteira sem reinicio novo, ai sim: episodio encerrado.
        janela = orquestrador._restart_window().total_seconds()
        await asyncio.sleep(janela + 0.1)
        await orquestrador._check_health_once()
        assert "consumidor" not in orquestrador._restart_alerted

        # Segundo episodio, agora por falta de sinal de vida.
        agente.last_beat = datetime.now(UTC) - timedelta(hours=1)
        await orquestrador._check_health_once()

        await asyncio.sleep(0.05)  # deixa o alerta chegar a quem escuta
        escuta.cancel()
        await agente.stop()

        assert agente.restarts == 2
        assert len([a for a in alertas if a["type"] == "agent_restarted"]) == 2

    async def test_na_cadencia_de_producao_o_watchdog_desiste_e_para_de_alertar(
        self, settings, monkeypatch
    ):
        """A proporcao real: watchdog a cada 60s, timeout de 10min (1 : 10).

        Chamar `_check_health_once()` varias vezes seguidas, sem tempo entre
        elas, esconde o defeito: o agente nunca tem um ciclo em que pareca
        saudavel. Em producao ele tem nove, porque `started_at` foi renovado pelo
        reinicio -- e foi assim que a contagem de tentativas zerava sozinha.
        Medido com a contagem por ciclos consecutivos: 13 reinicios, 13 alertas,
        desistiu=False, ou ~144 alertas/dia para sempre e a recusa de operar
        nunca armando.
        """
        monkeypatch.setattr(orch_mod, "HEARTBEAT_TIMEOUT", timedelta(seconds=0.3))
        monkeypatch.setattr(base_mod, "IDLE_PULSE_SECONDS", 3600.0)
        ciclo = 0.03  # 1:10 com o timeout, como 60s : 600s

        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteQueNuncaBate(bus)
        orquestrador = BancadaOrquestrador(settings, {"mudo": agente}, bus)
        alertas, escuta = await _escuta_alertas(bus)

        await agente.start()
        for _ in range(120):
            await asyncio.sleep(ciclo)
            await orquestrador._check_health_once()

        await asyncio.sleep(0.05)
        escuta.cancel()
        medida = {
            "restarts": agente.restarts,
            "alertas_restart": len([a for a in alertas if a["type"] == "agent_restarted"]),
            "desistiu": "mudo" in orquestrador._given_up,
        }
        await agente.stop()

        assert medida["desistiu"], f"o watchdog reinicia e alerta para sempre: {medida}"
        assert medida["restarts"] == orch_mod.MAX_RESTART_ATTEMPTS, medida
        assert medida["alertas_restart"] == 1, f"alerta por tentativa: {medida}"
        assert len([a for a in alertas if a["type"] == "agent_unrecoverable"]) == 1


# ---------------------------------------------------------------------------
class TestAgentesReais:
    """O agente `strategy` de verdade, que foi quem sofreu o defeito."""

    async def test_strategy_ocioso_em_timeframe_diario_permanece_de_pe(
        self, settings, relogio_apertado
    ):
        bus = InMemoryEventBus()
        await bus.start()
        agente = StrategyAgent(bus, [get_strategy("ma_crossover", fast=3, slow=10)], settings)
        orquestrador = BancadaOrquestrador(settings, {"strategy": agente}, bus)

        await agente.start()
        await asyncio.sleep(0.05)
        await bus.publish(Topics.CANDLES, make_candles([100.0], timeframe="1d")[0])
        await asyncio.sleep(0.05)

        tarefa = agente._task
        await asyncio.sleep(0.5)
        await orquestrador._check_health_once()

        assert agente._task is tarefa
        assert agente.restarts == 0
        assert agente.state is AgentState.RUNNING
        assert agente.is_idle

        await agente.stop()

    async def test_health_expoe_ocioso_e_pendencia(self, settings, relogio_apertado):
        """O dashboard tem que poder mostrar "esperando", nao so "sem bater"."""
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteDeEvento(bus)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)
        orquestrador.market_data = _FalsoMarketData()
        orquestrador.strategy = _FalsaStrategy()
        orquestrador.risk_manager = _FalsoRisk()

        await agente.start()
        await asyncio.sleep(0.05)

        saude = await orquestrador.health()
        await agente.stop()

        estado = saude["agents"]["consumidor"]
        assert estado["idle"] is True
        assert estado["stale"] is False
        assert estado["pending_events"] == 0
        assert estado["lost_events"] == 0


class _FalsoMarketData:
    discovery_enabled = False

    def __init__(self) -> None:
        self.active_symbols = ["BTC/USDT"]
        self.latest_prices: dict = {}


class _FalsaStrategy:
    def __init__(self) -> None:
        self.strategy_names = ["ma_crossover"]


class _FalsoRisk:
    circuit_breaker_active = False


# ---------------------------------------------------------------------------
class TestSobrevivencia:
    """Achado 3 de D25: o processo morreu com a maquina, e nada disse isso depois."""

    async def test_desligamento_limpo_deixa_marca(self, settings):
        orquestrador = Orchestrator(settings)
        await orquestrador._mark_clean_shutdown("teste")

        async with session_scope(settings) as session:
            acoes = [r.action for r in await AuditLogRepository(session).list(limit=10)]
        assert "system_stopped" in acoes

    async def test_morte_sem_desligamento_e_denunciada_na_subida(self, settings):
        """Subida anterior sem parada correspondente = o processo foi derrubado."""
        async with session_scope(settings) as session:
            await AuditLogRepository(session).append(
                action="system_started", target="orchestrator"
            )

        orquestrador = Orchestrator(settings)
        assert await orquestrador._detect_unclean_shutdown() is not None

    async def test_subida_apos_desligamento_limpo_nao_denuncia_nada(self, settings):
        async with session_scope(settings) as session:
            repositorio = AuditLogRepository(session)
            await repositorio.append(action="system_started", target="orchestrator")
        async with session_scope(settings) as session:
            await AuditLogRepository(session).append(
                action="system_stopped", target="orchestrator"
            )

        orquestrador = Orchestrator(settings)
        assert await orquestrador._detect_unclean_shutdown() is None

    async def test_primeira_subida_da_vida_nao_denuncia_nada(self, settings):
        orquestrador = Orchestrator(settings)
        assert await orquestrador._detect_unclean_shutdown() is None


# ---------------------------------------------------------------------------
class TestControlePeloOperador:
    async def test_resumir_agente_morto_o_traz_de_volta(self, settings, relogio_apertado):
        """`resume()` num agente cuja tarefa morreu apenas liberava um gate vazio."""
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteDeEvento(bus)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)

        await agente.start()
        await asyncio.sleep(0.05)
        await agente._cancel_task()
        assert not agente.is_running

        assert await orquestrador.resume_agent("consumidor")
        assert agente.is_running

        await agente.stop()

    async def test_reinicio_nao_desfaz_a_pausa_do_operador(self, settings, relogio_apertado):
        """Um agente pausado que e reiniciado nao pode voltar operando sozinho."""
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteDeEvento(bus)

        await agente.start()
        await asyncio.sleep(0.05)
        agente.pause()

        await agente.restart()
        assert agente.is_paused
        assert agente.state is AgentState.PAUSED

        await agente.stop()

    async def test_agente_pausado_nao_e_reiniciado_pelo_watchdog(
        self, settings, relogio_apertado
    ):
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteDeEvento(bus)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)

        await agente.start()
        await asyncio.sleep(0.05)
        agente.pause()
        agente.last_beat = datetime.now(UTC) - timedelta(hours=1)

        await orquestrador._check_health_once()
        await agente.stop()

        assert agente.restarts == 0


# ---------------------------------------------------------------------------
class TestConsumidoresAntesDosProdutores:
    """A subida so pode publicar depois que os tres consumidores assinaram.

    Antes havia um `asyncio.sleep(0)` na subida, que bastava porque o laco do
    agente assinava direto. Com a caixa de entrada duravel existe uma tarefa a
    mais no caminho, e dormir zero passaria a ser aposta -- o preco do erro
    sendo os primeiros candles publicados no vazio.
    """

    async def _consumidores(self, settings, bus):
        from crypto_traders.agents.execution import ExecutionAgent
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.exchanges.paper import PaperBroker

        return (
            ExecutionAgent(bus, PaperBroker(initial_balance=Decimal("100")), settings),
            RiskManagerAgent(bus, settings),
            StrategyAgent(bus, [get_strategy("ma_crossover")], settings),
        )

    async def test_a_espera_termina_com_as_assinaturas_registradas(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        orquestrador = BancadaOrquestrador(settings, {}, bus)
        consumidores = await self._consumidores(settings, bus)

        for agente in consumidores:
            await agente.start()
        # Nenhum `sleep` de conveniencia aqui: e a espera do orquestrador que
        # tem que garantir o registro.
        await orquestrador._wait_for_subscriptions(consumidores)

        assert all(a.subscriptions_ready for a in consumidores)
        assert bus.subscriber_count[Topics.CANDLES] == 1
        assert bus.subscriber_count[Topics.SIGNALS] == 1
        assert bus.subscriber_count[Topics.ORDER_REQUESTS] == 1

        for agente in consumidores:
            await agente.stop()

    async def test_o_primeiro_candle_publicado_apos_a_espera_chega(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        orquestrador = BancadaOrquestrador(settings, {}, bus)
        execucao, risco, estrategia = await self._consumidores(settings, bus)
        consumidores = (execucao, risco, estrategia)

        for agente in consumidores:
            await agente.start()
        await orquestrador._wait_for_subscriptions(consumidores)

        await bus.publish(Topics.CANDLES, make_candles([100.0], timeframe="1d")[0])
        await asyncio.sleep(0.1)

        for agente in consumidores:
            await agente.stop()

        # O candle nao vira sinal (falta historico), mas a batida prova que ele
        # chegou ao agente -- que e o que o `sleep(0)` nao garantia mais.
        assert estrategia.last_beat is not None


# ---------------------------------------------------------------------------
class TestParadaGeralNaoBloqueiaFechamento:
    """D4: bloquear a abertura nunca pode bloquear o fechamento.

    `pause_all` pausava o Execution Agent, e o circuit breaker chama `pause_all`.
    Medido com uma posicao 20% abaixo do preco medio: a saida de protecao era
    emitida pelo Risk Manager, ficava na fila do agente pausado e **nao virava
    ordem** ate alguem retomar na mao. A trava dispara exatamente quando o
    mercado cai -- quando o stop mais precisa agir.
    """

    def _snapshot_furado(self):
        from crypto_traders.domain.enums import ExchangeName
        from crypto_traders.domain.models import PortfolioSnapshot, Position

        return PortfolioSnapshot(
            total_value=Decimal("40100"),
            cash_value=Decimal("100"),
            positions_value=Decimal("40000"),
            positions=[
                Position(
                    exchange=ExchangeName.BINANCE,
                    asset="BTC",
                    quantity=Decimal("1"),
                    average_price=Decimal("50000"),
                    current_price=Decimal("40000"),  # 20% abaixo, stop de 3%
                )
            ],
        )

    async def test_o_stop_loss_dispara_com_o_sistema_pausado(self, settings):
        from crypto_traders.agents.execution import ExecutionAgent
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.db.repositories import OrderRepository
        from crypto_traders.exchanges.paper import PaperBroker

        bus = InMemoryEventBus()
        await bus.start()
        broker = PaperBroker(initial_balance=Decimal("100"))
        broker.set_price("BTC", Decimal("40000"))
        risco = RiskManagerAgent(bus, settings)
        execucao = ExecutionAgent(bus, broker, settings)
        estrategia = StrategyAgent(bus, [get_strategy("ma_crossover")], settings)

        orquestrador = BancadaOrquestrador(
            settings,
            {"strategy": estrategia, "risk_manager": risco, "execution": execucao},
            bus,
        )
        await execucao.start()
        await asyncio.sleep(0.1)

        # A parada geral do circuit breaker.
        await orquestrador.pause_all(actor="circuit_breaker", reason="perda diaria")
        assert risco.is_paused, "a tomada de decisao tem que parar"
        assert not execucao.is_paused, "a execucao nao pode parar: ela e quem fecha"

        await risco.enforce_protective_exits(self._snapshot_furado())
        await asyncio.sleep(0.3)

        async with session_scope(settings) as session:
            ordens = await OrderRepository(session).list()

        await execucao.stop()

        assert len(ordens) == 1, "o stop-loss nao fechou a posicao com o sistema pausado"
        assert ordens[0].side == "sell"


# ---------------------------------------------------------------------------
class TestCadenciaNaoEAmbiente:
    """D15: prazo de vigilancia e infraestrutura, nao variavel de negocio."""

    def test_os_prazos_do_watchdog_nao_vem_do_env(self):
        from crypto_traders.config import Settings

        campos = set(Settings.model_fields)
        for proibido in ("heartbeat_timeout", "watchdog_interval", "idle_pulse_seconds"):
            assert proibido not in campos

    def test_a_batida_de_ocioso_cabe_dentro_do_timeout(self):
        """Bater a cada 60s com timeout de 10min da margem de 10 batidas."""
        timeout = orch_mod.HEARTBEAT_TIMEOUT.total_seconds()
        assert timeout > base_mod.IDLE_PULSE_SECONDS * 2
        assert timeout > orch_mod.WATCHDOG_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
class TestGuardiaoTravado:
    """O Risk Manager DE VERDADE, que tem duas tarefas fazendo coisas diferentes.

    Uma alimenta a fila de sinais, a outra avalia os lotes. Enquanto o sinal de
    vitalidade era um unico "estou esperando" por agente, quem o marcava era o
    alimentador -- e ele fica ocioso mesmo com o laco de avaliacao pendurado.
    Medido nessa versao: restarts=0, is_idle=True, batendo, stale=False. O
    guardiao pelo qual TODA ordem passa podia travar para sempre com o watchdog
    e o dashboard verdes. E o defeito de D25 espelhado.

    Os dois testes andam juntos de proposito, como em `TestOciosoOuTravado`:
    passar o primeiro sozinho e facil desligando a deteccao.
    """

    def _sinal(self):
        from crypto_traders.domain.enums import ExchangeName, SignalDirection
        from crypto_traders.domain.models import Signal

        return Signal(
            exchange=ExchangeName.BINANCE,
            symbol="BTC/USDT",
            timeframe="1d",
            direction=SignalDirection.LONG,
            confidence=0.9,
            strategy="ma_crossover",
            reason="teste de vitalidade",
            reference_price=Decimal("100"),
        )

    async def test_guardiao_ocioso_em_timeframe_diario_nao_e_reiniciado(
        self, settings, relogio_apertado
    ):
        """Sem sinal nenhum, as duas tarefas esperam -- e esperar e estar vivo."""
        from crypto_traders.agents.risk_manager import RiskManagerAgent

        bus = InMemoryEventBus()
        await bus.start()
        risco = RiskManagerAgent(bus, settings)
        orquestrador = BancadaOrquestrador(settings, {"risk_manager": risco}, bus)

        await risco.start()
        await asyncio.sleep(0.1)
        tarefa = risco._task

        await asyncio.sleep(0.6)  # o dobro do timeout
        await orquestrador._check_health_once()
        ocioso = risco.is_idle
        tarefa_final = risco._task
        await risco.stop()

        assert risco.restarts == 0, "guardiao legitimamente ocioso foi reiniciado"
        assert tarefa_final is tarefa
        assert ocioso, "as duas tarefas do guardiao estavam esperando"

    async def test_guardiao_com_o_laco_de_avaliacao_pendurado_e_reiniciado(
        self, settings, relogio_apertado, monkeypatch
    ):
        """Mesmo agente, mesma cadencia: agora a tarefa que TRABALHA travou."""
        from crypto_traders.agents.risk_manager import RiskManagerAgent

        bus = InMemoryEventBus()
        await bus.start()
        risco = RiskManagerAgent(bus, settings)
        orquestrador = BancadaOrquestrador(settings, {"risk_manager": risco}, bus)
        alertas, escuta = await _escuta_alertas(bus)

        travou = asyncio.Event()

        async def avaliacao_pendurada(batch):
            travou.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(risco, "_on_batch", avaliacao_pendurada)

        await risco.start()
        await asyncio.sleep(0.1)
        await bus.publish(Topics.SIGNALS, self._sinal())
        await asyncio.sleep(0.2)
        assert travou.is_set(), "o teste depende de o laco de avaliacao ter travado"

        # O alimentador continua ocioso o quanto quiser: nao pode bastar. E este
        # o ponto exato do defeito.
        assert not risco.is_idle, "agente com uma tarefa trabalhando nao esta ocioso"
        batida = risco.last_beat

        await asyncio.sleep(0.6)
        assert risco.last_beat == batida, "bateu como ocioso com o guardiao travado"

        await orquestrador._check_health_once()
        await asyncio.sleep(0.05)
        escuta.cancel()
        await risco.stop()

        assert risco.restarts == 1, "guardiao travado NAO foi reiniciado"
        assert [a for a in alertas if a["type"] == "agent_restarted"]

    async def test_tarefa_interna_que_morre_leva_o_agente_para_erro(
        self, settings, relogio_apertado
    ):
        """Tarefa interna que morre calada deixaria o agente de pe com uma perna."""

        class AgenteComTarefaFragil(BaseAgent):
            name = "fragil"

            async def _run(self) -> None:
                self.spawn(self._morre(), name="worker")
                while True:
                    await self.sleep(10)

            async def _morre(self) -> None:
                await asyncio.sleep(0.05)
                raise RuntimeError("a tarefa interna morreu")

        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteComTarefaFragil(bus)
        orquestrador = BancadaOrquestrador(settings, {"fragil": agente}, bus)

        await agente.start()
        await asyncio.sleep(0.15)
        assert agente.state is AgentState.ERROR
        assert "a tarefa interna morreu" in (agente.last_error or "")

        await orquestrador._check_health_once()
        await agente.stop()
        assert agente.restarts == 1


# ---------------------------------------------------------------------------
class BusQueEstoura(InMemoryEventBus):
    """Bus cujo stream de candles morre logo apos o primeiro evento.

    E o que o `xread` do Redis faz ao perder a conexao
    (`bus/redis_streams.py`): a excecao sai de dentro do gerador da assinatura.
    """

    def __init__(self) -> None:
        super().__init__()
        self.estourou = False
        self.armado = True

    async def subscribe(self, topic):
        if topic != Topics.CANDLES or not self.armado:
            async for evento in super().subscribe(topic):
                yield evento
            return
        self.armado = False
        async for evento in super().subscribe(topic):
            yield evento
            self.estourou = True
            raise ConnectionResetError("xread do bus caiu")


class AgenteQueLibera(BaseAgent):
    name = "consumidor"

    def __init__(self, bus) -> None:
        super().__init__(bus)
        self.recebidos: list = []
        self.liberou = False

    async def _run(self) -> None:
        async for evento in self.bus.subscribe(Topics.CANDLES):
            await self.wait_if_paused()
            self.recebidos.append(evento)
            await self.heartbeat()

    async def on_stop(self) -> None:
        self.liberou = True


class TestAssinaturaQueCai:
    """Um agente surdo e o estado que mais parece saudavel: de pe e sem erro."""

    async def test_stream_que_morre_nao_deixa_o_agente_surdo_e_verde(
        self, settings, relogio_apertado
    ):
        """Medido antes: 1 de 6 candles, running, ocioso, batendo, sem erro.

        Antes da caixa de entrada a assinatura era iterada dentro de `_run`, a
        excecao subia para `_guarded_run` e virava state=ERROR. Passar a
        alimentar numa tarefa a parte trocou uma falha detectada por perda
        silenciosa de candle -- a familia do stop-loss que nao disparava.
        """
        bus = BusQueEstoura()
        await bus.start()
        agente = AgenteQueLibera(bus)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)
        alertas, escuta = await _escuta_alertas(bus)

        await agente.start()
        await asyncio.sleep(0.1)
        await bus.publish(Topics.CANDLES, make_candles([100.0])[0])
        await asyncio.sleep(0.2)
        assert bus.estourou, "o teste depende de o stream ter estourado"

        # 1. A queda e VISIVEL: estado, log alto e alerta.
        assert agente.state is AgentState.ERROR
        assert agente.deaf_topics == [Topics.CANDLES]
        assert [a for a in alertas if a["type"] == "inbox_stream_failed"], (
            "assinatura caiu sem uma linha para o operador"
        )

        # 2. Surdo NAO bate como ocioso: esperar um evento que nao vem e travar.
        batida = agente.last_beat
        await asyncio.sleep(0.3)
        assert agente.last_beat == batida, "agente surdo continuou dizendo que esta vivo"

        # 3. O watchdog reassina, e o agente volta a ouvir de verdade.
        await orquestrador._check_health_once()
        await asyncio.sleep(0.1)
        assert agente.restarts == 1
        assert agente.deaf_topics == []
        assert bus.subscriber_count[Topics.CANDLES] == 1

        await bus.publish(Topics.CANDLES, make_candles([101.0])[0])
        await asyncio.sleep(0.1)
        escuta.cancel()
        recebidos = len(agente.recebidos)
        await agente.stop()

        assert recebidos == 2, f"nao voltou a ouvir depois de reassinar: {recebidos}"

    async def test_alimentador_que_apenas_TERMINA_tambem_deixa_o_agente_surdo(
        self, settings, relogio_apertado
    ):
        """Nem toda morte de alimentador levanta excecao.

        Cancelado, ou com o gerador do bus simplesmente chegando ao fim, ele
        morre sem `broken` preenchido -- e o agente fica de pe, sem erro, com a
        fila vazia e esperando: exatamente o retrato de um agente saudavel.
        Perguntar "estourou?" nao basta; a pergunta e "ainda tem alguem
        transferindo do bus para a fila?".
        """
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteQueLibera(bus)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)

        await agente.start()
        await asyncio.sleep(0.1)
        agente._inboxes[Topics.CANDLES]._feeder.cancel()
        await asyncio.sleep(0.05)

        assert agente.deaf_topics == [Topics.CANDLES]
        batida = agente.last_beat
        await asyncio.sleep(0.2)
        assert agente.last_beat == batida, "agente surdo bateu como se estivesse vivo"

        await orquestrador._check_health_once()
        await asyncio.sleep(0.1)
        await bus.publish(Topics.CANDLES, make_candles([100.0])[0])
        await asyncio.sleep(0.1)
        recebidos = len(agente.recebidos)
        reinicios = agente.restarts
        await agente.stop()

        assert reinicios == 1, "o watchdog nao viu um agente surdo"
        assert recebidos == 1, "reassinou mas nao voltou a receber"

    async def test_stop_nao_pode_estourar_por_causa_do_alimentador_morto(
        self, settings, relogio_apertado
    ):
        """`close()` engolia so `CancelledError`.

        Um alimentador morto com outra excecao fazia `stop()` relancar: o estado
        nao ia para STOPPED, `on_stop()` nao rodava (broker e sessao HTTP
        ficavam abertos) e as demais caixas nao eram fechadas. Como o
        orquestrador envolve cada `agent.stop()` num `suppress(Exception)`, o
        desligamento ainda se declarava limpo -- e `system_stopped` ja tinha
        sido gravado, entao nem a subida seguinte denunciava.
        """
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteQueLibera(bus)

        await agente.start()
        await asyncio.sleep(0.05)

        async def morre_feio() -> None:
            raise ConnectionResetError("xread do bus caiu")

        caixa = agente._inboxes[Topics.CANDLES]
        caixa._feeder.cancel()
        caixa._feeder = asyncio.create_task(morre_feio())
        await asyncio.sleep(0.05)

        erro = None
        try:
            await agente.stop()
        except Exception as exc:  # pragma: no cover - so falha se a defesa sumir
            erro = exc

        assert erro is None, f"stop() estourou ({erro!r}); state={agente.state}"
        assert agente.state is AgentState.STOPPED
        assert agente.liberou, "on_stop() nao rodou: broker e sessao HTTP vazaram"
        assert bus.subscriber_count.get(Topics.CANDLES, 0) == 0, "assinatura vazou"


# ---------------------------------------------------------------------------
class TestCircuitBreakerBloqueiaAbertura:
    """D4 pelos dois lados: barra a ABERTURA sem nunca barrar o fechamento.

    Tirar `execution` de `pause_all` destravou o stop-loss e abriu o lado
    inverso: o pedido de compra JA PUBLICADO sobrevive na caixa de entrada
    duravel e era executado depois da trava. Medido com broker lento e sem
    pausar nada na mao: `('voo-2', 'buy', 'filled')` depois de
    `pause_all(actor="circuit_breaker")`.
    """

    def _compra(self, cid, symbol):
        from crypto_traders.domain.enums import ExchangeName, OrderType, Side
        from crypto_traders.domain.models import OrderRequest

        return OrderRequest(
            client_order_id=cid,
            signal_id=None,
            risk_event_id="aprovado-antes-da-trava",
            exchange=ExchangeName.BINANCE,
            symbol=symbol,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            notional=Decimal("100"),
            # O Execution Agent recusa abertura sem stop-loss antes de enviar,
            # e toda aprovacao de abertura do Risk Manager carrega o campo.
            stop_loss=Decimal("95"),
            take_profit=Decimal("110"),
            strategy="ma_crossover",
        )

    async def test_compra_em_voo_e_recusada_e_a_venda_continua_passando(self, settings):
        from crypto_traders.agents.execution import ExecutionAgent
        from crypto_traders.db.repositories import OrderRepository
        from crypto_traders.domain.enums import Side
        from crypto_traders.exchanges.paper import PaperBroker

        class BrokerLento(PaperBroker):
            async def place_order(self, request):
                await asyncio.sleep(0.25)
                return await super().place_order(request)

        bus = InMemoryEventBus()
        await bus.start()
        broker = BrokerLento(initial_balance=Decimal("10000"))
        broker.set_price("BTC", Decimal("100"))
        broker.set_price("ETH", Decimal("100"))
        execucao = ExecutionAgent(bus, broker, settings)
        orquestrador = BancadaOrquestrador(settings, {"execution": execucao}, bus)

        await execucao.start()
        await asyncio.sleep(0.1)

        # Rajada: a primeira ocupa o agente por 0,25s, a segunda espera na fila.
        await bus.publish(Topics.ORDER_REQUESTS, self._compra("voo-1", "BTC/USDT"))
        await bus.publish(Topics.ORDER_REQUESTS, self._compra("voo-2", "ETH/USDT"))
        await asyncio.sleep(0.05)

        # A trava dispara com a segunda compra ainda na fila.
        await orquestrador.pause_all(actor="circuit_breaker", reason="perda diaria")
        await asyncio.sleep(0.8)

        async with session_scope(settings) as session:
            ordens = await OrderRepository(session).list()
        compras_depois = [
            (o.client_order_id, o.side, o.status)
            for o in ordens
            if o.client_order_id == "voo-2"
        ]
        assert compras_depois == [], (
            f"compra executada depois do circuit breaker: {compras_depois}"
        )
        assert execucao.is_running, "a execucao nao pode parar: ela e quem fecha"

        # O outro lado de D4: com a MESMA trava ativa, o fechamento passa.
        venda = self._compra("saida-1", "BTC/USDT").model_copy(update={"side": Side.SELL})
        await bus.publish(Topics.ORDER_REQUESTS, venda)
        await asyncio.sleep(0.6)

        async with session_scope(settings) as session:
            ordens = await OrderRepository(session).list()
            registros = [
                r.action for r in await AuditLogRepository(session).list(limit=20)
            ]
        await execucao.stop()

        vendas = [o for o in ordens if o.client_order_id == "saida-1"]
        assert len(vendas) == 1, "o circuit breaker bloqueou o FECHAMENTO (D4)"
        assert vendas[0].side == "sell"
        assert "opening_refused" in registros, (
            "a compra recusada precisa ficar no audit_log com o motivo"
        )

    async def test_trava_armada_no_banco_volta_valendo_na_subida(self, settings):
        """A recusa de abertura mora em memoria e voltava solta a cada reinicio.

        O padrao de fabrica de uma trava nao pode ser "solta": se o processo cai
        com o circuit breaker armado, a subida seguinte tem que subir travada.
        """
        from crypto_traders.agents.execution import ExecutionAgent
        from crypto_traders.db.repositories import RiskConfigRepository
        from crypto_traders.exchanges.paper import PaperBroker

        async with session_scope(settings) as session:
            repositorio = RiskConfigRepository(session)
            await repositorio.get_or_create(settings.risk.model_dump(mode="json"))
            await repositorio.trip_circuit_breaker("perda diaria da execucao anterior")

        bus = InMemoryEventBus()
        await bus.start()
        execucao = ExecutionAgent(bus, PaperBroker(initial_balance=Decimal("100")), settings)
        orquestrador = BancadaOrquestrador(settings, {"execution": execucao}, bus)

        assert execucao.openings_blocked is None, "o teste depende de comecar solta"
        await orquestrador._apply_persisted_circuit_breaker()

        assert execucao.openings_blocked is not None, (
            "o processo subiu aceitando abrir posicao com o circuit breaker armado"
        )
        assert "perda diaria" in execucao.openings_blocked


# ---------------------------------------------------------------------------
class TestExecucaoAbandonada:
    """Desistir do agente de execucao deixa posicao SEM stop-loss.

    Medido antes: a saida de protecao era emitida para um agente morto, o ativo
    entrava em `_exiting` e nunca era reemitido, o log registrava
    `risk.protective_exit` como se o stop tivesse agido, e o unico alerta falava
    em "tomada de decisao pausada" -- que e justamente o que nao importa aqui.
    """

    def _snapshot_furado(self):
        from crypto_traders.domain.enums import ExchangeName
        from crypto_traders.domain.models import PortfolioSnapshot, Position

        return PortfolioSnapshot(
            total_value=Decimal("40100"),
            cash_value=Decimal("100"),
            positions_value=Decimal("40000"),
            positions=[
                Position(
                    exchange=ExchangeName.BINANCE,
                    asset="BTC",
                    quantity=Decimal("1"),
                    average_price=Decimal("50000"),
                    current_price=Decimal("40000"),  # 20% abaixo, stop de 3%
                )
            ],
        )

    async def _bancada(self, settings, bus):
        from crypto_traders.agents.execution import ExecutionAgent
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.exchanges.paper import PaperBroker

        broker = PaperBroker(initial_balance=Decimal("100"))
        broker.set_price("BTC", Decimal("40000"))
        risco = RiskManagerAgent(bus, settings)
        execucao = ExecutionAgent(bus, broker, settings)
        orquestrador = BancadaOrquestrador(
            settings, {"risk_manager": risco, "execution": execucao}, bus
        )
        orquestrador.risk_manager = risco
        return risco, execucao, orquestrador

    async def test_o_alerta_de_desistencia_diz_que_a_posicao_esta_sem_stop(
        self, settings, relogio_apertado
    ):
        bus = InMemoryEventBus()
        await bus.start()
        _, execucao, orquestrador = await self._bancada(settings, bus)
        alertas, escuta = await _escuta_alertas(bus)

        await orquestrador._give_up_on(
            "execution", execucao, reason="falha", attempts=3, now=datetime.now(UTC)
        )
        await asyncio.sleep(0.05)
        escuta.cancel()

        aviso = next(a for a in alertas if a["type"] == "agent_unrecoverable")
        texto = aviso["message"].lower()
        assert "stop-loss" in texto, f"o alerta nao diz que a posicao ficou exposta: {texto}"

    async def test_sem_execucao_o_sistema_avisa_em_vez_de_fingir_que_protegeu(
        self, settings, relogio_apertado
    ):
        from crypto_traders.db.repositories import OrderRepository

        bus = InMemoryEventBus()
        await bus.start()
        risco, execucao, orquestrador = await self._bancada(settings, bus)
        alertas, escuta = await _escuta_alertas(bus)

        await execucao.start()
        await asyncio.sleep(0.05)
        await execucao._cancel_task()
        orquestrador._given_up.add("execution")

        await orquestrador._on_snapshot(self._snapshot_furado())
        await asyncio.sleep(0.05)

        assert risco._exiting == set(), (
            "o ativo ficou travado como 'saida em voo' para um agente que nao executa"
        )
        exposto = [a for a in alertas if a["type"] == "positions_unprotected"]
        assert len(exposto) == 1, f"nenhum alerta diz que a posicao esta exposta: {alertas}"
        assert "BTC" in exposto[0]["message"]
        assert not [a for a in alertas if a["type"] == "protective_exit"], (
            "o log diria que o stop agiu quando ninguem iria executa-lo"
        )

        # Uma vez por episodio, e nao a cada snapshot (D19).
        await orquestrador._on_snapshot(self._snapshot_furado())
        await asyncio.sleep(0.05)
        assert len([a for a in alertas if a["type"] == "positions_unprotected"]) == 1

        # Com a execucao de volta, a protecao volta a ser emitida de verdade.
        orquestrador._given_up.discard("execution")
        await execucao.start()
        await asyncio.sleep(0.05)
        await orquestrador._on_snapshot(self._snapshot_furado())
        await asyncio.sleep(0.3)
        escuta.cancel()

        async with session_scope(settings) as session:
            ordens = await OrderRepository(session).list()
        await execucao.stop()

        assert len(ordens) == 1 and ordens[0].side == "sell", (
            "a protecao nao voltou a agir depois que a execucao voltou"
        )


# ---------------------------------------------------------------------------
class TestRegrasDeInfraestrutura:
    async def test_tarefa_interna_de_agente_se_cria_com_spawn(self):
        """O que nao e vigiado nao conta para a vitalidade -- e trava calado.

        Foi assim que o laco de avaliacao do Risk Manager ficou fora da conta:
        `asyncio.create_task` cria tarefa que ninguem vigia nem supervisiona.
        Dentro de agente, tarefa se cria com `self.spawn`.
        """
        from pathlib import Path

        pasta = Path(base_mod.__file__).parent
        # `base.py` implementa `spawn` e o pulso; `orchestrator.py` nao e agente
        # -- as tarefas dele (watchdog, alertas, on-chain) tem supervisao propria
        # em `_supervise_own_tasks`, com teste em `TestTarefasDoOrquestrador`.
        isentos = {"base.py", "orchestrator.py"}
        # Procurar a string `asyncio.create_task` deixava o guarda-corpo com o
        # mesmo ponto cego que ele existe para cobrir: `loop.create_task`,
        # `asyncio.ensure_future` e `TaskGroup` criam tarefa igualmente
        # invisivel a vitalidade e passavam batido. A varredura e por AST, e
        # nao por texto, para nao acusar a palavra escrita num comentario.
        import ast

        proibidos = {"create_task", "ensure_future", "TaskGroup", "to_thread"}
        ofensores: dict[str, list[str]] = {}
        for arquivo in sorted(pasta.glob("*.py")):
            if arquivo.name in isentos:
                continue
            achados = sorted(
                {
                    nome
                    for no in ast.walk(ast.parse(arquivo.read_text(encoding="utf-8")))
                    if isinstance(no, ast.Call)
                    for nome in [
                        no.func.attr
                        if isinstance(no.func, ast.Attribute)
                        else no.func.id
                        if isinstance(no.func, ast.Name)
                        else ""
                    ]
                    if nome in proibidos
                }
            )
            if achados:
                ofensores[arquivo.name] = achados
        assert ofensores == {}, f"tarefa de agente fora da vigilancia: {ofensores}"

    async def test_alerta_escrito_com_detail_chega_com_corpo(self, settings):
        """`_listen_alerts` lia so `message`: alertas com `detail` chegavam vazios.

        O alerta de stop-loss disparado e o de capital nao autorizado usam
        `detail` -- os dois chegavam ao operador com o corpo em branco.
        """
        bus = InMemoryEventBus()
        await bus.start()
        orquestrador = BancadaOrquestrador(settings, {}, bus)
        enviados: list[tuple[str, str]] = []

        class NotificadorDeTeste:
            async def send(self, titulo, mensagem):
                enviados.append((titulo, mensagem))

        orquestrador.notifier = NotificadorDeTeste()
        tarefa = asyncio.create_task(orquestrador._listen_alerts())
        await asyncio.sleep(0)

        await bus.publish(
            Topics.ALERTS,
            {"type": "protective_exit", "title": "Stop-loss acionado", "detail": "preco 1 < 2"},
        )
        await asyncio.sleep(0.05)
        tarefa.cancel()

        assert enviados == [("Stop-loss acionado", "preco 1 < 2")]


# ---------------------------------------------------------------------------
# Bancada da execucao: usada pelas duas classes abaixo.
# ---------------------------------------------------------------------------
def _snapshot_com_stop_rompido():
    """Carteira com BTC 20% abaixo do preco medio, e o stop e de 3%."""
    from crypto_traders.domain.enums import ExchangeName
    from crypto_traders.domain.models import PortfolioSnapshot, Position

    return PortfolioSnapshot(
        total_value=Decimal("40100"),
        cash_value=Decimal("100"),
        positions_value=Decimal("40000"),
        positions=[
            Position(
                exchange=ExchangeName.BINANCE,
                asset="BTC",
                quantity=Decimal("1"),
                average_price=Decimal("50000"),
                current_price=Decimal("40000"),
            )
        ],
    )


def _compra(cid: str):
    from crypto_traders.domain.enums import ExchangeName, OrderType, Side
    from crypto_traders.domain.models import OrderRequest

    return OrderRequest(
        client_order_id=cid,
        signal_id=None,
        risk_event_id="aprovado-antes",
        exchange=ExchangeName.BINANCE,
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        notional=Decimal("40"),
        # Idem: sem stop-loss a compra e recusada no ultimo portao.
        stop_loss=Decimal("38000"),
        take_profit=Decimal("44000"),
        strategy="ma_crossover",
    )


async def _bancada_execucao(settings, bus):
    from crypto_traders.agents.execution import ExecutionAgent
    from crypto_traders.agents.risk_manager import RiskManagerAgent
    from crypto_traders.exchanges.paper import PaperBroker

    broker = PaperBroker(initial_balance=Decimal("100"))
    broker.set_price("BTC", Decimal("40000"))
    risco = RiskManagerAgent(bus, settings)
    execucao = ExecutionAgent(bus, broker, settings)
    orquestrador = BancadaOrquestrador(
        settings, {"risk_manager": risco, "execution": execucao}, bus
    )
    orquestrador.risk_manager = risco
    return risco, execucao, orquestrador


class TestPausarAExecucaoNaoDesligaOStopLoss:
    """A porta que o operador realmente usa: `POST /agents/execution/pause`.

    Medido antes desta correcao, pausando por essa rota e entregando o snapshot
    abaixo: ZERO ordens enviadas, `_exiting` travado em {'BTC'} para sempre (so
    solta quando o ativo deixa a carteira), `risk.protective_exit` no log e o
    alerta ao dono dizendo "Stop-loss acionado ... Posicao fechada". Nenhum
    aviso de posicao exposta. O sistema afirmava ter protegido uma posicao que
    seguia aberta -- a familia do stop-loss que nao disparava.

    Duas defesas independentes, e as duas tem teste aqui:

    1. Pausar a execucao pela interface NAO pausa o agente: vira bloqueio de
       ABERTURA (D4). O stop-loss continua saindo de verdade.
    2. Se o agente ficar pausado por qualquer outro caminho, o Risk Manager nao
       emite a saida -- o sistema avisa em vez de fingir que protegeu.
    """

    async def test_pausar_pela_interface_e_o_stop_loss_ainda_dispara(self, settings):
        from crypto_traders.db.repositories import OrderRepository

        bus = InMemoryEventBus()
        await bus.start()
        _, execucao, orquestrador = await _bancada_execucao(settings, bus)
        alertas, escuta = await _escuta_alertas(bus)
        await execucao.start()
        await asyncio.sleep(0.05)

        # Exatamente o que a rota da interface faz.
        assert await orquestrador.pause_agent("execution", actor="user")

        assert not execucao.is_paused, (
            "pausar o agente que FECHA posicao desliga o stop-loss (D4)"
        )
        assert execucao.openings_blocked is not None, (
            "o operador pediu para parar de operar e a compra continuou liberada"
        )

        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await asyncio.sleep(0.5)

        async with session_scope(settings) as session:
            ordens = await OrderRepository(session).list()
        vendas = [o for o in ordens if o.side == "sell"]
        assert len(vendas) == 1, (
            f"a saida de protecao nao virou ordem com a execucao 'pausada': {ordens}"
        )

        # O outro lado de D4, com a mesma trava: a compra nao passa.
        await bus.publish(Topics.ORDER_REQUESTS, _compra("apos-pausa"))
        await asyncio.sleep(0.3)
        async with session_scope(settings) as session:
            ordens = await OrderRepository(session).list()
            registros = [r.action for r in await AuditLogRepository(session).list(limit=30)]
        escuta.cancel()
        await execucao.stop()

        assert [o for o in ordens if o.client_order_id == "apos-pausa"] == [], (
            "a compra passou depois de o operador mandar pausar a execucao"
        )
        assert "opening_refused" in registros
        assert "execution_openings_blocked" in registros, (
            "a conversao da pausa precisa ficar registrada no audit_log"
        )
        assert [a for a in alertas if a["type"] == "execution_pause_converted"], (
            "o operador clicou em pausar e nao foi avisado do que realmente valeu"
        )

    async def test_execucao_pausada_por_fora_nao_finge_que_protegeu(self, settings):
        """A defesa em profundidade: pausa que nao passou pelo orquestrador.

        `restart()` repoe a pausa, `pause_all` pode voltar a alcancar a execucao
        num refactor, e um script pode chamar `pause()` direto. Se acontecer, o
        Risk Manager nao pode emitir a saida para uma fila que ninguem drena.
        """
        from crypto_traders.db.repositories import OrderRepository

        bus = InMemoryEventBus()
        await bus.start()
        risco, execucao, orquestrador = await _bancada_execucao(settings, bus)
        alertas, escuta = await _escuta_alertas(bus)
        await execucao.start()
        await asyncio.sleep(0.05)

        execucao.pause()  # sem passar pelo orquestrador
        assert execucao.is_running and not execucao.deaf_topics, (
            "o teste so vale se o agente pausado continuar parecendo saudavel"
        )

        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await asyncio.sleep(0.3)

        async with session_scope(settings) as session:
            ordens = await OrderRepository(session).list()
        escuta.cancel()
        await execucao.stop()

        assert ordens == [], "o teste depende de a execucao pausada nao executar nada"
        assert risco._exiting == set(), (
            "o ativo ficou travado como 'saida em voo' para quem nao consome a fila"
        )
        tipos = [a["type"] for a in alertas]
        assert "positions_unprotected" in tipos, f"ninguem avisou o dono: {tipos}"
        assert "protective_exit" not in tipos, (
            "o alerta dizia 'Posicao fechada' e nenhuma ordem existia"
        )

    async def test_execucao_em_erro_com_tarefa_viva_nao_finge_que_protegeu(self, settings):
        """`state=ERROR` convive com tarefa viva -- e ate o watchdog passar, nada sai."""
        bus = InMemoryEventBus()
        await bus.start()
        risco, execucao, orquestrador = await _bancada_execucao(settings, bus)
        alertas, escuta = await _escuta_alertas(bus)
        await execucao.start()
        await asyncio.sleep(0.05)

        execucao.state = AgentState.ERROR
        execucao.last_error = "assinatura de order.requests caiu"
        assert execucao.is_running, "o teste so vale com a tarefa ainda de pe"

        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await asyncio.sleep(0.2)
        escuta.cancel()
        await execucao.stop()

        assert risco._exiting == set()
        assert "positions_unprotected" in [a["type"] for a in alertas]

    async def test_retomar_a_execucao_nao_desarma_o_circuit_breaker(self, settings):
        """Liberar a abertura ao retomar seria desarmar a trava por porta lateral."""
        bus = InMemoryEventBus()
        await bus.start()
        risco, execucao, orquestrador = await _bancada_execucao(settings, bus)

        await orquestrador.pause_all(actor="circuit_breaker", reason="perda diaria")
        risco._circuit_breaker_active = True
        assert execucao.openings_blocked is not None

        assert await orquestrador.resume_agent("execution", actor="user")
        assert execucao.openings_blocked is not None, (
            "retomar o agente liberou a abertura com o circuit breaker armado"
        )

        # Com a trava rearmada, a mesma acao libera.
        risco._circuit_breaker_active = False
        await orquestrador.resume_agent("execution", actor="user")
        assert execucao.openings_blocked is None


class TestTarefasDoOrquestrador:
    """Quem vigia o vigia. As tarefas do orquestrador nao tinham supervisao.

    `_watchdog`, `_alert_listener` e `_onchain_refresher` sao criadas no
    `start()` e ninguem olhava para elas de novo. A morte do listener de alertas
    apaga o UNICO caminho de notificacao do sistema -- e nada ficava vermelho.
    """

    def _orquestrador(self, settings, bus):
        orquestrador = BancadaOrquestrador(settings, {}, bus)
        enviados: list[tuple[str, str]] = []

        class NotificadorDeTeste:
            async def send(self, titulo, mensagem):
                enviados.append((titulo, mensagem))

        orquestrador.notifier = NotificadorDeTeste()
        return orquestrador, enviados

    async def test_a_tarefa_de_alertas_que_morre_e_recriada_e_denunciada(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        orquestrador, _ = self._orquestrador(settings, bus)
        alertas, escuta = await _escuta_alertas(bus)

        async def morre() -> None:
            raise RuntimeError("o xread caiu")

        orquestrador._alert_listener = asyncio.create_task(morre())
        await asyncio.sleep(0.05)
        assert orquestrador._alert_listener.done()

        await orquestrador._check_health_once()
        await asyncio.sleep(0.05)

        viva = orquestrador._alert_listener
        assert viva is not None and not viva.done(), (
            "o unico caminho de notificacao morreu e ninguem o recriou"
        )
        aviso = [a for a in alertas if a["type"] == "orchestrator_task_died"]
        assert len(aviso) == 1, f"a morte da tarefa nao foi denunciada: {alertas}"
        assert "nenhum alerta" in aviso[0]["message"].lower(), (
            "o alerta precisa dizer o que se perdeu, nao so que uma tarefa morreu"
        )

        viva.cancel()
        escuta.cancel()

    async def test_a_morte_do_watchdog_e_vista_pelo_caminho_do_snapshot(self, settings):
        """O watchdog nao pode ser o unico vigia, senao ninguem vigia ele.

        Este caminho e movido pelo Portfolio Agent, que e outra tarefa: e por
        isso que a supervisao e mutua.
        """
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.domain.models import PortfolioSnapshot

        bus = InMemoryEventBus()
        await bus.start()
        orquestrador, _ = self._orquestrador(settings, bus)
        orquestrador.risk_manager = RiskManagerAgent(bus, settings)
        orquestrador._agentes = {"risk_manager": orquestrador.risk_manager}

        async def morre() -> None:
            raise RuntimeError("watchdog caiu")

        orquestrador._watchdog = asyncio.create_task(morre())
        await asyncio.sleep(0.05)
        assert orquestrador._watchdog.done()

        await orquestrador._on_snapshot(
            PortfolioSnapshot(
                total_value=Decimal("100"),
                cash_value=Decimal("100"),
                positions_value=Decimal("0"),
                positions=[],
            )
        )
        await asyncio.sleep(0.05)

        vivo = orquestrador._watchdog
        assert vivo is not None and not vivo.done(), (
            "o watchdog morreu e nada o trouxe de volta: travamento deixa de ser detectado"
        )
        assert orquestrador._own_tasks_alive()["watchdog"] is True, (
            "o dashboard precisa poder mostrar que o vigia esta de pe"
        )
        vivo.cancel()

    async def test_o_desligamento_nao_ressuscita_o_que_ele_acabou_de_matar(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        orquestrador, _ = self._orquestrador(settings, bus)
        orquestrador._shutting_down = True

        async def morre() -> None:
            raise RuntimeError("cancelada no stop")

        orquestrador._alert_listener = asyncio.create_task(morre())
        await asyncio.sleep(0.05)
        await orquestrador._check_health_once()

        assert orquestrador._alert_listener.done(), (
            "a supervisao ressuscitou uma tarefa durante o desligamento"
        )

    async def test_assinatura_de_alertas_que_apenas_TERMINA_nao_encerra_o_laco(
        self, settings
    ):
        """Nem toda morte levanta excecao -- a licao que `_Inbox.alive` ja aprendeu.

        `_drain_alerts` retornando era lido como "bus encerrado, desligamento
        normal". Com o gerador terminando por outro motivo (o caso do bus
        Redis), o sistema seguia de pe sem NENHUM alerta chegar ao operador.
        """
        bus = InMemoryEventBus()
        await bus.start()
        orquestrador, enviados = self._orquestrador(settings, bus)

        chamadas = {"n": 0}
        original = bus.subscribe

        def assinatura_que_acaba(topic):
            if topic != Topics.ALERTS:
                return original(topic)
            chamadas["n"] += 1
            if chamadas["n"] == 1:
                return _geradora_vazia()
            return original(topic)

        orquestrador.bus.subscribe = assinatura_que_acaba  # type: ignore[method-assign]
        tarefa = asyncio.create_task(orquestrador._listen_alerts())
        await asyncio.sleep(0.05)

        assert not tarefa.done(), (
            "o laco de alertas encerrou sozinho: o operador para de receber tudo"
        )
        assert enviados and "alertas" in enviados[0][0].lower(), (
            "a queda do caminho de alertas nao pode ser avisada PELO caminho de alertas"
        )

        # Reassinou: um alerta publicado agora ainda chega ao operador.
        await asyncio.sleep(1.2)
        await bus.publish(Topics.ALERTS, {"type": "t", "title": "depois", "message": "ok"})
        await asyncio.sleep(0.1)
        tarefa.cancel()

        assert ("depois", "ok") in enviados, f"a reassinatura nao valeu: {enviados}"


async def _geradora_vazia():
    """Gerador que termina de imediato, sem levantar nada."""
    return
    yield  # pragma: no cover - inalcancavel, so marca a funcao como geradora
