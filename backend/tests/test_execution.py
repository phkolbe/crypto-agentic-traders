"""Testes do Execution Agent e do PaperBroker.

O Execution Agent e o unico componente que pode gastar dinheiro. O que precisa
ser garantido: nunca executa o que nao foi aprovado, nunca duplica ordem, e
sempre deixa rastro -- inclusive quando falha.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_traders.agents.execution import ExecutionAgent
from crypto_traders.bus import InMemoryEventBus
from crypto_traders.db.repositories import OrderRepository, TradeRepository
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import (
    ExchangeName,
    OrderStatus,
    OrderType,
    Side,
    TradeOrigin,
)
from crypto_traders.domain.models import OrderRequest, OrderResult
from crypto_traders.exchanges.base import Broker
from crypto_traders.exchanges.paper import PaperBroker


def make_request(
    *,
    client_order_id: str = "cat-teste-001",
    side: Side = Side.BUY,
    quantity: str = "0.001",
    price: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id,
        signal_id="sig-1",
        risk_event_id="risk-1",
        exchange=ExchangeName.PAPER,
        symbol="BTC/USDT",
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
        price=Decimal(price) if price else None,
        notional=Decimal(quantity) * Decimal("50000"),
        stop_loss=Decimal("48500"),
        take_profit=Decimal("53000"),
        strategy="ma_crossover",
    )


class ExplodingBroker(Broker):
    """Simula queda de rede no momento do envio."""

    name = "exploding"

    async def place_order(self, request):
        raise ConnectionError("conexao perdida com a exchange")

    async def fetch_balances(self):
        return {}

    async def fetch_positions(self, prices):
        return []

    async def close(self):
        return None


@pytest.fixture
def paper_broker() -> PaperBroker:
    broker = PaperBroker(initial_balance=Decimal("1000"))
    broker.set_price("BTC", Decimal("50000"))
    return broker


class TestPaperBroker:
    async def test_buy_moves_cash_into_the_asset(self, paper_broker):
        result = await paper_broker.place_order(make_request())
        assert result.status is OrderStatus.FILLED
        balances = await paper_broker.fetch_balances()
        assert balances["BTC"] == Decimal("0.001")
        assert balances["USDT"] < Decimal("1000")

    async def test_slippage_always_works_against_the_operator(self, paper_broker):
        """Simular a favor seria enganar a si mesmo sobre a estrategia."""
        buy = await paper_broker.place_order(make_request(side=Side.BUY))
        assert buy.average_price > Decimal("50000")

        paper_broker.credit("BTC", Decimal("1"))
        sell = await paper_broker.place_order(
            make_request(client_order_id="cat-teste-002", side=Side.SELL)
        )
        assert sell.average_price < Decimal("50000")

    async def test_charges_a_fee(self, paper_broker):
        result = await paper_broker.place_order(make_request())
        assert result.fee > 0
        assert result.fee_currency == "USDT"

    async def test_rejects_when_cash_is_insufficient(self, paper_broker):
        result = await paper_broker.place_order(make_request(quantity="10"))
        assert result.status is OrderStatus.REJECTED
        assert "insuficiente" in (result.error or "")

    async def test_rejected_order_does_not_move_balances(self, paper_broker):
        before = await paper_broker.fetch_balances()
        await paper_broker.place_order(make_request(quantity="10"))
        assert await paper_broker.fetch_balances() == before

    async def test_fails_without_a_reference_price(self):
        """Sem preco nao ha como preencher; falhar e melhor que inventar."""
        broker = PaperBroker(initial_balance=Decimal("1000"))
        result = await broker.place_order(make_request())
        assert result.status is OrderStatus.FAILED
        assert "preco de referencia" in (result.error or "")

    async def test_same_client_order_id_never_fills_twice(self, paper_broker):
        """Espelha a garantia de idempotencia da exchange real."""
        first = await paper_broker.place_order(make_request())
        second = await paper_broker.place_order(make_request())
        assert first.status is OrderStatus.FILLED
        assert second.status is OrderStatus.REJECTED
        assert (await paper_broker.fetch_balances())["BTC"] == Decimal("0.001")


class TestExecutionAgent:
    async def test_records_order_and_trade_on_fill(self, settings, paper_broker):
        agent = ExecutionAgent(InMemoryEventBus(), paper_broker, settings)
        request = make_request()

        result = await agent._execute(request)
        assert result is not None and result.status is OrderStatus.FILLED

        async with session_scope(settings) as session:
            orders = await OrderRepository(session).list()
            trades = await TradeRepository(session).list()

        assert len(orders) == 1
        assert orders[0].status == str(OrderStatus.FILLED)
        assert orders[0].mode == "dry_run"
        assert len(trades) == 1
        assert trades[0].origin == str(TradeOrigin.AGENT)
        assert trades[0].strategy == "ma_crossover"

    async def test_stores_stop_and_target_with_the_order(self, settings, paper_broker):
        """Sem isso, nao da para auditar depois com que protecao a ordem saiu."""
        agent = ExecutionAgent(InMemoryEventBus(), paper_broker, settings)
        await agent._execute(make_request())

        async with session_scope(settings) as session:
            order = (await OrderRepository(session).list())[0]
        assert Decimal(str(order.stop_loss)) == Decimal("48500")
        assert Decimal(str(order.take_profit)) == Decimal("53000")

    async def test_duplicate_client_order_id_is_not_sent_twice(self, settings, paper_broker):
        """Um retry de rede nao pode virar duas ordens na exchange."""
        agent = ExecutionAgent(InMemoryEventBus(), paper_broker, settings)
        request = make_request()

        await agent._execute(request)
        second = await agent._execute(request)

        assert second is None
        async with session_scope(settings) as session:
            assert len(await OrderRepository(session).list()) == 1
            assert len(await TradeRepository(session).list()) == 1

    async def test_network_failure_leaves_a_recorded_order(self, settings):
        """Ordem que falhou nao pode ficar PENDING para sempre sem explicacao."""
        agent = ExecutionAgent(InMemoryEventBus(), ExplodingBroker(), settings)
        result = await agent._execute(make_request())

        assert result is not None and result.status is OrderStatus.FAILED
        async with session_scope(settings) as session:
            orders = await OrderRepository(session).list()
            trades = await TradeRepository(session).list()

        assert orders[0].status == str(OrderStatus.FAILED)
        assert "conexao perdida" in (orders[0].error or "")
        assert trades == []  # falha nao gera trade no historico

    async def test_rejected_order_does_not_become_a_trade(self, settings, paper_broker):
        agent = ExecutionAgent(InMemoryEventBus(), paper_broker, settings)
        await agent._execute(make_request(quantity="10"))  # sem saldo

        async with session_scope(settings) as session:
            assert (await OrderRepository(session).list())[0].status == str(
                OrderStatus.REJECTED
            )
            assert await TradeRepository(session).list() == []

    async def test_fill_without_price_is_not_recorded_as_a_trade(
        self, settings, paper_broker, monkeypatch
    ):
        """Preco zero contaminaria PnL e historico fiscal em silencio."""

        async def broken_fill(request):
            return OrderResult(
                order_request_id=request.id,
                client_order_id=request.client_order_id,
                exchange_order_id="x",
                status=OrderStatus.FILLED,
                filled_quantity=request.quantity,
                average_price=None,
            )

        monkeypatch.setattr(paper_broker, "place_order", broken_fill)
        agent = ExecutionAgent(InMemoryEventBus(), paper_broker, settings)
        await agent._execute(make_request())

        async with session_scope(settings) as session:
            assert await TradeRepository(session).list() == []
            assert (await OrderRepository(session).list())[0].status == str(
                OrderStatus.FILLED
            )

    async def test_publishes_the_result_on_the_bus(self, settings, paper_broker):
        bus = InMemoryEventBus()
        await bus.start()
        agent = ExecutionAgent(bus, paper_broker, settings)

        received: list[OrderResult] = []

        async def collect():
            async for result in bus.subscribe("execution.order_results"):
                received.append(result)
                break

        import asyncio

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await agent._execute(make_request())
        await asyncio.wait_for(task, timeout=2)

        assert received and received[0].status is OrderStatus.FILLED
