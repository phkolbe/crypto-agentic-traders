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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..logging_setup import get_logger
from .deps import origem_confiavel, settings_dep
from .routes import (
    agents,
    backtest,
    notifications,
    portfolio,
    risk,
    trades,
    trading_config,
    ws,
)

if TYPE_CHECKING:
    from ..agents.orchestrator import Orchestrator

log = get_logger(__name__)

DESCRIPTION = """
API do sistema de agentes autonomos de negociacao.

**Modo de operacao** aparece em `/api/health`. Em `dry_run` nenhuma ordem sai da
maquina; alteracoes de limite de risco e rearme do circuit breaker sao sempre
registradas em `audit_log`.
"""

#: Metodos que causam efeito. GET/HEAD ficam fora porque nao mudam estado, e
#: OPTIONS fica fora porque e o preflight -- recusa-lo esconderia do navegador a
#: resposta que diz que a origem nao serve.
METODOS_COM_EFEITO = frozenset({"POST", "PUT", "PATCH", "DELETE"})


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

    @app.middleware("http")
    async def bloquear_origem_estranha(request: Request, call_next):
        """Recusa pedido com efeito vindo de pagina de outra origem.

        Isto nao e autenticacao e nao pretende ser: e a trava que falta quando
        nao existe autenticacao nenhuma. Ver `deps.origem_confiavel` para a
        medicao e para o motivo de loopback passar.
        """
        origem = request.headers.get("origin")
        if (
            origem
            and request.method in METODOS_COM_EFEITO
            and not origem_confiavel(origem, settings_dep(request))
        ):
            log.warning(
                "api.origem_recusada",
                origem=origem,
                metodo=request.method,
                rota=request.url.path,
                detail="pedido com efeito vindo de pagina de outra origem",
            )
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "origem nao autorizada a acionar esta API; "
                        "use o dashboard local"
                    )
                },
            )
        return await call_next(request)

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
    app.include_router(trading_config.router, prefix="/api", tags=["configuracao"])
    app.include_router(ws.router, tags=["tempo real"])

    return app
