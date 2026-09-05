"""Testes do motor de regras de risco.

Este e o componente que decide se dinheiro real e usado. Cada regra tem teste do
caso que aprova, do caso que rejeita e da fronteira entre os dois -- e as
assimetrias entre abrir e fechar posicao sao testadas explicitamente, porque sao
justamente o tipo de detalhe que uma refatoracao futura apagaria sem perceber.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_traders.domain.enums import ExchangeName, RiskDecision, SignalDirection
from crypto_traders.domain.models import Signal
from crypto_traders.risk.rules import PortfolioState, RiskEngine

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make_signal(
    *,
    symbol: str = "BTC/USDT",
    direction: SignalDirection = SignalDirection.LONG,
    confidence: float = 0.80,
    price: str = "50000",
) -> Signal:
    return Signal(
        exchange=ExchangeName.BINANCE,
        symbol=symbol,
        timeframe="15m",
        strategy="test",
        direction=direction,
        confidence=confidence,
        reason="teste",
        reference_price=Decimal(price),
    )


def make_state(
    *,
    total: str = "1000",
    cash: str = "1000",
    positions: dict[str, str] | None = None,
    prices: dict[str, str] | None = None,
    last_order_at: dict[str, datetime] | None = None,
    circuit_breaker: bool = False,
) -> PortfolioState:
    return PortfolioState(
        total_value=Decimal(total),
        cash=Decimal(cash),
        positions={k: Decimal(v) for k, v in (positions or {}).items()},
        prices={k: Decimal(v) for k, v in (prices or {"BTC": "50000"}).items()},
        last_order_at=last_order_at or {},
        circuit_breaker_active=circuit_breaker,
        circuit_breaker_reason="perda diaria de 6%" if circuit_breaker else None,
    )


@pytest.fixture
def engine(risk_limits) -> RiskEngine:
    return RiskEngine(risk_limits, quote_currency="USDT")


class TestApproval:
    def test_approves_a_clean_signal(self, engine):
        result = engine.evaluate(make_signal(), make_state(), now=NOW)
        assert result.decision is RiskDecision.APPROVED
        assert result.reasons == []
        assert result.approved_quantity > 0

    def test_size_is_the_smaller_of_absolute_and_percentage_caps(self, engine):
        """Os dois tetos existem porque protegem de coisas diferentes."""
        # 10% de 1000 = 100, igual ao teto absoluto -> 100
        assert engine.evaluate(make_signal(), make_state(), NOW).approved_notional == Decimal("100")

        # 10% de 10.000 = 1000, mas o teto absoluto (100) prevalece
        big = make_state(total="10000", cash="10000")
        assert engine.evaluate(make_signal(), big, NOW).approved_notional == Decimal("100")

        # 10% de 500 = 50, menor que o teto absoluto -> 50
        small = make_state(total="500", cash="500")
        assert engine.evaluate(make_signal(), small, NOW).approved_notional == Decimal("50")

    def test_never_spends_more_cash_than_available(self, engine):
        state = make_state(total="1000", cash="30")
        result = engine.evaluate(make_signal(), state, NOW)
        assert result.decision is RiskDecision.APPROVED
        assert result.approved_notional <= Decimal("30")

    def test_quantity_matches_notional_at_reference_price(self, engine):
        result = engine.evaluate(make_signal(price="50000"), make_state(), NOW)
        assert result.approved_quantity * Decimal("50000") == result.approved_notional

    def test_attaches_stop_loss_and_take_profit(self, engine):
        """A protecao vem do Risk Manager, nunca da estrategia."""
        result = engine.evaluate(make_signal(price="50000"), make_state(), NOW)
        assert result.stop_loss == Decimal("50000") * Decimal("0.97")
        assert result.take_profit == Decimal("50000") * Decimal("1.06")

    def test_stop_is_below_and_target_above_the_entry(self, engine):
        result = engine.evaluate(make_signal(price="50000"), make_state(), NOW)
        assert result.stop_loss < Decimal("50000") < result.take_profit


class TestWhitelists:
    def test_rejects_symbol_outside_whitelist(self, engine):
        result = engine.evaluate(make_signal(symbol="DOGE/USDT"), make_state(), NOW)
        assert result.decision is RiskDecision.REJECTED
        assert any("whitelist" in reason for reason in result.reasons)

    def test_rejects_quote_currency_mismatch(self, engine):
        """Par cotado em outra moeda quebraria todo o calculo de portfolio."""
        result = engine.evaluate(make_signal(symbol="BTC/EUR"), make_state(), NOW)
        assert result.decision is RiskDecision.REJECTED
        assert any("cotacao" in reason for reason in result.reasons)


class TestConfidence:
    def test_rejects_below_minimum(self, engine):
        result = engine.evaluate(make_signal(confidence=0.30), make_state(), NOW)
        assert result.decision is RiskDecision.REJECTED
        assert any("confianca" in reason for reason in result.reasons)

    def test_accepts_exactly_at_the_threshold(self, engine):
        """A fronteira e inclusiva: 0.55 passa, 0.5499 nao."""
        assert (
            engine.evaluate(make_signal(confidence=0.55), make_state(), NOW).decision
            is RiskDecision.APPROVED
        )
        assert (
            engine.evaluate(make_signal(confidence=0.5499), make_state(), NOW).decision
            is RiskDecision.REJECTED
        )


class TestCooldown:
    def test_rejects_inside_the_window(self, engine):
        state = make_state(last_order_at={"BTC/USDT": NOW - timedelta(seconds=300)})
        result = engine.evaluate(make_signal(), state, NOW)
        assert result.decision is RiskDecision.REJECTED
        assert any("cooldown" in reason for reason in result.reasons)

    def test_approves_after_the_window(self, engine):
        state = make_state(last_order_at={"BTC/USDT": NOW - timedelta(seconds=901)})
        assert engine.evaluate(make_signal(), state, NOW).decision is RiskDecision.APPROVED

    def test_is_per_symbol(self, engine):
        """Cooldown do ETH nao pode bloquear uma ordem de BTC."""
        state = make_state(last_order_at={"ETH/USDT": NOW - timedelta(seconds=10)})
        assert engine.evaluate(make_signal(), state, NOW).decision is RiskDecision.APPROVED


class TestExposure:
    def test_rejects_when_asset_already_at_the_cap(self, engine):
        # 0.011 BTC * 50.000 = 550 = 55% de 1000, acima do limite de 50%
        state = make_state(total="1000", cash="400", positions={"BTC": "0.011"})
        result = engine.evaluate(make_signal(), state, NOW)
        assert result.decision is RiskDecision.REJECTED
        assert any("exposicao" in reason for reason in result.reasons)

    def test_shrinks_the_order_to_fit_the_remaining_room(self, engine):
        """Perto do teto, reduzimos a ordem em vez de rejeitar.

        Rejeitar deixaria a carteira parada sem necessidade quando ainda ha
        espaco util -- menor que o pedido, mas acima do minimo negociavel.
        """
        # 0.0088 BTC * 50.000 = 440; teto de exposicao = 500 -> sobram 60
        state = make_state(total="1000", cash="500", positions={"BTC": "0.0088"})
        result = engine.evaluate(make_signal(), state, NOW)
        assert result.decision is RiskDecision.APPROVED
        assert result.approved_notional <= Decimal("60")

    def test_rejects_when_remaining_room_is_below_minimum_order(self, engine):
        # sobram apenas 5, abaixo do minimo de 10
        state = make_state(total="1000", cash="500", positions={"BTC": "0.0099"})
        result = engine.evaluate(make_signal(), state, NOW)
        assert result.decision is RiskDecision.REJECTED


class TestMinimumOrder:
    def test_rejects_when_cash_is_below_the_minimum(self, engine):
        """Ordem minuscula tem a taxa comendo o resultado; a exchange tambem recusa."""
        result = engine.evaluate(make_signal(), make_state(total="1000", cash="5"), NOW)
        assert result.decision is RiskDecision.REJECTED
        assert any("minimo" in reason for reason in result.reasons)


class TestOpenPositions:
    def test_rejects_when_the_limit_is_reached(self, engine):
        state = make_state(
            total="1000",
            cash="500",
            positions={"ETH": "0.1", "SOL": "2", "ADA": "100"},
            prices={"BTC": "50000", "ETH": "3000", "SOL": "150", "ADA": "1"},
        )
        result = engine.evaluate(make_signal(), state, NOW)
        assert result.decision is RiskDecision.REJECTED
        assert any("posicoes abertas" in reason for reason in result.reasons)

    def test_increasing_an_existing_position_is_not_a_new_one(self, engine):
        """O limite conta posicoes distintas, nao ordens."""
        state = make_state(
            total="10000",
            cash="5000",
            positions={"BTC": "0.01", "ETH": "0.1", "SOL": "2"},
            prices={"BTC": "50000", "ETH": "3000", "SOL": "150"},
        )
        assert engine.evaluate(make_signal(), state, NOW).decision is RiskDecision.APPROVED

    def test_dust_does_not_count_as_an_open_position(self, engine):
        """Restos irrisorios nao podem consumir as vagas de posicao."""
        state = make_state(
            total="1000",
            cash="500",
            positions={"ETH": "0.0001", "SOL": "0.001", "ADA": "1"},
            prices={"BTC": "50000", "ETH": "3000", "SOL": "150", "ADA": "1"},
        )
        assert engine.evaluate(make_signal(), state, NOW).decision is RiskDecision.APPROVED


class TestCircuitBreaker:
    def test_blocks_new_exposure(self, engine):
        result = engine.evaluate(make_signal(), make_state(circuit_breaker=True), NOW)
        assert result.decision is RiskDecision.REJECTED
        assert any("circuit breaker" in reason for reason in result.reasons)

    def test_does_not_block_closing_a_position(self, engine):
        """Assimetria deliberada e critica.

        O circuit breaker dispara exatamente durante uma queda. Se ele tambem
        impedisse a saida, a protecao viraria armadilha: o sistema ficaria preso
        na posicao justamente enquanto ela perde valor.
        """
        state = make_state(positions={"BTC": "0.01"}, circuit_breaker=True)
        result = engine.evaluate(make_signal(direction=SignalDirection.FLAT), state, NOW)
        assert result.decision is RiskDecision.APPROVED


class TestClosing:
    def test_closes_the_entire_position(self, engine):
        state = make_state(positions={"BTC": "0.01234"})
        result = engine.evaluate(make_signal(direction=SignalDirection.FLAT), state, NOW)
        assert result.decision is RiskDecision.APPROVED
        assert result.approved_quantity == Decimal("0.01234")

    def test_rejects_when_there_is_nothing_to_close(self, engine):
        result = engine.evaluate(
            make_signal(direction=SignalDirection.FLAT), make_state(), NOW
        )
        assert result.decision is RiskDecision.REJECTED
        assert any("nao ha posicao" in reason for reason in result.reasons)

    def test_ignores_cooldown(self, engine):
        """Bloquear uma saida por cooldown seria perigoso, nao prudente."""
        state = make_state(
            positions={"BTC": "0.01"}, last_order_at={"BTC/USDT": NOW - timedelta(seconds=1)}
        )
        result = engine.evaluate(make_signal(direction=SignalDirection.FLAT), state, NOW)
        assert result.decision is RiskDecision.APPROVED

    def test_ignores_minimum_notional(self, engine):
        """Uma posicao pequena precisa poder ser fechada, ou vira poeira presa."""
        state = make_state(positions={"BTC": "0.00002"})  # ~1 USDT, abaixo do minimo de 10
        result = engine.evaluate(make_signal(direction=SignalDirection.FLAT), state, NOW)
        assert result.decision is RiskDecision.APPROVED

    def test_does_not_attach_stop_or_target(self, engine):
        state = make_state(positions={"BTC": "0.01"})
        result = engine.evaluate(make_signal(direction=SignalDirection.FLAT), state, NOW)
        assert result.stop_loss is None
        assert result.take_profit is None


class TestUnsupportedDirections:
    def test_rejects_short(self, engine):
        """Spot nao permite venda a descoberto; falhar alto evita ordem sem sentido."""
        result = engine.evaluate(
            make_signal(direction=SignalDirection.SHORT), make_state(), NOW
        )
        assert result.decision is RiskDecision.REJECTED
        assert any("SHORT" in reason for reason in result.reasons)


class TestDegenerateInputs:
    def test_rejects_non_positive_price(self, engine):
        result = engine.evaluate(make_signal(price="0"), make_state(), NOW)
        assert result.decision is RiskDecision.REJECTED

    def test_rejects_when_portfolio_has_no_value(self, engine):
        result = engine.evaluate(make_signal(), make_state(total="0", cash="0"), NOW)
        assert result.decision is RiskDecision.REJECTED

    def test_collects_every_violated_rule_not_just_the_first(self, engine):
        """Ver todos os motivos de uma vez acelera o diagnostico."""
        state = make_state(
            circuit_breaker=True, last_order_at={"DOGE/USDT": NOW - timedelta(seconds=1)}
        )
        result = engine.evaluate(
            make_signal(symbol="DOGE/USDT", confidence=0.10), state, NOW
        )
        assert len(result.reasons) >= 3


class TestAuditTrail:
    def test_records_the_state_used_in_the_decision(self, engine):
        """Sem o retrato do momento, "por que operou?" fica sem resposta."""
        result = engine.evaluate(make_signal(), make_state(), NOW)
        assert result.snapshot["symbol"] == "BTC/USDT"
        assert result.snapshot["total_value"] == "1000"
        assert result.snapshot["limits"]["max_order_notional"] == "100"

    def test_rejections_are_assessments_too(self, engine):
        """Rejeicao tambem gera registro completo -- e o que explica a inacao."""
        result = engine.evaluate(make_signal(confidence=0.1), make_state(), NOW)
        assert result.signal_id is not None
        assert result.snapshot != {}
        assert result.reasons
