"""Comandos pesados recusam rodar dentro do conteiner do sistema (B3).

Medido em 04/11/2026: `docker exec crypto crypto-traders check` carregou o
catalogo da exchange dentro dos 512 MB do conteiner do sistema e o kernel matou
o conteiner inteiro. O `backtest` faz a mesma carga (o `fetch_ohlcv` do ccxt
carrega os mercados antes).
"""

from __future__ import annotations

import pytest

from crypto_traders import cli

SISTEMA = b"/app/.venv/bin/python\x00/app/.venv/bin/crypto-traders\x00run\x00"
SISTEMA_SEM_API = b"/app/.venv/bin/python\x00/app/.venv/bin/crypto-traders\x00run\x00--no-api\x00"
COM_TINI = b"/sbin/tini\x00--\x00crypto-traders\x00run\x00"
CHECK_PROPRIO = b"/app/.venv/bin/python\x00/app/.venv/bin/crypto-traders\x00check\x00"


@pytest.mark.parametrize("pid1", [SISTEMA, SISTEMA_SEM_API, COM_TINI])
def test_dentro_do_conteiner_do_sistema(pid1):
    assert cli.dividindo_conteiner_com_o_sistema(pid1, 4242) is True


@pytest.mark.parametrize(
    ("pid1", "meu_pid"),
    [(CHECK_PROPRIO, 4242), (SISTEMA, 1), (None, 4242), (b"", 4242)],
)
def test_fora_do_conteiner_do_sistema(pid1, meu_pid):
    """compose run (PID 1 e o proprio comando), o proprio sistema, e Windows sem /proc."""
    assert cli.dividindo_conteiner_com_o_sistema(pid1, meu_pid) is False


@pytest.mark.parametrize("comando", sorted(cli.COMANDOS_PESADOS))
def test_comandos_pesados_recusados_no_conteiner_do_sistema(comando):
    recusa = cli.recusa_no_conteiner_do_sistema(comando, SISTEMA, 4242)
    assert recusa and f"crypto-traders {comando}" in recusa


@pytest.mark.parametrize("comando", ["run", "migrar"])
def test_run_e_migrar_nunca_recusados(comando):
    assert cli.recusa_no_conteiner_do_sistema(comando, SISTEMA, 4242) is None


def test_main_recusa_antes_de_carregar_qualquer_coisa(monkeypatch, capsys):
    """A recusa vem do `main`, antes de configuracao, banco ou exchange."""
    monkeypatch.setattr(cli.Path, "read_bytes", lambda self: SISTEMA)
    monkeypatch.setattr(cli.os, "getpid", lambda: 4242)

    def nao_pode(*_a, **_k):
        raise AssertionError("carregou configuracao dentro do conteiner do sistema")

    monkeypatch.setattr(cli, "get_settings", nao_pode)
    assert cli.main(["backtest", "--symbol", "BTC/USDC"]) == 2
    assert "backtest recusado" in capsys.readouterr().err
