"""Risk Manager Agent — o guardiao.

Todo sinal passa por aqui antes de virar ordem. O agente e a casca com estado
(banco, circuit breaker, limites vigentes) em volta do `RiskEngine`, que contem a
logica pura e testada.

Duas garantias estruturais:

1. Apenas este agente constroi `OrderRequest`, e todo `OrderRequest` carrega um
   `risk_event_id` -- a chave da linha ja gravada em `risk_events`. O Execution
   Agent nao aceita outra coisa, entao nao existe atalho de sinal para ordem.
2. A avaliacao e persistida **antes** de a ordem ser publicada. Se o processo
   morrer no meio, sobra o registro do que foi decidido e por que.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ..bus import EventBus, Topics
from ..config import RiskSettings, Settings
from ..db.repositories import (
    AuditLogRepository,
    OrderRepository,
    PortfolioSnapshotRepository,
    RiskConfigRepository,
    RiskEventRepository,
)
from ..db.session import session_scope
from ..domain.enums import (
    ExchangeName,
    OrderType,
    RiskDecision,
    RiskEventType,
    Side,
    SignalDirection,
)
from ..domain.models import OrderRequest, PortfolioSnapshot, RiskAssessment, Signal
from ..onchain import OnChainProvider
from ..risk.rules import PortfolioState, RiskEngine
from .base import BaseAgent


class RiskManagerAgent(BaseAgent):
    name = "risk_manager"

    def __init__(
        self, bus: EventBus, settings: Settings, onchain: OnChainProvider | None = None
    ) -> None:
        super().__init__(bus)
        self._settings = settings
        self._onchain = onchain
        """Provedor on-chain opcional. Ausente significa "sem filtro de regime"."""

        self._limits = settings.risk
        self._engine = RiskEngine(self._limits, settings.trading.quote_currency)
        self._snapshot: PortfolioSnapshot | None = None
        self._circuit_breaker_active = False
        self._circuit_breaker_reason: str | None = None
        self._capital_alerted = False
        """Aviso de saldo nao autorizado sai uma vez por transicao."""

        self._exiting: set[str] = set()
        """Ativos com ordem de protecao em voo.

        Sem esta trava, cada snapshot reemitiria o mesmo fechamento enquanto a
        ordem anterior nao tivesse liquidado -- vendendo a posicao varias vezes.
        """

        self._discovered_universe: tuple[list[str], list[str]] | None = None
        """Whitelist vinda da descoberta automatica: (simbolos, ativos).

        Guardada separada dos limites porque `_load_state()` recarrega os
        limites do banco a cada sinal -- se o universo morasse dentro deles,
        cada recarga apagaria o resultado da ultima varredura.
        """

    @property
    def limits(self) -> RiskSettings:
        """Limites em vigor, ja com o universo descoberto aplicado."""
        return getattr(self, "_effective_limits", self._limits)

    @property
    def configured_limits(self) -> RiskSettings:
        """Limites como configurados, sem o universo da descoberta."""
        return self._limits

    @property
    def last_snapshot(self) -> PortfolioSnapshot | None:
        """Ultimo retrato recebido do Portfolio Agent."""
        return self._snapshot

    @property
    def circuit_breaker_active(self) -> bool:
        return self._circuit_breaker_active

    # ------------------------------------------------------------------
    async def _run(self) -> None:
        await self._load_state()

        # Uma tarefa alimenta a fila; o loop principal a drena em lotes. Iterar
        # a assinatura direto bloquearia ate o proximo sinal, e sem poder
        # esperar pela rajada nao ha o que comparar.
        queue: asyncio.Queue[Signal] = asyncio.Queue()

        async def feed() -> None:
            async for signal in self.bus.subscribe(Topics.SIGNALS):
                await queue.put(signal)

        feeder = asyncio.create_task(feed(), name="risk-signal-feed")
        try:
            while True:
                batch = await self._collect_batch(queue)
                await self.wait_if_paused()
                try:
                    await self._on_batch(batch)
                except Exception as exc:
                    # Falhar ao avaliar NAO pode virar "aprovado". A excecao e
                    # registrada e os sinais simplesmente nao geram ordem.
                    self.log.exception("risk.evaluation_failed", error=str(exc))
                await self.heartbeat()
        finally:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feeder

    async def _collect_batch(self, queue: asyncio.Queue[Signal]) -> list[Signal]:
        """Espera o primeiro sinal e junta os que chegarem na janela.

        A janela e o que transforma "quem chegou primeiro" em "qual e o melhor".
        Quanto maior, melhor a escolha e mais atrasada a execucao; o padrao de 2s
        cobre a rajada de um ciclo de coleta sem atrasar de forma perceptivel.
        """
        batch = [await queue.get()]
        window = self._settings.trading.signal_batch_window_seconds
        if window <= 0:
            return batch

        loop = asyncio.get_running_loop()
        deadline = loop.time() + window
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(queue.get(), timeout=remaining))
            except TimeoutError:
                break
        return batch

    async def _on_batch(self, batch: list[Signal]) -> None:
        """Decide o lote inteiro de uma vez, por confianca."""
        if self._snapshot is None:
            for signal in batch:
                await self._persist_and_publish(
                    RiskAssessment(
                        signal_id=signal.id,
                        decision=RiskDecision.REJECTED,
                        reasons=["portfolio ainda nao apurado (aguardando primeiro snapshot)"],
                    ),
                    signal,
                )
            return

        await self._load_state()
        state = await self._build_state(batch[0])
        for symbol in {s.symbol for s in batch}:
            base = symbol.partition("/")[0]
            for signal in batch:
                if signal.symbol == symbol:
                    state.prices[base] = signal.reference_price
                    break

        decisoes = self._engine.evaluate_batch(batch, state)
        if len(batch) > 1:
            self.log.info(
                "risk.batch_evaluated",
                sinais=len(batch),
                aprovados=sum(
                    1 for _, a in decisoes if a.decision is RiskDecision.APPROVED
                ),
                ranking=[
                    f"{s.symbol}@{s.confidence:.2f}"
                    for s, _ in decisoes
                    if s.direction is SignalDirection.LONG
                ],
            )
        for signal, assessment in decisoes:
            await self._persist_and_publish(assessment, signal)

    async def _load_state(self) -> None:
        """Carrega limites e estado do circuit breaker do banco.

        O `.env` fornece os valores iniciais; a partir da primeira alteracao pela
        interface, o banco e a fonte da verdade.
        """
        async with session_scope(self._settings) as session:
            config = await RiskConfigRepository(session).get_or_create(
                self._limits.model_dump(mode="json")
            )
            self._circuit_breaker_active = config.circuit_breaker_active
            self._circuit_breaker_reason = config.circuit_breaker_reason
            stored = dict(config.values or {})

        if stored:
            try:
                self._limits = RiskSettings.model_validate(stored)
            except Exception as exc:
                # Config invalida no banco nao pode derrubar o guardiao: seguimos
                # com os limites do .env, que sao conservadores por padrao.
                self.log.error("risk.stored_limits_invalid", error=str(exc))

        self._rebuild_engine()

    def _rebuild_engine(self) -> None:
        """Aplica o universo descoberto por cima dos limites vigentes.

        Divisao de responsabilidade: o banco (e a interface) governam os numeros
        -- tamanho de ordem, stop, exposicao; a descoberta governa QUAIS pares
        entram. Sem essa separacao, salvar um limite pela interface reverteria a
        whitelist para a lista estatica do `.env`.
        """
        limits = self._limits
        if self._discovered_universe is not None:
            symbols, assets = self._discovered_universe
            limits = limits.model_copy(
                update={"symbol_whitelist": list(symbols), "asset_whitelist": list(assets)}
            )
        self._effective_limits = limits
        self._engine = RiskEngine(limits, self._settings.trading.quote_currency)

    async def apply_discovered_universe(self, symbols: list[str], assets: list[str]) -> None:
        """Adota o resultado da descoberta como whitelist efetiva.

        Toda mudanca vai para o audit log: no modo "mar aberto" a whitelist deixa
        de ser digitada a mao, mas nao deixa de ser rastreavel -- em qualquer
        instante da para saber o que o sistema podia negociar e desde quando.
        """
        previous = self._discovered_universe[0] if self._discovered_universe else []
        if sorted(previous) == sorted(symbols):
            return

        self._discovered_universe = (list(symbols), list(assets))
        self._rebuild_engine()

        async with session_scope(self._settings) as session:
            await AuditLogRepository(session).append(
                action="trading_universe_discovered",
                actor="discovery",
                target="risk_config",
                before={"symbols": previous},
                after={"symbols": list(symbols)},
                detail="whitelist efetiva definida por descoberta automatica de mercado",
            )
        self.log.warning("risk.universe_updated", symbols=symbols)

    # ------------------------------------------------------------------
    def observe_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        """Recebe o retrato mais recente do portfolio, publicado pelo Portfolio Agent."""
        self._snapshot = snapshot

    async def check_capital_authorization(self, snapshot: PortfolioSnapshot) -> Decimal:
        """Avisa quando existe saldo que o sistema nao pode usar.

        Um deposito nao e uma ordem. Dinheiro que entra na conta por qualquer
        motivo -- venda de outro ativo, transferencia, reserva para outra
        finalidade -- nao deveria virar exposicao sem alguem dizer que sim. O
        portao existe para essa distincao.

        Mas dinheiro autorizado e parado tambem e um problema, e o oposto do
        anterior: o sistema ficaria de pe com caixa ocioso sem ninguem perceber.
        Por isso o aviso e ativo, e nao apenas uma linha no `check`.

        O alerta sai **uma vez por transicao**, nao a cada snapshot: com o
        Portfolio Agent rodando a cada 60s, avisar sempre seriam 1.440 mensagens
        por dia sobre o mesmo saldo, e um alerta que chega todo minuto deixa de
        ser lido.
        """
        autorizado = self._limits.authorized_capital
        if autorizado is None:
            return Decimal(0)

        nao_autorizado = max(Decimal(0), snapshot.total_value - autorizado)
        moeda = self._settings.trading.quote_currency
        # Tolerancia: variacao de preco das posicoes move o patrimonio para cima
        # sem que tenha entrado dinheiro. Avisar por causa disso seria ruido.
        relevante = nao_autorizado >= self._limits.min_order_notional

        if relevante and not self._capital_alerted:
            self._capital_alerted = True
            self.log.warning(
                "risk.unauthorized_capital",
                patrimonio=str(snapshot.total_value),
                autorizado=str(autorizado),
                parado=str(nao_autorizado),
            )
            await self.bus.publish(
                Topics.ALERTS,
                {
                    "type": "unauthorized_capital",
                    "title": f"{nao_autorizado:.2f} {moeda} disponiveis e nao autorizados",
                    "detail": (
                        f"O patrimonio e {snapshot.total_value:.2f} {moeda} e o capital "
                        f"autorizado a operar e {autorizado:.2f} {moeda}. "
                        f"O sistema NAO vai usar a diferenca ate voce autorizar. "
                        f"Autorize em Risco, na interface, para por esse saldo a trabalhar."
                    ),
                },
            )
        elif not relevante:
            # Saldo autorizado ou consumido: rearma o aviso para o proximo aporte.
            self._capital_alerted = False

        return nao_autorizado

    async def authorize_all_capital(self, actor: str = "user") -> RiskSettings:
        """Autoriza o patrimonio inteiro a operar, no valor apurado agora.

        Deliberadamente grava um NUMERO em vez de desligar o portao: desligar
        autorizaria tambem todo deposito futuro, que e exatamente o que o portao
        existe para impedir. Autorizar e um ato sobre o saldo de hoje.
        """
        if self._snapshot is None:
            raise ValueError(
                "portfolio ainda nao apurado; aguarde o primeiro snapshot para autorizar"
            )
        return await self.update_limits(
            {"authorized_capital": str(self._snapshot.total_value)}, actor=actor
        )

    async def enforce_protective_exits(self, snapshot: PortfolioSnapshot) -> list[OrderRequest]:
        """Fecha posicoes que romperam stop-loss ou take-profit.

        Ate aqui esses niveis eram calculados, gravados na ordem e nunca
        comparados com preco nenhum -- em producao **nem em backtest**. Uma
        posicao aberta so fechava se a estrategia emitisse sinal de saida.

        Esta e a versao em software, deliberadamente escolhida em vez de mandar
        uma OCO para a exchange. O que ela **nao** cobre, e precisa estar claro:
        morre junto com o processo, e nao age se o sistema perder acesso a
        exchange (o cenario de IP residencial descrito na secao 1 do
        `docs/SEGURANCA.md`). Protecao que depende do processo estar vivo cobre
        oscilacao de mercado, nao queda de infraestrutura.

        Reage a cada snapshot -- por padrao 60s -- comparando o preco corrente
        com os niveis. Nao ve o pavio dentro do intervalo: uma queda que desce
        abaixo do stop e volta antes do proximo snapshot passa batida. O backtest,
        que le a minima do candle, e nesse ponto mais severo que a producao.

        O nivel vem do **preco medio** da posicao, reconstruido pelo Portfolio
        Agent a partir do historico de trades -- o que inclui lancamentos
        manuais. Uma posicao comprada fora do sistema tambem passa a ser
        protegida, o que e o comportamento desejado: o Risk Manager guarda a
        carteira, nao apenas as ordens que ele originou.
        """
        limits = self._limits
        emitidas: list[OrderRequest] = []

        for position in snapshot.positions:
            if position.asset == self._settings.trading.quote_currency:
                continue
            if position.quantity <= 0 or position.average_price is None:
                continue
            if position.current_price is None or position.average_price <= 0:
                continue
            if position.asset in self._exiting:
                # Ordem de protecao ja em voo: reemitir a cada snapshot venderia
                # a posicao varias vezes.
                continue

            stop = position.average_price * (Decimal(1) - Decimal(str(limits.stop_loss_pct)))
            alvo = position.average_price * (Decimal(1) + Decimal(str(limits.take_profit_pct)))

            if position.current_price <= stop:
                motivo = "stop_loss"
            elif position.current_price >= alvo:
                motivo = "take_profit"
            else:
                continue

            symbol = f"{position.asset}/{self._settings.trading.quote_currency}"
            request = OrderRequest(
                client_order_id=_client_order_id(),
                signal_id=None,
                risk_event_id=f"protecao-{motivo}-{position.asset}",
                exchange=ExchangeName(self._settings.exchange),
                symbol=symbol,
                side=Side.SELL,
                order_type=OrderType.MARKET,
                quantity=position.quantity,
                notional=position.quantity * position.current_price,
                strategy="protecao",
            )

            self._exiting.add(position.asset)
            self.log.warning(
                "risk.protective_exit",
                motivo=motivo,
                symbol=symbol,
                preco=str(position.current_price),
                nivel=str(stop if motivo == "stop_loss" else alvo),
                medio=str(position.average_price),
            )
            await self.bus.publish(Topics.ORDER_REQUESTS, request)
            await self.bus.publish(
                Topics.ALERTS,
                {
                    "type": "protective_exit",
                    "title": f"{'Stop-loss' if motivo == 'stop_loss' else 'Take-profit'} "
                    f"acionado em {symbol}",
                    "detail": (
                        f"preco {position.current_price} contra nivel "
                        f"{stop if motivo == 'stop_loss' else alvo} "
                        f"(medio {position.average_price}). Posicao fechada."
                    ),
                },
            )
            emitidas.append(request)

        # Posicao que deixou de existir liberou a trava: o fechamento saiu.
        presentes = {p.asset for p in snapshot.positions if p.quantity > 0}
        self._exiting &= presentes
        return emitidas

    async def _on_signal(self, signal: Signal) -> None:
        if self._snapshot is None:
            # Sem retrato do portfolio nao ha como dimensionar a ordem. Registrar
            # e ignorar e o comportamento seguro -- e o Portfolio Agent publica
            # o primeiro snapshot nos primeiros segundos de vida do sistema.
            await self._persist_and_publish(
                RiskAssessment(
                    signal_id=signal.id,
                    decision=RiskDecision.REJECTED,
                    reasons=["portfolio ainda nao apurado (aguardando primeiro snapshot)"],
                ),
                signal,
            )
            return

        await self._load_state()
        state = await self._build_state(signal)
        assessment = self._engine.evaluate(signal, state)
        await self._persist_and_publish(assessment, signal)

    async def _build_state(self, signal: Signal) -> PortfolioState:
        snapshot = self._snapshot
        assert snapshot is not None

        positions = {p.asset: p.quantity for p in snapshot.positions}
        prices = {
            p.asset: p.current_price for p in snapshot.positions if p.current_price is not None
        }
        # O preco do par avaliado vem do sinal, que e mais recente que o snapshot.
        base = signal.symbol.partition("/")[0]
        prices[base] = signal.reference_price

        async with session_scope(self._settings) as session:
            last_order = await OrderRepository(session).last_order_time(signal.symbol)

        # Leitura de regime. Falha ou ausencia => None => filtro nao se aplica.
        mvrv_percentile = None
        if self._onchain is not None and self._limits.mvrv_max_percentile < 1.0:
            try:
                leitura = await self._onchain.mvrv_reading()
                mvrv_percentile = leitura.percentile if leitura else None
                if leitura is None:
                    self.log.warning("risk.mvrv_unavailable", detail="filtro nao aplicado")
            except Exception as exc:
                self.log.error("risk.mvrv_read_failed", error=str(exc))

        return PortfolioState(
            total_value=snapshot.total_value,
            cash=snapshot.cash_value,
            positions=positions,
            prices=prices,
            last_order_at={signal.symbol: last_order} if last_order else {},
            circuit_breaker_active=self._circuit_breaker_active,
            circuit_breaker_reason=self._circuit_breaker_reason,
            mvrv_percentile=mvrv_percentile,
        )

    async def _persist_and_publish(self, assessment: RiskAssessment, signal: Signal) -> None:
        # Persistir primeiro: um `OrderRequest` nunca pode referenciar um
        # risk_event que nao existe no banco.
        async with session_scope(self._settings) as session:
            await RiskEventRepository(session).save_assessment(assessment)

        await self.bus.publish(Topics.RISK_ASSESSMENTS, assessment)

        if assessment.decision is RiskDecision.REJECTED:
            self.log.info(
                "risk.rejected",
                symbol=signal.symbol,
                strategy=signal.strategy,
                reasons=assessment.reasons,
            )
            return

        request = OrderRequest(
            client_order_id=_client_order_id(),
            signal_id=signal.id,
            risk_event_id=assessment.id,
            exchange=signal.exchange,
            symbol=signal.symbol,
            side=Side.BUY if signal.direction is SignalDirection.LONG else Side.SELL,
            order_type=OrderType.MARKET,
            quantity=assessment.approved_quantity or Decimal(0),
            notional=assessment.approved_notional or Decimal(0),
            stop_loss=assessment.stop_loss,
            take_profit=assessment.take_profit,
            strategy=signal.strategy,
        )
        self.log.info(
            "risk.approved",
            symbol=request.symbol,
            side=str(request.side),
            quantity=str(request.quantity),
            notional=str(request.notional),
            stop_loss=str(request.stop_loss) if request.stop_loss else None,
        )
        await self.bus.publish(Topics.ORDER_REQUESTS, request)

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------
    async def check_circuit_breaker(self, snapshot: PortfolioSnapshot) -> str | None:
        """Dispara a trava quando a NEGOCIACAO perdeu demais no dia ou na semana.

        Retorna o motivo se a trava disparou agora, `None` caso contrario.
        Uma vez disparada, permanece ativa ate rearme **manual**: se o sistema
        perdeu dinheiro rapido o bastante para chegar aqui, a causa precisa ser
        entendida por uma pessoa antes de voltar a operar.

        **Mede resultado de negociacao, nao patrimonio bruto**, e a diferenca nao
        e teorica. Comparando patrimonio, um SAQUE da conta e indistinguivel de
        uma perda catastrofica: tirar R$100 de uma conta de R$150 dispara "perda
        diaria de 67%" e pausa tudo, culpando um prejuizo que nao existiu. Foi o
        que aconteceu no primeiro ensaio em dry_run, quando mudar o saldo
        simulado de 1000 para 150 disparou "perda de 85%" em tres minutos.

        `realized_pnl` (acumulado) + `unrealized_pnl` nao se mexem com deposito
        nem com saque -- apenas com o resultado das operacoes. E a base do
        percentual e o **capital autorizado**, nao o saldo total: com o portao em
        R$150 sobre uma conta de R$650, medir contra 650 tornaria a trava quatro
        vezes mais frouxa do que o configurado.
        """
        if self._circuit_breaker_active:
            return None

        now = snapshot.timestamp
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())

        async with session_scope(self._settings) as session:
            repository = PortfolioSnapshotRepository(session)
            day_reference = await repository.first_result_since(day_start)
            week_reference = await repository.first_result_since(week_start)

        atual = snapshot.realized_pnl + snapshot.unrealized_pnl
        base = self._capital_em_risco(snapshot)
        if base <= 0:
            return None

        for label, reference, limit in (
            ("diaria", day_reference, self._limits.daily_loss_limit_pct),
            ("semanal", week_reference, self._limits.weekly_loss_limit_pct),
        ):
            if reference is None:
                continue
            perda = reference - atual
            if perda <= 0:
                continue
            fracao = perda / base
            if fracao >= Decimal(str(limit)):
                reason = (
                    f"perda {label} de {fracao:.2%} do capital (limite {limit:.2%}); "
                    f"resultado de negociacao {reference:.2f} -> {atual:.2f} "
                    f"sobre {base:.2f} {self._settings.trading.quote_currency}"
                )
                await self._trip(reason)
                return reason
        return None

    def _capital_em_risco(self, snapshot: PortfolioSnapshot) -> Decimal:
        """Base do percentual da trava: o que o sistema pode comprometer."""
        autorizado = self._limits.authorized_capital
        if autorizado is None:
            return snapshot.total_value
        return min(snapshot.total_value, autorizado)

    async def _trip(self, reason: str) -> None:
        self._circuit_breaker_active = True
        self._circuit_breaker_reason = reason
        async with session_scope(self._settings) as session:
            await RiskConfigRepository(session).trip_circuit_breaker(reason)
            await RiskEventRepository(session).save_event(
                RiskEventType.CIRCUIT_BREAKER_TRIPPED, [reason]
            )
            await AuditLogRepository(session).append(
                action="circuit_breaker_tripped", target="risk_config", detail=reason
            )
        self.log.error("risk.circuit_breaker_tripped", reason=reason)
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "circuit_breaker",
                "title": "Circuit breaker acionado",
                "message": (
                    f"{reason}\n\nOs agentes de decisao foram pausados. O rearme e "
                    "manual, pela interface."
                ),
                "reason": reason,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def reset_circuit_breaker(self, actor: str = "user") -> None:
        """Rearme manual, sempre auditado."""
        previous = self._circuit_breaker_reason
        self._circuit_breaker_active = False
        self._circuit_breaker_reason = None
        async with session_scope(self._settings) as session:
            await RiskConfigRepository(session).reset_circuit_breaker()
            await RiskEventRepository(session).save_event(
                RiskEventType.CIRCUIT_BREAKER_RESET, [previous or "sem motivo registrado"]
            )
            await AuditLogRepository(session).append(
                action="circuit_breaker_reset",
                actor=actor,
                target="risk_config",
                before={"reason": previous},
            )
        self.log.warning("risk.circuit_breaker_reset", actor=actor)

    # ------------------------------------------------------------------
    async def update_limits(self, values: dict, actor: str = "user") -> RiskSettings:
        """Aplica novos limites vindos da interface, com trilha de auditoria."""
        merged = self._limits.model_dump(mode="json") | values
        updated = RiskSettings.model_validate(merged)  # falha alto se incoerente

        async with session_scope(self._settings) as session:
            await RiskConfigRepository(session).update_values(updated.model_dump(mode="json"))
            await AuditLogRepository(session).append(
                action="risk_limits_updated",
                actor=actor,
                target="risk_config",
                before=self._limits.model_dump(mode="json"),
                after=updated.model_dump(mode="json"),
            )

        self._limits = updated
        self._rebuild_engine()

        # Mantem `settings.risk` em sincronia: o `check`, o backtest e a leitura
        # de regime consultam de la. Sem isto haveria duas versoes dos limites
        # no mesmo processo -- a divergencia que a separacao ambiente/negocio
        # existe para eliminar, reintroduzida por dentro.
        self._settings.with_business_config(self._settings.trading, updated)

        self.log.warning("risk.limits_updated", actor=actor, changed=sorted(values))
        return self.limits


def _client_order_id() -> str:
    """ID de idempotencia.

    Prefixo curto + UUID: a Binance limita o tamanho do `clientOrderId`, e o
    prefixo ajuda a reconhecer ordens deste sistema no extrato da exchange.
    """
    return f"cat-{uuid.uuid4().hex[:20]}"
