"""Market Data Agent.

Busca candles e ticker das exchanges, normaliza, persiste e publica no bus.

Recebe uma `MarketDataSource` construida **sem credenciais**: dados de mercado
sao publicos, e nao ha motivo para este agente ter poder de gastar dinheiro.
"""

from __future__ import annotations

from decimal import Decimal

from ..bus import EventBus, Topics
from ..config import Settings
from ..db.repositories import AgentRunRepository, CandleRepository
from ..db.session import session_scope
from ..domain.models import Candle
from ..exchanges.base import MarketDataSource
from .base import BaseAgent


class MarketDataAgent(BaseAgent):
    name = "market_data"

    def __init__(
        self,
        bus: EventBus,
        source: MarketDataSource,
        settings: Settings,
    ) -> None:
        super().__init__(bus)
        self._source = source
        self._settings = settings
        self._last_published: dict[str, object] = {}
        """Ultimo `open_time` publicado por simbolo, para nao reprocessar candles."""

        self.latest_prices: dict[str, Decimal] = {}
        """Preco corrente por ativo base, consumido pelo Risk e pelo Portfolio."""

    async def _run(self) -> None:
        while True:
            await self.wait_if_paused()
            await self._cycle()
            await self.heartbeat(detail=f"{len(self.latest_prices)} precos")
            if not await self.sleep(self._settings.market_data_interval_seconds):
                return

    async def _cycle(self) -> None:
        for symbol in self._settings.symbols:
            try:
                await self._fetch_symbol(symbol)
            except Exception as exc:
                # Falha em um par nao pode impedir a coleta dos demais: um
                # simbolo deslistado travaria o sistema inteiro.
                self.log.warning("market_data.symbol_failed", symbol=symbol, error=str(exc))

    async def _fetch_symbol(self, symbol: str) -> None:
        candles = await self._source.fetch_candles(
            symbol, self._settings.timeframe, self._settings.candle_history_limit
        )
        if not candles:
            return

        closed = [candle for candle in candles if candle.closed]
        if not closed:
            return

        async with session_scope(self._settings) as session:
            stored = await CandleRepository(session).upsert_many(closed)
            await AgentRunRepository(session).heartbeat(
                self.name, str(self.state), f"{symbol}: {stored} candles novos"
            )

        latest = closed[-1]
        base = symbol.partition("/")[0]
        self.latest_prices[base] = latest.close

        # Publicamos apenas quando ha um candle fechado inedito. Republicar o
        # mesmo candle a cada ciclo faria a estrategia reavaliar -- e, com
        # cooldown vencido, reemitir -- o mesmo sinal indefinidamente.
        if self._last_published.get(symbol) != latest.open_time:
            self._last_published[symbol] = latest.open_time
            await self.bus.publish(Topics.CANDLES, latest)
            self.log.info(
                "market_data.candle_published",
                symbol=symbol,
                open_time=latest.open_time.isoformat(),
                close=str(latest.close),
            )

    async def history(self, symbol: str, limit: int | None = None) -> list[Candle]:
        """Janela historica do banco, usada pelo Strategy Agent e pelo backtest."""
        async with session_scope(self._settings) as session:
            return await CandleRepository(session).recent(
                self._settings.exchange,
                symbol,
                self._settings.timeframe,
                limit or self._settings.candle_history_limit,
            )

    async def on_stop(self) -> None:
        await self._source.close()
