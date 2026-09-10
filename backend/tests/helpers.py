"""Auxiliares compartilhados pelos testes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from crypto_traders.domain.enums import ExchangeName
from crypto_traders.domain.models import Candle

#: Raiz do repositorio, ancorada no arquivo e nao no diretorio de trabalho.
#: `backend/tests/helpers.py` -> `backend/tests` -> `backend` -> raiz.
RAIZ_DO_REPO = Path(__file__).resolve().parents[2]

#: Raiz do pacote de producao, para o teste que precisa LER codigo-fonte.
RAIZ_DO_BACKEND = RAIZ_DO_REPO / "backend"


def arquivo_do_repo(relativo: str) -> Path:
    """Caminho absoluto de um arquivo do repositorio.

    Existe porque `pathlib.Path("src/crypto_traders/...")` num teste depende do
    diretorio de trabalho de quem chamou o pytest: medido em 2026-09-10, a suite
    tem 1118 verdes rodando de `backend/` e 1 vermelho rodando da raiz do
    repositorio (`test_critico_item2_ataques.py` abre `src/...` relativo). Um
    teste que muda de resultado conforme o `cd` de quem roda nao mede o sistema.
    """
    return (RAIZ_DO_BACKEND / relativo).resolve()


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


def com_negocio(settings, **campos):
    """Copia `settings` trocando campos da configuracao de NEGOCIO.

    `settings.model_copy(update={"paper_initial_balance": ...})` nao falha e nao
    funciona: cria um atributo solto que ninguem le, porque o campo mora em
    `settings.trading`. Este helper existe para que o teste nao consiga errar
    silenciosamente desse jeito -- um campo inexistente aqui levanta erro.
    """
    return settings.model_copy(
        update={"trading": settings.trading.model_copy(update=campos, deep=True)}
    )
