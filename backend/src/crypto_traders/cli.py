"""Interface de linha de comando.

    crypto-traders check                      diagnostico do ambiente
    crypto-traders backtest --symbol BTC/USDT valida uma estrategia
    crypto-traders run                        sobe agentes + API
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal

from .config import get_settings
from .db.session import init_db, session_scope
from .domain.enums import TradingMode
from .logging_setup import configure_logging, get_logger

log = get_logger(__name__)

#: Duracao de cada timeframe em minutos, para converter dias em numero de candles.
TIMEFRAME_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720,
    "1d": 1440,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crypto-traders",
        description="Agentes autonomos de negociacao de criptomoedas",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="Diagnostico do ambiente e da configuracao")

    backtest = subparsers.add_parser("backtest", help="Roda um backtest com dados historicos")
    backtest.add_argument("--symbol", default=None, help="Par, ex.: BTC/USDT")
    backtest.add_argument("--strategy", default=None, help="Nome da estrategia")
    backtest.add_argument("--timeframe", default=None, help="Ex.: 15m, 1h, 4h")
    backtest.add_argument("--days", type=int, default=90, help="Janela historica em dias")
    backtest.add_argument("--balance", type=Decimal, default=None, help="Saldo inicial simulado")
    backtest.add_argument(
        "--all-strategies", action="store_true", help="Compara todas as estrategias"
    )

    run = subparsers.add_parser("run", help="Sobe os agentes e a API")
    run.add_argument("--no-api", action="store_true", help="Sobe apenas os agentes")

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    if args.command == "check":
        return asyncio.run(_check(settings))
    if args.command == "backtest":
        return asyncio.run(_backtest(settings, args))
    if args.command == "run":
        from .runtime import run_system

        return asyncio.run(run_system(with_api=not args.no_api))
    return 1


# ---------------------------------------------------------------------------
async def _check(settings) -> int:
    """Diagnostico: o que esta configurado, e o que ainda falta."""
    print("=" * 68)
    print(" crypto-agentic-traders — diagnostico do ambiente")
    print("=" * 68)

    mode_label = {
        TradingMode.DRY_RUN: "SIMULACAO (nenhuma ordem sai da maquina)",
        TradingMode.TESTNET: "TESTNET (ordens reais, dinheiro ficticio)",
        TradingMode.LIVE: "*** DINHEIRO REAL ***",
    }[settings.trading_mode]
    print(f"\n  Modo de operacao : {settings.trading_mode}  ->  {mode_label}")
    print(f"  Exchange         : {settings.exchange}")
    print(f"  Pares            : {', '.join(settings.symbols)}")
    print(f"  Timeframe        : {settings.timeframe}")
    print(f"  Estrategias      : {', '.join(settings.strategies)}")

    print("\n  Credenciais:")
    for name in ("binance", "coinbase"):
        credentials = settings.credentials_for(name)
        status = "configurada" if credentials.configured else "ausente"
        print(f"    {name:<10} {status}")
    if not settings.sends_real_orders:
        print("    (dados de mercado sao publicos; credenciais so sao exigidas fora do dry_run)")

    print("\n  Limites de risco:")
    risk = settings.risk
    print(f"    ordem maxima      : {risk.max_order_notional} {settings.quote_currency} "
          f"ou {risk.max_order_pct_portfolio:.1%} do portfolio (o menor)")
    print(f"    ordem minima      : {risk.min_order_notional} {settings.quote_currency}")
    print(f"    exposicao maxima  : {risk.max_asset_exposure_pct:.0%} por ativo")
    print(f"    stop / alvo       : -{risk.stop_loss_pct:.1%} / +{risk.take_profit_pct:.1%}")
    print(f"    circuit breaker   : -{risk.daily_loss_limit_pct:.1%} ao dia, "
          f"-{risk.weekly_loss_limit_pct:.1%} na semana")
    print(f"    cooldown          : {risk.cooldown_seconds}s por par")
    print(f"    whitelist         : {', '.join(risk.symbol_whitelist)}")

    print("\n  Infraestrutura:")
    dialect = settings.database_url.split(":", 1)[0]
    print(f"    banco     : {dialect}")
    print(f"    event bus : {settings.event_bus}")
    print(f"    API       : http://{settings.api_host}:{settings.api_port}")

    try:
        await init_db(settings)
        async with session_scope(settings) as session:
            from sqlalchemy import text

            await session.execute(text("SELECT 1"))
        print("    conexao   : OK")
    except Exception as exc:
        print(f"    conexao   : FALHOU — {exc}")
        return 1

    print("\n  Conectividade com a exchange (endpoints publicos):")
    try:
        from .exchanges import build_market_data_source

        source = build_market_data_source(settings.exchange)
        try:
            ticker = await source.fetch_ticker(settings.symbols[0])
            print(f"    {ticker.symbol}: {ticker.price}  OK")
        finally:
            await source.close()
    except Exception as exc:
        print(f"    FALHOU — {exc}")
        return 1

    if settings.is_live:
        print("\n  " + "!" * 62)
        print("  ATENCAO: LIVE_TRADING ativo. Ordens usarao DINHEIRO REAL.")
        print("  " + "!" * 62)

    print("\n  Tudo pronto.\n")
    return 0


# ---------------------------------------------------------------------------
async def _backtest(settings, args) -> int:
    from .backtest.engine import BacktestEngine
    from .exchanges import build_market_data_source
    from .strategies import available_strategies, get_strategy

    symbol = args.symbol or settings.symbols[0]
    timeframe = args.timeframe or settings.timeframe
    balance = args.balance or settings.paper_initial_balance

    minutes = TIMEFRAME_MINUTES.get(timeframe)
    if minutes is None:
        print(f"timeframe desconhecido: {timeframe}")
        return 1
    needed = min(int(args.days * 24 * 60 / minutes), 1000)

    print(f"\nBaixando {needed} candles de {symbol} ({timeframe})...")
    source = build_market_data_source(settings.exchange)
    try:
        candles = await source.fetch_candles(symbol, timeframe, needed)
    finally:
        await source.close()

    closed = [c for c in candles if c.closed]
    if not closed:
        print("nenhum candle fechado recebido da exchange")
        return 1

    span = closed[-1].open_time - closed[0].open_time
    print(
        f"{len(closed)} candles fechados | "
        f"{closed[0].open_time:%d/%m/%Y} a {closed[-1].open_time:%d/%m/%Y} "
        f"({span.days} dias)\n"
    )

    names = list(available_strategies()) if args.all_strategies else [
        args.strategy or settings.strategies[0]
    ]

    results = []
    for name in names:
        engine = BacktestEngine(
            get_strategy(name),
            settings.risk,
            quote_currency=settings.quote_currency,
            initial_balance=balance,
            fee_pct=settings.paper_fee_pct,
            slippage_pct=settings.paper_slippage_pct,
            lookback=settings.candle_history_limit,
        )
        try:
            results.append(await engine.run(closed))
        except ValueError as exc:
            print(f"  {name}: {exc}")

    if not results:
        return 1

    _print_backtest_table(results, balance, settings.quote_currency)
    return 0


def _print_backtest_table(results, balance: Decimal, quote: str) -> None:
    reference = results[0]
    print(f"{'estrategia':<22}{'retorno':>10}{'b&h':>10}{'drawdown':>11}"
          f"{'trades':>8}{'acerto':>9}{'sinais':>8}{'rejeit.':>9}")
    print("-" * 87)
    for result in sorted(results, key=lambda r: r.total_return_pct, reverse=True):
        print(
            f"{result.strategy:<22}"
            f"{result.total_return_pct:>9.2%}"
            f"{result.buy_and_hold_pct:>10.2%}"
            f"{result.max_drawdown_pct:>11.2%}"
            f"{len(result.trades):>8}"
            f"{result.win_rate:>9.1%}"
            f"{result.signals_generated:>8}"
            f"{result.signals_rejected:>9}"
        )

    print(f"\nSaldo inicial simulado: {balance} {quote}")
    print(
        "A coluna 'b&h' e o retorno de simplesmente comprar e segurar no mesmo periodo.\n"
        "Uma estrategia que fica abaixo dela destruiu valor, mesmo com retorno positivo."
    )

    if reference.rejection_reasons:
        print("\nPrincipais motivos de rejeicao pelo Risk Manager:")
        for reason, count in reference.rejection_reasons.most_common(5):
            print(f"  {count:>5}x  {reason}")

    print(
        "\nBacktest nao prova nada sobre o futuro. Antes de LIVE_TRADING: "
        "semanas em dry_run, depois testnet.\n"
    )


if __name__ == "__main__":
    sys.exit(main())
