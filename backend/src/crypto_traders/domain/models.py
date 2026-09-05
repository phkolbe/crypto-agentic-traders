"""Modelos de dominio trafegados no event bus.

Regra de ouro: valores monetarios e quantidades usam `Decimal`, nunca `float`.
Erro de arredondamento em ponto flutuante vira dinheiro perdido de verdade.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    ExchangeName,
    OrderStatus,
    OrderType,
    RiskDecision,
    Side,
    SignalDirection,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class DomainModel(BaseModel):
    """Base imutavel para tudo que trafega no bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Candle(DomainModel):
    """Candle OHLCV normalizado, independente de exchange."""

    exchange: ExchangeName
    symbol: str
    timeframe: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    closed: bool = True
    """Candles em formacao (`closed=False`) nunca devem disparar sinais."""


class Ticker(DomainModel):
    exchange: ExchangeName
    symbol: str
    price: Decimal
    timestamp: datetime = Field(default_factory=_now)


class IndicatorSnapshot(DomainModel):
    """Valores dos indicadores no instante da decisao.

    Guardado junto do sinal para que qualquer trade possa ser auditado depois
    ("por que o agente comprou aqui?") sem recalcular nada.
    """

    values: dict[str, float | None] = Field(default_factory=dict)


class Signal(DomainModel):
    """Sinal produzido por uma estrategia. NUNCA vira ordem sem passar pelo Risk Manager."""

    id: str = Field(default_factory=_new_id)
    created_at: datetime = Field(default_factory=_now)
    exchange: ExchangeName
    symbol: str
    timeframe: str
    strategy: str
    direction: SignalDirection
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    reference_price: Decimal
    indicators: IndicatorSnapshot = Field(default_factory=IndicatorSnapshot)


class OrderRequest(DomainModel):
    """Ordem ja aprovada pelo Risk Manager, pronta para execucao.

    Só o Risk Manager Agent constroi este objeto. O Execution Agent se recusa a
    executar qualquer coisa que nao seja um `OrderRequest` com `risk_event_id`.
    """

    id: str = Field(default_factory=_new_id)
    created_at: datetime = Field(default_factory=_now)
    client_order_id: str
    """ID de idempotencia enviado a exchange: garante que um retry nao duplique a ordem."""

    signal_id: str | None
    risk_event_id: str
    exchange: ExchangeName
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal = Field(gt=0)
    price: Decimal | None = None
    """Obrigatorio para ordens LIMIT, ignorado em MARKET."""

    notional: Decimal = Field(gt=0)
    """Valor aproximado da operacao na moeda de cotacao, no momento da aprovacao."""

    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    strategy: str | None = None


class OrderResult(DomainModel):
    """Resposta da exchange (ou do PaperBroker) para uma ordem enviada."""

    order_request_id: str
    client_order_id: str
    exchange_order_id: str | None
    status: OrderStatus
    filled_quantity: Decimal = Decimal(0)
    average_price: Decimal | None = None
    fee: Decimal = Decimal(0)
    fee_currency: str | None = None
    raw: dict = Field(default_factory=dict)
    error: str | None = None
    timestamp: datetime = Field(default_factory=_now)


class RiskAssessment(DomainModel):
    """Veredito do Risk Manager sobre um sinal. Persistido sempre, aprovado ou nao."""

    id: str = Field(default_factory=_new_id)
    created_at: datetime = Field(default_factory=_now)
    signal_id: str | None
    decision: RiskDecision
    reasons: list[str] = Field(default_factory=list)
    """Vazio quando aprovado sem ressalvas; contem cada regra violada quando rejeitado."""

    approved_quantity: Decimal | None = None
    approved_notional: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    snapshot: dict = Field(default_factory=dict)
    """Estado do portfolio/limites no momento da avaliacao, para auditoria."""


class Position(DomainModel):
    exchange: ExchangeName
    asset: str
    quantity: Decimal
    average_price: Decimal | None = None
    current_price: Decimal | None = None

    @property
    def market_value(self) -> Decimal:
        if self.current_price is None:
            return Decimal(0)
        return self.quantity * self.current_price

    @property
    def unrealized_pnl(self) -> Decimal:
        if self.current_price is None or self.average_price is None:
            return Decimal(0)
        return (self.current_price - self.average_price) * self.quantity


class PortfolioSnapshot(DomainModel):
    id: str = Field(default_factory=_new_id)
    timestamp: datetime = Field(default_factory=_now)
    total_value: Decimal
    cash_value: Decimal
    positions_value: Decimal
    realized_pnl: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)
    allocations: dict[str, float] = Field(default_factory=dict)
    """Percentual do portfolio por ativo, ex.: {"BTC": 0.42, "USDT": 0.58}."""

    positions: list[Position] = Field(default_factory=list)


class Heartbeat(DomainModel):
    agent: str
    state: str
    timestamp: datetime = Field(default_factory=_now)
    detail: str | None = None
