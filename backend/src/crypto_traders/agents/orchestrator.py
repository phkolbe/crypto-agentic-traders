"""Orquestrador.

Monta os agentes, gerencia o ciclo de vida coletivo, vigia heartbeats e expoe o
controle usado pela API (pausar, retomar, ajustar limites, rearmar o circuit
breaker).

Nao contem logica de trading -- e a camada fina que faz as pecas conversarem.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from ..bus import EventBus, Topics, build_event_bus
from ..business_config import install_business_config, update_trading_config
from ..config import Settings, TradingSettings, get_settings
from ..db.repositories import AuditLogRepository
from ..db.session import init_db, session_scope
from ..discovery import DiscoveryResult
from ..domain.enums import AgentState, TradingMode
from ..domain.models import PortfolioSnapshot
from ..exchanges import build_broker, build_market_data_source
from ..exchanges.base import Broker, MarketDataSource
from ..logging_setup import get_logger
from ..notifications import Notifier, build_notifier
from ..onchain import OnChainProvider
from ..risk.rules import SizingFeasibility, assess_sizing_feasibility
from ..strategies import build_strategies
from .base import BaseAgent
from .execution import ExecutionAgent
from .market_data import MarketDataAgent
from .portfolio import PortfolioAgent
from .risk_manager import RiskManagerAgent
from .strategy import StrategyAgent

log = get_logger(__name__)

#: Sem heartbeat por mais que isso, o agente e considerado travado.
HEARTBEAT_TIMEOUT = timedelta(minutes=10)

#: Intervalo da vigilancia de saude.
WATCHDOG_INTERVAL_SECONDS = 60

#: Com que frequencia o loop tenta atualizar a serie on-chain. O provedor tem seu
#: proprio piso de 12h (`onchain.REFRESH_INTERVAL`), entao acordar de hora em hora
#: e barato: quase sempre nao faz nada, e cobre o caso de a busca ter falhado.
ONCHAIN_REFRESH_INTERVAL_SECONDS = 3600


class Orchestrator:
    """Dono do ciclo de vida de todos os agentes."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.bus: EventBus = build_event_bus(self.settings)
        self.notifier: Notifier = build_notifier(self.settings)

        self._source: MarketDataSource | None = None
        self._broker: Broker | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._alert_listener: asyncio.Task[None] | None = None
        self._onchain_refresher: asyncio.Task[None] | None = None
        self.started_at: datetime | None = None
        self.sizing: SizingFeasibility | None = None
        self._sizing_alerted = False
        """Alerta de dimensionamento sai uma vez por transicao, nao a cada snapshot."""

        self.market_data: MarketDataAgent
        self.strategy: StrategyAgent
        self.risk_manager: RiskManagerAgent
        self.execution: ExecutionAgent
        self.portfolio: PortfolioAgent

    # ------------------------------------------------------------------
    @property
    def agents(self) -> dict[str, BaseAgent]:
        return {
            agent.name: agent
            for agent in (
                self.market_data,
                self.strategy,
                self.risk_manager,
                self.execution,
                self.portfolio,
            )
        }

    async def start(self) -> None:
        await init_db(self.settings)

        # Negocio antes de qualquer agente: `build_broker` e `build_strategies`
        # abaixo dependem dele. Sem isso os agentes subiriam com os padroes de
        # codigo e passariam a operar diferente do que a interface mostra.
        await install_business_config(self.settings)

        await self.bus.start()

        self._source = build_market_data_source(
            self.settings.exchange, testnet=self.settings.trading_mode is TradingMode.TESTNET
        )
        self._broker = build_broker(self.settings)

        self.market_data = MarketDataAgent(
            self.bus, self._source, self.settings, on_universe_change=self._on_universe_change
        )
        self.strategy = StrategyAgent(
            self.bus, build_strategies(self.settings.trading.strategies), self.settings
        )
        self.onchain = OnChainProvider(self.settings)
        self.risk_manager = RiskManagerAgent(self.bus, self.settings, onchain=self.onchain)
        self.execution = ExecutionAgent(self.bus, self._broker, self.settings)
        self.portfolio = PortfolioAgent(
            self.bus,
            self._broker,
            self.settings,
            price_source=lambda: dict(self.market_data.latest_prices),
            on_snapshot=self._on_snapshot,
        )

        if self.settings.is_live:
            log.warning(
                "orchestrator.live_trading",
                exchange=self.settings.exchange,
                message="ORDENS SERAO ENVIADAS COM DINHEIRO REAL",
            )

        # Consumidores antes dos produtores: o Market Data Agent so pode publicar
        # depois que Strategy, Risk e Execution ja estao inscritos no bus, senao
        # os primeiros candles caem no vazio.
        for agent in (self.execution, self.risk_manager, self.strategy):
            await agent.start()
        await asyncio.sleep(0)

        await self.market_data.start()

        # Primeiro snapshot antes de liberar o Portfolio Agent no ciclo normal:
        # sem ele o Risk Manager rejeita tudo por falta de retrato do portfolio.
        await self._prime_portfolio()
        await self.portfolio.start()

        self._alert_listener = asyncio.create_task(self._listen_alerts(), name="alert-listener")
        self._watchdog = asyncio.create_task(self._watch_health(), name="watchdog")
        self._onchain_refresher = asyncio.create_task(
            self._refresh_onchain_forever(), name="onchain-refresher"
        )
        self.started_at = datetime.now(UTC)

        async with session_scope(self.settings) as session:
            await AuditLogRepository(session).append(
                action="system_started",
                target="orchestrator",
                after={
                    "mode": str(self.settings.trading_mode),
                    "exchange": self.settings.exchange,
                    # Universo real, ja resolvido pela descoberta: registrar a
                    # config estatica gravaria uma lista vazia em "mar aberto".
                    "symbols": self.market_data.active_symbols,
                    "symbols_source": (
                        "descoberta" if self.settings.trading.discovery_enabled else "configurado"
                    ),
                    "strategies": self.settings.trading.strategies,
                },
            )
        log.info("orchestrator.started", mode=str(self.settings.trading_mode))

    async def _prime_portfolio(self) -> None:
        """Alimenta precos e produz o snapshot inicial."""
        try:
            # Descoberta ANTES da primeira coleta: sem universo definido, o
            # refresh nao teria par nenhum para buscar e o sistema subiria cego.
            #
            # Serie on-chain antes do primeiro sinal: sem ela o filtro de regime
            # nao se aplica, e isso precisa ser escolha, nao acidente. Buscamos
            # SEMPRE, mesmo com o filtro desligado, por dois motivos concretos:
            # o limiar vigente pode vir do banco (alterado pela interface) e nao
            # do `.env`, entao condicionar a busca ao `.env` deixaria o filtro
            # ligado e inerte; e ter a leitura disponivel permite exibi-la e
            # liga-la pela interface sem reiniciar o processo.
            await self.onchain.refresh_mvrv(force=True)
            await self.market_data.discover_symbols(force=True)
            await self.market_data.refresh()
            self._sync_paper_prices()
            snapshot = await self.portfolio.build_snapshot()
            self.risk_manager.observe_snapshot(snapshot)
        except Exception as exc:
            # Sem snapshot inicial o sistema sobe mesmo assim: o Risk Manager
            # simplesmente rejeita sinais ate o primeiro ciclo do Portfolio Agent.
            log.warning("orchestrator.portfolio_priming_failed", error=str(exc))

    async def stop(self) -> None:
        for task in (self._watchdog, self._alert_listener, self._onchain_refresher):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._watchdog = self._alert_listener = self._onchain_refresher = None

        for agent in self.agents.values():
            with contextlib.suppress(Exception):
                await agent.stop()

        # O broker e compartilhado entre Execution e Portfolio, entao quem o
        # criou e quem o fecha -- uma unica vez.
        for resource in (self._broker, self._source):
            if resource is not None:
                with contextlib.suppress(Exception):
                    await resource.close()

        await self.bus.stop()
        log.info("orchestrator.stopped")

    # ------------------------------------------------------------------
    # Controle (usado pela API)
    # ------------------------------------------------------------------
    async def pause_agent(self, name: str, actor: str = "user") -> bool:
        agent = self.agents.get(name)
        if agent is None:
            return False
        agent.pause()
        await self._audit("agent_paused", name, actor)
        return True

    async def resume_agent(self, name: str, actor: str = "user") -> bool:
        agent = self.agents.get(name)
        if agent is None:
            return False
        agent.resume()
        await self._audit("agent_resumed", name, actor)
        return True

    async def pause_all(self, actor: str = "system", reason: str | None = None) -> None:
        """Para a tomada de decisao, mantendo a coleta de dados viva.

        O Market Data Agent segue rodando de proposito: sem preco atualizado o
        dashboard congela e o circuit breaker perde a referencia para saber
        quando seria seguro voltar.
        """
        for name in ("strategy", "risk_manager", "execution"):
            self.agents[name].pause()
        await self._audit("all_agents_paused", "system", actor, reason)
        log.warning("orchestrator.paused_all", reason=reason)

    async def resume_all(self, actor: str = "user") -> None:
        for agent in self.agents.values():
            agent.resume()
        await self._audit("all_agents_resumed", "system", actor)

    async def health(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        agents = {}
        for name, agent in self.agents.items():
            stale = agent.last_beat is not None and (now - agent.last_beat) > HEARTBEAT_TIMEOUT
            agents[name] = {
                "state": str(agent.state),
                "running": agent.is_running,
                "paused": agent.is_paused,
                "last_heartbeat": agent.last_beat.isoformat() if agent.last_beat else None,
                "stale": stale,
                "last_error": agent.last_error,
            }
        return {
            "mode": str(self.settings.trading_mode),
            "exchange": self.settings.exchange,
            "symbols": self.market_data.active_symbols,
            "symbols_source": "descoberta" if self.market_data.discovery_enabled else "configurado",
            "strategies": self.strategy.strategy_names,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "circuit_breaker_active": self.risk_manager.circuit_breaker_active,
            "sizing_feasible": self.sizing.feasible if self.sizing else True,
            "sizing_detail": (
                self.sizing.explain(self.settings.trading.quote_currency) if self.sizing else None
            ),
            "agents": agents,
        }

    # ------------------------------------------------------------------
    async def _on_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        """Cada snapshot alimenta o Risk Manager e reavalia o circuit breaker."""
        self.risk_manager.observe_snapshot(snapshot)
        self._sync_paper_prices()

        # Protecao antes de qualquer outra coisa: se uma posicao rompeu o stop,
        # fechar vem primeiro. Falhar aqui nao pode impedir o circuit breaker de
        # ser reavaliado logo abaixo -- sao duas defesas independentes, e perder
        # as duas por um erro em uma delas seria o pior desfecho.
        try:
            await self.risk_manager.enforce_protective_exits(snapshot)
        except Exception as exc:
            log.error("orchestrator.protective_exit_failed", error=str(exc))

        await self._check_sizing(snapshot)

        reason = await self.risk_manager.check_circuit_breaker(snapshot)
        if reason:
            # O aviso ao operador sai por `_listen_alerts`, que e o unico caminho
            # de notificacao: o Risk Manager ja publicou o alerta ao disparar a
            # trava. Enviar aqui tambem geraria duas mensagens do mesmo evento.
            await self.pause_all(actor="circuit_breaker", reason=reason)

    async def update_trading(self, changes: dict, *, actor: str = "user") -> TradingSettings:
        """Aplica alteracoes de negocio vindas da interface, ja valendo.

        Gravar no banco nao basta: tres coisas dependem de configuracao lida
        **na construcao** dos agentes, e nao a cada uso. Sem reagir aqui, a tela
        mostraria uma configuracao que o sistema nao esta usando -- exatamente a
        divergencia silenciosa que motivou separar ambiente de negocio.
        """
        anterior = self.settings.trading
        novo = await update_trading_config(self.settings, changes, actor=actor)
        if novo == anterior:
            return novo

        # As estrategias sao instanciadas uma vez, na construcao do agente.
        if novo.strategies != anterior.strategies:
            self.strategy.replace_strategies(build_strategies(novo.strategies))

        # A descoberta so revarre no seu proprio intervalo (24h por padrao):
        # mudar o criterio e esperar um dia para ele valer nao seria aceitavel.
        # Universo explicito nao precisa disto -- `active_symbols` le a lista
        # corrente a cada ciclo de coleta.
        criterios = ("quote_currency", "discovery_max_symbols",
                     "discovery_min_quote_volume_24h", "discovery_exclude_assets")
        mudou_criterio = any(
            getattr(novo, campo) != getattr(anterior, campo) for campo in criterios
        )
        if novo.discovery_enabled and (mudou_criterio or not anterior.discovery_enabled):
            await self.market_data.discover_symbols(force=True)

        return novo

    async def _on_universe_change(self, result: DiscoveryResult) -> None:
        """A descoberta define o que o Risk Manager passa a aceitar.

        Sem esta ligacao, os pares descobertos seriam coletados mas rejeitados
        um a um por estarem fora da whitelist estatica -- o sistema pareceria
        funcionar e nunca operaria.
        """
        await self.risk_manager.apply_discovered_universe(result.symbols, result.assets)
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "universe_discovered",
                "title": "Universo de negociacao atualizado",
                "message": (
                    f"Pares selecionados por liquidez: {', '.join(result.symbols)}\n"
                    f"({result.considered} pares avaliados, "
                    f"{result.rejected_low_volume} abaixo do piso de volume)"
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def _check_sizing(self, snapshot: PortfolioSnapshot) -> None:
        """Avisa quando patrimonio e limites tornam qualquer ordem impossivel.

        Sem isso o sistema fica no pior estado que existe: de pe, com heartbeat
        verde e dashboard atualizando, rejeitando todo sinal em silencio -- e o
        motivo enterrado numa mensagem tecnica em `risk_events`.
        """
        result = assess_sizing_feasibility(self.risk_manager.limits, snapshot.total_value)
        self.sizing = result

        if result.feasible:
            if self._sizing_alerted:
                self._sizing_alerted = False
                log.info(
                    "orchestrator.sizing_ok",
                    detail=result.explain(self.settings.trading.quote_currency),
                )
            return

        if self._sizing_alerted:
            return

        self._sizing_alerted = True
        explanation = result.explain(self.settings.trading.quote_currency)
        log.error("orchestrator.sizing_infeasible", detail=explanation)
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "sizing_infeasible",
                "title": "Nenhuma ordem e possivel com o patrimonio atual",
                "message": (
                    explanation + "\n\n"
                    "O sistema vai continuar coletando dados e gerando sinais, mas "
                    "TODOS serao rejeitados. Aumente o patrimonio ou ajuste os "
                    "limites de risco na interface."
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    def _sync_paper_prices(self) -> None:
        """Mantem o PaperBroker com precos de mercado reais.

        Em dry_run o broker simulado nao tem de onde tirar preco sozinho; sem
        isso ele recusaria toda ordem a mercado por falta de referencia.
        Brokers reais ignoram esta chamada.
        """
        setter = getattr(self._broker, "set_prices", None)
        if setter is not None:
            setter(dict(self.market_data.latest_prices))

    async def _listen_alerts(self) -> None:
        """Caminho único de notificação: tudo que é crítico passa por aqui.

        Os agentes só publicam em `Topics.ALERTS` e não conhecem o notificador.
        Assim existe um lugar só que decide o que chega ao operador, e um alerta
        novo (como o de acesso negado) passa a ser notificado sem que o agente
        precise saber que Telegram existe.
        """
        async for alert in self.bus.subscribe(Topics.ALERTS):
            log.warning("orchestrator.alert", **alert)
            title = alert.get("title") or alert.get("type", "Alerta")
            message = alert.get("message") or alert.get("reason") or ""
            try:
                await self.notifier.send(title, message)
            except Exception as exc:
                # Falha ao notificar nunca pode derrubar o loop de alertas: o
                # evento já está no log, que é a fonte de verdade da auditoria.
                log.error("orchestrator.alert_delivery_failed", error=str(exc))

    async def _refresh_onchain_forever(self) -> None:
        """Mantem a serie MVRV atualizada enquanto o sistema roda.

        Sem isto a serie congelava no dia do start: o filtro de regime seguiria
        respondendo com o percentil daquele dia por semanas, e um mercado que
        esquentou depois passaria batido. Um dado velho que parece atual e pior
        que dado nenhum, porque o `None` ao menos desliga o filtro de forma
        visivel no log.
        """
        while True:
            await asyncio.sleep(ONCHAIN_REFRESH_INTERVAL_SECONDS)
            try:
                await self.onchain.refresh_mvrv()
            except Exception as exc:
                # Provedor externo instavel nao derruba o refresher: na proxima
                # volta tenta de novo, e o filtro nao depende disto para operar.
                log.warning("orchestrator.onchain_refresh_failed", error=str(exc))

    async def _watch_health(self) -> None:
        """Reinicia agentes que morreram ou pararam de dar sinal de vida.

        Um agente morto em silencio e o pior cenario: o sistema parece saudavel
        enquanto uma etapa da cadeia deixou de existir.
        """
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            now = datetime.now(UTC)
            for name, agent in self.agents.items():
                crashed = agent.state is AgentState.ERROR or not agent.is_running
                stale = (
                    agent.last_beat is not None
                    and (now - agent.last_beat) > HEARTBEAT_TIMEOUT
                    and not agent.is_paused
                )
                if crashed or stale:
                    log.error(
                        "orchestrator.restarting_agent",
                        agent=name,
                        crashed=crashed,
                        stale=stale,
                        error=agent.last_error,
                    )
                    await self.bus.publish(
                        Topics.ALERTS,
                        {
                            "type": "agent_restarted",
                            "title": f"Agente '{name}' reiniciado",
                            "message": (
                                f"motivo: {'falha' if crashed else 'heartbeat perdido'}\n"
                                f"ultimo erro: {agent.last_error or 'nenhum'}"
                            ),
                            "timestamp": now.isoformat(),
                        },
                    )
                    with contextlib.suppress(Exception):
                        await agent.stop()
                    await agent.start()

    async def _audit(
        self, action: str, target: str, actor: str, detail: str | None = None
    ) -> None:
        async with session_scope(self.settings) as session:
            await AuditLogRepository(session).append(
                action=action, actor=actor, target=target, detail=detail
            )
