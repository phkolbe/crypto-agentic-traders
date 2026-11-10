"""A barreira da suite contra o banco do sistema tambem guarda o Postgres.

`conftest.banco_do_ensaio_e_intocavel` so reconhecia o SQLite
`data/crypto_traders.db`; desde 24/10/2026 o dinheiro do ensaio esta no Postgres
`crypto` (porta 5433). `isolate_from_dotenv` desliga o `.env`, mas nao o
`os.environ`: com `DATABASE_URL` ou `POSTGRES_TEST_URL` apontando para la, a
suite gravava trades e `audit_log` -- append-only -- no historico real, e o
`README.md` ensinava a suite a usar o MESMO servidor, com o superusuario
(auditoria de 09/11/2026, testes, foco 3).
"""

from __future__ import annotations

import os

import pytest
from conftest import BancoDoEnsaioTocado
from helpers import (
    LIBERACAO_DO_BANCO_DO_SISTEMA,
    e_o_banco_do_sistema,
    isolar_do_ambiente,
    recusa_do_banco_do_sistema,
    variaveis_do_sistema,
)

from crypto_traders.config import Settings
from crypto_traders.db import session as db_session

DO_SISTEMA = [
    # O que o README mandava: outro database, mesmo servidor e superusuario.
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5433/crypto_teste",
    # A DATABASE_URL do conteiner `crypto` (docker-compose.yml).
    "postgresql+asyncpg://crypto:segredo@crypto-postgres:5432/crypto",
    # O database do sistema em qualquer porta.
    "postgresql+asyncpg://crypto:segredo@localhost:5432/crypto",
    "postgresql://postgres@postgres/qualquer",
]

DESCARTAVEIS = [
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres",
    # O do CI (`.github/workflows/ci.yml`).
    "postgresql+asyncpg://postgres:postgres@localhost:5432/crypto_teste",
    "sqlite+aiosqlite:///C:/tmp/teste.db",
    "",
]


@pytest.mark.parametrize("url", DO_SISTEMA)
def test_reconhece_o_postgres_do_sistema(url):
    assert e_o_banco_do_sistema(url)


@pytest.mark.parametrize("url", DESCARTAVEIS)
def test_nao_confunde_um_banco_descartavel(url):
    assert not e_o_banco_do_sistema(url)


@pytest.mark.parametrize("variavel", ["DATABASE_URL", "POSTGRES_TEST_URL"])
def test_a_suite_recusa_subir_apontada_para_o_sistema(variavel):
    motivo = recusa_do_banco_do_sistema({variavel: DO_SISTEMA[0]})

    assert motivo is not None
    assert variavel in motivo and "55432" in motivo
    assert "postgres:postgres@127.0.0.1:5433" not in motivo, "a senha nao vai para a mensagem"


def test_a_liberacao_explicita_deixa_passar():
    ambiente = {"POSTGRES_TEST_URL": DO_SISTEMA[0], LIBERACAO_DO_BANCO_DO_SISTEMA: "1"}

    assert recusa_do_banco_do_sistema(ambiente) is None


def test_ambiente_do_ci_passa():
    assert recusa_do_banco_do_sistema({"POSTGRES_TEST_URL": DESCARTAVEIS[1]}) is None


def test_engine_sobre_o_postgres_do_sistema_levanta(monkeypatch):
    """A porta da aplicacao: `Settings()` le `DATABASE_URL` do ambiente.

    `create_async_engine` nao conecta (o engine e preguicoso), entao no codigo
    anterior esta linha passava calada -- e o primeiro `session_scope` gravava.
    """
    monkeypatch.delenv(LIBERACAO_DO_BANCO_DO_SISTEMA, raising=False)

    with pytest.raises(BancoDoEnsaioTocado, match="Postgres do sistema"):
        db_session.create_async_engine(DO_SISTEMA[1])


def test_o_gancho_da_suite_recusa_antes_de_coletar(monkeypatch):
    """`pytest_configure` e quem barra a suite inteira (nada conecta aqui)."""
    import conftest

    monkeypatch.delenv(LIBERACAO_DO_BANCO_DO_SISTEMA, raising=False)
    monkeypatch.setenv("POSTGRES_TEST_URL", "postgresql+asyncpg://x:y@127.0.0.1:1/crypto")

    with pytest.raises(pytest.UsageError, match="Postgres do sistema"):
        conftest.pytest_configure(None)


# ---------------------------------------------------------------------------
# O ambiente do shell nao chega aos testes
# ---------------------------------------------------------------------------
def test_reconhece_as_variaveis_que_settings_leria():
    """Campo e campo aninhado (`__`) saem; o que nao e campo de `Settings` fica."""
    ambiente = {
        "DATABASE_URL": "postgresql://x@y/z",
        "TRADING_MODE": "live",
        "live_trading_confirmed": "true",
        "BINANCE__API_KEY": "k",
        "BINANCE_TESTNET__API_SECRET": "s",
        "SMTP_PASSWORD": "p",
        "API_KEY": "le-se sozinho: ExchangeCredentials e BaseSettings",
        "POSTGRES_TEST_URL": "postgresql://descartavel",
        "PATH": "/usr/bin",
        "BINANCE_QUALQUER": "nao e campo nem aninhado",
    }
    assert sorted(variaveis_do_sistema(ambiente)) == sorted(
        [
            "DATABASE_URL",
            "TRADING_MODE",
            "live_trading_confirmed",
            "BINANCE__API_KEY",
            "BINANCE_TESTNET__API_SECRET",
            "SMTP_PASSWORD",
            "API_KEY",
        ]
    )


def test_settings_do_teste_nao_le_o_modo_nem_as_chaves_do_shell(monkeypatch):
    """`isolate_from_dotenv` desligava so o `.env`: o `os.environ` passava.

    Com `TRADING_MODE=live` e as chaves da Binance exportados no shell, todo
    `Settings()` da suite nascia em live com as chaves da maquina. A fixture
    autouse roda ANTES do teste; aqui o corpo dela e chamado de novo, depois de
    sujar o ambiente, para provar que limpa o que o shell deixou.
    """
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("BINANCE__API_KEY", "chave-do-shell")
    monkeypatch.setenv("API_SECRET", "segredo-sem-prefixo")
    monkeypatch.setenv("POSTGRES_TEST_URL", "postgresql+asyncpg://p:p@127.0.0.1:55432/x")
    isolar_do_ambiente(monkeypatch)

    cfg = Settings()
    assert str(cfg.trading_mode) == "dry_run"
    assert not cfg.binance.api_key
    assert not cfg.binance.api_secret and not cfg.binance_testnet.api_secret
    assert os.environ["POSTGRES_TEST_URL"].endswith("/x")
