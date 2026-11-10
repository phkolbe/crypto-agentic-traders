"""A redacao do log (`logging_setup._redact_secrets`), a ultima linha de defesa.

Ate 10/11/2026 ela so olhava as chaves de nivel 1 e os nomes exatos (`password`,
`token`...): `smtp_password`, `whatsapp_access_token`, `database_url` (que leva a senha
do banco na URL) e um dict de credenciais aninhado passavam inteiros para o disco e para
o coletor OTLP (o espelho roda depois da redacao).
"""

from __future__ import annotations

from crypto_traders.logging_setup import _redact_secrets


def _redigir(evento: dict) -> dict:
    return _redact_secrets(None, "info", evento)


def test_nomes_compostos_de_segredo_sao_redigidos():
    saida = _redigir(
        {
            "event": "x",
            "smtp_password": "senha",
            "whatsapp_access_token": "tok",
            "access_token": "tok",
            "binance_api_secret": "seg",
            "database_url": "postgresql+asyncpg://crypto:senha@crypto-prd-postgres:5432/crypto",
            "dsn": "postgresql://u:p@h/db",
        }
    )
    assert saida["event"] == "x"
    for chave in (
        "smtp_password",
        "whatsapp_access_token",
        "access_token",
        "binance_api_secret",
        "database_url",
        "dsn",
    ):
        assert saida[chave] == "***", chave


def test_os_nomes_de_antes_continuam_redigidos():
    saida = _redigir({"api_key": "k", "Authorization": "Bearer x", "password": "p"})
    assert set(saida.values()) == {"***"}


def test_dict_aninhado_e_redigido_sem_mexer_no_original():
    credenciais = {"api_key": "k", "api_secret": "s", "exchange": "binance"}
    saida = _redigir({"event": "x", "credenciais": credenciais})
    assert saida["credenciais"] == {"api_key": "***", "api_secret": "***", "exchange": "binance"}
    # O dict e do chamador (pode ser a configuracao viva): a redacao trabalha numa copia.
    assert credenciais["api_key"] == "k"


def test_campo_comum_nao_e_redigido():
    """Redigir demais tambem custa: o log de ordem precisa do par e da quantidade."""
    evento = {"event": "ordem", "symbol": "BTC/USDC", "amount": "0.01", "order_id": "123"}
    assert _redigir(dict(evento)) == evento
