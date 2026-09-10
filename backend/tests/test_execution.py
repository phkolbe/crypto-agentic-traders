"""Testes do Execution Agent e do PaperBroker.

O Execution Agent e o unico componente que pode gastar dinheiro. O que precisa
ser garantido: nunca executa o que nao foi aprovado, nunca duplica ordem, e
sempre deixa rastro -- inclusive quando falha.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from crypto_traders.agents.execution import ExecutionAgent
from crypto_traders.bus import InMemoryEventBus, Topics
from crypto_traders.db.repositories import (
    AuditLogRepository,
    OrderRepository,
    TradeRepository,
)
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import (
    ExchangeName,
    OrderStatus,
    OrderType,
    Side,
    TradeOrigin,
)
from crypto_traders.domain.models import OrderRequest, OrderResult
from crypto_traders.exchanges.base import Broker, ExchangeError, InsufficientFunds
from crypto_traders.exchanges.filters import MarketFilter
from crypto_traders.exchanges.paper import PaperBroker


class BusEspiao(InMemoryEventBus):
    """Bus real, com registro do que foi publicado em cada topico."""

    def __init__(self) -> None:
        super().__init__()
        self.publicados: list[tuple[str, Any]] = []

    async def publish(self, topic: str, payload: Any) -> None:
        self.publicados.append((topic, payload))
        await super().publish(topic, payload)

    def alertas(self, tipo: str | None = None) -> list[dict]:
        alertas = [p for t, p in self.publicados if t == Topics.ALERTS]
        if tipo is None:
            return alertas
        return [a for a in alertas if a.get("type") == tipo]


async def audit_actions(settings) -> list[str]:
    async with session_scope(settings) as session:
        return [e.action for e in await AuditLogRepository(session).list()]


def make_request(
    *,
    client_order_id: str = "cat-teste-001",
    side: Side = Side.BUY,
    quantity: str = "0.001",
    price: str | None = None,
    symbol: str = "BTC/USDT",
    reference: str = "50000",
    risk_event_id: str | None = "risk-1",
) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id,
        signal_id="sig-1",
        risk_event_id=risk_event_id,
        exchange=ExchangeName.PAPER,
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
        price=Decimal(price) if price else None,
        notional=Decimal(quantity) * Decimal(reference),
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


class BrokerProgramado(Broker):
    """Devolve (ou levanta) o que o teste mandar, e conta os envios."""

    name = "programado"

    def __init__(self, resposta) -> None:
        self._resposta = resposta
        self.envios: list[str] = []

    async def place_order(self, request):
        self.envios.append(request.client_order_id)
        resultado = self._resposta(request)
        if isinstance(resultado, BaseException):
            raise resultado
        return resultado

    async def fetch_balances(self):
        return {}

    async def fetch_positions(self, prices):
        return []

    async def close(self):
        return None


class TimeoutDepoisDeAceitar(Broker):
    """A exchange aceita, a resposta se perde, o reenvio bate na duplicata.

    Reproduz o laco de retry que vive DENTRO do `CcxtExchange._with_retry`: o
    Execution Agent enxerga uma unica chamada, e e justamente por isso que o
    caso e invisivel de fora. A tentativa 1 chega na exchange e executa; o
    cliente recebe `RequestTimeout`; a tentativa 2 vai com o MESMO
    `clientOrderId` e a Binance responde `-2010 Duplicate order sent`
    (`ccxt.InvalidOrder` -> `ExchangeError` -> `OrderResult` FAILED).
    """

    name = "timeout-apos-aceitar"

    def __init__(self, real: PaperBroker, max_retries: int = 3) -> None:
        self._real = real
        self._max_retries = max_retries
        self.envios = 0

    async def place_order(self, request):
        ultimo: Exception | None = None
        for _ in range(1, self._max_retries + 1):
            try:
                return await self._crua(request)
            except TimeoutError as exc:  # _RETRYABLE no adaptador real
                ultimo = exc
                continue
            except ExchangeError as exc:  # ccxt.BaseError no adaptador real
                return OrderResult(
                    order_request_id=request.id,
                    client_order_id=request.client_order_id,
                    exchange_order_id=None,
                    status=OrderStatus.FAILED,
                    error=str(exc),
                )
        raise ExchangeError(f"falhou apos {self._max_retries} tentativas: {ultimo}")

    async def _crua(self, request):
        self.envios += 1
        if self.envios == 1:
            await self._real.place_order(request)  # a exchange EXECUTOU
            raise TimeoutError("request timeout apos a exchange aceitar")
        # O reenvio traz o MESMO `clientOrderId`, e a exchange responde o estado
        # da ordem que ela ja tem -- nao um preenchimento novo. E isto que faz o
        # reenvio ser inofensivo.
        return await self._real.place_order(request)

    async def fetch_balances(self):
        return await self._real.fetch_balances()

    async def fetch_positions(self, prices):
        return await self._real.fetch_positions(prices)

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
        assert (await paper_broker.fetch_balances())["BTC"] == Decimal("0.001")
        # A resposta do reenvio e a ORIGINAL, nao uma rejeicao: e o que a
        # exchange devolve quando consultada pelo `clientOrderId`. Dizer
        # "rejeitada" a quem reenviou depois de um timeout faria o sistema
        # esquecer uma compra que aconteceu.
        assert second.status is OrderStatus.FILLED
        assert second.filled_quantity == first.filled_quantity
        assert second.average_price == first.average_price
        assert second.raw["duplicate"] is True

    async def test_resend_does_not_move_balances_a_second_time(self, paper_broker):
        await paper_broker.place_order(make_request())
        depois_da_primeira = await paper_broker.fetch_balances()
        await paper_broker.place_order(make_request())
        assert await paper_broker.fetch_balances() == depois_da_primeira

    async def test_rejection_does_not_burn_the_client_order_id(self, paper_broker):
        """Rejeicao nao cria ordem na exchange, entao o id continua usavel.

        Memorizar a recusa faria uma nova tentativa legitima -- depois de um
        aporte, por exemplo -- receber para sempre a recusa antiga.
        """
        recusada = await paper_broker.place_order(make_request(quantity="10"))
        assert recusada.status is OrderStatus.REJECTED
        paper_broker.credit("USDT", Decimal("1000000"))
        agora = await paper_broker.place_order(make_request(quantity="10"))
        assert agora.status is OrderStatus.FILLED


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
        """Ordem sem confirmacao fica registrada -- e NAO como falha.

        A chamada que nao voltou nao prova que a ordem nao aconteceu. Gravar
        `FAILED` seria afirmar isso; o estado honesto e `PENDING` com o motivo,
        que e o unico que pede reconciliacao.
        """
        bus = BusEspiao()
        agent = ExecutionAgent(bus, ExplodingBroker(), settings)
        result = await agent._execute(make_request())

        assert result is not None and result.status is OrderStatus.PENDING
        async with session_scope(settings) as session:
            orders = await OrderRepository(session).list()
            trades = await TradeRepository(session).list()

        assert orders[0].status == str(OrderStatus.PENDING)
        assert "DESFECHO DESCONHECIDO" in (orders[0].error or "")
        assert "conexao perdida" in (orders[0].error or "")
        assert trades == []  # sem confirmacao de preenchimento, nada no historico
        assert bus.alertas("order_outcome_unknown"), "o operador precisa saber"
        assert "order_outcome_unknown" in await audit_actions(settings)

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


class TestIdempotencia:
    """O coracao do item: retry de rede nao pode duplicar nem apagar uma ordem.

    Duas garantias distintas, e o projeto tinha so a primeira:

    1. o `client_order_id` impede uma SEGUNDA ordem na exchange;
    2. o reenvio nao pode fazer o sistema ESQUECER a primeira.

    A (2) foi medida em 2026-09-09 e estava quebrada: a exchange respondia
    `-2010 Duplicate order sent` ao reenvio, o adaptador transformava isso em
    `FAILED`, e a compra que realmente aconteceu ficava sem trade -- logo sem
    preco medio, logo sem stop-loss.
    """

    async def test_timeout_depois_de_aceitar_nao_duplica_a_ordem(
        self, settings, paper_broker
    ):
        broker = TimeoutDepoisDeAceitar(paper_broker)
        agent = ExecutionAgent(BusEspiao(), broker, settings)

        await agent._execute(make_request(client_order_id="cat-timeout-1"))

        assert broker.envios == 2, "o retry precisa ter acontecido de verdade"
        saldos = await paper_broker.fetch_balances()
        # UMA compra, nao duas: 0.001 e nao 0.002. E o `client_order_id` agindo.
        assert saldos["BTC"] == Decimal("0.001")

    async def test_timeout_depois_de_aceitar_nao_perde_o_trade(
        self, settings, paper_broker
    ):
        """O reenvio inofensivo tambem tem que ser um reenvio HONESTO."""
        broker = TimeoutDepoisDeAceitar(paper_broker)
        agent = ExecutionAgent(BusEspiao(), broker, settings)

        result = await agent._execute(make_request(client_order_id="cat-timeout-2"))

        assert result is not None
        assert result.status is OrderStatus.FILLED
        assert result.filled_quantity == Decimal("0.001")

        async with session_scope(settings) as session:
            ordens = await OrderRepository(session).list()
            trades = await TradeRepository(session).list()

        assert len(ordens) == 1
        assert ordens[0].status == str(OrderStatus.FILLED)
        assert Decimal(str(ordens[0].filled_quantity)) == Decimal("0.001")
        # Sem este trade nao ha preco medio, e sem preco medio o Risk Manager
        # pula a posicao ao emitir stop-loss (`_emit_protective_exits`).
        assert len(trades) == 1
        assert Decimal(str(trades[0].quantity)) == Decimal("0.001")

    async def test_duplicata_relatada_como_falha_vira_desfecho_desconhecido(
        self, settings
    ):
        """Broker que so sabe dizer `-2010`: a ordem existe, e preciso reconciliar.

        E o caso do adaptador ccxt sem consulta por `clientOrderId`. O sistema
        nao sabe o preenchimento, mas sabe que NAO pode dizer que falhou.
        """
        broker = BrokerProgramado(
            lambda rq: OrderResult(
                order_request_id=rq.id,
                client_order_id=rq.client_order_id,
                exchange_order_id=None,
                status=OrderStatus.FAILED,
                error="binance.create_order: Duplicate order sent. (-2010)",
            )
        )
        bus = BusEspiao()
        agent = ExecutionAgent(bus, broker, settings)

        result = await agent._execute(make_request(client_order_id="cat-dup-1"))

        assert result is not None and result.status is OrderStatus.PENDING
        assert "FOI criada" in (result.error or "")
        async with session_scope(settings) as session:
            ordem = (await OrderRepository(session).list())[0]
        assert ordem.status == str(OrderStatus.PENDING)
        assert bus.alertas("order_outcome_unknown")
        assert "order_outcome_unknown" in await audit_actions(settings)

    async def test_duplicata_com_preenchimento_conhecido_nao_pede_reconciliacao(
        self, settings
    ):
        """Se a exchange ja disse quanto preencheu, nao ha o que reconciliar."""
        broker = BrokerProgramado(
            lambda rq: OrderResult(
                order_request_id=rq.id,
                client_order_id=rq.client_order_id,
                exchange_order_id="ex-1",
                status=OrderStatus.FILLED,
                filled_quantity=rq.quantity,
                average_price=Decimal("50010"),
                fee=Decimal("0.05"),
                fee_currency="USDT",
                raw={"duplicate": True},
            )
        )
        bus = BusEspiao()
        agent = ExecutionAgent(bus, broker, settings)
        result = await agent._execute(make_request(client_order_id="cat-dup-2"))

        assert result is not None and result.status is OrderStatus.FILLED
        assert not bus.alertas("order_outcome_unknown")
        async with session_scope(settings) as session:
            assert len(await TradeRepository(session).list()) == 1

    async def test_corrida_entre_dois_executores_nao_duplica(self, settings, paper_broker):
        """Dois executores no mesmo `client_order_id` ao mesmo tempo.

        Acontece de verdade: reiniciar o agente (o watchdog faz isso) deixa,
        por um instante, dois assinantes do mesmo topico. A unicidade de
        `uq_order_client_id` no banco e a autoridade final, e perder a corrida
        precisa ser um nao-evento silencioso.
        """
        a = ExecutionAgent(BusEspiao(), paper_broker, settings)
        b = ExecutionAgent(BusEspiao(), paper_broker, settings)
        pedido = make_request(client_order_id="cat-corrida-1")

        await asyncio.gather(a._execute(pedido), b._execute(pedido))

        async with session_scope(settings) as session:
            ordens = await OrderRepository(session).list()
            trades = await TradeRepository(session).list()
        assert len(ordens) == 1
        assert len(trades) == 1
        assert (await paper_broker.fetch_balances())["BTC"] == Decimal("0.001")


class TestPreenchimentoParcial:
    """Moeda que mudou de mao entra no historico, qualquer que seja o status.

    Medido em 2026-09-09: com `status is FILLED` como condicao, uma parcial de
    0,0004 BTC e um cancelamento apos preencher 0,0006 BTC gravavam ZERO trades.
    """

    def _resultado(self, status, filled):
        return lambda rq: OrderResult(
            order_request_id=rq.id,
            client_order_id=rq.client_order_id,
            exchange_order_id="ex-parcial",
            status=status,
            filled_quantity=Decimal(filled),
            average_price=Decimal("50010"),
            fee=Decimal("0.02"),
            fee_currency="USDT",
        )

    async def test_parcial_grava_a_parte_preenchida(self, settings):
        agent = ExecutionAgent(
            BusEspiao(),
            BrokerProgramado(self._resultado(OrderStatus.PARTIALLY_FILLED, "0.0004")),
            settings,
        )
        await agent._execute(make_request(client_order_id="cat-parcial-1"))

        async with session_scope(settings) as session:
            ordem = (await OrderRepository(session).list())[0]
            trades = await TradeRepository(session).list()

        assert ordem.status == str(OrderStatus.PARTIALLY_FILLED)
        assert len(trades) == 1
        assert Decimal(str(trades[0].quantity)) == Decimal("0.0004")

    async def test_cancelada_apos_preencher_parte_grava_essa_parte(self, settings):
        """Cancelamento nao devolve o que ja foi executado."""
        agent = ExecutionAgent(
            BusEspiao(),
            BrokerProgramado(self._resultado(OrderStatus.CANCELED, "0.0006")),
            settings,
        )
        await agent._execute(make_request(client_order_id="cat-cancel-1"))

        async with session_scope(settings) as session:
            ordem = (await OrderRepository(session).list())[0]
            trades = await TradeRepository(session).list()

        assert ordem.status == str(OrderStatus.CANCELED)
        assert len(trades) == 1
        assert Decimal(str(trades[0].quantity)) == Decimal("0.0006")

    async def test_cancelada_sem_preencher_nao_gera_trade(self, settings):
        agent = ExecutionAgent(
            BusEspiao(),
            BrokerProgramado(self._resultado(OrderStatus.CANCELED, "0")),
            settings,
        )
        await agent._execute(make_request(client_order_id="cat-cancel-2"))

        async with session_scope(settings) as session:
            assert (await OrderRepository(session).list())[0].status == str(
                OrderStatus.CANCELED
            )
            assert await TradeRepository(session).list() == []

    async def test_parcial_sem_preco_nao_inventa_trade(self, settings):
        """Preco ausente e bug do adaptador; trade com preco zero contamina o PnL."""
        agent = ExecutionAgent(
            BusEspiao(),
            BrokerProgramado(
                lambda rq: OrderResult(
                    order_request_id=rq.id,
                    client_order_id=rq.client_order_id,
                    exchange_order_id="ex-x",
                    status=OrderStatus.PARTIALLY_FILLED,
                    filled_quantity=Decimal("0.0004"),
                    average_price=None,
                )
            ),
            settings,
        )
        await agent._execute(make_request(client_order_id="cat-parcial-2", price=None))
        async with session_scope(settings) as session:
            assert await TradeRepository(session).list() == []


class TestSaldoInsuficienteNoEnvio:
    async def test_saldo_insuficiente_descoberto_no_envio_e_recusa_definitiva(
        self, settings
    ):
        """A exchange respondeu "nao": nao ha nada para reconciliar.

        Distinguir isso de um timeout importa: um pede reconciliacao manual e
        alerta, o outro e simplesmente uma ordem que nao aconteceu.
        """
        broker = BrokerProgramado(
            lambda rq: InsufficientFunds("saldo insuficiente de USDT: disponivel 1")
        )
        bus = BusEspiao()
        agent = ExecutionAgent(bus, broker, settings)

        result = await agent._execute(make_request(client_order_id="cat-sem-saldo-1"))

        assert result is not None and result.status is OrderStatus.REJECTED
        async with session_scope(settings) as session:
            ordem = (await OrderRepository(session).list())[0]
            assert await TradeRepository(session).list() == []
        assert ordem.status == str(OrderStatus.REJECTED)
        assert "insuficiente" in (ordem.error or "")
        assert not bus.alertas("order_outcome_unknown")

    async def test_saldo_insuficiente_no_paper_broker_nao_move_nada(
        self, settings, paper_broker
    ):
        bus = BusEspiao()
        agent = ExecutionAgent(bus, paper_broker, settings)
        antes = await paper_broker.fetch_balances()

        await agent._execute(
            make_request(client_order_id="cat-sem-saldo-2", quantity="10")
        )

        assert await paper_broker.fetch_balances() == antes
        async with session_scope(settings) as session:
            assert (await OrderRepository(session).list())[0].status == str(
                OrderStatus.REJECTED
            )


class TestFiltrosDaExchangeAntesDoEnvio:
    """Ordem que morreria nos filtros e recusada AQUI, com motivo -- nunca enviada.

    Antes disto, `check_order_viability` era chamado somente pelo `cli.py`: o
    caminho de envio nao consultava filtro nenhum, e uma ordem que o
    truncamento jogava abaixo do MIN_NOTIONAL era mandada para a Binance e
    recusada por ela.

    Numeros do ensaio de hoje (16 pares /USDC, ordem de 5,86 USDC): em BNB/USDC,
    passo de 0,001 BNB a 862,50, os 5,86 viram 0,006 BNB = 5,175 USDC e passam
    de raspao pelo minimo de 5. Basta o BNB subir para ~1.200 e os mesmos 5,86
    viram 0,004 BNB = 4,80 USDC -- abaixo do minimo.
    """

    BNB = MarketFilter("BNB/USDC", Decimal("0.001"), Decimal("5"), Decimal("0"))

    def _broker_com_filtro(self, preco: str) -> PaperBroker:
        broker = PaperBroker(quote_currency="USDC", initial_balance=Decimal("1000"))
        broker.set_price("BNB", Decimal(preco))
        broker.set_market_filter(self.BNB)
        return broker

    async def test_o_caso_de_hoje_ainda_passa(self, settings):
        """5,86 a 862,50 sobrevive: a protecao nao pode barrar o que funciona."""
        broker = self._broker_com_filtro("862.50")
        agent = ExecutionAgent(BusEspiao(), broker, settings)
        result = await agent._execute(
            make_request(
                client_order_id="cat-bnb-ok",
                symbol="BNB/USDC",
                quantity="0.006",
                reference="862.50",
            )
        )
        assert result is not None and result.status is OrderStatus.FILLED

    async def test_arredondamento_abaixo_do_minimo_nao_e_enviado(self, settings):
        broker = self._broker_com_filtro("1200")
        # 5,86 / 1200 = 0,004883 BNB -> trunca para 0,004 -> 4,80 USDC < 5
        pedido = make_request(
            client_order_id="cat-bnb-baixo",
            symbol="BNB/USDC",
            quantity="0.004883",
            reference="1200",
        )
        bus = BusEspiao()
        agent = ExecutionAgent(bus, broker, settings)

        result = await agent._execute(pedido)

        assert result is None, "a ordem nao pode ter sido enviada"
        # Nada saiu: o saldo do simulador esta intacto.
        assert (await broker.fetch_balances()) == {"USDC": Decimal("1000")}
        # E a recusa tem motivo registrado, nao e um silencio.
        async with session_scope(settings) as session:
            assert await OrderRepository(session).list() == []
        assert "order_below_exchange_filter" in await audit_actions(settings)
        alertas = bus.alertas("order_below_exchange_filter")
        assert alertas and "4.80" in alertas[0]["message"]

    async def test_venda_presa_abaixo_do_minimo_alerta_posicao_presa(self, settings):
        """Venda recusada por filtro significa stop-loss sem como sair.

        E um alerta proprio de proposito: e o estado mais perigoso que este
        caminho pode produzir, e confundi-lo com "ordem pequena demais"
        esconderia que a posicao ficou sem protecao.
        """
        broker = self._broker_com_filtro("1200")
        broker.credit("BNB", Decimal("0.004"))
        bus = BusEspiao()
        agent = ExecutionAgent(bus, broker, settings)

        result = await agent._execute(
            make_request(
                client_order_id="cat-bnb-presa",
                symbol="BNB/USDC",
                side=Side.SELL,
                quantity="0.004",
                reference="1200",
                risk_event_id="protecao-stop_loss-BNB",
            )
        )

        assert result is None
        alertas = bus.alertas("position_trapped")
        assert alertas, "posicao presa precisa de alerta proprio"
        assert "stop-loss" in alertas[0]["message"].lower()
        assert "position_trapped" in await audit_actions(settings)

    async def test_par_desconhecido_em_toda_fonte_segue_o_caminho_antigo(self, settings):
        """Ausencia de dado nunca vira recusa -- nem aprovacao silenciosa.

        O par tem que ser um que NAO existe na Binance: desde que o catalogo
        versionado passou a ser o piso, todo par real e conhecido, e usar um par
        real aqui testaria o contrario do que o nome diz.
        """
        broker = PaperBroker(quote_currency="USDC", initial_balance=Decimal("1000"))
        broker.set_price("MOEDAINEXISTENTE", Decimal("1200"))
        assert broker.market_filter("MOEDAINEXISTENTE/USDC") is None
        agent = ExecutionAgent(BusEspiao(), broker, settings)
        result = await agent._execute(
            make_request(
                client_order_id="cat-sem-filtro",
                symbol="MOEDAINEXISTENTE/USDC",
                quantity="0.004883",
                reference="1200",
            )
        )
        assert result is not None and result.status is OrderStatus.FILLED

    async def test_broker_que_nao_conhece_filtros_nao_e_barrado(self, settings):
        """`ExplodingBroker` nao implementa `MarketFilterSource`: passa reto."""
        agent = ExecutionAgent(BusEspiao(), ExplodingBroker(), settings)
        result = await agent._execute(make_request(client_order_id="cat-sem-source"))
        assert result is not None  # chegou a tentar enviar

    async def test_o_simulador_recusa_o_que_a_exchange_recusaria(self, settings):
        """Dry_run que preenche o que a Binance recusa mede uma estrategia falsa."""
        broker = self._broker_com_filtro("1200")
        result = await broker.place_order(
            make_request(symbol="BNB/USDC", quantity="0.004", reference="1200")
        )
        assert result.status is OrderStatus.REJECTED
        assert "filtro da exchange" in (result.error or "")
        assert (await broker.fetch_balances()) == {"USDC": Decimal("1000")}


class TestSoOQueORiskManagerAprovou:
    """Nada chega ao envio sem passar pelo Risk Manager.

    O caminho real: `Topics.ORDER_REQUESTS` e publicado **somente** pelo
    `risk_manager.py`, e o `_execute` recusa qualquer pedido sem
    `risk_event_id`. Os dois testes estruturais existem porque a garantia nao
    esta em nenhum tipo: bastaria uma rota da API publicar no topico para a
    fronteira deixar de existir, e nada no `OrderRequest` impediria isso.
    """

    async def test_pedido_sem_risk_event_id_nao_e_enviado(self, settings, paper_broker):
        agent = ExecutionAgent(BusEspiao(), paper_broker, settings)
        pedido = make_request(client_order_id="cat-sem-risco", risk_event_id="")

        result = await agent._execute(pedido)

        assert result is None
        assert (await paper_broker.fetch_balances()) == {"USDT": Decimal("1000")}
        async with session_scope(settings) as session:
            assert await OrderRepository(session).list() == []

    def test_so_o_risk_manager_publica_em_order_requests(self):
        src = Path(__file__).resolve().parents[1] / "src" / "crypto_traders"
        publicadores = sorted(
            arquivo.relative_to(src).as_posix()
            for arquivo in src.rglob("*.py")
            if "publish(Topics.ORDER_REQUESTS" in arquivo.read_text(encoding="utf-8")
        )
        assert publicadores == ["agents/risk_manager.py"], (
            "alguem passou a publicar ordens direto no topico do Execution Agent: "
            f"{publicadores}"
        )

    def test_so_o_execution_agent_chama_place_order_em_producao(self):
        """`place_order` fora do Execution Agent e uma ordem sem guardiao.

        O backtest e a excecao consciente (D3): ele reutiliza o `PaperBroker`
        de producao, e por definicao nenhuma ordem dele sai da maquina.
        """
        src = Path(__file__).resolve().parents[1] / "src" / "crypto_traders"
        chamadores = sorted(
            arquivo.relative_to(src).as_posix()
            for arquivo in src.rglob("*.py")
            if ".place_order(" in arquivo.read_text(encoding="utf-8")
        )
        assert chamadores == [
            "agents/execution.py",
            "backtest/engine.py",
            "backtest/portfolio.py",
        ], f"novo chamador de place_order fora do Execution Agent: {chamadores}"


class TestDinheiroEmDecimal:
    async def test_o_trade_gravado_nao_passa_por_float(self, settings, paper_broker):
        """D7: dinheiro em `Decimal` da ponta a ponta.

        A checagem e do VALOR, nao do tipo declarado: um `float` intermediario
        deixa residuo binario, e comparar com o `Decimal` exato acusa o desvio.
        """
        agent = ExecutionAgent(BusEspiao(), paper_broker, settings)
        await agent._execute(
            make_request(client_order_id="cat-decimal-1", quantity="0.003")
        )
        async with session_scope(settings) as session:
            trade = (await TradeRepository(session).list())[0]

        assert Decimal(str(trade.quantity)) == Decimal("0.003")
        # 50000 * 1.0005 = 50025 exatos; em float o produto nao fecha.
        assert Decimal(str(trade.price)) == Decimal("50025")
        assert Decimal(str(trade.fee)) == Decimal("0.003") * Decimal(
            "50025"
        ) * Decimal("0.001")


class TestOFiltroAgeNoSistemaComoEleSobe:
    """A checagem de filtro precisa AGIR no broker que o sistema constroi.

    Nao basta ela passar com um filtro injetado a mao pelo teste. Medido em
    2026-09-09: `set_market_filter` nao tinha UM chamador em `src/`, o
    `CcxtExchange` nao implementava `MarketFilterSource`, e o resultado era que
    em TODA configuracao real (dry_run, testnet, live) o `_preflight` devolvia
    `None` e a ordem de 4,80 USDC era enviada e recusada pela Binance --
    exatamente o que a checagem existia para impedir.
    """

    def _settings_de_fabrica(self):
        from crypto_traders.config import Settings

        return Settings(_env_file=None)

    def test_o_broker_de_dry_run_conhece_os_filtros_dos_pares(self):
        from crypto_traders.exchanges import build_broker

        broker = build_broker(self._settings_de_fabrica())
        assert isinstance(broker, PaperBroker)
        filtro = broker.market_filter("BNB/USDC")
        assert filtro is not None, "o broker que o sistema sobe nao conhece filtro nenhum"
        assert filtro.amount_step == Decimal("0.001")
        assert filtro.min_cost == Decimal("5")

    def test_o_broker_de_producao_implementa_a_fonte_de_filtros(self):
        from crypto_traders.exchanges.ccxt_adapter import CcxtExchange
        from crypto_traders.exchanges.filters import MarketFilterSource

        assert issubclass(CcxtExchange, MarketFilterSource)

    async def test_em_producao_o_catalogo_ao_vivo_tem_precedencia(self):
        """Filtro que a exchange acabou de informar vale mais que a foto datada."""
        from crypto_traders.exchanges.ccxt_adapter import CcxtExchange
        from crypto_traders.exchanges.filters import FONTE_AO_VIVO, FONTE_BASELINE

        exchange = CcxtExchange("binance", credentials=None)
        cliente = exchange._client
        try:
            # Sem `load_markets` nesta instancia: vale o catalogo versionado,
            # para que a PRIMEIRA ordem do processo tambem seja checada.
            antes = exchange.market_filter("BNB/USDC")
            assert antes is not None and antes.source == FONTE_BASELINE

            cliente.markets = {
                "BNB/USDC": {
                    "precision": {"amount": 0.0001},
                    "limits": {"cost": {"min": 7}, "amount": {"min": 0.0001}},
                }
            }
            agora = exchange.market_filter("BNB/USDC")
            assert agora is not None
            assert agora.source == FONTE_AO_VIVO
            assert agora.amount_step == Decimal("0.0001")
            assert agora.min_cost == Decimal("7")
        finally:
            await cliente.close()

    async def test_o_caso_do_mandato_nao_e_enviado_no_broker_real(self, settings):
        """5,86 USDC com BNB a 1.200 -> 0,004 BNB = 4,80, abaixo do minimo de 5.

        Nenhum filtro injetado: o broker e o que `build_broker` devolve.
        """
        from crypto_traders.exchanges import build_broker

        broker = build_broker(self._settings_de_fabrica())
        broker.set_price("BNB", Decimal("1200"))
        # Saldo de sobra de proposito: se recusar, tem que ser pelo filtro.
        broker.credit("USDC", Decimal("1000"))
        bus = BusEspiao()
        agent = ExecutionAgent(bus, broker, settings)

        result = await agent._execute(
            make_request(
                client_order_id="cat-real-bnb",
                symbol="BNB/USDC",
                quantity="0.004883",
                reference="1200",
            )
        )

        assert result is None, "a ordem foi enviada no broker que o sistema sobe"
        assert "order_below_exchange_filter" in await audit_actions(settings)
        alertas = bus.alertas("order_below_exchange_filter")
        assert alertas and "4.80" in alertas[0]["message"]
        # E a recusa diz de onde veio o filtro: catalogo datado nao e a mesma
        # afirmacao que dado ao vivo da exchange.
        assert "catalogo versionado" in alertas[0]["message"]

    async def test_o_par_de_hoje_que_funciona_continua_funcionando(self, settings):
        """BTC/USDC a 50 mil com 0,001 BTC = 50 USDC: passa, e tem que passar.

        Protecao que barra o que funciona nao e protecao, e um desligamento.
        """
        from crypto_traders.exchanges import build_broker

        broker = build_broker(self._settings_de_fabrica())
        broker.set_price("BTC", Decimal("50000"))
        broker.credit("USDC", Decimal("1000"))
        agent = ExecutionAgent(BusEspiao(), broker, settings)

        result = await agent._execute(
            make_request(client_order_id="cat-real-btc", symbol="BTC/USDC")
        )
        assert result is not None and result.status is OrderStatus.FILLED


class TestTravaDeAberturaFechaAJanela:
    """D4 com a ordem AINDA EM CASA: a trava e lida duas vezes.

    A primeira versao lia uma vez, e entre a leitura e o `place_order` havia
    dois `await`. Medido em 2026-09-09 armando a trava como ultimo ato de
    `create_pending`: sequencia ['trava armada', 'ordem enviada'] -- a compra
    saia depois do circuit breaker disparar. E alcancavel em producao porque
    `orchestrator.pause_all` chama `block_openings` de dentro do MESMO event
    loop, logo ela roda enquanto `_execute` esta suspenso no aiosqlite.
    """

    async def test_trava_armada_durante_a_gravacao_impede_o_envio(
        self, settings, paper_broker, monkeypatch
    ):
        bus = BusEspiao()
        agent = ExecutionAgent(bus, paper_broker, settings)

        fatos: list[str] = []
        create_pending_real = OrderRepository.create_pending
        place_order_real = PaperBroker.place_order

        async def create_pending_espiao(self, request, mode):
            registro = await create_pending_real(self, request, mode)
            agent.block_openings("perda diaria de 12% -- circuit breaker")
            fatos.append("trava armada")
            return registro

        async def place_order_espiao(self, request):
            fatos.append("ordem enviada")
            return await place_order_real(self, request)

        monkeypatch.setattr(OrderRepository, "create_pending", create_pending_espiao)
        monkeypatch.setattr(PaperBroker, "place_order", place_order_espiao)

        result = await agent._execute(make_request(client_order_id="cat-janela-1"))

        assert fatos == ["trava armada"], f"sequencia medida: {fatos}"
        assert result is None
        assert "BTC" not in await paper_broker.fetch_balances()

    async def test_a_ordem_pending_da_janela_nao_fica_pendurada(
        self, settings, paper_broker, monkeypatch
    ):
        """A linha PENDING gravada precisa ser encerrada com motivo.

        Deixa-la PENDING pediria reconciliacao de uma ordem que nunca existiu na
        exchange -- e PENDING e o status reservado para "tentada, sem
        confirmacao", que nao e o caso aqui.
        """
        bus = BusEspiao()
        agent = ExecutionAgent(bus, paper_broker, settings)
        create_pending_real = OrderRepository.create_pending

        async def create_pending_espiao(self, request, mode):
            registro = await create_pending_real(self, request, mode)
            agent.block_openings("perda semanal -- circuit breaker")
            return registro

        monkeypatch.setattr(OrderRepository, "create_pending", create_pending_espiao)

        await agent._execute(make_request(client_order_id="cat-janela-2"))

        async with session_scope(settings) as session:
            ordem = (await OrderRepository(session).list())[0]
            trades = await TradeRepository(session).list()
        assert ordem.status == str(OrderStatus.REJECTED), (
            f"ordem ficou como '{ordem.status}': PENDING aqui pede reconciliacao "
            "de uma ordem que nunca saiu"
        )
        assert "recusada antes do envio" in (ordem.error or "")
        assert trades == []
        # O cooldown do Risk Manager ignora REJECTED de proposito: uma ordem que
        # nao aconteceu nao pode consumir a janela de espera do par.
        async with session_scope(settings) as session:
            assert await OrderRepository(session).last_order_time("BTC/USDT") is None

        acoes = await audit_actions(settings)
        assert "opening_refused" in acoes
        assert bus.alertas("opening_refused"), "a recusa da janela precisa alertar"
        resultados = [p for t, p in bus.publicados if t == Topics.ORDER_RESULTS]
        assert [r.status for r in resultados] == [OrderStatus.REJECTED]

    async def test_a_janela_nunca_barra_o_fechamento(
        self, settings, paper_broker, monkeypatch
    ):
        """A segunda leitura mantem a assimetria: SELL passa com a trava armada."""
        paper_broker.credit("BTC", Decimal("0.001"))
        agent = ExecutionAgent(BusEspiao(), paper_broker, settings)
        create_pending_real = OrderRepository.create_pending

        async def create_pending_espiao(self, request, mode):
            registro = await create_pending_real(self, request, mode)
            agent.block_openings("perda diaria -- circuit breaker")
            return registro

        monkeypatch.setattr(OrderRepository, "create_pending", create_pending_espiao)

        result = await agent._execute(
            make_request(client_order_id="cat-janela-3", side=Side.SELL)
        )
        assert result is not None and result.status is OrderStatus.FILLED


class TestAberturaExigeStopLoss:
    """Ultimo portao do item "toda ordem carrega stop-loss antes de ser enviada".

    A garantia existia so a montante (`risk/rules.py` sempre preenche na
    aprovacao de abertura). Garantia a montante nao e portao: tres fabricas de
    teste do repositorio criavam BUY sem `stop_loss` e nada reclamava, o que
    prova que o caminho aceitava.
    """

    async def test_compra_sem_stop_loss_nao_e_enviada(self, settings, paper_broker):
        bus = BusEspiao()
        agent = ExecutionAgent(bus, paper_broker, settings)
        pedido = make_request(client_order_id="cat-sem-stop").model_copy(
            update={"stop_loss": None}
        )

        result = await agent._execute(pedido)

        assert result is None
        assert "BTC" not in await paper_broker.fetch_balances()
        async with session_scope(settings) as session:
            assert await OrderRepository(session).list() == []
        assert "opening_without_stop_loss" in await audit_actions(settings)
        alertas = bus.alertas("opening_without_stop_loss")
        assert alertas and "stop-loss" in alertas[0]["title"]

    async def test_fechamento_sem_stop_loss_continua_passando(
        self, settings, paper_broker
    ):
        """A saida de protecao do Risk Manager publica `stop_loss=None`.

        Barrar venda aqui mataria exatamente o stop-loss que se quer garantir.
        """
        paper_broker.credit("BTC", Decimal("0.001"))
        agent = ExecutionAgent(BusEspiao(), paper_broker, settings)
        pedido = make_request(
            client_order_id="cat-fecha-sem-stop",
            side=Side.SELL,
            risk_event_id="protecao-stop_loss-BTC",
        ).model_copy(update={"stop_loss": None, "take_profit": None})

        result = await agent._execute(pedido)
        assert result is not None and result.status is OrderStatus.FILLED

    async def test_a_recusa_repetida_registra_sempre_e_alerta_uma_vez(
        self, settings, paper_broker
    ):
        bus = BusEspiao()
        agent = ExecutionAgent(bus, paper_broker, settings)
        for i in range(6):
            await agent._execute(
                make_request(client_order_id=f"cat-sem-stop-{i}").model_copy(
                    update={"stop_loss": None}
                )
            )
        assert len(bus.alertas("opening_without_stop_loss")) == 1
        assert (await audit_actions(settings)).count("opening_without_stop_loss") == 6


class TestFreioDoAlertaDeRecusaPorFiltro:
    """Recusa por filtro nao e evento unico, e sem freio vira alerta continuo.

    Poeira abaixo do MIN_NOTIONAL faz o Risk Manager reemitir a venda de
    protecao a cada ciclo. Sem freio eram 20 alertas `position_trapped` para a
    MESMA posicao -- a mesma patologia dos 17 reinicios em 16 minutos do ensaio
    (D25), agora no alerta que menos pode ser ignorado.
    """

    def _broker(self) -> PaperBroker:
        broker = PaperBroker(quote_currency="USDC", initial_balance=Decimal("1000"))
        broker.set_price("BNB", Decimal("1200"))
        broker.credit("BNB", Decimal("0.004"))
        return broker

    async def test_a_mesma_posicao_presa_alerta_uma_vez_e_registra_sempre(
        self, settings
    ):
        broker = self._broker()
        bus = BusEspiao()
        agent = ExecutionAgent(bus, broker, settings)

        for i in range(20):
            await agent._execute(
                make_request(
                    client_order_id=f"cat-presa-{i}",
                    symbol="BNB/USDC",
                    side=Side.SELL,
                    quantity="0.004",
                    reference="1200",
                    risk_event_id="protecao-stop_loss-BNB",
                )
            )

        assert len(bus.alertas("position_trapped")) == 1
        # O freio e do ALERTA: o audit_log continua com um registro por ciclo,
        # porque e ele que prova quantas vezes a protecao tentou sair.
        assert (await audit_actions(settings)).count("position_trapped") == 20

    async def test_pares_diferentes_alertam_separado(self, settings):
        """Deduplicar por tipo esconderia a segunda posicao presa."""
        broker = self._broker()
        broker.set_price("SOL", Decimal("1200"))
        broker.credit("SOL", Decimal("0.004"))
        bus = BusEspiao()
        agent = ExecutionAgent(bus, broker, settings)

        for i, par in enumerate(("BNB/USDC", "SOL/USDC")):
            await agent._execute(
                make_request(
                    client_order_id=f"cat-presa-par-{i}",
                    symbol=par,
                    side=Side.SELL,
                    quantity="0.004",
                    reference="1200",
                )
            )
        assert len(bus.alertas("position_trapped")) == 2

    async def test_ordem_que_passa_encerra_o_episodio_do_par(self, settings):
        """Se o par voltar a ficar preso depois, tem que alertar de novo."""
        broker = self._broker()
        bus = BusEspiao()
        agent = ExecutionAgent(bus, broker, settings)

        async def venda_presa(cid: str):
            return await agent._execute(
                make_request(
                    client_order_id=cid,
                    symbol="BNB/USDC",
                    side=Side.SELL,
                    quantity="0.004",
                    reference="1200",
                )
            )

        assert await venda_presa("cat-episodio-1") is None
        assert await venda_presa("cat-episodio-2") is None
        assert len(bus.alertas("position_trapped")) == 1

        # Agora uma venda viavel do MESMO par consegue sair: episodio encerrado.
        broker.credit("BNB", Decimal("0.010"))
        ok = await agent._execute(
            make_request(
                client_order_id="cat-episodio-ok",
                symbol="BNB/USDC",
                side=Side.SELL,
                quantity="0.010",
                reference="1200",
            )
        )
        assert ok is not None and ok.status is OrderStatus.FILLED

        assert await venda_presa("cat-episodio-3") is None
        assert len(bus.alertas("position_trapped")) == 2, (
            "novo aprisionamento depois de uma ordem que passou tem que alertar"
        )


class TestDuplicataComPreenchimentoNaoFicaComoFalha:
    """`-2010` que ja traz o preenchimento: o status tem que concordar com o trade.

    `_classificar` saia cedo quando `filled_quantity > 0` e devolvia o resultado
    com status FAILED intacto. `_record` gravava o trade (certo, a cripto mudou
    de mao) e persistia a ordem como `failed` (errado): a tabela que o dono usa
    para conferir dinheiro ficava com uma ordem falhada carregando um trade
    preenchido, e o log saia como `execution.partially_filled status=failed`.
    """

    def _broker(self, preenchido: str):
        class Duplicata(Broker):
            name = "duplicata"

            async def place_order(self, request):
                return OrderResult(
                    order_request_id=request.id,
                    client_order_id=request.client_order_id,
                    exchange_order_id=None,
                    status=OrderStatus.FAILED,
                    error="binance.create_order: Duplicate order sent. (-2010)",
                    filled_quantity=Decimal(preenchido),
                    average_price=Decimal("50000"),
                )

            async def fetch_balances(self):
                return {}

            async def fetch_positions(self, prices):
                return []

            async def close(self):
                return None

        return Duplicata()

    async def test_preenchimento_total_vira_filled(self, settings):
        agent = ExecutionAgent(BusEspiao(), self._broker("0.001"), settings)
        result = await agent._execute(make_request(client_order_id="cat-dup-total"))

        assert result is not None and result.status is OrderStatus.FILLED
        # O motivo original nao se perde na correcao do status.
        assert "-2010" in (result.error or "")
        async with session_scope(settings) as session:
            ordem = (await OrderRepository(session).list())[0]
            trades = await TradeRepository(session).list()
        assert ordem.status == str(OrderStatus.FILLED)
        assert len(trades) == 1

    async def test_preenchimento_parcial_vira_partially_filled(self, settings):
        agent = ExecutionAgent(BusEspiao(), self._broker("0.0004"), settings)
        result = await agent._execute(make_request(client_order_id="cat-dup-parcial"))

        assert result is not None and result.status is OrderStatus.PARTIALLY_FILLED
        async with session_scope(settings) as session:
            ordem = (await OrderRepository(session).list())[0]
            trades = await TradeRepository(session).list()
        assert ordem.status == str(OrderStatus.PARTIALLY_FILLED)
        assert Decimal(str(trades[0].quantity)) == Decimal("0.0004")

    async def test_duplicata_sem_preenchimento_continua_pedindo_reconciliacao(
        self, settings
    ):
        """Sem quantidade preenchida na resposta, o desfecho segue desconhecido."""

        class SemQuantidade(Broker):
            name = "duplicata-sem-qtd"

            async def place_order(self, request):
                return OrderResult(
                    order_request_id=request.id,
                    client_order_id=request.client_order_id,
                    exchange_order_id=None,
                    status=OrderStatus.FAILED,
                    error="binance.create_order: Duplicate order sent. (-2010)",
                )

            async def fetch_balances(self):
                return {}

            async def fetch_positions(self, prices):
                return []

            async def close(self):
                return None

        bus = BusEspiao()
        agent = ExecutionAgent(bus, SemQuantidade(), settings)
        result = await agent._execute(make_request(client_order_id="cat-dup-sem-qtd"))
        assert result is not None and result.status is OrderStatus.PENDING
        assert bus.alertas("order_outcome_unknown")


class TestAuditoriaQueFalhaNaoFicaEmSilencio:
    """Auditoria que falha em silencio troca "nao registrei" por "nada aconteceu".

    As tres gravacoes de auditoria deste agente estavam dentro de
    `contextlib.suppress(Exception)`: a ordem era recusada e nao sobrava rastro
    em lugar nenhum. Engolir a excecao continua certo (falhar aqui nao pode
    desfazer uma recusa, que e o estado seguro), engolir calado nao.
    """

    async def test_a_falha_de_auditoria_vai_para_o_log_com_o_payload(
        self, settings, paper_broker, monkeypatch
    ):
        from structlog.testing import capture_logs

        async def append_explode(self, **kwargs):
            raise RuntimeError("banco em disco cheio")

        monkeypatch.setattr(AuditLogRepository, "append", append_explode)

        bus = BusEspiao()
        agent = ExecutionAgent(bus, paper_broker, settings)
        agent.block_openings("perda diaria -- circuit breaker")

        with capture_logs() as registros:
            result = await agent._execute(make_request(client_order_id="cat-audit-1"))

        # A recusa aconteceu, mesmo com a auditoria caida.
        assert result is None
        assert "BTC" not in await paper_broker.fetch_balances()

        falhas = [r for r in registros if r["event"] == "execution.audit_write_failed"]
        assert falhas, "auditoria falhou em silencio: nao sobrou rastro em lugar nenhum"
        assert falhas[0]["action"] == "opening_refused"
        assert falhas[0]["payload"]["client_order_id"] == "cat-audit-1"
        assert "banco em disco cheio" in falhas[0]["error"]
        # E o alerta continua saindo: o operador nao pode depender do audit_log
        # para saber que a trava agiu.
        assert bus.alertas("opening_refused")
