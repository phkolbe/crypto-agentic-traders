"""Testes da verificação de dimensionamento.

O caso que motivou isto: R$100 (~19 USDT) com os limites de fábrica. O sistema
sobe, coleta dados, gera sinais e rejeita **todos** — com heartbeat verde e
dashboard atualizando. Parece saudável e nunca vai operar.

Os números aqui vêm da Binance real: USDT/BRL a 5,18 e mínimo de 5 USDT por
ordem na exchange.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_traders.config import RiskSettings
from crypto_traders.risk.rules import assess_sizing_feasibility

#: R$100, R$1.000 e R$5.000 convertidos a 5,1838 BRL/USDT.
R100 = Decimal("19.29")
R1000 = Decimal("192.91")
R5000 = Decimal("964.54")


class TestInfeasibleSizing:
    def test_hundred_reais_cannot_produce_any_order(self):
        """O caso real: 2% de 19,29 são 39 centavos, mínimo é 10 USDT."""
        result = assess_sizing_feasibility(RiskSettings(), R100)
        assert not result.feasible
        assert result.max_possible_order < Decimal("1")

    def test_thousand_reais_still_cannot(self):
        assert not assess_sizing_feasibility(RiskSettings(), R1000).feasible

    def test_explanation_says_how_much_would_be_needed(self):
        """Apontar o problema sem o remédio deixaria o operador adivinhando."""
        result = assess_sizing_feasibility(RiskSettings(), R100)
        text = result.explain("USDT")
        assert "NENHUMA ordem e possivel" in text
        assert str(result.minimum_portfolio) in text

    def test_minimum_portfolio_is_actually_feasible(self):
        """O valor sugerido precisa funcionar de verdade, não ser aproximação."""
        limits = RiskSettings()
        suggested = assess_sizing_feasibility(limits, R100).minimum_portfolio
        assert assess_sizing_feasibility(limits, suggested).feasible


class TestFeasibleSizing:
    def test_five_thousand_reais_works(self):
        result = assess_sizing_feasibility(RiskSettings(), R5000)
        assert result.feasible
        assert result.max_possible_order == Decimal("19.2908")

    def test_absolute_cap_binds_on_large_portfolios(self):
        """Com carteira grande, o teto absoluto passa a ser o limitador."""
        result = assess_sizing_feasibility(RiskSettings(), Decimal("100000"))
        assert result.feasible
        assert result.max_possible_order == Decimal("50")
        assert "teto absoluto" in result.binding_limit


class TestBindingLimit:
    def test_identifies_the_percentage_cap(self):
        result = assess_sizing_feasibility(RiskSettings(), R100)
        assert "2%" in result.binding_limit

    def test_identifies_the_exposure_cap_when_it_is_tighter(self):
        """A exposição por ativo travou o caso de R$100 mesmo após afrouxar o %.

        Apontar o limite errado manda o operador investigar em vão.
        """
        limits = RiskSettings(max_order_pct_portfolio=0.55, max_asset_exposure_pct=0.30)
        result = assess_sizing_feasibility(limits, R100)
        assert not result.feasible
        assert "exposicao" in result.binding_limit

    def test_loosening_both_limits_makes_it_feasible(self):
        """Confirma o que a análise mostrou: são DOIS limites, não um."""
        limits = RiskSettings(max_order_pct_portfolio=0.55, max_asset_exposure_pct=0.60)
        assert assess_sizing_feasibility(limits, R100).feasible


class TestBoundary:
    def test_exactly_at_the_minimum_is_feasible(self):
        limits = RiskSettings(min_order_notional=Decimal("10"), max_order_pct_portfolio=0.02)
        assert assess_sizing_feasibility(limits, Decimal("500")).feasible

    def test_just_below_is_not(self):
        limits = RiskSettings(min_order_notional=Decimal("10"), max_order_pct_portfolio=0.02)
        assert not assess_sizing_feasibility(limits, Decimal("499")).feasible

    def test_zero_portfolio_is_infeasible(self):
        assert not assess_sizing_feasibility(RiskSettings(), Decimal("0")).feasible


class TestOrchestratorIntegration:
    async def _snapshot(self, total: str):
        from crypto_traders.domain.models import PortfolioSnapshot

        return PortfolioSnapshot(
            total_value=Decimal(total),
            cash_value=Decimal(total),
            positions_value=Decimal(0),
        )

    async def _orchestrator(self, settings):
        from crypto_traders.agents.market_data import MarketDataAgent
        from crypto_traders.agents.orchestrator import Orchestrator
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        orchestrator = Orchestrator(settings)
        orchestrator.bus = InMemoryEventBus()
        await orchestrator.bus.start()
        orchestrator.risk_manager = RiskManagerAgent(orchestrator.bus, settings)
        orchestrator.market_data = MarketDataAgent(orchestrator.bus, None, settings)
        return orchestrator

    async def test_alerts_when_no_order_is_possible(self, settings):
        import asyncio

        from crypto_traders.bus import Topics

        orchestrator = await self._orchestrator(settings)
        alerts: list[dict] = []

        async def listen():
            async for alert in orchestrator.bus.subscribe(Topics.ALERTS):
                alerts.append(alert)

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        await orchestrator._check_sizing(await self._snapshot("19.29"))
        await asyncio.sleep(0.05)
        task.cancel()

        assert len(alerts) == 1
        assert alerts[0]["type"] == "sizing_infeasible"
        assert "rejeitados" in alerts[0]["message"]

    async def test_alerts_once_not_every_snapshot(self, settings):
        """O Portfolio Agent produz um snapshot por minuto; alertar sempre viraria spam."""
        import asyncio

        from crypto_traders.bus import Topics

        orchestrator = await self._orchestrator(settings)
        alerts: list[dict] = []

        async def listen():
            async for alert in orchestrator.bus.subscribe(Topics.ALERTS):
                alerts.append(alert)

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        for _ in range(5):
            await orchestrator._check_sizing(await self._snapshot("19.29"))
        await asyncio.sleep(0.05)
        task.cancel()

        assert len(alerts) == 1

    async def test_no_alert_when_sizing_is_fine(self, settings):
        import asyncio

        from crypto_traders.bus import Topics

        orchestrator = await self._orchestrator(settings)
        alerts: list[dict] = []

        async def listen():
            async for alert in orchestrator.bus.subscribe(Topics.ALERTS):
                alerts.append(alert)

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        await orchestrator._check_sizing(await self._snapshot("5000"))
        await asyncio.sleep(0.05)
        task.cancel()

        assert alerts == []
        assert orchestrator.sizing is not None and orchestrator.sizing.feasible

    async def test_rearms_after_recovery(self, settings):
        """Se o patrimônio subir e cair de novo, o alerta precisa voltar."""
        orchestrator = await self._orchestrator(settings)
        await orchestrator._check_sizing(await self._snapshot("19.29"))
        assert orchestrator._sizing_alerted

        await orchestrator._check_sizing(await self._snapshot("5000"))
        assert not orchestrator._sizing_alerted

        await orchestrator._check_sizing(await self._snapshot("19.29"))
        assert orchestrator._sizing_alerted


class TestCheckCommand:
    def test_check_fails_when_no_order_is_possible(self, settings, capsys):
        """"Tudo pronto" para um sistema que nunca vai operar seria pior que um erro."""
        from crypto_traders.cli import _check_sizing

        tiny = settings.model_copy(update={"paper_initial_balance": R100})
        # `None` significa inviavel; quando viavel devolve o resultado, que a
        # checagem de filtros da exchange usa para simular a ordem.
        assert _check_sizing(tiny) is None
        output = capsys.readouterr().out
        assert "NENHUMA ordem" in output
        assert "RISK_MAX_ORDER_PCT_PORTFOLIO" in output

    def test_check_passes_with_a_workable_balance(self, settings, capsys):
        from crypto_traders.cli import _check_sizing

        result = _check_sizing(settings.model_copy(update={"paper_initial_balance": R5000}))
        assert result is not None and result.feasible
        assert "maior ordem possivel" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("brl", "expected"),
    [(100, False), (500, False), (1000, False), (2600, True), (5000, True)],
)
def test_matches_the_measured_thresholds(brl, expected):
    """Congela os valores apurados com a cotação real: R$2.600 é a virada."""
    usdt = (Decimal(brl) / Decimal("5.1838")).quantize(Decimal("0.01"))
    assert assess_sizing_feasibility(RiskSettings(), usdt).feasible is expected
