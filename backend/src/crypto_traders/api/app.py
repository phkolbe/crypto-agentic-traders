"""Aplicacao FastAPI.

Expoe leitura (dashboard, historico) e controle (pausar agentes, ajustar
limites, rearmar circuit breaker), mais um WebSocket para atualizacao em tempo
real.

Escuta apenas em `127.0.0.1` (ver `API_HOST`). Para acessar de fora, use uma VPN
pessoal -- nunca abra a porta no roteador.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import get_settings
from ..logging_setup import get_logger
from .routes import agents, backtest, notifications, portfolio, risk, trades, ws

if TYPE_CHECKING:
    from ..agents.orchestrator import Orchestrator

log = get_logger(__name__)

DESCRIPTION = """
API do sistema de agentes autonomos de negociacao.

**Modo de operacao** aparece em `/api/health`. Em `dry_run` nenhuma ordem sai da
maquina; alteracoes de limite de risco e rearme do circuit breaker sao sempre
registradas em `audit_log`.
"""


def create_app(orchestrator: Orchestrator | None = None) -> FastAPI:
    settings = get_settings() if orchestrator is None else orchestrator.settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if orchestrator is not None:
            app.state.orchestrator = orchestrator
            ws.attach_broadcaster(app, orchestrator)
        yield
        await ws.detach_broadcaster(app)

    app = FastAPI(
        title="crypto-agentic-traders",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.orchestrator = orchestrator
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    app.include_router(portfolio.router, prefix="/api", tags=["portfolio"])
    app.include_router(trades.router, prefix="/api", tags=["trades"])
    app.include_router(risk.router, prefix="/api", tags=["risco"])
    app.include_router(agents.router, prefix="/api", tags=["agentes"])
    app.include_router(backtest.router, prefix="/api", tags=["backtest"])
    app.include_router(notifications.router, prefix="/api", tags=["notificacoes"])
    app.include_router(ws.router, tags=["tempo real"])

    return app
