"""Historico de negociacoes: consulta, lancamento manual e exportacao."""

from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.repositories import (
    AuditLogRepository,
    OrderRepository,
    SignalRepository,
    TradeRepository,
)
from ...domain.enums import TradeOrigin
from ..deps import db_session
from ..schemas import ManualTradeIn, OrderOut, SignalOut, TradeOut, TradePage

router = APIRouter()


@router.get("/trades", response_model=TradePage)
async def list_trades(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    origin: str | None = Query(default=None, pattern="^(agent|manual)$"),
    symbol: str | None = None,
    side: str | None = Query(default=None, pattern="^(buy|sell)$"),
    start: datetime | None = None,
    end: datetime | None = None,
    session: AsyncSession = Depends(db_session),
) -> TradePage:
    """Historico consolidado: operacoes dos agentes E lancamentos manuais."""
    repository = TradeRepository(session)
    filters = {"origin": origin, "symbol": symbol, "side": side, "start": start, "end": end}
    rows = await repository.list(limit=limit, offset=offset, **filters)
    total = await repository.count(**filters)
    return TradePage(
        items=[TradeOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/trades/manual", response_model=TradeOut, status_code=status.HTTP_201_CREATED)
async def create_manual_trade(
    payload: ManualTradeIn, session: AsyncSession = Depends(db_session)
) -> TradeOut:
    """Registra uma operacao feita fora do sistema (ex.: pelo app da exchange).

    Entra na mesma tabela `trades`, o que mantem o dashboard, os filtros e a
    exportacao fiscal com uma unica fonte -- e faz o lancamento contar no
    calculo de preco medio junto com as operacoes dos agentes.
    """
    trade = await TradeRepository(session).record(
        executed_at=payload.executed_at,
        exchange=payload.exchange,
        symbol=payload.symbol,
        side=payload.side,
        quantity=payload.quantity,
        price=payload.price,
        fee=payload.fee,
        fee_currency=payload.fee_currency,
        origin=TradeOrigin.MANUAL,
        mode="manual",
        notes=payload.notes,
    )
    await AuditLogRepository(session).append(
        action="manual_trade_recorded",
        actor="user",
        target=trade.id,
        after={
            "symbol": payload.symbol,
            "side": payload.side,
            "quantity": str(payload.quantity),
            "price": str(payload.price),
        },
    )
    return TradeOut.model_validate(trade)


@router.delete("/trades/{trade_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_manual_trade(
    trade_id: str, session: AsyncSession = Depends(db_session)
) -> Response:
    """Remove um lancamento manual digitado errado.

    Operacoes dos agentes nunca sao removiveis: elas sao o registro do que o
    sistema de fato fez, e apagar isso destruiria a auditoria.
    """
    repository = TradeRepository(session)
    trade = await repository.get(trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="negociacao nao encontrada")
    if trade.origin != str(TradeOrigin.MANUAL):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="apenas lancamentos manuais podem ser removidos; "
            "operacoes dos agentes fazem parte da auditoria",
        )

    await repository.delete(trade_id)
    await AuditLogRepository(session).append(
        action="manual_trade_deleted", actor="user", target=trade_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/trades/export")
async def export_trades(
    start: datetime | None = None,
    end: datetime | None = None,
    session: AsyncSession = Depends(db_session),
) -> Response:
    """Exporta o historico em CSV.

    Pensado para apuracao fiscal: inclui taxa, moeda da taxa, origem e o modo de
    operacao, para que simulacao nunca seja confundida com dinheiro real.
    """
    rows = await TradeRepository(session).list(limit=100_000, start=start, end=end)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        [
            "data_hora_utc", "exchange", "par", "lado", "quantidade", "preco",
            "valor_total", "taxa", "moeda_taxa", "origem", "estrategia",
            "modo", "pnl_realizado", "observacoes",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.executed_at.isoformat(), row.exchange, row.symbol, row.side,
                row.quantity, row.price, row.notional, row.fee, row.fee_currency or "",
                row.origin, row.strategy or "", row.mode,
                row.realized_pnl if row.realized_pnl is not None else "",
                (row.notes or "").replace("\n", " "),
            ]
        )

    return Response(
        content=buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="negociacoes.csv"'},
    )


@router.get("/signals", response_model=list[SignalOut])
async def list_signals(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(db_session),
) -> list[SignalOut]:
    rows = await SignalRepository(session).list(limit=limit, offset=offset)
    return [SignalOut.model_validate(row) for row in rows]


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(db_session),
) -> list[OrderOut]:
    rows = await OrderRepository(session).list(limit=limit, offset=offset)
    return [OrderOut.model_validate(row) for row in rows]
