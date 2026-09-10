"""PROVA DE BRECHA (critico do item 5): a apuracao ignora `trades.mode`.

`PortfolioAgent._apurar` percorria TODOS os trades da tabela sem olhar a coluna
`mode`. Os trades do ensaio em dry_run (dinheiro de papel) entravam no mesmo
preco medio e no mesmo resultado realizado que os trades de dinheiro real.

O preco medio nao e um numero de relatorio: e a UNICA entrada do nivel de
stop-loss e de take-profit (`risk_manager.enforce_protective_exits`).
Contaminado, o stop dispara numa posicao que nao perdeu nada -- ou nunca dispara
na que perdeu.

CORRECAO DE FIXTURE (rodada 3). Estes tres testes nasceram usando a fixture
`settings`, cujo `trading_mode` e DRY_RUN -- o padrao de fabrica
(`config.py:391`). Com isso eles cobravam que, **com o modo corrente em
dry_run**, o trade de dry_run ficasse de fora e o de dinheiro real entrasse: o
oposto do filtro correto, que e por modo CORRENTE. O oposto tambem se contradizia
com `test_portfolio_agent.py::TestModoSeparaODinheiro`, onde o mesmo cenario
(dois trades dry_run, modo corrente dry_run, carteira de 60) e cobrado com
realizado -40 em `test_retrato_de_outro_modo_nao_ancora_o_realizado` enquanto
aqui era cobrado 0 -- as duas expectativas nao podem valer juntas.

O filtro nao pode ser "ignorar papel": o ensaio roda AGORA em dry_run, e excluir
dry_run sempre deixaria toda posicao do ensaio sem preco medio, ou seja SEM STOP
NENHUM (ver `test_o_ensaio_em_dry_run_continua_com_preco_medio_e_stop`).

Entao o que foi corrigido aqui e so o modo CORRENTE: a fixture passa a ser
`modo_real` e o trade de dinheiro real passa a ser gravado com esse mesmo modo.
Nenhuma asserçao mudou. TESTNET, e nao LIVE, pelo motivo que a fixture irma
declara: o filtro e uma igualdade sobre `trades.mode`, entao a contaminacao
papel-contra-real e identica nos dois, e nenhum teste da suite constroi
configuracao de dinheiro real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_traders.agents.portfolio import PortfolioAgent
from crypto_traders.agents.risk_manager import RiskManagerAgent
from crypto_traders.bus import InMemoryEventBus
from crypto_traders.db.repositories import TradeRepository
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import Side, TradeOrigin, TradingMode
from crypto_traders.exchanges.paper import PaperBroker

INICIO = datetime(2026, 9, 1, tzinfo=UTC)

#: Modo do trade de dinheiro real neste arquivo. Ver a correcao de fixture no
#: topo: e o MESMO modo corrente do agente, que e o que o filtro compara.
REAL = "testnet"


@pytest.fixture
def modo_real(settings):
    """Modo em que as ordens saem da maquina de verdade."""
    return settings.model_copy(update={"trading_mode": TradingMode.TESTNET})


async def registrar(settings, side, quantidade, preco, *, minuto, modo):
    async with session_scope(settings) as session:
        await TradeRepository(session).record(
            executed_at=INICIO + timedelta(minutes=minuto),
            exchange="binance",
            symbol="BTC/USDT",
            side=str(side),
            quantity=Decimal(quantidade),
            price=Decimal(preco),
            fee=Decimal(0),
            fee_currency="USDT",
            origin=TradeOrigin.AGENT,
            mode=modo,
        )


def agente(settings, broker, precos):
    fonte = {a: Decimal(v) for a, v in precos.items()}
    return PortfolioAgent(
        InMemoryEventBus(), broker, settings, price_source=lambda: dict(fonte)
    )


async def test_trade_de_dry_run_contamina_o_preco_medio_do_live(modo_real):
    """O ensaio comprou 1 BTC a 100 no papel. O real comprou 1 BTC a 50."""
    await registrar(modo_real, Side.BUY, "1", "100", minuto=0, modo="dry_run")
    await registrar(modo_real, Side.BUY, "1", "50", minuto=1, modo=REAL)

    # Na exchange existe UM BTC: o comprado com dinheiro real, a 50.
    broker = PaperBroker(initial_balance=Decimal("0"))
    broker.credit("BTC", Decimal("1"))
    snapshot = await agente(modo_real, broker, {"BTC": "50"}).build_snapshot()
    btc = next(p for p in snapshot.positions if p.asset == "BTC")

    assert btc.average_price == Decimal("50"), (
        f"preco medio contaminado pelo dry_run: {btc.average_price}"
    )
    assert snapshot.unrealized_pnl == Decimal("0"), (
        f"prejuizo inventado pelo trade de papel: {snapshot.unrealized_pnl}"
    )


async def test_stop_loss_dispara_em_posicao_que_nao_perdeu_nada(modo_real):
    """A consequencia: venda a mercado de dinheiro real, sem perda nenhuma.

    Medio real 50, preco 50, stop_loss_pct 3% => nivel 48,50, nada a fazer.
    Com o trade de papel dentro, o medio vira 75 e o nivel 72,75: o preco 50
    fica abaixo e o Risk Manager EMITE o fechamento.
    """
    await registrar(modo_real, Side.BUY, "1", "100", minuto=0, modo="dry_run")
    await registrar(modo_real, Side.BUY, "1", "50", minuto=1, modo=REAL)

    broker = PaperBroker(initial_balance=Decimal("0"))
    broker.credit("BTC", Decimal("1"))
    snapshot = await agente(modo_real, broker, {"BTC": "50"}).build_snapshot()

    bus = InMemoryEventBus()
    await bus.start()
    risco = RiskManagerAgent(bus, modo_real)
    emitidas = await risco.enforce_protective_exits(snapshot)

    assert emitidas == [], (
        "stop-loss emitido sem perda: "
        + "; ".join(f"{o.side} {o.quantity} {o.symbol}" for o in emitidas)
    )


async def test_resultado_realizado_do_papel_entra_no_numero_do_live(modo_real):
    """O ensaio perdeu 40 no papel. O real nunca fechou posicao: realizado 0."""
    await registrar(modo_real, Side.BUY, "1", "100", minuto=0, modo="dry_run")
    await registrar(modo_real, Side.SELL, "1", "60", minuto=1, modo="dry_run")

    broker = PaperBroker(initial_balance=Decimal("60"))
    snapshot = await agente(modo_real, broker, {"BTC": "60"}).build_snapshot()

    assert snapshot.realized_pnl == Decimal("0"), (
        f"prejuizo de papel no resultado do live: {snapshot.realized_pnl}"
    )


class TestNoModoEmQueODinheiroEDoDono:
    """O mesmo cenario, em `TRADING_MODE=live` de verdade.

    A verificacao final desta rodada apontou o residuo que os tres testes acima
    deixaram: eles usam TESTNET, e `grep TradingMode.LIVE tests/` nao devolvia
    **nenhum** teste construindo configuracao de dinheiro real. O filtro e uma
    igualdade sobre `trades.mode`, entao a aritmetica e a mesma nos dois modos --
    mas "a aritmetica e a mesma" era exatamente o tipo de raciocinio que deixou
    o stop-loss inerte passar despercebido neste projeto. O modo em que o
    dinheiro e do dono merece a medicao, nao a inferencia.

    Nada sai da maquina aqui: o broker e o `PaperBroker` e a `Settings` e
    construida em memoria com `_env_file=None`. As duas travas de D6 sao
    satisfeitas no objeto, e nao no `.env`, que continua em `dry_run`.
    """

    @pytest.fixture
    def modo_live(self, settings):
        """Configuracao de dinheiro real, construida em memoria.

        `database_url` vem da fixture compartilhada para o teste continuar no
        banco isolado -- nunca no `data/crypto_traders.db` do ensaio.
        """
        from crypto_traders.config import Settings

        # D6: `live` exige a segunda confirmacao. Passar as duas aqui prova
        # tambem que a trava e do objeto de configuracao, e nao do arquivo.
        return Settings(
            _env_file=None,
            trading_mode="live",
            live_trading_confirmed=True,
            database_url=settings.database_url,
        )

    async def test_o_trade_de_papel_fica_fora_do_preco_medio_do_dinheiro_real(
        self, modo_live
    ):
        """Medio real 50, e nao os 75 que a media com o papel produziria."""
        live = modo_live

        await registrar(live, Side.BUY, "1", "100", minuto=0, modo="dry_run")
        await registrar(live, Side.BUY, "1", "50", minuto=1, modo="live")

        broker = PaperBroker(initial_balance=Decimal("0"))
        broker.credit("BTC", Decimal("1"))
        snapshot = await agente(live, broker, {"BTC": "50"}).build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("50"), (
            f"em LIVE o preco medio veio contaminado pelo dry_run: {btc.average_price}"
        )

    async def test_o_stop_nao_vende_dinheiro_real_por_causa_do_ensaio(self, modo_live):
        """A consequencia que custaria dinheiro de verdade, medida em live."""
        live = modo_live

        await registrar(live, Side.BUY, "1", "100", minuto=0, modo="dry_run")
        await registrar(live, Side.BUY, "1", "50", minuto=1, modo="live")

        broker = PaperBroker(initial_balance=Decimal("0"))
        broker.credit("BTC", Decimal("1"))
        snapshot = await agente(live, broker, {"BTC": "50"}).build_snapshot()

        bus = InMemoryEventBus()
        await bus.start()
        emitidas = await RiskManagerAgent(bus, live).enforce_protective_exits(snapshot)

        assert emitidas == [], (
            "em LIVE o stop foi emitido sem perda nenhuma: "
            + "; ".join(f"{o.side} {o.quantity} {o.symbol}" for o in emitidas)
        )
