"""Estrategias baseadas em indicadores tecnicos classicos.

As quatro implementadas aqui sao deliberadamente simples e transparentes: o
objetivo do MVP e validar toda a cadeia de decisao (dados -> sinal -> risco ->
execucao -> portfolio), nao encontrar alfa. Estrategia sofisticada sobre um
pipeline nao testado e a forma mais rapida de perder dinheiro com elegancia.

Todas seguem o mesmo padrao:
- Decidem apenas sobre o **ultimo candle fechado**.
- Emitem `LONG` para abrir/aumentar, `FLAT` para fechar. Nao ha `SHORT`: o MVP
  opera spot, onde vender a descoberto nao existe.
- Devolvem `None` na maioria das barras -- nao operar e o caso normal.
"""

from __future__ import annotations

import pandas as pd

from ..domain.enums import SignalDirection
from ..domain.models import Signal
from ..indicators import (
    bollinger_bands,
    crossed_above,
    crossed_below,
    ema,
    macd,
    relative_move,
    rsi_detail,
    sma,
)
from .base import MIN_WINDOW_WIDTH, MarketFrame, Strategy, clean


class MovingAverageCrossover(Strategy):
    """Cruzamento de medias moveis (tendencia).

    Compra quando a media rapida cruza a lenta para cima, fecha no cruzamento
    inverso. A confianca cresce com a separacao entre as medias: um cruzamento
    raspando e ruido com muito mais frequencia do que um cruzamento decidido.

    Tem a mesma cegueira a largura da janela que as duas estrategias de
    reversao, e ela importa mais aqui do que em qualquer outra: esta e a unica
    estrategia habilitada em producao hoje. Medido em 2026-09-09, num par plano
    ao ultimo bit com um serrote de UM tique de 1e-08 (180 candles), esta
    estrategia emitia **39 LONGs** -- `relative_move` da janela: 4,9e-11. A
    confianca sai no piso (0,55), mas confianca baixa nao impede a ordem: com
    20% do capital por ordem e minimo de 5 USDC, 0,55 gasta dinheiro real num
    par que nao andou. Por isso a recusa vale para as quatro, e nao so para as
    de reversao.
    """

    name = "ma_crossover"
    description = "Cruzamento de media rapida sobre media lenta"

    def __init__(
        self,
        fast: int = 9,
        slow: int = 21,
        use_ema: bool = True,
        min_window_width: float = MIN_WINDOW_WIDTH,
    ) -> None:
        if fast >= slow:
            raise ValueError("ma_crossover exige fast < slow")
        super().__init__(
            fast=fast, slow=slow, use_ema=use_ema, min_window_width=min_window_width
        )
        self.fast = fast
        self.slow = slow
        self.use_ema = use_ema
        self.min_window_width = min_window_width
        self.min_candles = slow + 5

    def evaluate(self, market: MarketFrame) -> Signal | None:
        if not self._has_warmup(market):
            return None

        close = market.frame["close"]
        average = ema if self.use_ema else sma
        fast_line = average(close, self.fast)
        slow_line = average(close, self.slow)

        if pd.isna(fast_line.iloc[-1]) or pd.isna(slow_line.iloc[-1]):
            return None  # ainda em aquecimento

        indicators = {
            "ma_fast": clean(fast_line.iloc[-1]),
            "ma_slow": clean(slow_line.iloc[-1]),
            "close": clean(close.iloc[-1]),
        }

        # Separacao relativa entre as medias, usada como proxy de convicção.
        spread = abs(fast_line.iloc[-1] - slow_line.iloc[-1]) / slow_line.iloc[-1]
        confidence = 0.55 + min(0.35, float(spread) * 20)

        if bool(crossed_above(fast_line, slow_line).iloc[-1]):
            width = float(relative_move(close, self.slow).iloc[-2:].min(skipna=False))
            if self._window_is_too_narrow(
                market,
                width,
                medida="relative_move",
                separacao=float(spread),
            ):
                return None
            return self._signal(
                market,
                SignalDirection.LONG,
                confidence,
                f"MA{self.fast} cruzou acima da MA{self.slow} "
                f"(separacao {spread:.2%})",
                indicators,
            )

        if bool(crossed_below(fast_line, slow_line).iloc[-1]):
            return self._signal(
                market,
                SignalDirection.FLAT,
                confidence,
                f"MA{self.fast} cruzou abaixo da MA{self.slow} "
                f"(separacao {spread:.2%})",
                indicators,
            )

        return None


class RsiReversion(Strategy):
    """Reversao a media por RSI.

    Compra na **saida** da zona de sobrevenda, nao na entrada. A diferenca e
    importante: um ativo em queda forte pode ficar com RSI abaixo de 30 por
    dezenas de candles, e comprar na entrada da zona significa comprar no meio
    da queda, repetidamente.

    Sair da zona nao basta: a janela tambem precisa ter tido movimento
    economico. Medido em 2026-09-09, com 40 fechamentos diarios identicos
    seguidos de um tique de -0,01% e a volta ao mesmo preco, esta estrategia
    emitia LONG com **confianca 0,90 -- a maxima que ela sabe emitir**. A
    cadeia: janela plana -> `avg_gain` e `avg_loss` exatamente zero -> RSI 100;
    um tique de baixa -> `avg_gain` continua exatamente zero -> RSI 0 por
    definicao; o tique de volta -> RSI 51,9. A regra "0 <= 30 < 51,9" dava
    sobrevenda profunda, e `depth` batia no teto. Nada disso era mercado: era um
    par morto e um centavo. Com 20% do capital por ordem e selecao de lote por
    confianca, esse sinal ganharia de qualquer sinal legitimo do dia.

    A primeira versao da guarda usava `one_sided` -- `avg_gain == 0.0`,
    aritmetica exata -- e isso NAO fechava o defeito, fechava so o caso
    sintetico. Par sem liquidez de verdade tem movimento minusculo, nao
    movimento zero: um unico tique de alta de 1e-08 em qualquer barra da janela
    tira `avg_gain` do zero exato (1,62e-10), `one_sided` vira False e a compra
    de 0,90 volta inteira. No caso realista (altcoin a 0,00123000 USDC, um tique
    de 1e-08) e pior: o RSI anterior le 13,9 em vez de 0, entao nem o log
    denuncia -- uma inspecao humana leria "saiu de sobrevenda profunda".

    A guarda que vale e RELATIVA: `relative_width` (movimento medio por barra
    dividido pelo preco) contra `MIN_WINDOW_WIDTH`, cuja medicao em tres janelas
    independentes esta documentada em `base.py`. `one_sided` continua sendo
    reportado no log porque distingue "janela estreita" de "janela literalmente
    de um lado so", mas nao decide mais nada.

    A guarda vale so para a COMPRA. Fechar posicao nunca pode ser bloqueado por
    duvida sobre a qualidade do dado -- e a mesma regra do circuit breaker (D4).
    """

    name = "rsi_reversion"
    description = "Compra na saida da sobrevenda, fecha na sobrecompra"

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        min_window_width: float = MIN_WINDOW_WIDTH,
    ) -> None:
        if not 0 < oversold < overbought < 100:
            raise ValueError("rsi_reversion exige 0 < oversold < overbought < 100")
        super().__init__(
            period=period,
            oversold=oversold,
            overbought=overbought,
            min_window_width=min_window_width,
        )
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        self.min_window_width = min_window_width
        self.min_candles = period * 3

    def evaluate(self, market: MarketFrame) -> Signal | None:
        if not self._has_warmup(market):
            return None

        reading = rsi_detail(market.frame["close"], self.period)
        values = reading.value
        current, previous = values.iloc[-1], values.iloc[-2]
        if pd.isna(current) or pd.isna(previous):
            return None

        indicators = {"rsi": clean(current), "rsi_previous": clean(previous)}

        if previous <= self.oversold < current:
            # Largura das DUAS barras que a regra de D9 le. O minimo, nao a
            # media: basta uma das duas ser degenerada para a comparacao
            # "previous <= oversold < current" nao ser sobre mercado.
            widths = reading.relative_width(market.frame["close"])
            # `skipna=False`: NaN em qualquer das duas barras propaga, e NaN
            # recusa a compra. `min()` do Python devolveria a outra barra.
            width = float(widths.iloc[-2:].min(skipna=False))
            if self._window_is_too_narrow(
                market,
                width,
                medida="rsi.relative_width",
                rsi_anterior=clean(previous),
                rsi_atual=clean(current),
                avg_gain=clean(reading.avg_gain.iloc[-2]),
                avg_loss=clean(reading.avg_loss.iloc[-2]),
                one_sided=bool(reading.one_sided.iloc[-2] or reading.one_sided.iloc[-1]),
            ):
                return None
            # Quanto mais fundo esteve o RSI, mais forte a reversao.
            depth = (self.oversold - previous) / self.oversold
            return self._signal(
                market,
                SignalDirection.LONG,
                0.60 + min(0.30, float(depth)),
                f"RSI saiu da sobrevenda ({previous:.1f} -> {current:.1f})",
                indicators,
            )

        if previous >= self.overbought > current:
            return self._signal(
                market,
                SignalDirection.FLAT,
                0.60,
                f"RSI saiu da sobrecompra ({previous:.1f} -> {current:.1f})",
                indicators,
            )

        return None


class MacdTrend(Strategy):
    """Cruzamento do MACD com sua linha de sinal, filtrado pela tendencia.

    O filtro de tendencia (preco acima da EMA longa) existe porque o MACD gera
    muitos cruzamentos para cima dentro de mercados em queda -- cada um deles
    seria uma compra contra a tendencia dominante.

    O filtro de tendencia nao protege de par morto: num par plano com um unico
    tique de alta no fim, o preco fica acima da EMA por definicao e o
    cruzamento acontece. Medido em 2026-09-09: LONG com confianca 0,58 sobre
    `relative_move` de 1,6e-07. Dai a mesma recusa por largura de janela.
    """

    name = "macd_trend"
    description = "Cruzamento MACD x sinal, filtrado por EMA de tendencia"

    def __init__(
        self,
        fast: int = 12,
        slow: int = 26,
        signal_period: int = 9,
        trend_period: int = 100,
        min_window_width: float = MIN_WINDOW_WIDTH,
    ) -> None:
        super().__init__(
            fast=fast,
            slow=slow,
            signal_period=signal_period,
            trend_period=trend_period,
            min_window_width=min_window_width,
        )
        self.fast = fast
        self.slow = slow
        self.signal_period = signal_period
        self.trend_period = trend_period
        self.min_window_width = min_window_width
        self.min_candles = max(trend_period, slow + signal_period) + 5

    def evaluate(self, market: MarketFrame) -> Signal | None:
        if not self._has_warmup(market):
            return None

        close = market.frame["close"]
        result = macd(close, self.fast, self.slow, self.signal_period)
        trend = ema(close, self.trend_period)

        if pd.isna(result.macd.iloc[-1]) or pd.isna(trend.iloc[-1]):
            return None

        uptrend = bool(close.iloc[-1] > trend.iloc[-1])
        indicators = {
            "macd": clean(result.macd.iloc[-1]),
            "macd_signal": clean(result.signal.iloc[-1]),
            "macd_histogram": clean(result.histogram.iloc[-1]),
            "ema_trend": clean(trend.iloc[-1]),
        }

        if bool(crossed_above(result.macd, result.signal).iloc[-1]):
            if not uptrend:
                return None
            width = float(relative_move(close, self.slow).iloc[-2:].min(skipna=False))
            if self._window_is_too_narrow(
                market, width, medida="relative_move", em_alta=uptrend
            ):
                return None
            momentum = abs(float(result.histogram.iloc[-1])) / float(close.iloc[-1])
            return self._signal(
                market,
                SignalDirection.LONG,
                0.58 + min(0.30, momentum * 200),
                "MACD cruzou acima da linha de sinal com preco acima da "
                f"EMA{self.trend_period}",
                indicators,
            )

        if bool(crossed_below(result.macd, result.signal).iloc[-1]):
            return self._signal(
                market,
                SignalDirection.FLAT,
                0.60,
                "MACD cruzou abaixo da linha de sinal",
                indicators,
            )

        return None


class BollingerReversion(Strategy):
    """Reversao nas Bandas de Bollinger.

    Compra no reingresso apos tocar a banda inferior (nao no toque em si, pelo
    mesmo motivo do RSI: em queda forte o preco anda colado na banda), e fecha
    ao alcancar a banda superior.

    Tem o MESMO defeito do RSI, e por muito tempo sem guarda nenhuma: `percent_b`
    e a posicao do preco entre as bandas, normalizada pela largura da propria
    banda, e portanto e cego a largura. Medido em 2026-09-09, com 19 fechamentos
    identicos e um de 99,99 na janela de 20, o desvio padrao fica minusculo
    (`bandwidth` 8,72e-05, ou 0,0087% da banda media) e o %B salta de -0,59 para
    +0,56 com um centavo de movimento -- que a estrategia lia como "reingresso
    acima da banda inferior" e comprava com confianca 0,62.

    A recusa usa `bandwidth`, que ja e adimensional por construcao, contra
    `MIN_WINDOW_WIDTH` (medicao em `base.py`). Recusa so ABRIR: o fechamento no
    reingresso abaixo da banda superior continua livre (D4).
    """

    name = "bollinger_reversion"
    description = "Reversao nas bandas de Bollinger"

    def __init__(
        self,
        period: int = 20,
        std_multiplier: float = 2.0,
        min_window_width: float = MIN_WINDOW_WIDTH,
    ) -> None:
        super().__init__(
            period=period, std_multiplier=std_multiplier, min_window_width=min_window_width
        )
        self.period = period
        self.std_multiplier = std_multiplier
        self.min_window_width = min_window_width
        self.min_candles = period * 3

    def evaluate(self, market: MarketFrame) -> Signal | None:
        if not self._has_warmup(market):
            return None

        close = market.frame["close"]
        bands = bollinger_bands(close, self.period, self.std_multiplier)
        percent_b = bands.percent_b
        current, previous = percent_b.iloc[-1], percent_b.iloc[-2]
        if pd.isna(current) or pd.isna(previous):
            return None

        indicators = {
            "percent_b": clean(current),
            "bandwidth": clean(bands.bandwidth.iloc[-1]),
            "bb_upper": clean(bands.upper.iloc[-1]),
            "bb_lower": clean(bands.lower.iloc[-1]),
            "bb_middle": clean(bands.middle.iloc[-1]),
        }

        if previous <= 0.0 < current:
            width = float(bands.bandwidth.iloc[-2:].min(skipna=False))
            if self._window_is_too_narrow(
                market,
                width,
                medida="bollinger.bandwidth",
                percent_b_anterior=clean(previous),
                percent_b_atual=clean(current),
            ):
                return None
            return self._signal(
                market,
                SignalDirection.LONG,
                0.62,
                f"preco reingressou acima da banda inferior (%B {previous:.2f} -> {current:.2f})",
                indicators,
            )

        if previous >= 1.0 > current:
            return self._signal(
                market,
                SignalDirection.FLAT,
                0.62,
                f"preco reingressou abaixo da banda superior (%B {previous:.2f} -> {current:.2f})",
                indicators,
            )

        return None
