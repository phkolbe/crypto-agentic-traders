"""Rotas de controle dos agentes e saude do sistema."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ...agents.orchestrator import Orchestrator
from ...config import get_settings
from ...strategies import available_strategies
from ..deps import orchestrator_dep
from ..schemas import HealthOut, StrategyOut

router = APIRouter()


@router.get("/health", response_model=HealthOut)
async def health(orchestrator: Orchestrator = Depends(orchestrator_dep)) -> HealthOut:
    """Estado do sistema: modo de operacao, agentes e circuit breaker."""
    return HealthOut.model_validate(await orchestrator.health())


@router.post("/agents/{name}/pause")
async def pause_agent(
    name: str, orchestrator: Orchestrator = Depends(orchestrator_dep)
) -> dict[str, str]:
    if not await orchestrator.pause_agent(name):
        raise HTTPException(status_code=404, detail=f"agente desconhecido: {name}")
    return {"agent": name, "state": "paused"}


@router.post("/agents/{name}/resume")
async def resume_agent(
    name: str, orchestrator: Orchestrator = Depends(orchestrator_dep)
) -> dict[str, str]:
    if not await orchestrator.resume_agent(name):
        raise HTTPException(status_code=404, detail=f"agente desconhecido: {name}")
    return {"agent": name, "state": "running"}


@router.post("/agents/pause-all")
async def pause_all(
    orchestrator: Orchestrator = Depends(orchestrator_dep),
) -> dict[str, str]:
    """Parada de emergencia da tomada de decisao.

    O Market Data Agent continua rodando: sem preco atualizado o dashboard
    congela justamente no momento em que voce mais precisa olhar para ele.
    """
    await orchestrator.pause_all(actor="user", reason="parada manual pela interface")
    return {"state": "paused", "note": "coleta de dados de mercado continua ativa"}


@router.post("/agents/resume-all")
async def resume_all(
    orchestrator: Orchestrator = Depends(orchestrator_dep),
) -> dict[str, str]:
    if orchestrator.risk_manager.circuit_breaker_active:
        raise HTTPException(
            status_code=409,
            detail="circuit breaker acionado; rearme-o em /api/risk/circuit-breaker/reset",
        )
    await orchestrator.resume_all(actor="user")
    return {"state": "running"}


@router.get("/strategies", response_model=list[StrategyOut])
async def list_strategies(request: Request) -> list[StrategyOut]:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    active = set(settings.strategies)
    return [
        StrategyOut(name=name, description=description, active=name in active)
        for name, description in available_strategies().items()
    ]
