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
    max_order_notional: Decimal | None
    max_order_pct_portfolio: float
    max_asset_exposure_pct: float
    max_open_positions: int | None
    min_order_notional: Decimal
    stop_loss_pct: float
    take_profit_pct: float
    daily_loss_limit_pct: float
    weekly_loss_limit_pct: float
    min_signal_confidence: float
    asset_whitelist: list[str]
    symbol_whitelist: list[str]
    cooldown_seconds: int
    mvrv_max_percentile: float
    authorized_capital: Decimal | None = None
    circuit_breaker_active: bool = False
    circuit_breaker_reason: str | None = None


class RiskConfigIn(BaseModel):
    """Atualizacao parcial: apenas os campos enviados sao alterados."""

    model_config = ConfigDict(extra="forbid")

    max_order_notional: Decimal | None = Field(default=None, gt=0)
    clear_max_order_notional: bool = Field(
        default=False,
        description=(
            "Remove o teto absoluto por ordem, deixando o percentual mandar. "
            "Existe como campo proprio porque num PUT parcial `null` significa "
            "'nao enviei', e nao 'apague'."
        ),
    )
    max_order_pct_portfolio: float | None = Field(default=None, gt=0, le=1)
    max_asset_exposure_pct: float | None = Field(default=None, gt=0, le=1)
    max_open_positions: int | None = Field(default=None, ge=1)
    clear_max_open_positions: bool = Field(
        default=False, description="Remove o limite de posicoes; o caixa passa a limitar."
    )
    min_order_notional: Decimal | None = Field(default=None, gt=0)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=1)
    take_profit_pct: float | None = Field(default=None, gt=0, lt=5)
    daily_loss_limit_pct: float | None = Field(default=None, gt=0, le=1)
    weekly_loss_limit_pct: float | None = Field(default=None, gt=0, le=1)
    min_signal_confidence: float | None = Field(default=None, ge=0, le=1)
    asset_whitelist: list[str] | None = None
    symbol_whitelist: list[str] | None = None
    cooldown_seconds: int | None = Field(default=None, ge=0)
    mvrv_max_percentile: float | None = Field(
        default=None,
        gt=0,
        le=1,
        description="1.0 desliga o filtro de regime. Ver docs/SEGURANCA.md antes de baixar.",
    )
    authorized_capital: Decimal | None = Field(
        default=None,
        ge=0,
        description="Capital que o sistema pode por para trabalhar, na moeda de cotacao.",
    )
    clear_authorized_capital: bool = Field(
        default=False,
        description=(
            "Remove o portao de autorizacao: todo o patrimonio, INCLUSIVE "
            "depositos futuros, fica disponivel sem novo aval."
        ),
    )

    confirm: bool = Field(
        default=False,
        description="Confirmacao explicita: alterar limites afeta dinheiro real.",
    )


class CapitalStatusOut(MoneyModel):
    """Quanto capital esta autorizado, e quanto esta parado esperando aval."""

    total_value: Decimal
    authorized_capital: Decimal | None
    unauthorized_value: Decimal
    gate_active: bool
    quote_currency: str


# ---------------------------------------------------------------------------
class TradingConfigOut(MoneyModel):
    """Configuracao de negocio vigente. Vem do banco, nunca do `.env`."""

    quote_currency: str
    symbols: list[str]
    timeframe: str
    strategies: list[str]
    candle_history_limit: int
    market_data_interval_seconds: int
    portfolio_interval_seconds: int
    signal_batch_window_seconds: float
    discovery_min_quote_volume_24h: Decimal
    discovery_max_symbols: int
    discovery_exclude_assets: list[str]
    discovery_refresh_hours: int
    paper_initial_balance: Decimal
    paper_fee_pct: Decimal
    paper_slippage_pct: Decimal

    discovery_enabled: bool
    """Derivado: `symbols` vazio liga a descoberta automatica."""

    available_strategies: dict[str, str] = Field(default_factory=dict)
    """Nome -> descricao, para a interface montar a lista sem adivinhar."""


class TradingConfigIn(BaseModel):
    """Atualizacao parcial: apenas os campos enviados sao alterados.

    `symbols` aceita lista vazia -- e como se liga a descoberta automatica --
    entao o "nao enviado" precisa ser `None`, e nao `[]`.
    """

    model_config = ConfigDict(extra="forbid")

    quote_currency: str | None = Field(default=None, min_length=2)
    symbols: list[str] | None = None
    timeframe: str | None = Field(default=None, min_length=2)
    strategies: list[str] | None = None
    candle_history_limit: int | None = Field(default=None, ge=50, le=1000)
    market_data_interval_seconds: int | None = Field(default=None, ge=5)
    portfolio_interval_seconds: int | None = Field(default=None, ge=5)
    signal_batch_window_seconds: float | None = Field(default=None, ge=0)
    discovery_min_quote_volume_24h: Decimal | None = Field(default=None, gt=0)
    discovery_max_symbols: int | None = Field(default=None, ge=1, le=50)
    discovery_exclude_assets: list[str] | None = None
    discovery_refresh_hours: int | None = Field(default=None, ge=1)
    paper_initial_balance: Decimal | None = Field(default=None, gt=0)
    paper_fee_pct: Decimal | None = Field(default=None, ge=0, lt=1)
    paper_slippage_pct: Decimal | None = Field(default=None, ge=0, lt=1)

    confirm: bool = Field(
        default=False,
        description=(
            "Confirmacao explicita. Trocar pares, moeda de cotacao ou "
            "estrategias muda o que o sistema negocia com dinheiro real."
        ),
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
    symbols_source: str = "configurado"
    """`configurado` (SYMBOLS no .env) ou `descoberta` (varredura de mercado)."""

    strategies: list[str]
    started_at: datetime | None
    circuit_breaker_active: bool
    sizing_feasible: bool = True
    """False quando patrimonio e limites tornam qualquer ordem impossivel."""

    sizing_detail: str | None = None
    agents: dict[str, AgentStatusOut]


class StrategyOut(BaseModel):
    name: str
    description: str
    active: bool


# ---------------------------------------------------------------------------
class NotificationChannelStatus(BaseModel):
    """Se os SEGREDOS do canal estao no `.env`. Nunca devolve o valor deles."""

    configured: bool
    missing_settings: list[str] = Field(default_factory=list)


class NotificationConfigOut(BaseModel):
    email_enabled: bool
    email_to: str | None
    whatsapp_enabled: bool
    whatsapp_to: str | None
    email: NotificationChannelStatus
    whatsapp: NotificationChannelStatus


class NotificationConfigIn(BaseModel):
    """Atualizacao parcial dos canais. Segredos nao passam por aqui."""

    model_config = ConfigDict(extra="forbid")

    email_enabled: bool | None = None
    email_to: str | None = Field(default=None, max_length=320)
    whatsapp_enabled: bool | None = None
    whatsapp_to: str | None = Field(
        default=None,
        max_length=32,
        description="Numero em formato internacional, ex.: 5511999999999",
    )


class NotificationTestOut(BaseModel):
    results: list[dict[str, Any]]
    detail: str = ""


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
