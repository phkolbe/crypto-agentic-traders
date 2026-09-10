"""Testes da API.

Cobrem o contrato que o dashboard consome e, principalmente, as travas das
rotas de controle: alterar limite de risco e apagar historico sao as duas
operacoes pela interface que podem causar estrago real.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from crypto_traders.api.app import create_app
from crypto_traders.api.routes import ws as ws_module
from crypto_traders.db.repositories import PortfolioSnapshotRepository, TradeRepository
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import Side, TradeOrigin
from crypto_traders.domain.models import PortfolioSnapshot


@pytest.fixture
async def client(settings):
    """API sem orquestrador: modo somente-leitura do historico."""
    app = create_app()
    app.state.settings = settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def seeded(settings):
    async with session_scope(settings) as session:
        trades = TradeRepository(session)
        await trades.record(
            executed_at=datetime.now(UTC) - timedelta(hours=3),
            exchange="binance", symbol="BTC/USDT", side=str(Side.BUY),
            quantity=Decimal("0.001"), price=Decimal("50000"),
            fee=Decimal("0.05"), fee_currency="USDT",
            origin=TradeOrigin.AGENT, strategy="ma_crossover", mode="dry_run",
        )
        await trades.record(
            executed_at=datetime.now(UTC) - timedelta(hours=1),
            exchange="binance", symbol="ETH/USDT", side=str(Side.SELL),
            quantity=Decimal("0.5"), price=Decimal("2500"),
            origin=TradeOrigin.MANUAL, mode="manual",
        )
        await PortfolioSnapshotRepository(session).save(
            PortfolioSnapshot(
                total_value=Decimal("1050"),
                cash_value=Decimal("1000"),
                positions_value=Decimal("50"),
                allocations={"USDT": 0.95, "BTC": 0.05},
            ),
            "dry_run",
        )
    return settings


class TestPortfolio:
    async def test_returns_null_before_any_snapshot(self, client):
        response = await client.get("/api/portfolio")
        assert response.status_code == 200
        assert response.json()["current"] is None

    async def test_returns_the_latest_snapshot(self, client, seeded):
        response = await client.get("/api/portfolio")
        body = response.json()["current"]
        assert Decimal(body["total_value"]) == Decimal("1050")
        assert body["mode"] == "dry_run"

    async def test_money_is_serialized_as_string(self, client, seeded):
        """Float em JSON viraria centavo errado no JavaScript."""
        body = await client.get("/api/portfolio")
        assert isinstance(body.json()["current"]["total_value"], str)

    async def test_decimals_survive_the_round_trip_exactly(self, client):
        """0.4 precisa voltar 0.4, nao 0.400000000000000022.

        O SQLite nao tem decimal nativo: sem o tipo `Money`, um `Numeric` vira
        float64 e o erro se propaga por PnL, custo medio e exportacao fiscal.
        """
        await client.post(
            "/api/trades/manual",
            json={
                "executed_at": datetime.now(UTC).isoformat(),
                "exchange": "binance", "symbol": "ETH/USDT", "side": "buy",
                "quantity": "0.4", "price": "2410.10",
            },
        )
        item = (await client.get("/api/trades")).json()["items"][0]
        assert Decimal(item["quantity"]) == Decimal("0.4")
        assert Decimal(item["price"]) == Decimal("2410.10")
        assert Decimal(item["notional"]) == Decimal("964.040")

    async def test_history_is_chronological(self, client, seeded):
        response = await client.get("/api/portfolio/history?days=7")
        assert response.status_code == 200
        assert len(response.json()) >= 1


class TestTrades:
    async def test_lists_agent_and_manual_together(self, client, seeded):
        body = (await client.get("/api/trades")).json()
        assert body["total"] == 2
        assert {item["origin"] for item in body["items"]} == {"agent", "manual"}

    async def test_filters_by_origin(self, client, seeded):
        body = (await client.get("/api/trades?origin=manual")).json()
        assert body["total"] == 1
        assert body["items"][0]["symbol"] == "ETH/USDT"

    async def test_filters_by_symbol_and_side(self, client, seeded):
        body = (await client.get("/api/trades?symbol=BTC/USDT&side=buy")).json()
        assert body["total"] == 1

    async def test_records_a_manual_trade(self, client):
        payload = {
            "executed_at": datetime.now(UTC).isoformat(),
            "exchange": "binance",
            "symbol": "SOL/USDT",
            "side": "buy",
            "quantity": "2.5",
            "price": "150.00",
            "fee": "0.30",
            "fee_currency": "USDT",
            "notes": "comprado pelo app",
        }
        response = await client.post("/api/trades/manual", json=payload)
        assert response.status_code == 201
        assert response.json()["origin"] == "manual"

        listed = (await client.get("/api/trades?origin=manual")).json()
        assert listed["total"] == 1

    async def test_rejects_a_malformed_symbol(self, client):
        response = await client.post(
            "/api/trades/manual",
            json={
                "executed_at": datetime.now(UTC).isoformat(),
                "exchange": "binance", "symbol": "bitcoin", "side": "buy",
                "quantity": "1", "price": "100",
            },
        )
        assert response.status_code == 422

    async def test_rejects_non_positive_quantity(self, client):
        response = await client.post(
            "/api/trades/manual",
            json={
                "executed_at": datetime.now(UTC).isoformat(),
                "exchange": "binance", "symbol": "BTC/USDT", "side": "buy",
                "quantity": "0", "price": "100",
            },
        )
        assert response.status_code == 422

    async def test_manual_trade_can_be_deleted(self, client, seeded):
        manual = (await client.get("/api/trades?origin=manual")).json()["items"][0]
        assert (await client.delete(f"/api/trades/{manual['id']}")).status_code == 204
        assert (await client.get("/api/trades?origin=manual")).json()["total"] == 0

    async def test_agent_trade_cannot_be_deleted(self, client, seeded):
        """Apagar o que o sistema fez destruiria a auditoria."""
        agent_trade = (await client.get("/api/trades?origin=agent")).json()["items"][0]
        response = await client.delete(f"/api/trades/{agent_trade['id']}")
        assert response.status_code == 409
        assert "auditoria" in response.json()["detail"]

    async def test_export_is_csv_with_the_fiscal_columns(self, client, seeded):
        response = await client.get("/api/trades/export")
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        header = response.text.splitlines()[0]
        for column in ("taxa", "origem", "modo", "pnl_realizado"):
            assert column in header

    async def test_export_marks_simulated_rows(self, client, seeded):
        """Simulacao nunca pode ser confundida com dinheiro real no CSV."""
        text = (await client.get("/api/trades/export")).text
        assert "dry_run" in text


class TestControlWithoutAgents:
    async def test_control_routes_report_agents_are_down(self, client):
        """A API sobe sozinha para consultar historico; controle exige agentes."""
        for path in ("/api/health", "/api/risk/config"):
            assert (await client.get(path)).status_code == 503
        assert (await client.post("/api/agents/strategy/pause")).status_code == 503

    async def test_read_only_routes_still_work(self, client):
        for path in ("/api/trades", "/api/signals", "/api/orders", "/api/risk/events",
                     "/api/audit", "/api/strategies"):
            assert (await client.get(path)).status_code == 200


class TestStrategies:
    async def test_lists_strategies_marking_the_active_ones(self, client):
        body = (await client.get("/api/strategies")).json()
        names = {item["name"] for item in body}
        assert {"ma_crossover", "rsi_reversion", "macd_trend", "bollinger_reversion"} <= names
        assert any(item["active"] for item in body)


class TestRiskControl:
    """Rotas de controle com o orquestrador presente (agentes nao iniciados)."""

    @pytest.fixture
    async def controlled(self, settings):
        from crypto_traders.agents.orchestrator import Orchestrator
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        orchestrator = Orchestrator(settings)
        orchestrator.bus = InMemoryEventBus()
        await orchestrator.bus.start()
        orchestrator.risk_manager = RiskManagerAgent(orchestrator.bus, settings)

        app = create_app()
        app.state.settings = settings
        app.state.orchestrator = orchestrator
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, orchestrator

    async def test_reads_the_current_limits(self, controlled):
        http, _ = controlled
        body = (await http.get("/api/risk/config")).json()
        assert body["max_order_notional"] == "100"
        assert body["circuit_breaker_active"] is False

    async def test_update_requires_explicit_confirmation(self, controlled):
        """Afrouxar um limite nao pode ser efeito colateral de um clique."""
        http, _ = controlled
        response = await http.put("/api/risk/config", json={"max_order_notional": "500"})
        assert response.status_code == 400
        assert "confirm=true" in response.json()["detail"]

    async def test_update_applies_with_confirmation(self, controlled):
        http, orchestrator = controlled
        response = await http.put(
            "/api/risk/config", json={"max_order_notional": "25", "confirm": True}
        )
        assert response.status_code == 200
        assert orchestrator.risk_manager.limits.max_order_notional == Decimal("25")

    async def test_incoherent_limits_are_rejected(self, controlled):
        """Take-profit abaixo do stop tem esperanca matematica negativa."""
        http, _ = controlled
        response = await http.put(
            "/api/risk/config", json={"take_profit_pct": 0.01, "confirm": True}
        )
        assert response.status_code == 422

    async def test_unknown_field_is_refused(self, controlled):
        http, _ = controlled
        response = await http.put(
            "/api/risk/config", json={"campo_inexistente": 1, "confirm": True}
        )
        assert response.status_code == 422

    async def test_reset_fails_when_breaker_is_not_active(self, controlled):
        http, _ = controlled
        response = await http.post(
            "/api/risk/circuit-breaker/reset", json={"confirm": True}
        )
        assert response.status_code == 409

    async def test_limit_change_shows_up_in_the_audit_log(self, controlled):
        http, _ = controlled
        await http.put("/api/risk/config", json={"cooldown_seconds": 300, "confirm": True})
        entries = (await http.get("/api/audit")).json()
        assert any(entry["action"] == "risk_limits_updated" for entry in entries)


class TestTradingConfigRoutes:
    """A tela de Configuracoes e o unico lugar de editar negocio.

    Se o `.env` deixou de aceitar essas variaveis, esta rota precisa funcionar --
    caso contrario a separacao teria apenas tirado o controle do usuario.
    """

    @pytest.fixture
    async def controlled(self, settings):
        from crypto_traders.agents.market_data import MarketDataAgent
        from crypto_traders.agents.orchestrator import Orchestrator
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.agents.strategy import StrategyAgent
        from crypto_traders.bus import InMemoryEventBus
        from crypto_traders.strategies import build_strategies

        orchestrator = Orchestrator(settings)
        orchestrator.bus = InMemoryEventBus()
        await orchestrator.bus.start()
        orchestrator.risk_manager = RiskManagerAgent(orchestrator.bus, settings)
        orchestrator.strategy = StrategyAgent(
            orchestrator.bus, build_strategies(settings.trading.strategies), settings
        )
        orchestrator.market_data = MarketDataAgent(orchestrator.bus, None, settings)

        app = create_app()
        app.state.settings = settings
        app.state.orchestrator = orchestrator
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, orchestrator

    async def test_reads_the_current_configuration(self, controlled):
        http, _ = controlled
        body = (await http.get("/api/trading/config")).json()
        assert body["symbols"] == ["BTC/USDT"]
        assert body["discovery_enabled"] is False
        assert "ma_crossover" in body["available_strategies"]

    async def test_update_requires_explicit_confirmation(self, controlled):
        """Trocar pares muda o que o sistema negocia com dinheiro real."""
        http, _ = controlled
        response = await http.put("/api/trading/config", json={"timeframe": "4h"})
        assert response.status_code == 400
        assert "confirm=true" in response.json()["detail"]

    async def test_update_applies_to_the_running_process(self, controlled):
        http, orchestrator = controlled
        response = await http.put(
            "/api/trading/config", json={"timeframe": "4h", "confirm": True}
        )
        assert response.status_code == 200
        assert orchestrator.settings.trading.timeframe == "4h"

    async def test_changing_strategies_swaps_them_without_restart(self, controlled):
        http, orchestrator = controlled
        response = await http.put(
            "/api/trading/config",
            json={"strategies": ["macd_trend", "rsi_reversion"], "confirm": True},
        )
        assert response.status_code == 200
        assert orchestrator.strategy.strategy_names == ["macd_trend", "rsi_reversion"]

    async def test_unknown_strategy_is_refused(self, controlled):
        http, _ = controlled
        response = await http.put(
            "/api/trading/config", json={"strategies": ["nao_existe"], "confirm": True}
        )
        assert response.status_code == 422
        assert "inexistente" in response.json()["detail"]

    async def test_empty_symbols_turns_on_discovery(self, controlled):
        """Lista vazia e um valor com significado, nao ausencia de valor."""
        http, orchestrator = controlled
        response = await http.put(
            "/api/trading/config", json={"symbols": [], "confirm": True}
        )
        assert response.status_code == 200
        assert response.json()["discovery_enabled"] is True
        assert orchestrator.settings.trading.discovery_enabled is True

    async def test_unknown_field_is_refused(self, controlled):
        http, _ = controlled
        response = await http.put(
            "/api/trading/config", json={"campo_inexistente": 1, "confirm": True}
        )
        assert response.status_code == 422

    async def test_empty_payload_is_refused(self, controlled):
        http, _ = controlled
        response = await http.put("/api/trading/config", json={"confirm": True})
        assert response.status_code == 400


class TestMvrvIsEditable:
    """O filtro de regime nao era editavel pela interface -- so pelo `.env`.

    Com negocio morando no banco, um limite sem tela seria um limite sem dono.
    """

    async def test_risk_config_exposes_the_filter(self, settings):
        from crypto_traders.agents.orchestrator import Orchestrator
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        orchestrator = Orchestrator(settings)
        orchestrator.bus = InMemoryEventBus()
        await orchestrator.bus.start()
        orchestrator.risk_manager = RiskManagerAgent(orchestrator.bus, settings)

        app = create_app()
        app.state.settings = settings
        app.state.orchestrator = orchestrator
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            assert (await http.get("/api/risk/config")).json()["mvrv_max_percentile"] == 1.0

            response = await http.put(
                "/api/risk/config", json={"mvrv_max_percentile": 0.4, "confirm": True}
            )
            assert response.status_code == 200
            assert response.json()["mvrv_max_percentile"] == 0.4
            assert orchestrator.risk_manager.limits.mvrv_max_percentile == 0.4
            # `settings.risk` alimenta o check e o backtest: precisa acompanhar.
            assert settings.risk.mvrv_max_percentile == 0.4


# ---------------------------------------------------------------------------
#: Nomes de campo que carregam dinheiro. Em JSON, todos precisam ser string
#: (D7): `0.1 + 0.2` em JavaScript da `0.30000000000000004`, e numa tela de
#: dinheiro isso e centavo errado.
CAMPOS_DE_DINHEIRO = frozenset(
    {
        "open", "high", "low", "close", "volume", "price", "reference_price",
        "quantity", "notional", "stop_loss", "take_profit", "filled_quantity",
        "average_price", "fee", "approved_quantity", "approved_notional",
        "total_value", "cash_value", "positions_value", "realized_pnl",
        "unrealized_pnl", "market_value", "current_price", "initial_balance",
        "final_value", "value", "max_order_notional", "min_order_notional",
        "authorized_capital", "unauthorized_value", "paper_initial_balance",
        "discovery_min_quote_volume_24h",
    }
)


def dinheiro_em_number(corpo, caminho: str = "") -> list[str]:
    """Percorre um JSON inteiro e devolve todo campo de dinheiro que veio number.

    Varre recursivamente de proposito: o defeito de serializacao nao aparece no
    campo obvio do topo, aparece dentro de uma lista aninhada que ninguem olhou
    -- foi assim que `equity_curve[].value` do backtest passou batido.
    """
    if isinstance(corpo, dict):
        return [
            erro
            for chave, valor in corpo.items()
            for erro in dinheiro_em_number(valor, f"{caminho}.{chave}")
        ]
    if isinstance(corpo, list):
        return [
            erro
            for indice, valor in enumerate(corpo)
            for erro in dinheiro_em_number(valor, f"{caminho}[{indice}]")
        ]
    campo = caminho.split(".")[-1].split("[")[0]
    if campo in CAMPOS_DE_DINHEIRO and type(corpo) in (int, float):
        return [f"{caminho} = {corpo!r} ({type(corpo).__name__})"]
    return []


class CorpoComNomeQualquer(BaseModel):
    """Schema de entrada que NAO termina em `In`.

    Existe so para o teste que prova que a varredura de ambiente acha um corpo
    pelo caminho (`requestBody`) e nao pelo nome da classe. Mora no modulo
    porque `from __future__ import annotations` transforma uma classe local em
    ForwardRef que o FastAPI nao resolve.
    """

    trading_mode: str


def schemas_de_requestbody(openapi: dict) -> dict[str, dict]:
    """Todo schema que alguma rota aceita no CORPO, alcancado pelos `$ref`.

    Filtrar por `nome.endswith("In")`, como esta varredura fazia, deixava um
    ponto cego: um schema de entrada batizado de outra forma escapava da trava
    de ambiente sem nada quebrar. Aqui o criterio e o caminho -- se o schema e
    alcancavel por um `requestBody`, ele e entrada, chame-se como quiser.
    """
    componentes = (openapi.get("components") or {}).get("schemas") or {}
    pendentes: list = []
    for caminho in (openapi.get("paths") or {}).values():
        for operacao in caminho.values():
            if not isinstance(operacao, dict):
                continue
            corpo = operacao.get("requestBody") or {}
            for media in (corpo.get("content") or {}).values():
                if isinstance(media, dict) and media.get("schema"):
                    pendentes.append(media["schema"])

    encontrados: dict[str, dict] = {}
    while pendentes:
        no = pendentes.pop()
        if isinstance(no, list):
            pendentes.extend(no)
            continue
        if not isinstance(no, dict):
            continue
        referencia = no.get("$ref")
        if referencia:
            nome = referencia.rsplit("/", 1)[-1]
            if nome in encontrados or nome not in componentes:
                continue
            encontrados[nome] = componentes[nome]
            # Schema aninhado tambem e entrada: seguir o `$ref` para dentro.
            pendentes.append(componentes[nome])
            continue
        pendentes.extend(no.values())
    return encontrados


def _serie_oscilante(periodos: int = 300) -> list[Decimal]:
    """Serie com cruzamentos suficientes para o backtest gerar operacoes."""
    return [
        Decimal(str(round(50000 + 4000 * math.sin(indice / 9) + indice * 4, 2)))
        for indice in range(periodos)
    ]


def _snapshot(valor: str) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        total_value=Decimal(valor), cash_value=Decimal(valor), positions_value=Decimal(0)
    )


class FakeSocket:
    """WebSocket de mentira, com os tres comportamentos que importam.

    `travado` e o caso real, e nao o exotico: a maquina suspendeu, o celular
    perdeu sinal, a aba hibernou. O socket nao da erro -- ele simplesmente para
    de aceitar bytes, e o `send_text` fica pendurado para sempre.
    """

    def __init__(self, *, travado: bool = False, quebra: bool = False) -> None:
        self.travado = travado
        self.quebra = quebra
        self.recebidas: list[str] = []
        self.fechado = False

    async def accept(self) -> None:
        return None

    async def close(self, code: int = 1000) -> None:
        self.fechado = True

    async def send_text(self, texto: str) -> None:
        if self.quebra:
            raise RuntimeError("socket fechado sem handshake de saida")
        if self.travado:
            await asyncio.Event().wait()
        self.recebidas.append(texto)


class TestBacktestMoneyIsString:
    """O backtest era a rota que quebrava o D7, e quebrava calada.

    `BacktestOut.summary` era `dict[str, Any]` e a rota convertia tudo com
    `float(...)`: medido, `initial_balance: 1000.0`, `final_value: 1000.0` e cada
    ponto de `equity_curve` sairam como number. Um `dict[str, Any]` nao da ao
    `MoneyModel` como saber que aquilo e dinheiro, entao nao havia onde a regra
    ser aplicada nem onde um teste quebrar.
    """

    @pytest.fixture
    async def http(self, settings, monkeypatch):
        from crypto_traders.domain.enums import ExchangeName
        from crypto_traders.domain.models import Candle

        inicio = datetime(2026, 1, 1, tzinfo=UTC)
        candles = [
            Candle(
                exchange=ExchangeName.BINANCE,
                symbol="BTC/USDT",
                timeframe="15m",
                open_time=inicio + timedelta(minutes=15 * indice),
                open=preco,
                high=preco * Decimal("1.004"),
                low=preco * Decimal("0.996"),
                close=preco,
                volume=Decimal("10"),
            )
            for indice, preco in enumerate(_serie_oscilante())
        ]

        class FonteFalsa:
            async def fetch_candles(self, symbol, timeframe, limit):
                return candles[-limit:]

            async def close(self):
                return None

        monkeypatch.setattr(
            "crypto_traders.api.routes.backtest.build_market_data_source",
            lambda _exchange: FonteFalsa(),
        )
        app = create_app()
        app.state.settings = settings
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    async def _rodar(self, http, initial_balance: str = "1000"):
        response = await http.post(
            "/api/backtest",
            json={
                "symbol": "BTC/USDT",
                "strategy": "ma_crossover",
                "timeframe": "15m",
                "days": 5,
                "initial_balance": initial_balance,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def test_the_simulation_actually_traded(self, http):
        """Sem operacao nenhuma os testes abaixo nao olhariam `trades[]`."""
        body = await self._rodar(http)
        assert body["trades"], "a serie precisa gerar operacoes para haver o que medir"
        assert body["summary"]["trades"] > 0

    async def test_no_money_field_is_a_json_number(self, http):
        body = await self._rodar(http)
        erros = dinheiro_em_number(body, "/api/backtest")
        assert erros == [], "dinheiro em number no JSON: " + "; ".join(erros)

    async def test_every_trade_field_is_a_string(self, http):
        body = await self._rodar(http)
        trade = body["trades"][0]
        for campo in ("quantity", "price", "notional", "fee"):
            assert isinstance(trade[campo], str), f"{campo} veio {type(trade[campo]).__name__}"

    async def test_the_equity_curve_carries_strings(self, http):
        body = await self._rodar(http)
        assert body["equity_curve"]
        assert all(isinstance(ponto["value"], str) for ponto in body["equity_curve"])

    async def test_the_balance_returns_exactly_as_sent(self, http):
        """O ponto do D7 nao e o caso bonito: e nao existir a conversao que degrada.

        `float(Decimal("1000.123456789012345678"))` perde os ultimos digitos, e
        era exatamente por ali que o valor passava antes.
        """
        exato = "1000.123456789012345678"
        assert float(Decimal(exato)) != Decimal(exato), "o float precisa mesmo degradar"

        devolvido = (await self._rodar(http, exato))["summary"]["initial_balance"]
        assert isinstance(devolvido, str)
        assert Decimal(devolvido) == Decimal(exato)


class TestWebSocketMoneyIsString:
    """Todo evento do WebSocket, nao apenas os obvios.

    O dashboard aprende que dinheiro se moveu por aqui. Um campo em number
    chegaria ao navegador com residuo de float e sem nada indicando isso.
    """

    @staticmethod
    def _payloads() -> dict:
        from crypto_traders.domain.enums import (
            ExchangeName,
            OrderStatus,
            RiskDecision,
            SignalDirection,
        )
        from crypto_traders.domain.models import (
            IndicatorSnapshot,
            OrderResult,
            Position,
            RiskAssessment,
            Signal,
        )

        sinal = Signal(
            exchange=ExchangeName.BINANCE,
            symbol="BTC/USDC",
            timeframe="1d",
            strategy="ma_crossover",
            direction=SignalDirection.LONG,
            confidence=0.6,
            reason="cruzamento",
            reference_price=Decimal("0.1"),
            indicators=IndicatorSnapshot(values={"ma_fast": 0.1}),
        )
        return {
            "signal": sinal,
            "risk_assessment": RiskAssessment(
                signal_id=sinal.id,
                decision=RiskDecision.APPROVED,
                approved_quantity=Decimal("0.3"),
                approved_notional=Decimal("0.1"),
                stop_loss=Decimal("0.097"),
                take_profit=Decimal("0.106"),
                snapshot={"total_value": "29.29"},
            ),
            "order_result": OrderResult(
                order_request_id="r1",
                client_order_id="cat-1",
                exchange_order_id="x1",
                status=OrderStatus.FILLED,
                filled_quantity=Decimal("0.3"),
                average_price=Decimal("0.1"),
                fee=Decimal("0.0001"),
            ),
            "portfolio": PortfolioSnapshot(
                total_value=Decimal("29.29"),
                cash_value=Decimal("0.1"),
                positions_value=Decimal("29.19"),
                realized_pnl=Decimal("0.2"),
                unrealized_pnl=Decimal("-0.1"),
                allocations={"BTC": 0.9},
                positions=[
                    Position(
                        exchange=ExchangeName.BINANCE,
                        asset="BTC",
                        quantity=Decimal("0.001"),
                        average_price=Decimal("50000.1"),
                        current_price=Decimal("51000.2"),
                    )
                ],
            ),
            "alert": {"type": "circuit_breaker", "title": "trava", "reason": "perda diaria"},
        }

    async def test_every_streamed_event_sends_money_as_string(self):
        gerente = ws_module.ConnectionManager()
        cliente = FakeSocket()
        await gerente.connect(cliente)
        for evento, payload in self._payloads().items():
            await gerente.broadcast(evento, payload)
        await asyncio.sleep(0.05)

        assert len(cliente.recebidas) == len(self._payloads())
        for texto in cliente.recebidas:
            mensagem = json.loads(texto)
            erros = dinheiro_em_number(mensagem["data"], mensagem["event"])
            assert erros == [], "dinheiro em number no WebSocket: " + "; ".join(erros)

    def test_covers_every_topic_the_dashboard_listens_to(self):
        """Se um topico novo entrar no espelho, o teste acima tem que ve-lo."""
        assert set(ws_module.STREAMED_TOPICS.values()) == set(self._payloads())


class TestWebSocketDoesNotStallOnOneClient:
    """Um consumidor travado nao pode travar ninguem -- nem os outros
    navegadores, nem o assinante do event bus.

    Medido antes da correcao: o fan-out enviava em serie e esperava cada
    `send_text`. Com UM cliente travado, `broadcast` nunca retornava; o relay do
    bus parava de consumir; a fila do bus enchia e comecava a descartar. Trinta
    eventos publicados chegaram como UM ao cliente saudavel, e nada no log dizia
    que os outros 29 tinham sido perdidos.
    """

    async def test_a_stuck_client_does_not_delay_the_broadcast(self):
        gerente = ws_module.ConnectionManager()
        travado, saudavel = FakeSocket(travado=True), FakeSocket()
        await gerente.connect(travado)
        await gerente.connect(saudavel)

        # Sem prazo aqui o teste nao falharia: ele PENDURARIA.
        await asyncio.wait_for(gerente.broadcast("portfolio", _snapshot("1")), timeout=1.0)
        await asyncio.sleep(0.05)

        assert saudavel.recebidas, "o cliente saudavel recebeu o evento"
        assert travado.recebidas == []

    async def test_a_stuck_client_does_not_starve_the_bus_relay(self):
        """O caso do mandato: o consumidor travado nao pode travar o event bus."""
        from crypto_traders.bus import InMemoryEventBus, Topics

        bus = InMemoryEventBus(max_queue_size=10)
        await bus.start()
        gerente = ws_module.ConnectionManager()
        travado, saudavel = FakeSocket(travado=True), FakeSocket()
        await gerente.connect(travado)
        await gerente.connect(saudavel)

        async def relay() -> None:
            async for payload in bus.subscribe(Topics.PORTFOLIO_SNAPSHOTS):
                await gerente.broadcast("portfolio", payload)

        tarefa = asyncio.create_task(relay())
        await asyncio.sleep(0)
        for indice in range(30):
            await bus.publish(Topics.PORTFOLIO_SNAPSHOTS, _snapshot(str(indice)))
            await asyncio.sleep(0)
        await asyncio.sleep(0.1)
        tarefa.cancel()

        assert len(saudavel.recebidas) == 30, (
            f"o cliente saudavel recebeu {len(saudavel.recebidas)} de 30; "
            "um cliente travado esta fazendo o relay do bus perder eventos"
        )

    async def test_a_client_that_stops_reading_is_dropped_not_buffered_forever(self):
        gerente = ws_module.ConnectionManager()
        travado, saudavel = FakeSocket(travado=True), FakeSocket()
        await gerente.connect(travado)
        await gerente.connect(saudavel)

        total = ws_module.MAX_PENDING_PER_CLIENT + 10
        for indice in range(total):
            await gerente.broadcast("portfolio", _snapshot(str(indice)))
        await asyncio.sleep(0.1)

        assert gerente.count == 1, "o cliente que parou de ler foi derrubado"
        assert travado.fechado, "e o socket dele foi fechado para o navegador reconectar"
        assert len(saudavel.recebidas) == total, (
            f"o cliente saudavel recebeu {len(saudavel.recebidas)} de {total}; "
            "a fila dele nao pode estourar por causa do vizinho"
        )

    async def test_a_client_that_disconnects_mid_send_is_removed(self):
        gerente = ws_module.ConnectionManager()
        quebrado, saudavel = FakeSocket(quebra=True), FakeSocket()
        await gerente.connect(quebrado)
        await gerente.connect(saudavel)

        await gerente.broadcast("portfolio", _snapshot("1"))
        await asyncio.sleep(0.05)
        assert gerente.count == 1

        await gerente.broadcast("portfolio", _snapshot("2"))
        await asyncio.sleep(0.05)
        assert len(saudavel.recebidas) == 2, "a queda de um nao interrompe o outro"

    async def test_many_slow_clients_all_get_everything(self):
        """Cinquenta abas lentas nao podem virar meio segundo de fan-out."""

        class Lento(FakeSocket):
            async def send_text(self, texto: str) -> None:
                await asyncio.sleep(0.002)
                self.recebidas.append(texto)

        gerente = ws_module.ConnectionManager()
        clientes = [Lento() for _ in range(50)]
        for cliente in clientes:
            await gerente.connect(cliente)

        inicio = time.perf_counter()
        for indice in range(20):
            await gerente.broadcast("portfolio", _snapshot(str(indice)))
        decorrido = time.perf_counter() - inicio

        assert decorrido < 0.5, f"o fan-out levou {decorrido:.3f}s esperando pela rede"
        await asyncio.sleep(3)
        assert min(len(cliente.recebidas) for cliente in clientes) == 20
        assert gerente.count == 50


class TestRiskConfigRefusesContradictoryRequests:
    """Um pedido que se contradiz nao pode ser resolvido para o lado permissivo.

    Medido antes da correcao: `{"authorized_capital": "50",
    "clear_authorized_capital": true}` devolvia 200 e removia o portao de
    autorizacao INTEIRO. Quem pediu um teto de 50 recebeu "todo o patrimonio,
    inclusive depositos futuros, liberado sem novo aval" -- porque o `clear_*`
    era aplicado depois e sobrescrevia o valor enviado. Diante de duvida o
    sistema recusa; ele nao escolhe por conta o lado que arrisca mais.
    """

    @pytest.fixture
    async def controlled(self, settings):
        from crypto_traders.agents.orchestrator import Orchestrator
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        orchestrator = Orchestrator(settings)
        orchestrator.bus = InMemoryEventBus()
        await orchestrator.bus.start()
        orchestrator.risk_manager = RiskManagerAgent(orchestrator.bus, settings)

        app = create_app()
        app.state.settings = settings
        app.state.orchestrator = orchestrator
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, orchestrator

    @pytest.mark.parametrize(
        ("campo", "valor"),
        [
            ("authorized_capital", "50"),
            ("max_order_notional", "500"),
            ("max_open_positions", 5),
        ],
    )
    async def test_value_plus_clear_is_refused(self, controlled, campo, valor):
        http, orchestrator = controlled
        antes = getattr(orchestrator.risk_manager.configured_limits, campo)

        response = await http.put(
            "/api/risk/config",
            json={campo: valor, f"clear_{campo}": True, "confirm": True},
        )

        assert response.status_code == 400, response.text
        assert "contraditorio" in response.json()["detail"]
        assert (
            getattr(orchestrator.risk_manager.configured_limits, campo) == antes
        ), "um pedido recusado nao pode ter alterado nada"

    async def test_the_capital_gate_is_not_removed_by_a_contradictory_request(
        self, controlled
    ):
        """O campo em que errar LIBERA dinheiro merece a verificacao propria."""
        http, orchestrator = controlled
        await http.put("/api/risk/config", json={"authorized_capital": "20", "confirm": True})
        assert orchestrator.risk_manager.configured_limits.authorized_capital == Decimal("20")

        response = await http.put(
            "/api/risk/config",
            json={"authorized_capital": "50", "clear_authorized_capital": True, "confirm": True},
        )

        assert response.status_code == 400
        limites = orchestrator.risk_manager.configured_limits
        assert limites.authorized_capital == Decimal("20"), (
            "o portao de autorizacao continua de pe depois do pedido recusado"
        )

    async def test_clearing_alone_still_works(self, controlled):
        """A recusa e do pedido contraditorio, nao do gesto de remover."""
        http, orchestrator = controlled
        response = await http.put(
            "/api/risk/config", json={"clear_max_order_notional": True, "confirm": True}
        )
        assert response.status_code == 200, response.text
        assert orchestrator.risk_manager.configured_limits.max_order_notional is None

    async def test_setting_alone_still_works(self, controlled):
        http, orchestrator = controlled
        response = await http.put(
            "/api/risk/config", json={"max_order_notional": "25", "confirm": True}
        )
        assert response.status_code == 200, response.text
        assert orchestrator.risk_manager.configured_limits.max_order_notional == Decimal("25")


class TestRealMoneyCannotBeTurnedOnByClicking:
    """`TRADING_MODE` e ambiente por decisao explicita (D15/D25), e ligar
    dinheiro real exige dupla confirmacao fora da interface (D6).

    Nao basta que hoje nao exista rota para isso: o teste precisa quebrar no dia
    em que alguem acrescentar o campo a um schema de entrada.
    """

    CHAVES_DE_AMBIENTE = frozenset(
        {
            "trading_mode",
            "live_trading_confirmed",
            "api_key",
            "api_secret",
            "binance",
            "coinbase",
            "database_url",
            "api_host",
            "api_port",
            "smtp_password",
            "whatsapp_token",
            "cors_origins",
        }
    )

    @pytest.fixture
    async def controlled(self, settings):
        from crypto_traders.agents.market_data import MarketDataAgent
        from crypto_traders.agents.orchestrator import Orchestrator
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.agents.strategy import StrategyAgent
        from crypto_traders.bus import InMemoryEventBus
        from crypto_traders.strategies import build_strategies

        orchestrator = Orchestrator(settings)
        orchestrator.bus = InMemoryEventBus()
        await orchestrator.bus.start()
        orchestrator.risk_manager = RiskManagerAgent(orchestrator.bus, settings)
        orchestrator.strategy = StrategyAgent(
            orchestrator.bus, build_strategies(settings.trading.strategies), settings
        )
        orchestrator.market_data = MarketDataAgent(orchestrator.bus, None, settings)

        app = create_app()
        app.state.settings = settings
        app.state.orchestrator = orchestrator
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, orchestrator, app

    @pytest.mark.parametrize("rota", ["/api/risk/config", "/api/trading/config"])
    @pytest.mark.parametrize("chave", ["trading_mode", "live_trading_confirmed"])
    async def test_the_control_routes_refuse_the_environment_keys(self, controlled, rota, chave):
        http, orchestrator, _ = controlled
        modo_antes = orchestrator.settings.trading_mode

        response = await http.put(rota, json={chave: "live", "confirm": True})

        assert response.status_code == 422, response.text
        assert orchestrator.settings.trading_mode == modo_antes

    async def test_no_request_body_in_the_whole_api_accepts_an_environment_key(self, controlled):
        """Varre o OpenAPI: nenhuma rota tem como receber ambiente pelo corpo."""
        _, _, app = controlled
        entradas = schemas_de_requestbody(app.openapi())
        assert entradas, "o OpenAPI precisa expor os schemas de entrada"
        # As quatro rotas que aceitam corpo hoje. Se uma sair da lista, a
        # varredura passou a olhar menos do que existe.
        assert {
            "RiskConfigIn",
            "TradingConfigIn",
            "CapitalAuthorizationIn",
            "CircuitBreakerResetIn",
        } <= set(entradas), sorted(entradas)

        achados = [
            f"{nome}.{campo}"
            for nome, corpo in entradas.items()
            for campo in (corpo.get("properties") or {})
            if campo in self.CHAVES_DE_AMBIENTE
        ]
        assert achados == [], f"campo de ambiente em schema de entrada: {achados}"

    async def test_the_sweep_finds_a_body_schema_with_any_name(self, controlled):
        """A varredura por nome (`endswith("In")`) tinha um ponto cego.

        Um schema de entrada batizado de outra forma escapava da trava, e a
        trava existe justamente para o dia em que alguem acrescentar um campo de
        ambiente a um corpo novo. Aqui a rota falsa prova que a varredura acha o
        campo pelo CAMINHO (requestBody), e nao pelo nome da classe.
        """
        from fastapi import FastAPI

        falsa = FastAPI()

        @falsa.post("/api/qualquer")
        async def _rota(payload: CorpoComNomeQualquer) -> dict:  # pragma: no cover
            return {}

        entradas = schemas_de_requestbody(falsa.openapi())

        assert "CorpoComNomeQualquer" in entradas, sorted(entradas)
        assert not any(nome.endswith("In") for nome in entradas), (
            "o filtro antigo por nome nao veria este schema"
        )
        achados = [
            campo
            for corpo in entradas.values()
            for campo in (corpo.get("properties") or {})
            if campo in self.CHAVES_DE_AMBIENTE
        ]
        assert achados == ["trading_mode"], achados

    async def test_the_mode_reported_by_health_is_read_only(self, controlled):
        """A interface mostra o modo; mostrar nao pode virar poder de mudar."""
        http, orchestrator, _ = controlled
        modo = (await http.get("/api/health")).json()["mode"]
        assert modo == str(orchestrator.settings.trading_mode)
        assert modo != "live"

        for metodo in ("PUT", "POST", "PATCH", "DELETE"):
            response = await http.request(metodo, "/api/health", json={"mode": "live"})
            assert response.status_code == 405, f"{metodo} /api/health devia ser 405"
        assert orchestrator.settings.trading_mode == modo


class TestNoRouteEverReturnsASecret:
    """A tela de notificacoes diz se o segredo existe, nunca qual e.

    O teste procura o VALOR do segredo, nao a palavra "password": listar o nome
    da variavel que falta e o proposito da rota -- e o que diz ao operador o que
    configurar. Vazar o conteudo e o defeito.
    """

    #: Valores plantados no ambiente de teste. Nenhum pode sair pela API.
    SEGREDOS = ("senha-smtp-em-claro-xyz", "token-whatsapp-em-claro-xyz")

    @pytest.fixture
    async def com_segredos(self, settings):
        from pydantic import SecretStr

        configurado = settings.model_copy(
            update={
                "smtp_host": "smtp.exemplo.com",
                "smtp_username": "agente@exemplo.com",
                "smtp_from": "agente@exemplo.com",
                "smtp_password": SecretStr(self.SEGREDOS[0]),
                "whatsapp_phone_number_id": "1234567890",
                "whatsapp_template_name": "alerta",
                "whatsapp_access_token": SecretStr(self.SEGREDOS[1]),
            }
        )
        app = create_app()
        app.state.settings = configurado
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http

    async def test_the_notification_route_says_configured_without_the_value(
        self, com_segredos
    ):
        response = await com_segredos.get("/api/notifications/config")
        assert response.status_code == 200
        corpo = response.json()

        assert corpo["email"]["configured"] is True, "o canal esta configurado"
        assert corpo["whatsapp"]["configured"] is True
        for segredo in self.SEGREDOS:
            assert segredo not in response.text, "a rota devolveu o segredo em claro"
        assert set(corpo["email"]) == {"configured", "missing_settings"}

    async def test_no_read_route_leaks_the_secret(self, com_segredos):
        """Varre as rotas de leitura: o segredo nao sai por nenhuma delas."""
        rotas = (
            "/api/notifications/config",
            "/api/trades",
            "/api/signals",
            "/api/orders",
            "/api/risk/events",
            "/api/audit",
            "/api/strategies",
            "/api/portfolio",
            "/openapi.json",
        )
        for rota in rotas:
            response = await com_segredos.get(rota)
            assert response.status_code == 200, rota
            for segredo in self.SEGREDOS:
                assert segredo not in response.text, f"{rota} devolveu o segredo"


# ---------------------------------------------------------------------------
@pytest.fixture
async def orquestrado(settings):
    """API com o orquestrador presente e os agentes construidos (nao iniciados).

    Construir `strategy` e `market_data` importa: e o que permite conferir que
    um pedido recusado nao pausou ninguem, em vez de conferir apenas o codigo
    de status.
    """
    from crypto_traders.agents.market_data import MarketDataAgent
    from crypto_traders.agents.orchestrator import Orchestrator
    from crypto_traders.agents.risk_manager import RiskManagerAgent
    from crypto_traders.agents.strategy import StrategyAgent
    from crypto_traders.bus import InMemoryEventBus
    from crypto_traders.strategies import build_strategies

    orchestrator = Orchestrator(settings)
    orchestrator.bus = InMemoryEventBus()
    await orchestrator.bus.start()
    orchestrator.risk_manager = RiskManagerAgent(orchestrator.bus, settings)
    orchestrator.strategy = StrategyAgent(
        orchestrator.bus, build_strategies(settings.trading.strategies), settings
    )
    orchestrator.market_data = MarketDataAgent(orchestrator.bus, None, settings)

    app = create_app()
    app.state.settings = settings
    app.state.orchestrator = orchestrator
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, orchestrator, app
    await orchestrator.bus.stop()


class TestAuthorizingCapitalIsNotOneClick:
    """`authorized_capital` E um limite de risco: e o unico numero que decide
    quanto do patrimonio o sistema pode por para trabalhar.

    Medido no codigo anterior, com o portao do ensaio real (29,29 USDC) e um
    aporte de 5.000 na conta: `POST /api/risk/capital/authorize` sem corpo,
    sem `confirm` e sem parametro nenhum devolvia 200 e deixava
    `authorized_capital=5000` -- 170x mais dinheiro autorizado a trabalhar por
    um clique. A rota irma que edita o MESMO campo (`PUT /api/risk/config`)
    recusava com 400 desde sempre, com a justificativa de D8 escrita no codigo.
    """

    @staticmethod
    async def _com_portao(http, orchestrator, *, portao: str, patrimonio: str):
        await http.put(
            "/api/risk/config", json={"authorized_capital": portao, "confirm": True}
        )
        assert orchestrator.risk_manager.configured_limits.authorized_capital == Decimal(
            portao
        )
        orchestrator.risk_manager.observe_snapshot(_snapshot(patrimonio))

    async def test_a_post_without_a_body_leaves_the_gate_standing(self, orquestrado):
        """O ataque exato que passou: POST vazio, sem nada."""
        http, orchestrator, _ = orquestrado
        await self._com_portao(http, orchestrator, portao="29.29", patrimonio="5000")

        resposta = await http.post("/api/risk/capital/authorize")

        assert resposta.status_code == 400, resposta.text
        assert "confirm=true" in resposta.json()["detail"]
        depois = orchestrator.risk_manager.configured_limits.authorized_capital
        assert depois == Decimal("29.29"), (
            f"o portao de autorizacao foi afrouxado por um POST vazio: {depois}"
        )

    async def test_confirm_false_is_refused_too(self, orquestrado):
        """Mandar o campo nao e confirmar: `false` precisa recusar igual."""
        http, orchestrator, _ = orquestrado
        await self._com_portao(http, orchestrator, portao="29.29", patrimonio="5000")

        resposta = await http.post(
            "/api/risk/capital/authorize", json={"confirm": False}
        )

        assert resposta.status_code == 400, resposta.text
        assert orchestrator.risk_manager.configured_limits.authorized_capital == Decimal(
            "29.29"
        )

    async def test_it_works_with_the_explicit_confirmation(self, orquestrado):
        """A trava e da confirmacao, nao do gesto: confirmado, autoriza."""
        http, orchestrator, _ = orquestrado
        await self._com_portao(http, orchestrator, portao="29.29", patrimonio="5000")

        resposta = await http.post(
            "/api/risk/capital/authorize", json={"confirm": True}
        )

        assert resposta.status_code == 200, resposta.text
        corpo = resposta.json()
        assert corpo["authorized_capital"] == "5000", corpo
        # Dinheiro como string tambem nesta rota (D7).
        assert dinheiro_em_number(corpo, "/api/risk/capital/authorize") == []
        assert orchestrator.risk_manager.configured_limits.authorized_capital == Decimal(
            "5000"
        )

    async def test_a_deposit_arriving_after_the_screen_loaded_is_refused(
        self, orquestrado
    ):
        """Autorizar precisa ser um ato sobre um numero que a pessoa VIU.

        Sem isto, "autorizar tudo" autoriza o que existir no instante do clique
        -- inclusive um deposito que chegou depois de a tela carregar, que e
        exatamente o que o portao existe para segurar.
        """
        http, orchestrator, _ = orquestrado
        await self._com_portao(http, orchestrator, portao="29.29", patrimonio="29.29")
        visto_na_tela = (await http.get("/api/risk/capital")).json()["total_value"]
        assert visto_na_tela == "29.29"

        # Entre carregar a tela e clicar, entraram 5.000 na conta.
        orchestrator.risk_manager.observe_snapshot(_snapshot("5029.29"))
        resposta = await http.post(
            "/api/risk/capital/authorize",
            json={"confirm": True, "expected_total_value": visto_na_tela},
        )

        assert resposta.status_code == 409, resposta.text
        assert "5029.29" in resposta.json()["detail"]
        assert orchestrator.risk_manager.configured_limits.authorized_capital == Decimal(
            "29.29"
        ), "o deposito nao visto pela pessoa foi autorizado"

    async def test_the_expected_value_matching_authorizes(self, orquestrado):
        http, orchestrator, _ = orquestrado
        await self._com_portao(http, orchestrator, portao="29.29", patrimonio="100.50")

        resposta = await http.post(
            "/api/risk/capital/authorize",
            json={"confirm": True, "expected_total_value": "100.50"},
        )

        assert resposta.status_code == 200, resposta.text
        assert orchestrator.risk_manager.configured_limits.authorized_capital == Decimal(
            "100.50"
        )

    async def test_an_unknown_field_is_refused(self, orquestrado):
        """`extra=forbid`: campo desconhecido no corpo de uma rota de dinheiro
        e pedido mal entendido, nao campo a ignorar."""
        http, _, _ = orquestrado
        resposta = await http.post(
            "/api/risk/capital/authorize", json={"confirm": True, "authorize_all": True}
        )
        assert resposta.status_code == 422, resposta.text


class TestResettingTheBreakerIsNotOneClick:
    """Rearmar devolve ao sistema a permissao de ABRIR posicao (D4).

    A rota era um POST sem corpo: qualquer pagina aberta na maquina rearmava a
    trava que acabara de disparar, e o dono nao veria diferenca na tela.
    """

    async def test_the_breaker_stays_on_without_confirmation(self, orquestrado):
        http, orchestrator, _ = orquestrado
        await orchestrator.risk_manager._trip("perda diaria de teste")
        assert orchestrator.risk_manager.circuit_breaker_active

        resposta = await http.post("/api/risk/circuit-breaker/reset")

        assert resposta.status_code == 400, resposta.text
        assert "confirm=true" in resposta.json()["detail"]
        assert orchestrator.risk_manager.circuit_breaker_active, (
            "a trava de protecao foi rearmada sem confirmacao explicita"
        )
        # E a consequencia que importa: os agentes continuam recusando retomar.
        recusa = await http.post("/api/agents/resume-all")
        assert recusa.status_code == 409, recusa.text

    async def test_it_resets_with_the_explicit_confirmation(self, orquestrado):
        http, orchestrator, _ = orquestrado
        await orchestrator.risk_manager._trip("perda diaria de teste")

        resposta = await http.post(
            "/api/risk/circuit-breaker/reset", json={"confirm": True}
        )

        assert resposta.status_code == 200, resposta.text
        assert resposta.json()["circuit_breaker_active"] is False
        assert orchestrator.risk_manager.circuit_breaker_active is False


class TestAnotherPageCannotDriveThisApi:
    """A API nao tem autenticacao -- e nao ter e uma decisao: ela escuta so em
    `127.0.0.1`. Isso deixa de valer dentro do navegador.

    As rotas de controle eram POST sem corpo, ou seja "requisicao simples": o
    navegador as envia sem preflight, e CORS so impede a pagina de LER a
    resposta -- o efeito colateral ja aconteceu. Medido no codigo anterior: as
    quatro responderam 200 com `Origin: https://site-qualquer.example` e sem
    credencial nenhuma.
    """

    ESTRANHA = "https://site-qualquer.example"

    ROTAS_DE_CONTROLE = (
        "/api/risk/capital/authorize",
        "/api/risk/circuit-breaker/reset",
        "/api/agents/pause-all",
        "/api/agents/resume-all",
        "/api/agents/strategy/pause",
    )

    @pytest.mark.parametrize("rota", ROTAS_DE_CONTROLE)
    async def test_a_control_route_refuses_a_foreign_origin(self, orquestrado, rota):
        http, orchestrator, _ = orquestrado
        orchestrator.risk_manager.observe_snapshot(_snapshot("5000"))
        antes = orchestrator.risk_manager.configured_limits.authorized_capital

        resposta = await http.post(
            rota, headers={"Origin": self.ESTRANHA}, json={"confirm": True}
        )

        assert resposta.status_code == 403, f"{rota}: {resposta.status_code}"
        # Nao basta o status: nada pode ter acontecido.
        assert orchestrator.risk_manager.configured_limits.authorized_capital == antes
        assert orchestrator.strategy.is_paused is False, (
            f"{rota} pausou a tomada de decisao a pedido de outra origem"
        )

    async def test_changing_business_config_refuses_a_foreign_origin(self, orquestrado):
        http, orchestrator, _ = orquestrado
        antes = orchestrator.settings.trading.timeframe

        resposta = await http.put(
            "/api/trading/config",
            headers={"Origin": self.ESTRANHA},
            json={"timeframe": "4h", "confirm": True},
        )

        assert resposta.status_code == 403, resposta.text
        assert orchestrator.settings.trading.timeframe == antes

    async def test_deleting_history_refuses_a_foreign_origin(self, client, seeded):
        """DELETE tambem: apagar historico e efeito, e efeito irreversivel."""
        alvo = (await client.get("/api/trades?origin=manual")).json()["items"][0]["id"]

        resposta = await client.delete(
            f"/api/trades/{alvo}", headers={"Origin": self.ESTRANHA}
        )

        assert resposta.status_code == 403, resposta.text
        assert (await client.get("/api/trades?origin=manual")).json()["total"] == 1

    async def test_the_local_dashboard_still_works(self, orquestrado):
        """A trava nao pode fechar a porta do dono.

        As duas origens que precisam continuar passando: a configurada em
        `CORS_ORIGINS` e o loopback em qualquer porta -- o dashboard e servido
        pelo Vite e o dono pode abri-lo por `localhost` ou por `127.0.0.1`.
        """
        http, orchestrator, _ = orquestrado
        orchestrator.risk_manager.observe_snapshot(_snapshot("42"))

        for origem in ("http://localhost:5173", "http://127.0.0.1:5173"):
            resposta = await http.post(
                "/api/risk/capital/authorize",
                headers={"Origin": origem},
                json={"confirm": True},
            )
            assert resposta.status_code == 200, f"{origem}: {resposta.text}"

    async def test_reading_is_not_blocked(self, orquestrado):
        """A trava e sobre EFEITO. Leitura segue respondendo, e quem decide se a
        pagina estranha consegue LER a resposta e o CORS."""
        http, _, _ = orquestrado
        resposta = await http.get("/api/health", headers={"Origin": self.ESTRANHA})
        assert resposta.status_code == 200

    def test_the_realtime_stream_refuses_a_foreign_origin(self, settings):
        """WebSocket nao e protegido por CORS: sem esta trava, qualquer aba lia
        patrimonio, ordens e alertas do dono em tempo real."""
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        app = create_app()
        app.state.settings = settings
        antes = ws_module.manager.count

        with TestClient(app) as cliente:
            with (
                pytest.raises(WebSocketDisconnect),
                cliente.websocket_connect("/ws", headers={"Origin": self.ESTRANHA}) as socket,
            ):
                socket.receive_json()

            assert ws_module.manager.count == antes, (
                "a conexao de outra origem entrou no fan-out"
            )

            # E a outra metade da medicao: a trava nao pode fechar o dashboard.
            with cliente.websocket_connect(
                "/ws", headers={"Origin": "http://127.0.0.1:5173"}
            ):
                assert ws_module.manager.count == antes + 1


class TestTwoSimultaneousRequestsLoseNothing:
    """Ataque 2 do dono ("dois sinais simultaneos") na borda da API.

    Medido no codigo anterior, deterministico em 6 de 6 execucoes:
    `PUT /api/trading/config` com {"timeframe": "4h"} e
    {"candle_history_limit": 321} ao mesmo tempo devolvia 200 aos DOIS, com o
    log confirmando os dois, e o estado final tinha so o segundo --
    `timeframe` voltava ao padrao. E ler-alterar-gravar com `await` no meio:
    ambos leem a MESMA base e o ultimo a gravar apaga o outro.

    Duas abas abertas, ou dois cliques em Salvar, bastam. O caso que mais doi e
    `quote_currency` com `symbols`: eles tem de andar juntos (foi o que a
    migracao para USDC fez), e perder um deixa o sistema medindo o caixa numa
    moeda em que nenhum par negociado liquida, com o dashboard verde.
    """

    async def test_business_config_keeps_every_accepted_field(self, orquestrado):
        http, orchestrator, _ = orquestrado

        primeiro, segundo = await asyncio.gather(
            http.put("/api/trading/config", json={"timeframe": "4h", "confirm": True}),
            http.put(
                "/api/trading/config",
                json={"candle_history_limit": 321, "confirm": True},
            ),
        )

        assert primeiro.status_code == 200, primeiro.text
        assert segundo.status_code == 200, segundo.text
        trading = orchestrator.settings.trading
        assert trading.timeframe == "4h" and trading.candle_history_limit == 321, (
            "dois PUT aceitos com 200 e o estado final tem so um: "
            f"timeframe={trading.timeframe} "
            f"candle_history_limit={trading.candle_history_limit}"
        )

    async def test_the_pair_that_has_to_travel_together_survives(self, orquestrado):
        """A corrida que quebraria o sistema de verdade: moeda e pares.

        Se um dos dois se perder, o caixa passa a ser medido numa moeda em que
        os pares negociados nao liquidam -- e nada na tela diz isso.
        """
        http, orchestrator, _ = orquestrado

        respostas = await asyncio.gather(
            http.put(
                "/api/trading/config", json={"quote_currency": "USDC", "confirm": True}
            ),
            http.put(
                "/api/trading/config",
                json={"symbols": ["BTC/USDC", "ETH/USDC"], "confirm": True},
            ),
        )

        assert [r.status_code for r in respostas] == [200, 200], [
            r.text for r in respostas
        ]
        trading = orchestrator.settings.trading
        assert trading.quote_currency == "USDC", trading.quote_currency
        assert trading.symbols == ["BTC/USDC", "ETH/USDC"], trading.symbols
        assert all(par.endswith("/USDC") for par in trading.symbols), (
            "sobrou par que nao liquida na moeda de cotacao configurada: "
            f"{trading.quote_currency} com {trading.symbols}"
        )

    async def test_eight_simultaneous_limit_changes_all_stand(self, orquestrado):
        """A mesma corrida na rota de risco.

        Hoje ela escapa por dois motivos, e um teste que confirma "passou" sem
        exigir o resultado nao serviria para nada: o que se cobra aqui e que
        TODO campo que recebeu 200 esteja em vigor.
        """
        http, orchestrator, _ = orquestrado
        pedidos = [
            ("max_order_notional", "10"),
            ("min_signal_confidence", 0.99),
            ("cooldown_seconds", 1234),
            ("max_open_positions", 2),
            ("stop_loss_pct", 0.02),
            ("take_profit_pct", 0.04),
            ("daily_loss_limit_pct", 0.01),
            ("max_asset_exposure_pct", 0.2),
        ]

        respostas = await asyncio.gather(
            *(
                http.put("/api/risk/config", json={campo: valor, "confirm": True})
                for campo, valor in pedidos
            )
        )

        assert [r.status_code for r in respostas] == [200] * len(pedidos), [
            r.status_code for r in respostas
        ]
        limites = orchestrator.risk_manager.configured_limits
        perdidos = [
            f"{campo}: pedi {valor}, vigora {getattr(limites, campo)}"
            for campo, valor in pedidos
            if str(getattr(limites, campo)) != str(valor)
        ]
        assert perdidos == [], (
            f"{len(perdidos)} de {len(pedidos)} pedidos receberam 200 e nao estao "
            "em vigor: " + "; ".join(perdidos)
        )


class TestOneBadEventDoesNotSilenceAChannel:
    """Um evento que nao serializa nao pode calar um topico para sempre.

    Medido no codigo anterior: o `relay` capturava `Exception`, logava e fazia
    `return` -- a tarefa morria e o topico nao entregava mais nada ate a API
    reiniciar. Com ALERTS isso e o pior caso possivel: o log fica no servidor,
    mas a TELA e o que o dono olha, e uma tela sem alerta e indistinguivel de
    "nada aconteceu".
    """

    async def test_the_circuit_breaker_alert_still_reaches_the_browser(self, settings):
        from crypto_traders.agents.orchestrator import Orchestrator
        from crypto_traders.bus import InMemoryEventBus, Topics

        bus = InMemoryEventBus()
        await bus.start()
        orchestrator = Orchestrator(settings)
        orchestrator.bus = bus
        app = create_app()
        app.state.settings = settings
        navegador = FakeSocket()
        await ws_module.manager.connect(navegador)
        ws_module.attach_broadcaster(app, orchestrator)
        try:
            await asyncio.sleep(0.05)
            # Chave nao-string: `json.dumps` levanta TypeError.
            await bus.publish(Topics.ALERTS, {("tupla",): "chave impossivel"})
            await asyncio.sleep(0.05)
            # Agora o alerta que o dono precisa ver.
            await bus.publish(
                Topics.ALERTS,
                {"type": "circuit_breaker", "title": "trava", "reason": "perda diaria"},
            )
            await asyncio.sleep(0.05)

            recebidos = [json.loads(texto) for texto in navegador.recebidas]
            tipos = [
                m["data"].get("type") for m in recebidos if isinstance(m["data"], dict)
            ]
            assert "circuit_breaker" in tipos, (
                "o alerta de circuit breaker nunca chegou ao navegador: um evento "
                "anterior matou o relay do topico ALERTS de vez"
            )
        finally:
            await ws_module.detach_broadcaster(app)
            await bus.stop()

    async def test_the_other_channels_keep_working_too(self, settings):
        """A prova de que o canal segue VIVO, e nao de que um evento passou."""
        from crypto_traders.agents.orchestrator import Orchestrator
        from crypto_traders.bus import InMemoryEventBus, Topics

        bus = InMemoryEventBus()
        await bus.start()
        orchestrator = Orchestrator(settings)
        orchestrator.bus = bus
        app = create_app()
        app.state.settings = settings
        navegador = FakeSocket()
        await ws_module.manager.connect(navegador)
        ws_module.attach_broadcaster(app, orchestrator)
        try:
            await asyncio.sleep(0.05)
            for _ in range(3):
                await bus.publish(Topics.ALERTS, {("tupla",): "impossivel"})
                await asyncio.sleep(0.02)
            await bus.publish(Topics.PORTFOLIO_SNAPSHOTS, _snapshot("29.29"))
            await bus.publish(Topics.ALERTS, {"type": "capital_pendente"})
            await asyncio.sleep(0.1)

            eventos = [json.loads(texto) for texto in navegador.recebidas]
            nomes = [evento["event"] for evento in eventos]
            assert "portfolio" in nomes and "alert" in nomes, nomes
            dinheiro = next(e for e in eventos if e["event"] == "portfolio")
            assert dinheiro["data"]["total_value"] == "29.29", dinheiro
        finally:
            await ws_module.detach_broadcaster(app)
            await bus.stop()


class TestShuttingDownLeavesNothingRunning:
    """O desligamento derrubava os relays e deixava a tarefa de envio de cada
    navegador pendurada em `await queue.get()`.

    Com o processo saindo o custo e ruido ("Task was destroyed but it is
    pending"); num reload, ou com dois `create_app()` no mesmo processo, os
    writers antigos seguem vivos segurando socket morto -- e o `manager` e
    singleton de modulo, entao eles compartilham a lista.
    """

    @staticmethod
    def _tarefas_vivas(prefixo: str) -> list[asyncio.Task]:
        return [
            tarefa
            for tarefa in asyncio.all_tasks()
            if tarefa.get_name().startswith(prefixo) and not tarefa.done()
        ]

    async def test_the_real_lifespan_takes_the_writers_down(self, settings):
        """Pelo `lifespan` de verdade do `create_app`, nao por chamada direta."""
        from crypto_traders.agents.orchestrator import Orchestrator
        from crypto_traders.bus import InMemoryEventBus

        bus = InMemoryEventBus()
        await bus.start()
        orchestrator = Orchestrator(settings)
        orchestrator.bus = bus
        app = create_app(orchestrator)
        app.state.settings = settings

        navegador = FakeSocket(travado=True)
        async with app.router.lifespan_context(app):
            await ws_module.manager.connect(navegador)
            assert self._tarefas_vivas("ws-writer"), "o navegador tem tarefa de envio"
            assert self._tarefas_vivas("ws-relay"), "os relays estao no ar"

        assert self._tarefas_vivas("ws-writer") == [], (
            "tarefa de envio continua viva depois do desligamento da API"
        )
        assert self._tarefas_vivas("ws-relay") == []
        assert ws_module.manager.count == 0, "conexao continua na lista do fan-out"
        assert navegador.fechado, "o navegador nao recebeu o fechamento"
        await bus.stop()
