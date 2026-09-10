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


def sinal_direcionado(symbol: str, direction: SignalDirection, preco: str) -> Signal:
    return Signal(
        exchange=ExchangeName.BINANCE,
        symbol=symbol,
        timeframe="1d",
        strategy="teste",
        direction=direction,
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


class TestTheGateSurvivesRecycling:
    """Os furos que a reciclagem e a valorizacao poderiam abrir no portao."""

    def test_a_position_worth_more_than_the_authorization_stops_new_orders(self):
        """R$150 autorizados, posicao valorizou para R$200: nada de nova ordem.

        `authorized - aplicado` fica negativo, e o caixa utilizavel tem de ser
        ZERO -- nao um numero negativo, nao o caixa livre inteiro.
        """
        engine = RiskEngine(limites(authorized_capital=Decimal("150")), "BRL")
        state = estado(
            "1000", "800",
            posicoes={"ETH": "2"},
            precos={"BTC": "100", "ETH": "100"},
        )
        assert state.usable_cash(Decimal("150")) == Decimal(0)
        r = engine.evaluate(sinal(), state)
        assert r.decision is RiskDecision.REJECTED

    def test_an_illiquid_close_does_not_finance_an_opening(self):
        """Po abaixo do minimo negociavel NAO vende, e nao pode liberar caixa.

        Medido com a configuracao de producao antes da correcao: 29,29 USDC
        autorizados, todos aplicados em sete posicoes de poeira de 4 USDC (o
        minimo da exchange e 5). Os sete fechamentos, aprovados de proposito
        porque D4 nunca trava uma saida, creditavam 28 USDC de caixa imaginario
        no estado simulado e liberavam 11,72 USDC de NOVA exposicao -- exposicao
        total de 39,72 sobre 29,29 autorizados, 35,6% acima do portao.
        """
        limits = limites(
            authorized_capital=Decimal("29.29"),
            max_order_pct_portfolio=0.20,
            max_asset_exposure_pct=0.45,
            min_order_notional=Decimal("5"),
            symbol_whitelist=[f"P{i}/BRL" for i in range(7)] + ["BTC/BRL"],
            asset_whitelist=[f"P{i}" for i in range(7)] + ["BTC"],
        )
        engine = RiskEngine(limits, "BRL")
        state = PortfolioState(
            total_value=Decimal("29.29"),
            cash=Decimal("1.29"),
            positions={f"P{i}": Decimal("4") for i in range(7)},
            prices={f"P{i}": Decimal("1") for i in range(7)} | {"BTC": Decimal("100")},
        )
        lote = [
            sinal_direcionado(f"P{i}/BRL", SignalDirection.FLAT, "1") for i in range(7)
        ] + [sinal("BTC/BRL")]

        decisoes = engine.evaluate_batch(lote, state)
        nova_exposicao = sum(
            (a.approved_notional or Decimal(0))
            for s, a in decisoes
            if s.direction is SignalDirection.LONG and a.decision is RiskDecision.APPROVED
        )
        # O po continua na carteira porque a exchange nao vende 4 abaixo de 5.
        assert Decimal("28") + nova_exposicao <= Decimal("29.29")

    def test_a_liquid_close_still_frees_the_authorization(self):
        """A correcao nao pode matar a rotacao legitima: venda de verdade libera."""
        limits = limites(
            authorized_capital=Decimal("100"),
            max_order_pct_portfolio=0.50,
            max_asset_exposure_pct=1.0,
            min_order_notional=Decimal("5"),
        )
        engine = RiskEngine(limits, "BRL")
        state = PortfolioState(
            total_value=Decimal("100"),
            cash=Decimal(0),
            positions={"ETH": Decimal("1")},
            prices={"ETH": Decimal("100"), "BTC": Decimal("100")},
        )
        decisoes = engine.evaluate_batch(
            [sinal_direcionado("ETH/BRL", SignalDirection.FLAT, "100"), sinal("BTC/BRL")],
            state,
        )
        aprovados = {
            s.symbol for s, a in decisoes if a.decision is RiskDecision.APPROVED
        }
        assert aprovados == {"ETH/BRL", "BTC/BRL"}


class TestUnauthorizedValue:
    """O numero que a interface mostra como "esperando aval"."""

    def test_it_is_the_excess_over_the_authorization(self):
        assert estado("650", "650").unauthorized_value(Decimal("150")) == Decimal("500")

    def test_it_never_goes_negative(self):
        """Patrimonio abaixo do autorizado nao e saldo negativo esperando aval."""
        assert estado("100", "100").unauthorized_value(Decimal("150")) == Decimal(0)

    def test_without_a_gate_nothing_waits_for_approval(self):
        assert estado("650", "650").unauthorized_value(None) == Decimal(0)


class TestAuthorizingTheWholeBalance:
    """Autorizar grava um NUMERO, nao desliga o portao.

    Desligar autorizaria tambem todo deposito futuro, que e exatamente o que o
    portao existe para impedir. Autorizar e um ato sobre o saldo de hoje.
    """

    async def _agente(self, settings):
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        bus = InMemoryEventBus()
        await bus.start()
        configurado = settings.model_copy(
            update={"risk": limites(authorized_capital=Decimal("150"))}
        )
        return RiskManagerAgent(bus, configurado)

    def _snapshot(self, total: str):
        from crypto_traders.domain.models import PortfolioSnapshot

        return PortfolioSnapshot(
            total_value=Decimal(total),
            cash_value=Decimal(total),
            positions_value=Decimal(0),
        )

    async def test_it_refuses_before_the_first_snapshot(self, settings):
        """Sem patrimonio apurado, autorizar seria autorizar um numero inventado."""
        agente = await self._agente(settings)
        with pytest.raises(ValueError, match="ainda nao apurado"):
            await agente.authorize_all_capital()

    async def test_it_records_the_balance_of_today_not_a_disabled_gate(self, settings):
        agente = await self._agente(settings)
        agente.observe_snapshot(self._snapshot("650"))

        limites_novos = await agente.authorize_all_capital(actor="paulinho")

        assert limites_novos.authorized_capital == Decimal("650")
        # O portao continua LIGADO: um aporte futuro volta a esperar aval.
        assert await agente.check_capital_authorization(self._snapshot("1000")) == Decimal(
            "350"
        )

    async def test_authorizing_is_audited(self, settings):
        from crypto_traders.db.repositories import AuditLogRepository
        from crypto_traders.db.session import session_scope

        agente = await self._agente(settings)
        agente.observe_snapshot(self._snapshot("650"))
        await agente.authorize_all_capital(actor="paulinho")

        async with session_scope(settings) as session:
            entradas = await AuditLogRepository(session).list()
        assert any(
            e.action == "risk_limits_updated" and e.actor == "paulinho" for e in entradas
        )


class TestTheBreakerMeasuresAgainstTheAuthorizedCapital:
    """A base do percentual e o capital autorizado, nao o saldo total.

    Com o portao em R$150 sobre uma conta de R$650, medir contra 650 tornaria a
    trava quatro vezes mais frouxa do que o configurado.
    """

    async def _agente(self, settings, autorizado):
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        bus = InMemoryEventBus()
        await bus.start()
        configurado = settings.model_copy(
            update={"risk": limites(
                authorized_capital=autorizado,
                daily_loss_limit_pct=0.05,
                weekly_loss_limit_pct=0.12,
            )}
        )
        return RiskManagerAgent(bus, configurado)

    async def _referencia(self, settings, resultado: str, quando):
        from crypto_traders.db.repositories import PortfolioSnapshotRepository
        from crypto_traders.db.session import session_scope
        from crypto_traders.domain.models import PortfolioSnapshot

        async with session_scope(settings) as session:
            await PortfolioSnapshotRepository(session).save(
                PortfolioSnapshot(
                    total_value=Decimal("650"),
                    cash_value=Decimal("650"),
                    positions_value=Decimal(0),
                    realized_pnl=Decimal(resultado),
                    timestamp=quando,
                ),
                "dry_run",
            )

    def _agora(self):
        from datetime import UTC, datetime

        return datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)

    def _snapshot(self, resultado: str, quando, total: str = "650"):
        from crypto_traders.domain.models import PortfolioSnapshot

        return PortfolioSnapshot(
            total_value=Decimal(total),
            cash_value=Decimal(total),
            positions_value=Decimal(0),
            realized_pnl=Decimal(resultado),
            timestamp=quando,
        )

    async def test_a_loss_measured_against_the_gate_trips_it(self, settings):
        """Perda de R$10 sobre R$150 autorizados = 6,7%, acima dos 5%.

        Prejuizo de verdade: o resultado de negociacao E o patrimonio caem os
        mesmos R$10, que e o que uma perda de mercado faz.
        """
        agora = self._agora()
        agente = await self._agente(settings, Decimal("150"))
        await self._referencia(settings, "0", agora.replace(hour=0, minute=1))

        motivo = await agente.check_circuit_breaker(
            self._snapshot("-10", agora, total="640")
        )
        assert motivo is not None and "perda diaria" in motivo

    async def test_the_same_loss_against_the_whole_balance_would_not_trip(self, settings):
        """Prova que a base importa: R$10 sobre R$650 seria 1,5%, dentro do limite."""
        agora = self._agora()
        agente = await self._agente(settings, None)
        await self._referencia(settings, "0", agora.replace(hour=0, minute=1))

        assert await agente.check_circuit_breaker(
            self._snapshot("-10", agora, total="640")
        ) is None

    async def test_a_result_drop_without_an_equity_drop_is_bookkeeping(self, settings):
        """As duas medidas tem de confirmar a queda -- esta e a diferenca.

        Resultado de negociacao caindo R$10 sobre R$150 autorizados daria 6,7%,
        acima do limite. Mas o patrimonio nao se moveu, e prejuizo de mercado
        move os dois: e artefato de contabilidade (ordem em voo, lancamento
        apagado, lucro realizado), nao perda.
        """
        agora = self._agora()
        agente = await self._agente(settings, Decimal("150"))
        await self._referencia(settings, "0", agora.replace(hour=0, minute=1))

        # Dois retratos seguidos: o estado pode durar o dia inteiro, e nenhum
        # deles dispara -- nem o aviso da divergencia se repete a cada 60s.
        for _ in range(2):
            assert await agente.check_circuit_breaker(
                self._snapshot("-10", agora, total="650")
            ) is None
        assert not agente.circuit_breaker_active

    async def test_an_equity_drop_without_a_result_drop_is_a_withdrawal(self, settings):
        """O outro lado da mesma moeda, e o defeito original: saque nao e perda."""
        agora = self._agora()
        agente = await self._agente(settings, Decimal("150"))
        await self._referencia(settings, "0", agora.replace(hour=0, minute=1))

        assert await agente.check_circuit_breaker(
            self._snapshot("0", agora, total="150")
        ) is None
        assert not agente.circuit_breaker_active

    async def test_realizing_a_profit_is_not_a_loss(self, settings):
        """Fechar no lucro nao pode ser lido como prejuizo e pausar o sistema."""
        agora = self._agora()
        agente = await self._agente(settings, Decimal("29.29"))
        await self._referencia(settings, "3", agora.replace(hour=0, minute=1))

        # Continua com o mesmo resultado de negociacao: nada mudou.
        assert await agente.check_circuit_breaker(self._snapshot("3", agora)) is None

    async def test_without_a_base_it_cannot_measure_and_says_so(self, settings):
        """Zero autorizado: nada a proteger, e a trava nao dispara por acaso."""
        agora = self._agora()
        agente = await self._agente(settings, Decimal("0"))
        await self._referencia(settings, "0", agora.replace(hour=0, minute=1))

        assert await agente.check_circuit_breaker(self._snapshot("-100", agora)) is None
        assert not agente.circuit_breaker_active

    async def test_a_deposit_is_not_a_profit_and_a_withdrawal_is_not_a_loss(self, settings):
        """O patrimonio muda, o resultado de negociacao nao -- e a trava segue.

        Foi o defeito do primeiro ensaio: mudar o saldo simulado de 1000 para
        150 disparou "perda de 85%" em tres minutos, culpando um prejuizo que
        nao existiu.
        """
        agora = self._agora()
        agente = await self._agente(settings, None)
        await self._referencia(settings, "0", agora.replace(hour=0, minute=1))

        # Saque de 500: patrimonio despenca, resultado de negociacao intacto.
        assert await agente.check_circuit_breaker(
            self._snapshot("0", agora, total="150")
        ) is None
        assert not agente.circuit_breaker_active
        # E o inverso: deposito de 350, resultado intacto.
        assert await agente.check_circuit_breaker(
            self._snapshot("0", agora, total="1000")
        ) is None
        assert not agente.circuit_breaker_active


    async def test_the_divergence_is_reported_once_not_every_snapshot(self, settings):
        """Com o Portfolio Agent a cada 60s, avisar sempre seriam 1.440 por dia.

        O detalhe que quebrava isto: a avaliacao SEMANAL, dentro do limite,
        zerava o aviso da DIARIA a cada retrato -- e a mensagem voltava a sair
        de minuto em minuto.
        """
        from structlog.testing import capture_logs

        agora = self._agora()
        agente = await self._agente(settings, Decimal("150"))
        await self._referencia(settings, "0", agora.replace(hour=0, minute=1))

        with capture_logs() as registros:
            for _ in range(5):
                assert await agente.check_circuit_breaker(
                    self._snapshot("-10", agora, total="650")
                ) is None

        avisos = [r for r in registros if r["event"] == "risk.loss_not_confirmed_by_equity"]
        assert len(avisos) == 1
        assert avisos[0]["periodo"] == "diaria"

    async def test_the_divergence_warning_rearms_when_it_goes_away(self, settings):
        from structlog.testing import capture_logs

        agora = self._agora()
        agente = await self._agente(settings, Decimal("150"))
        await self._referencia(settings, "0", agora.replace(hour=0, minute=1))

        with capture_logs() as registros:
            await agente.check_circuit_breaker(self._snapshot("-10", agora, total="650"))
            # Resultado de negociacao volta ao normal: nada divergente a avisar.
            await agente.check_circuit_breaker(self._snapshot("0", agora, total="650"))
            # E divergindo de novo, o aviso volta a sair.
            await agente.check_circuit_breaker(self._snapshot("-10", agora, total="650"))

        avisos = [r for r in registros if r["event"] == "risk.loss_not_confirmed_by_equity"]
        assert len(avisos) == 2
