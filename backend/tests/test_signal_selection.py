"""Testes da seleção de sinais por confiança.

Sem isto, o Risk Manager atende quem chegou primeiro. Com muitos pares
monitorados isso é o pior dos mundos: o sistema tem escolhas e gasta as vagas
com sinal mediano por ordem de chegada — foi o que os dados mostraram, 369
sinais rejeitados por "posições abertas" num teste de 16 pares, descartados sem
nenhuma comparação de qualidade.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from helpers import com_negocio

from crypto_traders.domain.enums import ExchangeName, RiskDecision, SignalDirection
from crypto_traders.domain.models import Signal
from crypto_traders.risk.rules import PortfolioState, RiskEngine

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def sinal(
    symbol: str,
    confidence: float,
    *,
    direction: SignalDirection = SignalDirection.LONG,
    price: str = "100",
) -> Signal:
    return Signal(
        exchange=ExchangeName.BINANCE,
        symbol=symbol,
        timeframe="4h",
        strategy="teste",
        direction=direction,
        confidence=confidence,
        reason="teste",
        reference_price=Decimal(price),
    )


def estado(
    *, total: str = "1000", cash: str = "1000", positions: dict | None = None
) -> PortfolioState:
    pares = ["A", "B", "C", "D", "E", "F"]
    return PortfolioState(
        total_value=Decimal(total),
        cash=Decimal(cash),
        positions={k: Decimal(v) for k, v in (positions or {}).items()},
        prices={asset: Decimal("100") for asset in pares},
    )


@pytest.fixture
def engine() -> RiskEngine:
    from crypto_traders.config import RiskSettings

    limites = RiskSettings(
        max_order_notional=Decimal("100"),
        max_order_pct_portfolio=0.10,
        max_asset_exposure_pct=0.20,
        max_open_positions=2,
        min_order_notional=Decimal("10"),
        stop_loss_pct=0.03,
        take_profit_pct=0.06,
        min_signal_confidence=0.55,
        cooldown_seconds=900,
        symbol_whitelist=[f"{a}/USDT" for a in "ABCDEF"],
        asset_whitelist=list("ABCDEF"),
    )
    return RiskEngine(limites, "USDT")


def aprovados(decisoes) -> list[str]:
    return [s.symbol for s, a in decisoes if a.decision is RiskDecision.APPROVED]


class TestConfidenceRanking:
    def test_highest_confidence_wins_the_slots(self, engine):
        """Duas vagas, quatro candidatos: as duas melhores confianças passam."""
        batch = [
            sinal("A/USDT", 0.60),
            sinal("B/USDT", 0.95),
            sinal("C/USDT", 0.70),
            sinal("D/USDT", 0.88),
        ]
        assert aprovados(engine.evaluate_batch(batch, estado(), NOW)) == [
            "B/USDT",
            "D/USDT",
        ]

    def test_arrival_order_does_not_matter(self, engine):
        """O mesmo lote em qualquer ordem precisa dar o mesmo resultado.

        É exatamente isto que faltava: antes, a ordem de chegada decidia.
        """
        batch = [sinal("A/USDT", 0.60), sinal("B/USDT", 0.95), sinal("C/USDT", 0.70)]
        primeiro = aprovados(engine.evaluate_batch(batch, estado(), NOW))
        invertido = aprovados(engine.evaluate_batch(list(reversed(batch)), estado(), NOW))
        assert primeiro == invertido

    def test_ties_break_by_symbol_for_reproducibility(self, engine):
        """Empate resolvido de forma estável: backtest tem de ser reprodutível."""
        batch = [sinal("C/USDT", 0.80), sinal("A/USDT", 0.80), sinal("B/USDT", 0.80)]
        assert aprovados(engine.evaluate_batch(batch, estado(), NOW)) == [
            "A/USDT",
            "B/USDT",
        ]

    def test_losers_are_rejected_by_the_slot_limit(self, engine):
        batch = [sinal("A/USDT", 0.95), sinal("B/USDT", 0.90), sinal("C/USDT", 0.60)]
        decisoes = engine.evaluate_batch(batch, estado(), NOW)
        perdedor = next(a for s, a in decisoes if s.symbol == "C/USDT")
        assert perdedor.decision is RiskDecision.REJECTED
        assert any("posicoes abertas" in r for r in perdedor.reasons)


class TestClosesGoFirst:
    def test_close_is_evaluated_before_any_open(self, engine):
        """Fechar libera caixa e vaga; travar a saída é a armadilha a evitar.

        Com as duas vagas ocupadas, o fechamento tem de passar mesmo com
        confiança menor que as aberturas concorrentes.
        """
        batch = [
            sinal("A/USDT", 0.99),
            sinal("B/USDT", 0.50, direction=SignalDirection.FLAT),
        ]
        cheio = estado(cash="100", positions={"A": "0.5", "B": "0.5"})
        decisoes = engine.evaluate_batch(batch, cheio, NOW)
        fechamento = next(a for s, a in decisoes if s.symbol == "B/USDT")
        assert fechamento.decision is RiskDecision.APPROVED

    def test_freed_cash_becomes_available_to_the_next_signal(self, engine):
        """Depois de fechar, o caixa liberado precisa contar para a abertura."""
        batch = [
            sinal("A/USDT", 0.60, direction=SignalDirection.FLAT),
            sinal("B/USDT", 0.90),
        ]
        sem_caixa = estado(total="1000", cash="0", positions={"A": "5"})
        decisoes = engine.evaluate_batch(batch, sem_caixa, NOW)
        assert aprovados(decisoes) == ["A/USDT", "B/USDT"]


class TestStateSimulation:
    def test_cash_is_consumed_between_signals_of_the_same_batch(self, engine):
        """Sem simular a aprovação, dois sinais gastariam o mesmo dinheiro."""
        batch = [sinal("A/USDT", 0.95), sinal("B/USDT", 0.90)]
        apertado = estado(total="120", cash="120")
        decisoes = engine.evaluate_batch(batch, apertado, NOW)
        aprovadas = [a for _, a in decisoes if a.decision is RiskDecision.APPROVED]
        gasto = sum((a.approved_notional or Decimal(0)) for a in aprovadas)
        assert gasto <= Decimal("120")

    def test_caller_state_is_not_mutated(self, engine):
        """O retrato do portfólio pertence a quem chamou."""
        original = estado()
        antes = (original.cash, dict(original.positions), dict(original.last_order_at))
        engine.evaluate_batch([sinal("A/USDT", 0.95)], original, NOW)
        assert (original.cash, original.positions, original.last_order_at) == (
            antes[0],
            antes[1],
            antes[2],
        )

    def test_two_signals_on_the_same_pair_hit_the_cooldown(self, engine):
        """Duas ordens no mesmo par dentro de um lote seriam overtrading."""
        batch = [sinal("A/USDT", 0.95), sinal("A/USDT", 0.90)]
        assert aprovados(engine.evaluate_batch(batch, estado(), NOW)) == ["A/USDT"]


class TestDegenerateBatches:
    def test_empty_batch_returns_nothing(self, engine):
        assert engine.evaluate_batch([], estado(), NOW) == []

    def test_single_signal_behaves_like_evaluate(self, engine):
        um = sinal("A/USDT", 0.80)
        lote = engine.evaluate_batch([um], estado(), NOW)[0][1]
        direto = engine.evaluate(um, estado(), NOW)
        assert lote.decision is direto.decision
        assert lote.approved_notional == direto.approved_notional

    def test_every_signal_gets_an_assessment(self, engine):
        """Nenhum sinal pode desaparecer sem registro -- a auditoria depende disso."""
        batch = [sinal(f"{a}/USDT", 0.60 + i / 100) for i, a in enumerate("ABCDEF")]
        decisoes = engine.evaluate_batch(batch, estado(), NOW)
        assert len(decisoes) == len(batch)
        assert {s.id for s, _ in decisoes} == {s.id for s in batch}


class TestAgentBatching:
    async def test_collects_signals_within_the_window(self, settings):
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        configurado = com_negocio(settings, signal_batch_window_seconds=0.15)
        agent = RiskManagerAgent(InMemoryEventBus(), configurado)

        queue: asyncio.Queue[Signal] = asyncio.Queue()
        for i, letra in enumerate("ABC"):
            queue.put_nowait(sinal(f"{letra}/USDT", 0.6 + i / 100))

        batch = await agent._collect_batch(queue)
        assert len(batch) == 3

    async def test_window_zero_disables_batching(self, settings):
        """Escape hatch: volta ao comportamento de um sinal por vez."""
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        configurado = com_negocio(settings, signal_batch_window_seconds=0)
        agent = RiskManagerAgent(InMemoryEventBus(), configurado)

        queue: asyncio.Queue[Signal] = asyncio.Queue()
        queue.put_nowait(sinal("A/USDT", 0.8))
        queue.put_nowait(sinal("B/USDT", 0.9))

        assert len(await agent._collect_batch(queue)) == 1

    async def test_batch_persists_every_assessment(self, settings):
        """Aprovado ou rejeitado, todo sinal do lote vai para `risk_events`."""
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus
        from crypto_traders.db.repositories import RiskEventRepository
        from crypto_traders.db.session import session_scope
        from crypto_traders.domain.models import PortfolioSnapshot, Position

        bus = InMemoryEventBus()
        await bus.start()
        agent = RiskManagerAgent(bus, settings)
        agent.observe_snapshot(
            PortfolioSnapshot(
                total_value=Decimal("1000"),
                cash_value=Decimal("1000"),
                positions_value=Decimal(0),
                positions=[
                    Position(
                        exchange=ExchangeName.PAPER,
                        asset="USDT",
                        quantity=Decimal("1000"),
                        current_price=Decimal(1),
                    )
                ],
            )
        )

        batch = [
            sinal("BTC/USDT", 0.95, price="50000"),
            sinal("ETH/USDT", 0.70, price="2500"),
            sinal("SOL/USDT", 0.60, price="100"),
        ]
        await agent._on_batch(batch)

        async with session_scope(settings) as session:
            eventos = await RiskEventRepository(session).list()
        assert len(eventos) == 3
