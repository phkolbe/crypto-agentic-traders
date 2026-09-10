"""Testes dos agentes com estado: Risk Manager, Portfolio e a cadeia completa."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from helpers import make_candles
from pydantic import ValidationError

from crypto_traders.agents.execution import ExecutionAgent
from crypto_traders.agents.orchestrator import Orchestrator
from crypto_traders.agents.portfolio import PortfolioAgent
from crypto_traders.agents.risk_manager import RiskManagerAgent
from crypto_traders.agents.strategy import StrategyAgent
from crypto_traders.bus import InMemoryEventBus, Topics
from crypto_traders.db.repositories import (
    AuditLogRepository,
    CandleRepository,
    OrderRepository,
    PortfolioSnapshotRepository,
    RiskEventRepository,
    TradeRepository,
)
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import (
    ExchangeName,
    OrderStatus,
    OrderType,
    RiskDecision,
    Side,
    SignalDirection,
    TradeOrigin,
)
from crypto_traders.domain.models import OrderRequest, PortfolioSnapshot, Position, Signal
from crypto_traders.exchanges.paper import PaperBroker
from crypto_traders.strategies import get_strategy


def make_signal(
    direction: SignalDirection = SignalDirection.LONG, confidence: float = 0.8
) -> Signal:
    return Signal(
        exchange=ExchangeName.PAPER,
        symbol="BTC/USDT",
        timeframe="15m",
        strategy="ma_crossover",
        direction=direction,
        confidence=confidence,
        reason="teste",
        reference_price=Decimal("50000"),
    )


def make_snapshot(
    total: str = "1000",
    cash: str = "1000",
    btc: str = "0",
    timestamp: datetime | None = None,
    realized: str = "0",
    unrealized: str = "0",
) -> PortfolioSnapshot:
    positions = [
        Position(exchange=ExchangeName.PAPER, asset="USDT", quantity=Decimal(cash),
                 current_price=Decimal(1)),
    ]
    if Decimal(btc) > 0:
        positions.append(
            Position(exchange=ExchangeName.PAPER, asset="BTC", quantity=Decimal(btc),
                     current_price=Decimal("50000"))
        )
    return PortfolioSnapshot(
        timestamp=timestamp or datetime.now(UTC),
        total_value=Decimal(total),
        cash_value=Decimal(cash),
        positions_value=Decimal(total) - Decimal(cash),
        realized_pnl=Decimal(realized),
        unrealized_pnl=Decimal(unrealized),
        positions=positions,
    )


class TestRiskManagerAgent:
    async def test_rejects_before_the_first_portfolio_snapshot(self, settings):
        """Sem retrato do portfolio nao ha como dimensionar: rejeitar e o seguro."""
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)

        await agent._on_signal(make_signal())

        async with session_scope(settings) as session:
            events = await RiskEventRepository(session).list()
        assert len(events) == 1
        assert events[0].decision == str(RiskDecision.REJECTED)
        assert "portfolio ainda nao apurado" in events[0].reasons[0]

    async def test_approved_signal_becomes_an_order_request(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)
        agent.observe_snapshot(make_snapshot())

        received = []

        async def collect():
            async for request in bus.subscribe(Topics.ORDER_REQUESTS):
                received.append(request)
                break

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await agent._on_signal(make_signal())
        await asyncio.wait_for(task, timeout=2)

        request = received[0]
        assert request.side is Side.BUY
        assert request.quantity > 0
        assert request.stop_loss is not None and request.take_profit is not None

    async def test_order_request_points_to_a_persisted_risk_event(self, settings):
        """O risk_event_id precisa existir no banco antes da ordem ser publicada.

        E isso que torna a auditoria confiavel: nenhuma ordem referencia uma
        decisao que nao foi gravada.
        """
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)
        agent.observe_snapshot(make_snapshot())

        received = []

        async def collect():
            async for request in bus.subscribe(Topics.ORDER_REQUESTS):
                received.append(request)
                break

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await agent._on_signal(make_signal())
        await asyncio.wait_for(task, timeout=2)

        async with session_scope(settings) as session:
            events = await RiskEventRepository(session).list()
        assert received[0].risk_event_id in {event.id for event in events}

    async def test_rejection_is_persisted_with_its_reason(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)
        agent.observe_snapshot(make_snapshot())

        await agent._on_signal(make_signal(confidence=0.10))

        async with session_scope(settings) as session:
            events = await RiskEventRepository(session).list()
        assert events[0].decision == str(RiskDecision.REJECTED)
        assert any("confianca" in reason for reason in events[0].reasons)

    async def test_updating_limits_is_audited(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)

        updated = await agent.update_limits({"max_order_notional": "25"}, actor="paulinho")

        assert updated.max_order_notional == Decimal("25")
        async with session_scope(settings) as session:
            entries = await AuditLogRepository(session).list()
        assert entries[0].action == "risk_limits_updated"
        assert entries[0].actor == "paulinho"

    async def test_incoherent_limits_are_refused(self, settings):
        """Take-profit abaixo do stop daria esperanca matematica negativa."""
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)

        with pytest.raises(ValidationError):
            await agent.update_limits({"take_profit_pct": 0.01})

        assert agent.limits.take_profit_pct == 0.06  # inalterado


class TestCircuitBreaker:
    async def _seed(
        self, settings, reference: str, when: datetime, resultado: str = "0"
    ) -> None:
        """Grava o retrato de referencia do periodo.

        `resultado` e o lucro acumulado de negociacao naquele momento -- e ele,
        nao o patrimonio, que a trava compara. Ver
        `RiskManagerAgent.check_circuit_breaker`.
        """
        async with session_scope(settings) as session:
            await PortfolioSnapshotRepository(session).save(
                make_snapshot(
                    total=reference, cash=reference, timestamp=when, realized=resultado
                ),
                "dry_run",
            )

    async def test_trips_on_daily_loss_beyond_the_limit(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)

        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "1000", now.replace(hour=0, minute=1))

        # Prejuizo de 60 sobre capital de 1000 = 6%, acima do limite diario de 5%.
        reason = await agent.check_circuit_breaker(
            make_snapshot(total="940", timestamp=now, realized="-60")
        )

        assert reason is not None and "perda diaria" in reason
        assert agent.circuit_breaker_active

    async def test_does_not_trip_within_the_limit(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)

        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "1000", now.replace(hour=0, minute=1))

        reason = await agent.check_circuit_breaker(
            make_snapshot(total="970", timestamp=now, realized="-30")
        )

        assert reason is None
        assert not agent.circuit_breaker_active

    async def test_blocks_new_orders_once_active(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)
        agent.observe_snapshot(make_snapshot())

        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "1000", now.replace(hour=0, minute=1))
        await agent.check_circuit_breaker(
            make_snapshot(total="900", timestamp=now, realized="-100")
        )

        await agent._on_signal(make_signal())

        async with session_scope(settings) as session:
            events = await RiskEventRepository(session).list()
        rejection = next(e for e in events if e.decision == str(RiskDecision.REJECTED))
        assert any("circuit breaker" in reason for reason in rejection.reasons)

    async def test_still_allows_closing_a_position(self, settings):
        """A trava protege contra nova exposicao, nao contra a saida."""
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)
        agent.observe_snapshot(make_snapshot(total="1500", cash="1000", btc="0.01"))

        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "2000", now.replace(hour=0, minute=1))
        await agent.check_circuit_breaker(
            make_snapshot(total="1500", timestamp=now, realized="-500")
        )
        assert agent.circuit_breaker_active

        await agent._on_signal(make_signal(direction=SignalDirection.FLAT))

        async with session_scope(settings) as session:
            events = await RiskEventRepository(session).list()
        assert any(e.decision == str(RiskDecision.APPROVED) for e in events)

    async def test_does_not_trip_twice(self, settings):
        """Ja disparado, permanece ativo ate rearme manual -- sem re-disparar."""
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)

        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "1000", now.replace(hour=0, minute=1))

        assert await agent.check_circuit_breaker(
            make_snapshot(total="900", timestamp=now, realized="-100")
        )
        assert (
            await agent.check_circuit_breaker(
                make_snapshot(total="800", timestamp=now, realized="-200")
            )
            is None
        )

    async def test_reset_is_manual_and_audited(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)

        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "1000", now.replace(hour=0, minute=1))
        await agent.check_circuit_breaker(
            make_snapshot(total="900", timestamp=now, realized="-100")
        )

        await agent.reset_circuit_breaker(actor="paulinho")

        assert not agent.circuit_breaker_active
        async with session_scope(settings) as session:
            entries = await AuditLogRepository(session).list()
        assert any(e.action == "circuit_breaker_reset" and e.actor == "paulinho" for e in entries)

    async def test_survives_a_restart(self, settings):
        """O estado mora no banco: reiniciar o processo nao destrava a protecao."""
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)

        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "1000", now.replace(hour=0, minute=1))
        await agent.check_circuit_breaker(
            make_snapshot(total="900", timestamp=now, realized="-100")
        )

        revived = RiskManagerAgent(InMemoryEventBus(), settings)
        await revived._load_state()
        assert revived.circuit_breaker_active


class TestPortfolioAgent:
    async def test_computes_total_cash_and_allocations(self, settings):
        broker = PaperBroker(initial_balance=Decimal("1000"))
        broker.credit("BTC", Decimal("0.01"))
        agent = PortfolioAgent(
            InMemoryEventBus(), broker, settings, price_source=lambda: {"BTC": Decimal("50000")}
        )

        snapshot = await agent.build_snapshot()

        assert snapshot.cash_value == Decimal("1000")
        assert snapshot.positions_value == Decimal("500")
        assert snapshot.total_value == Decimal("1500")
        assert snapshot.allocations["BTC"] == pytest.approx(1 / 3, abs=0.001)

    async def test_unpriced_asset_does_not_inflate_the_total(self, settings):
        """Chutar preco poderia desarmar o circuit breaker na hora errada."""
        broker = PaperBroker(initial_balance=Decimal("1000"))
        broker.credit("XYZ", Decimal("100"))
        agent = PortfolioAgent(InMemoryEventBus(), broker, settings, price_source=dict)

        snapshot = await agent.build_snapshot()
        assert snapshot.total_value == Decimal("1000")

    async def test_average_cost_comes_from_the_trade_history(self, settings):
        """Spot nao expoe preco medio; ele e reconstruido dos trades."""
        async with session_scope(settings) as session:
            trades = TradeRepository(session)
            await trades.record(
                executed_at=datetime.now(UTC) - timedelta(hours=2),
                exchange="paper", symbol="BTC/USDT", side=str(Side.BUY),
                quantity=Decimal("0.01"), price=Decimal("40000"),
            )
            await trades.record(
                executed_at=datetime.now(UTC) - timedelta(hours=1),
                exchange="paper", symbol="BTC/USDT", side=str(Side.BUY),
                quantity=Decimal("0.01"), price=Decimal("60000"),
            )

        broker = PaperBroker(initial_balance=Decimal("1000"))
        broker.credit("BTC", Decimal("0.02"))
        agent = PortfolioAgent(
            InMemoryEventBus(), broker, settings, price_source=lambda: {"BTC": Decimal("50000")}
        )

        snapshot = await agent.build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")
        assert btc.average_price == pytest.approx(Decimal("50000"), abs=1)

    async def test_manual_trades_count_toward_the_average_cost(self, settings):
        """Uma compra feita pelo app da exchange e lancada aqui deve valer igual."""
        async with session_scope(settings) as session:
            await TradeRepository(session).record(
                executed_at=datetime.now(UTC),
                exchange="binance", symbol="BTC/USDT", side=str(Side.BUY),
                quantity=Decimal("0.01"), price=Decimal("30000"),
                origin=TradeOrigin.MANUAL,
            )

        broker = PaperBroker(initial_balance=Decimal("1000"))
        broker.credit("BTC", Decimal("0.01"))
        agent = PortfolioAgent(
            InMemoryEventBus(), broker, settings, price_source=lambda: {"BTC": Decimal("50000")}
        )

        snapshot = await agent.build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")
        assert btc.average_price == pytest.approx(Decimal("30000"), abs=1)
        assert btc.unrealized_pnl > 0

    async def test_snapshot_is_persisted_for_the_chart(self, settings):
        broker = PaperBroker(initial_balance=Decimal("1000"))
        agent = PortfolioAgent(InMemoryEventBus(), broker, settings, price_source=dict)

        await agent.build_snapshot()

        async with session_scope(settings) as session:
            assert await PortfolioSnapshotRepository(session).latest() is not None


class TestFullPipeline:
    async def test_candle_becomes_a_trade_through_every_agent(self, settings):
        """Cadeia completa: candle -> sinal -> risco -> execucao -> historico.

        Nada e simulado no meio: sao os agentes reais, ligados pelo bus real.
        """
        bus = InMemoryEventBus()
        await bus.start()

        broker = PaperBroker(initial_balance=Decimal("1000"))
        broker.set_price("BTC", Decimal("50000"))

        strategy_agent = StrategyAgent(
            bus, [get_strategy("ma_crossover", fast=3, slow=10)], settings
        )
        risk_agent = RiskManagerAgent(bus, settings)
        execution_agent = ExecutionAgent(bus, broker, settings)
        risk_agent.observe_snapshot(make_snapshot())

        await execution_agent.start()
        await risk_agent.start()
        await strategy_agent.start()
        # As assinaturas do bus so se registram quando o gerador e iterado pela
        # primeira vez; sem esta pausa o candle seria publicado no vazio.
        await asyncio.sleep(0.1)

        # Queda longa seguida de alta: a MA rapida cruza a lenta no candle 42.
        closes = [50000 - i * 100 for i in range(40)] + [46100 + i * 400 for i in range(3)]
        candles = make_candles([float(c) for c in closes])
        async with session_scope(settings) as session:
            await CandleRepository(session).upsert_many(candles)

        await bus.publish(Topics.CANDLES, candles[-1])

        for _ in range(50):
            await asyncio.sleep(0.05)
            async with session_scope(settings) as session:
                trades = await TradeRepository(session).list()
            if trades:
                break

        for agent in (strategy_agent, risk_agent, execution_agent):
            await agent.stop()

        assert trades, "nenhum trade chegou ao fim da cadeia"
        trade = trades[0]
        assert trade.symbol == "BTC/USDT"
        assert trade.side == str(Side.BUY)
        assert trade.origin == str(TradeOrigin.AGENT)
        # A rastreabilidade completa e o ponto: do trade ate o sinal que o causou.
        assert trade.signal_id is not None
        assert trade.strategy == "ma_crossover"


class TestCircuitBreakerIgnoresExternalFlows:
    """Saque e deposito nao sao prejuizo.

    O primeiro ensaio em dry_run disparou a trava em tres minutos com "perda
    diaria de 85%", porque o saldo simulado passou de 1000 para 150. Nao houve
    perda -- mudou a referencia. Comparando patrimonio bruto, um SAQUE e
    indistinguivel de uma catastrofe: tirar R$100 de uma conta de R$150 dispara
    "perda de 67%" e pausa tudo, culpando um prejuizo que nao existiu.
    """

    async def _seed(self, settings, total: str, when: datetime, resultado: str = "0") -> None:
        async with session_scope(settings) as session:
            await PortfolioSnapshotRepository(session).save(
                make_snapshot(total=total, cash=total, timestamp=when, realized=resultado),
                "dry_run",
            )

    async def _agente(self, settings, autorizado=None):
        bus = InMemoryEventBus()
        await bus.start()
        if autorizado is not None:
            settings = settings.model_copy(
                update={
                    "risk": settings.risk.model_copy(
                        update={"authorized_capital": Decimal(autorizado)}
                    )
                }
            )
        return RiskManagerAgent(bus, settings)

    async def test_a_withdrawal_does_not_trip_the_breaker(self, settings):
        """R$150 viram R$50 por saque, com resultado de negociacao zerado."""
        agent = await self._agente(settings)
        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "150", now.replace(hour=0, minute=1))

        reason = await agent.check_circuit_breaker(
            make_snapshot(total="50", cash="50", timestamp=now, realized="0")
        )

        assert reason is None
        assert not agent.circuit_breaker_active

    async def test_a_deposit_does_not_trip_the_breaker(self, settings):
        agent = await self._agente(settings)
        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "150", now.replace(hour=0, minute=1))

        reason = await agent.check_circuit_breaker(
            make_snapshot(total="650", cash="650", timestamp=now, realized="0")
        )
        assert reason is None

    async def test_a_real_loss_still_trips(self, settings):
        """A correcao nao pode ter desligado a trava."""
        agent = await self._agente(settings)
        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "1000", now.replace(hour=0, minute=1))

        reason = await agent.check_circuit_breaker(
            make_snapshot(total="940", timestamp=now, realized="-60")
        )
        assert reason is not None
        assert agent.circuit_breaker_active

    async def test_an_unrealized_loss_also_trips(self, settings):
        """Posicao aberta afundando conta, mesmo antes de virar prejuizo realizado.

        Esperar a realizacao deixaria a trava cega justamente no cenario que ela
        existe para pegar: a posicao caindo agora.
        """
        agent = await self._agente(settings)
        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "1000", now.replace(hour=0, minute=1))

        reason = await agent.check_circuit_breaker(
            make_snapshot(total="940", timestamp=now, realized="0", unrealized="-60")
        )
        assert reason is not None

    async def test_the_base_is_the_authorized_capital(self, settings):
        """Com portao em 150 sobre conta de 650, medir contra 650 afrouxaria 4x.

        Prejuizo de R$10 e 6,7% de 150 (dispara) e 1,5% de 650 (nao dispararia).
        """
        agent = await self._agente(settings, autorizado="150")
        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "650", now.replace(hour=0, minute=1))

        reason = await agent.check_circuit_breaker(
            make_snapshot(total="640", cash="640", timestamp=now, realized="-10")
        )
        assert reason is not None
        assert "sobre 150.00" in reason

    async def test_a_gain_never_trips(self, settings):
        agent = await self._agente(settings)
        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "1000", now.replace(hour=0, minute=1), resultado="-50")

        reason = await agent.check_circuit_breaker(
            make_snapshot(total="1100", timestamp=now, realized="50")
        )
        assert reason is None


class TestCircuitBreakerDePontaAPonta:
    """Secao 7 do plano: provocar a perda e VER a paralisacao acontecer.

    O que ja existe acima mede o Risk Manager sozinho: `check_circuit_breaker`
    devolve um motivo e `circuit_breaker_active` fica verdadeiro. Isso prova que
    a trava ARMA, e nao que ela PARA algo -- e a diferenca entre as duas coisas
    e exatamente onde o gauntlet achou dois defeitos graves:

    * `pause_all` pausava o Execution Agent, e e `pause_all` que o circuit
      breaker chama. A venda de protecao ficava na fila sem ninguem para
      executa-la, justamente quando o mercado cai: a trava de protecao virava
      amplificador de prejuizo;
    * com a execucao indisponivel o sistema parava de PROTEGER e continuava
      deixando ABRIR.

    Nenhum dos dois apareceria num teste que confere o campo. Os dois aparecem
    aqui, porque aqui a ordem chega ao broker -- ou nao chega -- e o saldo dele
    e a evidencia.

    A bancada e a de producao, com uma excecao anotada: o Market Data Agent e
    substituido por um portador de precos, porque coletar candle da exchange nao
    tem parte nenhuma nesta pergunta. Risk Manager, Execution Agent, Strategy
    Agent, `Orchestrator._on_snapshot`, `pause_all`, o `PaperBroker` e o banco
    sao os reais.
    """

    QUOTE = "USDT"

    class _PortadorDePrecos:
        """Dublê do Market Data Agent: entrega preco e nada mais.

        Existe porque `Orchestrator._sync_paper_prices` le
        `market_data.latest_prices`, e subir o agente de verdade pediria uma
        fonte de mercado -- rede, ou um segundo dublê maior que este.
        """

        name = "market_data"

        def __init__(self, precos: dict[str, Decimal]) -> None:
            self.latest_prices = dict(precos)

        def pause(self) -> None:
            return None

        def resume(self) -> None:
            return None

    async def _bancada(self, settings):
        """Sistema montado, agentes de pe, uma posicao em BTC na carteira."""
        bus = InMemoryEventBus()
        await bus.start()

        broker = PaperBroker(quote_currency=self.QUOTE, initial_balance=Decimal("500"))
        broker.credit("BTC", Decimal("0.01"))
        broker.set_price("BTC", Decimal("50000"))

        orquestrador = Orchestrator(settings)
        orquestrador.bus = bus
        orquestrador._broker = broker
        orquestrador.market_data = self._PortadorDePrecos({"BTC": Decimal("50000")})
        orquestrador.strategy = StrategyAgent(
            bus, [get_strategy("ma_crossover", fast=3, slow=10)], settings
        )
        orquestrador.risk_manager = RiskManagerAgent(bus, settings)
        orquestrador.execution = ExecutionAgent(bus, broker, settings)

        await orquestrador.execution.start()
        await orquestrador.risk_manager.start()
        await orquestrador.strategy.start()
        # As assinaturas do bus so se registram na primeira iteracao do gerador.
        await asyncio.sleep(0.1)
        return orquestrador, broker, bus

    async def _desmontar(self, orquestrador) -> None:
        for agente in (
            orquestrador.strategy,
            orquestrador.risk_manager,
            orquestrador.execution,
        ):
            await agente.stop()

    async def _referencia_do_dia(self, settings, total: str, resultado: str = "0"):
        """O retrato inicial do dia, que a trava usa como referencia."""
        agora = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        async with session_scope(settings) as sessao:
            await PortfolioSnapshotRepository(sessao).save(
                make_snapshot(
                    total=total,
                    cash=total,
                    timestamp=agora.replace(hour=0, minute=1),
                    realized=resultado,
                ),
                "dry_run",
            )
        return agora

    def _retrato_com_prejuizo(self, agora):
        """Perda de 31% do capital: seis vezes o limite diario de 5%.

        A queda tem de aparecer nas DUAS medidas -- resultado de negociacao e
        patrimonio --, senao a trava a trata como artefato de contabilidade e nao
        dispara. E o que um prejuizo de verdade faz.
        """
        return make_snapshot(
            total="690", cash="190", btc="0.01", timestamp=agora, realized="-310"
        )

    def _pedido(self, side: Side, *, com_stop: bool, ordem: str) -> OrderRequest:
        return OrderRequest(
            client_order_id=ordem,
            signal_id=None,
            risk_event_id=f"risco-{ordem}",
            exchange=ExchangeName.PAPER,
            symbol="BTC/USDT",
            side=side,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.001"),
            notional=Decimal("50"),
            stop_loss=Decimal("48500") if com_stop else None,
            strategy="ma_crossover",
        )

    async def _publicar_e_esperar(self, orquestrador, pedido) -> None:
        """Publica no bus e espera o Execution Agent terminar com o pedido.

        Passa pelo bus, e nao por `_execute` direto, porque o pedido que JA
        estava publicado quando a trava disparou e justamente o caso que o
        gauntlet mediu (a segunda leitura da trava em `execution.py`).
        """
        await orquestrador.bus.publish(Topics.ORDER_REQUESTS, pedido)
        for _ in range(60):
            await asyncio.sleep(0.05)
            async with session_scope(orquestrador.settings) as sessao:
                ordens = await OrderRepository(sessao).list()
            if any(o.client_order_id == pedido.client_order_id for o in ordens):
                return
        # Recusa antes de gravar PENDING nao deixa linha em `orders`: dar mais
        # uma volta e o que separa "recusado" de "ainda na fila".
        await asyncio.sleep(0.3)

    # ------------------------------------------------------------------
    async def test_a_perda_dispara_a_trava_e_paralisa_a_decisao(self, settings):
        """O disparo pelo caminho de producao: `_on_snapshot`, nao a mao."""
        orquestrador, _, _ = await self._bancada(settings)
        try:
            agora = await self._referencia_do_dia(settings, "1000")
            await orquestrador._on_snapshot(self._retrato_com_prejuizo(agora))

            assert orquestrador.risk_manager.circuit_breaker_active, (
                "a trava nao armou diante de 31% de prejuizo"
            )
            # A decisao para...
            assert orquestrador.strategy.is_paused
            assert orquestrador.risk_manager.is_paused
            # ...e a execucao NAO para (D4). Pausa-la desligaria o stop-loss.
            assert not orquestrador.execution.is_paused, (
                "o Execution Agent foi pausado: o stop-loss acabou de ser desligado"
            )
            # O que para na execucao e a ABERTURA, com motivo registrado.
            assert orquestrador.execution.openings_blocked is not None
            assert "perda diaria" in orquestrador.execution.openings_blocked
        finally:
            await self._desmontar(orquestrador)

    async def test_depois_da_trava_a_compra_nao_chega_ao_broker(self, settings):
        """Direcao ABRIR: barrada, e o saldo do broker e a evidencia."""
        orquestrador, broker, _ = await self._bancada(settings)
        try:
            agora = await self._referencia_do_dia(settings, "1000")
            await orquestrador._on_snapshot(self._retrato_com_prejuizo(agora))

            antes = await broker.fetch_balances()
            await self._publicar_e_esperar(
                orquestrador, self._pedido(Side.BUY, com_stop=True, ordem="cb-compra-1")
            )
            depois = await broker.fetch_balances()

            assert depois == antes, (
                f"a carteira mudou com a trava armada: {antes} -> {depois}"
            )
            async with session_scope(settings) as sessao:
                ordens = await OrderRepository(sessao).list()
                trades = await TradeRepository(sessao).list()
            assert trades == [], "a compra virou trade com a trava armada"
            assert [o for o in ordens if o.status == str(OrderStatus.FILLED)] == []
        finally:
            await self._desmontar(orquestrador)

    async def test_depois_da_trava_a_venda_de_protecao_e_executada(self, settings):
        """Direcao FECHAR: passa. E a metade que o gauntlet encontrou quebrada.

        A venda de protecao vem sem stop-loss por construcao, entao ela tambem
        exercita a assimetria do portao de stop: barrar venda ali seria matar o
        proprio stop-loss que se quer garantir.
        """
        orquestrador, broker, _ = await self._bancada(settings)
        try:
            agora = await self._referencia_do_dia(settings, "1000")
            await orquestrador._on_snapshot(self._retrato_com_prejuizo(agora))
            assert orquestrador.risk_manager.circuit_breaker_active

            saldos_antes = await broker.fetch_balances()
            btc_antes = saldos_antes.get("BTC", Decimal(0))
            caixa_antes = saldos_antes.get(self.QUOTE, Decimal(0))

            await self._publicar_e_esperar(
                orquestrador,
                self._pedido(Side.SELL, com_stop=False, ordem="cb-protecao-1"),
            )
            saldos = await broker.fetch_balances()

            assert saldos["BTC"] < btc_antes, (
                "a venda de protecao NAO saiu com a trava armada: o stop-loss "
                f"esta desligado (BTC {btc_antes} -> {saldos['BTC']})"
            )
            assert saldos[self.QUOTE] > caixa_antes, "a venda nao devolveu caixa"
            async with session_scope(settings) as sessao:
                trades = await TradeRepository(sessao).list()
            assert [t.side for t in trades] == [str(Side.SELL)]
        finally:
            await self._desmontar(orquestrador)

    async def test_contraprova_sem_a_trava_a_mesma_compra_passa(self, settings):
        """Sem esta contraprova, os testes acima passariam com o bus quebrado.

        Mesmo pedido, mesma bancada, sem prejuizo nenhum: a compra tem de virar
        trade. E o que prova que o bloqueio acima e a trava agindo, e nao o
        caminho da ordem estar entupido.
        """
        orquestrador, _, _ = await self._bancada(settings)
        try:
            agora = await self._referencia_do_dia(settings, "1000")
            # Retrato SEM perda: a trava nao tem por que disparar.
            await orquestrador._on_snapshot(
                make_snapshot(
                    total="1000", cash="500", btc="0.01", timestamp=agora, realized="0"
                )
            )
            assert not orquestrador.risk_manager.circuit_breaker_active
            assert orquestrador.execution.openings_blocked is None

            await self._publicar_e_esperar(
                orquestrador,
                self._pedido(Side.BUY, com_stop=True, ordem="cb-contraprova-1"),
            )

            async with session_scope(settings) as sessao:
                trades = await TradeRepository(sessao).list()
            assert [t.side for t in trades] == [str(Side.BUY)], (
                "a compra nao passou nem sem trava: o teste de bloqueio acima "
                "nao estaria medindo a trava"
            )
        finally:
            await self._desmontar(orquestrador)

    async def test_a_trava_sobrevive_ao_reinicio_e_a_abertura_continua_barrada(
        self, settings
    ):
        """O padrao de fabrica de uma trava nunca pode ser "solta".

        A recusa de abertura mora na memoria do Execution Agent; a trava mora no
        banco. Um processo novo tem de ler o banco e rearmar a recusa, senao
        reiniciar o sistema seria a porta lateral que destrava tudo.
        """
        orquestrador, _, _ = await self._bancada(settings)
        try:
            agora = await self._referencia_do_dia(settings, "1000")
            await orquestrador._on_snapshot(self._retrato_com_prejuizo(agora))
            assert orquestrador.execution.openings_blocked is not None
        finally:
            await self._desmontar(orquestrador)

        # Processo novo: nada em memoria, tudo no banco.
        renascido, broker, _ = await self._bancada(settings)
        try:
            assert renascido.execution.openings_blocked is None, (
                "premissa do teste: um agente recem-construido nasce permissivo"
            )
            await renascido.risk_manager._load_state()
            assert renascido.risk_manager.circuit_breaker_active

            await renascido._apply_persisted_circuit_breaker()
            assert renascido.execution.openings_blocked is not None
            assert not renascido.execution.is_paused, "D4: o fechamento segue vivo"

            antes = await broker.fetch_balances()
            await self._publicar_e_esperar(
                renascido, self._pedido(Side.BUY, com_stop=True, ordem="cb-reinicio-1")
            )
            assert await broker.fetch_balances() == antes
        finally:
            await self._desmontar(renascido)

    async def test_o_rearme_manual_libera_a_abertura_e_e_auditado(self, settings):
        """Sair da paralisacao e ato de pessoa, e fica registrado."""
        orquestrador, _, _ = await self._bancada(settings)
        try:
            agora = await self._referencia_do_dia(settings, "1000")
            await orquestrador._on_snapshot(self._retrato_com_prejuizo(agora))
            assert orquestrador.execution.openings_blocked is not None

            await orquestrador.risk_manager.reset_circuit_breaker(actor="paulinho")
            await orquestrador.resume_all(actor="paulinho")

            assert not orquestrador.risk_manager.circuit_breaker_active
            assert orquestrador.execution.openings_blocked is None
            assert not orquestrador.strategy.is_paused

            await self._publicar_e_esperar(
                orquestrador,
                self._pedido(Side.BUY, com_stop=True, ordem="cb-rearmado-1"),
            )
            async with session_scope(settings) as sessao:
                trades = await TradeRepository(sessao).list()
                acoes = [e.action for e in await AuditLogRepository(sessao).list(limit=50)]
            assert [t.side for t in trades] == [str(Side.BUY)], (
                "a abertura nao voltou depois do rearme manual"
            )
            assert "circuit_breaker_tripped" in acoes
            assert "circuit_breaker_reset" in acoes
            assert "all_agents_paused" in acoes
            assert "all_agents_resumed" in acoes
        finally:
            await self._desmontar(orquestrador)
