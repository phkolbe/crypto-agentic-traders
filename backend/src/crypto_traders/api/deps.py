"""Dependencias compartilhadas das rotas."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents.orchestrator import Orchestrator
from ..config import Settings, get_settings
from ..db.session import get_session_factory


def settings_dep(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


async def db_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = get_session_factory(settings_dep(request))
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def orchestrator_dep(request: Request) -> Orchestrator:
    """Rotas de controle exigem o orquestrador vivo.

    A API pode subir sozinha (para inspecionar o historico com os agentes
    parados); nesse caso as rotas de controle respondem 503 em vez de quebrar.
    """
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Os agentes nao estao rodando neste processo.",
        )
    return orchestrator
