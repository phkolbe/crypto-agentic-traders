"""Credencial da exchange so entra pelo nome com prefixo (rodada 3, achado da R6).

`ExchangeCredentials` era um `BaseSettings` criado por `default_factory`: cada
instancia lia o ambiente por conta propria, SEM prefixo. Medido: `API_SECRET=x` no
ambiente do conteiner preenchia ao mesmo tempo `binance` e `binance_testnet` -- uma
variavel generica, de qualquer outra ferramenta, virava credencial das duas contas,
inclusive a da testnet sendo usada como se fosse a real e vice-versa.
"""

from __future__ import annotations

import pytest

from crypto_traders.config import Settings


@pytest.fixture
def ambiente_limpo(monkeypatch):
    for nome in ("API_KEY", "API_SECRET", "PASSPHRASE"):
        monkeypatch.delenv(nome, raising=False)
    for prefixo in ("BINANCE__", "BINANCE_TESTNET__"):
        for nome in ("API_KEY", "API_SECRET", "PASSPHRASE"):
            monkeypatch.delenv(prefixo + nome, raising=False)
    return monkeypatch


def test_variavel_generica_nao_vira_credencial(ambiente_limpo):
    ambiente_limpo.setenv("API_KEY", "generica")
    ambiente_limpo.setenv("API_SECRET", "generica")
    settings = Settings(_env_file=None)
    assert settings.binance.api_key is None
    assert settings.binance.api_secret is None
    assert settings.binance_testnet.api_key is None
    assert settings.binance_testnet.api_secret is None


def test_o_nome_com_prefixo_continua_valendo_e_separado(ambiente_limpo):
    ambiente_limpo.setenv("BINANCE__API_KEY", "real")
    ambiente_limpo.setenv("BINANCE__API_SECRET", "real-segredo")
    ambiente_limpo.setenv("BINANCE_TESTNET__API_KEY", "teste")
    settings = Settings(_env_file=None)
    assert settings.binance.api_key.get_secret_value() == "real"
    assert settings.binance.api_secret.get_secret_value() == "real-segredo"
    assert settings.binance_testnet.api_key.get_secret_value() == "teste"
    assert settings.binance_testnet.api_secret is None
