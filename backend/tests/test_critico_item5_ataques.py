"""Ataques do dono do projeto contra o item 5, no que alcanca este agente."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update

from crypto_traders.agents.portfolio import PortfolioAgent
from crypto_traders.agents.risk_manager import RiskManagerAgent
from crypto_traders.bus import InMemoryEventBus
from crypto_traders.db import models as orm
from crypto_traders.db.repositories import TradeRepository
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import Side, TradeOrigin
from crypto_traders.exchanges.paper import PaperBroker


async def registrar(settings, side, quantidade, preco, quando, origem=TradeOrigin.AGENT):
    async with session_scope(settings) as session:
        return await TradeRepository(session).record(
            executed_at=quando,
            exchange="binance",
            symbol="BTC/USDT",
            side=str(side),
            quantity=Decimal(quantidade),
            price=Decimal(preco),
            fee=Decimal(0),
            fee_currency="USDT",
            origin=origem,
            mode="dry_run" if origem is TradeOrigin.AGENT else "manual",
        )


def agente(settings, broker, precos):
    fonte = {a: Decimal(v) for a, v in precos.items()}
    return PortfolioAgent(
        InMemoryEventBus(), broker, settings, price_source=lambda: dict(fonte)
    )


def carteira(caixa, **ativos):
    broker = PaperBroker(initial_balance=Decimal(caixa))
    for asset, quantidade in ativos.items():
        broker.credit(asset, Decimal(quantidade))
    return broker


async def datar(settings, snapshot, quando):
    async with session_scope(settings) as session:
        await session.execute(
            update(orm.PortfolioSnapshot)
            .where(orm.PortfolioSnapshot.id == snapshot.id)
            .values(timestamp=quando)
        )


@pytest.fixture
def producao(settings):
    return settings.model_copy(
        update={
            "risk": settings.risk.model_copy(
                update={"authorized_capital": Decimal("29.29")}
            )
        }
    )


async def test_ataque_3_ordem_em_voo_derruba_o_sistema(producao):
    """ATAQUE 3: circuit breaker com uma ordem EM VOO.

    A venda preencheu na exchange (o saldo de BTC ja saiu, o caixa ja entrou),
    mas a linha em `trades` ainda nao foi gravada -- `execution.py` grava depois
    de `apply_result`, e entre o preenchimento e o commit ha uma janela; um
    crash nessa janela a torna PERMANENTE.

    Nessa janela o nao realizado cai a zero e o realizado ainda e zero: o
    resultado de negociacao desaba, e a trava le isso como prejuizo do dia --
    exatamente os '10.24%' que o implementador diz ter corrigido.
    """
    agora = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    bus = InMemoryEventBus()
    await bus.start()
    risco = RiskManagerAgent(bus, producao)

    await registrar(producao, Side.BUY, "1", "10", agora - timedelta(days=1))
    primeiro = await agente(producao, carteira("19", BTC="1"), {"BTC": "13"}).build_snapshot()
    assert primeiro.unrealized_pnl == Decimal("3")
    await datar(producao, primeiro, agora.replace(hour=0, minute=1))

    # Preencheu na exchange; a linha em `trades` ainda nao existe.
    em_voo = (
        await agente(producao, carteira("32"), {"BTC": "13"}).build_snapshot()
    ).model_copy(update={"timestamp": agora})

    motivo = await risco.check_circuit_breaker(em_voo)
    assert motivo is None, f"trava disparou com ordem em voo, sem perda: {motivo}"
    assert not risco.circuit_breaker_active


async def test_apagar_lancamento_manual_dispara_a_trava(producao):
    """Corrigir um lancamento digitado errado nao pode armar a trava.

    `DELETE /trades/{id}` existe e e oferecido na interface. Com o realizado
    acumulado saindo do historico, apagar uma VENDA manual lucrativa derruba o
    resultado de negociacao no meio do dia -- e a trava le a correcao de digito
    como prejuizo, exigindo rearme manual.
    """
    agora = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    bus = InMemoryEventBus()
    await bus.start()
    risco = RiskManagerAgent(bus, producao)

    await registrar(producao, Side.BUY, "1", "10", agora - timedelta(days=2))
    venda = await registrar(
        producao, Side.SELL, "1", "13", agora - timedelta(days=1), TradeOrigin.MANUAL
    )
    venda_id = venda.id

    primeiro = await agente(producao, carteira("32"), {"BTC": "13"}).build_snapshot()
    assert primeiro.realized_pnl == Decimal("3")
    await datar(producao, primeiro, agora.replace(hour=0, minute=1))

    # O usuario percebe que digitou a venda errada e apaga o lancamento.
    async with session_scope(producao) as session:
        trade = await TradeRepository(session).get(venda_id)
        await session.delete(trade)

    depois = (
        await agente(producao, carteira("32"), {"BTC": "13"}).build_snapshot()
    ).model_copy(update={"timestamp": agora})

    motivo = await risco.check_circuit_breaker(depois)
    assert motivo is None, f"trava disparou por apagar um lancamento manual: {motivo}"
