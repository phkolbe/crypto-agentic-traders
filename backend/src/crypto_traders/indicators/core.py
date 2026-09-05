"""Indicadores tecnicos em pandas/numpy puro.

Implementacao propria em vez de `pandas-ta`/TA-Lib por dois motivos:

1. `pandas-ta` nao acompanha o pandas 3.x, e TA-Lib exige compilacao nativa no
   Windows -- dependencia fragil para um sistema que precisa subir sozinho apos
   um restart da maquina.
2. Estes numeros decidem o uso de dinheiro real. Cada funcao aqui tem teste
   unitario com valores conferidos a mao; uma caixa-preta de terceiros nao teria.

Convencoes:
- Toda funcao recebe e devolve `pd.Series` alinhadas pelo indice de entrada.
- O aquecimento (`warmup`) devolve `NaN`, nunca um valor "aproximado". Decidir
  com media movel de 200 periodos calculada sobre 12 candles seria pior do que
  nao decidir.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """Media movel simples."""
    _validate(period)
    return series.rolling(window=period, min_periods=period).mean()


def _smooth(series: pd.Series, period: int, alpha: float) -> pd.Series:
    """Suavizacao exponencial recursiva semeada pela media simples.

    A semente e a **media simples** dos primeiros `period` valores validos; so a
    partir dai vale `atual = alpha * valor + (1 - alpha) * anterior`.

    Isso importa. O `ewm(adjust=False)` do pandas comeca a recursao no primeiro
    valor da serie, nao numa semente de media simples -- e as plataformas de
    trading (TradingView, Binance) usam a semente de media simples. Sem esse
    ajuste, os indicadores calculados aqui divergem visivelmente dos que voce ve
    no grafico da exchange, e o backtest passa a validar um sistema diferente do
    que roda ao vivo.

    NaN inicial (produzido por `diff()`/`shift()`) e ignorado na semente.
    """
    _validate(period)
    valid = series.dropna()
    if len(valid) < period:
        return pd.Series(np.nan, index=series.index, dtype="float64")

    values = valid.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    current = float(values[:period].mean())
    out[period - 1] = current
    for i in range(period, len(values)):
        current = alpha * values[i] + (1.0 - alpha) * current
        out[i] = current

    return pd.Series(out, index=valid.index).reindex(series.index)


def ema(series: pd.Series, period: int) -> pd.Series:
    """Media movel exponencial (alpha = 2 / (period + 1))."""
    _validate(period)
    return _smooth(series, period, alpha=2.0 / (period + 1.0))


def rma(series: pd.Series, period: int) -> pd.Series:
    """Suavizacao de Wilder (alpha = 1 / period), base do RSI e do ATR."""
    _validate(period)
    return _smooth(series, period, alpha=1.0 / period)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Indice de Forca Relativa (Wilder).

    Retorna 0-100. Convencao classica: <30 sobrevendido, >70 sobrecomprado.
    """
    _validate(period)
    delta = series.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)

    avg_gain = rma(gains, period)
    avg_loss = rma(losses, period)

    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100.0 - (100.0 / (1.0 + rs))
    # Sem nenhuma perda na janela, RS -> infinito e o RSI e 100 por definicao.
    return result.where(avg_loss != 0, 100.0).where(avg_gain.notna())


@dataclass(frozen=True)
class MacdResult:
    macd: pd.Series
    signal: pd.Series
    histogram: pd.Series


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal_period: int = 9
) -> MacdResult:
    """MACD = EMA(fast) - EMA(slow), com linha de sinal e histograma."""
    if fast >= slow:
        raise ValueError("MACD exige fast < slow")
    macd_line = ema(series, fast) - ema(series, slow)
    # `ema` (e nao `ewm`) tambem aqui: a linha de sinal precisa da mesma semente
    # de media simples, senao diverge da que a exchange desenha.
    signal_line = ema(macd_line, signal_period)
    return MacdResult(macd=macd_line, signal=signal_line, histogram=macd_line - signal_line)


@dataclass(frozen=True)
class BollingerResult:
    middle: pd.Series
    upper: pd.Series
    lower: pd.Series
    bandwidth: pd.Series
    percent_b: pd.Series
    """Posicao do preco entre as bandas: 0 = banda inferior, 1 = banda superior."""


def bollinger_bands(
    series: pd.Series, period: int = 20, std_multiplier: float = 2.0
) -> BollingerResult:
    """Bandas de Bollinger.

    Usa desvio padrao populacional (`ddof=0`), como as plataformas de trading --
    o padrao do pandas e amostral (`ddof=1`) e produziria bandas ligeiramente
    mais largas do que as do grafico.
    """
    _validate(period)
    middle = sma(series, period)
    deviation = series.rolling(window=period, min_periods=period).std(ddof=0)
    upper = middle + std_multiplier * deviation
    lower = middle - std_multiplier * deviation

    band_range = upper - lower
    percent_b = (series - lower) / band_range.replace(0, np.nan)
    bandwidth = band_range / middle.replace(0, np.nan)

    return BollingerResult(
        middle=middle, upper=upper, lower=lower, bandwidth=bandwidth, percent_b=percent_b
    )


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    previous_close = close.shift(1)
    return pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range (Wilder): mede volatilidade, nao direcao.

    Serve para dimensionar stops proporcionais a agitacao do ativo -- um stop de
    3% e apertado demais para um altcoin e largo demais para um par estavel.
    """
    _validate(period)
    return rma(true_range(high, low, close), period)


def crossed_above(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """True apenas no candle em que `fast` cruza `slow` para cima.

    Exige que a barra anterior tenha valores validos: sem isso, o fim do periodo
    de aquecimento (NaN -> numero) seria lido como um cruzamento inexistente.
    """
    previous_valid = fast.shift(1).notna() & slow.shift(1).notna()
    return previous_valid & (fast.shift(1) <= slow.shift(1)) & (fast > slow)


def crossed_below(fast: pd.Series, slow: pd.Series) -> pd.Series:
    previous_valid = fast.shift(1).notna() & slow.shift(1).notna()
    return previous_valid & (fast.shift(1) >= slow.shift(1)) & (fast < slow)


def _validate(period: int) -> None:
    if period < 1:
        raise ValueError("periodo deve ser >= 1")


__all__ = [
    "BollingerResult",
    "MacdResult",
    "atr",
    "bollinger_bands",
    "crossed_above",
    "crossed_below",
    "ema",
    "macd",
    "rma",
    "rsi",
    "sma",
    "true_range",
]
