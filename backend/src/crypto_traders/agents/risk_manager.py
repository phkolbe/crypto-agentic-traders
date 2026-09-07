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
    OrderType,
    RiskDecision,
    RiskEventType,
    Side,
    SignalDirection,
)
from ..domain.models import OrderRequest, PortfolioSnapshot, RiskAssessment, Signal
from ..risk.rules import PortfolioState, RiskEngine
from .base import BaseAgent


class RiskManagerAgent(BaseAgent):
    name = "risk_manager"

    def __init__(self, bus: EventBus, settings: Settings) -> None:
        super().__init__(bus)
        self._settings = settings
        self._limits = settings.risk
        self._engine = RiskEngine(self._limits, settings.quote_currency)
        self._snapshot: PortfolioSnapshot | None = None
        self._circuit_breaker_active = False
        self._circuit_breaker_reason: str | None = None

    @property
    def limits(self) -> RiskSettings:
        return self._limits

    @property
    def circuit_breaker_active(self) -> bool:
        return self._circuit_breaker_active

    # ------------------------------------------------------------------
    async def _run(self) -> None:
        await self._load_state()
        async for signal in self.bus.subscribe(Topics.SIGNALS):
            await self.wait_if_paused()
            try:
                await self._on_signal(signal)
            except Exception as exc:
                # Falhar ao avaliar NAO pode virar "aprovado". A excecao e
                # registrada e o sinal simplesmente nao gera ordem.
                self.log.exception("risk.evaluation_failed", error=str(exc))
            await self.heartbeat()

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
                self._engine = RiskEngine(self._limits, self._settings.quote_currency)
            except Exception as exc:
                # Config invalida no banco nao pode derrubar o guardiao: seguimos
                # com os limites do .env, que sao conservadores por padrao.
                self.log.error("risk.stored_limits_invalid", error=str(exc))

    # ------------------------------------------------------------------
    def observe_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        """Recebe o retrato mais recente do portfolio, publicado pelo Portfolio Agent."""
        self._snapshot = snapshot

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

        return PortfolioState(
            total_value=snapshot.total_value,
            cash=snapshot.cash_value,
            positions=positions,
            prices=prices,
            last_order_at={signal.symbol: last_order} if last_order else {},
            circuit_breaker_active=self._circuit_breaker_active,
            circuit_breaker_reason=self._circuit_breaker_reason,
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
        """Compara o patrimonio atual com o do inicio do dia e da semana.

        Retorna o motivo se a trava disparou agora, `None` caso contrario.
        Uma vez disparada, permanece ativa ate rearme **manual**: se o sistema
        perdeu dinheiro rapido o bastante para chegar aqui, a causa precisa ser
        entendida por uma pessoa antes de voltar a operar.
        """
        if self._circuit_breaker_active:
            return None

        now = snapshot.timestamp
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())

        async with session_scope(self._settings) as session:
            repository = PortfolioSnapshotRepository(session)
            day_reference = await repository.first_value_since(day_start)
            week_reference = await repository.first_value_since(week_start)

        for label, reference, limit in (
            ("diaria", day_reference, self._limits.daily_loss_limit_pct),
            ("semanal", week_reference, self._limits.weekly_loss_limit_pct),
        ):
            if reference is None or reference <= 0:
                continue
            drop = (reference - snapshot.total_value) / reference
            if drop >= Decimal(str(limit)):
                reason = (
                    f"perda {label} de {drop:.2%} (limite {limit:.2%}); "
                    f"referencia {reference:.2f} -> atual {snapshot.total_value:.2f}"
                )
                await self._trip(reason)
                return reason
        return None

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
        self._engine = RiskEngine(updated, self._settings.quote_currency)
        self.log.warning("risk.limits_updated", actor=actor, changed=sorted(values))
        return updated


def _client_order_id() -> str:
    """ID de idempotencia.

    Prefixo curto + UUID: a Binance limita o tamanho do `clientOrderId`, e o
    prefixo ajuda a reconhecer ordens deste sistema no extrato da exchange.
    """
    return f"cat-{uuid.uuid4().hex[:20]}"
