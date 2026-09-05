"""Engine e sessao assincrona do SQLAlchemy.

O mesmo codigo atende SQLite (padrao local) e PostgreSQL/TimescaleDB, escolhido
apenas pela `DATABASE_URL`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..config import Settings, get_settings
from ..logging_setup import get_logger
from .models import Base

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _tune_sqlite(engine: AsyncEngine) -> None:
    """WAL + chaves estrangeiras.

    Sem WAL, a escrita dos agentes bloqueia a leitura da API e o dashboard
    congela justamente quando ha atividade -- exatamente a hora em que voce quer
    olhar para ele.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


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


async def init_db(settings: Settings | None = None) -> None:
    """Cria o schema e, no Postgres, converte as series temporais em hypertables."""
    settings = settings or get_settings()
    engine = get_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        if settings.database_url.startswith("postgresql"):
            for table, time_column in (
                ("candles", "open_time"),
                ("portfolio_snapshots", "timestamp"),
            ):
                try:
                    await conn.execute(
                        text(
                            f"SELECT create_hypertable('{table}', '{time_column}', "
                            "if_not_exists => TRUE, migrate_data => TRUE)"
                        )
                    )
                except Exception as exc:  # pragma: no cover - Postgres sem TimescaleDB
                    # Timescale e uma otimizacao, nao um requisito: sem a extensao
                    # o sistema segue funcionando com tabelas comuns.
                    log.warning("db.hypertable_skipped", table=table, error=str(exc))

    log.info("db.initialized")


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
