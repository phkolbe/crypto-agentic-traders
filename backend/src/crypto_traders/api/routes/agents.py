"""Rotas de controle dos agentes e saude do sistema."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...agents.orchestrator import Orchestrator
from ...config import get_settings
from ...strategies import available_strategies
from ..deps import orchestrator_dep
from ..schemas import HealthOut, StrategyOut

router = APIRouter()


class AgenteEstadoOut(BaseModel):
    """Estado de um agente RELIDO do orquestrador depois da acao.

    As rotas devolviam literais ("paused"/"running") que podiam nao existir:
    pausar a execucao nao a pausa (vira trava de abertura, D4), retomar com o
    circuit breaker armado nao solta a abertura, e um agente desistido seguia
    desistido. A resposta agora e o que o orquestrador diz depois de agir.
    """

    agent: str
    state: str
    running: bool
    paused: bool
    given_up: bool
    openings_blocked: str | None = None
    note: str


class SistemaEstadoOut(BaseModel):
    """Estado de todos os agentes depois de uma acao geral, relido."""

    agents: dict[str, AgenteEstadoOut]
    circuit_breaker_active: bool
    note: str


@router.get("/health", response_model=HealthOut)
async def health(
    sonda: bool = False, orchestrator: Orchestrator = Depends(orchestrator_dep)
) -> HealthOut:
    """Estado do sistema: modo de operacao, agentes e circuit breaker.

    Com `?sonda=1` (o HEALTHCHECK do conteiner) responde 503 quando o sistema
    nao esta protegendo a carteira -- agente critico sem batida, morto ou
    desistido, ou o laco do watchdog parado (`Orchestrator.vitalidade`). Sem o
    parametro continua 200: a tela le esta rota justamente quando algo esta
    errado, e precisa do corpo para mostrar o que.
    """
    if sonda:
        problemas = orchestrator.vitalidade()
        if problemas:
            raise HTTPException(status_code=503, detail=problemas)
    return HealthOut.model_validate(await orchestrator.health())


def _agente(nome: str, dados: dict[str, Any], breaker: bool) -> AgenteEstadoOut:
    return AgenteEstadoOut(
        agent=nome,
        state=dados["state"],
        running=dados["running"],
        paused=dados["paused"],
        given_up=dados["given_up"],
        openings_blocked=dados.get("openings_blocked"),
        note=_nota_do_agente(nome, dados, breaker),
    )


def _nota_do_agente(nome: str, dados: dict[str, Any], breaker: bool) -> str:
    if dados["given_up"]:
        return "o watchdog desistiu deste agente; veja o log"
    if not dados["running"]:
        return "tarefa parada"
    if nome != "execution":
        return "pausado" if dados["paused"] else "rodando"
    trava = dados.get("openings_blocked")
    if trava is None:
        return "execucao de pe: abre e fecha posicao"
    if breaker:
        return (
            "a execucao segue de pe para FECHAR posicao (stop-loss), mas a abertura "
            "continua bloqueada: circuit breaker armado, rearme-o para voltar a abrir"
        )
    return (
        "a execucao nao e pausada (D4): ela segue de pe para FECHAR posicao; "
        f"a abertura esta bloqueada ({trava})"
    )


async def _estado_do_sistema(orchestrator: Orchestrator) -> tuple[dict[str, Any], bool]:
    saude = await orchestrator.health()
    return saude["agents"], bool(saude["circuit_breaker_active"])


async def _responder_agente(orchestrator: Orchestrator, nome: str) -> AgenteEstadoOut:
    agentes, breaker = await _estado_do_sistema(orchestrator)
    return _agente(nome, agentes[nome], breaker)


async def _responder_sistema(orchestrator: Orchestrator, nota: str) -> SistemaEstadoOut:
    agentes, breaker = await _estado_do_sistema(orchestrator)
    return SistemaEstadoOut(
        agents={nome: _agente(nome, dados, breaker) for nome, dados in agentes.items()},
        circuit_breaker_active=breaker,
        note=nota,
    )


@router.post("/agents/{name}/pause", response_model=AgenteEstadoOut)
async def pause_agent(
    name: str, orchestrator: Orchestrator = Depends(orchestrator_dep)
) -> AgenteEstadoOut:
    if not await orchestrator.pause_agent(name):
        raise HTTPException(status_code=404, detail=f"agente desconhecido: {name}")
    return await _responder_agente(orchestrator, name)


@router.post("/agents/{name}/resume", response_model=AgenteEstadoOut)
async def resume_agent(
    name: str, orchestrator: Orchestrator = Depends(orchestrator_dep)
) -> AgenteEstadoOut:
    if not await orchestrator.resume_agent(name):
        raise HTTPException(status_code=404, detail=f"agente desconhecido: {name}")
    return await _responder_agente(orchestrator, name)


@router.post("/agents/pause-all", response_model=SistemaEstadoOut)
async def pause_all(
    orchestrator: Orchestrator = Depends(orchestrator_dep),
) -> SistemaEstadoOut:
    """Parada de emergencia da tomada de decisao.

    O Market Data Agent continua rodando: sem preco atualizado o dashboard
    congela justamente no momento em que voce mais precisa olhar para ele. E a
    execucao continua de pe para fechar posicao (D4); o que para nela e a
    ABERTURA.
    """
    await orchestrator.pause_all(actor="user", reason="parada manual pela interface")
    return await _responder_sistema(
        orchestrator,
        "decisao pausada (strategy, risk_manager); abertura bloqueada na execucao, que "
        "segue de pe para fechar posicao; coleta de dados de mercado continua ativa",
    )


@router.post("/agents/resume-all", response_model=SistemaEstadoOut)
async def resume_all(
    orchestrator: Orchestrator = Depends(orchestrator_dep),
) -> SistemaEstadoOut:
    if orchestrator.risk_manager.circuit_breaker_active:
        raise HTTPException(
            status_code=409,
            detail="circuit breaker acionado; rearme-o em /api/risk/circuit-breaker/reset",
        )
    await orchestrator.resume_all(actor="user")
    return await _responder_sistema(orchestrator, "retomada geral pedida; veja cada agente")


@router.get("/strategies", response_model=list[StrategyOut])
async def list_strategies(request: Request) -> list[StrategyOut]:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    active = set(settings.trading.strategies)
    return [
        StrategyOut(name=name, description=description, active=name in active)
        for name, description in available_strategies().items()
    ]
