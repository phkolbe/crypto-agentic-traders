"""Contrato de exchange.

Separa "ler mercado" de "enviar ordem" em duas interfaces deliberadamente:
`MarketDataSource` usa apenas endpoints publicos (sem chave de API), enquanto
`Broker` e o unico caminho que toca dinheiro. Assim o Market Data Agent nunca
recebe credenciais, e o codigo com poder de gastar fica em uma superficie
pequena e auditavel.
"""

from __future__ import annotations

import abc
from decimal import Decimal

from ..domain.models import Candle, OrderRequest, OrderResult, Position, Ticker


class ExchangeError(RuntimeError):
    """Falha ao falar com a exchange."""


class InsufficientFunds(ExchangeError):
    """Saldo insuficiente: erro de negocio, nao adianta retentar."""


class MarketDataSource(abc.ABC):
    """Leitura de mercado. Nao requer credenciais."""

    name: str

    @abc.abstractmethod
    async def fetch_candles(
        self, symbol: str, timeframe: str, limit: int = 500
    ) -> list[Candle]: ...

    @abc.abstractmethod
    async def fetch_ticker(self, symbol: str) -> Ticker: ...

    @abc.abstractmethod
    async def close(self) -> None: ...


class Broker(abc.ABC):
    """Envio de ordens e leitura de saldo. Requer credenciais (exceto no paper)."""

    name: str

    @abc.abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResult: ...

    @abc.abstractmethod
    async def fetch_balances(self) -> dict[str, Decimal]: ...

    @abc.abstractmethod
    async def fetch_positions(self, prices: dict[str, Decimal]) -> list[Position]: ...

    @abc.abstractmethod
    async def close(self) -> None: ...
