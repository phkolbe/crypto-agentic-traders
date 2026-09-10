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
from typing import Any

from ..domain.models import Candle, OrderRequest, OrderResult, Position, Ticker


class ExchangeError(RuntimeError):
    """Falha ao falar com a exchange."""


class InsufficientFunds(ExchangeError):
    """Saldo insuficiente: erro de negocio, nao adianta retentar."""


class ApiAccessDenied(ExchangeError):
    """A exchange recusou a credencial: chave invalida, IP fora da whitelist ou
    permissao ausente.

    Merece um tipo proprio porque a consequencia operacional e diferente de
    qualquer outra falha. Os dados de mercado sao publicos e continuam chegando,
    entao o dashboard segue vivo e aparentemente saudavel -- mas o sistema
    perdeu a capacidade de **sair** de posicao. Se houver posicao aberta, o
    sinal de fechamento e aprovado pelo Risk Manager e a ordem morre na
    exchange, sem que nada na tela indique isso.

    Na Binance, o caso mais comum e o IP residencial ter mudado (o endereco da
    whitelist e dinamico), e o codigo devolvido e `-2015`.
    """

    def __init__(self, message: str, *, exchange: str = "", operation: str = "") -> None:
        super().__init__(message)
        self.exchange = exchange
        self.operation = operation


#: Duracao de cada unidade de timeframe do ccxt, em segundos.
#:
#: `m` (minuto) e `M` (mes) diferem SO pela caixa -- normalizar a string para
#: minusculo confundiria os dois por um fator de 43.200. Por isso a busca aqui e
#: sensivel a caixa, de proposito.
_TIMEFRAME_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3_600,
    "d": 86_400,
    "w": 604_800,
    "M": 2_592_000,  # 30 dias: aproximacao suficiente para "este periodo fechou?"
}


def timeframe_seconds(timeframe: str) -> int:
    """Duracao de um candle em segundos ("15m" -> 900).

    Serve para responder a unica pergunta que a exchange nao responde de forma
    confiavel: **este candle ja fechou?** `open_time + duracao <= agora` e a
    unica verificacao que nao depende da ordem em que a exchange devolveu as
    linhas nem do relogio dela.

    Levanta `ValueError` no que nao souber interpretar. Recusar e mais seguro do
    que adivinhar: com duracao errada, candle em formacao passaria por fechado.
    """
    texto = timeframe.strip()
    if len(texto) < 2:
        raise ValueError(f"timeframe invalido: {timeframe!r}")
    quantidade, unidade = texto[:-1], texto[-1]
    if unidade not in _TIMEFRAME_UNITS:
        raise ValueError(f"unidade de timeframe desconhecida: {timeframe!r}")
    if not quantidade.isdigit() or int(quantidade) <= 0:
        raise ValueError(f"quantidade de timeframe invalida: {timeframe!r}")
    return int(quantidade) * _TIMEFRAME_UNITS[unidade]


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
    async def fetch_markets_and_tickers(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Catalogo de mercados e o resumo de 24h de todos eles.

        Base da descoberta automatica de pares. Devolve os dicionarios crus da
        exchange de proposito: quem seleciona e `discovery.select_markets`, que
        e uma funcao pura e testavel sem rede.
        """
        ...

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
