"""Configuracao da aplicacao, separada em duas naturezas que nao se misturam.

**Ambiente** (`Settings`) vem de variaveis de ambiente / `.env`, que fica fora do
controle de versao. Descreve a *instalacao*: credenciais, banco, event bus,
host da API, canais de alerta, e as travas de seguranca que precisam estar fora
do alcance do navegador. Nenhuma chave de API mora em codigo ou banco.

**Negocio** (`TradingSettings`, `RiskSettings`) vive no **banco** e e editada pela
aplicacao web. Responde "o que eu quero negociar e com quanto risco": pares,
timeframe, estrategias, cadencia, limites de risco. Estes modelos sao `BaseModel`
puros -- deliberadamente **nao** leem variaveis de ambiente, para que exista uma
unica fonte da verdade.

A fronteira nao e estetica. Quando os limites de risco viviam nos dois lugares, o
banco vencia em silencio: o `.env` dizia ordem maxima de 7 USDT enquanto o
sistema operava com 50. Configuracao duplicada nao e redundancia, e uma segunda
verdade sobre dinheiro real.

Duas fronteiras que exigiram decisao explicita, e o porque:

- `TRADING_MODE` e `LIVE_TRADING_CONFIRMED` sao **ambiente**. Ligar dinheiro real
  deve exigir editar um arquivo no servidor e reiniciar o processo -- duas travas
  que um clique no navegador nao alcanca.
- `EXCHANGE` e **ambiente** (esta amarrada a qual credencial existe no arquivo),
  mas `QUOTE_CURRENCY` e **negocio** (define o universo de pares negociaveis).
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .domain.enums import TradingMode

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "crypto_traders.db"

#: Exchanges suportadas nesta fase. A Coinbase esta na fase 5 do roadmap: o
#: adapter ccxt ja e generico, entao adiciona-la e reintroduzir a credencial e o
#: nome aqui -- nao ha codigo de agente a mudar.
SUPPORTED_EXCHANGES = frozenset({"binance"})

#: Chaves de negocio que ANTES moravam no `.env` e hoje moram no banco.
#:
#: Encontrar uma delas no ambiente e erro de configuracao, nao preferencia: quem
#: a escreveu espera que ela tenha efeito, e ela nao tem. Falhar alto aqui e a
#: licao direta do episodio em que o `.env` pedia ordem maxima de 7 e o sistema
#: operava com 50 sem avisar ninguem.
RETIRED_ENV_KEYS = frozenset(
    {
        # Mercado e universo
        "QUOTE_CURRENCY",
        "SYMBOLS",
        "TIMEFRAME",
        "STRATEGIES",
        "CANDLE_HISTORY_LIMIT",
        # Cadencia
        "MARKET_DATA_INTERVAL_SECONDS",
        "PORTFOLIO_INTERVAL_SECONDS",
        "SIGNAL_BATCH_WINDOW_SECONDS",
        # Descoberta automatica
        "DISCOVERY_MIN_QUOTE_VOLUME_24H",
        "DISCOVERY_MAX_SYMBOLS",
        "DISCOVERY_EXCLUDE_ASSETS",
        "DISCOVERY_REFRESH_HOURS",
        # Paper trading
        "PAPER_INITIAL_BALANCE",
        "PAPER_FEE_PCT",
        "PAPER_SLIPPAGE_PCT",
        # Limites de risco (todo o prefixo RISK_)
        "RISK_MAX_ORDER_NOTIONAL",
        "RISK_MAX_ORDER_PCT_PORTFOLIO",
        "RISK_MAX_ASSET_EXPOSURE_PCT",
        "RISK_MAX_OPEN_POSITIONS",
        "RISK_MIN_ORDER_NOTIONAL",
        "RISK_STOP_LOSS_PCT",
        "RISK_TAKE_PROFIT_PCT",
        "RISK_DAILY_LOSS_LIMIT_PCT",
        "RISK_WEEKLY_LOSS_LIMIT_PCT",
        "RISK_MIN_SIGNAL_CONFIDENCE",
        "RISK_ASSET_WHITELIST",
        "RISK_SYMBOL_WHITELIST",
        "RISK_COOLDOWN_SECONDS",
        "RISK_MVRV_MAX_PERCENTILE",
        "RISK_AUTHORIZED_CAPITAL",
    }
)


def _split_csv_value(value: object, *, upper: bool = False) -> object:
    """Aceita CSV alem de JSON.

    A aplicacao web envia listas como JSON, mas um formulario de texto e o
    caminho natural para digitar `BTC,ETH` -- aceitar os dois evita transformar
    a virgula em erro de validacao na cara do usuario.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            return value
        itens = [item.strip() for item in stripped.split(",") if item.strip()]
        return [item.upper() for item in itens] if upper else itens
    return value


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


class RiskSettings(BaseModel):
    """Limites do Risk Manager Agent. **Configuracao de negocio: mora no banco.**

    Os defaults sao deliberadamente conservadores: se ninguem configurar, o
    sistema erra para o lado de arriscar de menos.

    Note que este e um `BaseModel`, nao `BaseSettings`: ele **nao** le variaveis
    de ambiente. Um `RISK_*` no `.env` nao tem efeito nenhum aqui, e por isso
    `Settings` recusa subir quando encontra um.
    """

    model_config = ConfigDict(extra="forbid")

    max_order_notional: Decimal | None = Field(
        default=Decimal("50"),
        gt=0,
        description=(
            "Teto absoluto de uma unica ordem, na moeda de cotacao. "
            "NULO desliga o teto e deixa o percentual mandar sozinho, o que faz "
            "a ordem acompanhar a carteira sem ninguem reconfigurar nada."
        ),
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
    max_open_positions: int | None = Field(
        default=5,
        ge=1,
        description=(
            "Numero maximo de posicoes abertas ao mesmo tempo. NULO remove o "
            "limite: quem passa a limitar e o caixa, porque cada ordem consome "
            "dinheiro e a proxima so sai se sobrar acima da ordem minima."
        ),
    )
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
    authorized_capital: Decimal | None = Field(
        default=None,
        ge=0,
        description=(
            "Teto de capital que o sistema pode POR PARA TRABALHAR, na moeda de "
            "cotacao. Saldo acima disto e ignorado no dimensionamento e gera "
            "notificacao pedindo autorizacao. NULO desliga o portao: todo o "
            "patrimonio fica disponivel. "
            "Existe porque um deposito nao e uma ordem: dinheiro que entra na "
            "conta por qualquer motivo -- venda de outro ativo, transferencia, "
            "reserva para outra finalidade -- nao deveria virar exposicao sem "
            "alguem dizer que sim."
        ),
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
        return _split_csv_value(value, upper=True)

    @model_validator(mode="after")
    def _check_coherence(self) -> RiskSettings:
        if (
            self.max_order_notional is not None
            and self.min_order_notional > self.max_order_notional
        ):
            raise ValueError("a ordem minima nao pode ser maior que a ordem maxima")
        if self.take_profit_pct <= self.stop_loss_pct:
            raise ValueError(
                "o alvo de lucro deve ser maior que o stop loss "
                "(caso contrario a estrategia tem esperanca matematica negativa)"
            )
        if self.weekly_loss_limit_pct < self.daily_loss_limit_pct:
            raise ValueError("o limite semanal de perda deve ser >= o limite diario")
        return self


class TradingSettings(BaseModel):
    """O que negociar, com que frequencia. **Configuracao de negocio: mora no banco.**

    Como `RiskSettings`, e um `BaseModel` puro: nao le ambiente. Cada campo aqui
    responde a uma pergunta de negocio, e todos sao editaveis pela aplicacao web
    sem reiniciar o processo.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Universo negociado ----------------------------------------------
    quote_currency: str = Field(
        default="USDT",
        min_length=2,
        description="Moeda de cotacao. Define quais pares existem e em que moeda o saldo e medido.",
    )
    symbols: list[str] = Field(
        default_factory=lambda: ["BTC/USDT", "ETH/USDT"],
        description="Pares negociados. VAZIO ativa a descoberta automatica ('mar aberto').",
    )
    timeframe: str = Field(default="15m", min_length=2)
    strategies: list[str] = Field(default_factory=lambda: ["ma_crossover", "rsi_reversion"])

    # --- Cadencia ---------------------------------------------------------
    candle_history_limit: int = Field(
        default=500,
        ge=50,
        le=1000,
        description="Quantos candles buscar por par. Define o aquecimento dos indicadores.",
    )
    market_data_interval_seconds: int = Field(default=60, ge=5)
    portfolio_interval_seconds: int = Field(default=60, ge=5)
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

    # --- Descoberta automatica de pares ("mar aberto") --------------------
    # Vale apenas quando `symbols` esta vazio. Ver `discovery.py` para o que isso
    # muda na camada de seguranca da whitelist.
    discovery_min_quote_volume_24h: Decimal = Field(
        default=Decimal("50000000"),
        gt=0,
        description="Piso de liquidez em 24h. Dos 487 pares USDT da Binance, 312 movem <1M/dia.",
    )
    discovery_max_symbols: int = Field(
        default=8, ge=1, le=50, description="Teto de pares monitorados na descoberta."
    )
    discovery_exclude_assets: list[str] = Field(
        default_factory=list,
        description="Ativos a excluir alem das stablecoins ja excluidas por padrao.",
    )
    discovery_refresh_hours: int = Field(
        default=24, ge=1, description="Intervalo entre novas varreduras de mercado."
    )

    # --- Paper trading (usado em dry_run e no backtest) -------------------
    paper_initial_balance: Decimal = Field(default=Decimal("1000"), gt=0)
    paper_fee_pct: Decimal = Field(default=Decimal("0.001"), ge=0, lt=1)
    paper_slippage_pct: Decimal = Field(default=Decimal("0.0005"), ge=0, lt=1)

    @field_validator("symbols", "strategies", "discovery_exclude_assets", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        return _split_csv_value(value)

    @field_validator("quote_currency", mode="before")
    @classmethod
    def _upper_quote(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check_strategies_exist(self) -> TradingSettings:
        """Nome de estrategia errado nao pode virar "nenhuma estrategia".

        Sem esta checagem, um typo salvo pela interface deixaria o sistema de pe
        gerando zero sinais -- o mesmo modo de falha silencioso do `SYMBOLS`
        vazio, mas sem nem a descoberta para compensar.
        """
        # Import local: `strategies` nao depende de `config`, mas manter o
        # import aqui garante que essa direcao nunca se inverta por acidente.
        from .strategies import available_strategies

        disponiveis = available_strategies()
        desconhecidas = [nome for nome in self.strategies if nome not in disponiveis]
        if desconhecidas:
            raise ValueError(
                f"estrategia(s) inexistente(s): {', '.join(desconhecidas)}. "
                f"Disponiveis: {', '.join(sorted(disponiveis))}"
            )
        if not self.strategies:
            raise ValueError("ao menos uma estrategia precisa estar ativa")
        return self

    @property
    def discovery_enabled(self) -> bool:
        """`symbols` vazio significa "descubra os pares", nao "nao faca nada".

        Deixar o sistema de pe sem observar nenhum mercado seria o pior estado
        possivel: heartbeat verde, dashboard atualizando e nenhuma operacao
        jamais -- parece saudavel e nao e.
        """
        return not self.symbols


class Settings(BaseSettings):
    """Configuracao de **ambiente**: o que descreve esta instalacao.

    Os dois blocos de negocio (`trading` e `risk`) aparecem aqui como atributos
    por conveniencia de leitura -- `settings.trading.timeframe` -- mas quem os
    preenche e o banco, na subida, nao o `.env`.
    """

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
    # AMBIENTE por decisao explicita: ligar dinheiro real exige editar arquivo no
    # servidor e reiniciar o processo. Se isso morasse no banco, um clique no
    # navegador poderia ligar ordens reais.
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

    # --- Exchange ---------------------------------------------------------
    # AMBIENTE: esta amarrada a qual credencial existe neste arquivo. Apontar
    # para uma exchange sem chave configurada e erro de instalacao, nao decisao
    # de negocio. A moeda de cotacao, ao contrario, esta em `trading`.
    exchange: str = "binance"

    # --- Notificacoes -----------------------------------------------------
    # SEGREDOS e endpoints dos canais moram aqui. Liga/desliga e destinatarios
    # ficam no banco e sao editaveis pela interface.
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

    # --- Negocio (preenchido pelo banco na subida) ------------------------
    trading: TradingSettings = Field(default_factory=TradingSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        return _split_csv_value(value)

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
    def _reject_business_keys(self) -> Settings:
        """Recusa subir quando uma chave de negocio aparece no ambiente.

        Ignorar em silencio seria repetir o erro que motivou esta separacao: a
        pessoa edita o arquivo, o valor nao tem efeito, e ninguem descobre ate
        uma ordem sair do tamanho errado.
        """
        encontradas = sorted(chave for chave in RETIRED_ENV_KEYS if chave in os.environ)
        encontradas += sorted(
            chave
            for chave in _keys_in_dotenv(self.model_config.get("env_file"))
            if chave in RETIRED_ENV_KEYS and chave not in encontradas
        )
        if encontradas:
            raise ValueError(
                "estas sao configuracoes de NEGOCIO e agora moram no banco, "
                "editaveis em Configuracoes na interface web:\n    "
                + "\n    ".join(encontradas)
                + "\n  Remova essas linhas do `.env`. Enquanto estiverem la, o valor "
                "escrito no arquivo NAO tem efeito -- e um valor sem efeito que "
                "parece ter e pior que nenhum valor."
            )
        return self

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

    def with_business_config(
        self, trading: TradingSettings, risk: RiskSettings
    ) -> None:
        """Instala a configuracao de negocio vinda do banco, no lugar.

        Trocar o objeto inteiro (em vez de campo a campo) mantem a leitura
        atomica: nenhum agente ve metade da configuracao nova. E como todos leem
        `settings.trading.X` no momento do uso, a troca se propaga sozinha, sem
        copias envelhecendo em cada agente.
        """
        object.__setattr__(self, "trading", trading)
        object.__setattr__(self, "risk", risk)


def _keys_in_dotenv(env_file: object) -> set[str]:
    """Nomes de variaveis declarados no `.env`, sem interpretar valores.

    Ler o arquivo na mao (em vez de confiar no `extra="ignore"`) e o unico jeito
    de saber que uma chave foi *escrita*: o pydantic-settings descarta chaves
    desconhecidas sem deixar rastro.
    """
    if not env_file:
        return set()

    caminho = Path(str(env_file))
    if not caminho.is_absolute():
        caminho = Path.cwd() / caminho
    if not caminho.is_file():
        return set()

    chaves: set[str] = set()
    for linha in caminho.read_text(encoding="utf-8", errors="replace").splitlines():
        limpa = linha.strip()
        if not limpa or limpa.startswith("#") or "=" not in limpa:
            continue
        nome = limpa.split("=", 1)[0].strip()
        if nome.startswith("export "):
            nome = nome[len("export ") :].strip()
        chaves.add(nome.upper())
    return chaves


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Usado pelos testes, que trocam variaveis de ambiente entre casos."""
    get_settings.cache_clear()
