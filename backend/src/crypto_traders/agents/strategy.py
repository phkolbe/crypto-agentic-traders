"""Strategy / Signal Agent.

Reage a candles fechados, roda as estrategias configuradas e publica `Signal`.

Este agente **nunca** executa ordens e nem sequer conhece o broker. Ele responde
apenas "para onde e com quanta convicção"; tamanho, stop e take-profit sao
decisao exclusiva do Risk Manager.
"""

from __future__ import annotations

from ..bus import EventBus, Topics
from ..config import Settings
from ..db.repositories import CandleRepository, SignalRepository
from ..db.session import session_scope
from ..domain.models import Candle
from ..strategies import MarketFrame, Strategy
from .base import BaseAgent


class StrategyAgent(BaseAgent):
    name = "strategy"

    def __init__(self, bus: EventBus, strategies: list[Strategy], settings: Settings) -> None:
        super().__init__(bus)
        self._strategies = strategies
        self._settings = settings
        self._required_candles = max(
            (s.min_candles for s in strategies), default=50
        )

    @property
    def strategy_names(self) -> list[str]:
        return [s.name for s in self._strategies]

    def replace_strategies(self, strategies: list[Strategy]) -> None:
        """Troca as estrategias ativas sem reiniciar o agente.

        Alterar a lista pela interface precisa valer no proximo candle, nao no
        proximo restart -- se exigisse restart, a tela mostraria uma
        configuracao que o sistema nao esta usando, que e o modo de falha que
        esta separacao de ambiente e negocio existe para eliminar.
        """
        self._strategies = strategies
        self._required_candles = max((s.min_candles for s in strategies), default=50)
        self.log.info("strategy.replaced", estrategias=self.strategy_names)

    async def _run(self) -> None:
        async for candle in self.bus.subscribe(Topics.CANDLES):
            await self.wait_if_paused()
            try:
                await self._on_candle(candle)
            except Exception as exc:
                # Um erro em uma estrategia nao pode encerrar a assinatura do
                # bus -- isso silenciaria todos os sinais dali em diante.
                self.log.exception("strategy.evaluation_failed", error=str(exc))
            await self.heartbeat()

    async def _on_candle(self, candle: Candle) -> None:
        if not candle.closed:
            return

        async with session_scope(self._settings) as session:
            candles = await CandleRepository(session).recent(
                str(candle.exchange),
                candle.symbol,
                candle.timeframe,
                max(self._required_candles + 10, self._settings.trading.candle_history_limit),
            )

        if len(candles) < self._required_candles:
            self.log.debug(
                "strategy.warmup",
                symbol=candle.symbol,
                have=len(candles),
                need=self._required_candles,
            )
            return

        market = MarketFrame.from_candles(candles)
        signals = []
        for strategy in self._strategies:
            try:
                signal = strategy.evaluate(market)
            except Exception as exc:
                self.log.exception(
                    "strategy.failed", strategy=strategy.name, symbol=candle.symbol, error=str(exc)
                )
                continue
            if signal is not None:
                signals.append(signal)

        if not signals:
            return

        # Persistimos antes de publicar: um sinal que chega ao Risk Manager e
        # some do banco por causa de um crash quebraria a auditoria.
        async with session_scope(self._settings) as session:
            repository = SignalRepository(session)
            for signal in signals:
                await repository.save(signal)

        for signal in signals:
            self.log.info(
                "strategy.signal",
                strategy=signal.strategy,
                symbol=signal.symbol,
                direction=str(signal.direction),
                confidence=round(signal.confidence, 3),
                reason=signal.reason,
            )
            await self.bus.publish(Topics.SIGNALS, signal)
