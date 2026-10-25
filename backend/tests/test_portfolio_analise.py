"""Tela Portfolio: serie amostrada na janela inteira, periodo personalizado e analise.

O defeito que motivou a serie amostrada: `history(limit=5000)` devolvia os 5.000
retratos MAIS RECENTES, ~3,5 dias com um retrato por minuto, e os botoes 7d, 30d
e 90d desenhavam a mesma curva. O teste semeia 8 dias de retratos por minuto
(11.520) e confere que 7 dias comecam ha 7 dias -- e que o caminho antigo nao.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from crypto_traders.agents.portfolio import PortfolioAgent
from crypto_traders.analise import motivo_canonico, resultado_por_par, resumo_de_risco
from crypto_traders.api.app import create_app
from crypto_traders.bus.memory import InMemoryEventBus
from crypto_traders.db import models as orm
from crypto_traders.db.repositories import PortfolioSnapshotRepository
from crypto_traders.db.session import session_scope

AGORA = datetime.now(UTC).replace(second=0, microsecond=0)


@pytest.fixture
async def client(settings):
    app = create_app()
    app.state.settings = settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.fixture
async def oito_dias(settings):
    """Um retrato por minuto nos ultimos 8 dias, valor = minutos desde o inicio."""
    total = 8 * 24 * 60
    async with session_scope(settings) as ses:
        ses.add_all(
            orm.PortfolioSnapshot(
                id=f"s{i}",
                timestamp=AGORA - timedelta(minutes=total - i),
                total_value=Decimal(1000 + i),
                cash_value=Decimal(400),
                positions_value=Decimal(600 + i),
                realized_pnl=Decimal(0),
                unrealized_pnl=Decimal(i),
                allocations={"USDT": 0.4, "BTC": 0.6},
                positions=[],
                mode="dry_run",
            )
            for i in range(total)
        )
    return total


class TestSerieAmostrada:
    async def test_sete_dias_cobrem_sete_dias(self, client, oito_dias):
        pontos = (await client.get("/api/portfolio/history?days=7")).json()
        inicio = datetime.fromisoformat(pontos[0]["timestamp"])
        assert len(pontos) <= 500
        assert inicio <= AGORA - timedelta(days=6, hours=23)
        # O ultimo ponto e o retrato mais recente: o "agora" do grafico nao atrasa.
        assert Decimal(pontos[-1]["total_value"]) == Decimal(1000 + oito_dias - 1)
        assert pontos[-1]["allocations"] == {"USDT": 0.4, "BTC": 0.6}

    async def test_o_defeito_existia_no_caminho_antigo(self, settings, oito_dias):
        async with session_scope(settings) as ses:
            antigos = await PortfolioSnapshotRepository(ses).history(
                since=AGORA - timedelta(days=7), limit=5000
            )
        primeiro = antigos[0].timestamp.replace(tzinfo=UTC)
        assert primeiro > AGORA - timedelta(days=3, hours=12), "5000 por minuto = 3,5 dias"

    async def test_faixas_distintas_dao_curvas_distintas(self, client, oito_dias):
        sete = (await client.get("/api/portfolio/history?days=7")).json()
        um = (await client.get("/api/portfolio/history?days=1")).json()
        assert sete[0]["timestamp"] < um[0]["timestamp"]

    async def test_periodo_personalizado(self, client, oito_dias):
        de = (AGORA - timedelta(days=5)).isoformat()
        ate = (AGORA - timedelta(days=4)).isoformat()
        pontos = (
            await client.get("/api/portfolio/history", params={"start": de, "end": ate})
        ).json()
        assert pontos
        assert all(de <= p["timestamp"].replace("Z", "+00:00") <= ate for p in pontos)

    @pytest.mark.parametrize(
        "params",
        [
            {"start": "2025-01-01T00:00:00Z", "end": "2026-03-01T00:00:00Z"},
            {"start": "2026-03-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
        ],
    )
    async def test_mais_de_um_ano_ou_invertido_e_recusado(self, client, params):
        assert (await client.get("/api/portfolio/history", params=params)).status_code == 422


def _trade(i, simbolo, lado, qtd, preco, taxa="0", modo="dry_run", dias_atras=10, moeda_taxa=None):
    return SimpleNamespace(
        id=f"t{i:03d}",
        symbol=simbolo,
        side=lado,
        quantity=Decimal(qtd),
        price=Decimal(preco),
        fee=Decimal(taxa),
        fee_currency=moeda_taxa,
        mode=modo,
        executed_at=AGORA - timedelta(days=dias_atras, minutes=-i),
    )


HISTORICO = [
    _trade(1, "BTC/USDT", "buy", "2", "100", "1"),
    _trade(2, "BTC/USDT", "buy", "2", "200", "1"),
    _trade(3, "BTC/USDT", "sell", "1", "300", "1", dias_atras=2),  # 300 - 150,5 - 1
    _trade(4, "ETH/USDT", "buy", "1", "50", dias_atras=5),
    _trade(5, "ETH/USDT", "sell", "1", "40", "0.5", dias_atras=1),  # perda
    _trade(6, "SOL/USDT", "sell", "3", "10", dias_atras=1),  # sem base de custo
    _trade(7, "BTC/BRL", "sell", "1", "999999", dias_atras=1),  # outra cotacao
    _trade(8, "BTC/USDT", "sell", "1", "999", modo="live", dias_atras=1),  # outro modo
    _trade(9, "ADA/USDT", "buy", "10", "1", modo="manual", dias_atras=3),
    _trade(10, "ADA/USDT", "sell", "10", "2", modo="manual", dias_atras=1),
]


class TestResultadoPorPar:
    def test_bate_com_o_realizado_do_portfolio_agent(self, settings):
        agente = PortfolioAgent(InMemoryEventBus(), None, settings, lambda: {})
        total_agente = agente._apurar(HISTORICO).realizado
        pares = resultado_por_par(HISTORICO, "USDT", "dry_run", AGORA - timedelta(days=365), AGORA)
        assert sum(p.realizado for p in pares) == total_agente

    def test_contagens_e_exclusoes(self):
        pares = {
            p.simbolo: p
            for p in resultado_por_par(
                HISTORICO, "USDT", "dry_run", AGORA - timedelta(days=30), AGORA
            )
        }
        assert set(pares) == {"BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT"}
        btc = pares["BTC/USDT"]
        assert (btc.vendas, btc.ganhos, btc.perdas, btc.compras) == (1, 1, 0, 2)
        assert btc.realizado == Decimal("300") - Decimal("150.5") - Decimal("1")
        assert (pares["ETH/USDT"].ganhos, pares["ETH/USDT"].perdas) == (0, 1)
        assert pares["SOL/USDT"].vendas_sem_base == 1
        assert pares["SOL/USDT"].realizado == 0
        assert pares["ADA/USDT"].realizado == Decimal("10")  # manual conta sempre

    def test_so_o_periodo_conta_mas_a_base_vem_de_antes(self):
        pares = {
            p.simbolo: p
            for p in resultado_por_par(
                HISTORICO, "USDT", "dry_run", AGORA - timedelta(days=3), AGORA
            )
        }
        # As compras de BTC foram ha 10 dias (fora), mas dao a base da venda de 2 dias.
        assert pares["BTC/USDT"].compras == 0
        assert pares["BTC/USDT"].realizado == Decimal("148.5")


class TestMotivosDeRisco:
    def test_ativo_vira_marcador_e_sigla_do_texto_fica(self):
        ativos = {"AVAX", "SUI", "BTC"}
        assert motivo_canonico("nao ha posicao em AVAX para fechar", ativos) == (
            "nao ha posicao em <ativo> para fechar"
        )
        assert motivo_canonico("MVRV acima do percentil 0.80", ativos) == (
            "MVRV acima do percentil <n>"
        )
        assert motivo_canonico("cooldown de BTC/USDC: 900 s", ativos) == "cooldown de <par>: <n> s"

    def test_agrupa_e_conta(self):
        eventos = [
            SimpleNamespace(decision="rejected", reasons=["nao ha posicao em AVAX para fechar"]),
            SimpleNamespace(decision="rejected", reasons=["nao ha posicao em SUI para fechar"]),
            SimpleNamespace(decision="rejected", reasons=["ordem abaixo do minimo de 10"]),
            SimpleNamespace(decision="approved", reasons=[]),
            SimpleNamespace(decision=None, reasons=["circuit breaker"]),
        ]
        resumo = resumo_de_risco(eventos, {"AVAX", "SUI"})
        assert (resumo.aprovadas, resumo.rejeitadas) == (1, 3)
        assert resumo.motivos[0][:2] == ("nao ha posicao em <ativo> para fechar", 2)


class TestEndpointDeAnalise:
    async def test_benchmark_diarios_e_risco(self, client, settings, oito_dias):
        async with session_scope(settings) as ses:
            for d in range(10):
                ses.add(
                    orm.Candle(
                        exchange="binance",
                        symbol="BTC/USDT",
                        timeframe="1d",
                        open_time=(AGORA - timedelta(days=d)).replace(hour=0, minute=0),
                        open=Decimal(1),
                        high=Decimal(1),
                        low=Decimal(1),
                        close=Decimal(60000 + d),
                        volume=Decimal(1),
                    )
                )
        corpo = (await client.get("/api/portfolio/analise?days=7")).json()
        assert corpo["moeda"] == "USDT"
        assert corpo["benchmark"]["simbolo"] == "BTC/USDT"
        assert 7 <= len(corpo["benchmark"]["fechamentos"]) <= 9
        # Um fechamento por dia UTC, desde o dia anterior ao inicio.
        dias = [p["timestamp"][:10] for p in corpo["fechamentos_diarios"]]
        assert len(dias) == len(set(dias)) and 8 <= len(dias) <= 9
        assert corpo["risco"] == {"aprovadas": 0, "rejeitadas": 0, "motivos": []}
