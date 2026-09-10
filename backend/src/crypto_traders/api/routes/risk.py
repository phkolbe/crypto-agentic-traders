"""Rotas de risco: limites, decisoes do guardiao, circuit breaker e auditoria."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...agents.orchestrator import Orchestrator
from ...db.repositories import AuditLogRepository, RiskConfigRepository, RiskEventRepository
from ..deps import config_write_lock, db_session, orchestrator_dep
from ..schemas import (
    AuditEntryOut,
    CapitalAuthorizationIn,
    CapitalStatusOut,
    CircuitBreakerResetIn,
    RiskConfigIn,
    RiskConfigOut,
    RiskEventOut,
)

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

    limpar = {
        "max_order_notional": payload.clear_max_order_notional,
        "max_open_positions": payload.clear_max_open_positions,
        "authorized_capital": payload.clear_authorized_capital,
    }
    # Mandar um valor E pedir para apagar o mesmo campo e um pedido contraditorio.
    # Resolver isso sozinho era o pior caminho possivel: o `clear_*` sobrescrevia
    # o valor, e o resultado era sempre o lado MAIS permissivo. Medido, um PUT com
    # `authorized_capital=50` e `clear_authorized_capital=true` devolvia 200 e
    # removia o portao de autorizacao inteiro -- liberando, em vez de 50, todo o
    # patrimonio e todo deposito futuro. Diante de duvida o sistema recusa.
    contraditorios = sorted(
        campo
        for campo, remover in limpar.items()
        if remover and getattr(payload, campo) is not None
    )
    if contraditorios:
        conflitos = ", ".join(
            f"{campo} veio com valor e com clear_{campo}=true" for campo in contraditorios
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"pedido contraditorio: {conflitos}. "
                "Envie o valor OU o pedido de remocao, nunca os dois"
            ),
        )

    changes = payload.model_dump(
        exclude_none=True,
        exclude={"confirm", *(f"clear_{campo}" for campo in limpar)},
    )
    # Remover um teto e um gesto proprio: num PUT parcial, `null` significa "nao
    # enviei". Sem os campos `clear_*` nao existiria como apagar um limite.
    for campo, remover in limpar.items():
        if remover:
            changes[campo] = None

    if not changes:
        raise HTTPException(status_code=400, detail="nenhum campo enviado")

    # A trava cobre alterar E reler o estado que compoe a resposta. O
    # `update_limits` tem a trava dele, mas depender dela e depender de detalhe
    # de outra camada: aqui o que se garante e que dois PUT simultaneos nao
    # produzem uma resposta 200 descrevendo um estado que nunca existiu.
    async with config_write_lock("risk"):
        try:
            updated = await orchestrator.risk_manager.update_limits(changes, actor="user")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        config = await RiskConfigRepository(session).get_or_create(
            updated.model_dump(mode="json")
        )
    return RiskConfigOut(
        **updated.model_dump(),
        circuit_breaker_active=config.circuit_breaker_active,
        circuit_breaker_reason=config.circuit_breaker_reason,
    )


@router.get("/risk/capital", response_model=CapitalStatusOut)
async def get_capital_status(
    orchestrator: Orchestrator = Depends(orchestrator_dep),
) -> CapitalStatusOut:
    """Quanto capital esta autorizado a operar, e quanto espera aval."""
    limits = orchestrator.risk_manager.configured_limits
    snapshot = orchestrator.risk_manager.last_snapshot
    total = snapshot.total_value if snapshot else Decimal(0)
    autorizado = limits.authorized_capital
    return CapitalStatusOut(
        total_value=total,
        authorized_capital=autorizado,
        unauthorized_value=(
            max(Decimal(0), total - autorizado) if autorizado is not None else Decimal(0)
        ),
        gate_active=autorizado is not None,
        quote_currency=orchestrator.settings.trading.quote_currency,
    )


@router.post("/risk/capital/authorize", response_model=CapitalStatusOut)
async def authorize_capital(
    payload: CapitalAuthorizationIn | None = None,
    orchestrator: Orchestrator = Depends(orchestrator_dep),
) -> CapitalStatusOut:
    """Autoriza o patrimonio inteiro apurado agora a ser posto para trabalhar.

    Grava um numero em vez de desligar o portao: desligar autorizaria tambem
    todo deposito futuro, que e exatamente o que o portao existe para impedir.
    Autorizar e um ato sobre o saldo de hoje, e fica no `audit_log`.

    Exige `confirm=true` no corpo, pela mesma razao escrita em `PUT
    /api/risk/config`: este campo E um limite de risco, e afrouxa-lo nao pode
    ser efeito colateral de um clique. Ver `CapitalAuthorizationIn` para a
    medicao que motivou a trava.
    """
    if payload is None or not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "autorizar capital afrouxa um limite de risco e afeta dinheiro "
                "real; envie confirm=true"
            ),
        )

    async with config_write_lock("risk"):
        snapshot = orchestrator.risk_manager.last_snapshot
        apurado = snapshot.total_value if snapshot else None
        # Autorizar "tudo" sem dizer quanto e tudo autoriza o que aparecer no
        # instante do clique -- inclusive um deposito que chegou entre a tela
        # carregar e o botao ser apertado, que e precisamente o que o portao
        # existe para segurar. Com o valor esperado, divergencia recusa.
        if (
            payload.expected_total_value is not None
            and apurado is not None
            and payload.expected_total_value != apurado
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"o patrimonio apurado agora e {apurado}, nao "
                    f"{payload.expected_total_value}; recarregue e confira antes "
                    "de autorizar"
                ),
            )

        try:
            limits = await orchestrator.risk_manager.authorize_all_capital(actor="user")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    snapshot = orchestrator.risk_manager.last_snapshot
    total = snapshot.total_value if snapshot else Decimal(0)
    return CapitalStatusOut(
        total_value=total,
        authorized_capital=limits.authorized_capital,
        unauthorized_value=Decimal(0),
        gate_active=limits.authorized_capital is not None,
        quote_currency=orchestrator.settings.trading.quote_currency,
    )


@router.post("/risk/circuit-breaker/reset", response_model=RiskConfigOut)
async def reset_circuit_breaker(
    payload: CircuitBreakerResetIn | None = None,
    orchestrator: Orchestrator = Depends(orchestrator_dep),
) -> RiskConfigOut:
    """Rearma o circuit breaker e retoma os agentes.

    Deliberadamente manual: se a trava disparou, algo saiu do esperado, e uma
    pessoa precisa ter olhado antes de o sistema voltar a operar. Por isso
    tambem exige `confirm=true`: rearmar devolve ao sistema a permissao de abrir
    posicao, e essa e a direcao em que errar custa dinheiro.
    """
    if payload is None or not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "rearmar o circuit breaker devolve ao sistema a permissao de "
                "abrir posicao; envie confirm=true"
            ),
        )

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
