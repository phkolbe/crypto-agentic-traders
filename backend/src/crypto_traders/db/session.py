"""Engine e sessao assincrona do SQLAlchemy.

O mesmo codigo atende SQLite (padrao local) e PostgreSQL/TimescaleDB, escolhido
apenas pela `DATABASE_URL`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import Settings, get_settings
from ..logging_setup import get_logger
from .models import (
    Base,
    aplicar_protecao_append_only,
    avisar_se_a_aplicacao_pode_derrubar_gatilho,
    hypertable_possivel,
    instalar_autorizador_sqlite,
    relatar_dinheiro_fora_de_escala,
    verificar_protecao_append_only,
)

log = get_logger(__name__)

#: Series temporais que viram hypertable quando o TimescaleDB esta presente.
HYPERTABLES: tuple[tuple[str, str], ...] = (
    ("candles", "open_time"),
    ("portfolio_snapshots", "timestamp"),
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _tune_sqlite(engine: AsyncEngine) -> None:
    """WAL, chaves estrangeiras, gatilhos recursivos e o autorizador do SQLite.

    Sem WAL, a escrita dos agentes bloqueia a leitura da API e o dashboard
    congela justamente quando ha atividade -- exatamente a hora em que voce quer
    olhar para ele.

    `recursive_triggers=ON` nao esta aqui por causa de gatilho recursivo: nao
    existe nenhum no projeto. Esta aqui porque, no SQLite, a resolucao de
    conflito REPLACE so dispara gatilho de DELETE quando este pragma esta ligado,
    e o padrao e DESLIGADO (medido: 0). Com ele desligado,
    `INSERT OR REPLACE INTO audit_log (id, ...) VALUES (1, ...)` reescrevia uma
    linha de auditoria sem erro nenhum -- REPLACE apaga a linha em conflito, e
    esse apagamento passava por baixo do gatilho `audit_log_sem_delete`. Medido
    nos dois estados: com o pragma OFF o comando passa; com ON o MESMO comando
    volta `IntegrityError: audit_log e append-only`.

    Por isso ele e ligado na subida real, e nao apenas no teste: sem ele a
    auditoria do banco que esta rodando agora e reescrevivel.

    O autorizador (`instalar_autorizador_sqlite`) entra aqui pelo mesmo motivo
    que os pragmas: ele e por CONEXAO, nao por banco, e conexao nova do pool
    tambem tem de nascer protegida. Ele e a barreira que substitui a lista de
    palavras proibidas na decisao -- o SQLite o consulta com o comando ja
    parseado, entao comentario, espaco em branco e `PRAGMA main.x` nao mudam
    nada. Ver o bloco de comentario em `db.models` acima de
    `motivo_para_negar_no_sqlite`, inclusive para o que ele NAO cobre.

    Ordem importa: o autorizador entra DEPOIS dos pragmas desta funcao. Nao e
    obrigatorio (a politica nao nega nenhum destes quatro), mas evita que ligar
    um pragma novo aqui amanha dependa de a politica concordar.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA recursive_triggers=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
        instalar_autorizador_sqlite(dbapi_connection, _record)


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            future=True,
            pool_pre_ping=True,
        )
        if settings.database_url.startswith("sqlite"):
            _tune_sqlite(_engine)
        log.info("db.engine_created", dialect=settings.database_url.split(":", 1)[0])
    return _engine


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(settings), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


@asynccontextmanager
async def session_scope(settings: Settings | None = None) -> AsyncIterator[AsyncSession]:
    """Sessao transacional: commit no sucesso, rollback em qualquer excecao."""
    factory = get_session_factory(settings)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def timescale_instalado(engine: AsyncEngine) -> bool:
    """A extensao TimescaleDB existe neste banco?

    Perguntar antes evita transformar a ausencia da extensao em uma excecao por
    tabela -- e, no Postgres, excecao dentro de transacao tem consequencia (ver
    `criar_hypertables`).
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        )
        return result.first() is not None


async def criar_hypertables(engine: AsyncEngine) -> list[str]:
    """Converte as series temporais em hypertables. Uma transacao por tabela.

    A transacao separada e o ponto todo desta funcao. Antes, o
    `create_hypertable` rodava DENTRO da mesma transacao do `create_all`, e no
    PostgreSQL um comando que falha aborta a transacao inteira: sem a extensao
    TimescaleDB, o `except` engolia o erro, o COMMIT seguinte virava ROLLBACK e
    o schema recem-criado ia embora junto. O sistema subia "com sucesso" e sem
    tabela nenhuma. No SQLite isso nunca aparecia, porque la o erro nao
    contamina a transacao -- e producao e o Postgres.

    Timescale continua sendo otimizacao, nao requisito: a ausencia dela devolve
    lista vazia e o sistema segue com tabelas comuns.
    """
    try:
        if not await timescale_instalado(engine):
            log.warning(
                "db.timescale_ausente",
                detail="extensao nao instalada; series temporais ficam em tabelas comuns",
            )
            return []
    except Exception as exc:  # pragma: no cover - banco sem pg_extension
        log.warning("db.timescale_indisponivel", error=str(exc))
        return []

    criadas: list[str] = []
    for table, time_column in HYPERTABLES:
        # Perguntado ao schema ANTES de perguntar ao banco, porque este era o
        # defeito: o TimescaleDB exige que todo indice unico contenha a coluna
        # de particao, `candles` e `portfolio_snapshots` tinham PK sem ela, o
        # `create_hypertable` era recusado nas duas, e o `except` abaixo
        # engolia o erro num WARNING que ninguem le. O sistema subia em tabela
        # comum para sempre, achando que tinha hypertable. As PKs foram
        # corrigidas; se alguem adicionar um indice unico que quebre a regra de
        # novo, sai ERROR nomeando o indice, e nao um "skipped" generico.
        impedimentos = hypertable_possivel(table, time_column)
        if impedimentos:
            log.error(
                "db.hypertable_impossivel",
                table=table,
                time_column=time_column,
                detail=(
                    "o TimescaleDB exige a coluna de particao em todo indice unico; "
                    f"impedem: {impedimentos}"
                ),
            )
            continue
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        f"SELECT create_hypertable('{table}', '{time_column}', "
                        "if_not_exists => TRUE, migrate_data => TRUE)"
                    )
                )
            criadas.append(table)
        except Exception as exc:  # pragma: no cover - Postgres sem TimescaleDB
            log.error("db.hypertable_recusada", table=table, error=str(exc))

    faltando = [table for table, _ in HYPERTABLES if table not in criadas]
    if faltando:
        # Conferido DEPOIS tambem: "mandei criar" nao e "foi criada", e a
        # diferenca entre as duas foi exatamente o que passou despercebido aqui.
        log.error(
            "db.hypertables_ausentes",
            tables=faltando,
            detail="series temporais seguem em tabela comum, sem a otimizacao do TimescaleDB",
        )
    return criadas


async def init_db(settings: Settings | None = None) -> None:
    """Cria o schema e, no Postgres, converte as series temporais em hypertables."""
    settings = settings or get_settings()
    engine = get_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Re-aplicado a cada subida: banco criado antes destes gatilhos
        # existirem tambem precisa passar a recusar UPDATE/DELETE na auditoria.
        # Sem try/except de proposito, e ao contrario das hypertables: auditoria
        # imutavel nao e otimizacao. Se o gatilho nao puder ser criado, a
        # excecao sobe e o sistema NAO sobe.
        await conn.run_sync(aplicar_protecao_append_only)
        # E entao PERGUNTA AO BANCO se os gatilhos estao la, em vez de supor.
        # Aqui havia uma afirmacao falsa: "tudo dentro DESTA transacao". Nao
        # estava -- no pysqlite/aiosqlite o DDL emitido antes do primeiro DML da
        # conexao e autocommitado e sobrevive ao rollback (medido nos dois
        # lados, com o `UPDATE` do mesmo bloco voltando atras). Por isso nenhum
        # caminho de subida emite `DROP TRIGGER`: a protecao so cresce, nunca
        # sai do ar nem por um instante. E como rollback nao e garantia, quem
        # garante e esta conferencia, que levanta `ProtecaoAusenteError` e
        # impede o sistema de subir com trilha mutavel.
        await conn.run_sync(verificar_protecao_append_only)
        # So diagnostico, e nao escreve nada: a escala de dinheiro ja e imposta
        # na leitura por `normalizar_dinheiro_lido`, entao nao ha historico a
        # reescrever -- e reescrever historico exigiria justamente derrubar o
        # gatilho que acabou de ser conferido.
        await conn.run_sync(relatar_dinheiro_fora_de_escala)

    # Fora da transacao acima, e nao dentro dela, de proposito: no PostgreSQL um
    # comando que falha aborta a transacao inteira e leva o schema junto no
    # COMMIT seguinte. Vale para a hypertable e vale para a sonda de privilegio.
    if settings.database_url.startswith("postgresql"):
        async with engine.connect() as conn:
            await conn.run_sync(avisar_se_a_aplicacao_pode_derrubar_gatilho)
        await criar_hypertables(engine)

    log.info("db.initialized")


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
