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

    async def test_a_corrupt_row_refuses_to_start(self, settings):
        """Linha que nao valida recusa a subida, com erro que diz onde.

        Este teste exigia o contrario -- cair nos padroes do modelo -- com a
        justificativa de que eles "arriscam de menos". Nao arriscam: o padrao de
        `TradingSettings` e USDT, 15m e duas estrategias, contra o USDC/1d de
        hoje. O sistema subiria negociando outra moeda e as posicoes em USDC
        ficariam sem preco para o stop (auditoria de 09/11/2026, P0 #10).
        """
        from crypto_traders.business_config import ConfiguracaoGravadaInvalida
        from crypto_traders.db.repositories import TradingConfigRepository
        from crypto_traders.db.session import session_scope

        async with session_scope(settings) as session:
            await TradingConfigRepository(session).save({"timeframe": 42, "lixo": True})

        with pytest.raises(ConfiguracaoGravadaInvalida, match="trading_config"):
            await load_business_config(settings)

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


# Configuracao gravada como a do dry_run de hoje (USDC, 1d, so ma_crossover):
# diferente dos padroes do modelo em tudo que importa.
GRAVADA_HOJE = {
    "quote_currency": "USDC",
    "symbols": ["BTC/USDC", "ETH/USDC"],
    "timeframe": "1d",
    "strategies": ["ma_crossover"],
}


async def _gravar_trading(settings, valores: dict) -> None:
    from crypto_traders.db.repositories import TradingConfigRepository
    from crypto_traders.db.session import session_scope

    async with session_scope(settings) as session:
        await TradingConfigRepository(session).save(valores)


async def _gravar_risco(settings, valores: dict) -> None:
    from crypto_traders.db.repositories import RiskConfigRepository
    from crypto_traders.db.session import session_scope

    async with session_scope(settings) as session:
        await RiskConfigRepository(session).update_values(valores)


class TestUmaAtualizacaoNaoDerrubaAConfiguracaoGravada:
    """P0 #10: campo novo ou removido no modelo nao pode trocar o que se negocia.

    Os dois modelos sao `extra="forbid"`. Antes, bastava um deploy remover um
    campo de `TradingSettings` para a linha gravada ter uma chave a mais, nao
    validar, e o sistema subir com os padroes de fabrica: BTC/USDT em 15m com
    `rsi_reversion`, enquanto as posicoes abertas sao em USDC.
    """

    async def test_campo_que_saiu_do_modelo_e_ignorado_e_o_resto_vale(self, settings):
        await _gravar_trading(settings, {**GRAVADA_HOJE, "campo_que_saiu_do_modelo": 7})

        trading, _ = await load_business_config(settings)

        assert trading.quote_currency == "USDC"
        assert trading.symbols == ["BTC/USDC", "ETH/USDC"]
        assert trading.timeframe == "1d"
        assert trading.strategies == ["ma_crossover"]

    async def test_campo_novo_no_modelo_recebe_o_padrao_e_o_resto_vale(self, settings):
        # Linha gravada antes de `signal_batch_window_seconds` existir.
        await _gravar_trading(settings, dict(GRAVADA_HOJE))

        trading, _ = await load_business_config(settings)

        assert trading.quote_currency == "USDC"
        assert trading.signal_batch_window_seconds == 0.0

    async def test_a_linha_gravada_nao_e_reescrita_na_subida(self, settings):
        """A chave desconhecida fica no banco: voltar a versao anterior a reencontra."""
        from crypto_traders.db.repositories import TradingConfigRepository
        from crypto_traders.db.session import session_scope

        await _gravar_trading(settings, {**GRAVADA_HOJE, "campo_que_saiu_do_modelo": 7})
        await load_business_config(settings)

        async with session_scope(settings) as session:
            gravado = await TradingConfigRepository(session).get()
        assert gravado["campo_que_saiu_do_modelo"] == 7

    async def test_valor_invalido_recusa_subir_em_vez_de_operar_outra_moeda(self, settings):
        from crypto_traders.business_config import ConfiguracaoGravadaInvalida

        await _gravar_trading(settings, {**GRAVADA_HOJE, "strategies": ["estrategia_que_saiu"]})

        with pytest.raises(ConfiguracaoGravadaInvalida) as erro:
            await load_business_config(settings)
        assert "trading_config" in str(erro.value)
        assert "estrategia_que_saiu" in str(erro.value)

    async def test_a_subida_falha_e_nada_e_instalado(self, settings):
        """Quem sobe o sistema e o `install_business_config`: ele tem de propagar."""
        from crypto_traders.business_config import ConfiguracaoGravadaInvalida

        await _gravar_trading(settings, {**GRAVADA_HOJE, "timeframe": 42})
        with pytest.raises(ConfiguracaoGravadaInvalida):
            await install_business_config(settings)
        assert settings.trading.quote_currency == "USDT"

    async def test_risco_com_campo_que_saiu_preserva_os_limites(self, settings):
        await _gravar_risco(
            settings,
            {"stop_loss_pct": 0.03, "take_profit_pct": 0.08, "min_order_notional": "5",
             "campo_que_saiu_do_modelo": True},
        )

        _, risk = await load_business_config(settings)

        assert risk.take_profit_pct == 0.08
        assert risk.min_order_notional == Decimal("5")

    async def test_risco_invalido_recusa_subir(self, settings):
        from crypto_traders.business_config import ConfiguracaoGravadaInvalida

        await _gravar_risco(settings, {"stop_loss_pct": 0.08, "take_profit_pct": 0.03})
        with pytest.raises(ConfiguracaoGravadaInvalida, match="risk_config"):
            await load_business_config(settings)


class TestAuditoriaDaConfiguracaoDeNegocio:
    async def test_antes_e_depois_sao_completos(self, settings):
        """O `audit_log` precisa permitir reconstruir a configuracao inteira.

        Antes gravava so os campos enviados. Depois de um fallback silencioso,
        editar um campo persistia os padroes de fabrica em TODOS os outros e o
        log nao mostrava nada disso.
        """
        from crypto_traders.db.repositories import AuditLogRepository
        from crypto_traders.db.session import session_scope

        await install_business_config(settings)
        anterior = settings.trading.model_dump(mode="json")
        await update_trading_config(settings, {"timeframe": "4h"}, actor="paulo")

        async with session_scope(settings) as session:
            entradas = await AuditLogRepository(session).list(limit=10)
        alteracao = next(
            e for e in entradas
            if e.action == "trading_config_updated" and e.actor == "paulo"
        )
        assert alteracao.before == anterior
        assert alteracao.after == {**anterior, "timeframe": "4h"}
        assert "timeframe" in (alteracao.detail or "")


class TestEdicaoRecusaOQueOSistemaNaoOpera:
    """`PUT /trading/config` aceitava timeframe e pares que o sistema nao opera."""

    async def test_timeframe_invalido_e_recusado(self, settings):
        await install_business_config(settings)
        with pytest.raises(ValueError, match="timeframe"):
            await update_trading_config(settings, {"timeframe": "1x"}, actor="teste")
        assert settings.trading.timeframe == "15m"

    async def test_par_fora_da_moeda_de_cotacao_e_recusado(self, settings):
        await install_business_config(settings)
        antes = settings.trading.symbols
        with pytest.raises(ValueError, match="BTC/USDC"):
            await update_trading_config(settings, {"symbols": ["BTC/USDC"]}, actor="teste")
        assert settings.trading.symbols == antes

    async def test_trocar_a_moeda_sem_trocar_os_pares_e_recusado(self, settings):
        await install_business_config(settings)
        with pytest.raises(ValueError, match="USDC"):
            await update_trading_config(settings, {"quote_currency": "USDC"}, actor="teste")
        assert settings.trading.quote_currency == "USDT"

    async def test_moeda_e_pares_juntos_passam(self, settings):
        await install_business_config(settings)
        novo = await update_trading_config(
            settings, {"quote_currency": "USDC", "symbols": ["BTC/USDC"]}, actor="teste"
        )
        assert novo.symbols == ["BTC/USDC"]
