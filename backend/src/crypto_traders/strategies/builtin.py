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
from ..indicators import bollinger_bands, crossed_above, crossed_below, ema, macd, rsi, sma
from .base import MarketFrame, Strategy, clean


class MovingAverageCrossover(Strategy):
    """Cruzamento de medias moveis (tendencia).

    Compra quando a media rapida cruza a lenta para cima, fecha no cruzamento
    inverso. A confianca cresce com a separacao entre as medias: um cruzamento
    raspando e ruido com muito mais frequencia do que um cruzamento decidido.
    """

    name = "ma_crossover"
    description = "Cruzamento de media rapida sobre media lenta"

    def __init__(self, fast: int = 9, slow: int = 21, use_ema: bool = True) -> None:
        if fast >= slow:
            raise ValueError("ma_crossover exige fast < slow")
        super().__init__(fast=fast, slow=slow, use_ema=use_ema)
        self.fast = fast
        self.slow = slow
        self.use_ema = use_ema
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
    """

    name = "rsi_reversion"
    description = "Compra na saida da sobrevenda, fecha na sobrecompra"

    def __init__(self, period: int = 14, oversold: float = 30.0, overbought: float = 70.0) -> None:
        if not 0 < oversold < overbought < 100:
            raise ValueError("rsi_reversion exige 0 < oversold < overbought < 100")
        super().__init__(period=period, oversold=oversold, overbought=overbought)
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        self.min_candles = period * 3

    def evaluate(self, market: MarketFrame) -> Signal | None:
        if not self._has_warmup(market):
            return None

        values = rsi(market.frame["close"], self.period)
        current, previous = values.iloc[-1], values.iloc[-2]
        if pd.isna(current) or pd.isna(previous):
            return None

        indicators = {"rsi": clean(current), "rsi_previous": clean(previous)}

        if previous <= self.oversold < current:
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
    """

    name = "macd_trend"
    description = "Cruzamento MACD x sinal, filtrado por EMA de tendencia"

    def __init__(
        self, fast: int = 12, slow: int = 26, signal_period: int = 9, trend_period: int = 100
    ) -> None:
        super().__init__(
            fast=fast, slow=slow, signal_period=signal_period, trend_period=trend_period
        )
        self.fast = fast
        self.slow = slow
        self.signal_period = signal_period
        self.trend_period = trend_period
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
    """

    name = "bollinger_reversion"
    description = "Reversao nas bandas de Bollinger"

    def __init__(self, period: int = 20, std_multiplier: float = 2.0) -> None:
        super().__init__(period=period, std_multiplier=std_multiplier)
        self.period = period
        self.std_multiplier = std_multiplier
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
