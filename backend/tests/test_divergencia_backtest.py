"""D3: o backtest e a producao respondem o mesmo sobre a mesma entrada?

O backtest nao e um relatorio. E o unico lugar onde uma estrategia e reprovada
antes de o dinheiro do dono entrar. Um backtest que responde diferente da
producao nao erra o retorno: erra a DECISAO de operar.

Este projeto ja encontrou tres divergencias entre os dois lados:

1. o stop-loss existia no backtest e nao em producao;
2. o `.env` era sobreposto pelo banco, entao os limites medidos nao eram os
   aplicados;
3. os sinais concorrentes eram atendidos em ordem de dicionario num lado e por
   confianca no outro.

O gauntlet acabou de mexer no quarto candidato: `realized_pnl` no retrato do
portfolio passou a ser DERIVADO do historico por custo medio movel e a filtrar
por `trades.mode` (defeitos 2, 3 e 4). O backtest tem o proprio caminho de PnL,
em `backtest/portfolio.py`, que o gauntlet nao tocou -- `git log` daquele
diretorio para no commit anterior. Duas contas de custo medio escritas em
lugares diferentes, uma delas recem-reescrita, e a unica forma de saber se
concordam e rodar as duas sobre a MESMA entrada.

Foi o que este arquivo faz, e o que ele encontrou esta em
`TestAQuartaDivergencia`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import numpy as np
import pytest
from helpers import make_candles

from crypto_traders.agents.portfolio import ACAO_COTACAO, PortfolioAgent
from crypto_traders.backtest.portfolio import (
    PortfolioBacktestEngine,
    PortfolioBacktestResult,
)
from crypto_traders.bus import InMemoryEventBus
from crypto_traders.config import RiskSettings, Settings, TradingSettings
from crypto_traders.db.repositories import TradeRepository
from crypto_traders.db.session import dispose_engine, init_db, session_scope
from crypto_traders.domain.enums import (
    ExchangeName,
    RiskDecision,
    Side,
    SignalDirection,
)
from crypto_traders.domain.models import RiskAssessment, Signal
from crypto_traders.exchanges.paper import PaperBroker
from crypto_traders.risk.rules import RiskEngine
from crypto_traders.strategies import get_strategy
from crypto_traders.strategies.base import Strategy

INICIO = datetime(2026, 3, 2, tzinfo=UTC)

#: Escala das colunas de dinheiro (`db.models.MONEY_SCALE`). Toda comparacao
#: entre os dois caminhos tem este piso, e o motivo esta em
#: `TestOsDoisCaminhosDePnl.test_a_diferenca_residual_e_a_escala_do_banco`.
UM_WEI = Decimal(1).scaleb(-18)


# ---------------------------------------------------------------------------
# Bancada
# ---------------------------------------------------------------------------


def limites(quote: str = "USDC", **trocas) -> RiskSettings:
    base = {
        "max_order_notional": Decimal("100000"),
        "max_order_pct_portfolio": 0.5,
        "max_asset_exposure_pct": 1.0,
        "max_open_positions": 5,
        "min_order_notional": Decimal("10"),
        "stop_loss_pct": 0.03,
        "take_profit_pct": 0.06,
        "daily_loss_limit_pct": 0.05,
        "weekly_loss_limit_pct": 0.12,
        "min_signal_confidence": 0.0,
        "asset_whitelist": ["BTC", "ETH", "BRL", quote],
        "symbol_whitelist": ["BTC/USDC", "ETH/USDC", "BTC/BRL", "BTC/USDT"],
        "cooldown_seconds": 0,
    }
    return RiskSettings(**(base | trocas))


def serie(periodos: int, semente: int, base: float) -> list[float]:
    """Serie oscilante e reprodutivel: precisa gerar compra E venda."""
    rng = np.random.default_rng(semente)
    x = np.linspace(0, 10 * np.pi, periodos)
    return list(base + base * 0.25 * np.sin(x) + rng.normal(0, base * 0.01, periodos))


def motor(
    quote: str = "USDC", risco: RiskSettings | None = None, **trocas
) -> PortfolioBacktestEngine:
    padroes = {
        "quote_currency": quote,
        "initial_balance": Decimal("1000"),
        "fee_pct": Decimal("0.001"),
        "slippage_pct": Decimal("0.0005"),
    }
    return PortfolioBacktestEngine(
        [get_strategy("ma_crossover", fast=3, slow=10)],
        risco or limites(quote),
        **(padroes | trocas),
    )


async def rodar(symbols: dict[str, float], quote: str = "USDC", periodos: int = 300, **trocas):
    candles = {
        par: make_candles(serie(periodos, 7 + i, base), symbol=par, timeframe="1h")
        for i, (par, base) in enumerate(symbols.items())
    }
    return await motor(quote, **trocas).run(candles)


async def _configuracao(
    tmp_path, quote: str, modo: str = "dry_run", arquivo: str = "divergencia.db"
) -> Settings:
    """Producao de verdade, com banco descartavel.

    `arquivo` existe porque `init_db` cria o schema mas nao apaga linha: dois
    cenarios no mesmo `tmp_path` compartilhariam o historico, e o segundo apuraria
    os trades do primeiro. Custou uma falsa aprovacao ao escrever este arquivo.
    """
    await dispose_engine()
    configurado = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / arquivo).as_posix()}",
        trading_mode=modo,
        trading=TradingSettings(
            symbols=["BTC/USDC"], strategies=["ma_crossover"], quote_currency=quote
        ),
        risk=limites(quote),
    )
    await init_db(configurado)
    return configurado


async def apurar_como_producao(
    tmp_path, trades, quote: str, modo: str = "dry_run", arquivo: str = "divergencia.db"
):
    """Grava os trades e devolve a apuracao de PRODUCAO sobre eles.

    Passa pela tabela `trades` de proposito, e nao por objetos de mentira: o
    caminho de producao inclui a ida e a volta do banco, e e ali que a escala do
    tipo `Money` age. Um comparador que pulasse o banco compararia duas
    aritmeticas e nao os dois CAMINHOS.
    """
    configurado = await _configuracao(tmp_path, quote, modo, arquivo)
    async with session_scope(configurado) as sessao:
        repositorio = TradeRepository(sessao)
        for trade in trades:
            await repositorio.record(
                executed_at=trade.timestamp,
                exchange="paper",
                symbol=trade.symbol,
                side=trade.side,
                quantity=trade.quantity,
                price=trade.price,
                fee=trade.fee,
                fee_currency=quote,
                mode=modo,
                strategy=trade.strategy,
            )
    async with session_scope(configurado) as sessao:
        linhas = await TradeRepository(sessao).list(limit=5000)
        agente = PortfolioAgent(
            InMemoryEventBus(), PaperBroker(quote_currency=quote), configurado, lambda: {}
        )
        apuracao = agente._apurar(linhas)
    await dispose_engine()
    return apuracao


class _Lancamento:
    """Um trade escrito a mao, com a mesma forma de um `PortfolioTrade`."""

    def __init__(self, minuto, symbol, side, quantity, price, fee="0"):
        self.timestamp = INICIO + timedelta(minutes=minuto)
        self.symbol = symbol
        self.side = str(Side.BUY if side == "buy" else Side.SELL)
        self.quantity = Decimal(quantity)
        self.price = Decimal(price)
        self.fee = Decimal(fee)
        self.strategy = "sequencia"


async def realizado_pelo_backtest(sequencia: list[_Lancamento], quote: str = "USDC"):
    """A MESMA sequencia pelo caminho de PnL do backtest.

    Nao e reimplementacao: chama `PortfolioBacktestEngine._execute`, que e o
    codigo que roda num backtest de carteira, com um `PaperBroker` de verdade
    preenchendo as ordens. O que a bancada substitui e apenas quem escolhe os
    sinais -- aqui a sequencia e ditada, para que os dois lados recebam
    exatamente a mesma entrada.
    """
    engine = motor(quote, initial_balance=Decimal("1000000"), fee_pct=Decimal(0),
                   slippage_pct=Decimal(0))
    broker = PaperBroker(
        quote_currency=quote,
        initial_balance=Decimal("1000000"),
        fee_pct=Decimal(0),
        slippage_pct=Decimal(0),
    )
    resultado = PortfolioBacktestResult(
        quote_currency=quote,
        start=INICIO,
        end=INICIO + timedelta(days=1),
        initial_balance=Decimal("1000000"),
        final_value=Decimal(0),
        symbols=[],
    )
    custos: dict[str, Decimal] = {}
    protecao: dict[str, tuple[Decimal, Decimal]] = {}
    ultima: dict[str, datetime] = {}
    precos: dict[str, Decimal] = {}

    for ordem, lancamento in enumerate(sequencia, start=1):
        base = lancamento.symbol.partition("/")[0]
        broker.set_price(base, lancamento.price)
        precos[base] = lancamento.price
        comprando = lancamento.side == str(Side.BUY)
        sinal = Signal(
            exchange=ExchangeName.PAPER,
            symbol=lancamento.symbol,
            timeframe="1h",
            strategy=lancamento.strategy,
            direction=SignalDirection.LONG if comprando else SignalDirection.FLAT,
            confidence=0.9,
            reason="sequencia ditada",
            reference_price=lancamento.price,
        )
        veredito = RiskAssessment(
            signal_id=sinal.id,
            decision=RiskDecision.APPROVED,
            approved_quantity=lancamento.quantity,
            approved_notional=lancamento.quantity * lancamento.price,
            stop_loss=lancamento.price * Decimal("0.97"),
            take_profit=lancamento.price * Decimal("1.06"),
        )
        await engine._execute(
            sinal, veredito, broker, precos, custos, protecao, ultima,
            resultado, lancamento.timestamp, ordem,
        )
    return resultado, custos


# ---------------------------------------------------------------------------
# 1. O backtest reutiliza os objetos de producao?
# ---------------------------------------------------------------------------


class TestOMotorEODaProducao:
    """Rastreado no codigo, e depois medido AGINDO.

    "Usa o mesmo RiskEngine" e afirmacao de docstring (a de
    `api/routes/backtest.py` diz isso literalmente). Que o objeto seja da classe
    certa nao prova que ele decide nada -- foi assim que os filtros da exchange
    ficaram um ano sem chamador em `src/`. Aqui se confere a identidade E o
    efeito.
    """

    def test_o_motor_de_risco_e_o_da_producao(self):
        engine = motor()
        assert isinstance(engine.engine, RiskEngine)

    def test_as_estrategias_sao_as_da_producao(self):
        engine = motor()
        assert engine.strategies
        assert all(isinstance(e, Strategy) for e in engine.strategies)
        assert type(engine.strategies[0]) is type(get_strategy("ma_crossover"))

    async def test_o_preenchimento_e_o_paper_broker_da_producao(self, monkeypatch):
        """O broker e construido dentro de `run`, entao a prova e interceptando."""
        construidos: list[PaperBroker] = []
        original = PaperBroker.__init__

        def espiao(self, **kwargs):
            original(self, **kwargs)
            construidos.append(self)

        monkeypatch.setattr(PaperBroker, "__init__", espiao)
        await rodar({"BTC/USDC": 100.0}, periodos=120)
        assert construidos, "o backtest nao construiu um PaperBroker"
        assert all(isinstance(b, PaperBroker) for b in construidos)

    async def test_mudar_um_limite_de_risco_muda_o_backtest(self):
        """A prova de que o RiskEngine AGE, e nao so esta pendurado no motor.

        A alavanca e `min_order_notional` acima do que a carteira sustenta: com
        2.000 de minimo sobre 1.000 de saldo, ordem nenhuma e viavel. Se o motor
        ignorasse os limites, o numero de trades nao mudaria.

        A primeira alavanca tentada aqui foi `max_order_pct_portfolio` de 50%
        para 1%, e ela NAO mudou nada -- 1% de 1.000 e exatamente os 10 do
        minimo, entao a ordem seguia viavel. Supor que "mais apertado" e sempre
        "menos trades" era supor; o numero e que decide.
        """
        largo = await rodar({"BTC/USDC": 100.0})
        apertado = await rodar(
            {"BTC/USDC": 100.0}, risco=limites(min_order_notional=Decimal("2000"))
        )
        assert len(largo.trades) >= 6
        assert len(apertado.trades) == 0
        assert apertado.signals_rejected > 0

    async def test_o_stop_do_backtest_usa_o_limite_configurado(self):
        """D3 numero 1 ao contrario: o stop existe nos dois lados e obedece.

        O alvo fica FIXO nos dois lados de proposito, senao o numero de paradas
        mudaria por duas causas ao mesmo tempo. Varia so o stop.
        """
        frouxo = await rodar(
            {"BTC/USDC": 100.0}, risco=limites(stop_loss_pct=0.30, take_profit_pct=0.60)
        )
        justo = await rodar(
            {"BTC/USDC": 100.0}, risco=limites(stop_loss_pct=0.005, take_profit_pct=0.60)
        )
        assert justo.exits["stop"] > frouxo.exits["stop"], (
            f"stop 0,5% deu {justo.exits['stop']} paradas e stop 30% deu "
            f"{frouxo.exits['stop']}"
        )


# ---------------------------------------------------------------------------
# 2. Os dois caminhos de PnL sobre a mesma entrada
# ---------------------------------------------------------------------------


class TestOsDoisCaminhosDePnl:
    """`backtest/portfolio.py` versus `agents/portfolio.py::_apurar`.

    Duas implementacoes independentes de custo medio movel. A do backtest e
    incremental (guarda `cost_basis` por ativo enquanto anda); a de producao
    reapura o historico INTEIRO a cada retrato, de proposito (defeito 4 do
    gauntlet: a ancora de tempo somava o mesmo lucro 1.440 vezes por dia).
    Dois codigos, um resultado esperado.
    """

    async def test_a_sequencia_do_commit_da_650_nos_dois_caminhos(self, tmp_path):
        """`BUY 1@100 + BUY 3@200 + SELL 2@500` -- o caso que o defeito 3 mediu.

        E o teste de valor conhecido: 650 nao vem de rodar o codigo, vem da
        conta. Medio de 175 nas quatro unidades, venda de duas a 500,
        `2 * (500 - 175) = 650`, e o medio NAO se move na venda parcial.
        """
        sequencia = [
            _Lancamento(0, "BTC/USDC", "buy", "1", "100"),
            _Lancamento(1, "BTC/USDC", "buy", "3", "200"),
            _Lancamento(2, "BTC/USDC", "sell", "2", "500"),
        ]
        producao = await apurar_como_producao(tmp_path, sequencia, "USDC")
        backtest, custos = await realizado_pelo_backtest(sequencia)

        assert producao.realizado == Decimal("650")
        assert backtest.realized_pnl == Decimal("650")
        assert producao.realizado == backtest.realized_pnl
        # E o que sobra tambem tem de bater: o medio das duas unidades restantes
        # e a UNICA entrada do nivel de stop.
        assert producao.custo_medio("BTC") == Decimal("175")
        assert custos["BTC"] / Decimal("2") == Decimal("175")

    @pytest.mark.parametrize(
        "nome,sequencia",
        [
            (
                "fechamento total",
                [
                    _Lancamento(0, "BTC/USDC", "buy", "2", "100"),
                    _Lancamento(1, "BTC/USDC", "sell", "2", "150"),
                ],
            ),
            (
                "venda parcial e depois o resto",
                [
                    _Lancamento(0, "BTC/USDC", "buy", "4", "100"),
                    _Lancamento(1, "BTC/USDC", "sell", "1", "200"),
                    _Lancamento(2, "BTC/USDC", "sell", "3", "50"),
                ],
            ),
            (
                "prejuizo",
                [
                    _Lancamento(0, "BTC/USDC", "buy", "1", "100"),
                    _Lancamento(1, "BTC/USDC", "sell", "1", "40"),
                ],
            ),
            (
                "dois ativos disputando a mesma carteira",
                [
                    _Lancamento(0, "BTC/USDC", "buy", "1", "100"),
                    _Lancamento(1, "ETH/USDC", "buy", "10", "5"),
                    _Lancamento(2, "BTC/USDC", "sell", "1", "120"),
                    _Lancamento(3, "ETH/USDC", "sell", "10", "4"),
                ],
            ),
            (
                "reforco de posicao antes de vender",
                [
                    _Lancamento(0, "BTC/USDC", "buy", "1", "100"),
                    _Lancamento(1, "BTC/USDC", "buy", "1", "300"),
                    _Lancamento(2, "BTC/USDC", "buy", "2", "200"),
                    _Lancamento(3, "BTC/USDC", "sell", "4", "250"),
                ],
            ),
            (
                "ida e volta completa, duas rodadas",
                [
                    _Lancamento(0, "BTC/USDC", "buy", "3", "100"),
                    _Lancamento(1, "BTC/USDC", "sell", "3", "110"),
                    _Lancamento(2, "BTC/USDC", "buy", "3", "90"),
                    _Lancamento(3, "BTC/USDC", "sell", "3", "95"),
                ],
            ),
            (
                "medio com dizima: 100/3 nao termina",
                [
                    _Lancamento(0, "BTC/USDC", "buy", "3", "33.333333333333333"),
                    _Lancamento(1, "BTC/USDC", "sell", "3", "40"),
                    _Lancamento(2, "BTC/USDC", "buy", "3", "10"),
                    _Lancamento(3, "BTC/USDC", "sell", "3", "20"),
                ],
            ),
        ],
    )
    async def test_os_dois_caminhos_concordam(self, tmp_path, nome, sequencia):
        producao = await apurar_como_producao(tmp_path, sequencia, "USDC")
        backtest, _ = await realizado_pelo_backtest(sequencia)
        assert producao.realizado == backtest.realized_pnl, (
            f"{nome}: producao={producao.realizado} backtest={backtest.realized_pnl}"
        )

    async def test_o_residuo_do_fechamento_total_nao_contamina_a_rodada_seguinte(
        self, tmp_path
    ):
        """A producao zera o custo EXATO no fechamento total; o backtest subtrai.

        `custos[asset] = custo - vendido * medio if restante > 0 else Decimal(0)`
        existe por causa da dizima. A sequencia abaixo a produz de verdade:
        `1@10 + 2@20` da custo 50 em 3 unidades, e `50 / 3` e
        16,66666666666666666666666667 nos 28 digitos do `decimal`. Vender as
        tres deixa `50 - 3 * medio = -1E-26` -- verificado antes de escrever a
        assercao, porque a primeira sequencia tentada aqui (`3 @ 33,333...`)
        fechava EXATO e o teste passava sem medir nada.

        O backtest nao tem essa guarda, entao o residuo fica no `cost_basis` e
        entra no medio da posicao SEGUINTE -- que e a entrada do nivel de stop.
        Este teste mede o tamanho do residuo em vez de supor que e inofensivo.
        """
        sequencia = [
            _Lancamento(0, "BTC/USDC", "buy", "1", "10"),
            _Lancamento(1, "BTC/USDC", "buy", "2", "20"),
            _Lancamento(2, "BTC/USDC", "sell", "3", "40"),
        ]
        producao = await apurar_como_producao(tmp_path, sequencia, "USDC")
        _, custos = await realizado_pelo_backtest(sequencia)

        assert producao.custos["BTC"] == Decimal(0), "producao tem de zerar exato"
        assert abs(custos["BTC"]) <= UM_WEI, (
            f"residuo do backtest = {custos['BTC']}, acima de um wei"
        )
        # E a consequencia que importa: sem posicao, nao ha nivel de stop nos
        # dois lados. Um residuo positivo daria medio (e stop) a uma posicao
        # zerada.
        assert producao.custo_medio("BTC") is None

    async def test_a_diferenca_residual_e_a_escala_do_banco(self, tmp_path):
        """Backtest COMPLETO de ponta a ponta: rodar, e apurar o que ele produziu.

        Aqui ninguem dita a sequencia: a estrategia gera os sinais, o RiskEngine
        dimensiona, o PaperBroker preenche com taxa e deslizamento, e os trades
        resultantes vao para a tabela `trades` e para a apuracao de producao. E a
        pergunta do mandato na sua forma mais crua -- as duas contas concordam
        sobre a MESMA entrada?

        Concordam, e a diferenca que sobra tem causa conhecida e medida: as
        colunas de dinheiro tem escala 18 (`db.models.MONEY_SCALE`), e o backtest
        trabalha com os 28 digitos significativos do `decimal`. O que a producao
        apura e o historico ARREDONDADO na gravacao; o backtest nunca passou pelo
        banco. Nao e erro de formula -- e a formula sobre entradas que diferem no
        decimo nono decimal. Medido em 2026-09-10: 6,9E-18 em 155,05 de
        realizado, sobre dez trades.

        Este teste falha se a diferenca virar formula: o teto e um wei por trade.
        """
        resultado = await rodar({"BTC/USDC": 100.0})
        assert len(resultado.trades) >= 6, "sem vendas nao ha realizado a comparar"
        vendas = [t for t in resultado.trades if t.side == str(Side.SELL)]
        assert vendas, "a serie precisa gerar venda"

        producao = await apurar_como_producao(tmp_path, resultado.trades, "USDC")
        diferenca = abs(resultado.realized_pnl - producao.realizado)
        teto = UM_WEI * len(resultado.trades)

        assert diferenca <= teto, (
            f"backtest={resultado.realized_pnl} producao={producao.realizado} "
            f"diferenca={diferenca} teto={teto}"
        )
        # A diferenca e residual, e nao uma conta diferente: os dois lados
        # concordam em cada centavo.
        assert resultado.realized_pnl.quantize(Decimal("0.01")) == producao.realizado.quantize(
            Decimal("0.01")
        )

    async def test_o_modo_do_trade_separa_o_dinheiro_nos_dois_lados(self, tmp_path):
        """O filtro de modo (defeito 2) nao tem contraparte no backtest.

        O backtest nao tem modo -- roda sempre sobre dinheiro que nao existe.
        Entao a divergencia possivel e de USO: alimentar a apuracao de producao
        com trades de backtest gravados no modo errado. Aqui se prova que a
        producao os RECUSA: mesmos trades, modo `backtest`, realizado zero e
        aviso no audit_log.
        """
        sequencia = [
            _Lancamento(0, "BTC/USDC", "buy", "1", "100"),
            _Lancamento(1, "BTC/USDC", "sell", "1", "200"),
        ]
        no_modo = await apurar_como_producao(
            tmp_path, sequencia, "USDC", modo="dry_run", arquivo="no-modo.db"
        )
        assert no_modo.realizado == Decimal("100")

        configurado = await _configuracao(
            tmp_path, "USDC", modo="dry_run", arquivo="outro-modo.db"
        )
        async with session_scope(configurado) as sessao:
            repositorio = TradeRepository(sessao)
            for lancamento in sequencia:
                await repositorio.record(
                    executed_at=lancamento.timestamp,
                    exchange="paper",
                    symbol=lancamento.symbol,
                    side=lancamento.side,
                    quantity=lancamento.quantity,
                    price=lancamento.price,
                    mode="testnet",
                )
        async with session_scope(configurado) as sessao:
            linhas = await TradeRepository(sessao).list(limit=100)
            agente = PortfolioAgent(
                InMemoryEventBus(), PaperBroker(quote_currency="USDC"), configurado,
                lambda: {},
            )
            outro_modo = agente._apurar(linhas)
        await dispose_engine()

        assert outro_modo.realizado == Decimal(0)
        assert outro_modo.custo_medio("BTC") is None


# ---------------------------------------------------------------------------
# 3. A quarta divergencia
# ---------------------------------------------------------------------------


class TestAQuartaDivergencia:
    """ACHADO 2026-09-10: o backtest de carteira mistura moedas de cotacao.

    Producao aprendeu, no defeito 10 do gauntlet, que `symbol.partition("/")[0]`
    e chave errada: BTC/USDC e BTC/BRL gravavam o preco na MESMA chave "BTC" e o
    ultimo par da rajada vencia, em silencio. A correcao foi
    `MarketDataAgent._chave_de_preco`, que guarda o par de outra cotacao sob o
    nome COMPLETO -- e `PortfolioAgent._apurar`, que descarta os trades de outra
    cotacao com aviso no audit_log (`ACAO_COTACAO`).

    `backtest/portfolio.py` nao recebeu nem uma das duas: ainda usa
    `symbol.partition("/")[0]` como chave de preco, de posicao, de base de custo
    e de nivel de protecao. E a mesma linha da migracao de BRL para USDC (D24),
    onde `SYMBOLS` e editavel pela interface e um par da cotacao antiga esquecido
    na lista basta.

    O efeito medido nao e um numero levemente errado. E o backtest respondendo
    "esta estrategia nao opera" quando ela opera:

        so BTC/USDC ............. 10 trades, +15,51%
        BTC/USDC + BTC/BRL ......  0 trades,   0,00%

    O preco do BRL (387.188) sobrescreve a chave "BTC" dentro do PaperBroker, e
    a compra de 6,3 BTC aprovada pelo RiskEngine em USDC e recusada por saldo
    insuficiente. Nao sai aviso: o resultado apenas diz zero. Um backtest que
    responde zero e pior que um que levanta excecao, porque zero e uma resposta
    plausivel -- "a estrategia nao achou nada" -- e e ela que reprova a
    estrategia antes de o dinheiro entrar.

    O conserto e em `src/crypto_traders/backtest/portfolio.py`, fora do escopo
    deste item. Fica medido, e o `xfail(strict=True)` abaixo vira vermelho no dia
    em que for consertado.
    """

    #: A referencia: o par de USDC sozinho opera.
    SO_USDC: ClassVar[dict[str, float]] = {"BTC/USDC": 100.0}
    #: A mesma coisa com um residuo da cotacao antiga na lista de pares.
    COM_BRL: ClassVar[dict[str, float]] = {"BTC/USDC": 100.0, "BTC/BRL": 500000.0}

    async def test_a_producao_recusa_o_trade_de_outra_cotacao_com_aviso(self, tmp_path):
        """O lado que aprendeu: descarta e AVISA, em vez de misturar unidades."""
        sequencia = [
            _Lancamento(0, "BTC/USDC", "buy", "1", "100"),
            _Lancamento(1, "BTC/BRL", "buy", "1", "500000"),
            _Lancamento(2, "BTC/BRL", "sell", "1", "600000"),
            _Lancamento(3, "BTC/USDC", "sell", "1", "150"),
        ]
        producao = await apurar_como_producao(tmp_path, sequencia, "USDC")

        # 50 em USDC, e nem um centavo dos 100.000 BRL.
        assert producao.realizado == Decimal("50")
        assert any(aviso.action == ACAO_COTACAO for aviso in producao.avisos)
        aviso = next(a for a in producao.avisos if a.action == ACAO_COTACAO)
        assert aviso.target == "BRL"
        assert "sem stop" in aviso.detail

    async def test_o_backtest_soma_o_lucro_em_BRL_ao_realizado_em_USDC(self):
        """O lado que nao aprendeu, na aritmetica: 599.900 num livro de USDC."""
        sequencia = [
            _Lancamento(0, "BTC/USDC", "buy", "1", "100"),
            _Lancamento(1, "BTC/BRL", "buy", "1", "500000"),
            _Lancamento(2, "BTC/BRL", "sell", "1", "600000"),
            _Lancamento(3, "BTC/USDC", "sell", "1", "150"),
        ]
        backtest, _ = await realizado_pelo_backtest(sequencia)
        assert backtest.realized_pnl > Decimal("100000"), (
            f"realizado do backtest = {backtest.realized_pnl}"
        )

    async def test_um_par_da_cotacao_antiga_zera_o_backtest_do_par_certo(self):
        """O efeito de ponta a ponta, com a estrategia e o risco reais decidindo.

        Este teste PINA o defeito medido em vez de asserir o desejado: enquanto
        ele passar, o comportamento errado esta documentado com numero. Ele muda
        de resultado no dia do conserto, junto com o xfail abaixo.
        """
        referencia = await rodar(self.SO_USDC)
        contaminado = await rodar(self.COM_BRL)

        assert len(referencia.trades) >= 6, "a referencia precisa operar"
        assert len(contaminado.trades) == 0, (
            f"o par de BRL deixou de zerar o backtest: {len(contaminado.trades)} trades"
        )
        # E o mais grave: o resultado nao denuncia nada. Zero trades, zero
        # retorno, e `beat_the_market` verdadeiro porque nao operar foi melhor
        # que o mercado caindo.
        assert contaminado.total_return_pct == 0.0
        assert contaminado.realized_pnl == Decimal(0)

        # O RiskEngine barra a ORDEM em BRL -- essa defesa existe e age --, mas
        # ela chega tarde: o preco ja contaminou a chave "BTC" no passo 1 do
        # laco, antes de qualquer decisao.
        motivos = " ".join(contaminado.rejection_reasons)
        assert "moeda de cotacao" in motivos
        # Nao ha uma unica rejeicao por conta do par de USDC: os sinais dele
        # foram APROVADOS e morreram no preenchimento, sem contabilizacao.
        assert contaminado.signals_generated > contaminado.signals_rejected

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "DEFEITO MEDIDO 2026-09-10: backtest/portfolio.py usa "
            "symbol.partition('/')[0] como chave de preco, posicao, base de "
            "custo e protecao, entao um par cotado em outra moeda sobrescreve o "
            "par principal -- exatamente o defeito 10 do gauntlet, que foi "
            "consertado em MarketDataAgent._chave_de_preco e em "
            "PortfolioAgent._apurar e nunca chegou ao backtest. Medido: BTC/USDC "
            "sozinho da 10 trades e +15,51%; com BTC/BRL na lista da 0 trades e "
            "0,00%, sem aviso nenhum. Conserto em "
            "src/crypto_traders/backtest/portfolio.py, fora do escopo do item 10 "
            "-- xfail estrito para virar vermelho quando for consertado."
        ),
    )
    async def test_o_par_de_outra_cotacao_deveria_ser_inofensivo(self):
        """O que o backtest DEVERIA fazer: ignorar o par de outra cotacao.

        Producao ja se comporta assim -- o preco vai para o nome completo e os
        trades saem da apuracao com aviso. Um par a mais na lista, cotado em
        moeda que o sistema nao usa, nao pode mudar o que o backtest responde
        sobre o par que ele usa.
        """
        referencia = await rodar(self.SO_USDC)
        contaminado = await rodar(self.COM_BRL)
        assert len(contaminado.trades) == len(referencia.trades)
        assert contaminado.realized_pnl == referencia.realized_pnl
