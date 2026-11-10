"""`TRADING__*` e `RISK__*` no ambiente: a segunda fonte de verdade que a trava nao via.

`Settings` le o ambiente com `env_nested_delimiter="__"` (e o que faz
`BINANCE__API_KEY` chegar a `settings.binance.api_key`). O mesmo mecanismo levava
`TRADING__TIMEFRAME=1d` ou `RISK__MAX_ORDER_NOTIONAL=999` do `crypto.env` de
producao para `settings.trading` / `settings.risk` -- e o banco sobrescrevia na subida.
Valor escrito, sem efeito e sem aviso: o episodio do "`.env` dizia 7, o sistema operava
com 50". `RETIRED_ENV_KEYS` so conhecia os nomes antigos (`RISK_MAX_ORDER_NOTIONAL`).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crypto_traders.config import Settings


class TestNegocioAninhadoNoAmbiente:
    @pytest.mark.parametrize(
        "chave,valor",
        [
            ("TRADING__TIMEFRAME", "1d"),
            ("RISK__MAX_ORDER_NOTIONAL", "999"),
            ("RISK__STOP_LOSS_PCT", "0.5"),
            # O campo inteiro em JSON tambem e lido pelo pydantic-settings.
            ("TRADING", '{"timeframe": "1d"}'),
            ("RISK", '{"max_order_notional": "999"}'),
        ],
    )
    def test_recusa_subir(self, chave, valor, monkeypatch):
        monkeypatch.setenv(chave, valor)
        with pytest.raises(ValidationError, match="NEGOCIO") as erro:
            Settings()
        assert chave in str(erro.value)

    def test_valor_invalido_recebe_a_mensagem_da_trava_e_nao_a_do_modelo(self, monkeypatch):
        """Antes, `TRADING__TIMEFRAME=xyz` caia na validacao de `TradingSettings`.

        A mensagem falava de timeframe invalido -- e a pessoa corrigia o VALOR no
        arquivo, que continuaria sem efeito. A trava tem de falar primeiro.
        """
        monkeypatch.setenv("TRADING__TIMEFRAME", "xyz")
        with pytest.raises(ValidationError, match="moram no banco"):
            Settings()

    def test_no_arquivo_env_tambem_e_recusado(self, tmp_path, monkeypatch):
        arquivo = tmp_path / ".env"
        arquivo.write_text("API_PORT=9001\nRISK__MAX_OPEN_POSITIONS=9\n", encoding="utf-8")
        monkeypatch.setitem(Settings.model_config, "env_file", str(arquivo))
        with pytest.raises(ValidationError, match="RISK__MAX_OPEN_POSITIONS"):
            Settings()

    def test_ambiente_com_duplo_sublinhado_continua_valendo(self, monkeypatch):
        """A trava nao pega as credenciais aninhadas nem o modo de operacao."""
        monkeypatch.setenv("BINANCE__API_KEY", "chave")
        monkeypatch.setenv("BINANCE__API_SECRET", "segredo")
        monkeypatch.setenv("TRADING_MODE", "dry_run")
        configurado = Settings()
        assert configurado.binance.configured

    def test_negocio_passado_pelo_codigo_continua_valendo(self):
        """Quem preenche `trading`/`risk` e o banco, pelo codigo -- nao o ambiente."""
        from crypto_traders.config import TradingSettings

        configurado = Settings(trading=TradingSettings(timeframe="4h"))
        assert configurado.trading.timeframe == "4h"
