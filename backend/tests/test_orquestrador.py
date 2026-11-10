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
from helpers import esperar_ate, make_candles

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


async def _vendas_gravadas(settings) -> list:
    """Ordens de venda no banco: a condicao de espera dos testes de stop-loss.

    Antes era `sleep(0.3)`/`sleep(0.5)` e torcer para a execucao ter consumido o
    pedido nesse tempo -- num executor de CI lento, falha sem defeito.
    """
    from crypto_traders.db.repositories import OrderRepository

    async with session_scope(settings) as session:
        return [o for o in await OrderRepository(session).list() if o.side == "sell"]


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
        await esperar_ate(lambda: agente.subscriptions_ready)

        # Um evento no inicio, como na subida real (o priming publica candles),
        # e depois o silencio legitimo do timeframe diario.
        await bus.publish(Topics.CANDLES, make_candles([100.0])[0])
        await esperar_ate(lambda: agente.recebidos)
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
        await esperar_ate(lambda: agente.subscriptions_ready)
        await bus.publish(Topics.CANDLES, make_candles([100.0])[0])
        await esperar_ate(lambda: agente.recebidos)

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
        await esperar_ate(lambda: agente.subscriptions_ready)

        rajada = make_candles([100.0 + i for i in range(16)])
        subida_original = agente.start

        async def sobe_publicando_no_meio() -> None:
            # Janela exata: a tarefa antiga ja morreu, a nova ainda nao existe.
            for candle in rajada:
                await bus.publish(Topics.CANDLES, candle)
            await subida_original()

        agente.start = sobe_publicando_no_meio
        # Travado ha uma hora: sem batida E sem subida recente. Recuar so a batida
        # deixava um agente que acabou de subir parecer travado -- a regra que
        # reiniciava em laco o agente recem-reiniciado (corrigida em 04/11/2026).
        agente.last_beat = datetime.now(UTC) - timedelta(hours=1)
        agente.started_at = datetime.now(UTC) - timedelta(hours=1)

        await orquestrador._check_health_once()
        await esperar_ate(lambda: len(agente.recebidos) >= 16)

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
        await esperar_ate(lambda: agente.subscriptions_ready)

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
        await esperar_ate(lambda: agente.subscriptions_ready)
        await bus.publish(Topics.CANDLES, make_candles([100.0], symbol="ETH/USDT")[0])
        await esperar_ate(lambda: agente.recebidos)

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
        await esperar_ate(lambda: agente.subscriptions_ready)
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
        await esperar_ate(lambda: vivo.subscriptions_ready)

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
        await esperar_ate(lambda: agente.subscriptions_ready)

        # Primeiro episodio: travado no processamento.
        await bus.publish(Topics.CANDLES, make_candles([100.0])[0])
        await esperar_ate(lambda: agente.recebidos)
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
        await esperar_ate(lambda: agente.subscriptions_ready)
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
        await esperar_ate(lambda: agente.subscriptions_ready)

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

    def precos_para_avaliacao(self) -> dict:
        return dict(self.latest_prices)


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
        await esperar_ate(lambda: agente.subscriptions_ready)
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
        await esperar_ate(lambda: agente.subscriptions_ready)
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
        await esperar_ate(lambda: agente.subscriptions_ready)
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
        await esperar_ate(lambda: _vendas_gravadas(settings), prazo=5)

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
        await esperar_ate(lambda: execucao.subscriptions_ready)
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
        await esperar_ate(lambda: execucao.subscriptions_ready)
        await orquestrador._on_snapshot(self._snapshot_furado())
        await esperar_ate(lambda: _vendas_gravadas(settings), prazo=5)
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
        await esperar_ate(lambda: execucao.subscriptions_ready)

        # Exatamente o que a rota da interface faz.
        assert await orquestrador.pause_agent("execution", actor="user")

        assert not execucao.is_paused, (
            "pausar o agente que FECHA posicao desliga o stop-loss (D4)"
        )
        assert execucao.openings_blocked is not None, (
            "o operador pediu para parar de operar e a compra continuou liberada"
        )

        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await esperar_ate(lambda: _vendas_gravadas(settings), prazo=5)

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
        await esperar_ate(lambda: execucao.subscriptions_ready)

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
        await esperar_ate(lambda: execucao.subscriptions_ready)

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

    async def test_retomar_tudo_nao_desarma_o_circuit_breaker(self, settings):
        """`resume_all` soltava decisao e abertura sem olhar a trava.

        So a rota `POST /agents/resume-all` conferia o circuit breaker: quem
        chamasse o orquestrador por outro caminho desarmava a trava sem rearme
        (auditoria de 09/11/2026, orquestracao L6). A regra mora agora no
        orquestrador, junto de `_allow_openings_for`.
        """
        bus = InMemoryEventBus()
        await bus.start()
        risco, execucao, orquestrador = await _bancada_execucao(settings, bus)

        await orquestrador.pause_all(actor="circuit_breaker", reason="perda diaria")
        risco._circuit_breaker_active = True

        assert await orquestrador.resume_all(actor="user") is False
        assert execucao.openings_blocked is not None
        assert risco.is_paused

        risco._circuit_breaker_active = False
        assert await orquestrador.resume_all(actor="user") is True
        assert execucao.openings_blocked is None
        assert not risco.is_paused


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


# ---------------------------------------------------------------------------
# P0 #8 -- retomar um agente desistido tem que retoma-lo de verdade
# ---------------------------------------------------------------------------
async def _desistir_de_verdade(orquestrador, nome: str, agente) -> None:
    """Leva o agente a desistencia pelo caminho real do watchdog.

    `_restart_agent` reinicia ANTES de desistir, entao o agente desistido fica
    quase sempre com a tarefa VIVA -- o caso que `_given_up.add(...)` na mao
    nao exercia.
    """
    agora = datetime.now(UTC)
    for _ in range(orch_mod.MAX_RESTART_ATTEMPTS):
        await orquestrador._restart_agent(nome, agente, crashed=False, stale=True, now=agora)
    assert nome in orquestrador._given_up
    assert agente.is_running, "o cenario do relatorio e o desistido com a tarefa viva"


class TestRetomarODesistido:
    """Achado H1 da orquestracao (P0 #8).

    Medido no codigo anterior: o watchdog desiste da execucao logo depois de um
    `restart()` bem-sucedido, o operador clica em "retomar", a rota responde 200
    e `_given_up` continua com `execution` (so era limpo com a tarefa morta). A
    cada snapshot `_execution_unavailable` dizia "o watchdog desistiu" e o
    stop-loss NAO rodava, com o agente saudavel, ate reiniciar o processo.
    """

    async def test_retomar_a_execucao_desistida_devolve_o_stop(
        self, settings, relogio_apertado
    ):
        from crypto_traders.db.repositories import OrderRepository

        bus = InMemoryEventBus()
        await bus.start()
        _, execucao, orquestrador = await _bancada_execucao(settings, bus)
        await execucao.start()
        await esperar_ate(lambda: execucao.subscriptions_ready)
        await _desistir_de_verdade(orquestrador, "execution", execucao)

        assert await orquestrador.resume_agent("execution", actor="user")

        assert "execution" not in orquestrador._given_up, (
            "o operador retomou e o agente continuou desistido"
        )
        assert orquestrador._execution_unavailable() is None
        assert execucao.openings_blocked is None, (
            "retomar a execucao (sem circuit breaker) tem que soltar a abertura"
        )

        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await esperar_ate(lambda: _vendas_gravadas(settings), prazo=5)
        async with session_scope(settings) as session:
            ordens = await OrderRepository(session).list()
        await execucao.stop()

        assert [o for o in ordens if o.side == "sell"], (
            "a execucao retomada nao voltou a executar o stop-loss"
        )

    async def test_retomar_tudo_reabilita_os_desistidos_e_o_watchdog_volta_a_vigiar(
        self, settings, relogio_apertado
    ):
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteDeEvento(bus)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)
        await agente.start()
        await esperar_ate(lambda: agente.subscriptions_ready)
        await _desistir_de_verdade(orquestrador, "consumidor", agente)

        await orquestrador.resume_all(actor="user")
        assert "consumidor" not in orquestrador._given_up

        # Volta a ser vigiado: travado de novo, o watchdog reinicia.
        antes = agente.restarts
        agente.last_beat = datetime.now(UTC) - timedelta(hours=1)
        agente.started_at = agente.last_beat
        await orquestrador._check_health_once()
        await agente.stop()

        assert agente.restarts == antes + 1, "o watchdog seguiu ignorando o agente retomado"

    async def test_retomar_agente_em_erro_com_a_tarefa_viva_o_reinicia(
        self, settings, relogio_apertado
    ):
        """`resume()` sobrescrevia ERROR por RUNNING: o dashboard ficava verde
        com o agente quebrado, e o watchdog deixava de ve-lo como falha."""
        bus = InMemoryEventBus()
        await bus.start()
        agente = AgenteDeEvento(bus)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)
        await agente.start()
        await esperar_ate(lambda: agente.subscriptions_ready)
        agente.state = AgentState.ERROR
        agente.last_error = "tarefa interna morreu"
        antes = agente.restarts

        assert await orquestrador.resume_agent("consumidor")
        await agente.stop()

        assert agente.restarts == antes + 1


# ---------------------------------------------------------------------------
# P0 #9 -- alerta do primeiro retrato chega ao operador
# ---------------------------------------------------------------------------
class TestAlertasDaSubida:
    """Achado H2 da orquestracao (P0 #9).

    O primeiro `_on_snapshot` roda dentro de `_prime_portfolio`, e o assinante
    de ALERTS so era criado depois. O bus em memoria sem assinante descarta, e
    a flag de episodio (`_sizing_alerted`) ja ficava armada: com 22 USDC na
    conta o sistema subia verde rejeitando todo sinal, e o aviso NUNCA saia.
    """

    async def test_alerta_emitido_no_primeiro_retrato_chega_ao_canal(self, tmp_path):
        from crypto_traders.config import RiskSettings, Settings, TradingSettings
        from crypto_traders.db.session import dispose_engine
        from crypto_traders.simulacao import AgenteDeCenario, MercadoSimulado

        await dispose_engine()
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{(tmp_path / 'subida.db').as_posix()}",
            trading=TradingSettings(
                symbols=["BTC/USDT"],
                timeframe="1d",
                strategies=["ma_crossover"],
                quote_currency="USDT",
                candle_history_limit=100,
                market_data_interval_seconds=3600,
                portfolio_interval_seconds=3600,
                paper_initial_balance=Decimal("22"),
            ),
            risk=RiskSettings(
                max_order_notional=Decimal("100"),
                max_order_pct_portfolio=0.10,
                max_asset_exposure_pct=0.50,
                min_order_notional=Decimal("10"),
                asset_whitelist=["BTC", "USDT"],
                symbol_whitelist=["BTC/USDT"],
            ),
        )
        mercado = MercadoSimulado(timeframe="1d")
        mercado.adicionar_par("BTC/USDT", [Decimal(50000 + 10 * i) for i in range(61)])
        cenario = AgenteDeCenario(settings, mercado)

        entregues: list[tuple[str, str]] = []

        class CanalDeTeste:
            async def send(self, titulo, mensagem):
                entregues.append((titulo, mensagem))
                return []

        cenario.orquestrador.notifier = CanalDeTeste()
        await cenario.iniciar()
        try:
            await asyncio.sleep(0.1)
            publicados = cenario.alertas_do_tipo("sizing_infeasible")
        finally:
            await cenario.encerrar()

        assert publicados, "o cenario depende do aviso de dimensionamento na subida"
        titulo = publicados[0]["title"]
        assert titulo in [t for t, _ in entregues], (
            f"o alerta do primeiro retrato foi publicado e nao chegou ao canal: {entregues}"
        )


# ---------------------------------------------------------------------------
# P0 #11 -- a vitalidade que a sonda do conteiner consulta
# ---------------------------------------------------------------------------
class TestVitalidade:
    """`vitalidade()` e o que a sonda de saude do conteiner pergunta.

    A sonda antiga provava so que o objeto orquestrador existia. Aqui cada
    estado que deixa o stop parado tem que aparecer como problema -- e a pausa
    do operador, que nao e falha, nao.
    """

    async def _sistema(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agentes = {nome: AgenteDeEvento(bus) for nome in orch_mod.AGENTES_CRITICOS}
        orquestrador = BancadaOrquestrador(settings, agentes, bus)
        for agente in agentes.values():
            await agente.start()

        async def para_sempre() -> None:
            await asyncio.Event().wait()

        orquestrador._watchdog = asyncio.create_task(para_sempre())
        orquestrador._alert_listener = asyncio.create_task(para_sempre())
        orquestrador._watchdog_ultimo_ciclo = orch_mod.time.monotonic()
        await asyncio.sleep(0.05)
        return orquestrador, agentes

    async def _desmontar(self, orquestrador, agentes):
        for tarefa in (orquestrador._watchdog, orquestrador._alert_listener):
            if tarefa is not None:
                tarefa.cancel()
        for agente in agentes.values():
            await agente.stop()

    async def test_sistema_de_pe_e_saudavel(self, settings, relogio_apertado):
        orquestrador, agentes = await self._sistema(settings)
        try:
            assert orquestrador.vitalidade() == []
        finally:
            await self._desmontar(orquestrador, agentes)

    async def test_agente_critico_sem_batida_e_problema(self, settings, relogio_apertado):
        orquestrador, agentes = await self._sistema(settings)
        try:
            velho = datetime.now(UTC) - timedelta(hours=1)
            agentes["execution"].last_beat = velho
            agentes["execution"].started_at = velho
            problemas = orquestrador.vitalidade()
        finally:
            await self._desmontar(orquestrador, agentes)
        assert any(p.startswith("execution") for p in problemas), problemas

    async def test_agente_critico_morto_ou_desistido_e_problema(
        self, settings, relogio_apertado
    ):
        orquestrador, agentes = await self._sistema(settings)
        try:
            await agentes["portfolio"]._cancel_task()
            orquestrador._given_up.add("market_data")
            problemas = orquestrador.vitalidade()
        finally:
            await self._desmontar(orquestrador, agentes)
        assert any(p.startswith("portfolio") for p in problemas), problemas
        assert any(p.startswith("market_data") for p in problemas), problemas

    async def test_laco_do_watchdog_parado_e_problema(self, settings, relogio_apertado):
        orquestrador, agentes = await self._sistema(settings)
        try:
            orquestrador._watchdog_ultimo_ciclo = orch_mod.time.monotonic() - 3600
            problemas = orquestrador.vitalidade()
        finally:
            await self._desmontar(orquestrador, agentes)
        assert any("watchdog" in p for p in problemas), (
            "watchdog vivo e pendurado parecia saudavel"
        )

    async def test_pausa_do_operador_nao_e_problema(self, settings, relogio_apertado):
        """Reiniciar o conteiner por causa de uma pausa desfaria o pedido."""
        orquestrador, agentes = await self._sistema(settings)
        try:
            agentes["risk_manager"].pause()
            velho = datetime.now(UTC) - timedelta(hours=1)
            agentes["risk_manager"].last_beat = velho
            assert orquestrador.vitalidade() == []
        finally:
            await self._desmontar(orquestrador, agentes)

    async def test_o_ciclo_do_watchdog_marca_que_o_laco_anda(
        self, settings, relogio_apertado, monkeypatch
    ):
        monkeypatch.setattr(orch_mod, "WATCHDOG_INTERVAL_SECONDS", 0.01)
        bus = InMemoryEventBus()
        await bus.start()
        orquestrador = BancadaOrquestrador(settings, {}, bus)
        tarefa = asyncio.create_task(orquestrador._watch_health())
        await asyncio.sleep(0.1)
        tarefa.cancel()
        assert orquestrador._watchdog_ultimo_ciclo is not None


# ---------------------------------------------------------------------------
# M3 da orquestracao -- um so `crypto-traders run` por banco
# ---------------------------------------------------------------------------
class _ResultadoFalso:
    def __init__(self, valor):
        self._valor = valor

    def scalar(self):
        return self._valor


class _ConexaoFalsa:
    def __init__(self, livre: bool) -> None:
        self.livre = livre
        self.sql: list[str] = []
        self.fechada = False
        self.invalidada = False

    async def execute(self, clausula, parametros=None):
        self.sql.append(str(clausula))
        return _ResultadoFalso(self.livre if "try_advisory" in str(clausula) else True)

    async def commit(self):
        return None

    async def invalidate(self):
        self.invalidada = True

    async def close(self):
        self.fechada = True


class _MotorPostgresFalso:
    """So o que a trava usa do engine: o dialeto e uma conexao dedicada."""

    def __init__(self, livre: bool) -> None:
        self.conexao = _ConexaoFalsa(livre)
        self.dialect = type("Dialeto", (), {"name": "postgresql"})()

    async def connect(self):
        return self.conexao


class TestInstanciaUnica:
    """Dois `crypto-traders run` contra o mesmo banco sao duas execucoes.

    `docker compose run --rm crypto crypto-traders run` com o contêiner do
    sistema de pe sobe um segundo orquestrador: os dois avaliam o mesmo
    snapshot e emitem compra e protecao em dobro (o `client_order_id` e por
    instancia, o UNIQUE nao pega). E a subida da segunda grava `system_started`
    e confunde a deteccao de morte da primeira.
    """

    async def test_segunda_instancia_recusa_subir_sem_escrever_nada(
        self, settings, monkeypatch
    ):
        motor = _MotorPostgresFalso(livre=False)
        monkeypatch.setattr(orch_mod, "get_engine", lambda _settings=None: motor)
        segunda = Orchestrator(settings)

        with pytest.raises(orch_mod.InstanciaDuplicada):
            await segunda.start()
        # O que o runtime faz quando a subida falha.
        await segunda.stop()

        async with session_scope(settings) as session:
            acoes = [r.action for r in await AuditLogRepository(session).list(limit=20)]
        assert "system_started" not in acoes
        assert "system_stopped" not in acoes, (
            "a recusa gravou uma parada 'limpa' por cima da execucao que esta de pe"
        )
        assert motor.conexao.fechada

    async def test_a_trava_e_mantida_e_solta_no_desligamento(self, settings, monkeypatch):
        motor = _MotorPostgresFalso(livre=True)
        monkeypatch.setattr(orch_mod, "get_engine", lambda _settings=None: motor)
        orquestrador = Orchestrator(settings)

        await orquestrador._tomar_instancia_unica()
        assert not motor.conexao.fechada, "a trava de sessao morre com a conexao"
        await orquestrador._soltar_instancia_unica()

        assert any("advisory_unlock" in sql for sql in motor.conexao.sql)
        # De volta ao pool a sessao (e a trava) sobreviveria: a conexao e descartada.
        assert motor.conexao.invalidada

    async def test_sqlite_nao_usa_a_trava(self, settings):
        orquestrador = Orchestrator(settings)
        await orquestrador._tomar_instancia_unica()
        assert orquestrador._conexao_da_trava is None


@pytest.mark.skipif(
    not __import__("os").getenv("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL nao definida"
)
async def test_postgres_real_recusa_a_segunda_instancia():
    import os

    from crypto_traders.config import Settings
    from crypto_traders.db.session import dispose_engine

    await dispose_engine()
    configurado = Settings(database_url=os.environ["POSTGRES_TEST_URL"])
    primeira, segunda = Orchestrator(configurado), Orchestrator(configurado)
    await primeira._tomar_instancia_unica()
    try:
        with pytest.raises(orch_mod.InstanciaDuplicada):
            await segunda._tomar_instancia_unica()
    finally:
        await primeira._soltar_instancia_unica()
    # Solta de verdade: agora a segunda sobe.
    await segunda._tomar_instancia_unica()
    await segunda._soltar_instancia_unica()
    await dispose_engine()


# ---------------------------------------------------------------------------
# M1 da orquestracao -- a parada drena o que esta em voo, com prazo
# ---------------------------------------------------------------------------
class TestParadaDrena:
    """`stop()` cancelava na hora o evento em processamento.

    Medido no codigo anterior: `docker stop` no meio do `place_order` levava o
    `CancelledError` por cima do `except Exception` da execucao, a ordem ficava
    PENDING sem o alerta de "desfecho desconhecido", nenhuma linha
    `agent.event_lost_*` saia (o registro so existia no restart) e o audit_log
    dizia "desligamento solicitado" -- enquanto o docstring do runtime prometia
    que os agentes terminam o ciclo corrente antes de parar.
    """

    async def _em_voo(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        trava = asyncio.Event()
        agente = AgenteDeEvento(bus, trava=trava)
        await agente.start()
        await esperar_ate(lambda: agente.subscriptions_ready)
        await bus.publish(Topics.CANDLES, make_candles([100.0])[0])
        await esperar_ate(lambda: agente.recebidos)
        assert agente.recebidos, "o cenario depende do evento em processamento"
        return bus, agente, trava

    async def test_o_evento_em_processamento_termina_antes_da_parada(self, settings):
        _, agente, trava = await self._em_voo(settings)
        caixa = agente._inboxes[Topics.CANDLES]

        parada = asyncio.create_task(agente.stop())
        await asyncio.sleep(0.1)
        assert not parada.done(), "a parada cancelou o evento no meio em vez de drenar"
        trava.set()
        await asyncio.wait_for(parada, timeout=5)

        assert agente.lost_events == 0
        assert caixa.handled == 1, "o evento em voo nao chegou ao fim"
        assert agente.deixados_na_parada == []

    async def test_passado_o_prazo_cancela_e_registra_o_que_ficou(
        self, settings, monkeypatch
    ):
        monkeypatch.setattr(base_mod, "PRAZO_DRENAGEM_SEGUNDOS", 0.2)
        bus, agente, _ = await self._em_voo(settings)
        await bus.publish(Topics.CANDLES, make_candles([100.0, 101.0])[1])
        await asyncio.sleep(0.05)

        await asyncio.wait_for(agente.stop(), timeout=5)

        assert agente.lost_events == 1
        assert any("candles" in d for d in agente.deixados_na_parada), (
            agente.deixados_na_parada
        )
        assert any("pendente" in d for d in agente.deixados_na_parada), (
            "o pedido que ficou na fila sumia sem contagem"
        )

    async def test_a_parada_do_sistema_registra_o_que_ficou_no_audit_log(
        self, settings, monkeypatch
    ):
        monkeypatch.setattr(base_mod, "PRAZO_DRENAGEM_SEGUNDOS", 0.2)
        bus, agente, _ = await self._em_voo(settings)
        orquestrador = BancadaOrquestrador(settings, {"consumidor": agente}, bus)

        await asyncio.wait_for(orquestrador.stop(), timeout=5)

        async with session_scope(settings) as session:
            registros = await AuditLogRepository(session).list(limit=10)
        parada = next(r for r in registros if r.action == "system_stopped")
        assert parada.after and parada.after.get("deixados_na_parada"), (
            "a parada se declarava limpa com um evento cortado no meio"
        )
