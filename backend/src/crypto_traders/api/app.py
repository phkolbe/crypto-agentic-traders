"""Aplicacao FastAPI.

Expoe leitura (dashboard, historico) e controle (pausar agentes, ajustar
limites, rearmar circuit breaker), mais um WebSocket para atualizacao em tempo
real.

Escuta apenas em `127.0.0.1` (ver `API_HOST`). Para acessar de fora, use uma VPN
pessoal -- nunca abra a porta no roteador.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..config import PROJECT_ROOT, get_settings
from ..logging_setup import get_logger
from .deps import HOSTS_LOOPBACK, origem_confiavel, settings_dep
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
    # Por ultimo, para ficar POR FORA de tudo: recusa antes do CORS e das rotas.
    if settings.acesso_login_tailscale:
        app.add_middleware(PortaoTailscale, login=settings.acesso_login_tailscale)

    app.include_router(portfolio.router, prefix="/api", tags=["portfolio"])
    app.include_router(trades.router, prefix="/api", tags=["trades"])
    app.include_router(risk.router, prefix="/api", tags=["risco"])
    app.include_router(agents.router, prefix="/api", tags=["agentes"])
    app.include_router(backtest.router, prefix="/api", tags=["backtest"])
    app.include_router(notifications.router, prefix="/api", tags=["notificacoes"])
    app.include_router(trading_config.router, prefix="/api", tags=["configuracao"])
    app.include_router(ws.router, tags=["tempo real"])
    _servir_telas(app, PROJECT_ROOT / "frontend" / "dist")

    return app


#: Onde o produto mora na URL: telas em `/crypto-traders/`, API em
#: `/crypto-traders/api/...`, tempo real em `/crypto-traders/ws`. O frontend le o
#: mesmo valor do `base` do Vite (`import.meta.env.BASE_URL`); os dois mudam juntos.
RAIZ = "/crypto-traders"


def _caminho_da_rota(scope) -> str:  # type: ignore[no-untyped-def]
    """O caminho DENTRO do app, sem a raiz onde ele esta montado.

    O Starlette entrega ao app montado o caminho inteiro em `path` e a raiz em
    `root_path`; comparar `path` direto com `/api/health` nunca casaria debaixo
    de `/crypto-traders`.
    """
    caminho = scope.get("path") or ""
    raiz = scope.get("root_path") or ""
    return caminho[len(raiz) :] if raiz and caminho.startswith(raiz) else caminho


def montar_na_raiz(interno: FastAPI, raiz: str = RAIZ) -> FastAPI:
    """O app que o uvicorn serve: o `interno` debaixo de `raiz`, e `/` levando para la.

    O `interno` continua com as rotas em `/api/...` -- e o que a suite testa --, e
    so a montagem decide o prefixo. Duas coisas que a montagem sozinha nao faz:

    - o ciclo de vida (lifespan) de app montado NAO roda no Starlette, e e nele
      que o WebSocket liga o transmissor de eventos; o externo o executa;
    - qualquer outro caminho fora da raiz e 404, menos `/`, que redireciona para
      a raiz (quem digitar so o endereco do servidor cai nas telas).
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with interno.router.lifespan_context(interno):
            yield

    externo = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @externo.get("/", include_in_schema=False)
    async def para_a_raiz() -> RedirectResponse:
        return RedirectResponse(f"{raiz}/", status_code=307)

    externo.mount(raiz, interno)
    return externo


class PortaoTailscale:
    """So deixa passar quem o `tailscale serve` identificou como `login`.

    A API nao tem login proprio (escutava so em 127.0.0.1). Em producao ela fica
    atras de um no Tailscale so dela, e o `tailscale serve` poe em cada pedido o
    cabecalho `Tailscale-User-Login` com quem esta do outro lado -- e o
    SOBRESCREVE se o cliente tentar manda-lo, entao de fora nao se forja. A porta
    da API nao e publicada: o unico caminho ate aqui e o serve. Isto vale para
    HTTP e para o WebSocket (o `@app.middleware("http")` nao ve WebSocket, por
    isso o middleware e ASGI puro).

    Excecao unica: `GET /api/health` vindo do proprio conteiner (a sonda de saude
    do Docker), que nao passa pelo serve.
    """

    def __init__(self, app, login: str) -> None:  # type: ignore[no-untyped-def]
        self.app = app
        self.login = login.strip().lower()

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        cabecalhos = dict(scope.get("headers") or [])
        quem = cabecalhos.get(b"tailscale-user-login", b"").decode("latin-1").strip().lower()
        cliente = (scope.get("client") or ("", 0))[0]
        sonda = (
            scope["type"] == "http"
            and _caminho_da_rota(scope) == "/api/health"
            and cliente in HOSTS_LOOPBACK
        )
        if quem == self.login or sonda:
            await self.app(scope, receive, send)
            return
        log.warning(
            "api.acesso_recusado",
            login=quem or None,
            rota=scope.get("path"),
            detail="sem o login autorizado do Tailscale",
        )
        if scope["type"] == "websocket":
            # Fechar antes de aceitar vira 403 no handshake.
            await send({"type": "websocket.close", "code": 1008})
            return
        await JSONResponse(
            status_code=403, content={"detail": "acesso so do administrador"}
        )(scope, receive, send)


def _servir_telas(app: FastAPI, pasta: Path) -> None:
    """Serve o dashboard ja montado (`npm run build`), quando ele existe.

    No Docker nao ha Vite: a imagem traz `frontend/dist` e a propria API entrega
    as telas na mesma origem, entao `/api` e `/ws` continuam relativos e a trava
    de origem segue valendo. Rota desconhecida fora de `/api` devolve o
    `index.html` (o React Router resolve); dentro de `/api` continua 404.
    Em desenvolvimento com `npm run dev`, nada muda.
    """
    indice = pasta / "index.html"
    if not indice.is_file():
        return
    raiz = pasta.resolve()
    if (pasta / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=pasta / "assets"), name="telas")

    @app.get("/{caminho:path}", include_in_schema=False)
    async def telas(caminho: str) -> FileResponse:
        if caminho == "api" or caminho.startswith(("api/", "ws")):
            raise HTTPException(status_code=404, detail="Not Found")
        alvo = (pasta / caminho).resolve()
        if caminho and alvo.is_file() and alvo.is_relative_to(raiz):
            return FileResponse(alvo)
        # O index muda a cada build (nomes dos assets com hash): nunca em cache.
        return FileResponse(indice, headers={"Cache-Control": "no-cache"})
