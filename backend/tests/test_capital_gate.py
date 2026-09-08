"""O portão de capital autorizado.

Um depósito não é uma ordem. Dinheiro que entra na conta por qualquer motivo --
venda de outro ativo, transferência, reserva para outra finalidade -- não deveria
virar exposição sem alguém dizer que sim.

Mas dinheiro autorizado e parado é o problema oposto, e igualmente real: o
sistema ficaria de pé com caixa ocioso sem ninguém perceber. Por isso o saldo não
autorizado gera alerta ativo, e não apenas uma linha no `check`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from crypto_traders.config import RiskSettings
from crypto_traders.domain.enums import ExchangeName, RiskDecision, SignalDirection
from crypto_traders.domain.models import Signal
from crypto_traders.risk.rules import PortfolioState, RiskEngine, assess_sizing_feasibility


def limites(**kw) -> RiskSettings:
    base = {
        "max_order_notional": None,
        "max_order_pct_portfolio": 0.15,
        "max_asset_exposure_pct": 0.25,
        "max_open_positions": None,
        "min_order_notional": Decimal("15"),
        "stop_loss_pct": 0.03,
        "take_profit_pct": 0.06,
        "min_signal_confidence": 0.55,
        "cooldown_seconds": 0,
        "symbol_whitelist": ["BTC/BRL", "ETH/BRL", "SOL/BRL", "XRP/BRL"],
        "asset_whitelist": ["BTC", "ETH", "SOL", "XRP"],
    }
    return RiskSettings(**{**base, **kw})


def sinal(symbol: str = "BTC/BRL", preco: str = "100") -> Signal:
    return Signal(
        exchange=ExchangeName.BINANCE,
        symbol=symbol,
        timeframe="1d",
        strategy="teste",
        direction=SignalDirection.LONG,
        confidence=0.9,
        reason="teste",
        reference_price=Decimal(preco),
    )


def estado(total: str, caixa: str, posicoes=None, precos=None) -> PortfolioState:
    return PortfolioState(
        total_value=Decimal(total),
        cash=Decimal(caixa),
        positions={k: Decimal(v) for k, v in (posicoes or {}).items()},
        prices={k: Decimal(v) for k, v in (precos or {"BTC": "100"}).items()},
    )


class TestOptionalCaps:
    """Os dois tetos que impediam o auto-ajuste passam a aceitar "sem limite"."""

    def test_no_absolute_cap_lets_the_order_follow_the_portfolio(self):
        """Era o que travava: acima de ~R$167 o teto fixo congelava a ordem."""
        engine = RiskEngine(limites(), "BRL")
        for total, esperado in (("150", "22.50"), ("1000", "150.00"), ("10000", "1500.00")):
            r = engine.evaluate(sinal(), estado(total, total))
            assert r.decision is RiskDecision.APPROVED
            assert r.approved_notional == pytest.approx(Decimal(esperado), rel=Decimal("0.001"))

    def test_an_absolute_cap_still_binds_when_set(self):
        engine = RiskEngine(limites(max_order_notional=Decimal("25")), "BRL")
        r = engine.evaluate(sinal(), estado("1000", "1000"))
        assert r.approved_notional == Decimal("25")

    def test_no_position_limit_lets_cash_decide(self):
        """Sem teto de posicoes, quem limita e o caixa -- e isso basta.

        Com 15% por ordem, a sexta ordem ja encontra caixa insuficiente para o
        minimo, e o sistema para sozinho sem numero magico nenhum.
        """
        engine = RiskEngine(limites(), "BRL")
        # Sete posicoes abertas e caixa de R$10: abaixo do minimo de R$15.
        state = estado(
            "150", "10",
            posicoes={"ETH": "1", "SOL": "1"},
            precos={"BTC": "100", "ETH": "70", "SOL": "70"},
        )
        r = engine.evaluate(sinal(), state)
        assert r.decision is RiskDecision.REJECTED
        assert any("abaixo do minimo" in m for m in r.reasons)

    def test_a_position_limit_still_binds_when_set(self):
        engine = RiskEngine(limites(max_open_positions=1), "BRL")
        state = estado("1000", "900", posicoes={"ETH": "1"}, precos={"BTC": "100", "ETH": "100"})
        r = engine.evaluate(sinal(), state)
        assert r.decision is RiskDecision.REJECTED
        assert any("posicoes abertas" in m for m in r.reasons)

    def test_minimum_above_an_absent_maximum_is_allowed(self):
        """Sem teto nao existe incoerencia entre minimo e maximo."""
        assert limites(min_order_notional=Decimal("1000")).max_order_notional is None

    def test_minimum_above_a_present_maximum_is_refused(self):
        with pytest.raises(ValidationError, match="ordem minima"):
            limites(max_order_notional=Decimal("10"), min_order_notional=Decimal("20"))


class TestTheGate:
    def test_unauthorized_balance_does_not_grow_the_order(self):
        """O ponto do portao: R$1.000 na conta, R$150 autorizados."""
        engine = RiskEngine(limites(authorized_capital=Decimal("150")), "BRL")
        r = engine.evaluate(sinal(), estado("1000", "1000"))
        assert r.decision is RiskDecision.APPROVED
        # 15% de 150, nao de 1000.
        assert r.approved_notional == pytest.approx(Decimal("22.50"), rel=Decimal("0.001"))

    def test_no_gate_means_the_whole_portfolio(self):
        engine = RiskEngine(limites(authorized_capital=None), "BRL")
        r = engine.evaluate(sinal(), estado("1000", "1000"))
        assert r.approved_notional == pytest.approx(Decimal("150"), rel=Decimal("0.001"))

    def test_zero_authorized_blocks_everything_with_a_clear_reason(self):
        """Autorizar zero e uma escolha valida: o sistema para de abrir posicao."""
        engine = RiskEngine(limites(authorized_capital=Decimal("0")), "BRL")
        r = engine.evaluate(sinal(), estado("1000", "1000"))
        assert r.decision is RiskDecision.REJECTED
        assert any("autorize" in m.lower() for m in r.reasons)

    def test_open_positions_consume_the_authorization(self):
        """Sem isto o portao seria furado pela reciclagem.

        Com R$150 autorizados e R$140 ja aplicados, sobram R$10 -- abaixo do
        minimo. Se o caixa livre fosse olhado sozinho, R$860 de saldo nao
        autorizado financiariam a ordem.
        """
        engine = RiskEngine(limites(authorized_capital=Decimal("150")), "BRL")
        state = estado(
            "1000", "860",
            posicoes={"ETH": "1.4"},
            precos={"BTC": "100", "ETH": "100"},
        )
        r = engine.evaluate(sinal(), state)
        assert r.decision is RiskDecision.REJECTED
        assert any("abaixo do minimo" in m for m in r.reasons)

    def test_exposure_cap_also_uses_the_authorized_capital(self):
        """25% de 1.000 seria R$250; do autorizado, R$37,50."""
        engine = RiskEngine(limites(authorized_capital=Decimal("150")), "BRL")
        state = estado(
            "1000", "960",
            posicoes={"BTC": "0.35"},
            precos={"BTC": "100"},
        )
        r = engine.evaluate(sinal(), state)
        # Exposicao em BTC ja e R$35, teto R$37,50: sobra R$2,50, abaixo do minimo.
        assert r.decision is RiskDecision.REJECTED
        assert any("exposicao" in m.lower() or "minimo" in m for m in r.reasons)


class TestSizingFeasibilityRespectsTheGate:
    def test_it_measures_the_authorized_capital_not_the_balance(self):
        """Dizer "tudo pronto" sobre saldo intocavel seria o engano de sempre."""
        r = assess_sizing_feasibility(
            limites(authorized_capital=Decimal("50")), Decimal("10000")
        )
        assert r.portfolio_value == Decimal("50")
        assert not r.feasible

    def test_an_absent_cap_is_not_the_binding_limit(self):
        r = assess_sizing_feasibility(limites(), Decimal("1000"))
        assert r.feasible
        assert "teto absoluto" not in r.binding_limit


class TestTheAlert:
    async def _agente(self, settings, autorizado="150"):
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        bus = InMemoryEventBus()
        await bus.start()
        configurado = settings.model_copy(
            update={"risk": limites(authorized_capital=Decimal(autorizado))}
        )
        return RiskManagerAgent(bus, configurado)

    def _snapshot(self, total: str):
        from crypto_traders.domain.models import PortfolioSnapshot

        return PortfolioSnapshot(
            total_value=Decimal(total),
            cash_value=Decimal(total),
            positions_value=Decimal(0),
        )

    async def test_it_reports_the_idle_amount(self, settings):
        agente = await self._agente(settings, autorizado="150")
        assert await agente.check_capital_authorization(self._snapshot("650")) == Decimal("500")

    async def test_nothing_to_report_when_fully_authorized(self, settings):
        agente = await self._agente(settings, autorizado="650")
        assert await agente.check_capital_authorization(self._snapshot("650")) == Decimal(0)

    async def test_small_variation_is_not_worth_an_alert(self, settings):
        """Posicao valorizando move o patrimonio sem ninguem depositar nada.

        Alertar por R$3 de oscilacao transformaria o aviso em ruido, e um alerta
        que chega sempre deixa de ser lido.
        """
        agente = await self._agente(settings, autorizado="150")
        alertas: list[dict] = []

        from crypto_traders.bus import Topics

        async def escuta():
            async for a in agente.bus.subscribe(Topics.ALERTS):
                alertas.append(a)

        import asyncio

        tarefa = asyncio.create_task(escuta())
        await asyncio.sleep(0)
        await agente.check_capital_authorization(self._snapshot("153"))
        await asyncio.sleep(0.05)
        tarefa.cancel()
        assert alertas == []

    async def test_the_alert_fires_once_per_transition(self, settings):
        """A cada 60s o mesmo saldo geraria 1.440 mensagens por dia."""
        import asyncio

        from crypto_traders.bus import Topics

        agente = await self._agente(settings, autorizado="150")
        alertas: list[dict] = []

        async def escuta():
            async for a in agente.bus.subscribe(Topics.ALERTS):
                alertas.append(a)

        tarefa = asyncio.create_task(escuta())
        await asyncio.sleep(0)
        for _ in range(5):
            await agente.check_capital_authorization(self._snapshot("650"))
        await asyncio.sleep(0.05)
        tarefa.cancel()

        assert len(alertas) == 1
        assert alertas[0]["type"] == "unauthorized_capital"
        assert "500.00" in alertas[0]["title"]

    async def test_it_rearms_after_the_balance_is_authorized(self, settings):
        """Autorizar e depois receber outro aporte precisa avisar de novo."""
        import asyncio

        from crypto_traders.bus import Topics

        agente = await self._agente(settings, autorizado="150")
        alertas: list[dict] = []

        async def escuta():
            async for a in agente.bus.subscribe(Topics.ALERTS):
                alertas.append(a)

        tarefa = asyncio.create_task(escuta())
        await asyncio.sleep(0)
        await agente.check_capital_authorization(self._snapshot("650"))
        await agente.check_capital_authorization(self._snapshot("150"))  # autorizado/consumido
        await agente.check_capital_authorization(self._snapshot("650"))
        await asyncio.sleep(0.05)
        tarefa.cancel()
        assert len(alertas) == 2

    async def test_no_gate_means_no_alert(self, settings):
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        bus = InMemoryEventBus()
        await bus.start()
        agente = RiskManagerAgent(
            bus, settings.model_copy(update={"risk": limites(authorized_capital=None)})
        )
        assert await agente.check_capital_authorization(self._snapshot("10000")) == Decimal(0)
