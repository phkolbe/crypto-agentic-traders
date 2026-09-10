"""Rotas da configuracao de negocio: o que negociar e com que cadencia.

Esta e a contrapartida da regra "o `.env` so tem ambiente": se as variaveis de
negocio saem do arquivo, precisa existir um lugar de verdade para edita-las. E
este e o lugar.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ...agents.orchestrator import Orchestrator
from ...strategies import available_strategies
from ..deps import config_write_lock, orchestrator_dep
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

    # A trava e o que faz a validacao valer sobre o merge que REALMENTE fica
    # gravado. `update_trading` le a configuracao atual, monta o merge, valida e
    # grava com `await` no meio: sem serializar, dois PUT concorrentes leem a
    # MESMA base e o segundo a gravar apaga a alteracao do primeiro -- que
    # recebeu 200 e um corpo confirmando o valor novo. Medido no codigo
    # anterior, 6 vezes em 6.
    #
    # O caso que mais dói: `quote_currency` e `symbols` tem de andar juntos (foi
    # o que a migracao para USDC fez). Se os dois chegarem concorrentes, um se
    # perde e o sistema termina medindo o caixa numa moeda em que nenhum par
    # negociado liquida, com o dashboard verde.
    async with config_write_lock("trading"):
        try:
            atualizado = await orchestrator.update_trading(changes, actor="user")
        except ValueError as exc:
            # Validacao de negocio (estrategia inexistente, combinacao incoerente)
            # e erro do pedido, nao falha do servidor.
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return _to_out(atualizado)
