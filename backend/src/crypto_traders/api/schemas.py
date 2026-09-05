"""Schemas da API.

Valores monetarios trafegam como **string**, nao float. JSON nao tem decimal, e
`0.1 + 0.2` em JavaScript da `0.30000000000000004` -- em uma tela que mostra
dinheiro, isso vira centavo errado. O frontend converte com precisao onde
precisa e formata para exibicao.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class MoneyModel(BaseModel):
    """Base que serializa todo `Decimal` como string."""

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", when_used="json")
    def _decimals_as_string(self, value: Any) -> Any:
        return str(value) if isinstance(value, Decimal) else value


# ---------------------------------------------------------------------------
class PositionOut(MoneyModel):
    asset: str
    quantity: Decimal
    average_price: Decimal | None = None
    current_price: Decimal | None = None
    market_value: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)


class PortfolioOut(MoneyModel):
    timestamp: datetime
    total_value: Decimal
    cash_value: Decimal
    positions_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    allocations: dict[str, float]
    positions: list[PositionOut]
    mode: str


class EquityPoint(MoneyModel):
    timestamp: datetime
    total_value: Decimal


class PortfolioSummary(MoneyModel):
    """Cards do topo do dashboard."""

    current: PortfolioOut | None
    change_24h_pct: float | None = None
    change_7d_pct: float | None = None
    change_since_start_pct: float | None = None
    trades_today: int = 0


# ---------------------------------------------------------------------------
class TradeOut(MoneyModel):
    id: str
    executed_at: datetime
    exchange: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    fee_currency: str | None
    origin: str
    strategy: str | None
    signal_id: str | None
    mode: str
    realized_pnl: Decimal | None
    notes: str | None


class TradePage(BaseModel):
    items: list[TradeOut]
    total: int
    limit: int
    offset: int


class ManualTradeIn(BaseModel):
    """Lancamento de uma operacao feita fora do sistema (app da exchange).

    Entra na mesma tabela `trades` com `origin=manual`, mantendo um historico
    unico -- e alimentando o calculo de preco medio junto com as operacoes dos
    agentes.
    """

    executed_at: datetime
    exchange: str = Field(min_length=1, max_length=32)
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,12}/[A-Z0-9]{2,12}$")
    side: str = Field(pattern="^(buy|sell)$")
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Field(default=Decimal(0), ge=0)
    fee_currency: str | None = None
    notes: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
class SignalOut(MoneyModel):
    id: str
    created_at: datetime
    exchange: str
    symbol: str
    timeframe: str
    strategy: str
    direction: str
    confidence: float
    reason: str
    reference_price: Decimal
    indicators: dict[str, Any]


class RiskEventOut(MoneyModel):
    id: str
    created_at: datetime
    event_type: str
    signal_id: str | None
    decision: str | None
    reasons: list[str]
    approved_quantity: Decimal | None
    approved_notional: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    snapshot: dict[str, Any]


class OrderOut(MoneyModel):
    id: str
    created_at: datetime
    client_order_id: str
    exchange_order_id: str | None
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    notional: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    status: str
    filled_quantity: Decimal
    average_price: Decimal | None
    strategy: str | None
    mode: str
    error: str | None


class AuditEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    timestamp: datetime
    actor: str
    action: str
    target: str | None
    detail: str | None
    before: dict[str, Any]
    after: dict[str, Any]


# ---------------------------------------------------------------------------
class RiskConfigOut(MoneyModel):
    max_order_notional: Decimal
    max_order_pct_portfolio: float
    max_asset_exposure_pct: float
    max_open_positions: int
    min_order_notional: Decimal
    stop_loss_pct: float
    take_profit_pct: float
    daily_loss_limit_pct: float
    weekly_loss_limit_pct: float
    min_signal_confidence: float
    asset_whitelist: list[str]
    symbol_whitelist: list[str]
    cooldown_seconds: int
    circuit_breaker_active: bool = False
    circuit_breaker_reason: str | None = None


class RiskConfigIn(BaseModel):
    """Atualizacao parcial: apenas os campos enviados sao alterados."""

    model_config = ConfigDict(extra="forbid")

    max_order_notional: Decimal | None = Field(default=None, gt=0)
    max_order_pct_portfolio: float | None = Field(default=None, gt=0, le=1)
    max_asset_exposure_pct: float | None = Field(default=None, gt=0, le=1)
    max_open_positions: int | None = Field(default=None, ge=1)
    min_order_notional: Decimal | None = Field(default=None, gt=0)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=1)
    take_profit_pct: float | None = Field(default=None, gt=0, lt=5)
    daily_loss_limit_pct: float | None = Field(default=None, gt=0, le=1)
    weekly_loss_limit_pct: float | None = Field(default=None, gt=0, le=1)
    min_signal_confidence: float | None = Field(default=None, ge=0, le=1)
    asset_whitelist: list[str] | None = None
    symbol_whitelist: list[str] | None = None
    cooldown_seconds: int | None = Field(default=None, ge=0)

    confirm: bool = Field(
        default=False,
        description="Confirmacao explicita: alterar limites afeta dinheiro real.",
    )


# ---------------------------------------------------------------------------
class AgentStatusOut(BaseModel):
    state: str
    running: bool
    paused: bool
    last_heartbeat: datetime | None
    stale: bool
    last_error: str | None


class HealthOut(BaseModel):
    mode: str
    exchange: str
    symbols: list[str]
    strategies: list[str]
    started_at: datetime | None
    circuit_breaker_active: bool
    agents: dict[str, AgentStatusOut]


class StrategyOut(BaseModel):
    name: str
    description: str
    active: bool


# ---------------------------------------------------------------------------
class BacktestIn(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,12}/[A-Z0-9]{2,12}$")
    strategy: str
    timeframe: str = "1h"
    days: int = Field(default=90, ge=1, le=365)
    initial_balance: Decimal = Field(default=Decimal("1000"), gt=0)


class BacktestOut(BaseModel):
    summary: dict[str, Any]
    equity_curve: list[dict[str, Any]]
    trades: list[dict[str, Any]]
