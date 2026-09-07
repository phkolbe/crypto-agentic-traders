"""Testes do tratamento de acesso negado pela exchange (chave / IP / permissão).

Este é o cenário mais perigoso e menos visível do sistema: os dados de mercado
são públicos e continuam chegando, então o dashboard segue atualizando enquanto
nenhuma ordem consegue mais sair. Com posição aberta, o stop-loss deixa de
existir na prática.

O que precisa ser garantido: o erro é classificado corretamente, o alerta sai
uma vez por incidente (não por ordem), e a recuperação é anunciada.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from crypto_traders.agents.execution import ExecutionAgent
from crypto_traders.bus import InMemoryEventBus, Topics
from crypto_traders.db.repositories import OrderRepository, TradeRepository
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import ExchangeName, OrderStatus, OrderType, Side
from crypto_traders.domain.models import OrderRequest, OrderResult
from crypto_traders.exchanges.base import ApiAccessDenied, Broker
from crypto_traders.exchanges.ccxt_adapter import _access_denied_message


def make_request(client_order_id: str = "cat-acesso-001") -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id,
        signal_id="sig-1",
        risk_event_id="risk-1",
        exchange=ExchangeName.BINANCE,
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        notional=Decimal("50"),
        strategy="ma_crossover",
    )


class DeniedBroker(Broker):
    """Exchange recusando a credencial, como quando o IP sai da whitelist."""

    name = "binance"

    def __init__(self, raw: str = '{"code":-2015,"msg":"Invalid API-key, IP, or permissions"}'):
        self._raw = raw
        self.calls = 0
        self.allow = False
        """Quando True, passa a aceitar ordens — simula o IP sendo corrigido."""

    async def place_order(self, request: OrderRequest) -> OrderResult:
        self.calls += 1
        if self.allow:
            return OrderResult(
                order_request_id=request.id,
                client_order_id=request.client_order_id,
                exchange_order_id="ok-1",
                status=OrderStatus.FILLED,
                filled_quantity=request.quantity,
                average_price=Decimal("50000"),
            )
        raise ApiAccessDenied(
            _access_denied_message(self.name, "create_order", self._raw),
            exchange=self.name,
            operation="create_order",
        )

    async def fetch_balances(self):
        return {}

    async def fetch_positions(self, prices):
        return []

    async def close(self):
        return None


async def collect_alerts(
    bus: InMemoryEventBus, count: int
) -> tuple[list[dict], asyncio.Task[None]]:
    """Assina os alertas e devolve a lista que será preenchida, mais a tarefa.

    A lista volta vazia: quem chama publica o evento e depois aguarda a tarefa.
    A assinatura do bus só se registra quando o gerador é iterado, daí o
    `sleep(0)` antes de devolver — sem ele o evento seria publicado no vazio.
    """
    received: list[dict] = []

    async def listen() -> None:
        async for alert in bus.subscribe(Topics.ALERTS):
            received.append(alert)
            if len(received) >= count:
                break

    task = asyncio.create_task(listen())
    await asyncio.sleep(0)
    return received, task


class TestErrorClassification:
    def test_binance_2015_points_at_the_ip(self):
        """`-2015` é ambíguo na Binance, e num IP residencial a causa provável é o IP."""
        message = _access_denied_message(
            "binance",
            "create_order",
            '{"code":-2015,"msg":"Invalid API-key, IP, or permissions"}',
        )
        assert "IP desta maquina mudou" in message

    def test_explicit_whitelist_message_points_at_the_ip(self):
        message = _access_denied_message(
            "binance", "create_order", "Request IP is not in whitelist"
        )
        assert "IP desta maquina mudou" in message

    def test_key_format_error_does_not_blame_the_ip(self):
        """`-2014` é problema da chave; culpar o IP mandaria investigar o lugar errado."""
        message = _access_denied_message(
            "binance", "create_order", '{"code":-2014,"msg":"API-key format invalid."}'
        )
        assert "IP desta maquina mudou" not in message
        assert "chave" in message

    def test_signature_error_does_not_blame_the_ip(self):
        message = _access_denied_message(
            "binance",
            "create_order",
            '{"code":-1022,"msg":"Signature for this request is not valid."}',
        )
        assert "IP desta maquina mudou" not in message

    def test_unrelated_message_with_the_letters_ip_does_not_blame_the_ip(self):
        """"ip" como substring solta casaria com "multiple", "description"..."""
        message = _access_denied_message(
            "binance", "create_order", "Order would immediately match. Multiple descriptions"
        )
        assert "IP desta maquina mudou" not in message

    def test_keeps_the_original_error_for_diagnosis(self):
        message = _access_denied_message("binance", "create_order", "erro-cru-especifico")
        assert "erro-cru-especifico" in message


class TestExecutionAgentOnAccessDenied:
    async def test_order_is_recorded_as_failed(self, settings):
        """A ordem não pode ficar PENDING para sempre sem explicação."""
        agent = ExecutionAgent(InMemoryEventBus(), DeniedBroker(), settings)
        result = await agent._execute(make_request())

        assert result is not None and result.status is OrderStatus.FAILED
        async with session_scope(settings) as session:
            orders = await OrderRepository(session).list()
            trades = await TradeRepository(session).list()

        assert orders[0].status == str(OrderStatus.FAILED)
        assert "acesso negado" in (orders[0].error or "")
        assert trades == []

    async def test_publishes_an_alert_naming_the_open_position_risk(self, settings):
        """O alerta precisa dizer o que está em jogo, não só que houve erro."""
        bus = InMemoryEventBus()
        await bus.start()
        agent = ExecutionAgent(bus, DeniedBroker(), settings)

        received, task = await collect_alerts(bus, 1)
        await agent._execute(make_request())
        await asyncio.wait_for(task, timeout=2)

        alert = received[0]
        assert alert["type"] == "api_access_denied"
        assert "stop-loss" in alert["message"]
        assert "dashboard" in alert["message"]

    async def test_alerts_once_per_incident_not_per_order(self, settings):
        """Com o IP fora da whitelist toda ordem falha.

        Um alerta por ordem viraria spam, e spam faz o operador ignorar
        justamente o aviso que importa.
        """
        bus = InMemoryEventBus()
        await bus.start()
        broker = DeniedBroker()
        agent = ExecutionAgent(bus, broker, settings)

        alerts: list[dict] = []

        async def listen():
            async for alert in bus.subscribe(Topics.ALERTS):
                alerts.append(alert)

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)

        for index in range(4):
            await agent._execute(make_request(f"cat-acesso-{index}"))
        await asyncio.sleep(0.1)
        task.cancel()

        assert broker.calls == 4, "todas as ordens foram tentadas"
        assert len(alerts) == 1, f"esperado 1 alerta por incidente, veio {len(alerts)}"

    async def test_exposes_the_denied_state(self, settings):
        agent = ExecutionAgent(InMemoryEventBus(), DeniedBroker(), settings)
        assert not agent.access_denied
        await agent._execute(make_request())
        assert agent.access_denied

    async def test_announces_recovery_when_orders_are_accepted_again(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        broker = DeniedBroker()
        agent = ExecutionAgent(bus, broker, settings)

        alerts: list[dict] = []

        async def listen():
            async for alert in bus.subscribe(Topics.ALERTS):
                alerts.append(alert)

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)

        await agent._execute(make_request("cat-neg-1"))
        broker.allow = True  # IP corrigido na exchange
        await agent._execute(make_request("cat-ok-1"))
        await asyncio.sleep(0.1)
        task.cancel()

        assert [a["type"] for a in alerts] == ["api_access_denied", "api_access_restored"]
        assert not agent.access_denied

    async def test_a_plain_failure_does_not_trigger_the_access_alert(self, settings):
        """Saldo insuficiente não é perda de acesso; confundir os dois gera ruído."""
        bus = InMemoryEventBus()
        await bus.start()

        class BrokenBroker(DeniedBroker):
            async def place_order(self, request):
                raise ConnectionError("timeout de rede")

        agent = ExecutionAgent(bus, BrokenBroker(), settings)
        alerts: list[dict] = []

        async def listen():
            async for alert in bus.subscribe(Topics.ALERTS):
                alerts.append(alert)

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        result = await agent._execute(make_request())
        await asyncio.sleep(0.1)
        task.cancel()

        assert result is not None and result.status is OrderStatus.FAILED
        assert alerts == []
        assert not agent.access_denied


class TestCheckCommand:
    async def test_dry_run_does_not_require_credentials(self, settings, capsys):
        """Em dry_run o broker é simulado: exigir chave travaria o uso normal."""
        from crypto_traders.cli import _check_credentials

        assert await _check_credentials(settings) is True
        assert "nao exigida em dry_run" in capsys.readouterr().out

    async def test_testnet_without_credentials_fails(self, settings, capsys):
        from crypto_traders.cli import _check_credentials
        from crypto_traders.domain.enums import TradingMode

        configured = settings.model_copy(update={"trading_mode": TradingMode.TESTNET})
        assert await _check_credentials(configured) is False
        assert "AUSENTE" in capsys.readouterr().out

    async def test_denied_credentials_fail_and_show_the_current_ip(
        self, settings, capsys, monkeypatch
    ):
        """Mostrar o IP de saída resolve o caso mais comum sem investigação."""
        import crypto_traders.cli as cli
        from crypto_traders.config import ExchangeCredentials
        from crypto_traders.domain.enums import TradingMode

        configured = settings.model_copy(
            update={
                "trading_mode": TradingMode.TESTNET,
                "binance": ExchangeCredentials(api_key="k", api_secret="s"),
            }
        )

        class Denied:
            def __init__(self, *args, **kwargs): ...

            async def fetch_balances(self):
                raise ApiAccessDenied("acesso negado: IP fora da whitelist")

            async def close(self): ...

        monkeypatch.setattr("crypto_traders.exchanges.CcxtExchange", Denied)

        async def fake_ip():
            print("\n    IP de saida desta maquina agora: 203.0.113.7")

        monkeypatch.setattr(cli, "_print_public_ip", fake_ip)

        assert await cli._check_credentials(configured) is False
        output = capsys.readouterr().out
        assert "ACESSO NEGADO" in output
        assert "203.0.113.7" in output

    async def test_valid_credentials_pass(self, settings, capsys, monkeypatch):
        import crypto_traders.cli as cli
        from crypto_traders.config import ExchangeCredentials
        from crypto_traders.domain.enums import TradingMode

        configured = settings.model_copy(
            update={
                "trading_mode": TradingMode.TESTNET,
                "binance": ExchangeCredentials(api_key="k", api_secret="s"),
            }
        )

        class Working:
            def __init__(self, *args, **kwargs): ...

            async def fetch_balances(self):
                return {"USDT": Decimal("1000"), "BTC": Decimal("0.01")}

            async def close(self): ...

        monkeypatch.setattr("crypto_traders.exchanges.CcxtExchange", Working)

        assert await cli._check_credentials(configured) is True
        output = capsys.readouterr().out
        assert "leitura de saldo OK" in output
        assert "BTC" in output and "USDT" in output


class TestAlertRouting:
    """O orquestrador é o caminho único entre `Topics.ALERTS` e o operador.

    Os agentes só publicam no bus e não conhecem o notificador — é isso que faz
    um alerta novo (como o de acesso negado) chegar ao Telegram sem que o agente
    precise saber que Telegram existe.
    """

    async def _run_listener(self, settings, alerts: list[dict]):
        from crypto_traders.agents.orchestrator import Orchestrator

        orchestrator = Orchestrator(settings)
        orchestrator.bus = InMemoryEventBus()
        await orchestrator.bus.start()

        sent: list[tuple[str, str]] = []

        class Capturing:
            async def send(self, title: str, body: str) -> None:
                sent.append((title, body))

        orchestrator.notifier = Capturing()
        task = asyncio.create_task(orchestrator._listen_alerts())
        await asyncio.sleep(0)
        for alert in alerts:
            await orchestrator.bus.publish(Topics.ALERTS, alert)
        await asyncio.sleep(0.1)
        task.cancel()
        return sent

    async def test_forwards_an_alert_to_the_notifier(self, settings):
        sent = await self._run_listener(
            settings,
            [{"type": "api_access_denied", "title": "Acesso negado", "message": "detalhe"}],
        )
        assert sent == [("Acesso negado", "detalhe")]

    async def test_falls_back_to_type_and_reason(self, settings):
        """Alertas antigos usavam `reason` em vez de `message`."""
        sent = await self._run_listener(settings, [{"type": "circuit_breaker", "reason": "queda"}])
        assert sent == [("circuit_breaker", "queda")]

    async def test_a_failing_notifier_does_not_kill_the_listener(self, settings):
        """Se o loop de alertas morresse, o sistema ficaria mudo em silêncio."""
        from crypto_traders.agents.orchestrator import Orchestrator

        orchestrator = Orchestrator(settings)
        orchestrator.bus = InMemoryEventBus()
        await orchestrator.bus.start()

        delivered: list[str] = []

        class Flaky:
            def __init__(self) -> None:
                self.calls = 0

            async def send(self, title: str, body: str) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("telegram fora do ar")
                delivered.append(title)

        orchestrator.notifier = Flaky()
        task = asyncio.create_task(orchestrator._listen_alerts())
        await asyncio.sleep(0)
        await orchestrator.bus.publish(Topics.ALERTS, {"type": "a", "title": "primeiro"})
        await asyncio.sleep(0.05)
        await orchestrator.bus.publish(Topics.ALERTS, {"type": "b", "title": "segundo"})
        await asyncio.sleep(0.05)
        task.cancel()

        assert delivered == ["segundo"], "o listener sobreviveu à falha de entrega"


@pytest.mark.parametrize(
    "raw",
    [
        '{"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}',
        "Request IP is not in the whitelist",
        '{"code":-2014,"msg":"API-key format invalid."}',
    ],
)
def test_message_always_identifies_exchange_and_operation(raw):
    message = _access_denied_message("binance", "create_order", raw)
    assert message.startswith("binance.create_order:")
