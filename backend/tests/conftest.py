"""Fixtures compartilhadas."""

from __future__ import annotations

from decimal import Decimal

import pytest
from helpers import make_candles

from crypto_traders.config import RiskSettings


@pytest.fixture
def risk_limits() -> RiskSettings:
    """Limites previsiveis, independentes do `.env` da maquina."""
    return RiskSettings(
        max_order_notional=Decimal("100"),
        max_order_pct_portfolio=0.10,
        max_asset_exposure_pct=0.50,
        max_open_positions=3,
        min_order_notional=Decimal("10"),
        stop_loss_pct=0.03,
        take_profit_pct=0.06,
        daily_loss_limit_pct=0.05,
        weekly_loss_limit_pct=0.12,
        min_signal_confidence=0.55,
        asset_whitelist=["BTC", "ETH", "USDT"],
        symbol_whitelist=["BTC/USDT", "ETH/USDT"],
        cooldown_seconds=900,
    )


@pytest.fixture
def candle_factory():
    return make_candles
