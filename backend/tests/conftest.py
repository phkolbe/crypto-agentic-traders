"""Fixtures compartilhadas."""

from __future__ import annotations

from decimal import Decimal

import pytest
from helpers import make_candles

from crypto_traders.config import RiskSettings, Settings
from crypto_traders.db.session import dispose_engine, init_db


@pytest.fixture(autouse=True)
def isolate_from_dotenv(monkeypatch):
    """Impede que os testes leiam o `.env` da maquina.

    `Settings` e `RiskSettings` carregam `.env` por padrao -- e correto em
    producao, desastroso em teste: a suite passaria ou falharia conforme a
    configuracao pessoal de quem roda. Foi exatamente o que aconteceu quando o
    `.env` local passou a ter limites diferentes dos padroes de fabrica, e 18
    testes quebraram sem que uma linha de codigo de producao tivesse mudado.
    """
    for model in (Settings, RiskSettings):
        monkeypatch.setitem(model.model_config, "env_file", None)


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
async def settings(tmp_path, risk_limits) -> Settings:
    """Configuracao apontando para um banco SQLite descartavel.

    O engine do SQLAlchemy e um singleton de modulo, entao precisa ser descartado
    entre os testes -- caso contrario o segundo teste continuaria escrevendo no
    banco do primeiro.
    """
    await dispose_engine()
    configured = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        symbols=["BTC/USDT"],
        strategies=["ma_crossover"],
        quote_currency="USDT",
        paper_initial_balance=Decimal("1000"),
        risk=risk_limits,
    )
    await init_db(configured)
    yield configured
    await dispose_engine()


@pytest.fixture
def candle_factory():
    return make_candles
