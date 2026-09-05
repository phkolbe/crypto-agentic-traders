"""Rotas de portfolio: cards, grafico de patrimonio e posicoes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.repositories import PortfolioSnapshotRepository, TradeRepository
from ..deps import db_session
from ..schemas import EquityPoint, PortfolioOut, PortfolioSummary, PositionOut

router = APIRouter()


def _to_out(row) -> PortfolioOut:
    positions = [
        PositionOut(
            asset=p["asset"],
            quantity=Decimal(str(p["quantity"])),
            average_price=Decimal(str(p["average_price"])) if p.get("average_price") else None,
            current_price=Decimal(str(p["current_price"])) if p.get("current_price") else None,
            market_value=_market_value(p),
            unrealized_pnl=_unrealized(p),
        )
        for p in (row.positions or [])
    ]
    return PortfolioOut(
        timestamp=row.timestamp,
        total_value=Decimal(str(row.total_value)),
        cash_value=Decimal(str(row.cash_value)),
        positions_value=Decimal(str(row.positions_value)),
        realized_pnl=Decimal(str(row.realized_pnl or 0)),
        unrealized_pnl=Decimal(str(row.unrealized_pnl or 0)),
        allocations=row.allocations or {},
        positions=positions,
        mode=row.mode,
    )


def _market_value(position: dict) -> Decimal:
    if not position.get("current_price"):
        return Decimal(0)
    return Decimal(str(position["quantity"])) * Decimal(str(position["current_price"]))


def _unrealized(position: dict) -> Decimal:
    if not position.get("current_price") or not position.get("average_price"):
        return Decimal(0)
    delta = Decimal(str(position["current_price"])) - Decimal(str(position["average_price"]))
    return delta * Decimal(str(position["quantity"]))


@router.get("/portfolio", response_model=PortfolioSummary)
async def current_portfolio(session: AsyncSession = Depends(db_session)) -> PortfolioSummary:
    """Retrato atual do portfolio, com as variacoes dos cards do dashboard."""
    repository = PortfolioSnapshotRepository(session)
    latest = await repository.latest()
    if latest is None:
        return PortfolioSummary(current=None)

    now = datetime.now(UTC)
    current_value = Decimal(str(latest.total_value))

    async def change_since(delta: timedelta) -> float | None:
        reference = await repository.value_at_or_before(now - delta)
        if reference is None or reference <= 0:
            return None
        return float((current_value - reference) / reference)

    history = await repository.history(limit=1)
    first = history[0] if history else None
    since_start = None
    if first is not None:
        base = Decimal(str(first.total_value))
        if base > 0:
            since_start = float((current_value - base) / base)

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    trades_today = await TradeRepository(session).count(start=day_start)

    return PortfolioSummary(
        current=_to_out(latest),
        change_24h_pct=await change_since(timedelta(days=1)),
        change_7d_pct=await change_since(timedelta(days=7)),
        change_since_start_pct=since_start,
        trades_today=trades_today,
    )


@router.get("/portfolio/history", response_model=list[EquityPoint])
async def portfolio_history(
    days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(db_session),
) -> list[EquityPoint]:
    """Serie temporal do patrimonio, para o grafico de evolucao."""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = await PortfolioSnapshotRepository(session).history(since=since, limit=5000)
    return [
        EquityPoint(timestamp=row.timestamp, total_value=Decimal(str(row.total_value)))
        for row in rows
    ]
