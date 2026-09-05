"""Testes dos agentes com estado: Risk Manager, Portfolio e a cadeia completa."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from helpers import make_candles
from pydantic import ValidationError

from crypto_traders.agents.execution import ExecutionAgent
from crypto_traders.agents.portfolio import PortfolioAgent
from crypto_traders.agents.risk_manager import RiskManagerAgent
from crypto_traders.agents.strategy import StrategyAgent
from crypto_traders.bus import InMemoryEventBus, Topics
from crypto_traders.db.repositories import (
    AuditLogRepository,
    CandleRepository,
    PortfolioSnapshotRepository,
    RiskEventRepository,
    TradeRepository,
)
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import (
    ExchangeName,
    RiskDecision,
    Side,
    SignalDirection,
    TradeOrigin,
)
from crypto_traders.domain.models import PortfolioSnapshot, Position, Signal
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
    async def _seed(self, settings, reference: str, when: datetime) -> None:
        async with session_scope(settings) as session:
            await PortfolioSnapshotRepository(session).save(
                make_snapshot(total=reference, cash=reference, timestamp=when), "dry_run"
            )

    async def test_trips_on_daily_loss_beyond_the_limit(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)

        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "1000", now.replace(hour=0, minute=1))

        # Queda de 6%, acima do limite diario de 5%.
        reason = await agent.check_circuit_breaker(make_snapshot(total="940", timestamp=now))

        assert reason is not None and "perda diaria" in reason
        assert agent.circuit_breaker_active

    async def test_does_not_trip_within_the_limit(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)

        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "1000", now.replace(hour=0, minute=1))

        reason = await agent.check_circuit_breaker(make_snapshot(total="970", timestamp=now))

        assert reason is None
        assert not agent.circuit_breaker_active

    async def test_blocks_new_orders_once_active(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)
        agent.observe_snapshot(make_snapshot())

        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "1000", now.replace(hour=0, minute=1))
        await agent.check_circuit_breaker(make_snapshot(total="900", timestamp=now))

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
        await agent.check_circuit_breaker(make_snapshot(total="1500", timestamp=now))
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

        assert await agent.check_circuit_breaker(make_snapshot(total="900", timestamp=now))
        assert await agent.check_circuit_breaker(make_snapshot(total="800", timestamp=now)) is None

    async def test_reset_is_manual_and_audited(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)

        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await self._seed(settings, "1000", now.replace(hour=0, minute=1))
        await agent.check_circuit_breaker(make_snapshot(total="900", timestamp=now))

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
        await agent.check_circuit_breaker(make_snapshot(total="900", timestamp=now))

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
