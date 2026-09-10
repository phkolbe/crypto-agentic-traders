"""Backtest sob demanda pela interface."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ...backtest.engine import BacktestEngine
from ...cli import TIMEFRAME_MINUTES
from ...config import Settings
from ...exchanges import build_market_data_source
from ...strategies import get_strategy
from ..deps import settings_dep
from ..schemas import (
    BacktestEquityPoint,
    BacktestIn,
    BacktestOut,
    BacktestSummaryOut,
    BacktestTradeOut,
)

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
        quote_currency=settings.trading.quote_currency,
        initial_balance=payload.initial_balance,
        fee_pct=settings.trading.paper_fee_pct,
        slippage_pct=settings.trading.paper_slippage_pct,
        lookback=settings.trading.candle_history_limit,
    )
    try:
        result = await engine.run(candles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Dinheiro sai como string (D7). Os Decimais vem do proprio `result`, e nao
    # do `summary()`, porque `summary()` entrega `initial_balance` e
    # `final_value` ja convertidos para float -- reserializar aquele float
    # gravaria o residuo em vez do valor exato.
    summary = result.summary() | {
        "initial_balance": result.initial_balance,
        "final_value": result.final_value,
    }
    return BacktestOut(
        summary=BacktestSummaryOut.model_validate(summary),
        equity_curve=[
            BacktestEquityPoint(timestamp=moment.isoformat(), value=value)
            for moment, value in result.equity_curve
        ],
        trades=[
            BacktestTradeOut(
                timestamp=trade.timestamp.isoformat(),
                side=trade.side,
                quantity=trade.quantity,
                price=trade.price,
                notional=trade.notional,
                fee=trade.fee,
                reason=trade.reason,
                realized_pnl=trade.realized_pnl,
            )
            for trade in result.trades
        ],
    )
