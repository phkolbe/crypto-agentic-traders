"""Market Data Agent.

Busca candles e ticker das exchanges, normaliza, persiste e publica no bus.

Recebe uma `MarketDataSource` construida **sem credenciais**: dados de mercado
sao publicos, e nao ha motivo para este agente ter poder de gastar dinheiro.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ..bus import EventBus, Topics
from ..config import Settings
from ..db.repositories import AgentRunRepository, CandleRepository
from ..db.session import session_scope
from ..discovery import DEFAULT_EXCLUDED_ASSETS, DiscoveryCriteria, DiscoveryResult, discover
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
        on_universe_change: Callable[[DiscoveryResult], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(bus)
        self._source = source
        self._settings = settings
        self._last_published: dict[str, object] = {}
        """Ultimo `open_time` publicado por simbolo, para nao reprocessar candles."""

        self.latest_prices: dict[str, Decimal] = {}
        """Preco corrente por ativo base, consumido pelo Risk e pelo Portfolio."""

        self._on_universe_change = on_universe_change
        self._discovered: list[str] = []
        self._last_discovery: datetime | None = None
        self.discovery: DiscoveryResult | None = None

    # ------------------------------------------------------------------
    # Universo de pares observados
    # ------------------------------------------------------------------
    @property
    def active_symbols(self) -> list[str]:
        """Pares realmente observados agora.

        Com `SYMBOLS` preenchido e a lista do `.env`; vazio, e o resultado da
        ultima descoberta. Nunca "nenhum": subir sem observar mercado nenhum e o
        estado que mais engana, porque tudo parece saudavel.
        """
        if self._settings.trading.symbols:
            return list(self._settings.trading.symbols)
        return list(self._discovered)

    @property
    def discovery_enabled(self) -> bool:
        return self._settings.trading.discovery_enabled

    def _criteria(self) -> DiscoveryCriteria:
        extra = {asset.upper() for asset in self._settings.trading.discovery_exclude_assets}
        return DiscoveryCriteria(
            quote_currency=self._settings.trading.quote_currency,
            min_quote_volume_24h=self._settings.trading.discovery_min_quote_volume_24h,
            max_symbols=self._settings.trading.discovery_max_symbols,
            exclude_assets=DEFAULT_EXCLUDED_ASSETS | extra,
        )

    async def discover_symbols(self, force: bool = False) -> list[str]:
        """Varre a exchange e atualiza o universo de pares.

        Nao faz nada quando `SYMBOLS` esta preenchido: configuracao explicita
        sempre vence descoberta automatica.
        """
        if not self.discovery_enabled:
            return self.active_symbols

        now = datetime.now(UTC)
        intervalo = timedelta(hours=self._settings.trading.discovery_refresh_hours)
        due = (
            force or self._last_discovery is None or now - self._last_discovery >= intervalo
        )
        if not due:
            return self.active_symbols

        try:
            result = await discover(self._source, self._criteria())
        except Exception as exc:
            # Falha na varredura nao pode derrubar a coleta: seguimos com o
            # universo anterior, que ainda e melhor do que nenhum mercado.
            self.log.error("market_data.discovery_failed", error=str(exc))
            return self.active_symbols

        self._last_discovery = now
        previous = set(self._discovered)
        self._discovered = result.symbols
        self.discovery = result

        if not result.symbols:
            self.log.error(
                "market_data.discovery_empty",
                min_volume=str(self._settings.trading.discovery_min_quote_volume_24h),
                detail="nenhum par atingiu o piso de liquidez; "
                "reveja DISCOVERY_MIN_QUOTE_VOLUME_24H",
            )
        elif set(result.symbols) != previous and self._on_universe_change is not None:
            await self._on_universe_change(result)

        return self.active_symbols

    async def _run(self) -> None:
        while True:
            await self.wait_if_paused()
            await self.discover_symbols()
            await self.refresh()
            await self.heartbeat(detail=f"{len(self.latest_prices)} precos")
            if not await self.sleep(self._settings.trading.market_data_interval_seconds):
                return

    async def refresh(self) -> None:
        """Um ciclo de coleta. Publico para o orquestrador poder aquecer o sistema."""
        for symbol in self.active_symbols:
            try:
                await self._fetch_symbol(symbol)
            except Exception as exc:
                # Falha em um par nao pode impedir a coleta dos demais: um
                # simbolo deslistado travaria o sistema inteiro.
                self.log.warning("market_data.symbol_failed", symbol=symbol, error=str(exc))

    async def _fetch_symbol(self, symbol: str) -> None:
        candles = await self._source.fetch_candles(
            symbol, self._settings.trading.timeframe, self._settings.trading.candle_history_limit
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
                self._settings.trading.timeframe,
                limit or self._settings.trading.candle_history_limit,
            )

    async def on_stop(self) -> None:
        await self._source.close()
