"""Integracao com exchanges: leitura de mercado e execucao de ordens."""

from __future__ import annotations

from ..config import Settings
from ..domain.enums import TradingMode
from ..logging_setup import get_logger
from .base import Broker, ExchangeError, InsufficientFunds, MarketDataSource
from .ccxt_adapter import CcxtExchange, build_market_data_source
from .paper import PaperBroker

log = get_logger(__name__)

__all__ = [
    "Broker",
    "CcxtExchange",
    "ExchangeError",
    "InsufficientFunds",
    "MarketDataSource",
    "PaperBroker",
    "build_broker",
    "build_market_data_source",
]


def build_broker(settings: Settings) -> Broker:
    """Escolhe o broker pelo modo de operacao.

    Ponto unico onde se decide se ordens saem ou nao da maquina. Em DRY_RUN --
    o padrao -- e sempre o `PaperBroker`, entao esquecer de configurar algo
    resulta em simulacao, nunca em uma ordem real inesperada.
    """
    if settings.trading_mode is TradingMode.DRY_RUN:
        return PaperBroker(
            quote_currency=settings.trading.quote_currency,
            initial_balance=settings.trading.paper_initial_balance,
            fee_pct=settings.trading.paper_fee_pct,
            slippage_pct=settings.trading.paper_slippage_pct,
        )

    credentials = settings.credentials_for(settings.exchange)
    if not credentials.configured:
        raise ExchangeError(
            f"TRADING_MODE={settings.trading_mode} exige credenciais da exchange "
            f"'{settings.exchange}' no .env."
        )

    testnet = settings.trading_mode is TradingMode.TESTNET
    log.warning(
        "broker.real_orders_enabled",
        exchange=settings.exchange,
        mode=str(settings.trading_mode),
        testnet=testnet,
    )
    return CcxtExchange(settings.exchange, credentials, testnet=testnet)
