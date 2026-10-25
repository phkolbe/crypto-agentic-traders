"""Rotas de portfolio: cards, grafico de patrimonio e posicoes."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...analise import resultado_por_par, resumo_de_risco
from ...db import models as orm
from ...db.repositories import PortfolioSnapshotRepository, TradeRepository
from ..deps import db_session, settings_dep
from ..schemas import (
    AnaliseOut,
    BenchmarkOut,
    EquityPoint,
    FechamentoBenchmark,
    MotivoOut,
    PortfolioOut,
    PortfolioSummary,
    PositionOut,
    ResultadoParOut,
    RiscoOut,
)

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


#: O seletor da tela vai ate 1 ano; o dia a mais cobre ano bissexto e o fuso de
#: quem escolhe "de 25/10/2025 a 25/10/2026" nas datas.
JANELA_MAXIMA = timedelta(days=366)

#: Pontos por serie: mais do que isso a tela nao distingue (o grafico tem poucas
#: centenas de pixels), e menos deixaria a curva de 1 ano em degraus.
ALVO_DE_PONTOS = 500


def _utc(valor: datetime) -> datetime:
    return valor if valor.tzinfo else valor.replace(tzinfo=UTC)


def periodo(days: int, start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    """A janela pedida: `days` ate agora, ou `start`/`end` (no maximo 1 ano).

    Data sem fuso e lida como UTC. Janela invertida ou maior que um ano e erro de
    quem pediu (422), e nao um recorte silencioso: um grafico que mostra menos do
    que o titulo diz e o tipo de numero que parece certo e nao e.
    """
    agora = datetime.now(UTC)
    if start is None and end is None:
        return agora - timedelta(days=days), agora
    fim = _utc(end) if end is not None else agora
    inicio = _utc(start) if start is not None else fim - timedelta(days=days)
    if inicio >= fim:
        raise HTTPException(status_code=422, detail="o inicio do periodo tem de ser antes do fim")
    if fim - inicio > JANELA_MAXIMA:
        raise HTTPException(status_code=422, detail="o periodo vai ate 1 ano")
    return inicio, fim


def segundos_por_faixa(inicio: datetime, fim: datetime) -> int:
    """Faixa que cobre a janela com ate `ALVO_DE_PONTOS` pontos (no minimo 1 min).

    Divide por `ALVO - 1` porque as faixas sao alinhadas no relogio, e a janela
    quase nunca comeca no inicio de uma: as duas pontas parciais somam uma faixa.
    """
    return max(60, math.ceil((fim - inicio).total_seconds() / (ALVO_DE_PONTOS - 1)))


def _ponto(row) -> EquityPoint:
    return EquityPoint(
        # O SQLite devolve a data sem fuso; sem ele o navegador le hora LOCAL.
        timestamp=_utc(row.timestamp),
        total_value=Decimal(str(row.total_value)),
        cash_value=Decimal(str(row.cash_value)),
        positions_value=Decimal(str(row.positions_value)),
        realized_pnl=Decimal(str(row.realized_pnl or 0)),
        unrealized_pnl=Decimal(str(row.unrealized_pnl or 0)),
        allocations=row.allocations or {},
    )


@router.get("/portfolio/history", response_model=list[EquityPoint])
async def portfolio_history(
    days: int = Query(default=30, ge=1, le=365),
    start: datetime | None = Query(default=None, description="Inicio (ISO-8601); ate 1 ano"),
    end: datetime | None = Query(default=None, description="Fim (ISO-8601); padrao agora"),
    session: AsyncSession = Depends(db_session),
) -> list[EquityPoint]:
    """Serie do patrimonio na janela INTEIRA, com ate 500 pontos.

    Cada ponto e o ultimo retrato da sua faixa de tempo, com caixa, posicoes,
    resultado e alocacao -- o que os graficos da tela Portfolio desenham.
    """
    inicio, fim = periodo(days, start, end)
    rows = await PortfolioSnapshotRepository(session).amostrado(
        inicio, fim, segundos_por_faixa(inicio, fim)
    )
    return [_ponto(row) for row in rows]


@router.get("/portfolio/analise", response_model=AnaliseOut)
async def portfolio_analise(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    session: AsyncSession = Depends(db_session),
) -> AnaliseOut:
    """Fechamentos diarios, o BTC no mesmo periodo, resultado por par e decisoes de risco."""
    settings = settings_dep(request)
    quote = settings.trading.quote_currency
    modo = str(settings.trading_mode)
    inicio, fim = periodo(days, start, end)
    um_dia = timedelta(days=1)

    diarios = await PortfolioSnapshotRepository(session).amostrado(
        inicio - um_dia, fim, int(um_dia.total_seconds())
    )

    simbolo_btc = f"BTC/{quote}"
    candles = (
        await session.execute(
            select(orm.Candle)
            .where(
                orm.Candle.symbol == simbolo_btc,
                orm.Candle.timeframe == "1d",
                orm.Candle.open_time >= inicio - um_dia,
                orm.Candle.open_time <= fim,
            )
            .order_by(orm.Candle.open_time.asc())
        )
    ).scalars().all()
    benchmark = (
        BenchmarkOut(
            simbolo=simbolo_btc,
            fechamentos=[
                FechamentoBenchmark(timestamp=_utc(c.open_time), close=Decimal(str(c.close)))
                for c in candles
            ],
        )
        if candles
        else None
    )

    trades = await TradeRepository(session).for_cost_basis()
    pares = resultado_por_par(trades, quote, modo, inicio, fim)

    eventos = (
        await session.execute(
            select(orm.RiskEvent).where(
                orm.RiskEvent.created_at >= inicio, orm.RiskEvent.created_at <= fim
            )
        )
    ).scalars().all()
    ativos = {s.partition("/")[0] for s in settings.trading.symbols}
    ativos |= {t.symbol.partition("/")[0] for t in trades}
    for evento in eventos:
        simbolo = (evento.snapshot or {}).get("symbol")
        if isinstance(simbolo, str):
            ativos.add(simbolo.partition("/")[0])
    risco = resumo_de_risco(eventos, ativos)

    return AnaliseOut(
        inicio=inicio,
        fim=fim,
        moeda=quote,
        modo=modo,
        fechamentos_diarios=[_ponto(row) for row in diarios],
        benchmark=benchmark,
        pares=[ResultadoParOut(**vars(par)) for par in pares],
        risco=RiscoOut(
            aprovadas=risco.aprovadas,
            rejeitadas=risco.rejeitadas,
            motivos=[MotivoOut(motivo=m, total=t, exemplo=e) for m, t, e in risco.motivos],
        ),
    )
