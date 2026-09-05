"""Testes da API.

Cobrem o contrato que o dashboard consome e, principalmente, as travas das
rotas de controle: alterar limite de risco e apagar historico sao as duas
operacoes pela interface que podem causar estrago real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from crypto_traders.api.app import create_app
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
        response = await http.post("/api/risk/circuit-breaker/reset")
        assert response.status_code == 409

    async def test_limit_change_shows_up_in_the_audit_log(self, controlled):
        http, _ = controlled
        await http.put("/api/risk/config", json={"cooldown_seconds": 300, "confirm": True})
        entries = (await http.get("/api/audit")).json()
        assert any(entry["action"] == "risk_limits_updated" for entry in entries)
