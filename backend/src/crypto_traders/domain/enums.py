"""Enumeracoes do dominio.

Todos os valores sao strings estaveis: eles sao persistidos no banco e trafegam
na API, entao renomear um valor aqui e uma quebra de contrato.
"""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    """Lado de uma ordem ou negociacao."""

    BUY = "buy"
    SELL = "sell"


class SignalDirection(StrEnum):
    """Direcao sugerida por uma estrategia."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"
    """FLAT significa 'feche a posicao atual', nao 'abra uma posicao vendida'."""


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    FAILED = "failed"


class TradeOrigin(StrEnum):
    """Origem do registro de negociacao no historico consolidado."""

    AGENT = "agent"
    MANUAL = "manual"


class RiskDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RiskEventType(StrEnum):
    SIGNAL_EVALUATED = "signal_evaluated"
    CIRCUIT_BREAKER_TRIPPED = "circuit_breaker_tripped"
    CIRCUIT_BREAKER_RESET = "circuit_breaker_reset"
    LIMITS_UPDATED = "limits_updated"


class AgentState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


class TradingMode(StrEnum):
    """Modo de execucao. LIVE exige ativacao deliberada e explicita."""

    DRY_RUN = "dry_run"
    """Nenhuma ordem sai da maquina: tudo passa pelo PaperBroker."""

    TESTNET = "testnet"
    """Ordens reais contra a testnet/sandbox da exchange (dinheiro ficticio)."""

    LIVE = "live"
    """Ordens reais com dinheiro real."""


class ExchangeName(StrEnum):
    BINANCE = "binance"
    COINBASE = "coinbase"
    PAPER = "paper"
