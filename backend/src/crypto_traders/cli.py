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
from .risk.rules import SizingFeasibility

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
    print(" crypto-agentic-traders — diagnostico")
    print("=" * 68)

    # O banco vem primeiro porque e de la que sai toda a configuracao de
    # negocio. Imprimir os numeros antes de carrega-la mostraria os padroes de
    # codigo -- um diagnostico que descreve outra instalacao, nao esta.
    print("\n  Infraestrutura (ambiente, do `.env`):")
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

    await _load_business_config(settings)

    mode_label = {
        TradingMode.DRY_RUN: "SIMULACAO (nenhuma ordem sai da maquina)",
        TradingMode.TESTNET: "TESTNET (ordens reais, dinheiro ficticio)",
        TradingMode.LIVE: "*** DINHEIRO REAL ***",
    }[settings.trading_mode]
    print(f"\n  Modo de operacao : {settings.trading_mode}  ->  {mode_label}")
    print(f"  Exchange         : {settings.exchange}")
    if settings.trading.discovery_enabled:
        print("  Pares            : DESCOBERTA AUTOMATICA (lista de pares vazia)")
        descoberta = settings.trading
        print(f"                     ate {descoberta.discovery_max_symbols} pares com volume "
              f"24h >= {descoberta.discovery_min_quote_volume_24h:,.0f} "
              f"{descoberta.quote_currency}")
    else:
        print(f"  Pares            : {', '.join(settings.trading.symbols)}")
    print(f"  Timeframe        : {settings.trading.timeframe}")
    print(f"  Estrategias      : {', '.join(settings.trading.strategies)}")

    print("\n  Credenciais:")
    credentials = settings.credentials_for(settings.exchange)
    status = "configurada" if credentials.configured else "ausente"
    print(f"    {settings.exchange:<10} {status}")
    if not settings.sends_real_orders:
        print("    (dados de mercado sao publicos; credenciais so sao exigidas fora do dry_run)")

    print("\n  Limites de risco:")
    risk = settings.risk
    moeda = settings.trading.quote_currency
    if risk.max_order_notional is None:
        # Sem teto absoluto a ordem acompanha a carteira. Imprimir "None BRL"
        # deixaria o leitor sem saber se e ausencia de limite ou erro de leitura.
        print(f"    ordem maxima      : {risk.max_order_pct_portfolio:.1%} do capital "
              f"(sem teto absoluto — a ordem acompanha o saldo)")
    else:
        print(f"    ordem maxima      : {risk.max_order_notional} {moeda} "
              f"ou {risk.max_order_pct_portfolio:.1%} do portfolio (o menor)")
    print(f"    ordem minima      : {risk.min_order_notional} {settings.trading.quote_currency}")
    print(f"    exposicao maxima  : {risk.max_asset_exposure_pct:.0%} por ativo")
    print(f"    stop / alvo       : -{risk.stop_loss_pct:.1%} / +{risk.take_profit_pct:.1%} "
          f"— aplicado pelo SISTEMA, nao pela exchange")
    print(f"    circuit breaker   : -{risk.daily_loss_limit_pct:.1%} ao dia, "
          f"-{risk.weekly_loss_limit_pct:.1%} na semana")
    print(f"    cooldown          : {risk.cooldown_seconds}s por par")
    posicoes = risk.max_open_positions
    print(
        "    posicoes abertas  : "
        + (
            f"no maximo {posicoes}"
            if posicoes is not None
            else "sem limite — o caixa limita, cada ordem consome dinheiro"
        )
    )
    if settings.trading.discovery_enabled:
        # A whitelist configurada nao vale neste modo: quem a define e a
        # varredura de mercado logo abaixo. Mostrar a lista salva aqui enganaria.
        print("    whitelist         : definida pela descoberta (ver pares abaixo)")
        if risk.max_open_positions is not None:
            print(f"    (com a whitelist definida por criterio, o limite de "
                  f"{risk.max_open_positions} posicoes e a defesa principal)")
        else:
            # Sem whitelist aprovada a mao E sem teto de posicoes, as duas defesas
            # que sobram sao a exposicao por ativo e o stop. Dizer isso e melhor
            # que imprimir "None" onde antes havia um numero tranquilizador.
            print("    (sem whitelist aprovada a mao e sem teto de posicoes, as")
            print("     defesas que restam sao a exposicao por ativo e o stop)")
    else:
        print(f"    whitelist         : {', '.join(risk.symbol_whitelist)}")

    _check_capital_gate(settings)
    _check_protection_scope(settings)
    await _check_regime_filter(settings)

    print("\n  Conectividade com a exchange (endpoints publicos):")
    markets: dict = {}
    tickers: dict = {}
    active_symbols: list[str] = []
    try:
        from .exchanges import build_market_data_source

        source = build_market_data_source(settings.exchange)
        try:
            # Uma varredura so alimenta a descoberta E a checagem de filtros
            # logo abaixo: pedir os mercados duas vezes seria desperdicio de
            # chamada e ainda poderia devolver precos diferentes entre elas.
            markets, tickers = await source.fetch_markets_and_tickers()

            if settings.trading.discovery_enabled:
                from .discovery import DEFAULT_EXCLUDED_ASSETS, DiscoveryCriteria, select_markets

                criteria = DiscoveryCriteria(
                    quote_currency=settings.trading.quote_currency,
                    min_quote_volume_24h=settings.trading.discovery_min_quote_volume_24h,
                    max_symbols=settings.trading.discovery_max_symbols,
                    exclude_assets=DEFAULT_EXCLUDED_ASSETS
                    | {a.upper() for a in settings.trading.discovery_exclude_assets},
                )
                result = select_markets(markets, tickers, criteria)
                if not result.symbols:
                    print("    NENHUM par atingiu o piso de liquidez.")
                    print("    Em Configuracoes: reduza o piso de liquidez, ou informe os pares.")
                    return 1
                print(f"    {result.considered} pares avaliados, "
                      f"{result.rejected_low_volume} abaixo do piso de volume")
                for market in result.markets:
                    volume = float(market.quote_volume_24h) / 1e6
                    print(f"      {market.symbol:<14} volume 24h {volume:>8,.0f}M")
                active_symbols = result.symbols
            else:
                active_symbols = list(settings.trading.symbols)
                for symbol in active_symbols:
                    price = (tickers.get(symbol) or {}).get("last")
                    print(f"    {symbol}: {price if price else 'sem cotacao'}  OK")
        finally:
            await source.close()
    except Exception as exc:
        print(f"    FALHOU — {exc}")
        return 1

    credentials_ok, quote_balance = await _check_credentials(settings)
    if not credentials_ok:
        return 1

    sizing = _check_sizing(settings, quote_balance)
    if sizing is None:
        return 1

    if not _check_market_filters(settings, markets, tickers, active_symbols, sizing):
        return 1

    if settings.is_live:
        print("\n  " + "!" * 62)
        print("  ATENCAO: LIVE_TRADING ativo. Ordens usarao DINHEIRO REAL.")
        print("  " + "!" * 62)

    print("\n  Tudo pronto.\n")
    return 0


async def _load_business_config(settings) -> None:
    """Carrega a configuracao de negocio do banco para dentro de `settings`.

    Sem isto o diagnostico imprimiria os padroes de codigo -- e o `check` existe
    justamente para responder "o que ESTA instalacao vai fazer".
    """
    from .business_config import load_business_config
    from .db.repositories import TradingConfigRepository

    try:
        async with session_scope(settings) as session:
            existia = await TradingConfigRepository(session).get() is not None
        trading, risk = await load_business_config(settings)
        settings.with_business_config(trading, risk)
    except Exception as exc:
        print(f"\n  Configuracao de negocio : FALHOU ao ler do banco — {exc}")
        print("    Seguindo com os padroes conservadores do codigo.")
        return

    origem = "banco" if existia else "padroes de fabrica (linha criada agora)"
    print(f"    negocio   : {origem}")
    if not existia:
        print("    Primeira subida: os valores de negocio foram gravados com os")
        print("    padroes. Ajuste-os em Configuracoes, na interface web.")


def _check_capital_gate(settings) -> None:
    """Mostra o portao de capital: quanto o sistema pode por para trabalhar.

    Sem esta linha, um saldo parado por falta de autorizacao seria
    indistinguivel de "o sistema nao acha oportunidade" -- o mesmo modo de falha
    silenciosa que o filtro de regime e o dimensionamento ja tiveram aqui.
    """
    autorizado = settings.risk.authorized_capital
    moeda = settings.trading.quote_currency
    print("\n  Portao de capital:")
    if autorizado is None:
        print("    DESLIGADO — todo o patrimonio esta disponivel, e depositos")
        print("    futuros entram em operacao sem novo aval.")
        return
    print(f"    autorizado : {autorizado} {moeda}")
    print("    Saldo acima disto fica PARADO e gera notificacao pedindo")
    print("    autorizacao. Um deposito nao e uma ordem: dinheiro que entra")
    print("    por outro motivo nao deveria virar exposicao sozinho.")
    print("    Para liberar: botao em Risco, na interface.")


def _check_protection_scope(settings) -> None:
    """Diz ONDE o stop e aplicado, e o que essa escolha nao cobre.

    A linha "stop / alvo: -3,0% / +6,0%" acima nao diz quem segura a posicao.
    Lendo so aquilo, a conclusao natural e que a exchange esta segurando -- e
    durante todo o desenvolvimento a conclusao era pior ainda, porque o nivel
    nao era comparado com preco nenhum, em lugar nenhum.

    Nomear o alcance da protecao e a licao desse episodio: um numero exibido sem
    dizer quem o executa e um numero em que se confia sem motivo.
    """
    intervalo = settings.trading.portfolio_interval_seconds
    print("\n  Alcance da protecao (stop / alvo):")
    print(f"    onde       : no SISTEMA, a cada {intervalo}s — nao na exchange")
    print("    NAO cobre  : processo encerrado, maquina reiniciada, ou perda de")
    print("                 acesso a API (IP fora da whitelist). Nesses casos uma")
    print("                 posicao aberta fica sem saida automatica.")
    print("                 Tambem nao ve o pavio dentro do intervalo: uma queda")
    print("                 que rompe o stop e volta antes da proxima leitura")
    print("                 passa batida. O backtest, que le a minima do candle,")
    print("                 e nesse ponto MAIS severo que a producao.")
    print("    ver        : docs/SEGURANCA.md, secao 12")


async def _check_regime_filter(settings) -> None:
    """Mostra a leitura MVRV atual e se ela esta barrando entradas agora.

    Um filtro de regime pode estar corretamente configurado e, ainda assim,
    bloquear toda compra hoje. Isso e indistinguivel de "o sistema nao acha
    oportunidades" olhando so o dashboard, entao o diagnostico precisa dizer.
    """
    from .db.repositories import RiskConfigRepository
    from .onchain import OnChainProvider

    limiar = settings.risk.mvrv_max_percentile
    try:
        async with session_scope(settings) as session:
            gravado = dict((await RiskConfigRepository(session).get_or_create({})).values or {})
        if "mvrv_max_percentile" in gravado:
            limiar = float(gravado["mvrv_max_percentile"])
    except Exception:
        pass

    print("\n  Filtro de regime (MVRV Z-Score):")
    if limiar >= 1.0:
        print("    DESLIGADO (limiar 1.0) — nenhuma entrada e barrada por regime.")
        return

    print(f"    limiar     : percentil <= {limiar:.0%} para ABRIR posicao")
    provider = OnChainProvider(settings)
    leitura = await provider.mvrv_reading()
    if leitura is None:
        novos = await provider.refresh_mvrv(force=True)
        leitura = await provider.mvrv_reading()
        if leitura is None:
            print("    leitura    : INDISPONIVEL — sem o dado o filtro nao se aplica")
            print(f"                 (a busca trouxe {novos} pontos; ver logs)")
            return

    print(f"    leitura    : z = {leitura.value:.2f}, percentil {leitura.percentile:.0%} "
          f"({leitura.zone})")
    if leitura.percentile > limiar:
        print("    AGORA      : ABRIR POSICAO ESTA BLOQUEADO pelo filtro.")
        print("                 Fechar posicao continua liberado — o filtro nunca")
        print("                 trava a saida. Sinais de compra serao rejeitados")
        print("                 com motivo registrado, e isso e o esperado.")
    else:
        margem = limiar - leitura.percentile
        print(f"    AGORA      : liberado (margem de {margem:.0%} ate o bloqueio).")


def _check_sizing(settings, quote_balance: Decimal | None = None) -> SizingFeasibility | None:
    """Confere se patrimonio e limites permitem alguma ordem existir.

    Sem esta checagem, o diagnostico diria "tudo pronto" para uma configuracao
    em que todo sinal sera rejeitado -- e o sistema ficaria dias de pe, com
    heartbeat verde, sem nunca operar.

    Em `dry_run` usa o saldo simulado. Nos modos reais usa o saldo em moeda de
    cotacao, que e o poder de compra efetivo.

    Devolve o resultado (para a checagem de filtros da exchange saber o tamanho
    da ordem) ou `None` quando nenhuma ordem e possivel.
    """
    from .risk.rules import assess_sizing_feasibility

    quote = settings.trading.quote_currency
    print("\n  Dimensionamento de ordens:")

    if settings.sends_real_orders:
        # Cair no saldo simulado aqui seria o pior falso positivo do sistema: o
        # diagnostico aprovaria uma configuracao LIVE usando dinheiro de mentira
        # como referencia -- exatamente o "esta tudo certo" enganoso que esta
        # checagem existe para evitar.
        if quote_balance is None or quote_balance <= 0:
            print("    " + "!" * 62)
            print(f"    SEM SALDO em {quote} na conta da exchange.")
            print(f"    O sistema negocia pares cotados em {quote} e precisa dessa")
            print("    moeda para comprar. Converta o saldo que voce tem, ou aponte")
            print("    a moeda de cotacao, em Configuracoes, para a que voce possui.")
            print("    " + "!" * 62)
            return None
        balance = quote_balance
        origin = f"saldo real em {quote} na exchange"
    else:
        balance = settings.trading.paper_initial_balance
        origin = f"saldo simulado ({quote})"

    result = assess_sizing_feasibility(settings.risk, balance)
    print(f"    patrimonio de referencia: {balance:.2f} {quote} — {origin}")

    if result.feasible:
        print(f"    {result.explain(quote)}")
        return result

    print("    " + "!" * 62)
    for line in _wrap(result.explain(quote), 60):
        print(f"    {line}")
    print("    Aumente o patrimonio, ou, na tela de risco: reduza a ordem")
    print("    minima e suba o percentual por ordem / exposicao por ativo.")
    print("    " + "!" * 62)
    return None


def _check_market_filters(
    settings,
    markets: dict,
    tickers: dict,
    symbols: list[str],
    sizing: SizingFeasibility,
) -> bool:
    """Simula a ordem contra os filtros REAIS da exchange.

    O dimensionamento acima so conhece os nossos limites. A exchange tem os
    dela: a quantidade e truncada para o passo do lote e so entao o valor
    minimo e aplicado. Em BTC, com passo de 0,00001 a 79 mil, cada passo vale
    ~0,79 USDT -- uma ordem de 5,50 vira 4,74 e a Binance recusa, enquanto o
    mesmo valor passa em ETH ou SOL.

    Sem esta checagem, o diagnostico aprovaria uma configuracao em que toda
    ordem seria rejeitada, e a descoberta viria operando com dinheiro real.
    """
    from .exchanges.filters import check_all

    if not symbols or not markets:
        return True

    quote = settings.trading.quote_currency
    notional = sizing.max_possible_order
    results = check_all(markets, tickers, symbols, notional)
    if not results:
        return True

    print(f"\n  Filtros da exchange (ordem de {notional:.2f} {quote}):")
    blocked = [r for r in results if not r.viable]

    for result in results:
        mark = "OK " if result.viable else "NAO"
        print(
            f"    {mark} {result.symbol:<14} {result.requested_notional:>7.2f} -> "
            f"{result.effective_notional:>7.2f} {quote}"
            f"   (minimo da exchange: {result.min_cost:g})"
        )

    if not blocked:
        return True

    needed = max(r.suggested_notional for r in blocked)
    print("    " + "!" * 62)
    print(f"    {len(blocked)} par(es) rejeitariam a ordem por causa do arredondamento")
    print("    de lote. Ajustes possiveis:")
    print(f"      - subir a ordem minima para {needed:.2f} (e o teto por")
    print("        ordem junto, para caber), ou")
    print(f"      - remover da whitelist: {', '.join(r.symbol for r in blocked)}")
    print("    " + "!" * 62)
    return False


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width)


async def _check_credentials(settings) -> tuple[bool, Decimal | None]:
    """Valida a credencial contra um endpoint AUTENTICADO da exchange.

    A checagem de conectividade acima usa apenas endpoints publicos, que
    funcionam mesmo com a chave bloqueada. Sem esta sonda, o diagnostico diria
    "tudo pronto" para um sistema que nao consegue enviar uma unica ordem --
    e a descoberta viria na primeira tentativa de operar.

    Devolve `(ok, saldo_em_moeda_de_cotacao)`. O saldo alimenta a checagem de
    dimensionamento: em testnet/live o que vale e o dinheiro que existe na
    conta, nao o saldo simulado da configuracao de negocio.
    """
    from .exchanges import CcxtExchange
    from .exchanges.base import ApiAccessDenied

    credentials = settings.credentials_for(settings.exchange)

    if not settings.sends_real_orders:
        print("\n  Credencial de negociacao: nao exigida em dry_run (broker simulado).")
        if credentials.configured:
            print("    Chave presente no .env, mas nao sera usada neste modo.")
        return True, None

    if not credentials.configured:
        print(f"\n  Credencial de negociacao: AUSENTE para '{settings.exchange}'.")
        print(f"    TRADING_MODE={settings.trading_mode} exige chave de API no .env.")
        return False, None

    print("\n  Credencial de negociacao (endpoint autenticado):")
    client = CcxtExchange(
        settings.exchange,
        credentials,
        testnet=settings.trading_mode is TradingMode.TESTNET,
    )
    try:
        balances = await client.fetch_balances()
    except ApiAccessDenied as exc:
        print("    ACESSO NEGADO pela exchange.")
        print(f"    {exc}")
        await _print_public_ip()
        return False, None
    except Exception as exc:
        print(f"    FALHOU — {exc}")
        return False, None
    finally:
        await client.close()

    assets = ", ".join(sorted(balances)) if balances else "nenhum saldo positivo"
    print(f"    leitura de saldo OK — ativos: {assets}")
    print("    (envio de ordem nao e testado aqui: isso exigiria uma ordem real)")
    return True, balances.get(settings.trading.quote_currency)


async def _print_public_ip() -> None:
    """Mostra o IP de saida desta maquina, para comparar com a whitelist.

    Best-effort e apenas neste caminho de erro: e a informacao que resolve o
    caso mais comum (IP residencial dinamico mudou). Se o servico externo nao
    responder, o diagnostico segue sem ele.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=8) as http:
            response = await http.get("https://api.ipify.org")
            response.raise_for_status()
            print(f"\n    IP de saida desta maquina agora: {response.text.strip()}")
            print("    Confira se e exatamente este o IP na whitelist da API.")
    except Exception:
        print("\n    (nao foi possivel descobrir o IP de saida automaticamente)")


# ---------------------------------------------------------------------------
async def _backtest(settings, args) -> int:
    from .backtest.engine import BacktestEngine
    from .exchanges import build_market_data_source
    from .strategies import available_strategies, get_strategy

    if not args.symbol and not settings.trading.symbols:
        print(
            "A lista de pares esta vazia (descoberta automatica), entao o backtest "
            "precisa de um par explicito.\n"
            "Exemplo: crypto-traders backtest --symbol BTC/USDT"
        )
        return 1
    symbol = args.symbol or settings.trading.symbols[0]
    timeframe = args.timeframe or settings.trading.timeframe
    balance = args.balance or settings.trading.paper_initial_balance

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
        args.strategy or settings.trading.strategies[0]
    ]

    results = []
    for name in names:
        engine = BacktestEngine(
            get_strategy(name),
            settings.risk,
            quote_currency=settings.trading.quote_currency,
            initial_balance=balance,
            fee_pct=settings.trading.paper_fee_pct,
            slippage_pct=settings.trading.paper_slippage_pct,
            lookback=settings.trading.candle_history_limit,
        )
        try:
            results.append(await engine.run(closed))
        except ValueError as exc:
            print(f"  {name}: {exc}")

    if not results:
        return 1

    _print_backtest_table(results, balance, settings.trading.quote_currency)
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
