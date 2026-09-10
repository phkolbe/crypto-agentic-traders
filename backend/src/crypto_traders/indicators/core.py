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

    NaN **inicial** (produzido por `diff()`/`shift()`) e artefato de aquecimento e
    e pulado na semente. NaN **no meio** da serie e outra coisa: e dado
    corrompido, e a recursao a partir dele fica indefinida -- por isso ele
    envenena o resultado para frente em vez de ser costurado.

    A versao anterior fazia `dropna()` antes da recursao e reindexava no fim.
    Isso produzia dois defeitos medidos:

    * um NaN no meio era **emendado em silencio**: com um furo no candle 20 de
      uma serie de 40, `rma(.., 14)` devolvia 113,3873 onde a serie intacta
      dava 113,4732. Numero plausivel, calculado sobre uma serie que nao
      existiu, sem NaN nenhum na saida para denunciar.
    * o `reindex` estourava `ValueError: cannot reindex on an axis with
      duplicate labels` quando a janela tinha `open_time` repetido. E estourava
      de forma assimetrica: `rsi`/`atr` (que passam serie com NaN inicial, logo
      indice diferente do original) quebravam, `ema`/`sma` (indice identico,
      atalho do pandas) passavam. O agente registrava `strategy.failed` so para
      as estrategias de RSI, e o sistema seguia verde operando com menos
      estrategias do que as configuradas.

    A recursao agora e posicional, sem `reindex`.
    """
    _validate(period)
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)

    start = 0
    while start < len(values) and np.isnan(values[start]):
        start += 1
    usable = values[start:]
    if len(usable) < period:
        return pd.Series(out, index=series.index, dtype="float64")

    # Semente com furo -> NaN, e o NaN se propaga sozinho pela recursao abaixo.
    current = float(usable[:period].mean())
    out[start + period - 1] = current
    for offset in range(period, len(usable)):
        current = alpha * usable[offset] + (1.0 - alpha) * current
        out[start + offset] = current

    return pd.Series(out, index=series.index, dtype="float64")


def ema(series: pd.Series, period: int) -> pd.Series:
    """Media movel exponencial (alpha = 2 / (period + 1))."""
    _validate(period)
    return _smooth(series, period, alpha=2.0 / (period + 1.0))


def rma(series: pd.Series, period: int) -> pd.Series:
    """Suavizacao de Wilder (alpha = 1 / period), base do RSI e do ATR."""
    _validate(period)
    return _smooth(series, period, alpha=1.0 / period)


@dataclass(frozen=True)
class RsiResult:
    value: pd.Series
    """O RSI, 0-100, identico ao que o TradingView desenha."""

    avg_gain: pd.Series
    avg_loss: pd.Series
    """As duas medias de Wilder que formam o RS. Ficam expostas porque o VALOR
    do RSI, sozinho, nao diz se a janela tinha informacao: com `avg_gain`
    exatamente zero o RSI e 0 por definicao, e 0 aqui nao significa
    "sobrevendido", significa "nenhuma alta na janela inteira"."""

    @property
    def one_sided(self) -> pd.Series:
        """True onde a janela tem so ganho ou so perda -- RSI colado em 100 ou 0.

        Nao e limiar calibrado: e aritmetica exata. `avg_gain == 0` significa
        que nao houve **uma unica** barra de alta desde a semente, e
        `avg_loss == 0`, nenhuma de baixa.

        ATENCAO -- isto detecta apenas o CANTO exato, e canto exato tem medida
        zero. Medido em 2026-09-09: um unico tique de alta de 1e-08 em qualquer
        barra da janela tira `avg_gain` do zero (vira 1,62e-10) e `one_sided`
        vira False, embora a janela continue sem informacao nenhuma. Quem
        precisa decidir "esta janela tem movimento economico?" tem que usar
        `relative_width`, que e RELATIVA ao preco, e nao esta propriedade.
        """
        return (self.avg_gain == 0.0) | (self.avg_loss == 0.0)

    def relative_width(self, reference: pd.Series) -> pd.Series:
        """Movimento medio por barra da janela, dividido pelo preco.

        `avg_gain + avg_loss` e a media de Wilder de `|delta|`: quanto o preco
        anda por barra, em unidade de preco. Dividido pelo preco de referencia
        vira adimensional, e portanto comparavel entre BTC a 60.000 e PEPE a
        0,00001 -- que e o ponto. O RSI e invariante a escala e essa invariancia
        e justamente o defeito: ele le "sobrevenda profunda" num par que andou
        um tique porque a razao entre dois ruidos e um numero bem-comportado.

        `reference` deve ser a serie de fechamentos alinhada a mesma janela.
        Preco zero devolve NaN (nao 0), para que o resultado seja "nao sei" e
        nao "janela larga".
        """
        return (self.avg_gain + self.avg_loss) / reference.replace(0, np.nan)


def rsi_detail(series: pd.Series, period: int = 14) -> RsiResult:
    """RSI de Wilder junto das medias que o formam.

    A formula e a do Pine Script (`ta.rsi`), incluindo os dois casos de canto:
    `avg_loss == 0` -> 100, `avg_gain == 0` -> 0. Serie totalmente plana cai no
    primeiro deles e devolve **100**, nao 50 -- e o que o TradingView devolve, e
    manter a paridade com o grafico da exchange vale mais aqui do que uma
    convencao mais bonita. Quem decide sobre isso usa `one_sided`.
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
    value = result.where(avg_loss != 0, 100.0).where(avg_gain.notna())
    return RsiResult(value=value, avg_gain=avg_gain, avg_loss=avg_loss)


def relative_move(series: pd.Series, period: int = 14) -> pd.Series:
    """Movimento medio absoluto por barra dividido pelo preco -- adimensional.

    `rma(|delta|, period) / preco`. E a resposta a pergunta "esta janela andou?"
    numa unidade comparavel entre BTC a 60.000 e PEPE a 0,00001. Serve de piso
    de operabilidade para qualquer estrategia: indicador normalizado (RSI, %B,
    cruzamento de medias) e cego a largura da janela por construcao, e num par
    morto le a razao entre dois ruidos como se fosse mercado.

    Vale a identidade `relative_move(close, p) == rsi_detail(close, p).
    relative_width(close)`: `rma` e linear e `gains + losses == |delta|`, entao
    `rma(gains) + rma(losses) == rma(|delta|)`. Sao a mesma grandeza por dois
    caminhos, e existe teste comparando as duas serie a serie -- o que torna o
    piso de `MIN_WINDOW_WIDTH` uma medida unica para as quatro estrategias, e
    nao quatro numeros calibrados separadamente.

    Usa `rma` (Wilder, D2) e nao media simples para que a janela herde a mesma
    memoria que o RSI e o ATR ja usam.
    """
    _validate(period)
    return rma(series.diff().abs(), period) / series.replace(0, np.nan)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Indice de Forca Relativa (Wilder).

    Retorna 0-100. Convencao classica: <30 sobrevendido, >70 sobrecomprado.
    """
    return rsi_detail(series, period).value


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
    "RsiResult",
    "atr",
    "bollinger_bands",
    "crossed_above",
    "crossed_below",
    "ema",
    "macd",
    "relative_move",
    "rma",
    "rsi",
    "rsi_detail",
    "sma",
    "true_range",
]
