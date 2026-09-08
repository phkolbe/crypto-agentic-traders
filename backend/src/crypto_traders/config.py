"""Configuracao tipada e validada (Pydantic Settings).

Tudo vem de variaveis de ambiente / `.env`, que fica FORA do controle de versao.
Nenhuma chave de API mora em codigo, banco ou arquivo versionado.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .domain.enums import TradingMode

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "crypto_traders.db"

#: Exchanges suportadas nesta fase. A Coinbase esta na fase 5 do roadmap: o
#: adapter ccxt ja e generico, entao adiciona-la e reintroduzir a credencial e o
#: nome aqui -- nao ha codigo de agente a mudar.
SUPPORTED_EXCHANGES = frozenset({"binance"})


class ExchangeCredentials(BaseSettings):
    """Credenciais de uma exchange. `SecretStr` evita vazamento acidental em log/repr."""

    model_config = SettingsConfigDict(extra="ignore")

    api_key: SecretStr | None = None
    api_secret: SecretStr | None = None
    passphrase: SecretStr | None = None
    """Usado por algumas APIs da Coinbase; ignorado pela Binance."""

    @field_validator("api_key", "api_secret", "passphrase", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        """`BINANCE__API_KEY=` no `.env` significa "nao configurado", nao "vazio".

        Sem isso, uma chave em branco viraria `SecretStr('')` -- que e diferente
        de `None` -- e o sistema se consideraria autenticado, tentando enviar
        ordens com credencial vazia em vez de recusar a subida.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

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
    asset_whitelist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTC", "ETH", "SOL"]
    )
    symbol_whitelist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    )
    cooldown_seconds: int = Field(
        default=900,
        ge=0,
        description="Intervalo minimo entre duas ordens do mesmo par, evitando overtrading.",
    )
    mvrv_max_percentile: float = Field(
        default=1.0,
        gt=0,
        le=1.0,
        description=(
            "Percentil maximo do MVRV Z-Score para permitir NOVA exposicao. "
            "1.0 desliga o filtro. 0.80 significa 'nao comprar quando o mercado "
            "estiver mais caro que 80% dos dias observados'. Usa percentil e nao "
            "valor absoluto porque o limiar classico de 7 nao foi alcancado "
            "nenhuma vez desde 2022 -- um filtro fixo ali ficaria inerte."
        ),
    )

    @field_validator("asset_whitelist", "symbol_whitelist", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Aceita CSV alem de JSON, porque o `.env` e escrito a mao.

        Os campos usam `NoDecode` justamente para chegarem aqui como string
        crua: sem isso o pydantic-settings tentaria `json.loads` primeiro e
        falharia em `BTC,ETH` antes deste validador rodar.
        """
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
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # --- Mercado ----------------------------------------------------------
    exchange: str = "binance"
    quote_currency: str = "USDT"
    symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTC/USDT", "ETH/USDT"]
    )
    timeframe: str = "15m"
    candle_history_limit: int = 500

    # --- Descoberta automatica de pares ("mar aberto") --------------------
    # Vale apenas quando SYMBOLS esta vazio. Ver `discovery.py` para o que isso
    # muda na camada de seguranca da whitelist.
    discovery_min_quote_volume_24h: Decimal = Field(
        default=Decimal("50000000"),
        gt=0,
        description="Piso de liquidez em 24h. Dos 487 pares USDT da Binance, 312 movem <1M/dia.",
    )
    discovery_max_symbols: int = Field(
        default=8, ge=1, le=50, description="Teto de pares monitorados na descoberta."
    )
    discovery_exclude_assets: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Ativos a excluir alem das stablecoins ja excluidas por padrao.",
    )
    discovery_refresh_hours: int = Field(
        default=24, ge=1, description="Intervalo entre novas varreduras de mercado."
    )
    market_data_interval_seconds: int = 60
    portfolio_interval_seconds: int = 60
    strategies: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["ma_crossover", "rsi_reversion"]
    )
    signal_batch_window_seconds: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Janela para juntar sinais concorrentes antes do Risk Manager decidir "
            "por confianca. DESLIGADO por padrao (zero) porque a medicao nao "
            "sustentou o ganho: ver docs/SEGURANCA.md. Ligar so faz sentido apos "
            "validar que a confianca da estrategia prediz resultado."
        ),
    )

    # --- Paper trading ----------------------------------------------------
    paper_initial_balance: Decimal = Decimal("1000")
    paper_fee_pct: Decimal = Decimal("0.001")
    paper_slippage_pct: Decimal = Decimal("0.0005")

    # --- Notificacoes -----------------------------------------------------
    # SEGREDOS dos canais moram aqui, nunca no banco. Liga/desliga e
    # destinatarios ficam no banco e sao editaveis pela interface.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = True
    smtp_from: str = ""

    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: SecretStr | None = None
    whatsapp_template_name: str = ""
    whatsapp_template_language: str = "pt_BR"

    binance: ExchangeCredentials = Field(default_factory=ExchangeCredentials)
    risk: RiskSettings = Field(default_factory=RiskSettings)

    @field_validator(
        "symbols", "cors_origins", "strategies", "discovery_exclude_assets", mode="before"
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return value
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("smtp_password", "whatsapp_access_token", mode="before")
    @classmethod
    def _blank_secret_is_unset(cls, value: object) -> object:
        """`SMTP_PASSWORD=` no `.env` e ausencia, nao senha vazia.

        Mesmo motivo das chaves de exchange: `SecretStr('')` e diferente de
        `None`, e o canal se consideraria configurado para falhar no envio.
        """
        if isinstance(value, str) and not value.strip():
            return None
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

    @model_validator(mode="after")
    def _check_exchange_supported(self) -> Settings:
        if self.exchange.lower() not in SUPPORTED_EXCHANGES:
            raise ValueError(
                f"EXCHANGE='{self.exchange}' nao e suportada nesta fase. "
                f"Disponiveis: {', '.join(sorted(SUPPORTED_EXCHANGES))}."
            )
        return self

    @property
    def discovery_enabled(self) -> bool:
        """SYMBOLS vazio significa "descubra os pares", nao "nao faca nada".

        Deixar o sistema de pe sem observar nenhum mercado seria o pior estado
        possivel: heartbeat verde, dashboard atualizando e nenhuma operacao
        jamais -- parece saudavel e nao e.
        """
        return not self.symbols

    @property
    def is_live(self) -> bool:
        return self.trading_mode is TradingMode.LIVE

    @property
    def sends_real_orders(self) -> bool:
        """True quando ordens saem da maquina (LIVE ou TESTNET)."""
        return self.trading_mode in (TradingMode.LIVE, TradingMode.TESTNET)

    def credentials_for(self, exchange: str) -> ExchangeCredentials:
        """Credenciais da exchange, ou vazias se ela nao for suportada.

        Devolver vazio (em vez de falhar) mantem o caminho seguro: sem
        credencial, `build_broker` recusa sair do modo simulado.
        """
        return {"binance": self.binance}.get(exchange.lower(), ExchangeCredentials())


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Usado pelos testes, que trocam variaveis de ambiente entre casos."""
    get_settings.cache_clear()
