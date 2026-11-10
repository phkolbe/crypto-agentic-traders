"""Copia o historico de um banco SQLite para o PostgreSQL, uma vez, na migracao.

O sistema rodou semanas em `dry_run` sobre SQLite antes de ir para o Docker, e
esse historico (candles, sinais, decisoes de risco, ordens, trades, snapshots,
auditoria e a configuracao de negocio) e o que da contexto ao dashboard. Esta
copia leva tudo para o Postgres do Docker sem reinterpretar nada:

- le cada tabela pelos MESMOS modelos do sistema, entao `Money` e as datas
  passam pela mesma normalizacao da leitura de todo dia;
- escreve no destino so com INSERT -- os gatilhos append-only nao sao tocados,
  e nem precisariam: eles recusam UPDATE/DELETE, nao INSERT;
- recusa rodar se o destino ja tiver qualquer linha (copia por cima de dados
  vivos misturaria dois historicos sem volta);
- e uma transacao so: ou o historico inteiro chega, ou nada chega;
- confere no fim, tabela a tabela, que o destino tem as mesmas linhas que a origem.

O destino precisa do schema criado (`crypto-traders migrar`) e de um usuario que
possa ajustar as sequencias (o dono do banco).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Integer, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..logging_setup import get_logger
from .models import Base

log = get_logger(__name__)

LOTE = 2_000


class CopiaRecusadaError(RuntimeError):
    """O destino nao esta em condicao de receber a copia."""


def _com_fuso(linha: dict[str, Any]) -> dict[str, Any]:
    """O SQLite devolve as datas sem fuso; o sistema sempre gravou UTC."""
    return {
        chave: valor.replace(tzinfo=UTC)
        if isinstance(valor, datetime) and valor.tzinfo is None
        else valor
        for chave, valor in linha.items()
    }


async def copiar_sqlite_para(destino: AsyncEngine, arquivo_sqlite: Path) -> dict[str, int]:
    """Copia todas as tabelas do modelo. Devolve as linhas copiadas por tabela."""
    if destino.dialect.name != "postgresql":
        raise CopiaRecusadaError("o destino da copia tem de ser o PostgreSQL")
    if not arquivo_sqlite.is_file():
        raise CopiaRecusadaError(f"arquivo SQLite nao encontrado: {arquivo_sqlite}")

    # Somente leitura: a copia nunca pode mexer no banco de origem.
    origem = create_async_engine(
        f"sqlite+aiosqlite:///file:{arquivo_sqlite.resolve().as_posix()}?mode=ro&uri=true"
    )
    tabelas = Base.metadata.sorted_tables
    copiadas: dict[str, int] = {}
    try:
        async with destino.begin() as conn:
            for tabela in tabelas:
                existentes = (await conn.execute(select(func.count()).select_from(tabela))).scalar()
                if existentes:
                    raise CopiaRecusadaError(
                        f"o destino ja tem {existentes} linha(s) em '{tabela.name}'; "
                        "a copia so roda num banco recem-migrado e vazio"
                    )

            async with origem.connect() as fonte:
                for tabela in tabelas:
                    total = 0
                    resultado = await fonte.stream(select(tabela))
                    async for parte in resultado.mappings().partitions(LOTE):
                        await conn.execute(tabela.insert(), [_com_fuso(dict(m)) for m in parte])
                        total += len(parte)
                    copiadas[tabela.name] = total
                    log.info("copia.tabela", tabela=tabela.name, linhas=total)

            # Ids copiados com valor explicito nao avancam a sequencia: sem isto,
            # o primeiro INSERT novo colidiria com uma linha copiada.
            for tabela in tabelas:
                for coluna in tabela.primary_key.columns:
                    if isinstance(coluna.type, Integer) and coluna.autoincrement in (True, "auto"):
                        await conn.execute(
                            text(
                                "SELECT setval(pg_get_serial_sequence(:t, :c), "
                                f"COALESCE((SELECT MAX({coluna.name}) FROM {tabela.name}), 1), "
                                f"(SELECT MAX({coluna.name}) FROM {tabela.name}) IS NOT NULL)"
                            ),
                            {"t": tabela.name, "c": coluna.name},
                        )

            for tabela in tabelas:
                no_destino = (await conn.execute(select(func.count()).select_from(tabela))).scalar()
                if no_destino != copiadas[tabela.name]:
                    raise CopiaRecusadaError(
                        f"'{tabela.name}': {copiadas[tabela.name]} lidas e {no_destino} no "
                        "destino; nada foi gravado"
                    )
    finally:
        await origem.dispose()
    return copiadas
