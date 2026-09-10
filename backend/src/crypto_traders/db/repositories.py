"""Repositorios: unico ponto de acesso ao banco.

Agentes e API nunca montam SQL nem manipulam entidades ORM diretamente. Isso
mantem as conversoes Decimal/datetime em um lugar so e permite trocar de banco
sem tocar na logica de negocio.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain import models as dm
from ..domain.enums import OrderStatus, RiskEventType, TradeOrigin
from . import models as orm


def _new_id() -> str:
    return uuid.uuid4().hex


def _as_decimal(value: Any) -> Decimal:
    """SQLite devolve `Numeric` como float/str; normalizamos sempre para Decimal."""
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else _as_decimal(value)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite perde o timezone na ida e volta; reanexamos UTC na leitura."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class CandleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(self, candles: list[dm.Candle]) -> int:
        """Grava candles ignorando os que ja existem.

        Refazer o fetch de historico e rotina (reconexao, restart do agente),
        entao a colisao na chave natural e o caso normal, nao um erro.
        """
        if not candles:
            return 0

        rows = [
            {
                "exchange": str(c.exchange),
                "symbol": c.symbol,
                "timeframe": c.timeframe,
                "open_time": c.open_time,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
            if c.closed
        ]
        if not rows:
            return 0

        dialect = self._session.bind.dialect.name if self._session.bind else "sqlite"
        if dialect == "sqlite":
            stmt = sqlite_insert(orm.Candle).values(rows).on_conflict_do_nothing()
        else:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(orm.Candle).values(rows).on_conflict_do_nothing()

        result = await self._session.execute(stmt)
        return result.rowcount or 0

    async def recent(
        self, exchange: str, symbol: str, timeframe: str, limit: int = 500
    ) -> list[dm.Candle]:
        stmt = (
            select(orm.Candle)
            .where(
                orm.Candle.exchange == exchange,
                orm.Candle.symbol == symbol,
                orm.Candle.timeframe == timeframe,
            )
            .order_by(orm.Candle.open_time.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            dm.Candle(
                exchange=row.exchange,
                symbol=row.symbol,
                timeframe=row.timeframe,
                open_time=_aware(row.open_time),
                open=_as_decimal(row.open),
                high=_as_decimal(row.high),
                low=_as_decimal(row.low),
                close=_as_decimal(row.close),
                volume=_as_decimal(row.volume),
            )
            # Devolvido em ordem cronologica: os indicadores dependem disso.
            for row in reversed(rows)
        ]


class SignalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, signal: dm.Signal) -> None:
        self._session.add(
            orm.Signal(
                id=signal.id,
                created_at=signal.created_at,
                exchange=str(signal.exchange),
                symbol=signal.symbol,
                timeframe=signal.timeframe,
                strategy=signal.strategy,
                direction=str(signal.direction),
                confidence=signal.confidence,
                reason=signal.reason,
                reference_price=signal.reference_price,
                indicators=signal.indicators.values,
            )
        )

    async def list(self, limit: int = 100, offset: int = 0) -> list[orm.Signal]:
        stmt = (
            select(orm.Signal).order_by(orm.Signal.created_at.desc()).limit(limit).offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())


class RiskEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_assessment(self, assessment: dm.RiskAssessment) -> None:
        self._session.add(
            orm.RiskEvent(
                id=assessment.id,
                created_at=assessment.created_at,
                event_type=str(RiskEventType.SIGNAL_EVALUATED),
                signal_id=assessment.signal_id,
                decision=str(assessment.decision),
                reasons=assessment.reasons,
                approved_quantity=assessment.approved_quantity,
                approved_notional=assessment.approved_notional,
                stop_loss=assessment.stop_loss,
                take_profit=assessment.take_profit,
                snapshot=assessment.snapshot,
            )
        )

    async def save_event(
        self, event_type: RiskEventType, reasons: list[str], snapshot: dict | None = None
    ) -> str:
        event_id = _new_id()
        self._session.add(
            orm.RiskEvent(
                id=event_id,
                event_type=str(event_type),
                reasons=reasons,
                snapshot=snapshot or {},
            )
        )
        return event_id

    async def list(self, limit: int = 100, offset: int = 0) -> list[orm.RiskEvent]:
        stmt = (
            select(orm.RiskEvent)
            .order_by(orm.RiskEvent.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_client_id(self, client_order_id: str) -> orm.Order | None:
        stmt = select(orm.Order).where(orm.Order.client_order_id == client_order_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def create_pending(self, request: dm.OrderRequest, mode: str) -> orm.Order:
        """Registra a ordem ANTES de enviar a exchange.

        A ordem nasce PENDING no banco. Se o processo morrer entre o envio e a
        confirmacao, sobra o rastro para reconciliar depois -- em vez de uma
        ordem existindo na exchange e em lugar nenhum aqui.
        """
        order = orm.Order(
            id=request.id,
            created_at=request.created_at,
            client_order_id=request.client_order_id,
            signal_id=request.signal_id,
            risk_event_id=request.risk_event_id,
            exchange=str(request.exchange),
            symbol=request.symbol,
            side=str(request.side),
            order_type=str(request.order_type),
            quantity=request.quantity,
            price=request.price,
            notional=request.notional,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            status=str(OrderStatus.PENDING),
            strategy=request.strategy,
            mode=mode,
        )
        self._session.add(order)
        await self._session.flush()
        return order

    async def apply_result(self, result: dm.OrderResult) -> orm.Order | None:
        order = await self._session.get(orm.Order, result.order_request_id)
        if order is None:
            return None
        order.status = str(result.status)
        order.exchange_order_id = result.exchange_order_id
        order.filled_quantity = result.filled_quantity
        order.average_price = result.average_price
        order.error = result.error
        order.raw = result.raw
        return order

    async def list(self, limit: int = 100, offset: int = 0) -> list[orm.Order]:
        stmt = select(orm.Order).order_by(orm.Order.created_at.desc()).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all())

    async def last_order_time(self, symbol: str) -> datetime | None:
        """Usado pelo cooldown do Risk Manager."""
        stmt = (
            select(func.max(orm.Order.created_at))
            .where(orm.Order.symbol == symbol)
            .where(orm.Order.status != str(OrderStatus.REJECTED))
        )
        return _aware((await self._session.execute(stmt)).scalar_one_or_none())


class TradeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        executed_at: datetime,
        exchange: str,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal = Decimal(0),
        fee_currency: str | None = None,
        origin: TradeOrigin = TradeOrigin.AGENT,
        order_id: str | None = None,
        signal_id: str | None = None,
        strategy: str | None = None,
        mode: str = "dry_run",
        realized_pnl: Decimal | None = None,
        notes: str | None = None,
    ) -> orm.Trade:
        trade = orm.Trade(
            id=_new_id(),
            executed_at=executed_at,
            exchange=exchange,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            notional=quantity * price,
            fee=fee,
            fee_currency=fee_currency,
            origin=str(origin),
            order_id=order_id,
            signal_id=signal_id,
            strategy=strategy,
            mode=mode,
            realized_pnl=realized_pnl,
            notes=notes,
        )
        self._session.add(trade)
        await self._session.flush()
        return trade

    async def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        origin: str | None = None,
        symbol: str | None = None,
        side: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[orm.Trade]:
        stmt = select(orm.Trade)
        if origin:
            stmt = stmt.where(orm.Trade.origin == origin)
        if symbol:
            stmt = stmt.where(orm.Trade.symbol == symbol)
        if side:
            stmt = stmt.where(orm.Trade.side == side)
        if start:
            stmt = stmt.where(orm.Trade.executed_at >= start)
        if end:
            stmt = stmt.where(orm.Trade.executed_at <= end)
        stmt = stmt.order_by(orm.Trade.executed_at.desc()).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all())

    async def count(
        self,
        *,
        origin: str | None = None,
        symbol: str | None = None,
        side: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(orm.Trade)
        if origin:
            stmt = stmt.where(orm.Trade.origin == origin)
        if symbol:
            stmt = stmt.where(orm.Trade.symbol == symbol)
        if side:
            stmt = stmt.where(orm.Trade.side == side)
        if start:
            stmt = stmt.where(orm.Trade.executed_at >= start)
        if end:
            stmt = stmt.where(orm.Trade.executed_at <= end)
        return int((await self._session.execute(stmt)).scalar_one())

    async def get(self, trade_id: str) -> orm.Trade | None:
        return await self._session.get(orm.Trade, trade_id)

    async def delete(self, trade_id: str) -> bool:
        """So faz sentido para lancamentos manuais digitados errado."""
        trade = await self._session.get(orm.Trade, trade_id)
        if trade is None or trade.origin != str(TradeOrigin.MANUAL):
            return False
        await self._session.delete(trade)
        return True

    async def for_cost_basis(self, limit: int = 200_000) -> list[orm.Trade]:
        """Historico inteiro, para derivar custo medio e PnL realizado.

        Sem filtro de modo, de cotacao ou de origem **de proposito**: quem apura
        (`PortfolioAgent._apurar`) precisa VER o trade que vai descartar para
        registrar o descarte no `audit_log`. Filtrar aqui deixaria o descarte
        invisivel, que e como este projeto perde protecao.

        A ordenacao final e feita em Python: compra tem de vir antes de venda
        quando o instante empata, e isso o banco nao sabe.
        """
        stmt = select(orm.Trade).order_by(orm.Trade.executed_at.asc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def realized_pnl_total(self) -> Decimal:
        """Soma a COLUNA `realized_pnl` em Python, nao com `SUM()` no banco.

        No SQLite estas colunas sao texto (ver `db.models.Money`), e deixar o
        banco somar forcaria uma conversao para float -- reintroduzindo pela
        agregacao o erro de precisao que o tipo evita no armazenamento.

        ATENCAO: nao e o realizado do portfolio. Esta coluna so e preenchida pelo
        Execution Agent quando ele mesmo fecha a posicao que abriu; lancamento
        manual, venda parcial e posicao herdada a deixam vazia. O realizado
        publicado no retrato e DERIVADO do historico por custo medio em
        `PortfolioAgent._apurar` -- com esta soma, um historico de 650 de lucro
        publicava zero.
        """
        stmt = select(orm.Trade.realized_pnl).where(orm.Trade.realized_pnl.is_not(None))
        values = (await self._session.execute(stmt)).scalars().all()
        return sum((_as_decimal(value) for value in values), start=Decimal(0))


class PortfolioSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, snapshot: dm.PortfolioSnapshot, mode: str) -> None:
        self._session.add(
            orm.PortfolioSnapshot(
                id=snapshot.id,
                timestamp=snapshot.timestamp,
                total_value=snapshot.total_value,
                cash_value=snapshot.cash_value,
                positions_value=snapshot.positions_value,
                realized_pnl=snapshot.realized_pnl,
                unrealized_pnl=snapshot.unrealized_pnl,
                allocations=snapshot.allocations,
                positions=[p.model_dump(mode="json") for p in snapshot.positions],
                mode=mode,
            )
        )

    async def latest(self, mode: str | None = None) -> orm.PortfolioSnapshot | None:
        """Ultimo retrato; com `mode`, o ultimo retrato DAQUELE modo.

        O recorte por modo existe porque o retrato do ensaio em dry_run carrega
        contabilidade de dinheiro de papel (realizado, saldo reconciliado). Usar
        esse retrato como referencia do modo real seria a contaminacao do papel
        entrando pela porta de tras, depois de barrada na apuracao dos trades.
        """
        stmt = select(orm.PortfolioSnapshot)
        if mode is not None:
            stmt = stmt.where(orm.PortfolioSnapshot.mode == mode)
        stmt = stmt.order_by(orm.PortfolioSnapshot.timestamp.desc()).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def history(
        self, since: datetime | None = None, limit: int = 1000
    ) -> list[orm.PortfolioSnapshot]:
        stmt = select(orm.PortfolioSnapshot)
        if since:
            stmt = stmt.where(orm.PortfolioSnapshot.timestamp >= since)
        stmt = stmt.order_by(orm.PortfolioSnapshot.timestamp.desc()).limit(limit)
        rows = list((await self._session.execute(stmt)).scalars().all())
        return list(reversed(rows))

    async def value_at_or_before(self, moment: datetime) -> Decimal | None:
        """Patrimonio na virada do dia/semana, base do circuit breaker."""
        stmt = (
            select(orm.PortfolioSnapshot.total_value)
            .where(orm.PortfolioSnapshot.timestamp <= moment)
            .order_by(orm.PortfolioSnapshot.timestamp.desc())
            .limit(1)
        )
        value = (await self._session.execute(stmt)).scalar_one_or_none()
        return _as_optional_decimal(value)

    async def first_value_since(self, moment: datetime) -> Decimal | None:
        stmt = (
            select(orm.PortfolioSnapshot.total_value)
            .where(orm.PortfolioSnapshot.timestamp >= moment)
            .order_by(orm.PortfolioSnapshot.timestamp.asc())
            .limit(1)
        )
        value = (await self._session.execute(stmt)).scalar_one_or_none()
        return _as_optional_decimal(value)

    async def first_result_since(self, moment: datetime) -> Decimal | None:
        """Resultado acumulado de NEGOCIACAO no primeiro retrato do periodo.

        `realized_pnl` (acumulado desde sempre) mais `unrealized_pnl` (marcacao a
        mercado das posicoes abertas). A soma e imune a deposito e saque: dinheiro
        que entra ou sai da conta nao muda lucro realizado nem nao realizado.

        E por isso que ela substitui o patrimonio bruto no circuit breaker. Ver
        `RiskManagerAgent.check_circuit_breaker`.
        """
        stmt = (
            select(
                orm.PortfolioSnapshot.realized_pnl,
                orm.PortfolioSnapshot.unrealized_pnl,
            )
            .where(orm.PortfolioSnapshot.timestamp >= moment)
            .order_by(orm.PortfolioSnapshot.timestamp.asc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        realizado = _as_optional_decimal(row[0]) or Decimal(0)
        nao_realizado = _as_optional_decimal(row[1]) or Decimal(0)
        return realizado + nao_realizado


class AgentRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def heartbeat(self, agent: str, state: str, detail: str | None = None) -> None:
        self._session.add(orm.AgentRun(agent=agent, state=state, detail=detail))

    async def latest_per_agent(self) -> dict[str, orm.AgentRun]:
        subquery = (
            select(orm.AgentRun.agent, func.max(orm.AgentRun.timestamp).label("ts"))
            .group_by(orm.AgentRun.agent)
            .subquery()
        )
        stmt = select(orm.AgentRun).join(
            subquery,
            (orm.AgentRun.agent == subquery.c.agent) & (orm.AgentRun.timestamp == subquery.c.ts),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.agent: row for row in rows}

    async def prune(self, older_than_days: int = 7) -> int:
        """Heartbeat vira lixo rapido; mantemos apenas a janela util."""
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        stmt = select(orm.AgentRun).where(orm.AgentRun.timestamp < cutoff)
        rows = (await self._session.execute(stmt)).scalars().all()
        for row in rows:
            await self._session.delete(row)
        return len(rows)


class AuditLogRepository:
    """Append-only por contrato: esta classe nao expoe update nem delete."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        action: str,
        actor: str = "system",
        target: str | None = None,
        before: dict | None = None,
        after: dict | None = None,
        detail: str | None = None,
    ) -> None:
        self._session.add(
            orm.AuditLog(
                actor=actor,
                action=action,
                target=target,
                before=before or {},
                after=after or {},
                detail=detail,
            )
        )

    async def list(self, limit: int = 100, offset: int = 0) -> list[orm.AuditLog]:
        stmt = (
            select(orm.AuditLog).order_by(orm.AuditLog.timestamp.desc()).limit(limit).offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())


class PortfolioLedgerRepository:
    """Leitura do razao de fantasmas do Portfolio Agent, gravado no `audit_log`.

    O razao registra quanta quantidade o historico afirma e a exchange nao
    confirma, e quanto credito ja foi atribuido a ela. Precisa das tres
    propriedades ao mesmo tempo: durar entre reinicios, ser append-only e ser
    legivel por uma pessoa -- e o registro do ajuste E o razao dele.

    Mora numa classe propria, e nao em `AuditLogRepository`, porque aquela e
    append-only por contrato e expoe EXATAMENTE `append` e `list` (existe teste
    cobrando o conjunto de metodos). Ler linhas ja gravadas nao afrouxa esse
    contrato -- nada aqui faz update nem delete --, e a separacao mantem obvio,
    para quem le a outra classe, que o contrato dela nao mudou.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def movements(
        self, actions: Sequence[str], limit: int = 20_000
    ) -> list[orm.AuditLog]:
        """Movimentos do razao, em ordem cronologica."""
        stmt = (
            select(orm.AuditLog)
            .where(orm.AuditLog.action.in_(list(actions)))
            .order_by(orm.AuditLog.timestamp.asc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())


class OnChainMetricRepository:
    """Serie diaria de metricas on-chain."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(self, metric: str, points: list[tuple[date, float]]) -> int:
        """Grava ignorando dias que ja existem.

        Rebuscar a serie inteira e o caminho normal (o provedor nao oferece
        recorte por data), entao colisao na chave natural e o caso esperado.
        """
        if not points:
            return 0
        rows = [{"metric": metric, "day": day, "value": value} for day, value in points]
        dialect = self._session.bind.dialect.name if self._session.bind else "sqlite"
        if dialect == "sqlite":
            stmt = sqlite_insert(orm.OnChainMetric).values(rows).on_conflict_do_nothing()
        else:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(orm.OnChainMetric).values(rows).on_conflict_do_nothing()
        result = await self._session.execute(stmt)
        return result.rowcount or 0

    async def series(self, metric: str) -> list[tuple[date, float]]:
        stmt = (
            select(orm.OnChainMetric.day, orm.OnChainMetric.value)
            .where(orm.OnChainMetric.metric == metric)
            .order_by(orm.OnChainMetric.day.asc())
        )
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], float(row[1])) for row in rows]

    async def latest(self, metric: str) -> tuple[date, float] | None:
        stmt = (
            select(orm.OnChainMetric.day, orm.OnChainMetric.value)
            .where(orm.OnChainMetric.metric == metric)
            .order_by(orm.OnChainMetric.day.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        return (row[0], float(row[1])) if row else None


class NotificationConfigRepository:
    """Preferencias de alerta (liga/desliga e destinatarios). Linha unica."""

    ROW_ID = 1

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(self) -> orm.NotificationConfig:
        config = await self._session.get(orm.NotificationConfig, self.ROW_ID)
        if config is None:
            # Ambos desligados por padrao: um canal so passa a existir quando o
            # operador liga conscientemente e informa o destinatario.
            config = orm.NotificationConfig(id=self.ROW_ID)
            self._session.add(config)
            await self._session.flush()
        return config

    async def update(self, values: dict) -> orm.NotificationConfig:
        config = await self.get_or_create()
        for field in ("email_enabled", "email_to", "whatsapp_enabled", "whatsapp_to"):
            if field in values:
                setattr(config, field, values[field])
        return config


class TradingConfigRepository:
    """Linha unica com a configuracao de negocio vigente."""

    ROW_ID = 1

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> dict | None:
        """Valores gravados, ou `None` quando a linha ainda nao existe.

        Devolver `None` (e nao um dicionario vazio) permite ao chamador
        distinguir "nunca configurado" de "configurado com os padroes" -- e essa
        diferenca decide se a semeadura inicial deve rodar.
        """
        config = await self._session.get(orm.TradingConfig, self.ROW_ID)
        if config is None:
            return None
        return dict(config.values or {})

    async def save(self, values: dict) -> None:
        config = await self._session.get(orm.TradingConfig, self.ROW_ID)
        if config is None:
            self._session.add(orm.TradingConfig(id=self.ROW_ID, values=values))
            await self._session.flush()
            return
        config.values = values


class RiskConfigRepository:
    """Linha unica com os limites vigentes e o estado do circuit breaker."""

    ROW_ID = 1

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(self, defaults: dict) -> orm.RiskConfig:
        config = await self._session.get(orm.RiskConfig, self.ROW_ID)
        if config is None:
            config = orm.RiskConfig(id=self.ROW_ID, values=defaults)
            self._session.add(config)
            await self._session.flush()
        return config

    async def update_values(self, values: dict) -> orm.RiskConfig:
        config = await self.get_or_create(values)
        config.values = values
        return config

    async def trip_circuit_breaker(self, reason: str) -> None:
        config = await self.get_or_create({})
        config.circuit_breaker_active = True
        config.circuit_breaker_reason = reason
        config.circuit_breaker_tripped_at = datetime.now(UTC)

    async def reset_circuit_breaker(self) -> None:
        """Rearme e sempre manual e deliberado -- nunca automatico por tempo."""
        config = await self.get_or_create({})
        config.circuit_breaker_active = False
        config.circuit_breaker_reason = None
        config.circuit_breaker_tripped_at = None
