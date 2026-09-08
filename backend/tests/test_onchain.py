"""Testes do MVRV Z-Score: indicador, interpretação e filtro de regime."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from crypto_traders.domain.enums import ExchangeName, RiskDecision, SignalDirection
from crypto_traders.domain.models import Signal
from crypto_traders.indicators.onchain import (
    mvrv_zscore,
    zone_by_classic_threshold,
    zone_by_percentile,
)
from crypto_traders.onchain import reading_from_series
from crypto_traders.risk.rules import PortfolioState, RiskEngine

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class TestZScoreFormula:
    def test_matches_the_definition(self):
        """z = (market_cap - realized_cap) / desvio expansivo do market_cap."""
        market = pd.Series([100.0, 110.0, 120.0, 130.0])
        realized = pd.Series([90.0, 92.0, 94.0, 96.0])
        result = mvrv_zscore(market, realized)
        esperado = (130.0 - 96.0) / market.expanding(min_periods=2).std(ddof=0).iloc[-1]
        assert result.iloc[-1] == pytest.approx(esperado)

    def test_first_point_has_no_deviation(self):
        """Com um ponto nao existe desvio: NaN e a resposta honesta."""
        result = mvrv_zscore(pd.Series([100.0, 110.0]), pd.Series([90.0, 95.0]))
        assert pd.isna(result.iloc[0])

    def test_negative_when_market_below_realized(self):
        """Mercado abaixo do preco medio pago: prejuizo agregado, z negativo."""
        market = pd.Series([100.0, 90.0, 80.0])
        realized = pd.Series([100.0, 100.0, 100.0])
        assert mvrv_zscore(market, realized).iloc[-1] < 0

    def test_rejects_mismatched_lengths(self):
        with pytest.raises(ValueError, match="mesmo tamanho"):
            mvrv_zscore(pd.Series([1.0, 2.0]), pd.Series([1.0]))

    def test_constant_market_cap_does_not_divide_by_zero(self):
        result = mvrv_zscore(pd.Series([100.0] * 5), pd.Series([90.0] * 5))
        assert pd.isna(result.iloc[-1])


class TestPercentileZones:
    def test_top_of_observed_history_is_euphoria(self):
        historia = [float(i) / 10 for i in range(100)]
        leitura = zone_by_percentile(9.9, historia)
        assert leitura.percentile >= 0.95
        assert leitura.expensive

    def test_bottom_is_cheap(self):
        historia = [float(i) / 10 for i in range(100)]
        leitura = zone_by_percentile(0.0, historia)
        assert leitura.cheap
        assert not leitura.expensive

    def test_empty_history_is_neutral_not_a_crash(self):
        leitura = zone_by_percentile(3.0, [])
        assert leitura.percentile == 0.5
        assert "sem historico" in leitura.zone

    def test_explanation_mentions_the_percentile(self):
        assert "%" in zone_by_percentile(1.0, [0.0, 1.0, 2.0]).explain()


class TestClassicThresholdsAreUnreachable:
    """Documenta por que o filtro usa percentil e nao o limiar da literatura.

    Nos 4 anos de serie disponiveis (2022-09 a 2026-09) o maximo foi 3,35 e nao
    houve um unico dia acima de 4. Um filtro em `z > 7` ficaria inerte.
    """

    def test_classic_labels_still_work(self):
        assert "topo de ciclo" in zone_by_classic_threshold(8.0)
        assert "fundo" in zone_by_classic_threshold(-0.5)

    def test_recent_maximum_is_far_below_the_classic_top(self):
        maximo_observado = 3.35
        assert "neutro" in zone_by_classic_threshold(maximo_observado)


class TestReadingFromSeries:
    def _serie(self):
        return [
            (date(2026, 1, 1), 0.5),
            (date(2026, 2, 1), 1.5),
            (date(2026, 3, 1), 3.0),
            (date(2026, 4, 1), 2.0),
        ]

    def test_uses_only_the_past(self):
        """O backtest nao pode saber o futuro.

        Em 15/02 o valor 1,5 e o maior ja visto (percentil 100%). Se a serie
        inteira fosse usada, o 3,0 de marco o faria parecer mediano.
        """
        leitura = reading_from_series(self._serie(), datetime(2026, 2, 15, tzinfo=UTC))
        assert leitura is not None
        assert leitura.value == 1.5
        assert leitura.percentile == 1.0

    def test_carries_the_last_available_point(self):
        leitura = reading_from_series(self._serie(), datetime(2026, 3, 20, tzinfo=UTC))
        assert leitura is not None and leitura.value == 3.0

    def test_returns_none_before_the_series_starts(self):
        assert reading_from_series(self._serie(), datetime(2025, 1, 1, tzinfo=UTC)) is None

    def test_empty_series_returns_none(self):
        assert reading_from_series([], NOW) is None


class TestRegimeFilter:
    def _engine(self, pct: float) -> RiskEngine:
        from crypto_traders.config import RiskSettings

        return RiskEngine(
            RiskSettings(
                max_order_notional=Decimal("100"),
                max_order_pct_portfolio=0.10,
                max_asset_exposure_pct=0.30,
                min_order_notional=Decimal("10"),
                stop_loss_pct=0.03,
                take_profit_pct=0.06,
                min_signal_confidence=0.55,
                mvrv_max_percentile=pct,
                symbol_whitelist=["BTC/USDT"],
                asset_whitelist=["BTC"],
            ),
            "USDT",
        )

    def _sinal(self, direction=SignalDirection.LONG) -> Signal:
        return Signal(
            exchange=ExchangeName.BINANCE,
            symbol="BTC/USDT",
            timeframe="1d",
            strategy="teste",
            direction=direction,
            confidence=0.85,
            reason="teste",
            reference_price=Decimal("50000"),
        )

    def _estado(self, pct: float | None, positions=None) -> PortfolioState:
        return PortfolioState(
            total_value=Decimal("1000"),
            cash=Decimal("1000"),
            positions={k: Decimal(v) for k, v in (positions or {}).items()},
            prices={"BTC": Decimal("50000")},
            mvrv_percentile=pct,
        )

    def test_blocks_new_exposure_when_market_is_expensive(self):
        resultado = self._engine(0.80).evaluate(self._sinal(), self._estado(0.95), NOW)
        assert resultado.decision is RiskDecision.REJECTED
        assert any("MVRV" in r for r in resultado.reasons)

    def test_allows_when_below_the_threshold(self):
        resultado = self._engine(0.80).evaluate(self._sinal(), self._estado(0.40), NOW)
        assert resultado.decision is RiskDecision.APPROVED

    def test_never_blocks_a_close(self):
        """O indicador diz "esta caro", nao "fique preso na posicao".

        Travar a saida seria a mesma armadilha do circuit breaker.
        """
        resultado = self._engine(0.80).evaluate(
            self._sinal(SignalDirection.FLAT), self._estado(0.99, {"BTC": "0.01"}), NOW
        )
        assert resultado.decision is RiskDecision.APPROVED

    def test_missing_data_does_not_block(self):
        """Provedor externo fora do ar nao pode parar a negociacao.

        Sem o dado, o estado alternativo e o mesmo que o sistema teve durante
        todo o desenvolvimento: conhecido e testado, nao arriscado.
        """
        resultado = self._engine(0.80).evaluate(self._sinal(), self._estado(None), NOW)
        assert resultado.decision is RiskDecision.APPROVED

    def test_threshold_one_disables_the_filter(self):
        resultado = self._engine(1.0).evaluate(self._sinal(), self._estado(0.99), NOW)
        assert resultado.decision is RiskDecision.APPROVED

    def test_filter_is_off_by_default(self):
        """Padrao desligado: a medicao nao sustentou ganho de retorno."""
        from crypto_traders.config import RiskSettings

        assert RiskSettings().mvrv_max_percentile == 1.0

    def test_percentile_is_recorded_in_the_audit_snapshot(self):
        resultado = self._engine(0.80).evaluate(self._sinal(), self._estado(0.40), NOW)
        assert resultado.snapshot["mvrv_percentile"] == 0.40


class TestPersistence:
    async def test_series_round_trips_through_the_database(self, settings):
        from crypto_traders.db.repositories import OnChainMetricRepository
        from crypto_traders.db.session import session_scope

        pontos = [(date(2026, 1, 1), 0.5), (date(2026, 1, 2), 1.25)]
        async with session_scope(settings) as session:
            gravados = await OnChainMetricRepository(session).upsert_many("mvrv_zscore", pontos)
        assert gravados == 2

        async with session_scope(settings) as session:
            serie = await OnChainMetricRepository(session).series("mvrv_zscore")
        assert serie == pontos

    async def test_refetching_the_series_does_not_duplicate(self, settings):
        """Rebuscar a serie inteira e o caminho normal: o provedor nao recorta."""
        from crypto_traders.db.repositories import OnChainMetricRepository
        from crypto_traders.db.session import session_scope

        pontos = [(date(2026, 1, 1), 0.5)]
        async with session_scope(settings) as session:
            await OnChainMetricRepository(session).upsert_many("mvrv_zscore", pontos)
        async with session_scope(settings) as session:
            novos = await OnChainMetricRepository(session).upsert_many("mvrv_zscore", pontos)
        assert novos == 0

    async def test_latest_returns_the_most_recent_day(self, settings):
        from crypto_traders.db.repositories import OnChainMetricRepository
        from crypto_traders.db.session import session_scope

        async with session_scope(settings) as session:
            await OnChainMetricRepository(session).upsert_many(
                "mvrv_zscore", [(date(2026, 1, 1), 0.5), (date(2026, 3, 1), 2.0)]
            )
        async with session_scope(settings) as session:
            assert await OnChainMetricRepository(session).latest("mvrv_zscore") == (
                date(2026, 3, 1),
                2.0,
            )


class TestSeriesStaysFresh:
    """A serie MVRV nao pode congelar no dia do start.

    Com o filtro ligado e a serie parada, o percentil de um dia velho seguiria
    valendo por semanas. Dado velho que parece atual e pior que dado nenhum: o
    `None` pelo menos desliga o filtro de forma visivel no log.
    """

    def _orquestrador(self, settings):
        from crypto_traders.agents.orchestrator import Orchestrator

        return Orchestrator(settings)

    async def test_priming_fetches_even_with_the_filter_off(self, settings, monkeypatch):
        """O limiar vigente pode vir do banco, nao do `.env`.

        Condicionar a busca ao `.env` deixaria o filtro ligado pela interface e
        inerte por falta de dado -- o pior dos dois mundos.
        """
        import crypto_traders.agents.orchestrator as mod

        assert settings.risk.mvrv_max_percentile == 1.0

        chamadas: list[bool] = []

        async def falso_refresh(self, force=False):
            chamadas.append(force)
            return 0

        monkeypatch.setattr(mod.OnChainProvider, "refresh_mvrv", falso_refresh)

        orch = self._orquestrador(settings)
        orch.onchain = mod.OnChainProvider(settings)
        await orch._prime_portfolio()

        assert chamadas == [True]

    async def _rodar_loop(self, orch, monkeypatch, voltas: int) -> None:
        """Roda o loop por N voltas e o encerra.

        O sleep e substituido por um contador que levanta `CancelledError` na
        volta N -- exatamente como um `stop()` faria. Nao usamos `create_task`
        porque trocar `asyncio.sleep` mexe no modulo compartilhado e o proprio
        teste perderia como ceder controle.
        """
        import asyncio
        import contextlib

        import crypto_traders.agents.orchestrator as mod

        restante = {"n": voltas}

        async def sleep_contado(_segundos):
            restante["n"] -= 1
            if restante["n"] < 0:
                raise asyncio.CancelledError

        monkeypatch.setattr(mod.asyncio, "sleep", sleep_contado)
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await orch._refresh_onchain_forever()
        finally:
            monkeypatch.undo()

    async def test_loop_never_forces_the_fetch(self, settings, monkeypatch):
        """O loop acorda de hora em hora, mas nao busca de hora em hora.

        Quem decide se vale ir a rede e o provedor (piso de 12h). Passar
        `force=True` aqui furaria esse piso e criaria duas verdades sobre a
        mesma regra.
        """
        import crypto_traders.agents.orchestrator as mod

        chamadas: list[bool] = []

        async def falso_refresh(self, force=False):
            chamadas.append(force)
            return 0

        monkeypatch.setattr(mod.OnChainProvider, "refresh_mvrv", falso_refresh)

        orch = self._orquestrador(settings)
        orch.onchain = mod.OnChainProvider(settings)
        await self._rodar_loop(orch, monkeypatch, voltas=3)

        assert chamadas == [False, False, False]

    async def test_a_failing_provider_does_not_kill_the_loop(self, settings, monkeypatch):
        import crypto_traders.agents.orchestrator as mod

        tentativas: list[int] = []

        async def explode(self, force=False):
            tentativas.append(1)
            raise RuntimeError("provedor fora do ar")

        monkeypatch.setattr(mod.OnChainProvider, "refresh_mvrv", explode)

        orch = self._orquestrador(settings)
        orch.onchain = mod.OnChainProvider(settings)
        await self._rodar_loop(orch, monkeypatch, voltas=3)

        assert len(tentativas) == 3, "o loop parou na primeira falha"
