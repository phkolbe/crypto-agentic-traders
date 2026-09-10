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
from ..db.repositories import AuditLogRepository, RiskConfigRepository
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
#:
#: O prazo continua sendo 10 minutos de proposito. O ensaio de D25 mostrou o
#: watchdog reiniciando um agente ocioso, e a correcao nao foi afrouxar isto --
#: um timeout maior apenas atrasa o mesmo diagnostico errado e enfraquece a
#: deteccao de travamento real. O que mudou e o sinal: agente esperando bate
#: heartbeat porque esta vivo (`base.IDLE_PULSE_SECONDS`), nao porque recebeu
#: trabalho.
HEARTBEAT_TIMEOUT = timedelta(minutes=10)

#: Intervalo da vigilancia de saude.
WATCHDOG_INTERVAL_SECONDS = 60

#: Quantos reinicios DENTRO DA JANELA antes de desistir de um agente.
#:
#: Reiniciar em laco nao consertou nada em D25 -- produziu 17 alertas em 16
#: minutos, no ritmo de 1.440 por dia. Um agente que nao volta em tres
#: tentativas nao volta na quarta, e insistir esconde o problema no ruido.
MAX_RESTART_ATTEMPTS = 3

#: A janela em que os reinicios sao contados, como multiplo do timeout.
#:
#: Contar reinicios CONSECUTIVOS nao funciona na cadencia real. O watchdog roda
#: a cada 60s e o timeout e de 600s: logo depois de um reinicio o agente parece
#: saudavel por uns nove ciclos, porque `started_at` acabou de ser renovado.
#: Isso zerava a contagem toda vez -- medido: 13 reinicios, 13 alertas, e a
#: desistencia NUNCA armava. Extrapolando, ~144 alertas por dia para sempre, e a
#: recusa de operar (que e a protecao) nao acontecendo nunca.
#:
#: Com janela de tempo, tres reinicios em 30 minutos encerram o assunto,
#: independentemente de quantos ciclos "bons" existirem entre eles.
RESTART_WINDOW_FACTOR = 3

#: Quantos registros de auditoria olhar para achar o ultimo evento de ciclo de
#: vida do sistema. Sao registros raros (subida, parada, pausa, limites), entao
#: esta janela cobre varias execucoes.
LIFECYCLE_SCAN_LIMIT = 500

#: Espera maxima pelos consumidores se registrarem no bus antes de publicar o
#: primeiro candle.
SUBSCRIPTION_WAIT_SECONDS = 5.0

#: Com que frequencia o loop tenta atualizar a serie on-chain. O provedor tem seu
#: proprio piso de 12h (`onchain.REFRESH_INTERVAL`), entao acordar de hora em hora
#: e barato: quase sempre nao faz nada, e cobre o caso de a busca ter falhado.
ONCHAIN_REFRESH_INTERVAL_SECONDS = 3600

#: O que se perde enquanto cada tarefa do orquestrador esta morta. Dizer so
#: "tarefa morreu" nao ajuda quem opera; dizer o que deixou de funcionar, sim.
_OWN_TASK_CONSEQUENCE = {
    "watchdog": (
        "Enquanto ela esteve morta, agente travado NAO era detectado nem "
        "reiniciado: o sistema parecia saudavel com uma etapa da cadeia parada."
    ),
    "alert-listener": (
        "Enquanto ela esteve morta, NENHUM alerta do sistema chegou ate voce -- "
        "inclusive os de stop-loss e de circuit breaker. Se houve alerta nessa "
        "janela, ele esta apenas no log."
    ),
    "onchain-refresher": (
        "Enquanto ela esteve morta, a serie on-chain ficou congelada no ultimo "
        "valor buscado."
    ),
}


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

        self._restart_history: dict[str, list[datetime]] = {}
        """Quando cada agente foi reiniciado, dentro da janela corrente."""

        self._restart_alerted: set[str] = set()
        """Agentes que ja avisaram neste episodio -- um alerta por episodio (D19)."""

        self._given_up: set[str] = set()
        """Agentes que o watchdog parou de reiniciar. So o operador os traz de volta."""

        self._unprotected_alerted = False
        """Aviso de posicao sem stop-loss sai uma vez por episodio, nao por snapshot."""

        self._openings_blocked_reason: str | None = None
        """Motivo com que ESTE caminho travou a abertura por execucao indisponivel.

        Guardado para que a liberacao acerte apenas a propria trava. Soltar a
        abertura porque o agente voltou nunca pode desarmar o circuit breaker
        nem desfazer a pausa pedida pela interface -- seria o desarme por porta
        lateral que `_allow_openings_for` ja recusa, com outro nome. `None`
        significa "a trava corrente, se existe, nao e nossa".
        """

        self._openings_block_alerted = False
        """Aviso de "a abertura parou" sai uma vez por episodio (D19)."""

        self._unclean_previous_run: str | None = None
        """Subida anterior sem parada correspondente: o processo foi derrubado."""

        self._shutting_down = False
        """True so a partir de `stop()`. Fora dele, tarefa que termina e falha."""

        self._alert_path_alerted = False
        """Se o operador ja foi avisado de que o caminho de alertas caiu."""

        self.market_data: MarketDataAgent
        self.strategy: StrategyAgent
        self.risk_manager: RiskManagerAgent
        self.execution: ExecutionAgent
        self.portfolio: PortfolioAgent

    # ------------------------------------------------------------------
    #: Ordem de montagem dos agentes. Nomeada aqui para que `agents` funcione
    #: mesmo com o sistema meio montado.
    AGENT_ATTRIBUTES = ("market_data", "strategy", "risk_manager", "execution", "portfolio")

    @property
    def agents(self) -> dict[str, BaseAgent]:
        """Os agentes ja construidos.

        Tolera montagem incompleta de proposito: se `start()` falhar no meio, o
        `stop()` precisa liberar o que ja subiu em vez de estourar com
        AttributeError e deixar broker e conexao abertos.
        """
        built = (getattr(self, attribute, None) for attribute in self.AGENT_ATTRIBUTES)
        return {agent.name: agent for agent in built if agent is not None}

    async def start(self) -> None:
        await init_db(self.settings)

        # Antes de qualquer coisa: a execucao anterior desligou ou foi derrubada?
        # A resposta so existe agora, com o banco aberto e nada escrito ainda.
        self._unclean_previous_run = await self._detect_unclean_shutdown()

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
        consumers = (self.execution, self.risk_manager, self.strategy)
        for agent in consumers:
            await agent.start()
        await self._wait_for_subscriptions(consumers)

        await self.market_data.start()

        # Primeiro snapshot antes de liberar o Portfolio Agent no ciclo normal:
        # sem ele o Risk Manager rejeita tudo por falta de retrato do portfolio.
        await self._prime_portfolio()
        await self.portfolio.start()

        # Trava ativa no banco tem que valer desde a subida. O RiskEngine ja
        # rejeitava todo sinal novo, mas a recusa de ABERTURA na execucao mora
        # em memoria e voltava permissiva a cada reinicio do processo -- e o
        # padrao de fabrica de uma trava nunca pode ser "solta".
        await self._apply_persisted_circuit_breaker()

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

        # Depois do `_listen_alerts` existir, senao o alerta seria publicado sem
        # ninguem escutando e nao chegaria ao operador.
        await self._report_unclean_shutdown()

    async def _apply_persisted_circuit_breaker(self) -> None:
        """Repoe a parada geral se a trava ficou armada da execucao anterior.

        Le do banco em vez de confiar em `risk_manager.circuit_breaker_active`,
        que so e preenchido quando o laco do agente roda pela primeira vez: a
        subida nao pode depender de quem ganha essa corrida.
        """
        try:
            async with session_scope(self.settings) as session:
                config = await RiskConfigRepository(session).get_or_create(
                    self.settings.risk.model_dump(mode="json")
                )
                ativo = config.circuit_breaker_active
                motivo = config.circuit_breaker_reason
        except Exception as exc:
            log.error("orchestrator.circuit_breaker_state_unreadable", error=str(exc))
            return

        if not ativo:
            return
        log.warning("orchestrator.circuit_breaker_still_tripped", reason=motivo)
        await self.pause_all(
            actor="circuit_breaker",
            reason=motivo or "circuit breaker ainda armado da execucao anterior",
        )

    async def _wait_for_subscriptions(self, agents: tuple[BaseAgent, ...]) -> None:
        """Espera os consumidores estarem de fato inscritos antes de publicar.

        Antes aqui havia um `asyncio.sleep(0)`, que funcionava porque o laco do
        agente assinava direto e uma volta do event loop bastava. Com a caixa de
        entrada duravel existe uma tarefa a mais no caminho, e "dormir zero"
        passaria a ser aposta. Agora a espera termina quando a assinatura existe
        -- e, se nao existir, isso vira erro no log em vez de candle perdido.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SUBSCRIPTION_WAIT_SECONDS
        while loop.time() < deadline:
            missing = [agent.name for agent in agents if not agent.subscriptions_ready]
            if not missing:
                return
            await asyncio.sleep(0.005)
        log.error(
            "orchestrator.subscriptions_not_ready",
            agents=[agent.name for agent in agents if not agent.subscriptions_ready],
            detail="publicar agora pode perder os primeiros candles",
        )

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

    # ------------------------------------------------------------------
    # Sobrevivencia do processo (achado 3 de D25)
    # ------------------------------------------------------------------
    async def _detect_unclean_shutdown(self) -> str | None:
        """Olha se a execucao anterior desligou ou apenas parou de existir.

        O ensaio de D25 morreu junto com a maquina: sem traceback, com o log
        parando no meio, e nada na subida seguinte dizia isso. Um processo que
        morre em silencio e indistinguivel de um que nunca subiu.

        Manter a arvore de processos viva e trabalho de sistema operacional
        (Tarefa Agendada no Windows, `systemd` no Linux) e nao cabe aqui.
        Deixar a morte VISIVEL cabe: se o ultimo registro de ciclo de vida no
        `audit_log` e uma subida sem parada correspondente, a execucao anterior
        foi derrubada.

        Devolve o instante daquela subida, ou None. Nunca impede o sistema de
        subir -- e observacao, nao portao.
        """
        try:
            async with session_scope(self.settings) as session:
                records = await AuditLogRepository(session).list(limit=LIFECYCLE_SCAN_LIMIT)
        except Exception as exc:
            log.warning("orchestrator.lifecycle_scan_failed", error=str(exc))
            return None

        for record in records:  # mais recente primeiro
            if record.action == "system_stopped":
                return None
            if record.action == "system_started":
                return record.timestamp.isoformat() if record.timestamp else "desconhecido"
        return None

    async def _report_unclean_shutdown(self) -> None:
        if self._unclean_previous_run is None:
            return
        log.error(
            "orchestrator.previous_run_died",
            started_at=self._unclean_previous_run,
            detail="sem registro de parada: o processo anterior foi derrubado",
        )
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "unclean_shutdown",
                "title": "A execucao anterior nao desligou -- ela foi derrubada",
                "message": (
                    f"A subida de {self._unclean_previous_run} nao tem parada "
                    "correspondente no audit_log.\n\n"
                    "Causa tipica: suspensao ou logoff da maquina derrubando a arvore "
                    "de processos. Enquanto o processo nao rodar sob supervisao do "
                    "sistema operacional (Tarefa Agendada com 'executar mesmo sem "
                    "usuario conectado', ou servico), 'deixa rodando por alguns dias' "
                    "nao e uma instrucao que o sistema consegue cumprir."
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def _mark_clean_shutdown(self, reason: str) -> None:
        """Deixa no `audit_log` a prova de que este desligamento foi pedido.

        E a contraparte de `_detect_unclean_shutdown`: sem esta marca, todo
        desligamento normal seria denunciado como morte na proxima subida, e um
        alerta que chega sempre deixa de ser lido.
        """
        try:
            async with session_scope(self.settings) as session:
                await AuditLogRepository(session).append(
                    action="system_stopped",
                    target="orchestrator",
                    detail=reason,
                    after={
                        "mode": str(self.settings.trading_mode),
                        "started_at": self.started_at.isoformat() if self.started_at else None,
                    },
                )
        except Exception as exc:
            # Falhar aqui nao pode impedir o desligamento; o preco e um falso
            # "morreu derrubado" na proxima subida, que e o lado seguro do erro.
            log.warning("orchestrator.shutdown_mark_failed", error=str(exc))

    async def stop(self) -> None:
        # Antes de cancelar qualquer coisa: a partir daqui, tarefa que termina e
        # desligamento, e nao morte. Sem esta marca a supervisao ressuscitaria o
        # que o `stop()` acabou de derrubar.
        self._shutting_down = True
        await self._mark_clean_shutdown("desligamento solicitado")

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
        """Pausa um agente. Menos o de execucao -- ver `_block_openings_for`.

        Pausar o agente que fecha posicao desliga o stop-loss, e isso e D4 pelo
        avesso. A decisao ja estava tomada e medida em `DECISION_AGENTS` (a
        parada geral nunca pausa a execucao); o que faltava era `pause_agent`
        honra-la, porque `POST /agents/execution/pause` continua na tela.
        """
        agent = self.agents.get(name)
        if agent is None:
            return False
        if name == "execution":
            await self._block_openings_for(agent, actor)
            return True
        agent.pause()
        await self._audit("agent_paused", name, actor)
        return True

    async def _block_openings_for(self, agent: BaseAgent, actor: str) -> None:
        """Converte "pausar a execucao" em "parar de ABRIR posicao" (D4).

        Medido antes desta conversao, pausando pela propria rota da interface e
        entregando um snapshot com posicao 20% abaixo do preco medio: zero
        ordens enviadas, `_exiting` travado em {'BTC'} para sempre e o alerta ao
        dono dizendo "Stop-loss acionado ... Posicao fechada". O sistema
        afirmava ter protegido uma posicao que seguia aberta -- a mesma familia
        do stop-loss que nao disparava.

        O que o operador quer ao pausar a execucao e parar de operar, e e isso
        que ele recebe: nenhuma compra passa. O que ele nao recebe -- porque
        ninguem deveria poder pedir isso com posicao aberta -- e uma carteira
        sem stop-loss.
        """
        motivo = "pausa da execucao pedida pela interface (D4: fechar continua passando)"
        blocker = getattr(agent, "block_openings", None)
        if blocker is None:
            # Agente sem trava de abertura (dubles de teste). Pausar e o
            # comportamento antigo, entao pausa -- mas o snapshot seguinte cai
            # em `_execution_unavailable` e o operador e avisado em alto.
            agent.pause()
            log.error(
                "orchestrator.execution_paused_without_block",
                detail="agente de execucao sem block_openings: posicoes ficam sem stop-loss",
            )
            await self._audit("agent_paused", "execution", actor, motivo)
            return

        anterior = getattr(agent, "openings_blocked", None)
        # A trava armada por execucao indisponivel e transitoria e nossa: ela
        # cede o lugar ao pedido do operador, que e mais duravel e e o que ele
        # espera ver no dashboard. As outras, nao.
        nossa = anterior is not None and anterior == self._openings_blocked_reason
        ja_bloqueada = anterior is not None and not nossa
        if not ja_bloqueada:
            # Trava ja armada nao troca de motivo: se o circuit breaker a armou,
            # o motivo dele e o que importa no log e no dashboard -- reescrever
            # com "pedida pela interface" apagaria a causa de verdade.
            blocker(motivo)
        # A partir daqui a trava e do operador: a execucao voltar a agir nao a
        # solta, porque soltar apagaria um pedido explicito de parar de operar.
        self._openings_blocked_reason = None
        log.warning(
            "orchestrator.execution_pause_converted",
            actor=actor,
            detail="abertura bloqueada; o agente segue de pe para poder FECHAR posicao",
        )
        await self._audit("execution_openings_blocked", "execution", actor, motivo)
        if ja_bloqueada:
            # Clicar duas vezes nao gera dois alertas (D19).
            return
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "execution_pause_converted",
                "title": "Execucao: abertura bloqueada (o agente NAO foi pausado)",
                "message": (
                    "Voce pediu para pausar o agente de execucao. Ele continua de pe "
                    "de proposito: e o unico caminho por onde uma posicao e FECHADA, "
                    "e pausa-lo desligaria o stop-loss e o take-profit das posicoes "
                    "abertas (D4 -- bloquear a abertura nunca pode bloquear o "
                    "fechamento).\n\n"
                    "O que valeu: NENHUMA compra sera executada a partir de agora. "
                    "Vendas e saidas de protecao continuam passando.\n\n"
                    "Para liberar de novo, retome o agente de execucao pela interface."
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def resume_agent(self, name: str, actor: str = "user") -> bool:
        agent = self.agents.get(name)
        if agent is None:
            return False
        if name == "execution":
            await self._allow_openings_for(agent, actor)
        agent.resume()

        # Retomar um agente cuja tarefa morreu apenas liberava um gate vazio: o
        # dashboard passava a mostrar "running" e nada rodava. Se o watchdog
        # desistiu dele, e aqui que o operador o traz de volta.
        if not agent.is_running:
            self._given_up.discard(name)
            self._restart_history.pop(name, None)
            self._restart_alerted.discard(name)
            log.warning("orchestrator.reviving_agent", agent=name, actor=actor)
            await agent.restart()

        await self._audit("agent_resumed", name, actor)
        return True

    async def _allow_openings_for(self, agent: BaseAgent, actor: str) -> None:
        """Contraparte de `_block_openings_for`, com uma recusa importante.

        Retomar a execucao nao pode desarmar o circuit breaker por uma porta
        lateral: `resume_all` ja e recusado com a trava armada, e liberar a
        abertura aqui seria o mesmo desarme com outro nome.
        """
        unblocker = getattr(agent, "allow_openings", None)
        if unblocker is None:
            return
        if self._circuit_breaker_active():
            log.warning(
                "orchestrator.openings_stay_blocked",
                actor=actor,
                detail="circuit breaker armado; a abertura so volta com ele rearmado",
            )
            return
        unblocker()

    def _circuit_breaker_active(self) -> bool:
        """Tolera montagem incompleta: o atributo pode nem existir ainda."""
        risk = getattr(self, "risk_manager", None)
        return bool(getattr(risk, "circuit_breaker_active", False))

    #: Quem para numa parada geral. `execution` NAO esta aqui, e o motivo e D4.
    #:
    #: Pausar a execucao tambem parava o stop-loss: medido com posicao 20% abaixo
    #: do preco medio, a saida de protecao era emitida pelo Risk Manager e ficava
    #: na fila do agente de execucao, sem virar ordem, ate alguem retomar na
    #: mao. O circuit breaker dispara exatamente quando o mercado esta caindo --
    #: e o momento em que o stop MAIS precisa disparar --, entao pausar a
    #: execucao transformava a trava de protecao em amplificador de prejuizo.
    #:
    #: Pausar o laco do Risk Manager impede que sinal NOVO vire ordem, mas nao
    #: alcanca o pedido que ja foi publicado: a caixa de entrada duravel da
    #: execucao guarda esses pedidos de proposito, e o agente consome um por vez,
    #: com ida ao banco e a exchange. Medido com broker lento e sem pausar nada
    #: na mao: uma compra publicada ANTES da trava terminou preenchida DEPOIS
    #: dela. Por isso a trava tambem desce ate a execucao -- ver `pause_all`.
    DECISION_AGENTS = ("strategy", "risk_manager")

    async def pause_all(self, actor: str = "system", reason: str | None = None) -> None:
        """Para a tomada de decisao e a ABERTURA, mantendo coleta e protecao vivas.

        O Market Data Agent segue rodando de proposito: sem preco atualizado o
        dashboard congela e o circuit breaker perde a referencia para saber
        quando seria seguro voltar. E o Execution Agent segue rodando por uma
        razao mais importante -- ver `DECISION_AGENTS` acima: bloquear a
        abertura de posicao nunca pode bloquear o fechamento dela (D4).

        A escolha nao e entre bloquear tudo e bloquear nada: a execucao passa a
        recusar `Side.BUY` e continua aceitando `Side.SELL`, que e exatamente o
        que D4 pede.
        """
        agents = self.agents
        for name in self.DECISION_AGENTS:
            agent = agents.get(name)
            if agent is not None:
                agent.pause()

        execution = agents.get("execution")
        blocker = getattr(execution, "block_openings", None)
        if blocker is not None:
            blocker(reason or "operacao suspensa")

        await self._audit("all_agents_paused", "system", actor, reason)
        log.warning("orchestrator.paused_all", reason=reason)

    async def resume_all(self, actor: str = "user") -> None:
        agents = self.agents
        for agent in agents.values():
            agent.resume()

        execution = agents.get("execution")
        unblocker = getattr(execution, "allow_openings", None)
        if unblocker is not None:
            unblocker()

        await self._audit("all_agents_resumed", "system", actor)

    async def health(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        agents = {}
        for name, agent in self.agents.items():
            agents[name] = {
                "state": str(agent.state),
                "running": agent.is_running,
                "paused": agent.is_paused,
                "last_heartbeat": agent.last_beat.isoformat() if agent.last_beat else None,
                "stale": self._is_stale(agent, now),
                "last_error": agent.last_error,
                # "ocioso" e "travado" nao podem parecer a mesma coisa no
                # dashboard: era exatamente a confusao que o watchdog fazia.
                "idle": agent.is_idle,
                "idle_detail": agent.idle_detail or None,
                "pending_events": agent.pending_events,
                "lost_events": agent.lost_events,
                # Surdo e o estado que mais parece saudavel: de pe, sem erro e
                # sem eventos. Tem que ter nome proprio no dashboard.
                "deaf_topics": agent.deaf_topics,
                "inbox_failures": agent.inbox_failures,
                "restarts": agent.restarts,
                "given_up": name in self._given_up,
            }
            if name == "execution":
                # Pausar a execucao vira bloqueio de abertura (D4, ver
                # `_block_openings_for`), entao `paused` seria sempre False e o
                # operador nao teria como ver que a compra esta barrada.
                agents[name]["openings_blocked"] = getattr(agent, "openings_blocked", None)
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
            # As tarefas do orquestrador tambem morrem, e uma delas morta
            # (alertas) apaga o unico caminho de notificacao: precisa ser
            # visivel onde o operador olha, e nao so no log.
            "tasks": self._own_tasks_alive(),
        }

    # ------------------------------------------------------------------
    async def _on_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        """Cada snapshot alimenta o Risk Manager e reavalia o circuit breaker."""
        self.risk_manager.observe_snapshot(snapshot)
        self._sync_paper_prices()

        # Metade da supervisao mutua: este caminho e movido pelo Portfolio
        # Agent, entao e daqui que a morte do proprio watchdog e vista.
        #
        # Vem ANTES da protecao de proposito: se o listener de alertas estiver
        # morto, o aviso de "posicoes sem stop-loss" publicado logo abaixo nao
        # chegaria a ninguem. Ressuscitar o carteiro antes de mandar a carta.
        try:
            await self._supervise_own_tasks(datetime.now(UTC))
        except Exception as exc:  # pragma: no cover - defesa em profundidade
            log.error("orchestrator.own_task_supervision_failed", error=str(exc))

        # Protecao antes de qualquer outra coisa: se uma posicao rompeu o stop,
        # fechar vem primeiro. Falhar aqui nao pode impedir o circuit breaker de
        # ser reavaliado logo abaixo -- sao duas defesas independentes, e perder
        # as duas por um erro em uma delas seria o pior desfecho.
        impedimento = self._execution_unavailable()
        if impedimento is not None:
            # Quem nao consegue FECHAR nao pode continuar ABRINDO. Vem antes do
            # aviso de proposito, para que o texto dele diga a verdade sobre o
            # estado em que o sistema ja esta.
            await self._block_openings_while_unavailable(impedimento)
            # Emitir a saida de protecao para um agente que nao vai executa-la e
            # pior que nao emitir: o Risk Manager trava o ativo em `_exiting`
            # para nao vender duas vezes, e essa trava so cai quando o ativo sai
            # da carteira -- ou seja, nunca. O log ficaria com um
            # `risk.protective_exit` como se o stop tivesse agido, e a posicao
            # seguiria aberta e desprotegida em silencio.
            await self._warn_positions_unprotected(snapshot, impedimento)
        else:
            self._unprotected_alerted = False
            self._release_openings_after_recovery()
            try:
                # ATENCAO a quem le o log: `risk.protective_exit` diz que a saida
                # foi EMITIDA, nao que a posicao fechou. A confirmacao e a ordem
                # `filled` na tabela `orders`. Existe um caminho aberto em que as
                # duas coisas divergem -- a ordem de protecao recusada pela
                # exchange (IP fora da whitelist) nao solta `_exiting`, e o ativo
                # nao tem novo stop tentado. Correcao pertence ao Risk Manager
                # (`risk_manager.py:_exiting`), fora deste arquivo.
                await self.risk_manager.enforce_protective_exits(snapshot)
            except Exception as exc:
                log.error("orchestrator.protective_exit_failed", error=str(exc))

        # Saldo que entrou e ninguem autorizou: o sistema nao o usa, e avisar e
        # a unica forma de isso nao virar caixa ocioso silencioso.
        try:
            await self.risk_manager.check_capital_authorization(snapshot)
        except Exception as exc:
            log.error("orchestrator.capital_check_failed", error=str(exc))

        await self._check_sizing(snapshot)

        reason = await self.risk_manager.check_circuit_breaker(snapshot)
        if reason:
            # O aviso ao operador sai por `_listen_alerts`, que e o unico caminho
            # de notificacao: o Risk Manager ja publicou o alerta ao disparar a
            # trava. Enviar aqui tambem geraria duas mensagens do mesmo evento.
            await self.pause_all(actor="circuit_breaker", reason=reason)

    def _execution_unavailable(self) -> str | None:
        """Diz por que a execucao nao vai executar nada, ou None se ela vai.

        A pergunta e "esta agindo?", nao "esta configurada?". Cada linha aqui
        cobre um estado em que o agente de execucao NAO consome a fila de
        ordens -- e o Risk Manager nao pode emitir a saida de protecao para uma
        fila que ninguem drena, porque ele trava o ativo em `_exiting` e o log
        registra `risk.protective_exit` como se o stop tivesse agido.

        A ordem das perguntas vai do estado mais obvio para o que mais se
        parece com saudavel. `is_paused` foi o buraco medido: um agente pausado
        tem `is_running=True`, `deaf_topics=[]`, `last_error=None` e nao esta em
        `_given_up` -- e era o unico destes estados que um clique produz.
        """
        agent = self.agents.get("execution")
        if agent is None:
            return "o agente de execucao nao esta montado"
        if "execution" in self._given_up:
            return "o watchdog desistiu do agente de execucao"
        if not agent.is_running:
            return "o agente de execucao nao esta rodando"
        if agent.deaf_topics:
            return f"o agente de execucao perdeu a assinatura de {agent.deaf_topics}"
        if agent.is_paused:
            # O laco da execucao chama `wait_if_paused()` DEPOIS de tirar o
            # pedido da caixa de entrada: o pedido sai da fila e fica parado
            # dentro do agente. Nem a fila pendente denuncia o problema.
            return "o agente de execucao esta pausado e nao consome a fila de ordens"
        if agent.state is AgentState.ERROR:
            # Tarefa viva e estado de erro convivem: `report_inbox_failure` e
            # `_worker_finished` marcam ERROR sem derrubar o laco principal. Ate
            # o watchdog reiniciar, tratar como se fosse executar e otimismo.
            detalhe = agent.last_error or "sem detalhe"
            return f"o agente de execucao esta em estado de erro ({detalhe})"
        return None

    async def _block_openings_while_unavailable(self, motivo: str) -> None:
        """Com a execucao fora do ar, para de ABRIR tambem.

        Sem isto o sistema ficava no estado mais incoerente que ja existiu aqui:
        no MESMO ciclo em que declara em alto que nao consegue FECHAR posicao
        (`positions_unprotected`, logo abaixo), ele seguia deixando abrir. O
        Risk Manager continuava aprovando compras, que se empilhavam na caixa de
        entrada duravel da execucao para serem executadas quando ela voltasse --
        e, no estado `ERROR` com a tarefa viva, executadas na hora. Medido antes
        desta correcao, no ciclo seguinte ao aviso de posicao exposta:
        `execution.filled side=buy quantity=0.001`. O estado seguro nao e
        "arrisca menos", e "nao arrisca" (disciplina 3).

        A trava e a de ABERTURA, nunca a pausa do agente: pausar quem fecha
        posicao desliga o stop-loss, e foi esse o defeito grave que o item 6
        encontrou na rodada 1 (D4). `Side.SELL` continua passando.
        """
        agent = self.agents.get("execution")
        blocker = getattr(agent, "block_openings", None)
        if blocker is None:
            # Agente ausente ou sem trava de abertura (dubles de teste): nao ha
            # o que travar, e o aviso abaixo diz que a abertura segue solta.
            return
        if getattr(agent, "openings_blocked", None) is not None:
            # Trava ja armada -- pelo circuit breaker ou pela interface -- nao
            # troca de motivo, e nao passa a ser nossa para soltar depois.
            return

        motivo_trava = f"execucao indisponivel: {motivo}"
        blocker(motivo_trava)
        self._openings_blocked_reason = motivo_trava
        log.error(
            "orchestrator.openings_blocked_unavailable",
            reason=motivo,
            detail="abertura barrada enquanto a execucao nao age; fechamento segue passando",
        )
        if self._openings_block_alerted:
            return  # um aviso por episodio (D19); o log registra todos
        self._openings_block_alerted = True
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "openings_blocked_unavailable",
                "title": "Abertura bloqueada -- a execucao nao esta agindo",
                "message": (
                    f"{motivo}.\n\n"
                    "Enquanto isso durar, NENHUMA compra sera executada: se o "
                    "sistema nao consegue FECHAR posicao, ele tambem nao pode "
                    "abrir. Aprovar compras que ficariam esperando na fila da "
                    "execucao seria acumular risco novo justamente sem stop-loss.\n\n"
                    "Vendas e saidas de protecao continuam passando (D4) -- o que "
                    "falta e o agente voltar a consumir a fila. A abertura e "
                    "liberada sozinha quando ele voltar."
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    def _release_openings_after_recovery(self) -> None:
        """Solta a abertura quando -- e somente quando -- a trava e nossa.

        Travar e nunca destravar trocaria um defeito por outro: um erro
        transitorio na execucao aposentaria o sistema em silencio, que e o
        estado que `_check_sizing` existe para denunciar.

        A comparacao com o motivo guardado e o que impede o desarme por porta
        lateral. Se o circuit breaker ou a interface reescreveu a trava depois,
        o motivo corrente ja nao e o nosso e ela fica onde esta -- inclusive
        porque o circuit breaker so volta atras com rearme manual.
        """
        self._openings_block_alerted = False
        if self._openings_blocked_reason is None:
            return
        if self._circuit_breaker_active():
            # Defesa em profundidade: com a trava geral armada a abertura so
            # volta com ela rearmada, a mesma recusa de `_allow_openings_for`.
            return
        agent = self.agents.get("execution")
        if getattr(agent, "openings_blocked", None) != self._openings_blocked_reason:
            self._openings_blocked_reason = None
            return
        unblocker = getattr(agent, "allow_openings", None)
        if unblocker is None:  # pragma: no cover - quem trava sabe destravar
            return
        unblocker()
        self._openings_blocked_reason = None
        log.warning(
            "orchestrator.openings_allowed_recovered",
            detail="a execucao voltou a consumir a fila; abertura liberada",
        )

    def _openings_blocked_now(self) -> bool:
        """Se a abertura esta barrada agora, para o aviso nao mentir."""
        return getattr(self.agents.get("execution"), "openings_blocked", None) is not None

    async def _warn_positions_unprotected(
        self, snapshot: PortfolioSnapshot, motivo: str
    ) -> None:
        """Diz em alto que as posicoes estao sem stop-loss.

        Uma vez por episodio (D19): com o Portfolio Agent rodando a cada 60s,
        avisar a cada snapshot seriam 1.440 mensagens por dia e o aviso deixaria
        de ser lido -- justamente quando ele e o unico que importa.
        """
        expostas = [
            p.asset
            for p in snapshot.positions
            if p.quantity > 0 and p.asset != self.settings.trading.quote_currency
        ]
        log.error(
            "orchestrator.positions_unprotected",
            reason=motivo,
            positions=expostas,
            detail="stop-loss e take-profit nao serao executados",
        )
        if self._unprotected_alerted or not expostas:
            return
        self._unprotected_alerted = True
        abertura = (
            "A ABERTURA de posicao esta bloqueada enquanto isso durar: quem nao "
            "consegue fechar tambem nao abre.\n\n"
            if self._openings_blocked_now()
            else "ATENCAO: a abertura de posicao NAO esta bloqueada -- este agente "
            "de execucao nao tem trava de abertura.\n\n"
        )
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "positions_unprotected",
                "title": "Posicoes SEM stop-loss -- a execucao nao esta agindo",
                "message": (
                    f"{motivo}.\n\n"
                    f"Posicoes expostas: {', '.join(expostas)}.\n\n"
                    + abertura
                    + "Enquanto isso durar, o sistema NAO emite a saida de protecao: "
                    "emitir para um agente que nao executa deixaria o ativo travado "
                    "como 'saida em voo' e o log diria que o stop agiu. Retome o "
                    "agente de execucao pela interface, ou feche as posicoes "
                    "manualmente pela exchange."
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

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

        A assinatura e reaberta se cair. Sendo o caminho unico, uma excecao
        vinda do bus (o `xread` do Redis faz isso ao perder conexao) matava esta
        tarefa e o sistema seguia de pe sem NENHUM alerta chegar ao operador --
        silencio que se parece com "esta tudo bem".

        E nem toda morte levanta excecao: o gerador que simplesmente TERMINA
        deixava este laco fazer `return` como se fosse desligamento. Fora do
        `stop()` isso e falha, e falha aqui e o operador parar de receber
        qualquer aviso. E a mesma licao que `_Inbox.alive` ja tinha aprendido.
        """
        while True:
            try:
                await self._drain_alerts()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("orchestrator.alert_listener_failed", error=str(exc))
            else:
                if self._shutting_down:
                    return  # bus encerrado pelo `stop()`: desligamento normal
                log.error(
                    "orchestrator.alert_stream_ended",
                    detail="a assinatura de ALERTS terminou sem desligamento; reassinando",
                )
                if not self._alert_path_alerted:
                    # Avisar PELO BUS seria pedir socorro pelo telefone que caiu.
                    # Um aviso por episodio (D19): o laco reassina a cada
                    # segundo, e um bus morto viraria uma mensagem por segundo.
                    self._alert_path_alerted = True
                    await self._notify_directly(
                        "Caminho de alertas caiu -- reassinando",
                        "A assinatura de Topics.ALERTS terminou sozinha. Enquanto isso "
                        "durar, nenhum alerta do sistema chega ate voce. O sistema "
                        "continua tentando reassinar.",
                    )
            await asyncio.sleep(1)

    async def _notify_directly(self, title: str, message: str) -> None:
        """Fala com o operador sem passar pelo bus, para quando o bus e o problema."""
        try:
            await self.notifier.send(title, message)
        except Exception as exc:
            log.error("orchestrator.direct_notify_failed", error=str(exc))

    async def _drain_alerts(self) -> None:
        async for alert in self.bus.subscribe(Topics.ALERTS):
            # Alerta chegando de verdade e a prova de que o caminho voltou: e o
            # que rearma o aviso de "o caminho de alertas caiu".
            self._alert_path_alerted = False
            log.warning("orchestrator.alert", **alert)
            title = alert.get("title") or alert.get("type", "Alerta")
            # `detail` na lista porque alertas escritos com essa chave -- entre
            # eles o do stop-loss disparado e o de capital nao autorizado --
            # chegavam ao operador com o corpo VAZIO.
            message = (
                alert.get("message") or alert.get("detail") or alert.get("reason") or ""
            )
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
            try:
                await self._check_health_once()
            except Exception as exc:
                # A vigilancia nunca pode morrer por um erro dela mesma: sem
                # watchdog, um agente travado deixa de ser detectado.
                log.error("orchestrator.watchdog_cycle_failed", error=str(exc))

    def _is_stale(self, agent: BaseAgent, now: datetime) -> bool:
        """Ha quanto tempo o agente nao da sinal de vida.

        A referencia e a ultima batida ou, se ele nunca bateu, a subida da
        tarefa. Sem essa segunda parte, `last_beat is None` significava
        eternidade: um agente que sobe e nunca bate -- o pior caso -- passava
        despercebido para sempre.
        """
        if agent.is_paused:
            return False
        reference = agent.last_beat or agent.started_at
        if reference is None:
            return False
        return (now - reference) > HEARTBEAT_TIMEOUT

    async def _check_health_once(self) -> None:
        """Um ciclo de vigilancia.

        Separado do laco para que o teste possa exercer o diagnostico sem
        esperar `WATCHDOG_INTERVAL_SECONDS`.
        """
        now = datetime.now(UTC)
        await self._supervise_own_tasks(now)
        for name, agent in self.agents.items():
            # Surdo conta como falha, e nao "so" como falta de sinal de vida: o
            # agente esta de pe e nao ouve mais o topico, entao esperar o timeout
            # seriam mais 10 minutos de evento de publicacao unica caindo no
            # vazio. Reiniciar reassina.
            deaf = agent.deaf_topics
            crashed = agent.state is AgentState.ERROR or not agent.is_running or bool(deaf)
            stale = self._is_stale(agent, now)
            if deaf:
                log.error("orchestrator.agent_deaf", agent=name, topics=deaf)

            if name in self._given_up:
                continue
            if agent.is_paused and not crashed:
                # Pausa pedida pelo operador (ou pelo circuit breaker) nao e
                # travamento: o agente esta parado porque foi mandado parar.
                continue
            if not crashed and not stale:
                self._note_healthy(name, now)
                continue

            await self._restart_agent(name, agent, crashed=crashed, stale=stale, now=now)

    #: As tarefas do proprio orquestrador: nome, atributo e como recria-las.
    #:
    #: Elas nao sao agentes e nao tinham vigilancia NENHUMA -- o watchdog cuidava
    #: de todo mundo menos de si mesmo. Se `_listen_alerts` morre, o operador
    #: para de receber qualquer aviso; se `_watchdog` morre, agente travado deixa
    #: de ser detectado. Nos dois casos o sistema segue verde.
    OWN_TASKS = (
        ("watchdog", "_watchdog", "_watch_health"),
        ("alert-listener", "_alert_listener", "_listen_alerts"),
        ("onchain-refresher", "_onchain_refresher", "_refresh_onchain_forever"),
    )

    def _own_tasks_alive(self) -> dict[str, bool]:
        """Quais tarefas do orquestrador estao de pe agora."""
        estado: dict[str, bool] = {}
        for nome, atributo, _ in self.OWN_TASKS:
            task: asyncio.Task[None] | None = getattr(self, atributo, None)
            estado[nome] = task is not None and not task.done()
        return estado

    async def _supervise_own_tasks(self, now: datetime) -> None:
        """Ressuscita tarefa do orquestrador que morreu, e diz que morreu.

        A supervisao e MUTUA de proposito: este metodo roda no ciclo do watchdog
        e tambem no caminho do snapshot, movido pelo Portfolio Agent. Um
        supervisor que so rodasse dentro do watchdog nunca perceberia a morte do
        proprio watchdog -- que e justamente a tarefa cuja morte apaga toda a
        deteccao de travamento.
        """
        if self._shutting_down:
            return
        for nome, atributo, metodo in self.OWN_TASKS:
            task: asyncio.Task[None] | None = getattr(self, atributo, None)
            chave = f"task:{nome}"
            if task is None or not task.done():
                # `None` e a montagem de teste, que nunca subiu estas tarefas.
                self._note_healthy(chave, now)
                continue

            erro: BaseException | None = None
            if not task.cancelled():
                with contextlib.suppress(Exception):
                    erro = task.exception()
            historico = self._recent_restarts(chave, now)
            historico.append(now)
            self._restart_history[chave] = historico
            log.error(
                "orchestrator.own_task_died",
                task=nome,
                error=str(erro) if erro else None,
                cancelled=task.cancelled(),
                attempt=len(historico),
            )
            setattr(self, atributo, asyncio.create_task(getattr(self, metodo)(), name=nome))

            if chave in self._restart_alerted:
                continue  # um alerta por episodio (D19); o log registra todas
            self._restart_alerted.add(chave)
            await self.bus.publish(
                Topics.ALERTS,
                {
                    "type": "orchestrator_task_died",
                    "title": f"Tarefa interna '{nome}' morreu e foi recriada",
                    "message": (
                        f"erro: {erro or 'terminou sem excecao'}\n\n"
                        + _OWN_TASK_CONSEQUENCE.get(nome, "")
                        + "\nA tarefa foi recriada. Novas mortes do mesmo episodio "
                        "ficam so no log."
                    ),
                    "timestamp": now.isoformat(),
                },
            )

    def _restart_window(self) -> timedelta:
        """Lida do modulo a cada chamada para acompanhar o timeout vigente."""
        return HEARTBEAT_TIMEOUT * RESTART_WINDOW_FACTOR

    def _recent_restarts(self, name: str, now: datetime) -> list[datetime]:
        """Reinicios ainda dentro da janela, descartando os antigos."""
        window = self._restart_window()
        recent = [when for when in self._restart_history.get(name, []) if now - when <= window]
        if recent:
            self._restart_history[name] = recent
        else:
            self._restart_history.pop(name, None)
        return recent

    def _note_healthy(self, name: str, now: datetime) -> None:
        """Fecha o episodio e rearma o alerta -- mas so quando ele acabou mesmo.

        Um ciclo saudavel nao encerra episodio nenhum: logo depois de um
        reinicio o agente SEMPRE parece saudavel, porque `_is_stale` mede a
        partir de `started_at` e ele acabou de ser renovado. Era assim que a
        contagem zerava sozinha e a desistencia nunca armava. O episodio so
        fecha quando a janela inteira passa sem um reinicio novo.
        """
        if self._recent_restarts(name, now):
            return
        if name in self._restart_alerted:
            self._restart_alerted.discard(name)
            log.info("orchestrator.agent_recovered", agent=name)

    async def _restart_agent(
        self, name: str, agent: BaseAgent, *, crashed: bool, stale: bool, now: datetime
    ) -> None:
        history = self._recent_restarts(name, now)
        history.append(now)
        self._restart_history[name] = history
        streak = len(history)
        reason = "falha" if crashed else "sem sinal de vida"

        log.error(
            "orchestrator.restarting_agent",
            agent=name,
            crashed=crashed,
            stale=stale,
            attempt=streak,
            error=agent.last_error,
        )

        try:
            lost = await agent.restart()
        except Exception as exc:
            log.error("orchestrator.restart_failed", agent=name, error=str(exc))
            lost = None

        if streak >= MAX_RESTART_ATTEMPTS:
            await self._give_up_on(name, agent, reason=reason, attempts=streak, now=now)
            return

        if name in self._restart_alerted:
            # Um alerta por episodio, e episodio e uma JANELA DE TEMPO. Foram 17
            # alertas em 16 minutos em D25, no ritmo de 1.440 por dia -- e um
            # alerta que chega sempre deixa de ser lido (o mesmo criterio de
            # D19). O log registra todas as vezes.
            return

        self._restart_alerted.add(name)
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "agent_restarted",
                "title": f"Agente '{name}' reiniciado",
                "message": (
                    f"motivo: {reason}\n"
                    f"ultimo erro: {agent.last_error or 'nenhum'}\n"
                    + (
                        f"evento perdido no meio do processamento: {lost}\n"
                        if lost
                        else "nenhum evento perdido: a assinatura sobreviveu ao reinicio\n"
                    )
                    + "Novos reinicios do mesmo episodio ficam so no log."
                ),
                "timestamp": now.isoformat(),
            },
        )

    async def _give_up_on(
        self, name: str, agent: BaseAgent, *, reason: str, attempts: int, now: datetime
    ) -> None:
        """Para de reiniciar e para de operar.

        Insistir nao consertou nada em D25. E um agente da cadeia que nao volta
        significa que o sistema nao sabe mais o que esta acontecendo -- entao
        ele recusa operar em vez de operar com uma etapa faltando. O estado
        seguro nao e "arrisca menos", e "nao arrisca".
        """
        self._given_up.add(name)
        log.error(
            "orchestrator.agent_unrecoverable",
            agent=name,
            attempts=attempts,
            reason=reason,
            error=agent.last_error,
        )
        consequencia = (
            "O watchdog parou de tentar e a tomada de decisao foi pausada. "
            "Investigue o log e retome o agente pela interface."
        )
        if name == "execution":
            # A consequencia que importa nao e "parou de decidir": e que NENHUMA
            # ordem sai mais, inclusive as de protecao. Dizer so o primeiro
            # deixaria o operador tranquilo com a carteira desprotegida.
            consequencia = (
                "ATENCAO: sem o agente de execucao NENHUMA ordem sai da fila -- "
                "as posicoes abertas estao SEM stop-loss e SEM take-profit ate ele "
                "voltar. O sistema parou de decidir tambem, mas isso e o menor dos "
                "dois problemas agora. Considere fechar posicoes manualmente pela "
                "exchange e retome o agente pela interface."
            )
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "agent_unrecoverable",
                "title": f"Agente '{name}' nao volta -- operacao suspensa",
                "message": (
                    f"{attempts} reinicios na mesma janela nao resolveram ({reason}).\n"
                    f"ultimo erro: {agent.last_error or 'nenhum'}\n\n" + consequencia
                ),
                "timestamp": now.isoformat(),
            },
        )
        await self.pause_all(
            actor="watchdog", reason=f"agente '{name}' nao recuperou em {attempts} tentativas"
        )

    async def _audit(
        self, action: str, target: str, actor: str, detail: str | None = None
    ) -> None:
        async with session_scope(self.settings) as session:
            await AuditLogRepository(session).append(
                action=action, actor=actor, target=target, detail=detail
            )
