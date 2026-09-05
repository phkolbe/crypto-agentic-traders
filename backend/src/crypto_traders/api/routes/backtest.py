"""Backtest sob demanda pela interface."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ...backtest.engine import BacktestEngine
from ...cli import TIMEFRAME_MINUTES
from ...config import Settings
from ...exchanges import build_market_data_source
from ...strategies import get_strategy
from ..deps import settings_dep
from ..schemas import BacktestIn, BacktestOut

router = APIRouter()

#: A exchange limita o historico por requisicao; alem disso seria paginacao.
MAX_CANDLES = 1000


@router.post("/backtest", response_model=BacktestOut)
async def run_backtest(
    payload: BacktestIn, settings: Settings = Depends(settings_dep)
) -> BacktestOut:
    """Roda um backtest com dados historicos reais da exchange.

    Usa a mesma estrategia, o mesmo RiskEngine e o mesmo PaperBroker da
    producao -- os limites de risco vigentes valem aqui tambem, entao o
    resultado reflete o que o sistema realmente faria.
    """
    minutes = TIMEFRAME_MINUTES.get(payload.timeframe)
    if minutes is None:
        raise HTTPException(
            status_code=422,
            detail=f"timeframe invalido; use um de: {', '.join(TIMEFRAME_MINUTES)}",
        )

    try:
        strategy = get_strategy(payload.strategy)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    needed = min(int(payload.days * 24 * 60 / minutes), MAX_CANDLES)
    source = build_market_data_source(settings.exchange)
    try:
        candles = await source.fetch_candles(payload.symbol, payload.timeframe, needed)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"falha ao obter dados: {exc}") from exc
    finally:
        await source.close()

    engine = BacktestEngine(
        strategy,
        settings.risk,
        quote_currency=settings.quote_currency,
        initial_balance=payload.initial_balance,
        fee_pct=settings.paper_fee_pct,
        slippage_pct=settings.paper_slippage_pct,
        lookback=settings.candle_history_limit,
    )
    try:
        result = await engine.run(candles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return BacktestOut(
        summary=result.summary(),
        equity_curve=[
            {"timestamp": moment.isoformat(), "value": float(value)}
            for moment, value in result.equity_curve
        ],
        trades=[
            {
                "timestamp": trade.timestamp.isoformat(),
                "side": trade.side,
                "quantity": float(trade.quantity),
                "price": float(trade.price),
                "notional": float(trade.notional),
                "fee": float(trade.fee),
                "reason": trade.reason,
                "realized_pnl": float(trade.realized_pnl)
                if trade.realized_pnl is not None
                else None,
            }
            for trade in result.trades
        ],
    )
