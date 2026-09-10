"""Dependencias compartilhadas das rotas."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents.orchestrator import Orchestrator
from ..config import Settings, get_settings
from ..db.session import get_session_factory

#: Hosts que sao a propria maquina. Ver `origem_confiavel`.
HOSTS_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def settings_dep(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


# ---------------------------------------------------------------------------
# Travas de escrita de configuracao.
#
# `PUT /api/trading/config` e `PUT /api/risk/config` fazem ler-alterar-gravar
# com `await` no meio. Medido no codigo anterior, deterministico em 6 de 6
# execucoes: dois PUT concorrentes ({"timeframe": "4h"} e
# {"candle_history_limit": 321}) receberam 200 os DOIS, o log confirmou os dois,
# e o estado final tinha apenas o segundo -- `timeframe` voltou ao padrao. Uma
# alteracao aceita com 200 que nao existe e a pior forma de perder um pedido,
# porque banco, memoria e resposta contam historias coerentes e uma delas e
# falsa. Dois cliques em Salvar, ou duas abas abertas, bastam.
#
# A trava e por configuracao (negocio e risco nao esperam uma pela outra) e por
# event loop: `asyncio.Lock` se amarra ao loop em que e usada pela primeira vez
# e levanta RuntimeError se depois aparecer em outro. Um singleton de modulo
# quebraria a suite inteira, onde cada teste tem o seu loop.
_config_locks: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
] = weakref.WeakKeyDictionary()


def config_write_lock(nome: str) -> asyncio.Lock:
    """Trava que serializa a escrita de UMA configuracao."""
    loop = asyncio.get_running_loop()
    por_nome = _config_locks.get(loop)
    if por_nome is None:
        por_nome = {}
        _config_locks[loop] = por_nome
    trava = por_nome.get(nome)
    if trava is None:
        trava = asyncio.Lock()
        por_nome[nome] = trava
    return trava


# ---------------------------------------------------------------------------
def origem_confiavel(origem: str, settings: Settings) -> bool:
    """Diz se um `Origin` de navegador pode acionar efeito nesta API.

    A API nao tem autenticacao -- por decisao: ela escuta so em `127.0.0.1` e
    quem chega ate ela ja esta na maquina. Isso deixa de valer dentro do
    navegador: uma aba QUALQUER aberta na mesma maquina pode disparar um POST
    para `127.0.0.1:8000`. CORS nao impede o pedido de ser ENVIADO, ele impede a
    pagina de LER a resposta -- e o efeito colateral ja aconteceu. Medido no
    codigo anterior: `POST /api/risk/capital/authorize`,
    `/api/risk/circuit-breaker/reset`, `/api/agents/pause-all` e
    `/api/agents/resume-all` responderam 200 com
    `Origin: https://site-qualquer.example`, sem credencial nenhuma.

    Conferir o `Origin` e a defesa que existe sem autenticacao: o navegador
    manda esse cabecalho em toda requisicao que muda estado e a pagina nao pode
    falsifica-lo. Sem `Origin` (curl, script, a propria CLI) o pedido passa:
    quem nao e navegador nao e o vetor que isto fecha.

    Loopback em qualquer porta e aceito de proposito. O dashboard e servido pelo
    Vite em `localhost:5173`, mas o dono pode abri-lo por `127.0.0.1:5173` --
    hoje isso funciona (o proxy do Vite faz tudo virar same-origin) e recusar
    ali seria quebrar o sistema em producao para fechar um ataque que exigiria
    um servidor malicioso ja rodando na maquina.
    """
    if "*" in settings.cors_origins or origem in settings.cors_origins:
        return True
    try:
        partes = urlsplit(origem)
    except ValueError:
        return False
    if partes.scheme not in ("http", "https"):
        return False
    return (partes.hostname or "").lower() in HOSTS_LOOPBACK


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
