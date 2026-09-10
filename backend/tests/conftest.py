"""Fixtures compartilhadas."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from helpers import make_candles

from crypto_traders.config import DEFAULT_DB_PATH, RiskSettings, Settings, TradingSettings
from crypto_traders.db import session as db_session
from crypto_traders.db.session import dispose_engine, init_db

#: O banco de VERDADE: e nele que o ensaio em dry_run acumula o historico que
#: da preco medio -- e portanto stop -- as posicoes. Corrompe-lo e perder a
#: base de custo do dinheiro real.
BANCO_DO_ENSAIO = DEFAULT_DB_PATH


class BancoDoEnsaioTocado(RuntimeError):
    """A suite tentou abrir o banco do ensaio. Nao existe caso legitimo disso."""


def _e_o_banco_do_ensaio(alvo: object) -> bool:
    """O destino desta conexao e o arquivo do ensaio?

    Compara o caminho RESOLVIDO, e nao o texto. `data/crypto_traders.db`,
    `./data/crypto_traders.db`, o absoluto e a mesma coisa vinda de uma URL de
    SQLAlchemy sao o mesmo arquivo, e uma barreira que dependesse de grafia
    seria a mesma classe de defesa que o gauntlet trocou por uma de verdade no
    `audit_log` (regex sobre texto de comando).
    """
    if alvo is None:
        return False
    texto = alvo.decode("utf-8", "replace") if isinstance(alvo, bytes) else str(alvo)
    if not texto or texto == ":memory:":
        return False
    # URL do SQLAlchemy: `sqlite+aiosqlite:///C:/.../crypto_traders.db`.
    if "://" in texto:
        texto = texto.split("://", 1)[1].lstrip("/")
        if not texto or texto.startswith(":memory:"):
            return False
    texto = texto.split("?", 1)[0]
    try:
        return Path(texto).expanduser().resolve() == BANCO_DO_ENSAIO.resolve()
    except (OSError, ValueError):
        return False


@pytest.fixture(autouse=True)
def banco_do_ensaio_e_intocavel(monkeypatch):
    """Levanta em vez de deixar a suite abrir `data/crypto_traders.db`.

    Medido em 2026-09-10: hoje a suite NAO abre esse arquivo -- 1118 testes, zero
    tentativas. Mas nada impedia, e a distancia entre "nao acontece" e "nao pode
    acontecer" e de uma linha: `Settings(_env_file=None)` resolve `database_url`
    para o banco do ensaio (`config._resolve_database_url`), e a suite constroi
    esse objeto em uma duzia de lugares. Basta um `session_scope(cfg)` a mais
    para a suite gravar no historico do dinheiro real -- e `trades` tem gatilho
    append-only, entao a linha errada NAO sai depois.

    A barreira e nas duas portas por onde este projeto abre SQLite, porque
    fechar uma so seria fechar a porta e deixar a janela:

    1. `db.session.create_async_engine`, por onde passa todo acesso da
       aplicacao (o unico chamador em `src/` esta em `get_engine`);
    2. `sqlite3.connect`, que a propria suite usa crua para conferir gatilhos
       (`test_persistencia.py`, `test_ataque_critico3_item7.py`) e que o
       aiosqlite usa por baixo.

    Nao redireciona nem inventa caminho: LEVANTA. Um redirecionamento silencioso
    faria um teste mal escrito passar medindo outro banco, e este projeto ja
    aprendeu que configuracao que parece agir e nao age e pior que nenhuma.
    """
    engine_original = db_session.create_async_engine
    connect_original = sqlite3.connect

    def engine_guardado(url, *args, **kwargs):
        if _e_o_banco_do_ensaio(url):
            raise BancoDoEnsaioTocado(
                f"a suite tentou abrir um engine sobre o banco do ensaio ({url}). "
                "Use a fixture `settings`, que aponta para tmp_path."
            )
        return engine_original(url, *args, **kwargs)

    def connect_guardado(database, *args, **kwargs):
        if _e_o_banco_do_ensaio(database):
            raise BancoDoEnsaioTocado(
                f"a suite tentou conectar no banco do ensaio ({database}). "
                "Use a fixture `settings`, que aponta para tmp_path."
            )
        return connect_original(database, *args, **kwargs)

    monkeypatch.setattr(db_session, "create_async_engine", engine_guardado)
    monkeypatch.setattr(sqlite3, "connect", connect_guardado)


@pytest.fixture(autouse=True)
def isolate_from_dotenv(monkeypatch):
    """Impede que os testes leiam o `.env` da maquina.

    `Settings` carrega `.env` por padrao -- correto em producao, desastroso em
    teste: a suite passaria ou falharia conforme a configuracao pessoal de quem
    roda. Foi exatamente o que aconteceu quando o `.env` local passou a ter
    limites diferentes dos padroes de fabrica, e 18 testes quebraram sem que uma
    linha de codigo de producao tivesse mudado.

    `RiskSettings` e `TradingSettings` nao aparecem aqui porque nao leem ambiente
    nenhum: sao configuracao de negocio, e negocio mora no banco.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)


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
        trading=TradingSettings(
            symbols=["BTC/USDT"],
            strategies=["ma_crossover"],
            quote_currency="USDT",
            paper_initial_balance=Decimal("1000"),
        ),
        risk=risk_limits,
    )
    await init_db(configured)
    yield configured
    await dispose_engine()


@pytest.fixture
def candle_factory():
    return make_candles
