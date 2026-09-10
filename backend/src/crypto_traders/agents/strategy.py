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
        self._fetch_candles = self._greediest_warmup(strategies)
        self._muted: dict[str, tuple[str, ...]] = {}
        """Ultima combinacao de estrategias mudas por aquecimento, por simbolo.

        Existe so para nao repetir a mesma linha de log a cada candle. O
        projeto ja pagou por log repetitivo: o watchdog alertou 1.440 vezes por
        dia sobre o mesmo agente ocioso e o ruido virou cegueira."""

    @staticmethod
    def _greediest_warmup(strategies: list[Strategy]) -> int:
        """Quantos candles buscar: o suficiente para a estrategia mais exigente.

        Isto dimensiona a BUSCA, e nao mais a decisao. Antes, o mesmo maximo
        tambem era a porta de entrada: o agente saia sem avaliar NENHUMA
        estrategia quando o simbolo tinha menos candles que o maximo, e so
        registrava em `log.debug` -- invisivel com `LOG_LEVEL=INFO`. Habilitar
        `macd_trend` pela interface eleva a exigencia de 26 para 105 candles;
        em 1d, um par recem-descoberto ficava meses sem nenhuma estrategia,
        inclusive as que ja tinham aquecimento sobrando, e nada no log em nivel
        operacional dizia isso. E a mesma familia de defeito do reindex
        assimetrico: o sistema segue verde operando com menos estrategias do
        que as configuradas.
        """
        return max((s.min_candles for s in strategies), default=50)

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
        self._fetch_candles = self._greediest_warmup(strategies)
        self._muted.clear()  # a proxima recusa por aquecimento volta a ser dita
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
                max(self._fetch_candles + 10, self._settings.trading.candle_history_limit),
            )

        # Cada estrategia e julgada pelo PROPRIO aquecimento. Uma exigente nao
        # pode calar as outras: `ma_crossover` precisa de 26 candles e nao tem
        # por que emudecer porque `macd_trend`, habilitada ao lado, precisa de
        # 105. (A propria `evaluate` reconfere via `_has_warmup`; aqui o ponto e
        # nao descartar o simbolo inteiro, e dizer quem ficou de fora.)
        prontas = [s for s in self._strategies if len(candles) >= s.min_candles]
        mudas = tuple(s.name for s in self._strategies if len(candles) < s.min_candles)
        if mudas and self._muted.get(candle.symbol) != mudas:
            # Nivel INFO, nao DEBUG: "o sistema esta operando com menos
            # estrategias do que as configuradas" e informacao operacional, e em
            # DEBUG ela nao aparece com o `LOG_LEVEL` de producao.
            self.log.info(
                "strategy.warmup_incompleto",
                symbol=candle.symbol,
                timeframe=candle.timeframe,
                candles=len(candles),
                mudas=list(mudas),
                avaliando=[s.name for s in prontas],
                motivo="candles insuficientes para o aquecimento declarado",
            )
        self._muted[candle.symbol] = mudas
        if not prontas:
            return

        try:
            market = MarketFrame.from_candles(candles)
        except ValueError as exc:
            # Janela recusada pelo dado (hoje: `open_time` repetido). Recusar e
            # o comportamento certo, mas recusa calada faz o agente parecer
            # ocioso -- e o nivel tem que ser visivel com `LOG_LEVEL=INFO`.
            self.log.warning(
                "strategy.janela_recusada",
                symbol=candle.symbol,
                timeframe=candle.timeframe,
                candles=len(candles),
                motivo=str(exc),
            )
            return

        signals = []
        for strategy in prontas:
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
