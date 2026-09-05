"""Configuracao tipada e validada (Pydantic Settings).

Tudo vem de variaveis de ambiente / `.env`, que fica FORA do controle de versao.
Nenhuma chave de API mora em codigo, banco ou arquivo versionado.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.enums import TradingMode

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "crypto_traders.db"


class ExchangeCredentials(BaseSettings):
    """Credenciais de uma exchange. `SecretStr` evita vazamento acidental em log/repr."""

    model_config = SettingsConfigDict(extra="ignore")

    api_key: SecretStr | None = None
    api_secret: SecretStr | None = None
    passphrase: SecretStr | None = None
    """Usado por algumas APIs da Coinbase; ignorado pela Binance."""

    @property
    def configured(self) -> bool:
        return self.api_key is not None and self.api_secret is not None


class RiskSettings(BaseSettings):
    """Limites do Risk Manager Agent.

    Os defaults sao deliberadamente conservadores: se o operador esquecer de
    configurar, o sistema erra para o lado de arriscar de menos.
    """

    model_config = SettingsConfigDict(env_prefix="RISK_", env_file=".env", extra="ignore")

    max_order_notional: Decimal = Field(
        default=Decimal("50"),
        gt=0,
        description="Valor maximo absoluto de uma unica ordem, na moeda de cotacao.",
    )
    max_order_pct_portfolio: float = Field(
        default=0.02,
        gt=0,
        le=1,
        description="Fracao maxima do portfolio comprometida em uma unica ordem.",
    )
    max_asset_exposure_pct: float = Field(
        default=0.30,
        gt=0,
        le=1,
        description="Fracao maxima do portfolio concentrada em um unico ativo.",
    )
    max_open_positions: int = Field(default=5, ge=1)
    min_order_notional: Decimal = Field(
        default=Decimal("10"),
        gt=0,
        description="Abaixo disso a ordem e rejeitada: a taxa come o resultado.",
    )
    stop_loss_pct: float = Field(default=0.03, gt=0, lt=1)
    take_profit_pct: float = Field(default=0.06, gt=0, lt=5)
    daily_loss_limit_pct: float = Field(
        default=0.05,
        gt=0,
        le=1,
        description="Queda diaria do portfolio que dispara o circuit breaker.",
    )
    weekly_loss_limit_pct: float = Field(default=0.12, gt=0, le=1)
    min_signal_confidence: float = Field(default=0.55, ge=0, le=1)
    asset_whitelist: list[str] = Field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    symbol_whitelist: list[str] = Field(
        default_factory=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    )
    cooldown_seconds: int = Field(
        default=900,
        ge=0,
        description="Intervalo minimo entre duas ordens do mesmo par, evitando overtrading.",
    )

    @field_validator("asset_whitelist", "symbol_whitelist", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Aceita CSV alem de JSON, porque o `.env` e escrito a mao."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return value
            return [item.strip().upper() for item in stripped.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _check_coherence(self) -> RiskSettings:
        if self.min_order_notional > self.max_order_notional:
            raise ValueError(
                "RISK_MIN_ORDER_NOTIONAL nao pode ser maior que RISK_MAX_ORDER_NOTIONAL"
            )
        if self.take_profit_pct <= self.stop_loss_pct:
            raise ValueError(
                "RISK_TAKE_PROFIT_PCT deve ser maior que RISK_STOP_LOSS_PCT "
                "(caso contrario a estrategia tem esperanca matematica negativa)"
            )
        if self.weekly_loss_limit_pct < self.daily_loss_limit_pct:
            raise ValueError("RISK_WEEKLY_LOSS_LIMIT_PCT deve ser >= RISK_DAILY_LOSS_LIMIT_PCT")
        return self


class Settings(BaseSettings):
    """Configuracao global da aplicacao."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_name: str = "crypto-agentic-traders"
    environment: str = "local"
    log_level: str = "INFO"
    log_json: bool = False

    # --- Modo de operacao -------------------------------------------------
    trading_mode: TradingMode = TradingMode.DRY_RUN
    """Padrao de fabrica e DRY_RUN. Ir para LIVE exige duas confirmacoes."""

    live_trading_confirmed: bool = False
    """Segunda tranca: TRADING_MODE=live sozinho nao basta."""

    # --- Persistencia -----------------------------------------------------
    database_url: str = Field(default="")
    """Vazio => SQLite local. Troque por postgresql+asyncpg ao subir o docker compose."""

    db_echo: bool = False

    # --- Event bus --------------------------------------------------------
    event_bus: str = Field(default="memory", pattern="^(memory|redis)$")
    redis_url: str = "redis://localhost:6379/0"

    # --- API --------------------------------------------------------------
    api_host: str = "127.0.0.1"
    """Nunca 0.0.0.0 por padrao: o backend so escuta em localhost."""

    api_port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # --- Mercado ----------------------------------------------------------
    exchange: str = "binance"
    quote_currency: str = "USDT"
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])
    timeframe: str = "15m"
    candle_history_limit: int = 500
    market_data_interval_seconds: int = 60
    portfolio_interval_seconds: int = 60
    strategies: list[str] = Field(default_factory=lambda: ["ma_crossover", "rsi_reversion"])

    # --- Paper trading ----------------------------------------------------
    paper_initial_balance: Decimal = Decimal("1000")
    paper_fee_pct: Decimal = Decimal("0.001")
    paper_slippage_pct: Decimal = Decimal("0.0005")

    # --- Notificacoes -----------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    binance: ExchangeCredentials = Field(default_factory=ExchangeCredentials)
    coinbase: ExchangeCredentials = Field(default_factory=ExchangeCredentials)
    risk: RiskSettings = Field(default_factory=RiskSettings)

    @field_validator("symbols", "cors_origins", "strategies", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return value
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _resolve_database_url(self) -> Settings:
        if not self.database_url:
            DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            object.__setattr__(
                self, "database_url", f"sqlite+aiosqlite:///{DEFAULT_DB_PATH.as_posix()}"
            )
        return self

    @model_validator(mode="after")
    def _guard_live_trading(self) -> Settings:
        """Impede que LIVE seja ligado por acidente (typo no .env, copiar/colar)."""
        if self.trading_mode is TradingMode.LIVE and not self.live_trading_confirmed:
            raise ValueError(
                "TRADING_MODE=live exige tambem LIVE_TRADING_CONFIRMED=true. "
                "Essa dupla confirmacao e proposital: operar com dinheiro real "
                "nunca pode ser o resultado de um unico caractere trocado."
            )
        return self

    @property
    def is_live(self) -> bool:
        return self.trading_mode is TradingMode.LIVE

    @property
    def sends_real_orders(self) -> bool:
        """True quando ordens saem da maquina (LIVE ou TESTNET)."""
        return self.trading_mode in (TradingMode.LIVE, TradingMode.TESTNET)

    def credentials_for(self, exchange: str) -> ExchangeCredentials:
        return {"binance": self.binance, "coinbase": self.coinbase}.get(
            exchange.lower(), ExchangeCredentials()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Usado pelos testes, que trocam variaveis de ambiente entre casos."""
    get_settings.cache_clear()
