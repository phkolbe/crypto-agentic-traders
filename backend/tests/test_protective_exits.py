"""Stop-loss e take-profit no backtest.

Estes testes existem por causa de um defeito que nao se manifestava como erro:
os niveis eram calculados pelo Risk Manager, gravados na ordem e **nunca
comparados com preco nenhum**. Toda posicao andava ate a estrategia mandar sair,
e as quedas maximas medidas eram o retrato de um sistema sem stop -- justamente
o numero que o stop existe para limitar.

Um parametro que nao faz nada e invisivel no teste tradicional: a suite passava
inteira com o stop inerte. O que denunciou foi uma varredura de 768 combinacoes
em que mudar `stop_loss_pct` nao alterou **um unico** resultado.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_traders.backtest.portfolio import PortfolioBacktestEngine
from crypto_traders.config import RiskSettings
from crypto_traders.domain.enums import ExchangeName, SignalDirection
from crypto_traders.domain.models import Candle, Signal
from crypto_traders.strategies.base import MarketFrame, Strategy

INICIO = datetime(2026, 1, 1, tzinfo=UTC)


def candle(i: int, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(
        exchange=ExchangeName.BINANCE,
        symbol="BTC/USDT",
        timeframe="1d",
        open_time=INICIO + timedelta(days=i),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        volume=Decimal("10"),
    )


class CompraUmaVez(Strategy):
    """Compra no primeiro candle apos o aquecimento e nunca mais opina.

    Isola a protecao: se a posicao fechar, foi o stop ou o alvo -- nao existe
    outra saida possivel.
    """

    name = "compra_uma_vez"
    description = "teste"
    min_candles = 3

    def __init__(self) -> None:
        self._comprou = False

    def evaluate(self, market: MarketFrame) -> Signal | None:
        if self._comprou:
            return None
        self._comprou = True
        return Signal(
            exchange=market.exchange,
            symbol=market.symbol,
            timeframe=market.timeframe,
            strategy=self.name,
            direction=SignalDirection.LONG,
            confidence=0.9,
            reason="teste",
            reference_price=Decimal(str(market.frame["close"].iloc[-1])),
        )


def limites(stop: float, alvo: float) -> RiskSettings:
    return RiskSettings(
        max_order_notional=Decimal("1000"),
        max_order_pct_portfolio=0.9,
        max_asset_exposure_pct=0.9,
        max_open_positions=3,
        min_order_notional=Decimal("10"),
        stop_loss_pct=stop,
        take_profit_pct=alvo,
        daily_loss_limit_pct=0.9,
        weekly_loss_limit_pct=0.9,
        min_signal_confidence=0.5,
        cooldown_seconds=0,
        symbol_whitelist=["BTC/USDT"],
        asset_whitelist=["BTC"],
    )


def motor(stop: float = 0.05, alvo: float = 0.10) -> PortfolioBacktestEngine:
    return PortfolioBacktestEngine(
        [CompraUmaVez()],
        limites(stop, alvo),
        quote_currency="USDT",
        initial_balance=Decimal("1000"),
        fee_pct=Decimal("0"),
        slippage_pct=Decimal("0"),
        lookback=50,
    )


#: Candles planos em 100. A compra sai no quarto (indice 3, o primeiro apos o
#: aquecimento de 3); os demais existem porque o motor recusa series curtas.
AQUECIMENTO = [candle(i, 100, 100, 100, 100) for i in range(6)]
PRIMEIRO = len(AQUECIMENTO)  # indice do primeiro candle de teste


class TestStopLoss:
    async def test_fires_when_the_low_breaches_it(self):
        """Queda de 8% com stop em 5%: a posicao tem de fechar."""
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 100, 100, 92, 93)]
        r = await motor(stop=0.05).run({"BTC/USDT": candles})
        assert r.exits["stop"] == 1
        saida = next(t for t in r.trades if t.exit_reason == "stop")
        assert saida.price == pytest.approx(Decimal("95"))

    async def test_does_not_fire_above_the_level(self):
        """Queda de 3% com stop em 5%: a posicao continua aberta."""
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 100, 100, 97, 98)]
        r = await motor(stop=0.05).run({"BTC/USDT": candles})
        assert r.exits["stop"] == 0

    async def test_a_gap_fills_at_the_open_not_at_the_level(self):
        """Abriu a 80 com stop em 95: executa a 80.

        Executar no nivel do stop seria creditar ao sistema um preco que nunca
        existiu naquele candle. E exatamente assim que stops machucam de verdade.
        """
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 80, 82, 78, 81)]
        r = await motor(stop=0.05).run({"BTC/USDT": candles})
        saida = next(t for t in r.trades if t.exit_reason == "stop")
        assert saida.price == pytest.approx(Decimal("80"))

    async def test_tighter_stop_fires_earlier(self):
        """O parametro precisa MUDAR o resultado -- era o que nao acontecia."""
        candles = [
            *AQUECIMENTO,
            candle(PRIMEIRO, 100, 100, 96, 97),
            candle(PRIMEIRO + 1, 97, 98, 90, 91),
        ]
        # Mesmo alvo nos dois (e longe demais para disparar) para isolar o stop:
        # `take_profit` tem de ser maior que `stop_loss`, senao a validacao
        # recusa a configuracao.
        apertado = await motor(stop=0.03, alvo=0.30).run({"BTC/USDT": candles})
        largo = await motor(stop=0.15, alvo=0.30).run({"BTC/USDT": candles})
        assert apertado.exits["stop"] == 1
        assert largo.exits["stop"] == 0
        assert apertado.total_return_pct != largo.total_return_pct


class TestTakeProfit:
    async def test_fires_when_the_high_reaches_it(self):
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 100, 112, 100, 110)]
        r = await motor(alvo=0.10).run({"BTC/USDT": candles})
        assert r.exits["alvo"] == 1
        saida = next(t for t in r.trades if t.exit_reason == "alvo")
        assert saida.price == pytest.approx(Decimal("110"))

    async def test_a_favourable_gap_gets_no_credit(self):
        """Abriu a 130 com alvo em 110: executa em 110, nao em 130.

        Assimetria deliberada em relacao ao gap contrario do stop: o backtest
        erra sempre para o lado pessimista.
        """
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 130, 135, 128, 132)]
        r = await motor(alvo=0.10).run({"BTC/USDT": candles})
        saida = next(t for t in r.trades if t.exit_reason == "alvo")
        assert saida.price == pytest.approx(Decimal("110"))


class TestBothInTheSameCandle:
    async def test_the_stop_wins(self):
        """O OHLC nao diz qual preco veio primeiro.

        Supor o alvo seria escolher o desfecho bom com base em informacao que
        nao existe -- e um backtest de risco nao pode errar para o otimista.
        """
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 100, 115, 90, 100)]
        r = await motor(stop=0.05, alvo=0.10).run({"BTC/USDT": candles})
        assert r.exits["stop"] == 1
        assert r.exits["alvo"] == 0


class TestDrawdownIsNowBounded:
    async def test_the_stop_limits_the_fall(self):
        """A razao de tudo isto: sem stop a posicao ia ao fundo do periodo.

        A serie cai 45% de forma continua. Com stop em 5% a perda fica perto
        disso; sem protecao, o prejuizo seria a queda inteira.
        """
        queda = [candle(PRIMEIRO + i, 100 - i * 5, 100 - i * 5, 95 - i * 5, 95 - i * 5)
                 for i in range(9)]
        r = await motor(stop=0.05).run({"BTC/USDT": AQUECIMENTO + queda})
        assert r.exits["stop"] == 1
        # Sem protecao a carteira teria seguido a queda de 45%; com stop em 5%
        # o prejuizo fica em uma fracao disso.
        assert r.total_return_pct > -0.10, r.total_return_pct
        assert r.max_drawdown_pct < 0.10, r.max_drawdown_pct


class TestBookkeeping:
    async def test_the_exit_is_recorded_as_a_closed_trade(self):
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 100, 100, 92, 93)]
        r = await motor(stop=0.05).run({"BTC/USDT": candles})
        fechadas = r.closed_trades
        assert len(fechadas) == 1
        assert fechadas[0].realized_pnl is not None
        assert fechadas[0].realized_pnl < 0

    async def test_the_position_is_fully_closed(self):
        """Meia posicao sobrando viraria exposicao invisivel."""
        candles = [
            *AQUECIMENTO,
            candle(PRIMEIRO, 100, 100, 92, 93),
            candle(PRIMEIRO + 1, 93, 94, 92, 93),
        ]
        r = await motor(stop=0.05).run({"BTC/USDT": candles})
        # Nada mais a vender: uma segunda passagem nao gera nova saida.
        assert r.exits["stop"] == 1

    async def test_no_exit_without_a_position(self):
        """Serie que so cai, sem compra nenhuma: nada a proteger."""

        class NuncaCompra(Strategy):
            name = "nunca"
            description = "teste"
            min_candles = 3

            def evaluate(self, market):
                return None

        engine = PortfolioBacktestEngine(
            [NuncaCompra()], limites(0.05, 0.10),
            quote_currency="USDT", initial_balance=Decimal("1000"),
            fee_pct=Decimal("0"), slippage_pct=Decimal("0"), lookback=50,
        )
        r = await engine.run({"BTC/USDT": [*AQUECIMENTO, candle(PRIMEIRO, 100, 100, 50, 55)]})
        assert r.exits["stop"] == 0
        assert r.total_return_pct == 0.0
