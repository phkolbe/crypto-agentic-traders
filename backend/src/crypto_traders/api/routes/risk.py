"""Rotas de risco: limites, decisoes do guardiao, circuit breaker e auditoria."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...agents.orchestrator import Orchestrator
from ...db.repositories import AuditLogRepository, RiskConfigRepository, RiskEventRepository
from ..deps import db_session, orchestrator_dep
from ..schemas import AuditEntryOut, RiskConfigIn, RiskConfigOut, RiskEventOut

router = APIRouter()


@router.get("/risk/config", response_model=RiskConfigOut)
async def get_risk_config(
    orchestrator: Orchestrator = Depends(orchestrator_dep),
    session: AsyncSession = Depends(db_session),
) -> RiskConfigOut:
    config = await RiskConfigRepository(session).get_or_create(
        orchestrator.risk_manager.limits.model_dump(mode="json")
    )
    return RiskConfigOut(
        **orchestrator.risk_manager.limits.model_dump(),
        circuit_breaker_active=config.circuit_breaker_active,
        circuit_breaker_reason=config.circuit_breaker_reason,
    )


@router.put("/risk/config", response_model=RiskConfigOut)
async def update_risk_config(
    payload: RiskConfigIn,
    orchestrator: Orchestrator = Depends(orchestrator_dep),
    session: AsyncSession = Depends(db_session),
) -> RiskConfigOut:
    """Altera os limites do Risk Manager.

    Exige `confirm=true` no corpo. Afrouxar um limite muda diretamente quanto
    dinheiro real o sistema pode comprometer, entao a confirmacao explicita
    existe para que isso nunca seja o efeito colateral de um clique.
    """
    if not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="alterar limites de risco afeta dinheiro real; envie confirm=true",
        )

    changes = payload.model_dump(exclude_none=True, exclude={"confirm"})
    if not changes:
        raise HTTPException(status_code=400, detail="nenhum campo enviado")

    try:
        updated = await orchestrator.risk_manager.update_limits(changes, actor="user")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    config = await RiskConfigRepository(session).get_or_create(updated.model_dump(mode="json"))
    return RiskConfigOut(
        **updated.model_dump(),
        circuit_breaker_active=config.circuit_breaker_active,
        circuit_breaker_reason=config.circuit_breaker_reason,
    )


@router.post("/risk/circuit-breaker/reset", response_model=RiskConfigOut)
async def reset_circuit_breaker(
    orchestrator: Orchestrator = Depends(orchestrator_dep),
) -> RiskConfigOut:
    """Rearma o circuit breaker e retoma os agentes.

    Deliberadamente manual: se a trava disparou, algo saiu do esperado, e uma
    pessoa precisa ter olhado antes de o sistema voltar a operar.
    """
    if not orchestrator.risk_manager.circuit_breaker_active:
        raise HTTPException(status_code=409, detail="o circuit breaker nao esta acionado")

    await orchestrator.risk_manager.reset_circuit_breaker(actor="user")
    await orchestrator.resume_all(actor="user")

    return RiskConfigOut(
        **orchestrator.risk_manager.limits.model_dump(),
        circuit_breaker_active=False,
        circuit_breaker_reason=None,
    )


@router.get("/risk/events", response_model=list[RiskEventOut])
async def list_risk_events(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(db_session),
) -> list[RiskEventOut]:
    """Toda decisao do guardiao, incluindo as rejeicoes.

    Saber por que o sistema NAO operou e tao importante quanto o contrario.
    """
    rows = await RiskEventRepository(session).list(limit=limit, offset=offset)
    return [RiskEventOut.model_validate(row) for row in rows]


@router.get("/audit", response_model=list[AuditEntryOut])
async def list_audit(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(db_session),
) -> list[AuditEntryOut]:
    rows = await AuditLogRepository(session).list(limit=limit, offset=offset)
    return [AuditEntryOut.model_validate(row) for row in rows]
