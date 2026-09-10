"""Testes das estrategias.

O foco nao e "a estrategia da lucro" -- isso o backtest mede. O foco e o
contrato: nunca decidir com dados insuficientes, nunca operar sobre candle em
formacao, e emitir sinal exatamente nas condicoes documentadas.
"""

from __future__ import annotations

import ast
import inspect
import pkgutil
from typing import ClassVar
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from helpers import make_candles
from structlog.testing import capture_logs

import crypto_traders.strategies as strategies_package
from crypto_traders.agents import strategy as strategy_agent_module
from crypto_traders.bus import Topics
from crypto_traders.domain.enums import SignalDirection
from crypto_traders.domain.models import Signal
from crypto_traders.indicators import (
    bollinger_bands,
    crossed_above,
    ema,
    macd,
    rsi,
    rsi_detail,
    sma,
)
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
from crypto_traders.strategies.base import MIN_WINDOW_WIDTH


def market_walk(size: int, seed: int = 20260909) -> list[float]:
    """Serie com altas E baixas, do tamanho pedido.

    Reta de `np.linspace` nao serve para testar aquecimento nem sobrevenda: ela
    e monotona, e mercado nenhum e. Foi justamente a monotonia que escondia o
    defeito da janela de um lado so.
    """
    rng = np.random.default_rng(seed)
    return list(100.0 + rng.normal(0, 1.5, size).cumsum())


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
        # O cruzamento ocorre alguns candles antes do fim; varremos a serie
        # barra a barra, como o agente faz. O `or` que estava aqui deixava o
        # teste passar mesmo sem cruzamento nenhum.
        found = _first_signal(strategy, closes, SignalDirection.LONG)
        assert found is not None
        assert found.indicators.values["ma_fast"] > found.indicators.values["ma_slow"]

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
        # Sem os dois sinais o teste nao mede nada -- o `if` que estava aqui
        # fazia ele passar em silencio quando um dos dois nao saia.
        assert gentle is not None and sharp is not None
        assert sharp.confidence > gentle.confidence

    def test_rejects_fast_greater_than_slow(self):
        with pytest.raises(ValueError, match="fast < slow"):
            MovingAverageCrossover(fast=21, slow=9)


class TestRsiReversion:
    def test_emits_long_when_leaving_oversold_not_when_entering(self):
        """A distincao evita comprar repetidamente no meio de uma queda forte.

        A queda tem repiques de proposito. `np.linspace` puro e monotono, e
        janela monotona nao e mercado: ela deixa `avg_gain` exatamente zero e
        cai no caso degenerado que `TestDegenerateWindowRefusesToBuy` cobre.
        """
        strategy = RsiReversion(period=14)
        rng = np.random.default_rng(31)
        falling = [100.0 - i * 1.0 + rng.normal(0, 0.7) for i in range(40)]
        recovering = [falling[-1] + i * 2.2 for i in range(1, 11)]

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
        rng = np.random.default_rng(31)
        falling = [100.0 - i * 1.0 + rng.normal(0, 0.7) for i in range(40)]
        closes = falling + [falling[-1] + i * 2.2 for i in range(1, 11)]
        signal = _first_signal(strategy, closes, SignalDirection.LONG)
        assert signal is not None
        assert "rsi" in signal.indicators.values
        assert "rsi_previous" in signal.indicators.values

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


class TestDeclaredWarmupIsTheRealWarmup:
    """`min_candles` tem que cobrir o aquecimento de verdade de cada estrategia.

    Aquecimento subdeclarado nao levanta erro: ele deixa a estrategia decidir
    com indicador NaN ou com a janela incompleta, e o sinal sai (ou deixa de
    sair) sem nada no log. Por isso a prova aqui e sobre as DUAS ultimas barras:
    toda estrategia compara a leitura atual com a anterior -- cruzamento, saida
    de sobrevenda, reingresso na banda --, e uma barra anterior NaN faz o
    detector de cruzamento se calar por construcao (`crossed_above` exige
    `shift(1)` valido). Se `min_candles` fosse curto, a estrategia estaria muda
    exatamente na primeira barra em que deveria poder falar.
    """

    @staticmethod
    def _pairs_read_by(strategy, frame: pd.DataFrame) -> dict[str, pd.Series]:
        """As series que cada estrategia consulta em `evaluate`."""
        close = frame["close"]
        if isinstance(strategy, MovingAverageCrossover):
            average = ema if strategy.use_ema else sma
            return {
                "ma_fast": average(close, strategy.fast),
                "ma_slow": average(close, strategy.slow),
            }
        if isinstance(strategy, RsiReversion):
            return {"rsi": rsi(close, strategy.period)}
        if isinstance(strategy, MacdTrend):
            result = macd(close, strategy.fast, strategy.slow, strategy.signal_period)
            return {
                "macd": result.macd,
                "macd_signal": result.signal,
                "ema_trend": ema(close, strategy.trend_period),
            }
        if isinstance(strategy, BollingerReversion):
            bands = bollinger_bands(close, strategy.period, strategy.std_multiplier)
            return {"percent_b": bands.percent_b}
        raise AssertionError(f"estrategia sem mapeamento no teste: {strategy.name}")

    @pytest.mark.parametrize(
        "strategy",
        [
            # Os padroes de fabrica, que sao o que `build_strategies` instancia.
            MovingAverageCrossover(fast=9, slow=21),
            RsiReversion(period=14),
            MacdTrend(),
            BollingerReversion(period=20),
            # E parametrizacoes de canto, porque a interface web pode mandar
            # qualquer periodo: a formula de `min_candles` tem que cobrir todas,
            # nao so a combinacao que alguem testou uma vez.
            MovingAverageCrossover(fast=1, slow=2),
            MovingAverageCrossover(fast=3, slow=10),
            MovingAverageCrossover(fast=50, slow=200),
            MovingAverageCrossover(fast=3, slow=10, use_ema=False),
            RsiReversion(period=2),
            RsiReversion(period=50),
            MacdTrend(trend_period=5),
            MacdTrend(fast=2, slow=3, signal_period=50, trend_period=5),
            MacdTrend(trend_period=200),
            BollingerReversion(period=2),
            BollingerReversion(period=50),
        ],
        ids=lambda s: f"{s.name}-{s.min_candles}",
    )
    def test_at_exactly_min_candles_every_series_it_reads_is_ready(self, strategy):
        closes = market_walk(strategy.min_candles)
        frame = MarketFrame.from_candles(make_candles(closes)).frame
        assert len(frame) == strategy.min_candles

        for label, series in self._pairs_read_by(strategy, frame).items():
            assert not pd.isna(series.iloc[-1]), f"{strategy.name}: {label} NaN na barra atual"
            assert not pd.isna(series.iloc[-2]), f"{strategy.name}: {label} NaN na barra anterior"

    @pytest.mark.parametrize(
        "strategy",
        [
            MovingAverageCrossover(fast=9, slow=21),
            RsiReversion(period=14),
            MacdTrend(),
            BollingerReversion(period=20),
        ],
        ids=lambda s: s.name,
    )
    def test_one_candle_short_of_min_candles_refuses_to_decide(self, strategy):
        closes = market_walk(strategy.min_candles - 1)
        market = MarketFrame.from_candles(make_candles(closes))
        assert strategy.evaluate(market) is None

    @pytest.mark.parametrize(
        "strategy",
        [
            MovingAverageCrossover(fast=9, slow=21),
            RsiReversion(period=14),
            MacdTrend(),
            BollingerReversion(period=20),
        ],
        ids=lambda s: s.name,
    )
    def test_a_single_candle_is_never_a_signal(self, strategy):
        """Par recem-listado, banco recem-limpo: uma barra e nenhuma decisao."""
        market = MarketFrame.from_candles(make_candles([100.0]))
        assert market.size == 1
        assert strategy.evaluate(market) is None


class TestD9BuysOnTheWayOutOfTheZone:
    """D9: compra na SAIDA da zona extrema, nunca dentro dela.

    A prova nao e ler o `if`: e varrer uma serie longa barra a barra, como o
    agente faz, e conferir o indicador GRAVADO em cada sinal emitido. Se algum
    dia o `<=` virar `<`, ou a comparacao trocar de lado, isto quebra.
    """

    def test_rsi_reversion_never_buys_with_the_rsi_still_inside_oversold(self):
        strategy = RsiReversion(period=14, oversold=30.0, overbought=70.0)
        longs = _all_signals(strategy, market_walk(600), SignalDirection.LONG)
        assert longs, "a serie precisa produzir compras para o teste valer algo"
        for signal in longs:
            values = signal.indicators.values
            assert values["rsi"] > 30.0, "comprou com RSI ainda dentro da sobrevenda"
            assert values["rsi_previous"] <= 30.0, "comprou sem ter estado sobrevendido"

    def test_rsi_reversion_only_closes_on_the_way_out_of_overbought(self):
        strategy = RsiReversion(period=14, oversold=30.0, overbought=70.0)
        flats = _all_signals(strategy, market_walk(600), SignalDirection.FLAT)
        assert flats
        for signal in flats:
            values = signal.indicators.values
            assert values["rsi"] < 70.0
            assert values["rsi_previous"] >= 70.0

    def test_bollinger_only_buys_after_the_price_is_back_above_the_lower_band(self):
        strategy = BollingerReversion(period=20)
        longs = _all_signals(strategy, market_walk(600), SignalDirection.LONG)
        assert longs
        for signal in longs:
            # %B > 0 e, por definicao, preco acima da banda inferior: o
            # reingresso ja aconteceu. Comprar em %B <= 0 seria comprar dentro.
            assert signal.indicators.values["percent_b"] > 0.0

    def test_ma_crossover_only_buys_with_the_cross_already_confirmed(self):
        strategy = MovingAverageCrossover(fast=3, slow=10)
        longs = _all_signals(strategy, market_walk(600), SignalDirection.LONG)
        assert longs
        for signal in longs:
            values = signal.indicators.values
            assert values["ma_fast"] > values["ma_slow"]

    def test_macd_never_buys_below_its_trend_filter(self):
        strategy = MacdTrend(trend_period=50)
        longs = _all_signals(strategy, market_walk(600), SignalDirection.LONG)
        assert longs
        for signal in longs:
            assert float(signal.reference_price) > signal.indicators.values["ema_trend"]


class TestDegenerateWindowRefusesToBuy:
    """Par morto + um centavo produzia LONG com a confianca MAXIMA (0,90).

    Medido em 2026-09-09, na cotacao em USDC: 40 fechamentos diarios identicos,
    um tique de -0,01%, a volta ao mesmo preco. A cadeia era
    RSI 100 -> 0 -> 51,9, e "0 <= 30 < 51,9" satisfazia a regra de saida da
    sobrevenda com `depth` no teto. Com 20% do capital por ordem e selecao de
    lote por confianca, esse sinal ganharia de qualquer sinal legitimo do dia.
    """

    DEAD_PAIR = [100.0] * 40 + [99.99, 100.0]

    def test_the_reading_that_used_to_fool_the_rule_is_still_there(self):
        """A guarda nao funciona por acidente: a regra de zona ainda casa.

        Sem esta parte, o teste seguinte poderia passar porque o RSI mudou de
        valor, e nao porque a estrategia passou a recusar.
        """
        values = rsi(pd.Series(self.DEAD_PAIR), 14)
        previous, current = values.iloc[-2], values.iloc[-1]
        assert previous == pytest.approx(0.0)
        assert current == pytest.approx(51.85, abs=0.01)
        assert previous <= 30.0 < current  # a regra de D9, satisfeita

    def test_refuses_the_buy(self):
        strategy = RsiReversion(period=14)
        assert _all_signals(strategy, self.DEAD_PAIR, SignalDirection.LONG) == []

    def test_says_in_the_log_why_it_refused(self):
        """Recusa calada e indistinguivel de estrategia que parou de funcionar.

        Sem esta linha, quem olhasse o log veria `rsi_reversion` emitir nada
        por semanas e nao teria como saber se era o dado ou um bug.
        """
        strategy = RsiReversion(period=14)
        candles = make_candles(self.DEAD_PAIR)
        with capture_logs() as registros:
            for end in range(strategy.min_candles, len(candles) + 1):
                strategy.evaluate(MarketFrame.from_candles(candles[:end]))

        recusas = [r for r in registros if r["event"] == "estrategia.janela_estreita"]
        assert recusas, "recusou a compra sem dizer no log"
        assert recusas[0]["strategy"] == "rsi_reversion"
        assert recusas[0]["medida"] == "rsi.relative_width"
        # O numero medido e o piso contra o qual ele foi comparado, os dois no
        # log: sem eles a linha nao permite auditar a decisao depois.
        assert recusas[0]["largura"] < recusas[0]["piso"]
        assert recusas[0]["rsi_anterior"] == pytest.approx(0.0)
        assert "compra recusada" in recusas[0]["motivo"]

    def test_the_confidence_that_would_have_been_emitted_was_the_maximum(self):
        """Documenta o tamanho do estrago evitado, em numero."""
        strategy = RsiReversion(period=14, oversold=30.0)
        depth = (strategy.oversold - 0.0) / strategy.oversold
        assert 0.60 + min(0.30, depth) == pytest.approx(0.90)

    def test_still_closes_a_position_from_a_one_sided_window(self):
        """A guarda vale so para a COMPRA -- fechar nunca pode ser bloqueado.

        Mesma regra do circuit breaker (D4): duvida sobre a qualidade do dado
        recusa abrir exposicao, nunca recusa encerrar. Aqui a janela e de um
        lado so (so altas -> RSI 100) e a estrategia ainda emite FLAT.
        """
        strategy = RsiReversion(period=14)
        # 41 barras so de alta -> `avg_loss` exatamente zero -> RSI 100, janela
        # de um lado so. Uma queda de 20 na barra seguinte leva o RSI a 39,4.
        closes = [*(100.0 + i for i in range(41)), 120.0]
        reading = rsi_detail(pd.Series(closes), 14)
        assert bool(reading.one_sided.iloc[-2])  # a janela E degenerada
        assert reading.value.iloc[-2] == pytest.approx(100.0)

        flats = _all_signals(strategy, closes, SignalDirection.FLAT)
        assert flats, "a estrategia deixou de conseguir fechar posicao"
        assert flats[0].indicators.values["rsi_previous"] == pytest.approx(100.0)
        assert flats[0].indicators.values["rsi"] == pytest.approx(39.39, abs=0.01)

    def test_a_legitimate_oversold_exit_still_fires(self):
        """A guarda nao pode calar a estrategia num mercado de verdade.

        Queda com repiques (nao uma reta) seguida de recuperacao: a janela tem
        alta e baixa, e a compra sai.
        """
        strategy = RsiReversion(period=14)
        rng = np.random.default_rng(7)
        falling = [100.0 - i * 1.5 + rng.normal(0, 0.8) for i in range(40)]
        recovering = [falling[-1] + i * 2.0 for i in range(1, 12)]
        longs = _all_signals(strategy, falling + recovering, SignalDirection.LONG)
        assert longs
        assert longs[0].indicators.values["rsi"] > 30.0

    @pytest.mark.parametrize(
        "strategy",
        [
            MovingAverageCrossover(fast=3, slow=10),
            RsiReversion(period=14),
            MacdTrend(trend_period=50),
            BollingerReversion(period=20),
        ],
        ids=lambda s: s.name,
    )
    def test_a_perfectly_flat_market_never_produces_a_buy(self, strategy):
        """Sem movimento nao ha nada a reverter nem tendencia a seguir."""
        closes = [100.0] * max(strategy.min_candles + 20, 130)
        assert _all_signals(strategy, closes, SignalDirection.LONG) == []


#: Par plano ao ultimo bit, com UM tique de alta de 1e-08 no meio da janela.
#: Foi assim que a guarda anterior (`avg_gain == 0.0`, aritmetica exata) caiu:
#: o tique tira `avg_gain` do zero exato (vira 1,62e-10), `one_sided` fica False
#: e a compra de confianca 0,90 volta inteira sobre uma janela sem informacao.
UM_TIQUE = (
    ["100.00000000"] * 20
    + ["100.00000001"]
    + ["100.00000000"] * 19
    + ["99.99000000", "100.00000000"]
)

#: O mesmo em precisao de altcoin real (0,00123000 USDC, 8 decimais). Pior,
#: porque aqui o RSI anterior le 13,9 e nao 0: nem o log denuncia.
UM_TIQUE_ALTCOIN = (
    ["0.00123000"] * 18
    + ["0.00123001"]
    + ["0.00123000"] * 21
    + ["0.00122999", "0.00123000"]
)


class TestTheWidthFloorIsRelativeAndNotExact:
    """A recusa de janela morta compara com o PRECO, nao com zero.

    A guarda da rodada anterior era `avg_gain == 0.0`. Canto exato tem medida
    zero: um tique de 1e-08 em qualquer barra o contorna, e a compra volta com a
    MESMA confianca maxima. Par sem liquidez de verdade tem movimento minusculo,
    nao movimento zero -- entao o criterio tem que ser relativo.
    """

    def test_the_old_exact_guard_really_does_not_see_the_one_tick_window(self):
        """Contraprova: `one_sided` e False, e a regra de D9 esta satisfeita.

        Sem esta parte, os testes seguintes poderiam passar por outro motivo
        ("a serie tem mercado de verdade", ou "o RSI mudou de valor") em vez de
        por causa da recusa nova.
        """
        reading = rsi_detail(pd.Series([float(v) for v in UM_TIQUE]), 14)
        avg_gain = float(reading.avg_gain.iloc[-2])
        assert 0.0 < avg_gain < 1e-8  # deixou de ser zero, sem ficar informativo
        assert not bool(reading.one_sided.iloc[-2])
        assert not bool(reading.one_sided.iloc[-1])
        assert reading.value.iloc[-2] <= 30.0 < reading.value.iloc[-1]

        # E a largura RELATIVA enxerga o que a exata nao enxergava.
        largura = float(reading.relative_width(pd.Series([float(v) for v in UM_TIQUE])).iloc[-2])
        assert largura < MIN_WINDOW_WIDTH / 10

    @pytest.mark.parametrize("closes", [UM_TIQUE, UM_TIQUE_ALTCOIN], ids=["100", "altcoin"])
    def test_the_one_tick_window_no_longer_buys(self, closes):
        assert _all_signals(RsiReversion(period=14), closes, SignalDirection.LONG) == []

    def test_the_altcoin_case_is_the_one_the_log_would_not_have_denounced(self):
        """Fixa o agravante em numero: o RSI anterior le 13,9, nao 0.

        Uma inspecao humana do log leria "RSI saiu de sobrevenda profunda
        (13,9 -> 51,2)" como sinal legitimo. Nao ha valor de RSI que denuncie
        esta janela -- so a largura relativa denuncia.
        """
        serie = pd.Series([float(v) for v in UM_TIQUE_ALTCOIN])
        reading = rsi_detail(serie, 14)
        assert float(reading.value.iloc[-2]) == pytest.approx(13.92, abs=0.05)
        assert not bool(reading.one_sided.iloc[-2])
        assert float(reading.relative_width(serie).iloc[-2]) == pytest.approx(8.2e-07, rel=0.1)

    # ------------------------------------------------------------------
    #: Uma janela morta por estrategia, com o movimento que ela usa como
    #: gatilho de COMPRA. Serve para provar a recusa nas quatro, e nao so nas
    #: duas de reversao que o ataque original mirou.
    JANELAS_MORTAS: ClassVar[dict[str, list[str]]] = {
        "ma_crossover": ["100.00000000" if i % 4 < 2 else "100.00000001" for i in range(180)],
        "rsi_reversion": UM_TIQUE,
        "macd_trend": ["0.00123000"] * 150 + ["0.00123001"] * 18,
        "bollinger_reversion": ["100.00"] * 110 + ["99.99", "100.00"],
    }

    @staticmethod
    def _instancia(nome: str, **extra):
        return {
            "ma_crossover": lambda: MovingAverageCrossover(fast=9, slow=21, **extra),
            "rsi_reversion": lambda: RsiReversion(period=14, **extra),
            "macd_trend": lambda: MacdTrend(**extra),
            "bollinger_reversion": lambda: BollingerReversion(period=20, **extra),
        }[nome]()

    @pytest.mark.parametrize("nome", sorted(JANELAS_MORTAS))
    def test_the_guard_acts_and_is_the_only_reason_the_buy_disappears(self, nome):
        """A prova de que a protecao AGE, nao de que o campo esta preenchido.

        A MESMA serie e avaliada duas vezes pela MESMA estrategia, mudando so o
        piso: com o piso de producao nao sai compra nenhuma; com o piso em zero
        (a guarda desligada) as compras voltam. Se a recusa nao fosse a causa,
        os dois lados dariam o mesmo resultado e o teste passaria a nao medir
        nada -- por isso o lado desligado tambem e afirmado.
        """
        closes = self.JANELAS_MORTAS[nome]
        com_guarda = _all_signals(self._instancia(nome), closes, SignalDirection.LONG)
        sem_guarda = _all_signals(
            self._instancia(nome, min_window_width=0.0), closes, SignalDirection.LONG
        )
        assert sem_guarda, f"{nome}: a serie nao ataca nada; o teste nao mede a guarda"
        assert com_guarda == [], (
            f"{nome} comprou janela morta: "
            + ", ".join(f"conf={s.confidence:.2f}" for s in com_guarda)
        )

    @pytest.mark.parametrize("nome", sorted(JANELAS_MORTAS))
    def test_the_refusal_is_logged_with_the_measured_number(self, nome):
        """Recusa calada e indistinguivel de estrategia que parou de funcionar."""
        strategy = self._instancia(nome)
        candles = make_candles([float(c) for c in self.JANELAS_MORTAS[nome]])
        with capture_logs() as registros:
            for end in range(strategy.min_candles, len(candles) + 1):
                strategy.evaluate(MarketFrame.from_candles(candles[:end]))

        recusas = [r for r in registros if r["event"] == "estrategia.janela_estreita"]
        assert recusas, f"{nome} recusou sem dizer no log"
        assert {r["strategy"] for r in recusas} == {nome}
        assert all(r["largura"] < r["piso"] for r in recusas)
        assert all("fechamento livre" in r["motivo"] for r in recusas)

    # ------------------------------------------------------------------
    #: Janela igualmente morta, mas com o movimento que dispara o FECHAMENTO.
    FECHAMENTOS_EM_JANELA_MORTA: ClassVar[dict[str, list[str]]] = {
        "ma_crossover": ["100.00000000" if i % 4 < 2 else "100.00000001" for i in range(180)],
        # RSI: 45 barras planas -> `avg_loss` zero -> RSI 100; um tique de baixa
        # -> RSI 0. "100 >= 70 > 0" e a regra de fechamento, numa janela cuja
        # largura relativa e 1e-11. Tem que sair FLAT mesmo assim.
        "rsi_reversion": ["100.00000000"] * 45 + ["99.99999999"],
        "macd_trend": ["100.00000000" if i % 4 < 2 else "100.00000001" for i in range(180)],
        "bollinger_reversion": ["100.00"] * 70 + ["100.01", "100.00"],
    }

    @pytest.mark.parametrize("nome", sorted(FECHAMENTOS_EM_JANELA_MORTA))
    def test_closing_a_position_is_never_blocked_by_a_narrow_window(self, nome):
        """D4: a duvida sobre o dado recusa ABRIR, jamais recusa SAIR.

        Mesma assimetria do circuit breaker. Uma guarda que calasse o FLAT
        prenderia posicao aberta num par que parou de negociar, que e o oposto
        do estado seguro.
        """
        flats = _all_signals(
            self._instancia(nome), self.FECHAMENTOS_EM_JANELA_MORTA[nome], SignalDirection.FLAT
        )
        assert flats, f"{nome} perdeu a capacidade de fechar posicao em janela estreita"

    # ------------------------------------------------------------------
    def test_the_floor_sits_inside_the_measured_empty_band(self):
        """Fixa a medicao que sustenta o numero (regra 2: tres janelas).

        Medido em 2026-09-09 sobre as 34 series do banco em tres janelas
        temporais disjuntas (ver `MIN_WINDOW_WIDTH`): a pior janela degenerada
        atacada mede 8,72e-05 e o menor gatilho real mede 4,23e-04. O piso tem
        que ficar ESTRITAMENTE dentro dessa banda vazia -- encostar em qualquer
        das pontas e perder a folga que faz a escolha nao ser curva ajustada.
        """
        pior_degenerada = 8.7178e-05  # bandwidth do par morto + um centavo
        menor_gatilho_real = 4.2254e-04  # ma_crossover, janela 167-333
        assert pior_degenerada < MIN_WINDOW_WIDTH < menor_gatilho_real
        assert MIN_WINDOW_WIDTH / pior_degenerada > 2.0
        assert menor_gatilho_real / MIN_WINDOW_WIDTH > 2.0

    @pytest.mark.parametrize("nome", sorted(JANELAS_MORTAS))
    def test_a_real_market_is_not_silenced(self, nome):
        """A recusa nao pode calar as quatro num mercado de verdade.

        Contraparte obrigatoria: uma guarda que recusasse tudo tambem passaria
        em todos os testes acima.
        """
        strategy = self._instancia(nome)
        closes = market_walk(max(strategy.min_candles + 260, 400), seed=11)
        with capture_logs() as registros:
            sinais = _all_signals(strategy, closes, None)
        assert sinais, f"{nome} ficou muda num mercado de verdade"
        assert not [r for r in registros if r["event"] == "estrategia.janela_estreita"], (
            f"{nome} recusou janela de mercado real por largura"
        )

    def test_a_nan_width_refuses_the_buy(self):
        """Largura NaN e "nao sei medir", e nao sei nao autoriza abrir."""
        strategy = RsiReversion(period=14)
        assert strategy._window_is_too_narrow(
            _dead_market(), float("nan"), medida="teste"
        ), "NaN passou pela guarda"


class TestZeroVolumeCandle:
    """Volume zero e candle sem negocio: preco parado, nao preco novo.

    Nenhuma das quatro estrategias le volume -- o teste fixa isso, para que uma
    quinta estrategia que passe a ler volume nao herde silenciosamente uma
    divisao por zero.
    """

    @pytest.mark.parametrize(
        "strategy",
        [
            MovingAverageCrossover(fast=3, slow=10),
            RsiReversion(period=14),
            MacdTrend(trend_period=50),
            BollingerReversion(period=20),
        ],
        ids=lambda s: s.name,
    )
    def test_volume_zero_changes_nothing_and_raises_nothing(self, strategy):
        from decimal import Decimal

        closes = market_walk(max(strategy.min_candles + 30, 140))
        normal = make_candles(closes)
        without_volume = [c.model_copy(update={"volume": Decimal("0")}) for c in normal]

        with_volume = _all_signals(strategy, closes, None)

        zeroed = []
        for end in range(strategy.min_candles, len(without_volume) + 1):
            signal = strategy.evaluate(MarketFrame.from_candles(without_volume[:end]))
            if signal is not None:
                zeroed.append(signal)

        assert with_volume, "sem sinal nenhum a comparacao nao mede nada"
        assert [(s.direction, round(s.confidence, 9)) for s in zeroed] == [
            (s.direction, round(s.confidence, 9)) for s in with_volume
        ]


class TestCorruptedFrameNeverBecomesAPlausibleNumber:
    """Furo no dado tem que virar "nao decido", nunca um numero calculado.

    O caminho vivo esta selado na fronteira: `Candle` recusa `NaN`/`Infinity` na
    validacao, entao o adaptador nao consegue entregar furo por ali. Mas
    `MarketFrame` tambem e construido direto de `DataFrame` no backtest de
    carteira (`backtest/portfolio.py`), e por ali um furo entra. O teste cobre
    os dois lados.
    """

    def test_candle_rejects_non_finite_values_at_the_boundary(self):
        from datetime import UTC, datetime
        from decimal import Decimal

        from pydantic import ValidationError

        from crypto_traders.domain.enums import ExchangeName
        from crypto_traders.domain.models import Candle

        for bad in ("NaN", "Infinity", "-Infinity"):
            with pytest.raises(ValidationError):
                Candle(
                    exchange=ExchangeName.BINANCE,
                    symbol="BTC/USDC",
                    timeframe="1d",
                    open_time=datetime(2026, 1, 1, tzinfo=UTC),
                    open=Decimal(bad),
                    high=Decimal(bad),
                    low=Decimal(bad),
                    close=Decimal(bad),
                    volume=Decimal("1"),
                )

    @pytest.mark.parametrize(
        "strategy",
        [
            MovingAverageCrossover(fast=3, slow=10),
            RsiReversion(period=14),
            MacdTrend(trend_period=50),
        ],
        ids=lambda s: s.name,
    )
    def test_a_hole_in_the_frame_gives_no_signal_instead_of_a_wrong_one(self, strategy):
        """Indicador de memoria infinita nao retoma depois do furo: fica NaN.

        E NaN faz a estrategia devolver `None`, que e o estado seguro. Antes da
        correcao em `_smooth`, o furo era costurado e o indicador saia com um
        numero plausivel calculado sobre uma serie que nao existiu.
        """
        size = max(strategy.min_candles + 40, 150)
        frame = MarketFrame.from_candles(make_candles(market_walk(size))).frame
        holed = frame.copy()
        holed.iloc[size // 2, :] = np.nan

        market = MarketFrame(
            exchange=_binance(),
            symbol="BTC/USDC",
            timeframe="1d",
            frame=holed,
        )
        assert strategy.evaluate(market) is None

        # A prova direta: o que a estrategia LE esta NaN. Sem isto o teste
        # passaria so porque a ultima barra nao tinha cruzamento -- e passaria
        # tambem com o furo costurado, que e exatamente o defeito.
        read = TestDeclaredWarmupIsTheRealWarmup._pairs_read_by(strategy, holed)
        assert all(pd.isna(series.iloc[-1]) for series in read.values()), (
            f"{strategy.name} produziu numero apos o furo: "
            f"{ {k: v.iloc[-1] for k, v in read.items()} }"
        )

    def test_a_duplicated_open_time_is_refused_instead_of_averaged(self):
        """`open_time` repetido recusa a janela -- nao devolve numero.

        Historia deste teste, que e a razao de ele existir na forma atual. A
        versao 1 media um `reindex` que estourava so para `rsi`/`atr`: o agente
        registrava `strategy.failed` apenas para as estrategias de RSI e seguia
        verde, operando com menos estrategias do que as configuradas. A correcao
        desse defeito (recursao posicional em `_smooth`) trocou o erro por
        numero, e ai o defeito ficou PIOR: as quatro estrategias avaliavam sem
        reclamar sobre uma serie com uma barra contada duas vezes -- "numero
        plausivel calculado sobre uma serie que nao existiu", que e exatamente a
        definicao usada aqui para classificar o furo de NaN como grave.

        Barra duplicada e dado corrompido, e o estado seguro e nao operar.
        """
        closes = market_walk(140)
        candles = make_candles(closes)
        duplicated = [*candles[:100], candles[99], *candles[100:]]

        with pytest.raises(ValueError, match="open_time repetido"):
            MarketFrame.from_candles(duplicated)

    def test_the_refusal_also_closes_the_dataframe_door(self):
        """A recusa e no `__post_init__`, nao so no `from_candles`.

        O backtest de carteira monta `MarketFrame` direto de `DataFrame`
        (`backtest/portfolio.py:284`). Se a validacao vivesse so no
        `from_candles`, essa porta ficaria aberta -- e era por ela que a
        duplicata entrava no backtest.
        """
        frame = MarketFrame.from_candles(make_candles(market_walk(60))).frame
        duplicated = pd.concat([frame, frame.iloc[[30]]]).sort_index()
        assert not duplicated.index.is_unique

        with pytest.raises(ValueError, match="open_time repetido"):
            MarketFrame(
                exchange=_binance(), symbol="BTC/USDC", timeframe="1d", frame=duplicated
            )

    def test_the_duplicated_bar_flips_the_decision_not_just_the_decimals(self):
        """O estrago nao e arredondamento: e o SINAL mudando de existencia.

        Sem esta parte, a recusa poderia ser defendida como preciosismo ("muda
        0,8 ponto de RSI, e daí"). Os tres casos abaixo foram encontrados
        varrendo seeds em 2026-09-09 e sao a resposta: com uma barra contada
        duas vezes, `ma_crossover` INVENTA um cruzamento que a serie real nao
        teve (seed 8) e APAGA um que ela teve (seed 36).
        """
        for seed, posicao, real, deformado in [(8, 53, False, True), (36, 42, True, False)]:
            closes = market_walk(60, seed=seed)
            corrompida = [*closes[: posicao + 1], closes[posicao], *closes[posicao + 1 :]]
            cruzou = lambda s: bool(  # noqa: E731
                crossed_above(ema(pd.Series(s), 9), ema(pd.Series(s), 21)).iloc[-1]
            )
            assert cruzou(closes) is real
            assert cruzou(corrompida) is deformado, (
                f"seed {seed}: a barra duplicada deixou de mudar a decisao; "
                "o caso precisa ser remedido"
            )

        # E na regra de D9 do RSI: 29,88 -> 30,13 atravessa o limiar de
        # sobrevenda, ou seja o "previous <= 30" para de valer por causa da
        # barra repetida.
        closes = market_walk(60, seed=59)
        corrompida = [*closes[:54], closes[53], *closes[54:]]
        real, deformado = rsi(pd.Series(closes), 14), rsi(pd.Series(corrompida), 14)
        assert float(real.iloc[-2]) == pytest.approx(29.88, abs=0.01)
        assert float(deformado.iloc[-2]) == pytest.approx(30.13, abs=0.01)
        assert (real.iloc[-2] <= 30.0) and not (deformado.iloc[-2] <= 30.0)


class TestTheAgentOnlyPublishesSignals:
    """O Strategy Agent nunca executa ordem: ele so publica `Signal`.

    Duas provas: uma estatica (o pacote nao conhece broker nem execucao) e uma
    dinamica (o unico topico em que ele escreve e `SIGNALS`).
    """

    def test_the_package_imports_nothing_that_can_send_an_order(self):
        """Le os IMPORTS pela AST, nao o texto do arquivo.

        Casar palavra no fonte reprovaria a propria docstring do agente, que
        diz que ele nao conhece o broker. O que interessa e o grafo de
        dependencia: sem `exchanges`, sem `agents.execution`, sem broker
        nenhum, nao existe caminho pelo qual este pacote alcance a exchange --
        toda ordem tem que nascer do Risk Manager.
        """
        forbidden = ("exchanges", "agents.execution", "broker")
        modules = [strategy_agent_module]
        for info in pkgutil.iter_modules(strategies_package.__path__):
            modules.append(__import__(f"crypto_traders.strategies.{info.name}", fromlist=["_"]))

        for module in modules:
            tree = ast.parse(inspect.getsource(module))
            imported: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported += [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    imported.append(base)
                    imported += [f"{base}.{alias.name}" for alias in node.names]
            for name in imported:
                for token in forbidden:
                    assert token not in name.lower(), (
                        f"{module.__name__} importa '{name}', que alcanca a exchange"
                    )

    def test_the_agent_has_no_broker_attribute(self):
        from crypto_traders.bus import InMemoryEventBus
        from crypto_traders.config import Settings

        agent = _strategy_agent(InMemoryEventBus(), Settings(_env_file=None))
        assert not any("broker" in name.lower() for name in vars(agent))
        assert not hasattr(agent, "place_order")
        assert not hasattr(agent, "execute")

    async def test_publishes_only_to_the_signals_topic(self, settings):
        """Varre uma serie inteira pelo `_on_candle` e olha TODO topico escrito.

        Se algum dia alguem fizer o agente publicar em `ORDER_REQUESTS` para
        "economizar um salto", isto quebra -- e esse salto e exatamente o que
        pularia o Risk Manager.
        """
        from crypto_traders.bus import InMemoryEventBus
        from crypto_traders.db.repositories import CandleRepository
        from crypto_traders.db.session import session_scope

        bus = InMemoryEventBus()
        published: list[tuple[str, object]] = []

        async def record(topic, payload):
            published.append((topic, payload))

        agent = _strategy_agent(bus, settings)
        agent.bus.publish = record  # type: ignore[method-assign]

        # Um candle por vez: `CandleRepository.recent` devolve tudo o que esta
        # no banco, entao inserir a serie inteira de uma vez faria toda chamada
        # avaliar a MESMA janela final -- o oposto de varrer a serie.
        candles = make_candles(market_walk(200))
        for candle in candles:
            async with session_scope(settings) as session:
                await CandleRepository(session).upsert_many([candle])
            await agent._on_candle(candle)

        assert published, "o teste precisa ter produzido algum sinal"
        assert {topic for topic, _ in published} == {Topics.SIGNALS}
        assert all(isinstance(payload, Signal) for _, payload in published)

    async def test_a_candle_still_forming_produces_nothing(self, settings):
        from crypto_traders.bus import InMemoryEventBus

        bus = InMemoryEventBus()
        published: list[tuple[str, object]] = []

        async def record(topic, payload):
            published.append((topic, payload))

        agent = _strategy_agent(bus, settings)
        agent.bus.publish = record  # type: ignore[method-assign]

        forming = make_candles(market_walk(200), closed=False)
        await agent._on_candle(forming[-1])
        assert published == []


class TestOneGreedyStrategyDoesNotSilenceTheOthers:
    """Aquecimento e julgado por estrategia, nao pelo maximo de todas.

    Antes, `_required_candles` era o MAX dos `min_candles` e o agente saia sem
    avaliar NENHUMA estrategia quando o simbolo tinha menos candles que esse
    maximo, registrando so em `log.debug` -- invisivel com `LOG_LEVEL=INFO`.
    Habilitar `macd_trend` pela interface eleva a exigencia de 26 para 105
    candles; em 1d, um par novo ficaria meses sem nenhuma estrategia, e o log
    operacional nao diria isso.
    """

    def test_the_coupling_that_caused_it_is_real_and_measured(self):
        """Contraprova em numero: 105 contra 26."""
        quatro = build_strategies(
            ["ma_crossover", "rsi_reversion", "macd_trend", "bollinger_reversion"]
        )
        assert max(s.min_candles for s in quatro) == 105
        assert get_strategy("ma_crossover").min_candles == 26

    async def test_the_ready_strategy_still_decides_and_the_mute_one_is_logged(self, settings):
        """A prova de que AGE: 60 candles, uma estrategia pronta, uma muda.

        60 candles bastam para `ma_crossover` (26) e nao bastam para
        `macd_trend` (105). Antes, o agente nao avaliava nenhuma das duas.
        """
        from crypto_traders.bus import InMemoryEventBus
        from crypto_traders.db.repositories import CandleRepository
        from crypto_traders.db.session import session_scope

        bus = InMemoryEventBus()
        published: list[tuple[str, object]] = []

        async def record(topic, payload):
            published.append((topic, payload))

        agent = strategy_agent_module.StrategyAgent(
            bus,
            [MovingAverageCrossover(fast=3, slow=10), MacdTrend()],
            settings,
        )
        agent.bus.publish = record  # type: ignore[method-assign]

        # Queda longa e alta forte: garante o cruzamento para cima da MA.
        closes = [*(120.0 - i * 0.5 for i in range(45)), *(97.5 + i * 2.0 for i in range(15))]
        candles = make_candles(closes)
        with capture_logs() as registros:
            for candle in candles:
                async with session_scope(settings) as session:
                    await CandleRepository(session).upsert_many([candle])
                await agent._on_candle(candle)

        assert published, "a estrategia com aquecimento pronto ficou muda"
        assert {s.strategy for _, s in published} == {"ma_crossover"}

        avisos = [r for r in registros if r["event"] == "strategy.warmup_incompleto"]
        assert avisos, "ninguem soube que macd_trend estava muda"
        # Duas combinacoes distintas ao longo da serie: primeiro as duas mudas,
        # depois so `macd_trend`. E o estado que interessa e o ultimo.
        assert [tuple(a["mudas"]) for a in avisos] == [
            ("ma_crossover", "macd_trend"),
            ("macd_trend",),
        ]
        assert avisos[-1]["avaliando"] == ["ma_crossover"]
        # Uma linha por combinacao NOVA, nao uma por candle: sao 60 candles e
        # 2 linhas. Log repetitivo vira cegueira -- o watchdog deste projeto ja
        # alertou 1.440 vezes por dia sobre o mesmo agente ocioso.
        assert len(avisos) == 2

    async def test_a_corrupted_window_is_refused_out_loud(self, settings):
        """`open_time` repetido recusa a janela E aparece no log operacional.

        A recusa e o comportamento certo (dado corrompido nao decide dinheiro),
        mas recusa calada faz o agente parecer apenas ocioso.
        """
        from crypto_traders.bus import InMemoryEventBus

        bus = InMemoryEventBus()
        agent = _strategy_agent(bus, settings)
        candles = make_candles(market_walk(60))
        duplicados = [*candles[:40], candles[39], *candles[40:]]

        async def fake_recent(*_args, **_kwargs):
            return duplicados

        with capture_logs() as registros, patch.object(
            strategy_agent_module.CandleRepository, "recent", fake_recent
        ):
            await agent._on_candle(candles[-1])

        recusas = [r for r in registros if r["event"] == "strategy.janela_recusada"]
        assert recusas, "janela corrompida foi descartada em silencio"
        assert "open_time repetido" in recusas[0]["motivo"]


def _binance():
    from crypto_traders.domain.enums import ExchangeName

    return ExchangeName.BINANCE


def _strategy_agent(bus, settings):
    return strategy_agent_module.StrategyAgent(
        bus,
        [
            MovingAverageCrossover(fast=3, slow=10),
            RsiReversion(period=14),
            BollingerReversion(period=20),
        ],
        settings,
    )


# ---------------------------------------------------------------------------
# Auxiliares: percorrem a serie barra a barra, como o agente faz em producao.
# ---------------------------------------------------------------------------
def _dead_market():
    """Janela minima so para carregar simbolo/timeframe ao log de recusa."""
    return MarketFrame.from_candles(make_candles([100.0] * 5))


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
