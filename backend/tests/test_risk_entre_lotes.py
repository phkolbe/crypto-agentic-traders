"""Caixa e posicoes entre lotes: o que ja foi aprovado e ainda nao esta no retrato.

O defeito (auditoria de 09/11/2026, P0 #6): a simulacao de caixa e vagas
consumidas (`_apply_fill`) so valia DENTRO de um lote. Com a janela de lote em 0
(o padrao), cada sinal e um lote de um, e todos os sinais entre dois retratos do
portfolio (60s) eram avaliados contra o mesmo caixa, as mesmas posicoes e a mesma
contagem. No fechamento diario saem varios LONG em segundos -- e cada um via o
caixa inteiro.

Cenario medido na auditoria, com a configuracao de producao: 29,29 USDC
autorizados, 4 posicoes abertas (23,40 aplicados), 50 USDC depositados e nao
autorizados. Tres LONG de pares diferentes: cada um via `usable_cash` = 5,89 e
os tres eram aprovados -- 17,6 gastos, exposicao total 41,0, +40% acima do
autorizado, e a Binance aceitava porque o caixa real existia.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_traders.agents.risk_manager import RiskManagerAgent
from crypto_traders.bus import InMemoryEventBus, Topics
from crypto_traders.config import RiskSettings
from crypto_traders.db.repositories import OrderRepository
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import ExchangeName, OrderStatus, SignalDirection
from crypto_traders.domain.models import PortfolioSnapshot, Position, Signal

AUTORIZADO = Decimal("29.29")
ABERTAS = ["ADA", "XRP", "DOT", "LINK"]
NOVOS = ["SOL", "AVAX", "BNB"]


def limites(**kw) -> RiskSettings:
    base = dict(
        max_order_notional=None,
        max_order_pct_portfolio=0.20,
        max_asset_exposure_pct=0.20,
        max_open_positions=None,
        min_order_notional=Decimal("5"),
        stop_loss_pct=0.03,
        take_profit_pct=0.06,
        daily_loss_limit_pct=0.05,
        weekly_loss_limit_pct=0.12,
        min_signal_confidence=0.55,
        cooldown_seconds=900,
        authorized_capital=AUTORIZADO,
        asset_whitelist=[*ABERTAS, *NOVOS, "USDT"],
        symbol_whitelist=[f"{a}/USDT" for a in (*ABERTAS, *NOVOS)],
    )
    base.update(kw)
    return RiskSettings(**base)


async def agente(settings, **kw) -> tuple[RiskManagerAgent, list]:
    bus = InMemoryEventBus()
    await bus.start()
    rm = RiskManagerAgent(bus, settings.model_copy(update={"risk": limites(**kw)}))
    pedidos: list = []

    async def escuta():
        async for pedido in bus.subscribe(Topics.ORDER_REQUESTS):
            pedidos.append(pedido)

    rm._escuta = asyncio.create_task(escuta())  # type: ignore[attr-defined]
    await asyncio.sleep(0)
    return rm, pedidos


def retrato(caixa: str = "55.89", quando: datetime | None = None, extra=None) -> PortfolioSnapshot:
    """4 posicoes de 5,85 (23,40 aplicados) e o caixa com o deposito nao autorizado."""
    posicoes = [
        Position(
            exchange=ExchangeName.BINANCE, asset=a, quantity=Decimal("5.85"),
            average_price=Decimal(1), current_price=Decimal(1),
        )
        for a in ABERTAS
    ]
    for asset, quantidade in (extra or {}).items():
        posicoes.append(
            Position(
                exchange=ExchangeName.BINANCE, asset=asset, quantity=Decimal(quantidade),
                average_price=Decimal(1), current_price=Decimal(1),
            )
        )
    aplicado = sum((p.quantity for p in posicoes), start=Decimal(0))
    posicoes.append(
        Position(
            exchange=ExchangeName.BINANCE, asset="USDT", quantity=Decimal(caixa),
            current_price=Decimal(1),
        )
    )
    return PortfolioSnapshot(
        total_value=aplicado + Decimal(caixa),
        cash_value=Decimal(caixa),
        positions_value=aplicado,
        positions=posicoes,
        timestamp=quando or datetime.now(UTC),
    )


def sinal(asset: str, confianca: float = 0.8) -> Signal:
    return Signal(
        exchange=ExchangeName.BINANCE,
        symbol=f"{asset}/USDT",
        timeframe="1d",
        strategy="ma_crossover",
        direction=SignalDirection.LONG,
        confidence=confianca,
        reason="fechamento diario",
        reference_price=Decimal(1),
    )


async def tres_longs(rm: RiskManagerAgent) -> None:
    # Janela 0: cada sinal e o seu proprio lote, como em producao.
    for asset in NOVOS:
        await rm._on_batch([sinal(asset)])
    await asyncio.sleep(0.05)


class TestOCapitalAutorizadoValeEntreLotes:
    async def test_tres_longs_no_mesmo_fechamento_respeitam_o_autorizado(self, settings):
        rm, pedidos = await agente(settings)
        rm.observe_snapshot(retrato())
        await tres_longs(rm)

        gasto = sum((p.notional for p in pedidos), start=Decimal(0))
        aplicado = Decimal("23.40")
        # Antes: tres aprovacoes de 5,85 (17,6 gastos, exposicao 41,0 = +40%).
        assert len(pedidos) == 1
        assert aplicado + gasto <= AUTORIZADO

    async def test_o_limite_de_posicoes_tambem_vale_entre_lotes(self, settings):
        """Sem deposito e sem portao: so a contagem de vagas decide."""
        rm, pedidos = await agente(
            settings, authorized_capital=None, max_open_positions=5,
        )
        rm.observe_snapshot(retrato(caixa="100"))
        await tres_longs(rm)
        # 4 abertas + 1 vaga. Antes: as tres passavam e a carteira ia a 7.
        assert len(pedidos) == 1

    async def test_dois_longs_do_mesmo_par_antes_do_retrato_param_no_cooldown(self, settings):
        """Duas estrategias, mesmo candle, mesmo par: o segundo nao dobra a posicao.

        O cooldown dependia da linha PENDING que o Execution grava em paralelo;
        sem ela no banco (ou antes dela), o segundo passava.
        """
        rm, pedidos = await agente(settings, authorized_capital=None)
        rm.observe_snapshot(retrato(caixa="100"))
        await rm._on_batch([sinal("SOL")])
        await rm._on_batch([sinal("SOL", 0.9)])
        await asyncio.sleep(0.05)
        assert len(pedidos) == 1


class TestAReservaCai:
    """A reserva so existe enquanto o retrato nao mostra a ordem.

    Reservar para sempre trocaria um furo por outro: o capital autorizado ficaria
    preso em ordens que ja terminaram, e o sistema pararia de operar em silencio.
    """

    async def _gravar(self, settings, pedido, status, preenchido, quando):
        async with session_scope(settings) as session:
            ordem = await OrderRepository(session).create_pending(pedido, "dry_run")
            ordem.status = str(status)
            ordem.filled_quantity = Decimal(preenchido)
            await session.flush()
            ordem.updated_at = quando

    async def test_ordem_recusada_libera_o_caixa_na_hora(self, settings):
        rm, pedidos = await agente(settings)
        rm.observe_snapshot(retrato())
        await rm._on_batch([sinal("SOL")])
        await asyncio.sleep(0.05)
        await self._gravar(
            settings, pedidos[0], OrderStatus.REJECTED, "0", datetime.now(UTC)
        )
        await rm._on_batch([sinal("AVAX")])
        await asyncio.sleep(0.05)
        assert [p.symbol for p in pedidos] == ["SOL/USDT", "AVAX/USDT"]

    async def test_ordem_preenchida_sai_da_reserva_quando_o_retrato_a_mostra(self, settings):
        rm, pedidos = await agente(settings, authorized_capital=None, max_open_positions=6)
        inicio = datetime.now(UTC)
        rm.observe_snapshot(retrato(caixa="100", quando=inicio))
        await rm._on_batch([sinal("SOL")])
        await asyncio.sleep(0.05)
        await self._gravar(
            settings, pedidos[0], OrderStatus.FILLED, str(pedidos[0].quantity),
            inicio + timedelta(seconds=5),
        )
        assert len(rm._reservas) == 1

        # Um retrato DEPOIS do preenchimento ainda pode ter lido o saldo antes
        # dele (o carimbo e do fim da montagem): a reserva continua.
        rm.observe_snapshot(retrato(caixa="100", quando=inicio + timedelta(seconds=60)))
        await rm._on_batch([sinal("AVAX")])
        assert "SOL/USDT" in {r.symbol for r in rm._reservas.values()}

        # O seguinte foi montado inteiro depois: ja mostra o SOL comprado.
        rm.observe_snapshot(
            retrato(
                caixa=str(Decimal(100) - pedidos[0].notional),
                quando=inicio + timedelta(seconds=120),
                extra={"SOL": str(pedidos[0].quantity)},
            )
        )
        await rm._on_batch([sinal("BNB")])
        assert "SOL/USDT" not in {r.symbol for r in rm._reservas.values()}

    async def test_pedido_que_nunca_virou_ordem_libera_depois_do_prazo(self, settings):
        """Recusa por filtro de mercado nao grava linha em `orders`."""
        rm, pedidos = await agente(settings)
        rm.observe_snapshot(retrato())
        await rm._on_batch([sinal("SOL")])
        await asyncio.sleep(0.05)
        reserva = next(iter(rm._reservas.values()))
        rm._reservas[reserva.client_order_id] = reserva.__class__(
            **{**reserva.__dict__, "emitted_at": reserva.emitted_at - timedelta(minutes=3)}
        )
        await rm._on_batch([sinal("AVAX")])
        await asyncio.sleep(0.05)
        assert [p.symbol for p in pedidos] == ["SOL/USDT", "AVAX/USDT"]

    async def test_ordem_sem_desfecho_continua_reservada(self, settings):
        """Desfecho desconhecido pode ter gasto o dinheiro: nao se libera no escuro."""
        rm, pedidos = await agente(settings)
        rm.observe_snapshot(retrato())
        await rm._on_batch([sinal("SOL")])
        await asyncio.sleep(0.05)
        await self._gravar(
            settings, pedidos[0], OrderStatus.PENDING, "0", datetime.now(UTC)
        )
        rm.observe_snapshot(retrato(quando=datetime.now(UTC) + timedelta(minutes=5)))
        rm.observe_snapshot(retrato(quando=datetime.now(UTC) + timedelta(minutes=6)))
        await rm._on_batch([sinal("AVAX")])
        await asyncio.sleep(0.05)
        assert len(pedidos) == 1
