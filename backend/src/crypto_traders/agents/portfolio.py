"""Portfolio / Reporting Agent.

Apura saldos, posicoes, PnL e grava snapshots da serie temporal que alimenta o
grafico do dashboard e a avaliacao do circuit breaker.

O preco medio de entrada nao vem da exchange (spot nao expoe isso): e
reconstruido a partir do historico de `trades`, o que tambem cobre os
lancamentos manuais. Assim uma compra feita pelo app da Binance e digitada na
interface entra corretamente no custo medio.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

from ..bus import EventBus, Topics
from ..config import Settings
from ..db.repositories import AgentRunRepository, PortfolioSnapshotRepository, TradeRepository
from ..db.session import session_scope
from ..domain.enums import Side
from ..domain.models import PortfolioSnapshot, Position
from ..exchanges.base import Broker
from .base import BaseAgent


class PortfolioAgent(BaseAgent):
    name = "portfolio"

    def __init__(
        self,
        bus: EventBus,
        broker: Broker,
        settings: Settings,
        price_source: Callable[[], dict[str, Decimal]],
        on_snapshot: Callable[[PortfolioSnapshot], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(bus)
        self._broker = broker
        self._settings = settings
        self._price_source = price_source
        """Precos correntes, fornecidos pelo Market Data Agent."""

        self._on_snapshot = on_snapshot
        self.latest: PortfolioSnapshot | None = None

    async def _run(self) -> None:
        while True:
            await self.wait_if_paused()
            try:
                await self.build_snapshot()
            except Exception as exc:
                self.log.exception("portfolio.snapshot_failed", error=str(exc))
            await self.heartbeat()
            if not await self.sleep(self._settings.portfolio_interval_seconds):
                return

    async def build_snapshot(self) -> PortfolioSnapshot:
        quote = self._settings.quote_currency
        prices = self._price_source()
        positions = await self._broker.fetch_positions(prices)

        cash = Decimal(0)
        positions_value = Decimal(0)
        priced: list[Position] = []
        unpriced: list[str] = []

        cost_basis = await self._average_costs()

        for position in positions:
            if position.asset == quote:
                cash += position.quantity
                priced.append(position.model_copy(update={"current_price": Decimal(1)}))
                continue

            price = position.current_price or prices.get(position.asset)
            if price is None:
                # Sem preco, o ativo nao entra no total. Chutar um valor
                # inflaria o patrimonio e poderia desarmar o circuit breaker
                # justamente quando ele deveria disparar.
                unpriced.append(position.asset)
                priced.append(position)
                continue

            value = position.quantity * price
            positions_value += value
            priced.append(
                position.model_copy(
                    update={
                        "current_price": price,
                        "average_price": cost_basis.get(position.asset),
                    }
                )
            )

        if unpriced:
            self.log.warning("portfolio.unpriced_assets", assets=unpriced)

        total = cash + positions_value
        unrealized = sum(
            (p.unrealized_pnl for p in priced if p.asset != quote), start=Decimal(0)
        )

        async with session_scope(self._settings) as session:
            realized = await TradeRepository(session).realized_pnl_total()

        snapshot = PortfolioSnapshot(
            total_value=total,
            cash_value=cash,
            positions_value=positions_value,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            allocations=_allocations(priced, total, quote),
            positions=priced,
        )

        async with session_scope(self._settings) as session:
            await PortfolioSnapshotRepository(session).save(
                snapshot, str(self._settings.trading_mode)
            )
            await AgentRunRepository(session).heartbeat(
                self.name, str(self.state), f"patrimonio {total:.2f} {quote}"
            )

        self.latest = snapshot
        await self.bus.publish(Topics.PORTFOLIO_SNAPSHOTS, snapshot)
        if self._on_snapshot is not None:
            await self._on_snapshot(snapshot)

        self.log.info(
            "portfolio.snapshot",
            total=str(round(total, 2)),
            cash=str(round(cash, 2)),
            positions=len([p for p in priced if p.asset != quote]),
            realized_pnl=str(round(realized, 2)),
        )
        return snapshot

    async def _average_costs(self) -> dict[str, Decimal]:
        """Custo medio por ativo, reconstruido do historico de trades.

        Percorre em ordem cronologica aplicando custo medio movel: compras somam
        ao custo, vendas reduzem proporcionalmente. Inclui os lancamentos
        manuais, que sao trades como quaisquer outros na tabela.
        """
        async with session_scope(self._settings) as session:
            trades = await TradeRepository(session).list(limit=10_000)

        quantities: dict[str, Decimal] = {}
        costs: dict[str, Decimal] = {}

        for trade in sorted(trades, key=lambda t: t.executed_at):
            asset = trade.symbol.partition("/")[0]
            quantity = Decimal(str(trade.quantity))
            price = Decimal(str(trade.price))
            fee = Decimal(str(trade.fee or 0))

            held = quantities.get(asset, Decimal(0))
            cost = costs.get(asset, Decimal(0))

            if trade.side == str(Side.BUY):
                quantities[asset] = held + quantity
                costs[asset] = cost + quantity * price + fee
            else:
                if held > 0:
                    average = cost / held
                    sold = min(quantity, held)
                    quantities[asset] = held - sold
                    costs[asset] = cost - sold * average
                else:
                    # Venda sem posicao registrada: acontece quando o sistema
                    # comeca a operar com saldo preexistente na exchange.
                    quantities[asset] = Decimal(0)
                    costs[asset] = Decimal(0)

        return {
            asset: costs[asset] / quantity
            for asset, quantity in quantities.items()
            if quantity > 0 and costs.get(asset, Decimal(0)) > 0
        }


def _allocations(
    positions: list[Position], total: Decimal, quote: str
) -> dict[str, float]:
    """Percentual do patrimonio por ativo, para o grafico de pizza."""
    if total <= 0:
        return {}
    result: dict[str, float] = {}
    for position in positions:
        value = position.quantity if position.asset == quote else position.market_value
        if value > 0:
            result[position.asset] = float(value / total)
    return result
