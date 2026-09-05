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


def clean(value: object) -> float | None:
    """Converte valor de indicador para JSON-safe, transformando NaN em None."""
    if value is None:
        return None
    number = float(value)
    return None if pd.isna(number) else number
