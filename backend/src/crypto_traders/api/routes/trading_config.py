"""Rotas da configuracao de negocio: o que negociar e com que cadencia.

Esta e a contrapartida da regra "o `.env` so tem ambiente": se as variaveis de
negocio saem do arquivo, precisa existir um lugar de verdade para edita-las. E
este e o lugar.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ...agents.orchestrator import Orchestrator
from ...strategies import available_strategies
from ..deps import orchestrator_dep
from ..schemas import TradingConfigIn, TradingConfigOut

router = APIRouter()


def _to_out(trading) -> TradingConfigOut:
    return TradingConfigOut(
        **trading.model_dump(),
        discovery_enabled=trading.discovery_enabled,
        available_strategies=available_strategies(),
    )


@router.get("/trading/config", response_model=TradingConfigOut)
async def get_trading_config(
    orchestrator: Orchestrator = Depends(orchestrator_dep),
) -> TradingConfigOut:
    return _to_out(orchestrator.settings.trading)


@router.put("/trading/config", response_model=TradingConfigOut)
async def update_trading_config(
    payload: TradingConfigIn,
    orchestrator: Orchestrator = Depends(orchestrator_dep),
) -> TradingConfigOut:
    """Altera a configuracao de negocio, ja valendo no processo em execucao.

    Exige `confirm=true` pelo mesmo motivo da tela de risco: trocar pares, moeda
    de cotacao ou estrategias muda o que o sistema negocia com dinheiro real, e
    isso nunca pode ser efeito colateral de um clique.
    """
    if not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "alterar a configuracao de negociacao muda o que o sistema opera; "
                "envie confirm=true"
            ),
        )

    changes = payload.model_dump(exclude_none=True, exclude={"confirm"})
    if not changes:
        raise HTTPException(status_code=400, detail="nenhum campo enviado")

    try:
        atualizado = await orchestrator.update_trading(changes, actor="user")
    except ValueError as exc:
        # Validacao de negocio (estrategia inexistente, combinacao incoerente)
        # e erro do pedido, nao falha do servidor.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return _to_out(atualizado)
