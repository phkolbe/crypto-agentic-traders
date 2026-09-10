"""Ataques da rodada 4 do critico ao item 7 (camada de persistencia).

A barreira que protege as trilhas append-only contra SQL cru
(`db.models._recusar_sql_cru_contra_a_protecao`) e uma lista de palavras
proibidas, casada por expressao regular sobre o texto do comando. Lista de
palavras proibidas nao e barreira: ela para exatamente as grafias que quem a
escreveu imaginou, e quem quer passar so precisa escrever de outro jeito.

Cada teste aqui e o ataque completo da rodada 2 -- derrubar o gatilho e
reescrever a linha de auditoria em seguida -- com UMA mudanca cosmetica no
comando. Os tres passam contra o codigo de hoje, ou seja: a auditoria volta a
ser reescrivel pela propria aplicacao, que e a brecha que o item 7 declarou
fechada.

Medido antes de escrever: `audit_log` volta com `action='ADULTERADO'` nos tres
casos.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from crypto_traders.db import models as orm
from crypto_traders.db.models import AuditoriaImutavelError
from crypto_traders.db.session import get_engine, session_scope

pytestmark = [pytest.mark.asyncio]

#: O gatilho que recusa `UPDATE` em `audit_log`. Enquanto ele existir, a linha
#: de auditoria nao muda; sem ele, um `UPDATE` cru passa direto.
GATILHO = "audit_log_sem_update"


async def _uma_linha_de_auditoria(settings) -> None:
    async with session_scope(settings) as ses:
        ses.add(orm.AuditLog(actor="paulo", action="risk_limits_updated"))


async def _gatilhos(settings) -> set[str]:
    async with session_scope(settings) as ses:
        return set(
            (
                await ses.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))
            ).scalars()
        )


async def _acao_gravada(settings) -> str:
    async with session_scope(settings) as ses:
        return (await ses.execute(select(orm.AuditLog))).scalars().one().action


@pytest.mark.parametrize(
    ("apelido", "sabotagem"),
    [
        # `/**/` e espaco em branco para o SQLite, mas nao e `\s` para a regex
        # `\bdrop\s+trigger\b`. Dois caracteres a mais e o comando passa.
        ("comentario de bloco colado", f"DROP/**/TRIGGER {GATILHO}"),
        ("comentario de bloco com texto", f"DROP /* nada aqui */ TRIGGER {GATILHO}"),
        # `--` ate o fim da linha tambem e espaco em branco para o SQLite.
        ("comentario de linha", f"DROP--x\nTRIGGER {GATILHO}"),
    ],
)
async def test_comentario_sql_atravessa_a_barreira_e_a_auditoria_e_reescrita(
    settings, apelido, sabotagem
):
    """O ataque da rodada 2, com um comentario no meio do comando.

    A barreira deve recusar; o gatilho deve continuar no ar; a linha deve
    continuar `risk_limits_updated`.
    """
    await _uma_linha_de_auditoria(settings)

    engine = get_engine(settings)
    with pytest.raises(AuditoriaImutavelError):
        async with engine.begin() as conn:
            await conn.exec_driver_sql(sabotagem)

    assert GATILHO in await _gatilhos(settings), f"{apelido}: o gatilho saiu do ar"

    # E, com o gatilho no ar, o UPDATE cru continua sendo recusado pelo banco.
    async with engine.begin() as conn:
        with pytest.raises(Exception, match="append-only"):
            await conn.exec_driver_sql("UPDATE audit_log SET action = 'ADULTERADO'")

    assert await _acao_gravada(settings) == "risk_limits_updated"


@pytest.mark.parametrize(
    ("apelido", "pragma"),
    [
        ("writable_schema qualificado", "PRAGMA main.writable_schema=ON"),
        ("recursive_triggers qualificado", "PRAGMA temp.recursive_triggers=off"),
    ],
)
async def test_pragma_qualificado_por_schema_atravessa_a_barreira(settings, apelido, pragma):
    """`PRAGMA main.x` e o mesmo pragma que `PRAGMA x`, e a regex so ve o segundo.

    Com `writable_schema` ligado, `DELETE FROM sqlite_master WHERE name = ...`
    apaga o gatilho sem emitir a palavra `TRIGGER` em lugar nenhum -- medido: a
    cadeia inteira passa e o gatilho some.
    """
    engine = get_engine(settings)
    with pytest.raises(AuditoriaImutavelError):
        async with engine.begin() as conn:
            await conn.exec_driver_sql(pragma)

    assert GATILHO in await _gatilhos(settings), f"{apelido}: o gatilho saiu do ar"


async def test_cadeia_writable_schema_apaga_o_gatilho_sem_dizer_trigger(settings):
    """A cadeia completa, do jeito que ela funciona hoje.

    Nenhum dos tres comandos casa com a lista de palavras proibidas na grafia em
    que ela foi escrita, e ao fim o gatilho nao existe mais.
    """
    await _uma_linha_de_auditoria(settings)
    engine = get_engine(settings)

    with pytest.raises(AuditoriaImutavelError):
        async with engine.begin() as conn:
            await conn.exec_driver_sql("PRAGMA main.writable_schema=ON")
            await conn.exec_driver_sql(
                f"DELETE FROM sqlite_master WHERE name = '{GATILHO}'"
            )
            await conn.exec_driver_sql("PRAGMA main.writable_schema=OFF")

    assert GATILHO in await _gatilhos(settings), "a cadeia writable_schema apagou o gatilho"
    assert await _acao_gravada(settings) == "risk_limits_updated"
