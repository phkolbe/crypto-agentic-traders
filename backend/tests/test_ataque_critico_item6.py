"""Ataque ao item 6: o sistema para de PROTEGER e continua deixando ABRIR.

O item 6 deixou o orquestrador honesto sobre a PROTECAO. Quando o agente de
execucao nao esta agindo (`_execution_unavailable`), `_on_snapshot` se recusa a
emitir a saida de protecao -- emitir para uma fila que ninguem drena trava o
ativo em `_exiting` e escreve `risk.protective_exit` no log como se o stop
tivesse agido -- e avisa o dono com `positions_unprotected`.

Ficou de fora o outro lado da mesma moeda: a ABERTURA seguia liberada. No MESMO
ciclo em que o sistema declara em alto que NAO consegue fechar posicao, o Risk
Manager continuava aprovando compras, que se empilhavam na caixa de entrada
duravel da execucao e eram executadas assim que ela voltasse. Medido antes desta
correcao, com o agente em `state=ERROR` e a tarefa viva: alerta
`positions_unprotected` emitido, nenhuma venda de protecao -- e a compra
publicada em seguida terminou gravada e enviada ao broker.

Isso contraria a disciplina 3 do projeto ("o estado seguro nao e arrisca menos,
e nao arrisca") e e da familia dos tres defeitos silenciosos daqui: o log diz uma
coisa e o sistema faz outra.

A trava tem que ser ASSIMETRICA, e por isso os testes medem as duas direcoes
(D4): bloquear a abertura nunca pode bloquear o fechamento. Foi exatamente esse
o defeito grave que o item 6 encontrou na rodada 1, quando `pause_all` pausava o
agente de execucao e a trava de protecao desligava o stop-loss.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from crypto_traders.agents.base import BaseAgent
from crypto_traders.agents.execution import ExecutionAgent
from crypto_traders.agents.orchestrator import Orchestrator
from crypto_traders.agents.risk_manager import RiskManagerAgent
from crypto_traders.bus import InMemoryEventBus, Topics
from crypto_traders.db.repositories import AuditLogRepository, OrderRepository
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import (
    AgentState,
    ExchangeName,
    OrderType,
    Side,
)
from crypto_traders.domain.models import OrderRequest, PortfolioSnapshot, Position
from crypto_traders.exchanges.paper import PaperBroker


# ---------------------------------------------------------------------------
# Bancada
# ---------------------------------------------------------------------------
class BancadaOrquestrador(Orchestrator):
    """Orquestrador com o conjunto de agentes escolhido pelo teste.

    O `start()` real precisa de exchange, banco e descoberta; o que esta em
    julgamento aqui e o que `_on_snapshot` faz com a execucao indisponivel.
    """

    def __init__(self, settings, agentes: dict[str, BaseAgent], bus: InMemoryEventBus) -> None:
        super().__init__(settings)
        self.bus = bus
        self._agentes = agentes

    @property
    def agents(self) -> dict[str, BaseAgent]:
        return self._agentes


async def _bancada(settings, bus):
    """Risk Manager e Execution de verdade, com broker de papel.

    O BTC creditado no broker existe para que o FECHAMENTO seja possivel: um
    teste em que a venda falha por falta de saldo nao provaria nada sobre D4.
    """
    broker = PaperBroker(initial_balance=Decimal("100"))
    broker.set_price("BTC", Decimal("40000"))
    broker.credit("BTC", Decimal("1"))
    risco = RiskManagerAgent(bus, settings)
    execucao = ExecutionAgent(bus, broker, settings)
    orquestrador = BancadaOrquestrador(
        settings, {"risk_manager": risco, "execution": execucao}, bus
    )
    orquestrador.risk_manager = risco
    return risco, execucao, orquestrador


async def _escuta_alertas(bus: InMemoryEventBus) -> tuple[list[dict], asyncio.Task]:
    alertas: list[dict] = []

    async def escuta() -> None:
        async for alerta in bus.subscribe(Topics.ALERTS):
            alertas.append(alerta)

    tarefa = asyncio.create_task(escuta())
    await asyncio.sleep(0)
    return alertas, tarefa


def _snapshot_com_stop_rompido() -> PortfolioSnapshot:
    """Carteira com BTC 20% abaixo do preco medio, e o stop e de 3%."""
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


def _pedido(cid: str, side: Side) -> OrderRequest:
    return OrderRequest(
        client_order_id=cid,
        signal_id=None,
        risk_event_id="aprovado-antes-da-queda",
        exchange=ExchangeName.BINANCE,
        symbol="BTC/USDT",
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        notional=Decimal("40"),
        # Abertura sem stop-loss e recusada no ultimo portao da execucao, entao
        # sem isto o teste passaria pelo motivo errado. Fechamento publica None
        # por construcao -- e a mesma assimetria de D4.
        stop_loss=Decimal("38000") if side is Side.BUY else None,
        take_profit=Decimal("44000") if side is Side.BUY else None,
        strategy="ma_crossover",
    )


def _adoece(execucao: ExecutionAgent) -> None:
    """Deixa a execucao indisponivel no estado que mais parece saudavel.

    `state=ERROR` com a tarefa viva e o unico impedimento em que o agente
    REALMENTE continua drenando a fila -- e por isso o unico em que o defeito
    aparece ponta a ponta: a compra publicada depois do aviso era executada de
    verdade, e nao apenas enfileirada.
    """
    execucao.state = AgentState.ERROR
    execucao.last_error = "assinatura de order.requests caiu"


async def _ordens(settings) -> list:
    async with session_scope(settings) as session:
        return await OrderRepository(session).list()


# ---------------------------------------------------------------------------
class TestSemProtecaoTambemSemAbertura:
    async def test_execucao_indisponivel_barra_a_compra_e_deixa_a_venda_passar(
        self, settings
    ):
        """As duas direcoes no mesmo teste, de proposito.

        Uma metade sozinha nao prova nada: barrar a compra e facil pausando o
        agente, e foi assim que a rodada 1 desligou o stop-loss.
        """
        bus = InMemoryEventBus()
        await bus.start()
        _, execucao, orquestrador = await _bancada(settings, bus)
        alertas, escuta = await _escuta_alertas(bus)
        await execucao.start()
        await asyncio.sleep(0.05)

        _adoece(execucao)
        assert execucao.is_running and not execucao.is_paused, (
            "o teste so vale se o agente indisponivel continuar drenando a fila"
        )
        assert orquestrador._execution_unavailable() is not None

        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await asyncio.sleep(0.05)

        assert "positions_unprotected" in [a["type"] for a in alertas], (
            "o teste so vale se o sistema tiver declarado que nao consegue fechar"
        )
        assert execucao.openings_blocked is not None, (
            "o sistema disse em alto que NAO fecha posicao e seguiu deixando abrir"
        )

        await bus.publish(Topics.ORDER_REQUESTS, _pedido("abertura", Side.BUY))
        await bus.publish(Topics.ORDER_REQUESTS, _pedido("fechamento", Side.SELL))
        await asyncio.sleep(0.4)

        ordens = await _ordens(settings)
        async with session_scope(settings) as session:
            registros = [r.action for r in await AuditLogRepository(session).list(limit=40)]
        # Lidos antes do desligamento do teste: depois do `stop()` todo agente
        # esta parado, e a pergunta de D4 e sobre o agente em operacao.
        seguia_de_pe = execucao.is_running
        seguia_solto = not execucao.is_paused
        escuta.cancel()
        await execucao.stop()

        compras = [o for o in ordens if o.client_order_id == "abertura"]
        assert compras == [], (
            f"posicao aberta no ciclo em que o sistema nao consegue fechar: {compras}"
        )
        assert "opening_refused" in registros, (
            "a recusa precisa ficar no audit_log com o motivo"
        )

        # D4, a outra direcao: bloquear a abertura nunca pode bloquear o
        # fechamento -- era esse o defeito grave da rodada 1.
        vendas = [o for o in ordens if o.client_order_id == "fechamento"]
        assert len(vendas) == 1 and vendas[0].side == "sell", (
            f"a trava de abertura bloqueou o FECHAMENTO (D4): {ordens}"
        )
        assert seguia_solto, "o agente que FECHA posicao foi pausado (D4)"
        assert seguia_de_pe, "o agente que FECHA posicao foi derrubado (D4)"

    async def test_o_dono_e_avisado_de_que_a_abertura_parou(self, settings):
        """Parar de comprar em silencio seria trocar um defeito por outro.

        E o estado que `_check_sizing` existe para denunciar: de pe, com
        heartbeat verde e dashboard atualizando, recusando tudo sem dizer por
        que. O aviso sai uma vez por episodio (D19), nao a cada snapshot.
        """
        bus = InMemoryEventBus()
        await bus.start()
        _, execucao, orquestrador = await _bancada(settings, bus)
        alertas, escuta = await _escuta_alertas(bus)
        await execucao.start()
        await asyncio.sleep(0.05)

        _adoece(execucao)
        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await asyncio.sleep(0.05)
        escuta.cancel()
        await execucao.stop()

        avisos = [a for a in alertas if a["type"] == "openings_blocked_unavailable"]
        assert len(avisos) == 1, (
            f"o dono nao foi avisado (ou foi avisado a cada snapshot): {alertas}"
        )
        texto = avisos[0]["message"].lower()
        assert "fechar" in texto and "abrir" in texto, (
            f"o aviso nao explica que quem nao fecha tambem nao abre: {texto}"
        )

    async def test_a_abertura_volta_quando_a_execucao_volta(self, settings):
        """Travar e nunca destravar seria um defeito novo, nao uma protecao.

        Um erro transitorio na execucao nao pode aposentar o sistema em
        silencio: quando o impedimento sai, a abertura volta -- no mesmo ponto
        em que o aviso de posicao exposta e rearmado.
        """
        bus = InMemoryEventBus()
        await bus.start()
        _, execucao, orquestrador = await _bancada(settings, bus)
        await execucao.start()
        await asyncio.sleep(0.05)

        _adoece(execucao)
        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await asyncio.sleep(0.05)
        assert execucao.openings_blocked is not None, "a trava nao armou; teste invalido"

        execucao.state = AgentState.RUNNING
        execucao.last_error = None
        assert orquestrador._execution_unavailable() is None
        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await asyncio.sleep(0.05)

        assert execucao.openings_blocked is None, (
            "a execucao voltou a agir e a abertura ficou travada para sempre"
        )

        await bus.publish(Topics.ORDER_REQUESTS, _pedido("abertura-liberada", Side.BUY))
        await asyncio.sleep(0.4)
        ordens = await _ordens(settings)
        await execucao.stop()

        assert [o for o in ordens if o.client_order_id == "abertura-liberada"], (
            f"a compra continuou barrada depois de a execucao voltar: {ordens}"
        )


class TestALiberacaoNaoDesarmaNadaAlheio:
    """Soltar a abertura e soltar a PROPRIA trava, e so ela.

    Destravar porque o agente voltou nao pode desarmar o circuit breaker nem
    desfazer uma pausa pedida pela interface -- e o mesmo desarme por porta
    lateral que `_allow_openings_for` ja recusa.
    """

    async def test_recuperar_a_execucao_nao_desarma_o_circuit_breaker(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        risco, execucao, orquestrador = await _bancada(settings, bus)
        await execucao.start()
        await asyncio.sleep(0.05)

        await orquestrador.pause_all(actor="circuit_breaker", reason="perda diaria de 12%")
        risco._circuit_breaker_active = True
        assert execucao.openings_blocked is not None

        _adoece(execucao)
        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await asyncio.sleep(0.05)
        assert "perda diaria" in (execucao.openings_blocked or ""), (
            "a trava do circuit breaker perdeu o motivo de verdade"
        )

        execucao.state = AgentState.RUNNING
        execucao.last_error = None
        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await asyncio.sleep(0.05)
        await execucao.stop()

        assert execucao.openings_blocked is not None, (
            "a execucao voltar desarmou o circuit breaker por porta lateral"
        )
        assert "perda diaria" in execucao.openings_blocked

    async def test_recuperar_a_execucao_nao_desfaz_a_pausa_pedida_na_interface(
        self, settings
    ):
        """A ordem importa: primeiro a trava por indisponibilidade, depois o clique.

        Quem clicou em pausar a execucao pediu para parar de operar. Soltar a
        abertura porque o agente voltou apagaria um pedido explicito do
        operador, e ele nao teria como saber.
        """
        bus = InMemoryEventBus()
        await bus.start()
        _, execucao, orquestrador = await _bancada(settings, bus)
        await execucao.start()
        await asyncio.sleep(0.05)

        _adoece(execucao)
        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await asyncio.sleep(0.05)
        assert execucao.openings_blocked is not None, "a trava nao armou; teste invalido"

        # Exatamente o que a rota `POST /agents/execution/pause` faz.
        assert await orquestrador.pause_agent("execution", actor="user")
        assert not execucao.is_paused, "pausar quem FECHA posicao desliga o stop-loss (D4)"

        execucao.state = AgentState.RUNNING
        execucao.last_error = None
        await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
        await asyncio.sleep(0.05)

        await bus.publish(Topics.ORDER_REQUESTS, _pedido("apos-o-clique", Side.BUY))
        await asyncio.sleep(0.4)
        ordens = await _ordens(settings)
        await execucao.stop()

        assert execucao.openings_blocked is not None, (
            "a recuperacao da execucao desfez a pausa pedida pelo operador"
        )
        assert [o for o in ordens if o.client_order_id == "apos-o-clique"] == [], (
            "a compra passou depois de o operador mandar parar de operar"
        )


@pytest.mark.parametrize(
    "impedimento",
    ["given_up", "tarefa_morta", "pausado", "erro"],
    ids=["watchdog desistiu", "tarefa morta", "pausado", "state=ERROR"],
)
async def test_todo_impedimento_de_execucao_barra_a_abertura(settings, impedimento):
    """A trava e do IMPEDIMENTO, e nao de um estado especifico.

    `_execution_unavailable` responde por cinco estados diferentes. Cobrir so o
    que o teste anterior usa deixaria a trava valendo para um deles: era assim
    que `is_paused` -- o unico que um clique produz -- ficou de fora da primeira
    versao daquele metodo.
    """
    bus = InMemoryEventBus()
    await bus.start()
    _, execucao, orquestrador = await _bancada(settings, bus)
    await execucao.start()
    await asyncio.sleep(0.05)

    if impedimento == "given_up":
        orquestrador._given_up.add("execution")
    elif impedimento == "tarefa_morta":
        await execucao._cancel_task()
    elif impedimento == "pausado":
        execucao.pause()
    else:
        _adoece(execucao)

    assert orquestrador._execution_unavailable() is not None
    await orquestrador._on_snapshot(_snapshot_com_stop_rompido())
    await asyncio.sleep(0.05)
    await execucao.stop()

    assert execucao.openings_blocked is not None, (
        f"abertura liberada com a execucao indisponivel ({impedimento})"
    )
