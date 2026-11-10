"""`GET /api/atividade`: o historico do feed "Atividade em tempo real".

O feed do Dashboard so guardava o que chegava pelo WebSocket com a tela aberta, e
sumia ao recarregar. Com o sistema em `dry_run` sem sinais, o operador abria a tela
e via "Aguardando eventos" sem saber se o sistema tinha subido, parado ou disparado
o circuit breaker de madrugada. A rota junta as fontes que ja existem no banco
(sinais, decisoes de risco, ordens, `audit_log`) no MESMO formato do WebSocket,
para a tela mostrar o historico e o tempo real com uma descricao so.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from crypto_traders.api.app import create_app
from crypto_traders.db.repositories import (
    AuditLogRepository,
    OrderRepository,
    RiskEventRepository,
    SignalRepository,
)
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import (
    ExchangeName,
    OrderStatus,
    OrderType,
    RiskDecision,
    RiskEventType,
    Side,
    SignalDirection,
)
from crypto_traders.domain.models import (
    OrderRequest,
    OrderResult,
    RiskAssessment,
    Signal,
)

AGORA = datetime.now(UTC)


@pytest.fixture
async def client(settings):
    app = create_app()
    app.state.settings = settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def povoado(settings):
    """Um episodio de cada fonte, com o que NAO deve aparecer no meio."""
    sinal = Signal(
        created_at=AGORA - timedelta(minutes=50),
        exchange=ExchangeName.BINANCE,
        symbol="BTC/USDT",
        timeframe="1h",
        strategy="ma_crossover",
        direction=SignalDirection.LONG,
        confidence=0.8,
        reason="cruzamento das medias",
        reference_price=Decimal("50000.123456789"),
    )
    avaliacao = RiskAssessment(
        created_at=AGORA - timedelta(minutes=49),
        signal_id=sinal.id,
        decision=RiskDecision.APPROVED,
        approved_quantity=Decimal("0.0002"),
        approved_notional=Decimal("10.000024"),
        snapshot={"symbol": "BTC/USDT", "carteira": {"muito": "grande"}},
    )
    pedido = OrderRequest(
        created_at=AGORA - timedelta(minutes=48),
        client_order_id="feed-1",
        signal_id=sinal.id,
        risk_event_id=avaliacao.id,
        exchange=ExchangeName.BINANCE,
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.0002"),
        notional=Decimal("10.000024"),
    )
    pendente = OrderRequest(
        created_at=AGORA - timedelta(minutes=47),
        client_order_id="feed-2",
        signal_id=None,
        risk_event_id=avaliacao.id,
        exchange=ExchangeName.BINANCE,
        symbol="ETH/USDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        notional=Decimal("25"),
    )
    async with session_scope(settings) as session:
        await SignalRepository(session).save(sinal)
        await RiskEventRepository(session).save_assessment(avaliacao)
        # Evento de risco que nao e decisao sobre sinal: o circuit breaker ja
        # chega como alerta (pelo `audit_log`); como `risk_assessment` a tela
        # diria "Risk Manager rejeitou" sobre algo que nao foi rejeicao.
        await RiskEventRepository(session).save_event(
            RiskEventType.CIRCUIT_BREAKER_TRIPPED, ["perda diaria de 5%"]
        )
        ordens = OrderRepository(session)
        await ordens.create_pending(pedido, "dry_run")
        await ordens.apply_result(
            OrderResult(
                order_request_id=pedido.id,
                client_order_id=pedido.client_order_id,
                exchange_order_id="x-1",
                status=OrderStatus.FILLED,
                filled_quantity=Decimal("0.0002"),
                average_price=Decimal("50000.12"),
            )
        )
        # Pendente ainda nao tem resultado: o WebSocket nunca publica isso.
        await ordens.create_pending(pendente, "dry_run")
        auditoria = AuditLogRepository(session)
        await auditoria.append(
            action="system_started", target="orchestrator", after={"mode": "dry_run"}
        )
        await auditoria.append(
            action="circuit_breaker_tripped", target="risk_config", detail="perda diaria de 5%"
        )
        # Acao administrativa: tem tela propria (Risco), nao e marco do feed.
        await auditoria.append(
            action="risk_limits_updated", actor="user", target="risk_config"
        )
    return {"sinal": sinal, "avaliacao": avaliacao, "pedido": pedido, "pendente": pendente}


def _instante(item: dict) -> datetime:
    return datetime.fromisoformat(item["at"].replace("Z", "+00:00"))


class TestAtividade:
    async def test_sem_nada_no_banco_devolve_lista_vazia(self, client):
        resposta = await client.get("/api/atividade")
        assert resposta.status_code == 200, resposta.text
        assert resposta.json() == []

    async def test_junta_as_fontes_no_formato_do_websocket(self, client, povoado):
        resposta = await client.get("/api/atividade")
        assert resposta.status_code == 200, resposta.text
        itens = resposta.json()
        por_evento: dict[str, list[dict]] = {}
        for item in itens:
            por_evento.setdefault(item["event"], []).append(item)

        assert set(por_evento) == {"signal", "risk_assessment", "order_result", "alert", "system"}

        # Os ids sao os mesmos do payload do WebSocket: e com eles que a tela
        # junta historico e tempo real sem mostrar o mesmo evento duas vezes.
        (sinal,) = por_evento["signal"]
        assert sinal["data"]["id"] == povoado["sinal"].id
        assert sinal["data"]["direction"] == "long"
        assert sinal["data"]["reason"] == "cruzamento das medias"

        (risco,) = por_evento["risk_assessment"]
        assert risco["data"]["id"] == povoado["avaliacao"].id
        assert risco["data"]["decision"] == "approved"
        # So o par sai do retrato: o resto e o estado inteiro da carteira, que
        # pesaria a resposta sem a tela usar.
        assert risco["data"]["snapshot"] == {"symbol": "BTC/USDT"}

        (ordem,) = por_evento["order_result"]
        assert ordem["data"]["order_request_id"] == povoado["pedido"].id
        assert ordem["data"]["status"] == "filled"
        assert ordem["data"]["symbol"] == "BTC/USDT"

        (alerta,) = por_evento["alert"]
        # O `type` e o mesmo do alerta publicado pelo Risk Manager.
        assert alerta["data"]["type"] == "circuit_breaker"
        assert alerta["data"]["reason"] == "perda diaria de 5%"

        (marco,) = por_evento["system"]
        assert marco["data"]["type"] == "system_started"
        assert marco["data"]["mode"] == "dry_run"

    async def test_dinheiro_sai_como_string(self, client, povoado):
        itens = (await client.get("/api/atividade")).json()
        risco = next(i for i in itens if i["event"] == "risk_assessment")
        ordem = next(i for i in itens if i["event"] == "order_result")
        sinal = next(i for i in itens if i["event"] == "signal")
        assert risco["data"]["approved_notional"] == "10.000024"
        assert ordem["data"]["average_price"] == "50000.12"
        assert sinal["data"]["reference_price"] == "50000.123456789"

    async def test_do_mais_novo_para_o_mais_antigo_com_fuso(self, client, povoado):
        itens = (await client.get("/api/atividade")).json()
        instantes = [_instante(item) for item in itens]
        assert all(instante.tzinfo is not None for instante in instantes), itens
        assert instantes == sorted(instantes, reverse=True)
        assert itens[-1]["event"] == "signal"

    async def test_limite_corta_nos_mais_recentes(self, client, povoado):
        todos = (await client.get("/api/atividade")).json()
        dois = (await client.get("/api/atividade", params={"limite": 2})).json()
        assert dois == todos[:2]

    @pytest.mark.parametrize("limite", [0, -1, 201])
    async def test_limite_fora_da_faixa_e_recusado(self, client, limite):
        resposta = await client.get("/api/atividade", params={"limite": limite})
        assert resposta.status_code == 422

    async def test_atras_do_portao_de_acesso(self, settings):
        producao = settings.model_copy(
            update={
                "acesso_login_tailscale": "dono@exemplo.com",
                "acesso_proxy_tailscale": ["172.18.0.4"],
                "cors_origins": ["https://crypto.exemplo.ts.net"],
            }
        )
        with patch("crypto_traders.api.app.get_settings", return_value=producao):
            app = create_app()
        app.state.settings = producao
        # Vindo do host (nao do serve), com o cabecalho forjado: recusa.
        transporte = ASGITransport(app=app, client=("172.18.0.1", 40000))
        async with AsyncClient(transport=transporte, base_url="http://test") as http:
            resposta = await http.get(
                "/api/atividade", headers={"Tailscale-User-Login": "dono@exemplo.com"}
            )
        assert resposta.status_code == 403
