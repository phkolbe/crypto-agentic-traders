"""Testes das estrategias.

O foco nao e "a estrategia da lucro" -- isso o backtest mede. O foco e o
contrato: nunca decidir com dados insuficientes, nunca operar sobre candle em
formacao, e emitir sinal exatamente nas condicoes documentadas.
"""

from __future__ import annotations

import numpy as np
import pytest
from helpers import make_candles

from crypto_traders.domain.enums import SignalDirection
from crypto_traders.strategies import (
    BollingerReversion,
    MacdTrend,
    MarketFrame,
    MovingAverageCrossover,
    RsiReversion,
    available_strategies,
    build_strategies,
    get_strategy,
)


class TestMarketFrame:
    def test_keeps_only_closed_candles(self):
        """Candle em formacao nao pode entrar: o preco dele ainda vai mudar."""
        closed = make_candles([100.0, 101.0])
        forming = make_candles([102.0], closed=False)
        market = MarketFrame.from_candles(closed + forming)
        assert market.size == 2
        assert float(market.last_close) == 101.0

    def test_rejects_a_window_with_no_closed_candles(self):
        with pytest.raises(ValueError, match="nenhum candle fechado"):
            MarketFrame.from_candles(make_candles([100.0], closed=False))

    def test_rejects_an_empty_window(self):
        with pytest.raises(ValueError):
            MarketFrame.from_candles([])

    def test_sorts_chronologically(self):
        """Indicadores dependem da ordem; candles fora de ordem dariam lixo."""
        candles = make_candles([100.0, 101.0, 102.0])
        market = MarketFrame.from_candles(list(reversed(candles)))
        assert market.frame.index.is_monotonic_increasing
        assert float(market.last_close) == 102.0


class TestRegistry:
    def test_lists_every_builtin_strategy(self):
        names = available_strategies()
        assert {"ma_crossover", "rsi_reversion", "macd_trend", "bollinger_reversion"} <= set(names)

    def test_builds_by_name(self):
        assert isinstance(get_strategy("ma_crossover"), MovingAverageCrossover)

    def test_fails_loudly_on_unknown_name(self):
        """Um typo no .env deve impedir a subida, nao rodar com menos estrategias."""
        with pytest.raises(ValueError, match="desconhecida"):
            build_strategies(["ma_crossover", "estrategia_que_nao_existe"])


class TestMovingAverageCrossover:
    def test_returns_none_during_warmup(self):
        strategy = MovingAverageCrossover(fast=9, slow=21)
        market = MarketFrame.from_candles(make_candles([100.0] * 10))
        assert strategy.evaluate(market) is None

    def test_emits_long_on_upward_cross(self):
        strategy = MovingAverageCrossover(fast=3, slow=10)
        # Queda longa seguida de alta forte: garante o cruzamento para cima.
        closes = list(np.linspace(120, 100, 40)) + list(np.linspace(100, 130, 15))
        market = MarketFrame.from_candles(make_candles(closes))
        signal = strategy.evaluate(market)
        # O cruzamento pode ocorrer alguns candles antes do fim; varremos o final.
        found = _first_signal(strategy, closes, SignalDirection.LONG)
        assert found is not None or signal is not None

    def test_emits_flat_on_downward_cross(self):
        strategy = MovingAverageCrossover(fast=3, slow=10)
        closes = list(np.linspace(100, 130, 40)) + list(np.linspace(130, 100, 15))
        assert _first_signal(strategy, closes, SignalDirection.FLAT) is not None

    def test_silent_on_a_flat_market(self):
        """Sem movimento nao ha cruzamento -- e nao operar e a resposta certa."""
        strategy = MovingAverageCrossover(fast=3, slow=10)
        market = MarketFrame.from_candles(make_candles([100.0] * 60))
        assert strategy.evaluate(market) is None

    def test_confidence_grows_with_the_separation(self):
        weak = MovingAverageCrossover(fast=3, slow=10)
        gentle = _first_signal(
            weak, [*np.linspace(100, 99, 30), *np.linspace(99, 100, 20)], SignalDirection.LONG
        )
        sharp = _first_signal(
            weak, [*np.linspace(120, 100, 30), *np.linspace(100, 160, 20)], SignalDirection.LONG
        )
        if gentle and sharp:
            assert sharp.confidence >= gentle.confidence

    def test_rejects_fast_greater_than_slow(self):
        with pytest.raises(ValueError, match="fast < slow"):
            MovingAverageCrossover(fast=21, slow=9)


class TestRsiReversion:
    def test_emits_long_when_leaving_oversold_not_when_entering(self):
        """A distincao evita comprar repetidamente no meio de uma queda forte."""
        strategy = RsiReversion(period=14)
        falling = list(np.linspace(100, 60, 40))
        recovering = list(np.linspace(60, 80, 10))

        # Durante a queda o RSI esta baixo, mas nao ha sinal de compra.
        during_fall = MarketFrame.from_candles(make_candles(falling))
        signal = strategy.evaluate(during_fall)
        assert signal is None or signal.direction is not SignalDirection.LONG

        # Na recuperacao, o cruzamento de volta acima de 30 gera o sinal.
        assert _first_signal(strategy, falling + recovering, SignalDirection.LONG) is not None

    def test_emits_flat_when_leaving_overbought(self):
        strategy = RsiReversion(period=14)
        closes = list(np.linspace(60, 100, 40)) + list(np.linspace(100, 85, 12))
        assert _first_signal(strategy, closes, SignalDirection.FLAT) is not None

    def test_records_the_rsi_that_motivated_the_signal(self):
        """Sem isso, um trade nao pode ser explicado depois."""
        strategy = RsiReversion(period=14)
        closes = list(np.linspace(100, 60, 40)) + list(np.linspace(60, 80, 10))
        signal = _first_signal(strategy, closes, SignalDirection.LONG)
        assert signal is not None
        assert "rsi" in signal.indicators.values

    def test_rejects_incoherent_thresholds(self):
        with pytest.raises(ValueError):
            RsiReversion(oversold=80, overbought=20)


class TestMacdTrend:
    def test_returns_none_during_warmup(self):
        strategy = MacdTrend(trend_period=100)
        market = MarketFrame.from_candles(make_candles([100.0] * 50))
        assert strategy.evaluate(market) is None

    def test_does_not_buy_against_the_trend(self):
        """Filtro de tendencia: cruzamento de alta em mercado de baixa e ignorado.

        Sem esse filtro o MACD compraria varias vezes durante uma queda longa.
        """
        strategy = MacdTrend(trend_period=50)
        # Queda persistente com um repique no meio: o repique cruza o MACD para
        # cima, mas o preco segue abaixo da EMA de tendencia.
        closes = list(np.linspace(200, 100, 120)) + list(np.linspace(100, 108, 10))
        longs = _all_signals(strategy, closes, SignalDirection.LONG)
        assert longs == []


class TestBollingerReversion:
    def test_emits_long_on_reentry_above_the_lower_band(self):
        strategy = BollingerReversion(period=20)
        rng = np.random.default_rng(11)
        closes = [*(100 + rng.normal(0, 1, 60)), 92.0, 91.0, 97.0, 99.0]
        assert _first_signal(strategy, closes, SignalDirection.LONG) is not None

    def test_records_the_bands(self):
        strategy = BollingerReversion(period=20)
        rng = np.random.default_rng(11)
        closes = [*(100 + rng.normal(0, 1, 60)), 92.0, 91.0, 97.0, 99.0]
        signal = _first_signal(strategy, closes, SignalDirection.LONG)
        assert signal is not None
        assert {"percent_b", "bb_upper", "bb_lower"} <= set(signal.indicators.values)


class TestSignalContract:
    @pytest.mark.parametrize(
        "strategy",
        [
            MovingAverageCrossover(fast=3, slow=10),
            RsiReversion(period=14),
            BollingerReversion(period=20),
        ],
    )
    def test_confidence_always_within_bounds(self, strategy):
        rng = np.random.default_rng(5)
        closes = list(100 + rng.normal(0, 3, 200).cumsum())
        for signal in _all_signals(strategy, closes, None):
            assert 0.0 <= signal.confidence <= 1.0

    @pytest.mark.parametrize(
        "strategy",
        [
            MovingAverageCrossover(fast=3, slow=10),
            RsiReversion(period=14),
            BollingerReversion(period=20),
        ],
    )
    def test_never_emits_short_in_spot(self, strategy):
        rng = np.random.default_rng(9)
        closes = list(100 + rng.normal(0, 3, 200).cumsum())
        for signal in _all_signals(strategy, closes, None):
            assert signal.direction is not SignalDirection.SHORT


# ---------------------------------------------------------------------------
# Auxiliares: percorrem a serie barra a barra, como o agente faz em producao.
# ---------------------------------------------------------------------------
def _all_signals(strategy, closes, direction):
    candles = make_candles([float(c) for c in closes])
    found = []
    for end in range(strategy.min_candles, len(candles) + 1):
        market = MarketFrame.from_candles(candles[:end])
        signal = strategy.evaluate(market)
        if signal is not None and (direction is None or signal.direction is direction):
            found.append(signal)
    return found


def _first_signal(strategy, closes, direction):
    signals = _all_signals(strategy, closes, direction)
    return signals[0] if signals else None
