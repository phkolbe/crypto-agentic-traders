"""Contrato de estrategia.

Toda estrategia e um plugin com a mesma interface `evaluate(frame) -> Signal | None`.
Isso permite rodar varias em paralelo, comparar performance entre elas e
adicionar estrategias novas (ML, LLM, arbitragem) sem tocar em nenhum agente.

Uma estrategia **nunca** decide tamanho de posicao, stop-loss ou take-profit.
Ela responde apenas "para onde e com quanta convicção" -- o dimensionamento e a
protecao sao responsabilidade exclusiva do Risk Manager. Assim, uma estrategia
escrita meses depois nao tem como esquecer de definir um stop: ela nem participa
dessa etapa.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from ..domain.enums import ExchangeName, SignalDirection
from ..domain.models import Candle, IndicatorSnapshot, Signal
from ..logging_setup import get_logger

_log = get_logger(__name__)

MIN_WINDOW_WIDTH = 2e-4
"""Piso de largura RELATIVA da janela abaixo do qual nenhuma estrategia ABRE.

Toda estrategia aqui presume que houve movimento de preco para operar. Num par
sem liquidez o movimento nao e zero -- e minusculo -- e todo indicador
normalizado (RSI, %B, separacao de medias) e cego a diferenca: a razao entre
dois ruidos e um numero bem-comportado. Por isso o criterio tem que ser
RELATIVO ao preco, e nunca uma comparacao exata com zero: `avg_gain == 0.0`
fecha o caso sintetico e um tique de 1e-08 em qualquer barra o contorna.

Cada estrategia mede a largura com a grandeza adimensional natural dela, no
proprio periodo, e recusa ABRIR quando o minimo das DUAS barras que ela le fica
abaixo deste piso:

| estrategia | medida | periodo |
|---|---|---|
| rsi_reversion       | `RsiResult.relative_width` | `period` (14) |
| bollinger_reversion | `bandwidth`                | `period` (20) |
| ma_crossover        | `relative_move`            | `slow` (21)   |
| macd_trend          | `relative_move`            | `slow` (26)   |

`relative_width` e `relative_move` sao a MESMA grandeza por dois caminhos (ver a
identidade provada em `indicators/core.py`), o que permite um numero unico em
vez de quatro calibracoes independentes.

**De onde vem o 2e-4** (regra 2 do projeto: tres janelas independentes).
Medido em 2026-09-09 sobre as 34 series do banco (16 pares /USDC 1d, 16 /BRL 1d,
2 /USDT 15m) recortadas em tres janelas temporais DISJUNTAS, olhando so as
barras em que cada estrategia dispara a COMPRA e tomando o minimo das duas
barras lidas -- `n` = gatilhos reais, `min` = a menor largura entre eles:

| estrategia | J1 (candles 0-166) | J2 (167-333) | J3 (334-499) |
|---|---|---|---|
| rsi_reversion       | n=26  min=2,74e-02 | n=152 min=2,09e-03 | n=68  min=1,15e-03 |
| bollinger_reversion | n=140 min=5,32e-03 | n=215 min=2,52e-03 | n=122 min=3,04e-03 |
| ma_crossover        | n=119 min=1,33e-03 | n=81  min=4,23e-04 | n=139 min=6,19e-04 |
| macd_trend          | n=99  min=1,42e-03 | n=14  min=5,50e-04 | n=54  min=5,56e-04 |

E as janelas degeneradas dos ataques, na medida de cada estrategia: par plano
4,76e-06 a 7,14e-06; par plano + tique de 1e-08 identico (o tique nao muda a
ordem de grandeza); altcoin a 0,00123 USDC + tique 6,7e-07 a 8,2e-07; serrote de
um tique 4,9e-11; `bandwidth` do par morto + um centavo 8,72e-05.

Ha portanto uma banda VAZIA entre **8,72e-05** (a pior janela degenerada
medida, em qualquer das duas grandezas) e **4,23e-04** (o menor gatilho real
medido, em qualquer das quatro estrategias e das tres janelas). 2e-4 e o meio
geometrico dessa banda -- `sqrt(8,72e-05 * 4,23e-04) = 1,92e-04` --, com 2,3x de
folga para baixo e 2,1x para cima.

O que sustenta a escolha nao e o valor exato, e a INSENSIBILIDADE a ele: 1e-4,
2e-4 e 3e-4 silenciam **0 de 1.129** gatilhos reais nas tres janelas, e todos
recusam as sete janelas degeneradas atacadas. So a partir de 5e-4 o piso comeca
a silenciar mercado de verdade (2 gatilhos de ma_crossover em J2), e ai ele
deixaria de ser piso de operabilidade e passaria a ser filtro de regime -- que e
exatamente a familia de parametro que este projeto ja mediu e DESLIGOU tres
vezes por nao replicar fora da janela de escolha.

Ancora economica independente, apontando no mesmo sentido: 2e-4 e 0,02% de
movimento medio por barra, um quinto da taxa de spot de uma unica perna na
Binance. Uma janela abaixo do piso nao paga a ida e a volta nem se a operacao
acertar em cheio -- o piso recusa somente o que ja era inoperavel.

D15: este numero e parametro de NEGOCIO. Nasce como argumento de construtor,
igual a `oversold`, `overbought` e `std_multiplier`, editavel por
`get_strategy(nome, min_window_width=...)`, e **nunca** pode ir para o `.env` --
quando os parametros de estrategia ganharem casa no banco, este vai com eles.
"""


@dataclass(frozen=True)
class MarketFrame:
    """Janela de mercado entregue a estrategia.

    Contem apenas candles **fechados**: um sinal calculado sobre um candle ainda
    em formacao pode aparecer e sumir dentro do mesmo minuto, e cada oscilacao
    dessas viraria uma ordem.
    """

    exchange: ExchangeName
    symbol: str
    timeframe: str
    frame: pd.DataFrame
    """Colunas: open, high, low, close, volume. Indice: open_time (UTC), crescente."""

    def __post_init__(self) -> None:
        """Recusa janela com `open_time` repetido -- em QUALQUER caminho.

        Indice nao-unico significa a mesma barra contada duas vezes, e barra
        contada duas vezes deforma toda media movel que passa por ela. Depois de
        a recursao de `_smooth` virar posicional, isso deixou de estourar e
        passou a devolver **numero**: com o candle 30 de uma serie de 50
        duplicado, as quatro estrategias avaliam sem reclamar sobre uma serie
        que nao existiu. Trocar fail-closed por fail-open num dado corrompido e
        pior do que o `ValueError` que existia antes -- por isso a recusa e aqui,
        no `__post_init__`, e nao so no `from_candles`: o backtest de carteira
        monta `MarketFrame` direto de `DataFrame` (backtest/portfolio.py) e essa
        porta tem que fechar tambem.

        A recusa e do DADO, nao da direcao: nenhum sinal sai desta janela, nem
        de compra nem de fechamento. Nao ha assimetria a preservar aqui porque
        nao existe leitura confiavel nenhuma sobre a qual decidir.
        """
        if not self.frame.index.is_unique:
            repetidos = self.frame.index[self.frame.index.duplicated()].unique()
            raise ValueError(
                f"janela de {self.symbol} {self.timeframe} tem open_time repetido "
                f"({len(repetidos)} instante(s), o primeiro em {repetidos[0]}): "
                "barra contada duas vezes deforma as medias moveis; janela recusada"
            )

    @property
    def last_close(self) -> Decimal:
        return Decimal(str(self.frame["close"].iloc[-1]))

    @property
    def size(self) -> int:
        return len(self.frame)

    @classmethod
    def from_candles(cls, candles: list[Candle]) -> MarketFrame:
        if not candles:
            raise ValueError("MarketFrame exige ao menos um candle")

        closed = [c for c in candles if c.closed]
        if not closed:
            raise ValueError("nenhum candle fechado na janela recebida")

        frame = pd.DataFrame(
            {
                "open": [float(c.open) for c in closed],
                "high": [float(c.high) for c in closed],
                "low": [float(c.low) for c in closed],
                "close": [float(c.close) for c in closed],
                "volume": [float(c.volume) for c in closed],
            },
            index=pd.DatetimeIndex([c.open_time for c in closed], name="open_time"),
        ).sort_index()

        reference = closed[-1]
        return cls(
            exchange=reference.exchange,
            symbol=reference.symbol,
            timeframe=reference.timeframe,
            frame=frame,
        )


class Strategy(abc.ABC):
    """Plugin de estrategia."""

    name: str
    description: str = ""
    min_candles: int = 50
    """Janela minima para os indicadores aquecerem. Abaixo disso nao ha sinal."""

    min_window_width: float = MIN_WINDOW_WIDTH
    """Largura relativa minima da janela para ABRIR posicao. Ver `MIN_WINDOW_WIDTH`."""

    def __init__(self, **params: object) -> None:
        self.params = params

    @abc.abstractmethod
    def evaluate(self, market: MarketFrame) -> Signal | None:
        """Devolve um sinal, ou `None` quando nao ha nada a fazer.

        `None` e a resposta mais comum e mais saudavel: nao operar e uma decisao
        legitima na maioria dos candles.
        """
        ...

    # ------------------------------------------------------------------
    def _signal(
        self,
        market: MarketFrame,
        direction: SignalDirection,
        confidence: float,
        reason: str,
        indicators: dict[str, float | None],
    ) -> Signal:
        return Signal(
            exchange=market.exchange,
            symbol=market.symbol,
            timeframe=market.timeframe,
            strategy=self.name,
            direction=direction,
            # Confianca fora de [0,1] seria erro de programacao da estrategia;
            # limitamos aqui para que isso nunca derrube o pipeline inteiro.
            confidence=max(0.0, min(1.0, confidence)),
            reason=reason,
            reference_price=market.last_close,
            indicators=IndicatorSnapshot(values=indicators),
        )

    def _has_warmup(self, market: MarketFrame) -> bool:
        return market.size >= self.min_candles

    def _window_is_too_narrow(
        self, market: MarketFrame, width: float, medida: str, **contexto: object
    ) -> bool:
        """True quando a janela nao tem largura economica para ABRIR posicao.

        Chamada apenas no ramo de compra de cada estrategia. Fechar posicao
        nunca pode ser bloqueado por duvida sobre a qualidade do dado -- e a
        mesma assimetria do circuit breaker (D4): a duvida recusa entrar, jamais
        recusa sair.

        `width` NaN tambem recusa: "nao sei medir a largura" nao autoriza abrir.
        Recusa silenciosa e indistinguivel de estrategia que parou de funcionar,
        entao toda recusa aparece no log estruturado com o numero medido.
        """
        if not (width >= self.min_window_width):  # NaN cai aqui de proposito
            _log.info(
                "estrategia.janela_estreita",
                strategy=self.name,
                symbol=market.symbol,
                timeframe=market.timeframe,
                medida=medida,
                largura=clean(width),
                piso=self.min_window_width,
                motivo="janela sem largura economica; compra recusada, fechamento livre",
                **contexto,
            )
            return True
        return False


def clean(value: object) -> float | None:
    """Converte valor de indicador para JSON-safe, transformando NaN em None."""
    if value is None:
        return None
    number = float(value)
    return None if pd.isna(number) else number
