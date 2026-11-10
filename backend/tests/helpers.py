"""Auxiliares compartilhados pelos testes."""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy.engine import make_url

from crypto_traders.domain.enums import ExchangeName
from crypto_traders.domain.models import Candle

#: Raiz do repositorio, ancorada no arquivo e nao no diretorio de trabalho.
#: `backend/tests/helpers.py` -> `backend/tests` -> `backend` -> raiz.
RAIZ_DO_REPO = Path(__file__).resolve().parents[2]

#: Raiz do pacote de producao, para o teste que precisa LER codigo-fonte.
RAIZ_DO_BACKEND = RAIZ_DO_REPO / "backend"


#: O Postgres do SISTEMA (`docker-compose.yml`): database `crypto`, porta 5433
#: publicada no host (`PORTA_POSTGRES` muda) e o nome do servico/conteiner dentro
#: da rede do compose. E nele que o ensaio em dry_run guarda o historico que da
#: preco medio -- e portanto stop -- as posicoes, e `trades`/`audit_log` sao
#: append-only: a linha que a suite gravar la NAO sai depois.
DATABASE_DO_SISTEMA = "crypto"
PORTA_DO_SISTEMA = 5433
HOSTS_DO_SISTEMA = frozenset({"crypto-postgres", "postgres"})

#: Unica forma de rodar a suite contra um desses alvos. Nao existe caso de uso
#: no dia a dia; existe para o dia em que alguem precisar, e ter de escrever o
#: nome inteiro e a confirmacao de que sabe o que esta fazendo.
LIBERACAO_DO_BANCO_DO_SISTEMA = "CRYPTO_TESTES_ACEITAM_BANCO_DO_SISTEMA"


def e_o_banco_do_sistema(url: object) -> bool:
    """A URL aponta para o Postgres do sistema (database, porta ou host)?

    Qualquer um dos tres basta. Pela porta: `127.0.0.1:5433/crypto_teste` e
    OUTRO database, mas no mesmo servidor e com o superusuario dele -- um teste
    que errasse o nome do banco (ou um `DROP ... WITH (FORCE)`) ja estaria la
    dentro. O README ensinava exatamente esse caminho.
    """
    texto = url.decode("utf-8", "replace") if isinstance(url, bytes) else str(url or "")
    if not texto.startswith("postgres"):
        return False
    try:
        alvo = make_url(texto)
    except Exception:
        return False
    portas = {PORTA_DO_SISTEMA}
    porta_do_ambiente = os.environ.get("PORTA_POSTGRES", "")
    if porta_do_ambiente.isdigit():
        portas.add(int(porta_do_ambiente))
    return (
        alvo.database == DATABASE_DO_SISTEMA
        or alvo.port in portas
        or (alvo.host or "").lower() in HOSTS_DO_SISTEMA
    )


def recusa_do_banco_do_sistema(ambiente: dict[str, str]) -> str | None:
    """Motivo para a suite NAO rodar com este ambiente, ou `None`."""
    if ambiente.get(LIBERACAO_DO_BANCO_DO_SISTEMA) == "1":
        return None
    for variavel in ("DATABASE_URL", "POSTGRES_TEST_URL"):
        valor = ambiente.get(variavel)
        if valor and e_o_banco_do_sistema(valor):
            alvo = make_url(valor).render_as_string(hide_password=True)
            return (
                f"{variavel}={alvo} aponta para o Postgres do sistema (database "
                f"'{DATABASE_DO_SISTEMA}', porta {PORTA_DO_SISTEMA} ou host do conteiner). "
                "A suite grava em tabelas append-only: use um Postgres descartavel, ex. "
                "`docker run --rm -d -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=postgres "
                "postgres:16` e POSTGRES_TEST_URL=postgresql+asyncpg://postgres:postgres@"
                f"127.0.0.1:55432/postgres. Para forcar, {LIBERACAO_DO_BANCO_DO_SISTEMA}=1."
            )
    return None


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


async def esperar_ate(
    condicao: Callable[[], object],
    *,
    prazo: float = 2.0,
    intervalo: float = 0.01,
    mensagem: str = "",
) -> None:
    """Espera ATE a condicao valer, com prazo, em vez de um `sleep` fixo.

    O `sleep(0.2)` "para dar tempo" erra para os dois lados: num CI lento a
    tarefa ainda nao chegou la e o teste falha sem defeito; numa maquina rapida
    gasta tempo a toa. Aqui a espera acaba no instante em que a condicao vale, e
    o prazo so estoura quando ela de fato nao acontece -- com a mensagem do teste.

    A condicao pode ser sincrona ou devolver um awaitable.
    """
    limite = time.monotonic() + prazo
    while True:
        valor = condicao()
        if inspect.isawaitable(valor):
            valor = await valor
        if valor:
            return
        if time.monotonic() >= limite:
            raise AssertionError(mensagem or f"a condicao nao se cumpriu em {prazo}s")
        await asyncio.sleep(intervalo)
