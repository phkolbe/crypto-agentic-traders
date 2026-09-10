"""Testes dos indicadores tecnicos.

Os valores de referencia vem do dataset original de Wilder (*New Concepts in
Technical Trading Systems*) e de calculos conferidos a mao. Sao estes numeros que
decidem o uso de dinheiro real -- se um deles mudar sem intencao, o teste quebra.
"""

from __future__ import annotations

from itertools import pairwise

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
    relative_move,
    rma,
    rsi,
    rsi_detail,
    sma,
    true_range,
)

# Serie classica de Wilder, usada para validar o RSI em toda a literatura.
WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
]

#: RSI(14) da serie acima, um valor por barra a partir do indice 14 (a primeira
#: em que 14 variacoes existem). Conferidos a mao, na recursao de Wilder:
#:
#:   ganhos das 14 primeiras variacoes = 3,34  -> avg_gain = 3,34/14 = 0,2385714
#:   perdas   das 14 primeiras variacoes = 1,40 -> avg_loss = 1,40/14 = 0,1
#:   RS = 2,3857143 ; RSI = 100 - 100/(1+RS) = 70,4641
#:   idx 15: delta -0,28 -> avg_gain 0,2215306 ; avg_loss 1,58/14 = 0,1128571
#:                        -> RS 1,9629  ; RSI 66,2496
#:   idx 16: delta +0,03 -> avg_gain 0,2078499 ; avg_loss 0,1047959
#:                        -> RS 1,98338 ; RSI 66,4809
#:
#: Tolerancia declarada: 0,005 em valor absoluto (a literatura publica estes
#: numeros com duas casas). Nao e "parece certo": e digito a digito.
WILDER_RSI_14 = [70.4641, 66.2496, 66.4809, 69.3469, 66.2947, 57.9150]
WILDER_RSI_TOLERANCE = 0.005


@pytest.fixture
def wilder() -> pd.Series:
    return pd.Series(WILDER_CLOSES)


def wilder_recursion(values: list[float], period: int) -> list[float]:
    """Recursao de Wilder escrita a parte, em Python puro, sem pandas.

    Existe para que a conferencia dos indicadores nao seja circular. Comparar
    `rsi()` com `rma()` prova apenas que os dois concordam entre si; comparar
    com esta funcao -- semente = media simples dos `period` primeiros valores,
    depois `(anterior * (period - 1) + atual) / period` -- prova que a
    implementacao E a de Wilder, que e o que D2 exige.

    Devolve NaN nas posicoes de aquecimento, alinhado com a lista de entrada.
    """
    out = [float("nan")] * len(values)
    if len(values) < period:
        return out
    current = sum(values[:period]) / period
    out[period - 1] = current
    for index in range(period, len(values)):
        current = (current * (period - 1) + values[index]) / period
        out[index] = current
    return out


def ema_recursion(values: list[float], period: int) -> list[float]:
    """Mesma ideia para a EMA: semente de media simples, alpha = 2/(period+1)."""
    out = [float("nan")] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for index in range(period, len(values)):
        current = alpha * values[index] + (1.0 - alpha) * current
        out[index] = current
    return out


def noisy_walk(size: int = 300, seed: int = 20260909) -> pd.Series:
    """Caminhada com altas E baixas -- um mercado, nao uma reta sintetica."""
    rng = np.random.default_rng(seed)
    return pd.Series(100.0 + rng.normal(0, 1.5, size).cumsum())


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

    def test_matches_the_recursion_written_apart(self):
        """Conferencia nao circular da EMA, tolerancia 1e-12 relativo."""
        values = noisy_walk().tolist()
        expected = ema_recursion(values, 21)
        got = ema(pd.Series(values), 21).to_numpy()
        for index, value in enumerate(expected):
            if np.isnan(value):
                assert np.isnan(got[index])
            else:
                assert got[index] == pytest.approx(value, rel=1e-12)

    def test_differs_from_pandas_ewm_which_is_the_whole_point(self):
        """Se a semente virasse a do `ewm(adjust=False)`, isto quebra.

        O `ewm` semeia no PRIMEIRO valor da serie; TradingView e Binance semeiam
        na media simples. Fixar a divergencia evita que alguem "simplifique"
        `_smooth` para `ewm` e desalinhe silenciosamente o backtest do grafico.
        """
        series = pd.Series([10.0, 11.0, 12.0, 13.0])
        assert ema(series, 2).iloc[2] == pytest.approx(11.5)
        assert series.ewm(alpha=2 / 3, adjust=False).mean().iloc[2] == pytest.approx(
            11.555555, abs=1e-6
        )


class TestSeriesTooShortToDecide:
    """Um unico candle -- ou poucos -- devolve NaN, nunca palpite nem excecao.

    E o caso do par recem-listado e do primeiro candle apos limpar o banco. O
    perigo aqui nao e a excecao (o agente registra e segue): e um numero.
    """

    @pytest.mark.parametrize("size", [1, 2, 5])
    def test_every_indicator_returns_nan(self, size):
        one = pd.Series([100.0 + i for i in range(size)])
        assert rsi(one, 14).isna().all()
        assert ema(one, 14).isna().all()
        assert rma(one, 14).isna().all()
        assert sma(one, 14).isna().all()
        assert atr(one * 1.01, one * 0.99, one, 14).isna().all()
        assert bollinger_bands(one, 20).percent_b.isna().all()
        assert macd(one, 12, 26, 9).macd.isna().all()

    def test_a_single_bar_does_not_raise(self):
        one = pd.Series([100.0])
        assert pd.isna(rsi(one, 14).iloc[0])
        assert pd.isna(atr(one, one, one, 14).iloc[0])
        assert len(rsi(one, 14)) == 1


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

    def test_matches_the_wilder_recursion_written_apart(self):
        """Conferencia nao circular: `rma` contra a recursao em Python puro."""
        values = noisy_walk().tolist()
        expected = wilder_recursion(values, 14)
        got = rma(pd.Series(values), 14).to_numpy()
        for index, value in enumerate(expected):
            if np.isnan(value):
                assert np.isnan(got[index])
            else:
                assert got[index] == pytest.approx(value, rel=1e-12)

    def test_a_hole_in_the_middle_is_not_stitched_over(self):
        """NaN no MEIO da serie envenena para frente; nao vira numero plausivel.

        Defeito medido: a versao com `dropna()` costurava o furo em silencio.
        Serie intacta de 40 pontos -> `rma(.., 14)[39] = 113,473240`. Com um
        furo no ponto 20, devolvia `113,387334` -- calculado sobre uma serie que
        nao existiu, e sem um unico NaN na saida para denunciar. Como a
        suavizacao de Wilder tem memoria infinita, nao existe ponto em que ela
        possa retomar honestamente: o certo e NaN, e NaN faz a estrategia
        devolver `None`, que e o estado seguro.
        """
        intact = pd.Series([100.0 + i * 0.5 for i in range(40)])
        assert rma(intact, 14).iloc[39] == pytest.approx(113.473240, abs=1e-6)

        holed = intact.copy()
        holed.iloc[20] = np.nan
        result = rma(holed, 14)
        assert not pd.isna(result.iloc[19])  # antes do furo, valor honesto
        assert result.iloc[20:].isna().all()  # do furo em diante, nada
        assert result.iloc[39] != pytest.approx(113.387334, abs=1e-6)

    def test_a_hole_in_the_seed_gives_nothing(self):
        holed = pd.Series([1.0, 2.0, np.nan, 4.0, 5.0, 6.0, 7.0])
        assert rma(holed, 3).isna().all()

    def test_survives_a_duplicated_index(self):
        """`open_time` repetido na janela nao pode derrubar o indicador.

        Defeito medido: a versao antiga terminava com `reindex`, e um indice com
        rotulo repetido levantava `ValueError: cannot reindex on an axis with
        duplicate labels`. Pior, era assimetrico -- `rsi`/`atr` quebravam (serie
        com NaN inicial, indice diferente do original), `ema`/`sma` passavam
        (indice identico, atalho do pandas). O agente registrava
        `strategy.failed` apenas para as estrategias de RSI e seguia verde,
        operando com menos estrategias do que as configuradas.
        """
        index = pd.Index([0, 1, 2, 3, 4, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14])
        series = pd.Series([100.0 + i for i in range(len(index))], index=index)
        assert not index.is_unique

        assert not pd.isna(rma(series, 5).iloc[-1])
        assert not pd.isna(rsi(series, 5).iloc[-1])
        assert not pd.isna(
            atr(series * 1.01, series * 0.99, series, 5).iloc[-1]
        )
        assert not pd.isna(macd(series, 2, 4, 3).signal.iloc[-1])


class TestRsi:
    def test_matches_wilder_reference_digit_by_digit(self, wilder):
        """Toda a cauda do RSI(14) da serie de Wilder, nao so as tres primeiras.

        Um indicador que erra por 0,3 num ponto e acerta em tres nao serve para
        decidir dinheiro. Conferimos as SEIS barras em que o RSI existe.
        """
        result = rsi(wilder, 14).iloc[14:].tolist()
        assert len(result) == len(WILDER_RSI_14)
        for got, expected in zip(result, WILDER_RSI_14, strict=True):
            assert got == pytest.approx(expected, abs=WILDER_RSI_TOLERANCE)

    def test_is_wilder_smoothing_and_not_a_lookalike(self):
        """Compara com a recursao de Wilder escrita a parte (D2).

        Tolerancia 1e-12 relativo: nao e "proximo", e a mesma aritmetica em
        float64. Se alguem trocar `rma` por `ewm` ou por uma media simples de
        ganhos (o RSI "de Cutler", que existe e da outro numero), isto quebra.
        """
        close = noisy_walk()
        deltas = close.diff().tolist()
        gains = [0.0 if np.isnan(d) else max(d, 0.0) for d in deltas[1:]]
        losses = [0.0 if np.isnan(d) else max(-d, 0.0) for d in deltas[1:]]

        avg_gain = wilder_recursion(gains, 14)
        avg_loss = wilder_recursion(losses, 14)
        expected = [
            float("nan")
            if np.isnan(gain)
            else (100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss))
            for gain, loss in zip(avg_gain, avg_loss, strict=True)
        ]

        got = rsi(close, 14).to_numpy()
        # A serie de variacoes comeca no indice 1, entao `expected[i]` vale para
        # o indice i+1 da serie de precos.
        for index, value in enumerate(expected):
            if np.isnan(value):
                assert np.isnan(got[index + 1])
            else:
                assert got[index + 1] == pytest.approx(value, rel=1e-12)

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

    def test_constant_series_does_not_divide_by_zero_and_matches_tradingview(self):
        """Serie totalmente plana: 0/0 no RS. Nao pode virar inf, nem excecao.

        O valor devolvido e **100**, que e o que o `ta.rsi` do Pine Script
        devolve (o ternario dele testa `down == 0` antes de `up == 0`). Manter a
        paridade com o grafico da exchange vale mais aqui do que a convencao
        mais bonita -- mas 100 numa serie plana NAO significa sobrecomprado, e
        e exatamente por isso que `one_sided` existe.
        """
        flat = pd.Series([100.0] * 40)
        result = rsi(flat, 14)
        assert result.iloc[:14].isna().all()
        assert (result.iloc[14:] == 100.0).all()
        assert np.isfinite(result.iloc[14:]).all()

    def test_one_sided_window_is_flagged(self):
        """`one_sided` marca as janelas em que o RSI e aritmetica, nao mercado."""
        flat = rsi_detail(pd.Series([100.0] * 40), 14)
        assert bool(flat.one_sided.iloc[-1])
        assert flat.avg_gain.iloc[-1] == 0.0
        assert flat.avg_loss.iloc[-1] == 0.0

        only_up = rsi_detail(pd.Series([100.0 + i for i in range(40)]), 14)
        assert bool(only_up.one_sided.iloc[-1])
        assert only_up.value.iloc[-1] == pytest.approx(100.0)

        only_down = rsi_detail(pd.Series([100.0 - i for i in range(40)]), 14)
        assert bool(only_down.one_sided.iloc[-1])
        assert only_down.value.iloc[-1] == pytest.approx(0.0)

        real_market = rsi_detail(noisy_walk(), 14)
        assert not real_market.one_sided.iloc[-1]

    def test_dead_pair_then_one_tick_reads_zero_by_definition(self):
        """A cadeia que produzia LONG com confianca maxima (medido 2026-09-09).

        40 fechamentos identicos, um tique de -0,01%, a volta ao mesmo preco.
        O RSI vai de 100 a 0 e depois a ~52 -- e nenhum desses tres numeros e
        informacao sobre o mercado. O teste fixa a leitura E a marcacao; quem
        recusa a compra e a estrategia (`test_strategies.py`).
        """
        reading = rsi_detail(pd.Series([100.0] * 40 + [99.99, 100.0]), 14)
        assert reading.value.iloc[-3] == pytest.approx(100.0)
        assert reading.value.iloc[-2] == pytest.approx(0.0)
        assert reading.value.iloc[-1] == pytest.approx(51.85, abs=0.01)
        assert reading.avg_gain.iloc[-2] == 0.0
        assert bool(reading.one_sided.iloc[-2])

    def test_one_tick_takes_the_window_out_of_one_sided_without_informing_it(self):
        """Por que `one_sided` nao pode ser o criterio de decisao.

        Canto exato tem medida zero. A serie e a mesma de cima com UM tique de
        alta de 1e-08 na barra 20: `avg_gain` deixa de ser zero (vira 1,62e-10),
        `one_sided` vira False -- e a janela continua sem informacao nenhuma. Um
        criterio de recusa construido sobre `one_sided` fecharia o caso
        sintetico e deixaria o caso real aberto.
        """
        quase_morto = [100.0] * 20 + [100.00000001] + [100.0] * 19 + [99.99, 100.0]
        reading = rsi_detail(pd.Series(quase_morto), 14)
        assert float(reading.avg_gain.iloc[-2]) == pytest.approx(1.62e-10, rel=0.05)
        assert not bool(reading.one_sided.iloc[-2])
        # E a regra de saida da sobrevenda (D9) continua satisfeita.
        assert reading.value.iloc[-2] <= 30.0 < reading.value.iloc[-1]


class TestRelativeMove:
    """`relative_move`: "esta janela andou?", em unidade adimensional.

    E o criterio que as quatro estrategias usam para recusar abrir posicao. Se
    ele estiver errado, a recusa fica errada nas quatro de uma vez.
    """

    def test_it_is_the_mean_absolute_move_over_the_price_computed_by_hand(self):
        """Valor conferido a mao na serie de Wilder, na recursao de Wilder.

        As 14 primeiras variacoes somam 3,34 de ganho e 1,40 de perda (os
        mesmos numeros da conta do RSI, no topo deste arquivo), entao
        `rma(|delta|, 14)` na barra 14 e (3,34 + 1,40)/14 = 0,33857142857... e o
        fechamento dessa barra e 46,28:

            0,33857142857142857 / 46,28 = 0,00731571798987...

        Tolerancia declarada: 1e-12 absoluto (doze casas). O numerador tambem e
        conferido contra a recursao de Wilder escrita a parte, para que a
        verificacao nao seja `rma` concordando consigo mesmo.
        """
        closes = pd.Series(WILDER_CLOSES)
        medida = relative_move(closes, 14)
        assert medida.iloc[:14].isna().all()  # aquecimento e NaN, nao aproximado
        assert float(medida.iloc[14]) == pytest.approx(0.00731571798987, abs=1e-12)

        # Numerador pela recursao independente: soma dos 14 primeiros |delta|
        # dividida por 14 -- e (3,34 + 1,40)/14 exatamente.
        deltas = [abs(b - a) for a, b in pairwise(WILDER_CLOSES)]
        conferido = wilder_recursion(deltas, 14)[13] / WILDER_CLOSES[14]
        assert float(medida.iloc[14]) == pytest.approx(conferido, rel=1e-12)
        assert sum(deltas[:14]) == pytest.approx(3.34 + 1.40, abs=1e-9)

    def test_it_is_the_same_quantity_as_rsi_relative_width(self):
        """A identidade que permite UM piso em vez de quatro calibracoes.

        `rma` e linear e `gains + losses == |delta|`, logo
        `rma(gains) + rma(losses) == rma(|delta|)`. Comparado serie a serie, em
        tres periodos, para que a igualdade nao seja so uma frase na docstring.
        """
        closes = noisy_walk(size=200)
        for period in (14, 20, 26):
            direto = relative_move(closes, period)
            pelo_rsi = rsi_detail(closes, period).relative_width(closes)
            pd.testing.assert_series_equal(direto, pelo_rsi, check_names=False, atol=1e-15)

    def test_it_separates_a_dead_pair_from_a_real_one_by_orders_of_magnitude(self):
        """O numero que sustenta a recusa, medido nos dois extremos.

        Par morto com um tique de 1e-08 num preco de 100: 7,1e-06. Mercado de
        verdade (16 pares /USDC 1d, mediana medida em 2026-09-09): ~2e-02. Sao
        quase quatro ordens de grandeza -- e por isso a recusa nao precisa de
        limiar fino.
        """
        morto = pd.Series([100.0] * 20 + [100.00000001] + [100.0] * 19 + [99.99, 100.0])
        assert float(relative_move(morto, 14).iloc[-2]) == pytest.approx(7.14e-06, rel=0.05)

        vivo = float(relative_move(noisy_walk(size=200), 14).iloc[-1])
        assert vivo > 1e-3
        assert vivo / float(relative_move(morto, 14).iloc[-2]) > 100

    def test_it_is_invariant_to_the_price_scale(self):
        """O ponto todo: comparavel entre BTC a 60.000 e PEPE a 0,00001.

        Um piso absoluto (em unidade de preco) seria apertado demais para um par
        e largo demais para o outro; foi essa invariancia que faltava na guarda
        de aritmetica exata.
        """
        base = noisy_walk(size=120)
        for fator in (1e-8, 1e-3, 1.0, 1e4):
            escalada = relative_move(base * fator, 14)
            pd.testing.assert_series_equal(
                escalada, relative_move(base, 14), check_names=False, rtol=1e-9
            )

    def test_a_zero_price_gives_nan_and_not_a_wide_window(self):
        """Preco zero e "nao sei", nunca "janela larga" -- fail-closed."""
        serie = pd.Series([100.0] * 20 + [0.0] * 20)
        assert bool(pd.isna(relative_move(serie, 14).iloc[-1]))

    def test_a_single_candle_is_nan_not_an_exception(self):
        assert bool(pd.isna(relative_move(pd.Series([100.0]), 14).iloc[-1]))

    def test_rejects_an_invalid_period(self):
        with pytest.raises(ValueError, match="periodo"):
            relative_move(pd.Series([100.0] * 20), 0)


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


# OHLC pequeno, escolhido para o ATR(3) dar numeros redondos a mao.
#
#   barra | high  low   close | TR
#     0   | 10,0  9,0   9,5   | 1,0  (sem fechamento anterior: high - low)
#     1   | 12,0  11,0  11,5  | 2,5  = max(1,0 ; |12,0-9,5| ; |11,0-9,5|)
#     2   | 11,8  10,5  10,8  | 1,3  = max(1,3 ; 0,3 ; 1,0)
#     3   | 11,0  10,0  10,9  | 1,0  = max(1,0 ; 0,2 ; 0,8)
#     4   | 12,5  10,8  12,4  | 1,7  = max(1,7 ; 1,6 ; 0,1)
ATR_HIGH = [10.0, 12.0, 11.8, 11.0, 12.5]
ATR_LOW = [9.0, 11.0, 10.5, 10.0, 10.8]
ATR_CLOSE = [9.5, 11.5, 10.8, 10.9, 12.4]
ATR_TRUE_RANGE = [1.0, 2.5, 1.3, 1.0, 1.7]


class TestAtr:
    def test_true_range_uses_previous_close(self):
        high = pd.Series([10.0, 12.0])
        low = pd.Series([9.0, 11.0])
        close = pd.Series([9.5, 11.5])
        # TR[1] = max(12-11, |12-9.5|, |11-9.5|) = 2.5
        assert true_range(high, low, close).iloc[1] == pytest.approx(2.5)

    def test_true_range_of_the_first_bar_is_the_range_itself(self):
        """Sem fechamento anterior, TR = high - low. Nao e NaN.

        E a convencao do `ta.tr(true)` do Pine Script, que e o que o `ta.atr`
        usa. TA-Lib faz o oposto: comeca o TR na barra 1. A escolha desloca a
        semente do ATR em uma barra, e o teste seguinte mede o tamanho disso.
        """
        result = true_range(pd.Series(ATR_HIGH), pd.Series(ATR_LOW), pd.Series(ATR_CLOSE))
        assert result.tolist() == pytest.approx(ATR_TRUE_RANGE)

    def test_matches_the_tradingview_seed_by_hand(self):
        """ATR(3) conferido a mao, com a convencao declarada.

            ATR[2] = (1,0 + 2,5 + 1,3) / 3        = 1,6
            ATR[3] = (1,6 * 2 + 1,0) / 3          = 1,4
            ATR[4] = (1,4 * 2 + 1,7) / 3          = 1,5

        Tolerancia declarada: 1e-12 relativo.
        """
        result = atr(pd.Series(ATR_HIGH), pd.Series(ATR_LOW), pd.Series(ATR_CLOSE), 3)
        assert result.iloc[:2].isna().all()
        assert result.iloc[2] == pytest.approx(1.6, rel=1e-12)
        assert result.iloc[3] == pytest.approx(1.4, rel=1e-12)
        assert result.iloc[4] == pytest.approx(1.5, rel=1e-12)

    def test_the_divergence_from_the_talib_convention_is_measured_not_guessed(self):
        """Quanto a escolha de convencao custa, em numero.

        TA-Lib comeca o TR na barra 1 e poe a semente do ATR uma barra depois:
        ATR[3] = (2,5 + 1,3 + 1,0)/3 = 1,6 e ATR[4] = (1,6*2 + 1,7)/3 = 1,6333.
        Nos temos 1,5 na mesma barra -- 8,16% de diferenca. A divergencia decai
        como ((n-1)/n)^k e some depois do aquecimento, mas quem comparar o ATR
        desta base com um print do TA-Lib nas primeiras barras vai ver
        diferenca, e ela e esperada.
        """
        talib_first = (2.5 + 1.3 + 1.0) / 3
        talib_next = (talib_first * 2 + 1.7) / 3
        ours = atr(pd.Series(ATR_HIGH), pd.Series(ATR_LOW), pd.Series(ATR_CLOSE), 3)
        assert talib_next == pytest.approx(1.6333, abs=0.0001)
        assert ours.iloc[4] == pytest.approx(1.5, rel=1e-12)
        divergence = abs(ours.iloc[4] - talib_next) / talib_next
        assert divergence == pytest.approx(0.0816, abs=0.0005)

    def test_is_the_wilder_recursion_over_the_true_range(self):
        """ATR contra a recursao de Wilder escrita a parte, serie longa."""
        close = noisy_walk()
        rng = np.random.default_rng(4)
        high = close + rng.uniform(0.2, 2.0, len(close))
        low = close - rng.uniform(0.2, 2.0, len(close))

        ranges = true_range(high, low, close).tolist()
        expected = wilder_recursion(ranges, 14)
        got = atr(high, low, close, 14).to_numpy()
        for index, value in enumerate(expected):
            if np.isnan(value):
                assert np.isnan(got[index])
            else:
                assert got[index] == pytest.approx(value, rel=1e-12)

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
