"""Backtest sob demanda pela interface."""

from __future__ import annotations

import asyncio
import inspect

from fastapi import APIRouter, Depends, HTTPException

from ...backtest.engine import BacktestEngine, BacktestResult
from ...cli import TIMEFRAME_MINUTES
from ...config import Settings
from ...exchanges import build_market_data_source
from ...logging_setup import get_logger
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
log = get_logger(__name__)

#: Teto do historico de um backtest, por MEMORIA. O backtest roda no processo dos
#: agentes, limitado a 512 MB em producao, e o que cresce com o pedido e a lista
#: de candles, a curva de capital e a resposta: medido (tracemalloc, motor real
#: e resposta serializada) ~2 KB por candle -- 4,1 MB com 2000 candles, 14,9 MB
#: com 8000. 20.000 candles sao ~40 MB, menos de um decimo do processo, e cobrem
#: um ano em 30m (17.520) e em 1h (8.760). Acima disto a rota devolve 422 dizendo
#: o que fazer, em vez de cortar: ate a rodada 3 o corte era em 1000 e calado, e
#: os 90 dias padrao em 1h (2160) eram medidos como 41.
MAX_CANDLES = 20_000

#: Um backtest por vez no processo inteiro. O backtest divide processo, memoria
#: (512 MB em producao) e CPU com os agentes que guardam o stop; dois cliques em
#: "Rodar" eram dois historicos, dois clientes da exchange e dois motores ao
#: mesmo tempo. O segundo pedido recebe 429 na hora, em vez de esperar na fila:
#: uma fila so esconderia o custo.
_UM_POR_VEZ = asyncio.Semaphore(1)


def _executar_motor(engine: BacktestEngine, candles: list) -> BacktestResult:
    """Roda o motor numa thread, com laco de eventos proprio.

    O `run` e `async` so por causa do `PaperBroker`, que nao faz E/S nenhuma: na
    pratica e um laco sincrono de ate `MAX_CANDLES` iteracoes montando `DataFrame` e
    avaliando a estrategia. No laco de eventos do sistema, ele parava o Risk
    Manager (stop), o WebSocket e as outras rotas pelo tempo inteiro do backtest.
    Aceita tambem um `run` sincrono, para nao depender de como o motor evolui.
    """
    resultado = engine.run(candles)
    if inspect.isawaitable(resultado):
        return asyncio.run(resultado)
    return resultado


@router.post("/backtest", response_model=BacktestOut)
async def run_backtest(
    payload: BacktestIn, settings: Settings = Depends(settings_dep)
) -> BacktestOut:
    """Roda um backtest com dados historicos reais da exchange.

    Usa a mesma estrategia, o mesmo RiskEngine e o mesmo PaperBroker da
    producao -- os limites de risco vigentes valem aqui tambem, entao o
    resultado reflete o que o sistema realmente faria.

    Um por vez (429 se ja houver um rodando), com o motor fora do laco de
    eventos: ver `_UM_POR_VEZ` e `_executar_motor`.
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

    needed = int(payload.days * 24 * 60 / minutes)
    if needed > MAX_CANDLES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{payload.days} dias em {payload.timeframe} sao {needed} candles, acima do "
                f"teto de {MAX_CANDLES} (memoria do processo). Use menos dias ou um "
                f"timeframe maior: no maximo {MAX_CANDLES * minutes // (24 * 60)} dias "
                f"em {payload.timeframe}."
            ),
        )

    # Conferir e tomar a vaga sem `await` no meio: no mesmo laco de eventos,
    # nenhum outro pedido entra entre as duas linhas.
    if _UM_POR_VEZ.locked():
        raise HTTPException(
            status_code=429,
            detail="ja ha um backtest rodando; espere ele terminar e tente de novo",
        )
    async with _UM_POR_VEZ:
        result = await _rodar(payload, settings, strategy, needed)

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


async def _rodar(payload: BacktestIn, settings: Settings, strategy, needed: int) -> BacktestResult:
    # Cliente proprio e fechado ao fim, mesmo com erro. O do orquestrador nao e
    # reaproveitado de proposito: ele aponta para a testnet quando o sistema roda
    # em testnet, e o backtest quer o historico real; com um backtest por vez, ha
    # no maximo um cliente extra vivo.
    source = build_market_data_source(settings.exchange)
    try:
        candles = await source.fetch_candles(payload.symbol, payload.timeframe, needed)
    except Exception as exc:
        # O texto do ccxt leva URL e corpo da resposta da exchange: fica no log,
        # e o cliente recebe so o que fazer.
        log.warning("backtest.dados_indisponiveis", symbol=payload.symbol, erro=str(exc))
        raise HTTPException(
            status_code=502,
            detail="falha ao obter o historico da exchange; tente de novo em instantes",
        ) from exc
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
        return await asyncio.to_thread(_executar_motor, engine, candles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
