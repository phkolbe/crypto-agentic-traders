"""Taxa cobrada em terceira moeda (BNB): convertida pelo candle gravado do instante.

Ate 10/11/2026 a taxa em BNB ficava fora do custo medio e do realizado, com o
argumento de que "a cotacao do instante nao e guardada". Ela e: os candles de
BNB/<cotacao> estao no banco sempre que o par e coletado. Descartar subestimava o
medio (stop um pouco mais baixo) e deixava a taxa da venda fora do realizado que a
trava diaria obedece. Agora so se descarta, com aviso, quando nao ha candle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from crypto_traders.agents.portfolio import (
    TOLERANCIA_COTACAO_DA_TAXA,
    PortfolioAgent,
    cotacoes_das_taxas,
)
from crypto_traders.analise import resultado_por_par
from crypto_traders.bus import InMemoryEventBus
from crypto_traders.db.repositories import AuditLogRepository, CandleRepository, TradeRepository
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import ExchangeName, Side
from crypto_traders.domain.models import Candle
from crypto_traders.exchanges.paper import PaperBroker

#: O trade acontece as 10:00; o candle diario de BNB que FECHOU antes dele e o da
#: vespera (fecha a 00:00 do dia do trade).
DIA = datetime(2026, 10, 5, tzinfo=UTC)
INSTANTE = DIA + timedelta(hours=10)


async def gravar_candle(settings, open_time: datetime, close: str, timeframe: str = "1d"):
    async with session_scope(settings) as session:
        await CandleRepository(session).upsert_many(
            [
                Candle(
                    exchange=ExchangeName.BINANCE,
                    symbol="BNB/USDT",
                    timeframe=timeframe,
                    open_time=open_time,
                    open=Decimal(close),
                    high=Decimal(close),
                    low=Decimal(close),
                    close=Decimal(close),
                    volume=Decimal(1),
                )
            ]
        )


async def registrar(settings, side: Side, quantidade: str, preco: str, taxa: str, quando):
    async with session_scope(settings) as session:
        await TradeRepository(session).record(
            executed_at=quando,
            exchange="paper",
            symbol="BTC/USDT",
            side=str(side),
            quantity=Decimal(quantidade),
            price=Decimal(preco),
            fee=Decimal(taxa),
            fee_currency="BNB",
            mode="dry_run",
        )


def agente(settings, btc: str, caixa: str, preco: str) -> PortfolioAgent:
    broker = PaperBroker(initial_balance=Decimal(caixa))
    if Decimal(btc) > 0:
        broker.credit("BTC", Decimal(btc))
    return PortfolioAgent(
        InMemoryEventBus(), broker, settings, price_source=lambda: {"BTC": Decimal(preco)}
    )


async def acoes(settings) -> list[str]:
    async with session_scope(settings) as session:
        return [r.action for r in await AuditLogRepository(session).list(limit=50)]


class TestConversaoPeloCandle:
    async def test_compra_com_taxa_em_bnb_entra_no_custo_medio(self, settings):
        # Candle diario de BNB que fechou a 00:00 do dia do trade: 600 USDT.
        await gravar_candle(settings, DIA - timedelta(days=1), "600")
        await registrar(settings, Side.BUY, "1", "100", "0.01", INSTANTE)

        snapshot = await agente(settings, "1", "0", "100").build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        # 100 + 0,01 BNB x 600 = 106: a taxa e custo de adquirir o ativo.
        assert btc.average_price == Decimal("106")
        assert "portfolio_taxa_descartada" not in await acoes(settings)

    async def test_taxa_em_bnb_da_venda_sai_do_realizado(self, settings):
        await gravar_candle(settings, DIA - timedelta(days=1), "600")
        await registrar(settings, Side.BUY, "1", "100", "0", INSTANTE)
        await registrar(settings, Side.SELL, "1", "120", "0.01", INSTANTE + timedelta(hours=1))

        snapshot = await agente(settings, "0", "120", "120").build_snapshot()

        # 1 x (120 - 100) - 0,01 x 600 = 14
        assert snapshot.realized_pnl == Decimal("14")

    async def test_candle_que_fecha_depois_do_trade_nao_e_usado(self, settings):
        """O candle do proprio dia fecha depois do trade: seria preco do futuro.

        Alem de ser lookahead, a escolha mudaria com o tempo (o candle do dia so
        chega no fim dele) e o realizado ja publicado se moveria sozinho.
        """
        await gravar_candle(settings, DIA - timedelta(days=1), "600")
        await gravar_candle(settings, DIA, "900")
        await registrar(settings, Side.BUY, "1", "100", "0.01", INSTANTE)

        snapshot = await agente(settings, "1", "0", "100").build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("106")

    async def test_o_candle_mais_recente_entre_timeframes_vence(self, settings):
        # O diario fechou a 00:00 (600); o de 4h fechou as 08:00 (650).
        await gravar_candle(settings, DIA - timedelta(days=1), "600")
        await gravar_candle(settings, DIA + timedelta(hours=4), "650", timeframe="4h")
        await registrar(settings, Side.BUY, "1", "100", "0.01", INSTANTE)

        snapshot = await agente(settings, "1", "0", "100").build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("106.5")


class TestSemCotacaoContinuaDescartada:
    async def test_sem_candle_nenhum_descarta_com_aviso(self, settings):
        await registrar(settings, Side.BUY, "1", "100", "0.01", INSTANTE)

        snapshot = await agente(settings, "1", "0", "100").build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("100")
        assert "portfolio_taxa_descartada" in await acoes(settings)

    async def test_candle_velho_demais_descarta_com_aviso(self, settings):
        """Um preco de semanas antes nao e "o instante do trade"."""
        await gravar_candle(
            settings, INSTANTE - TOLERANCIA_COTACAO_DA_TAXA - timedelta(days=2), "600"
        )
        await registrar(settings, Side.BUY, "1", "100", "0.01", INSTANTE)

        snapshot = await agente(settings, "1", "0", "100").build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("100")
        assert "portfolio_taxa_descartada" in await acoes(settings)


class TestAnaliseUsaAMesmaRegra:
    """A tela Portfolio soma o realizado por par pela mesma regra do retrato."""

    async def test_resultado_por_par_converte_com_as_mesmas_cotacoes(self, settings):
        await gravar_candle(settings, DIA - timedelta(days=1), "600")
        await registrar(settings, Side.BUY, "1", "100", "0", INSTANTE)
        await registrar(settings, Side.SELL, "1", "120", "0.01", INSTANTE + timedelta(hours=1))

        async with session_scope(settings) as session:
            trades = await TradeRepository(session).for_cost_basis()
            cotacoes = await cotacoes_das_taxas(session, trades, "USDT")

        (btc,) = resultado_por_par(
            trades, "USDT", "dry_run", DIA, DIA + timedelta(days=1),
            cotacoes_da_taxa=cotacoes,
        )
        assert btc.realizado == Decimal("14")
        assert btc.taxas == Decimal("6")

    def test_venda_com_realizado_zero_nao_e_perda(self):
        """Empatar nao e perder: a contagem de perdas mentia na tela."""
        trades = [
            SimpleNamespace(
                id="c", symbol="BTC/USDT", side="buy", quantity="1", price="100",
                fee="0", fee_currency="USDT", mode="dry_run", executed_at=INSTANTE,
            ),
            SimpleNamespace(
                id="v", symbol="BTC/USDT", side="sell", quantity="1", price="100",
                fee="0", fee_currency="USDT", mode="dry_run",
                executed_at=INSTANTE + timedelta(hours=1),
            ),
        ]
        (btc,) = resultado_por_par(trades, "USDT", "dry_run", DIA, DIA + timedelta(days=1))
        assert btc.vendas == 1
        assert btc.perdas == 0
        assert btc.ganhos == 0
