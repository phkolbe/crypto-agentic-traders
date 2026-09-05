"""Testes dos indicadores tecnicos.

Os valores de referencia vem do dataset original de Wilder (*New Concepts in
Technical Trading Systems*) e de calculos conferidos a mao. Sao estes numeros que
decidem o uso de dinheiro real -- se um deles mudar sem intencao, o teste quebra.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_traders.indicators import (
    atr,
    bollinger_bands,
    crossed_above,
    crossed_below,
    ema,
    macd,
    rma,
    rsi,
    sma,
    true_range,
)

# Serie classica de Wilder, usada para validar o RSI em toda a literatura.
WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
]


@pytest.fixture
def wilder() -> pd.Series:
    return pd.Series(WILDER_CLOSES)


class TestSma:
    def test_matches_manual_average(self):
        series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        assert sma(series, 3).iloc[2] == pytest.approx(2.0)
        assert sma(series, 3).iloc[4] == pytest.approx(4.0)

    def test_warmup_is_nan_not_partial_average(self):
        """Aquecimento devolve NaN, nunca uma media de menos periodos.

        Uma "MA de 200" calculada sobre 12 candles seria um numero plausivel e
        completamente errado -- pior do que nao ter valor nenhum.
        """
        series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = sma(series, 3)
        assert result.iloc[:2].isna().all()
        assert not pd.isna(result.iloc[2])

    def test_rejects_invalid_period(self):
        with pytest.raises(ValueError):
            sma(pd.Series([1.0]), 0)


class TestEma:
    def test_is_seeded_with_simple_average(self):
        """Semente = SMA dos primeiros `period` valores, como no TradingView.

        O `ewm(adjust=False)` do pandas semeia com o PRIMEIRO valor da serie e
        produziria 10.667 aqui -- divergindo do indicador que a exchange desenha.
        """
        series = pd.Series([10.0, 11.0, 12.0, 13.0])
        result = ema(series, 2)
        # semente = SMA(2) = 10.5; alpha = 2/(2+1) = 2/3
        # ema[2] = 12*(2/3) + 10.5*(1/3) = 11.5
        assert result.iloc[1] == pytest.approx(10.5)
        assert result.iloc[2] == pytest.approx(11.5)

    def test_reacts_faster_than_sma_to_a_recent_jump(self):
        """Apos um salto isolado, a EMA ja incorporou mais do movimento."""
        series = pd.Series([10.0] * 20 + [20.0])
        assert ema(series, 5).iloc[-1] > sma(series, 5).iloc[-1]


class TestRma:
    def test_seeds_with_simple_average(self):
        series = pd.Series([2.0, 4.0, 6.0, 8.0])
        result = rma(series, 3)
        assert result.iloc[2] == pytest.approx(4.0)  # media de 2,4,6
        # (4*2 + 8)/3 = 5.333...
        assert result.iloc[3] == pytest.approx(16 / 3)

    def test_returns_all_nan_when_series_shorter_than_period(self):
        assert rma(pd.Series([1.0, 2.0]), 5).isna().all()

    def test_ignores_leading_nan(self):
        """`diff()` e `shift()` produzem NaN inicial; a semente deve pula-lo."""
        with_nan = pd.Series([np.nan, 2.0, 4.0, 6.0])
        without_nan = pd.Series([2.0, 4.0, 6.0])
        assert rma(with_nan, 3).iloc[3] == pytest.approx(rma(without_nan, 3).iloc[2])


class TestRsi:
    def test_matches_wilder_reference(self, wilder):
        """Valores publicados por Wilder para esta serie."""
        result = rsi(wilder, 14)
        assert result.iloc[14] == pytest.approx(70.46, abs=0.01)
        assert result.iloc[15] == pytest.approx(66.25, abs=0.01)
        assert result.iloc[16] == pytest.approx(66.48, abs=0.01)

    def test_warmup_is_nan(self, wilder):
        assert rsi(wilder, 14).iloc[:14].isna().all()

    def test_bounded_between_zero_and_hundred(self, wilder):
        values = rsi(wilder, 14).dropna()
        assert values.between(0, 100).all()

    def test_returns_hundred_when_there_are_no_losses(self):
        """Sem nenhuma perda na janela, RS tende ao infinito e o RSI e 100."""
        series = pd.Series([float(x) for x in range(1, 40)])
        assert rsi(series, 14).iloc[-1] == pytest.approx(100.0)

    def test_low_when_falling(self):
        series = pd.Series([float(x) for x in range(40, 1, -1)])
        assert rsi(series, 14).iloc[-1] < 5.0


class TestMacd:
    def test_macd_is_difference_of_emas(self):
        series = pd.Series(np.linspace(100, 140, 80))
        result = macd(series, 12, 26, 9)
        expected = ema(series, 12) - ema(series, 26)
        assert result.macd.iloc[-1] == pytest.approx(expected.iloc[-1])

    def test_histogram_is_macd_minus_signal(self):
        series = pd.Series(np.linspace(100, 140, 80))
        result = macd(series)
        assert result.histogram.iloc[-1] == pytest.approx(
            result.macd.iloc[-1] - result.signal.iloc[-1]
        )

    def test_positive_in_uptrend(self):
        series = pd.Series(np.linspace(100, 200, 100))
        assert macd(series).macd.iloc[-1] > 0

    def test_rejects_fast_not_less_than_slow(self):
        with pytest.raises(ValueError, match="fast < slow"):
            macd(pd.Series([1.0] * 50), fast=26, slow=12)


class TestBollinger:
    def test_middle_band_is_sma(self):
        series = pd.Series(np.random.default_rng(42).normal(100, 5, 60))
        bands = bollinger_bands(series, 20)
        assert bands.middle.iloc[-1] == pytest.approx(sma(series, 20).iloc[-1])

    def test_uses_population_std_like_trading_platforms(self):
        """`ddof=0`: o padrao do pandas (amostral) daria bandas mais largas."""
        series = pd.Series([float(x) for x in range(1, 21)])
        bands = bollinger_bands(series, 20, 2.0)
        expected = series.std(ddof=0)
        assert (bands.upper.iloc[-1] - bands.middle.iloc[-1]) == pytest.approx(2 * expected)

    def test_percent_b_matches_its_definition(self):
        """%B = (preco - inferior) / (superior - inferior)."""
        series = pd.Series(np.random.default_rng(7).normal(100, 3, 60))
        bands = bollinger_bands(series, 20)
        expected = (series.iloc[-1] - bands.lower.iloc[-1]) / (
            bands.upper.iloc[-1] - bands.lower.iloc[-1]
        )
        assert bands.percent_b.iloc[-1] == pytest.approx(expected)

    def test_percent_b_is_half_at_the_middle_band(self):
        series = pd.Series([float(x) for x in range(1, 21)])
        bands = bollinger_bands(series, 20)
        assert bands.percent_b.iloc[-1] > 0.5  # serie crescente termina acima da media

    def test_constant_series_gives_no_division_by_zero(self):
        """Banda de largura zero nao pode virar inf/NaN silencioso."""
        bands = bollinger_bands(pd.Series([100.0] * 40), 20)
        assert pd.isna(bands.percent_b.iloc[-1])
        assert bands.bandwidth.iloc[-1] == pytest.approx(0.0)


class TestAtr:
    def test_true_range_uses_previous_close(self):
        high = pd.Series([10.0, 12.0])
        low = pd.Series([9.0, 11.0])
        close = pd.Series([9.5, 11.5])
        # TR[1] = max(12-11, |12-9.5|, |11-9.5|) = 2.5
        assert true_range(high, low, close).iloc[1] == pytest.approx(2.5)

    def test_grows_with_volatility(self):
        calm_high = pd.Series([100.0 + i * 0.1 for i in range(40)])
        calm = atr(calm_high, calm_high - 0.5, calm_high, 14).iloc[-1]

        rng = np.random.default_rng(3)
        wild_close = pd.Series(100 + rng.normal(0, 10, 40).cumsum())
        wild = atr(wild_close + 5, wild_close - 5, wild_close, 14).iloc[-1]

        assert wild > calm


class TestCrossings:
    def test_detects_only_the_crossing_bar(self):
        fast = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        slow = pd.Series([3.0, 3.0, 3.0, 3.0, 3.0])
        crosses = crossed_above(fast, slow)
        assert crosses.tolist() == [False, False, False, True, False]

    def test_crossed_below_is_mirror(self):
        fast = pd.Series([5.0, 4.0, 3.0, 2.0, 1.0])
        slow = pd.Series([3.0] * 5)
        assert crossed_below(fast, slow).tolist() == [False, False, False, True, False]

    def test_end_of_warmup_is_not_a_crossing(self):
        """NaN -> numero nao pode ser lido como cruzamento.

        Sem essa guarda, toda estrategia de cruzamento emitiria um sinal falso
        exatamente no primeiro candle em que os indicadores ficam prontos.
        """
        fast = pd.Series([np.nan, np.nan, 5.0, 6.0])
        slow = pd.Series([np.nan, np.nan, 3.0, 3.0])
        assert not crossed_above(fast, slow).iloc[2]
