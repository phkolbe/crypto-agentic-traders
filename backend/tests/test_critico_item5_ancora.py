"""PROVAS DE BRECHA (critico do item 5, rodada 2): a ancora por TIMESTAMP.

A rodada 2 trocou "realizado = soma do historico" por "realizado = realizado do
retrato anterior + o que foi executado DEPOIS dele". O corte e
`trade.executed_at > ancora.timestamp` (portfolio.py:531). Duas consequencias
que o implementador nao mediu, e as duas mexem no numero que arma/desarma a
trava:

1. `executed_at` NAO e monotonico. Ele vem do preenchimento (ou da digitacao do
   usuario, sem nenhuma validacao de data futura em `ManualTradeIn`), enquanto a
   ancora anda com o relogio. Trade datado no futuro fica "posterior" a TODA
   ancora seguinte e entra no incremento a cada retrato, 1.440 vezes por dia.
2. Insercao retroativa e indistinguivel de exclusao retroativa para esse corte.
   A imunidade a `DELETE /trades/{id}` foi comprada cegando o realizado para
   qualquer lancamento manual com data anterior ao ultimo retrato -- que e
   exatamente a data que um lancamento manual tem.

E a reconciliacao de saldo credita uma vez, mas nao desfaz: quantidade que sai e
VOLTA (carteira fria, Binance Earn, transferencia entre carteiras) deixa o
credito para tras e a marcacao a mercado volta junto com o ativo.
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
from crypto_traders.domain.enums import Side, TradeOrigin
from crypto_traders.exchanges.paper import PaperBroker


async def registrar(
    settings,
    side: Side,
    quantidade: str,
    preco: str,
    quando: datetime,
    *,
    origem: TradeOrigin = TradeOrigin.AGENT,
    modo: str = "dry_run",
) -> None:
    async with session_scope(settings) as session:
        await TradeRepository(session).record(
            executed_at=quando,
            exchange="paper",
            symbol="BTC/USDT",
            side=str(side),
            quantity=Decimal(quantidade),
            price=Decimal(preco),
            fee=Decimal(0),
            fee_currency="USDT",
            origin=origem,
            mode=modo,
        )


def agente(settings, broker: PaperBroker, precos: dict[str, str] | None = None):
    fonte = {a: Decimal(v) for a, v in (precos or {}).items()}
    return PortfolioAgent(
        InMemoryEventBus(), broker, settings, price_source=lambda: dict(fonte)
    )


def carteira(caixa: str, **ativos: str) -> PaperBroker:
    broker = PaperBroker(initial_balance=Decimal(caixa))
    for asset, quantidade in ativos.items():
        broker.credit(asset, Decimal(quantidade))
    return broker


@pytest.fixture
def producao(settings):
    """Configuracao do ensaio: 29,29 autorizados, trava diaria de 5%."""
    return settings.model_copy(
        update={
            "risk": settings.risk.model_copy(
                update={"authorized_capital": Decimal("29.29")}
            )
        }
    )


# ----------------------------------------------------------------------
# BRECHA 1: trade com data no futuro entra no incremento para sempre
# ----------------------------------------------------------------------
async def test_lancamento_com_data_no_futuro_infla_o_realizado_a_cada_retrato(settings):
    """Digitar 2027 em vez de 2026 no lancamento manual nao pode virar dinheiro.

    `ManualTradeIn` (api/schemas.py:100) aceita qualquer `executed_at`, sem teto.
    Com o corte por timestamp, esse trade e "posterior" a toda ancora futura:
    o mesmo lucro de 3 e somado a cada retrato, 1.440 vezes por dia.

    A ancora no futuro foi tratada (portfolio.py:292); o TRADE no futuro, nao.
    """
    futuro = datetime.now(UTC) + timedelta(days=365)
    await registrar(settings, Side.BUY, "1", "10", futuro, origem=TradeOrigin.MANUAL,
                    modo="manual")
    await registrar(settings, Side.SELL, "1", "13", futuro + timedelta(minutes=1),
                    origem=TradeOrigin.MANUAL, modo="manual")

    agent = agente(settings, carteira("13"), {"BTC": "13"})
    primeiro = await agent.build_snapshot()
    assert primeiro.realized_pnl == Decimal("3")

    for volta in range(1, 4):
        seguinte = await agente(settings, carteira("13"), {"BTC": "13"}).build_snapshot()
        assert seguinte.realized_pnl == Decimal("3"), (
            f"retrato {volta}: o mesmo lucro somado de novo -> {seguinte.realized_pnl}"
        )


async def test_relogio_corrigido_para_tras_nao_recontabiliza_o_lucro(settings):
    """NTP puxa o relogio uma hora para tras; os trades da hora viram 'futuro'.

    A ancora de antes do salto e descartada (esta no futuro, e isso o codigo ja
    trata). O que nao e tratado: as ancoras NOVAS nascem uma hora atras dos
    trades ja executados, entao cada retrato soma o realizado deles outra vez.
    """
    agora = datetime.now(UTC)
    # Trades preenchidos "na hora seguinte" ao relogio atual: e o que o salto
    # para tras produz sem que ninguem digite nada errado.
    await registrar(settings, Side.BUY, "1", "10", agora + timedelta(minutes=20))
    await registrar(settings, Side.SELL, "1", "13", agora + timedelta(minutes=25))

    primeiro = await agente(settings, carteira("13"), {"BTC": "13"}).build_snapshot()
    assert primeiro.realized_pnl == Decimal("3")
    segundo = await agente(settings, carteira("13"), {"BTC": "13"}).build_snapshot()

    assert segundo.realized_pnl == Decimal("3"), (
        f"lucro recontado apos o salto do relogio: {segundo.realized_pnl}"
    )


async def test_realizado_inflado_desarma_a_trava_diante_de_prejuizo_real(producao):
    """A consequencia da brecha 1: a protecao NAO AGE onde deveria.

    Um lancamento manual datado errado (ano digitado no futuro) com lucro de 1
    soma 1 ao realizado A CADA retrato. Trinta retratos depois -- meia hora de
    apuracao a cada minuto -- o resultado de negociacao publicado esta 30 acima
    do verdadeiro, e a queda em relacao a referencia do dia ficou negativa.

    Ai o sistema perde 9,20 de verdade (31% dos 29,29 autorizados, seis vezes o
    limite de 5%) e a trava nao dispara.
    """
    from sqlalchemy import update

    from crypto_traders.db import models as orm

    agora = datetime.now(UTC)
    inicio_do_dia = agora.replace(hour=0, minute=0, second=1, microsecond=0)
    bus = InMemoryEventBus()
    await bus.start()
    risco = RiskManagerAgent(bus, producao)

    # Referencia do dia: 29,29 em caixa, nenhum resultado.
    primeiro = await agente(producao, carteira("29.29"), {"BTC": "10"}).build_snapshot()
    async with session_scope(producao) as session:
        await session.execute(
            update(orm.PortfolioSnapshot)
            .where(orm.PortfolioSnapshot.id == primeiro.id)
            .values(timestamp=inicio_do_dia)
        )

    # O sistema opera e PERDE de verdade: comprou a 10, vendeu a 0,80.
    await registrar(producao, Side.BUY, "1", "10", agora - timedelta(hours=6))
    await registrar(producao, Side.SELL, "1", "0.80", agora - timedelta(hours=5))

    # E existe um lancamento manual com o ano digitado errado: lucro de 1.
    futuro = agora + timedelta(days=365)
    await registrar(producao, Side.BUY, "1", "9", futuro, origem=TradeOrigin.MANUAL,
                    modo="manual")
    await registrar(producao, Side.SELL, "1", "10", futuro + timedelta(minutes=1),
                    origem=TradeOrigin.MANUAL, modo="manual")

    for _ in range(30):
        ultimo = await agente(
            producao, carteira("20.09"), {"BTC": "0.80"}
        ).build_snapshot()

    motivo = await risco.check_circuit_breaker(ultimo)
    assert motivo is not None, (
        "trava NAO disparou com 9,20 de prejuizo realizado (31% de 29,29); "
        f"resultado de negociacao publicado = {ultimo.realized_pnl} "
        f"(o verdadeiro e -8.20)"
    )


# ----------------------------------------------------------------------
# BRECHA 2: lancamento manual retroativo nunca entra no realizado
# ----------------------------------------------------------------------
async def test_credito_da_reconciliacao_e_corrigido_pelo_trade_verdadeiro(producao):
    """O credito marca a mercado; o trade real tem OUTRO preco, e ninguem corrige.

    A venda preencheu a 8. O retrato seguinte ve o BTC fora do saldo e credita a
    marcacao a mercado do INSTANTE DO RETRATO -- o preco ja caiu para 4, entao
    ele credita -6 onde o prejuizo verdadeiro e -2. Quando a linha em `trades`
    finalmente aparece (ou o usuario a digita), ela e anterior a ancora: nunca
    entra no incremento, e o -6 errado fica para sempre.

    Com os processos morrendo sem traceback ao suspender a maquina (achado 3 do
    ensaio), a distancia entre o preenchimento e o retrato seguinte nao e de um
    minuto: e de quanto tempo a maquina passou dormindo.
    """
    agora = datetime.now(UTC)

    await registrar(producao, Side.BUY, "1", "10", agora - timedelta(days=1))
    com_posicao = await agente(
        producao, carteira("19.29", BTC="1"), {"BTC": "10"}
    ).build_snapshot()
    assert com_posicao.realized_pnl == Decimal("0")

    # Preencheu a 8; quando o retrato roda, o preco ja esta em 4.
    em_voo = await agente(producao, carteira("27.29"), {"BTC": "4"}).build_snapshot()

    # A linha do trade aparece (gravada pelo Execution, ou digitada pelo usuario).
    await registrar(producao, Side.SELL, "1", "8", agora - timedelta(minutes=2))
    depois = await agente(producao, carteira("27.29"), {"BTC": "4"}).build_snapshot()

    assert depois.realized_pnl == Decimal("-2"), (
        "o prejuizo publicado nao e o do trade verdadeiro (1 * (8 - 10) = -2); "
        f"ficou a marcacao a mercado do retrato: {depois.realized_pnl} "
        f"(em voo publicou {em_voo.realized_pnl})"
    )


async def test_venda_manual_retroativa_sem_reconciliacao_previa(producao):
    """O mesmo, sem a reconciliacao para disfarcar: o numero fica em zero.

    Aqui o saldo NUNCA registrou o ativo (a venda no app aconteceu antes do
    primeiro retrato deste ativo), entao nao ha o que reconciliar. O historico
    diz -6 de prejuizo; o publicado diz 0, para sempre.
    """
    agora = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    await registrar(producao, Side.BUY, "1", "10", agora - timedelta(days=2))

    # Retratos correndo com o fantasma (historico diz 1 BTC, exchange nao tem).
    primeiro = await agente(producao, carteira("23.29"), {"BTC": "4"}).build_snapshot()
    assert primeiro.realized_pnl == Decimal("0")

    await registrar(
        producao,
        Side.SELL,
        "1",
        "4",
        agora - timedelta(days=1),
        origem=TradeOrigin.MANUAL,
        modo="manual",
    )
    depois = await agente(producao, carteira("23.29"), {"BTC": "4"}).build_snapshot()

    assert depois.realized_pnl == Decimal("-6"), (
        f"prejuizo manual retroativo ignorado para sempre: {depois.realized_pnl}"
    )


# ----------------------------------------------------------------------
# BRECHA 3: o credito da reconciliacao nao e desfeito quando o ativo volta
# ----------------------------------------------------------------------
async def test_ativo_que_sai_e_volta_nao_conta_o_resultado_duas_vezes(producao):
    """Carteira fria, Binance Earn, transferencia entre carteiras: sai e volta.

    Sair do saldo spot credita a marcacao a mercado no realizado (uma vez, por
    desenho). Quando o ativo VOLTA, a ancora seguinte nao tem mais a quantidade,
    entao nada e estornado -- e o nao realizado volta junto com o ativo. O
    resultado de negociacao passa a contar o mesmo ganho duas vezes, e cada ida
    e volta soma outra.

    A trava promete imunidade a deposito e saque
    (`risk_manager.check_circuit_breaker`, docstring): esta e a porta por onde
    saque e deposito voltam a mexer no numero dela.
    """
    agora = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    await registrar(producao, Side.BUY, "1", "10", agora - timedelta(days=1))

    com_ativo = await agente(
        producao, carteira("19.29", BTC="1"), {"BTC": "13"}
    ).build_snapshot()
    assert com_ativo.realized_pnl == Decimal("0")
    assert com_ativo.unrealized_pnl == Decimal("3")

    # Transferido para fora do spot: o saldo perde o BTC.
    fora = await agente(producao, carteira("19.29"), {"BTC": "13"}).build_snapshot()
    assert fora.realized_pnl + fora.unrealized_pnl == Decimal("3")

    # Devolvido ao spot: o mesmo BTC, o mesmo preco, o mesmo custo.
    voltou = await agente(
        producao, carteira("19.29", BTC="1"), {"BTC": "13"}
    ).build_snapshot()

    assert voltou.realized_pnl + voltou.unrealized_pnl == Decimal("3"), (
        "resultado de negociacao contado duas vezes por uma ida e volta: "
        f"realizado {voltou.realized_pnl} + nao realizado {voltou.unrealized_pnl}"
    )


async def test_idas_e_voltas_nao_desarmam_a_trava(producao):
    """A consequencia da brecha 3: a protecao NAO AGE apos varias idas e voltas.

    Cada ida e volta do BTC entre o spot e a carteira fria (ou o Binance Earn)
    soma 3 de ganho inexistente ao resultado de negociacao. Dez idas e voltas
    somam 30. Depois disso o sistema perde 9,20 de verdade -- 31% do capital
    autorizado, seis vezes o limite diario -- e a trava nao dispara, porque o
    numero inflado ainda esta acima da referencia do dia.
    """
    from sqlalchemy import update

    from crypto_traders.db import models as orm

    agora = datetime.now(UTC)
    inicio_do_dia = agora.replace(hour=0, minute=0, second=1, microsecond=0)
    bus = InMemoryEventBus()
    await bus.start()
    risco = RiskManagerAgent(bus, producao)

    await registrar(producao, Side.BUY, "1", "10", agora - timedelta(days=1))
    referencia = await agente(
        producao, carteira("19.29", BTC="1"), {"BTC": "13"}
    ).build_snapshot()
    assert referencia.realized_pnl + referencia.unrealized_pnl == Decimal("3")
    async with session_scope(producao) as session:
        await session.execute(
            update(orm.PortfolioSnapshot)
            .where(orm.PortfolioSnapshot.id == referencia.id)
            .values(timestamp=inicio_do_dia)
        )

    for _ in range(10):
        await agente(producao, carteira("19.29"), {"BTC": "13"}).build_snapshot()
        await agente(
            producao, carteira("19.29", BTC="1"), {"BTC": "13"}
        ).build_snapshot()

    # Prejuizo de verdade: vendido a 0,80 o que custou 10.
    await registrar(producao, Side.SELL, "1", "0.80", agora - timedelta(minutes=5))
    final = await agente(producao, carteira("20.09"), {"BTC": "0.80"}).build_snapshot()

    motivo = await risco.check_circuit_breaker(final)
    assert motivo is not None, (
        "trava NAO disparou com 9,20 de prejuizo realizado (31% de 29,29); "
        f"resultado publicado = {final.realized_pnl + final.unrealized_pnl} "
        "(o verdadeiro e -9.20)"
    )
