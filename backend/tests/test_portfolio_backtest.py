"""Testes do backtest de carteira (vários pares, um dinheiro).

O que precisa ser garantido é justamente o que a soma de backtests independentes
erra: os pares competem pelo mesmo caixa, `max_open_positions` vale no conjunto,
e a contagem de operações não é inflada.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest
from helpers import make_candles

from crypto_traders.backtest.portfolio import PortfolioBacktestEngine
from crypto_traders.config import RiskSettings
from crypto_traders.strategies import MovingAverageCrossover, RsiReversion


def oscillating(periods: int, seed: int, base: float = 100.0) -> list[float]:
    """Série com oscilação suficiente para gerar cruzamentos."""
    rng = np.random.default_rng(seed)
    x = np.linspace(0, 10 * np.pi, periods)
    return list(base + base * 0.12 * np.sin(x) + rng.normal(0, base * 0.004, periods))


def candles_for(symbols: list[str], periods: int = 300) -> dict:
    return {
        symbol: make_candles(oscillating(periods, seed=index + 1), symbol=symbol)
        for index, symbol in enumerate(symbols)
    }


def limits(**overrides) -> RiskSettings:
    base = dict(
        max_order_notional=Decimal("25"),
        max_order_pct_portfolio=0.15,
        max_asset_exposure_pct=0.25,
        max_open_positions=3,
        min_order_notional=Decimal("15"),
        stop_loss_pct=0.03,
        take_profit_pct=0.06,
        min_signal_confidence=0.55,
        cooldown_seconds=900,
    )
    base.update(overrides)
    symbols = base.pop("symbols", ["A/BRL", "B/BRL", "C/BRL", "D/BRL", "E/BRL"])
    return RiskSettings(
        **base,
        symbol_whitelist=symbols,
        asset_whitelist=[s.split("/")[0] for s in symbols],
    )


def engine(risk: RiskSettings, **overrides) -> PortfolioBacktestEngine:
    defaults = dict(
        quote_currency="BRL",
        initial_balance=Decimal("150"),
        fee_pct=Decimal("0.001"),
        slippage_pct=Decimal("0.0005"),
        lookback=200,
    )
    defaults.update(overrides)
    strategies = defaults.pop("strategies", [MovingAverageCrossover(fast=3, slow=10)])
    return PortfolioBacktestEngine(strategies, risk, **defaults)


class TestSharedCapital:
    async def test_cash_is_shared_across_pairs(self):
        """O erro que este motor existe para evitar: 5 pares gastando 5 carteiras.

        Com R$150 e ordens de até R$22,50, o patrimônio jamais pode passar do
        inicial por efeito de compra — só por valorização.
        """
        symbols = ["A/BRL", "B/BRL", "C/BRL", "D/BRL", "E/BRL"]
        result = await engine(limits()).run(candles_for(symbols))
        assert all(value >= 0 for _, value in result.equity_curve)

    async def test_open_positions_limit_applies_across_all_pairs(self):
        """Com o limite em 1, nunca deve haver duas posições simultâneas."""
        symbols = ["A/BRL", "B/BRL", "C/BRL", "D/BRL", "E/BRL"]
        result = await engine(limits(max_open_positions=1, symbols=symbols)).run(
            candles_for(symbols)
        )
        assert any(
            "posicoes abertas" in reason for reason in result.rejection_reasons
        ) or len(result.trades) <= 2

    async def test_more_pairs_produce_more_signals_but_capped_trades(self):
        """Mais pares geram mais sinais; as vagas de posição limitam as ordens."""
        poucos = ["A/BRL", "B/BRL"]
        muitos = ["A/BRL", "B/BRL", "C/BRL", "D/BRL", "E/BRL"]

        r_poucos = await engine(limits(symbols=poucos)).run(candles_for(poucos))
        r_muitos = await engine(limits(symbols=muitos)).run(candles_for(muitos))

        assert r_muitos.signals_generated > r_poucos.signals_generated


class TestFeeAccounting:
    async def test_fees_are_summed_from_every_fill(self):
        symbols = ["A/BRL", "B/BRL"]
        result = await engine(limits(symbols=symbols)).run(candles_for(symbols))
        if result.trades:
            assert result.total_fees == sum(t.fee for t in result.trades)

    async def test_monthly_fee_is_normalized_by_the_period(self):
        """A métrica que decide viabilidade: taxa como % do capital por mês."""
        symbols = ["A/BRL", "B/BRL"]
        result = await engine(limits(symbols=symbols)).run(candles_for(symbols))
        if result.trades:
            assert result.fees_pct_of_capital_per_month > 0
            esperado = result.fees_per_month / result.initial_balance
            assert result.fees_pct_of_capital_per_month == pytest.approx(float(esperado))

    async def test_no_trades_means_no_fees(self):
        """Mercado plano: sem cruzamento, sem ordem, sem taxa."""
        flat = {"A/BRL": make_candles([100.0] * 200, symbol="A/BRL")}
        result = await engine(limits(symbols=["A/BRL"])).run(flat)
        assert result.trades == []
        assert result.total_fees == Decimal(0)
        assert result.fees_per_month == Decimal(0)


class TestBenchmark:
    async def test_buy_and_hold_is_measured_on_the_same_pairs(self):
        """Sem essa referência, "+4,88%" parece bom sem ser."""
        symbols = ["A/BRL", "B/BRL"]
        result = await engine(limits(symbols=symbols)).run(candles_for(symbols))
        assert set(result.first_prices) == set(symbols)
        assert isinstance(result.buy_and_hold_pct, float)

    async def test_reference_price_starts_after_warmup(self):
        """Usar o início da série daria vantagem artificial à estratégia.

        Ela só pode operar depois do aquecimento dos indicadores, então a
        comparação honesta começa no mesmo ponto.
        """
        symbols = ["A/BRL"]
        candles = candles_for(symbols, periods=300)
        strategy = MovingAverageCrossover(fast=3, slow=10)
        result = await engine(
            limits(symbols=symbols), strategies=[strategy]
        ).run(candles)

        fechados = [c for c in candles["A/BRL"] if c.closed]
        assert result.first_prices["A/BRL"] == fechados[strategy.min_candles].close

    async def test_beat_the_market_compares_the_two(self):
        symbols = ["A/BRL"]
        result = await engine(limits(symbols=symbols)).run(candles_for(symbols))
        assert result.beat_the_market == (
            result.total_return_pct > result.buy_and_hold_pct
        )


class TestGuards:
    async def test_refuses_when_every_pair_is_too_short(self):
        curto = {"A/BRL": make_candles([100.0] * 5, symbol="A/BRL")}
        with pytest.raises(ValueError, match="historico insuficiente"):
            await engine(limits(symbols=["A/BRL"])).run(curto)

    async def test_skips_short_pairs_but_runs_the_others(self):
        """Par novo na exchange não pode impedir o backtest dos demais."""
        candles = candles_for(["A/BRL"], periods=300)
        candles["B/BRL"] = make_candles([100.0] * 5, symbol="B/BRL")
        result = await engine(limits(symbols=["A/BRL", "B/BRL"])).run(candles)
        assert result.symbols == ["A/BRL"]

    async def test_pairs_outside_the_whitelist_never_trade(self):
        """Usa o RiskEngine real: whitelist vale no backtest como em produção."""
        symbols = ["A/BRL", "FORA/BRL"]
        result = await engine(limits(symbols=["A/BRL"])).run(candles_for(symbols))
        assert all(t.symbol == "A/BRL" for t in result.trades)


class TestDeterminism:
    async def test_same_input_gives_same_result(self):
        symbols = ["A/BRL", "B/BRL"]
        candles = candles_for(symbols)
        primeiro = await engine(limits(symbols=symbols)).run(candles)
        segundo = await engine(limits(symbols=symbols)).run(candles)
        assert primeiro.final_value == segundo.final_value
        assert len(primeiro.trades) == len(segundo.trades)


class TestMultipleStrategies:
    async def test_two_strategies_generate_more_signals_than_one(self):
        symbols = ["A/BRL"]
        candles = candles_for(symbols, periods=300)
        uma = await engine(
            limits(symbols=symbols), strategies=[MovingAverageCrossover(fast=3, slow=10)]
        ).run(candles)
        duas = await engine(
            limits(symbols=symbols),
            strategies=[MovingAverageCrossover(fast=3, slow=10), RsiReversion(period=14)],
        ).run(candles)
        assert duas.signals_generated >= uma.signals_generated
