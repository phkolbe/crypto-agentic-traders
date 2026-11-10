"""Migracoes versionadas do schema e a conferencia do banco contra o modelo.

Ate 09/11/2026 o `crypto-traders migrar` era so `create_all`, que cria tabela
AUSENTE e nada mais: coluna nova, indice novo, NOT NULL ou constraint em tabela
que ja existe nunca chegavam ao banco, e a subida conferia so os gatilhos. O
cenario do relatorio (auditoria, banco F2): uma versao acrescenta uma coluna em
`trades`, o `migrar` termina "schema e gatilhos prontos", a aplicacao sobe, e no
primeiro fill o INSERT de `trades` falha -- a transacao de `apply_result` +
`record` volta atras e a ordem executada na exchange fica PENDING e sem trade.

Duas pecas, sem Alembic (dependencia nova de producao, e o schema e pequeno):

1. **Migracoes numeradas** (`MIGRACOES`), aplicadas pelo `migrar` com o DONO do
   banco, dentro da mesma transacao do schema e dos gatilhos, e registradas em
   `schema_versao`. Banco criado do zero e carimbado na versao atual (o
   `create_all` ja o criou como o modelo diz); banco que ja existia sem a tabela
   de versao e a linha de base (versao 1, o schema do `create_all` ate aqui) e
   recebe as migracoes seguintes.
2. **Conferencia** (`divergencias_do_schema`): colunas, nulabilidade, tipos (no
   Postgres), chave primaria, indices e restricoes do modelo, perguntados ao
   BANCO. A subida da aplicacao recusa se a versao gravada nao for a deste
   codigo ou se o banco divergir do modelo; o `migrar` confere no fim tambem, e
   falha se as migracoes nao levaram o banco ao modelo.

Regras para a proxima migracao: numero novo no fim de `MIGRACOES`, idempotente
(`IF NOT EXISTS`, ou perguntar ao catalogo antes), e o modelo em `models.py`
mudado no mesmo commit -- a conferencia do fim do `migrar` pega o esquecimento.
Migracao de DADOS em `trades`/`risk_events`/`audit_log` exige derrubar gatilho,
e NAO entra aqui: e procedimento manual do dono, numa transacao do Postgres (la
o DDL e transacional), religando o gatilho antes do COMMIT.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    inspect,
    select,
    text,
)

from ..logging_setup import get_logger
from .models import _OPCAO_INTERNA, CHECKS_DE_TRADES, Base

log = get_logger(__name__)


class SchemaDivergenteError(RuntimeError):
    """O banco nao e o que este codigo espera. O sistema nao sobe assim."""


class MigracaoRecusadaError(RuntimeError):
    """Uma migracao achou dado que ela nao pode resolver sozinha."""


#: Fora de `Base.metadata` de proposito: a copia do SQLite (`db.copia`) e a
#: conferencia percorrem o metadata do modelo, e a versao do schema nao e dado
#: a copiar -- o destino tem a dele, carimbada pelo `migrar`.
_METADATA_VERSAO = MetaData()
SCHEMA_VERSAO = Table(
    "schema_versao",
    _METADATA_VERSAO,
    Column("versao", Integer, primary_key=True, autoincrement=False),
    Column("descricao", Text, nullable=False),
    Column("aplicada_em", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class Migracao:
    versao: int
    descricao: str
    aplicar: Callable[[Connection], None]


def _executar(connection: Connection, comando: str) -> None:
    # Marcado como interno: `ALTER TABLE trades` e recusado pela barreira de
    # texto para todo o resto do processo, e a migracao e o caminho do dono.
    connection.execute(text(comando).execution_options(**_OPCAO_INTERNA))


def _linha_de_base(connection: Connection) -> None:
    """Versao 1: o schema que o `create_all` criava ate 09/11/2026. Nada a fazer."""


def _recusar_se_houver(connection: Connection, consulta: str, problema: str) -> None:
    linhas = connection.execute(text(consulta).execution_options(**_OPCAO_INTERNA)).all()
    if linhas:
        exemplos = ", ".join(" x".join(str(v) for v in linha) for linha in linhas[:10])
        raise MigracaoRecusadaError(
            f"{problema}: {exemplos}{' ...' if len(linhas) > 10 else ''}. `trades` e "
            "append-only, entao a migracao nao corrige isso sozinha: o dono decide, numa "
            "transacao propria, e roda o `migrar` de novo. Nada foi alterado."
        )


def _v2_trades_um_por_ordem(connection: Connection) -> None:
    """Um trade de agente por ordem, dominio fechado e o indice do razao.

    Antes de criar a restricao, procura o que ela recusaria: um trade de agente
    repetido ja gravado ou um valor fora do dominio. Achando, para com a lista --
    criar o indice falharia de qualquer jeito, com uma mensagem pior.
    """
    _recusar_se_houver(
        connection,
        "SELECT order_id, count(*) FROM trades WHERE origin = 'agent' "
        "AND order_id IS NOT NULL GROUP BY order_id HAVING count(*) > 1",
        "ha trades de agente repetidos para a mesma ordem (order_id x quantidade)",
    )
    _executar(
        connection,
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_order_agent ON trades (order_id) "
        "WHERE origin = 'agent'",
    )
    _executar(
        connection,
        'CREATE INDEX IF NOT EXISTS ix_audit_action_ts ON audit_log (action, "timestamp")',
    )
    if connection.dialect.name != "postgresql":
        # SQLite nao acrescenta CHECK a tabela existente (so recriando a tabela,
        # o que em `trades` exigiria derrubar o gatilho). Banco SQLite novo nasce
        # com elas pelo `create_all`; o antigo segue sem, e a conferencia nao as
        # cobra no SQLite. Producao e o Postgres.
        return
    existentes = set(
        connection.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'trades'::regclass "
                "AND contype = 'c'"
            )
        ).scalars()
    )
    for nome, regra in CHECKS_DE_TRADES.items():
        if nome in existentes:
            continue
        coluna = regra.split(" ", 1)[0]
        _recusar_se_houver(
            connection,
            f"SELECT {coluna}, count(*) FROM trades WHERE {coluna} IS NULL OR NOT ({regra}) "
            f"GROUP BY {coluna}",
            f"ha trades com {coluna} fora de [{regra}] (valor x quantidade)",
        )
        _executar(connection, f"ALTER TABLE trades ADD CONSTRAINT {nome} CHECK ({regra})")


MIGRACOES: tuple[Migracao, ...] = (
    Migracao(1, "linha de base: o schema do create_all ate 09/11/2026", _linha_de_base),
    Migracao(
        2,
        "trades: um trade de agente por ordem e CHECK em origin/side/mode; "
        "audit_log: indice (action, timestamp)",
        _v2_trades_um_por_ordem,
    ),
)
VERSAO_ATUAL = MIGRACOES[-1].versao


def _versoes_aplicadas(connection: Connection) -> set[int]:
    return set(connection.execute(select(SCHEMA_VERSAO.c.versao)).scalars())


def _carimbar(connection: Connection, migracao: Migracao) -> None:
    connection.execute(
        SCHEMA_VERSAO.insert().values(
            versao=migracao.versao,
            descricao=migracao.descricao,
            aplicada_em=datetime.now(UTC),
        )
    )


def aplicar_migracoes(connection: Connection) -> list[int]:
    """Leva o banco a `VERSAO_ATUAL`. So no `migrar` (ou subida com migrar=True).

    Devolve as versoes aplicadas agora (vazio quando ja estava em dia ou quando
    o banco acabou de ser criado). Roda na transacao de quem chama: no Postgres,
    uma migracao que falha desfaz tudo, inclusive o carimbo.
    """
    existentes = set(inspect(connection).get_table_names())
    if SCHEMA_VERSAO.name in existentes:
        # ANTES do `create_all`: codigo antigo nao cria, num banco que uma
        # versao nova migrou, a tabela que ela tirou (no SQLite o DDL nem volta
        # atras com o rollback).
        _recusar_versao_mais_nova(_versoes_aplicadas(connection))
    banco_novo = not (existentes & set(Base.metadata.tables))
    _METADATA_VERSAO.create_all(connection)
    Base.metadata.create_all(connection)

    aplicadas = _versoes_aplicadas(connection)
    if not aplicadas:
        if banco_novo:
            # O `create_all` acabou de criar o schema como o modelo diz: todas
            # as migracoes ja estao, por construcao, no banco.
            for migracao in MIGRACOES:
                _carimbar(connection, migracao)
            log.info("db.schema_criado", versao=VERSAO_ATUAL)
            return []
        _carimbar(connection, MIGRACOES[0])
        aplicadas = {MIGRACOES[0].versao}
        log.warning(
            "db.schema_sem_versao",
            detail="banco anterior as migracoes versionadas; tratado como a linha de base (1)",
        )
    _recusar_versao_mais_nova(aplicadas)

    feitas: list[int] = []
    for migracao in MIGRACOES:
        if migracao.versao in aplicadas:
            continue
        migracao.aplicar(connection)
        _carimbar(connection, migracao)
        feitas.append(migracao.versao)
        log.info("db.migracao_aplicada", versao=migracao.versao, descricao=migracao.descricao)
    return feitas


def _recusar_versao_mais_nova(aplicadas: set[int]) -> None:
    desconhecidas = sorted(v for v in aplicadas if v > VERSAO_ATUAL)
    if desconhecidas:
        raise SchemaDivergenteError(
            f"o banco esta na versao {max(desconhecidas)} do schema e este codigo conhece "
            f"ate a {VERSAO_ATUAL}: e codigo ANTIGO contra banco migrado por uma versao "
            "mais nova. Suba a versao do codigo que migrou o banco."
        )


def verificar_versao(connection: Connection) -> int:
    """Subida da aplicacao: a versao gravada tem de ser exatamente a deste codigo."""
    if SCHEMA_VERSAO.name not in inspect(connection).get_table_names():
        raise SchemaDivergenteError(
            "o banco nao tem a tabela schema_versao: foi criado antes das migracoes "
            f"versionadas. Rode `crypto-traders migrar` com o usuario dono do banco "
            f"(leva o banco a versao {VERSAO_ATUAL}) antes de subir."
        )
    aplicadas = _versoes_aplicadas(connection)
    _recusar_versao_mais_nova(aplicadas)
    faltando = [m.versao for m in MIGRACOES if m.versao not in aplicadas]
    if faltando:
        raise SchemaDivergenteError(
            f"faltam as migracoes {faltando} neste banco (este codigo precisa da versao "
            f"{VERSAO_ATUAL}). Rode `crypto-traders migrar` com o usuario dono do banco "
            "antes de subir."
        )
    return VERSAO_ATUAL


def _tipo(tipo: Any, connection: Connection) -> str:
    """O tipo como o Postgres o escreve, para comparar modelo e banco."""
    compilado = tipo.compile(dialect=connection.dialect).upper().replace(" ", "")
    # `Float` sem precisao vira `FLOAT` no DDL e o Postgres o guarda como
    # `double precision`; e o mesmo tipo.
    return {"FLOAT": "DOUBLEPRECISION", "FLOAT(53)": "DOUBLEPRECISION"}.get(
        compilado, compilado
    )


def divergencias_do_schema(connection: Connection) -> list[str]:
    """O que o banco tem de diferente do modelo, do jeito que quebraria a escrita.

    Conta (recusa a subida): tabela ou coluna do modelo ausente; coluna NOT
    NULL no banco e anulavel no modelo (o INSERT de None falharia); tipo
    diferente (so no Postgres -- o SQLite aceita qualquer tipo em qualquer
    coluna, e o banco SQLite antigo guarda dinheiro com outra declaracao);
    coluna que so o banco tem e que e NOT NULL sem default (o INSERT do modelo
    falharia); chave primaria diferente; indice UNIQUE ou restricao UNIQUE do
    modelo ausente ou diferente (sem ele o banco aceita a duplicata que o modelo
    promete recusar -- e em `trades` ela e irremovivel); e (Postgres) CHECK do
    modelo ausente.

    Vai so para o log (WARNING `db.schema_diferente_sem_risco`), porque nao
    impede gravar nem afrouxa promessa do modelo: coluna anulavel no banco onde
    o modelo e NOT NULL (o banco SQLite antigo e assim), indice comum ausente
    (custa desempenho, nao dado). E nem isso para o que so sobra no banco: tabela
    fora do modelo, indice a mais (o TimescaleDB cria os dele).
    """
    insp = inspect(connection)
    postgres = connection.dialect.name == "postgresql"
    existentes = set(insp.get_table_names())
    problemas: list[str] = []
    sem_risco: list[str] = []
    for tabela in Base.metadata.sorted_tables:
        nome = tabela.name
        if nome not in existentes:
            problemas.append(f"{nome}: tabela ausente")
            continue
        colunas = {c["name"]: c for c in insp.get_columns(nome)}
        for coluna in tabela.columns:
            no_banco = colunas.pop(coluna.name, None)
            if no_banco is None:
                problemas.append(f"{nome}.{coluna.name}: coluna ausente")
                continue
            if bool(no_banco["nullable"]) != bool(coluna.nullable):
                (sem_risco if no_banco["nullable"] else problemas).append(
                    f"{nome}.{coluna.name}: "
                    f"{'anulavel' if no_banco['nullable'] else 'NOT NULL'} no banco, "
                    f"{'anulavel' if coluna.nullable else 'NOT NULL'} no modelo"
                )
            if postgres:
                no_modelo, gravado = _tipo(coluna.type, connection), _tipo(
                    no_banco["type"], connection
                )
                if no_modelo != gravado:
                    problemas.append(
                        f"{nome}.{coluna.name}: tipo {gravado} no banco, {no_modelo} no modelo"
                    )
        for sobra, info in colunas.items():
            if not info["nullable"] and info.get("default") is None:
                problemas.append(
                    f"{nome}.{sobra}: coluna NOT NULL sem default que o modelo nao conhece "
                    "(o INSERT do sistema falharia)"
                )
            else:
                sem_risco.append(f"{nome}.{sobra}: coluna fora do modelo (anulavel ou com default)")

        pk_banco = insp.get_pk_constraint(nome).get("constrained_columns") or []
        pk_modelo = [c.name for c in tabela.primary_key.columns]
        if sorted(pk_banco) != sorted(pk_modelo):
            problemas.append(f"{nome}: chave primaria {pk_banco} no banco, {pk_modelo} no modelo")

        indices = {i["name"]: i for i in insp.get_indexes(nome)}
        for indice in tabela.indexes:
            refletido = indices.get(indice.name)
            colunas_modelo = [c.name for c in indice.columns]
            destino = problemas if indice.unique else sem_risco
            if refletido is None:
                destino.append(f"{nome}: indice {indice.name} {colunas_modelo} ausente")
            elif list(refletido["column_names"]) != colunas_modelo or bool(
                refletido.get("unique")
            ) != bool(indice.unique):
                destino.append(
                    f"{nome}: indice {indice.name} e {refletido['column_names']}"
                    f"{' UNIQUE' if refletido.get('unique') else ''} no banco, "
                    f"{colunas_modelo}{' UNIQUE' if indice.unique else ''} no modelo"
                )

        unicas = {u["name"]: u for u in insp.get_unique_constraints(nome)}
        checks = (
            {c["name"] for c in insp.get_check_constraints(nome)} if postgres else set()
        )
        for restricao in tabela.constraints:
            if isinstance(restricao, UniqueConstraint):
                colunas_modelo = [c.name for c in restricao.columns]
                refletida = unicas.get(restricao.name)
                if refletida is None or list(refletida["column_names"]) != colunas_modelo:
                    problemas.append(
                        f"{nome}: restricao UNIQUE {restricao.name} {colunas_modelo} ausente "
                        "ou diferente"
                    )
            elif postgres and isinstance(restricao, CheckConstraint) and restricao.name:
                if restricao.name not in checks:
                    problemas.append(f"{nome}: restricao CHECK {restricao.name} ausente")
    if sem_risco:
        log.warning(
            "db.schema_diferente_sem_risco",
            diferencas=sem_risco,
            detail="nao impede gravar; o banco segue diferente do modelo ate uma migracao",
        )
    return problemas


def verificar_schema(connection: Connection) -> None:
    """Recusa com a lista inteira, e nao com a primeira diferenca."""
    problemas = divergencias_do_schema(connection)
    if problemas:
        raise SchemaDivergenteError(
            "o banco diverge do modelo deste codigo, e gravar nele perderia dado "
            "(um fill executado na exchange ficaria PENDING sem trade): "
            + "; ".join(problemas)
            + ". Rode `crypto-traders migrar` com o usuario dono do banco; se a "
            "divergencia continuar, falta uma migracao em `db/migracoes.py`."
        )
