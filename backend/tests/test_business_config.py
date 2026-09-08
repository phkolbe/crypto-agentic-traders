"""A fronteira entre ambiente e negocio.

O `.env` descreve a instalacao; o banco guarda o que negociar e com quanto
risco. Estes testes defendem essa fronteira, porque ela e facil de furar sem
ninguem perceber -- e foi exatamente o que aconteceu antes de existir: o `.env`
pedia ordem maxima de 7 USDT enquanto o sistema operava com 50.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from crypto_traders.business_config import (
    install_business_config,
    load_business_config,
    update_trading_config,
)
from crypto_traders.config import (
    RETIRED_ENV_KEYS,
    RiskSettings,
    Settings,
    TradingSettings,
)


class TestBusinessKeysAreRefused:
    """Chave de negocio no ambiente impede a subida, com a lista na mensagem."""

    @pytest.mark.parametrize(
        "chave,valor",
        [
            ("RISK_MAX_ORDER_NOTIONAL", "999"),
            ("SYMBOLS", "BTC/USDT"),
            ("TIMEFRAME", "1h"),
            ("PAPER_FEE_PCT", "0.005"),
            ("RISK_MVRV_MAX_PERCENTILE", "0.4"),
        ],
    )
    def test_a_single_business_key_blocks_startup(self, chave, valor, monkeypatch):
        monkeypatch.setenv(chave, valor)
        with pytest.raises(ValidationError, match="NEGOCIO"):
            Settings()

    def test_the_message_names_every_offending_key(self, monkeypatch):
        """Uma mensagem que diz "existe erro" sem dizer onde custa uma hora."""
        monkeypatch.setenv("SYMBOLS", "BTC/USDT")
        monkeypatch.setenv("RISK_COOLDOWN_SECONDS", "60")
        with pytest.raises(ValidationError) as erro:
            Settings()
        texto = str(erro.value)
        assert "SYMBOLS" in texto
        assert "RISK_COOLDOWN_SECONDS" in texto

    def test_environment_keys_still_work(self, monkeypatch):
        """A trava nao pode pegar ambiente por engano."""
        monkeypatch.setenv("API_PORT", "9001")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("EXCHANGE", "binance")
        configurado = Settings()
        assert configurado.api_port == 9001
        assert configurado.log_level == "DEBUG"

    def test_every_business_field_is_covered_by_the_guard(self):
        """Campo novo de negocio sem entrada na trava seria um furo silencioso.

        Este teste falha ao adicionar um campo a `TradingSettings` ou
        `RiskSettings` sem registra-lo em `RETIRED_ENV_KEYS` -- o que deixaria a
        variavel correspondente passar batida pelo `.env` outra vez.
        """
        esperadas = {campo.upper() for campo in TradingSettings.model_fields}
        esperadas |= {f"RISK_{campo.upper()}" for campo in RiskSettings.model_fields}
        assert esperadas <= RETIRED_ENV_KEYS, esperadas - RETIRED_ENV_KEYS


class TestBusinessModelsIgnoreTheEnvironment:
    """Negocio nao le ambiente. Se lesse, existiriam duas fontes da verdade."""

    def test_trading_settings_does_not_read_env(self, monkeypatch):
        monkeypatch.setenv("TIMEFRAME", "1d")
        monkeypatch.setenv("QUOTE_CURRENCY", "BRL")
        assert TradingSettings().timeframe == "15m"
        assert TradingSettings().quote_currency == "USDT"

    def test_risk_settings_does_not_read_env(self, monkeypatch):
        monkeypatch.setenv("RISK_MAX_ORDER_NOTIONAL", "12345")
        assert RiskSettings().max_order_notional == Decimal("50")


class TestValidation:
    def test_unknown_strategy_is_refused(self):
        """Typo salvo pela interface deixaria o sistema gerando zero sinais."""
        with pytest.raises(ValidationError, match="inexistente"):
            TradingSettings(strategies=["ma_crossoverr"])

    def test_empty_strategy_list_is_refused(self):
        with pytest.raises(ValidationError, match="ao menos uma"):
            TradingSettings(strategies=[])

    def test_csv_is_accepted_because_forms_are_text(self):
        configurado = TradingSettings(symbols="BTC/USDT, ETH/USDT")
        assert configurado.symbols == ["BTC/USDT", "ETH/USDT"]

    def test_quote_currency_is_normalized(self):
        assert TradingSettings(quote_currency=" brl ").quote_currency == "BRL"

    def test_empty_symbols_means_discovery(self):
        assert TradingSettings(symbols=[]).discovery_enabled is True

    def test_unknown_field_is_refused(self):
        """`extra="forbid"` transforma typo de campo em erro visivel."""
        with pytest.raises(ValidationError):
            TradingSettings(timefrmae="1h")


class TestPersistence:
    async def test_first_load_seeds_the_row(self, settings):
        from crypto_traders.db.repositories import TradingConfigRepository
        from crypto_traders.db.session import session_scope

        async with session_scope(settings) as session:
            assert await TradingConfigRepository(session).get() is None

        await load_business_config(settings)

        async with session_scope(settings) as session:
            gravado = await TradingConfigRepository(session).get()
        assert gravado is not None
        assert gravado["timeframe"] == TradingSettings().timeframe

    async def test_round_trip_preserves_values(self, settings):
        await update_trading_config(
            settings, {"timeframe": "4h", "symbols": ["SOL/USDT"]}, actor="teste"
        )
        recarregado, _ = await load_business_config(settings)
        assert recarregado.timeframe == "4h"
        assert recarregado.symbols == ["SOL/USDT"]

    async def test_a_corrupt_row_falls_back_to_safe_defaults(self, settings):
        """Linha invalida no banco nao pode derrubar a subida.

        O estado alternativo aqui nao e "arriscado", e "arrisca de menos": os
        padroes do modelo sao conservadores por construcao.
        """
        from crypto_traders.db.repositories import TradingConfigRepository
        from crypto_traders.db.session import session_scope

        async with session_scope(settings) as session:
            await TradingConfigRepository(session).save({"timeframe": 42, "lixo": True})

        trading, _ = await load_business_config(settings)
        assert trading.timeframe == TradingSettings().timeframe

    async def test_update_is_recorded_in_the_audit_log(self, settings):
        from crypto_traders.db.repositories import AuditLogRepository
        from crypto_traders.db.session import session_scope

        await install_business_config(settings)
        await update_trading_config(settings, {"timeframe": "1h"}, actor="paulo")

        async with session_scope(settings) as session:
            entradas = await AuditLogRepository(session).list(limit=10)
        alteracoes = [e for e in entradas if e.action == "trading_config_updated"]
        assert alteracoes, "alterar o que o sistema negocia precisa deixar rastro"
        assert any(e.actor == "paulo" for e in alteracoes)
        assert any(e.after.get("timeframe") == "1h" for e in alteracoes)

    async def test_update_installs_the_new_value_in_settings(self, settings):
        """Gravar sem instalar deixaria a interface e o sistema discordando."""
        await install_business_config(settings)
        await update_trading_config(settings, {"timeframe": "2h"}, actor="teste")
        assert settings.trading.timeframe == "2h"

    async def test_invalid_change_leaves_the_current_value_intact(self, settings):
        await install_business_config(settings)
        antes = settings.trading.timeframe
        with pytest.raises(ValidationError):
            await update_trading_config(settings, {"strategies": ["nao_existe"]}, actor="teste")
        assert settings.trading.timeframe == antes

    async def test_validation_runs_over_the_merged_result(self, settings):
        """Um campo isolado pode ser valido e tornar o conjunto incoerente."""
        await install_business_config(settings)
        # Enviado sozinho, `symbols` e valido; o merge precisa continuar valido.
        atualizado = await update_trading_config(settings, {"symbols": []}, actor="teste")
        assert atualizado.discovery_enabled is True
