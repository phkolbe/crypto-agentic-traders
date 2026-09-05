"""Auxiliares compartilhados pelos testes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_traders.domain.enums import ExchangeName
from crypto_traders.domain.models import Candle


def make_candles(
    closes: list[float],
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "15m",
    start: datetime | None = None,
    closed: bool = True,
) -> list[Candle]:
    """Constroi uma serie de candles a partir de uma lista de fechamentos."""
    start = start or datetime(2026, 1, 1, tzinfo=UTC)
    candles = []
    for index, close in enumerate(closes):
        value = Decimal(str(close))
        candles.append(
            Candle(
                exchange=ExchangeName.BINANCE,
                symbol=symbol,
                timeframe=timeframe,
                open_time=start + timedelta(minutes=15 * index),
                open=value,
                high=value * Decimal("1.005"),
                low=value * Decimal("0.995"),
                close=value,
                volume=Decimal("10"),
                closed=closed,
            )
        )
    return candles
