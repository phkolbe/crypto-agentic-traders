"""Modelo de dados (SQLAlchemy 2.0).

Portabilidade SQLite <-> PostgreSQL/TimescaleDB e requisito: os tipos usados aqui
existem nos dois. Valores monetarios usam `Numeric` (nunca `Float`), preservando
precisao decimal exata no Postgres; no SQLite a camada de repositorio converte
de volta para `Decimal` na leitura.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Dialect,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Precisao ampla o suficiente para satoshis (8 casas) e tokens com 18 casas.
MONEY_PRECISION = Numeric(38, 18)


class Money(TypeDecorator):
    """Valor monetario exato, em SQLite e em PostgreSQL.

    O SQLite nao tem tipo decimal nativo: um `Numeric` la vira `REAL`, ou seja,
    float64. Isso e suficiente para estragar dinheiro de verdade -- gravar
    `0.4` e ler `0.400000000000000022` foi exatamente o que aconteceu antes
    deste tipo existir, e o erro se propaga por PnL, custo medio e exportacao
    fiscal.

    A solucao e guardar o decimal como TEXTO no SQLite (representacao exata) e
    como `NUMERIC` no PostgreSQL, que tem decimal de verdade. A conversao para
    `Decimal` acontece na leitura, nos dois casos.

    Consequencia a lembrar: no SQLite estas colunas sao texto, entao `ORDER BY`
    e comparacoes numericas em SQL sobre elas nao sao confiaveis. Nenhuma query
    do projeto faz isso -- agregacoes de dinheiro sao somadas em Python.
    """

    impl = Numeric
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(64))
        return dialect.type_descriptor(MONEY_PRECISION)

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if dialect.name == "sqlite":
            # `format(..., "f")` evita notacao cientifica: `1E-8` como texto
            # voltaria como string nao comparavel e confundiria a leitura.
            return format(number, "f")
        return number

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


MONEY = Money()


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Candle(Base):
    """Serie temporal de candles OHLCV.

    No Postgres esta tabela vira uma hypertable do TimescaleDB (ver
    `docker/timescale_init.sql`); no SQLite continua uma tabela comum com indice.
    """

    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", "timeframe", "open_time", name="uq_candle"),
        Index("ix_candles_lookup", "exchange", "symbol", "timeframe", "open_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[Decimal] = mapped_column(MONEY)
    high: Mapped[Decimal] = mapped_column(MONEY)
    low: Mapped[Decimal] = mapped_column(MONEY)
    close: Mapped[Decimal] = mapped_column(MONEY)
    volume: Mapped[Decimal] = mapped_column(MONEY)


class Signal(Base):
    """Sinal gerado por uma estrategia, aprovado ou nao."""

    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    strategy: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[float] = mapped_column(Numeric(5, 4))
    reason: Mapped[str] = mapped_column(Text)
    reference_price: Mapped[Decimal] = mapped_column(MONEY)
    indicators: Mapped[dict] = mapped_column(JSON, default=dict)


class RiskEvent(Base):
    """Toda decisao do Risk Manager, incluindo rejeicoes e circuit breaker.

    Esta tabela e a resposta a pergunta "por que o agente (nao) operou?".
    """

    __tablename__ = "risk_events"
    __table_args__ = (Index("ix_risk_events_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    event_type: Mapped[str] = mapped_column(String(32))
    signal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    approved_quantity: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    approved_notional: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)


class Order(Base):
    """Ordem enviada (ou simulada) e seu ciclo de vida na exchange."""

    __tablename__ = "orders"
    __table_args__ = (
        # Idempotencia: a exchange e o banco concordam que este ID e unico.
        UniqueConstraint("client_order_id", name="uq_order_client_id"),
        Index("ix_orders_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    client_order_id: Mapped[str] = mapped_column(String(64))
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_event_id: Mapped[str] = mapped_column(String(32))
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    notional: Mapped[Decimal] = mapped_column(MONEY)
    stop_loss: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    status: Mapped[str] = mapped_column(String(24))
    filled_quantity: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    average_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(16))
    """dry_run | testnet | live -- registrado por ordem, para o historico nunca ser ambiguo."""

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class Trade(Base):
    """Historico consolidado: negociacoes dos agentes E lancamentos manuais.

    Uma unica tabela para os dois casos, distinguidos por `origin`. Isso mantem o
    dashboard, os filtros e a exportacao fiscal simples, sem UNION entre tabelas.
    """

    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_executed", "executed_at"),
        Index("ix_trades_symbol", "symbol"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    price: Mapped[Decimal] = mapped_column(MONEY)
    notional: Mapped[Decimal] = mapped_column(MONEY)
    fee: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    fee_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    origin: Mapped[str] = mapped_column(String(16))
    """agent | manual"""

    order_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    signal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="dry_run")
    realized_pnl: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class PortfolioSnapshot(Base):
    """Serie temporal do patrimonio, que alimenta o grafico do dashboard."""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (Index("ix_snapshots_ts", "timestamp"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    total_value: Mapped[Decimal] = mapped_column(MONEY)
    cash_value: Mapped[Decimal] = mapped_column(MONEY)
    positions_value: Mapped[Decimal] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    allocations: Mapped[dict] = mapped_column(JSON, default=dict)
    positions: Mapped[list] = mapped_column(JSON, default=list)
    mode: Mapped[str] = mapped_column(String(16), default="dry_run")


class AgentRun(Base):
    """Heartbeat e estado de cada agente ao longo do tempo."""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_agent_ts", "agent", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    """Log append-only de acoes sensiveis.

    A aplicacao so faz INSERT aqui -- nao existe caminho de UPDATE nem DELETE no
    codigo. Mudanca de limite de risco, pausa de agente e ativacao de LIVE
    passam obrigatoriamente por esta tabela.
    """

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_ts", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class OnChainMetric(Base):
    """Serie diaria de metrica on-chain (hoje: MVRV Z-Score).

    Cacheada porque o dado vem de provedor externo, e nao da exchange: guardar
    permite backtest sem rede e evita depender de um site de terceiros estar no
    ar para o sistema decidir. O passado da serie e imutavel, entao gravar uma
    vez e suficiente.
    """

    __tablename__ = "onchain_metrics"
    __table_args__ = (
        UniqueConstraint("metric", "day", name="uq_onchain_metric_day"),
        Index("ix_onchain_metric_day", "metric", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric: Mapped[str] = mapped_column(String(32))
    day: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    """Indicador normalizado, nao dinheiro -- `Float` serve e mantem a query simples."""

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationConfig(Base):
    """Canais de alerta: liga/desliga e destinatarios, editaveis pela interface.

    Linha unica (`id=1`). Guarda apenas o que NAO e segredo -- toggles, endereco
    de e-mail e numero de WhatsApp. Senha de SMTP e token da Meta continuam
    exclusivamente no `.env`, porque credencial em banco contraria a premissa de
    seguranca do projeto e um backup do banco passaria a vazar acesso.
    """

    __tablename__ = "notification_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    email_to: Mapped[str | None] = mapped_column(String(320), nullable=True)
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    whatsapp_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """Numero no formato internacional, so digitos: 5511999999999."""


class RiskConfig(Base):
    """Limites de risco vigentes, editaveis pela interface sem mexer em codigo.

    Linha unica (`id=1`). O `.env` fornece os valores iniciais; a partir dai esta
    tabela e a fonte da verdade, e toda alteracao gera um `AuditLog`.
    """

    __tablename__ = "risk_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    circuit_breaker_active: Mapped[bool] = mapped_column(Boolean, default=False)
    circuit_breaker_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    circuit_breaker_tripped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
