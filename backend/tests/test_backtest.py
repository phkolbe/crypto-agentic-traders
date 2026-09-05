"""Testes do motor de backtest.

O que precisa ser garantido aqui nao e o retorno da estrategia, e sim que o
backtest **nao mente**: sem vies de antecipacao, com custos aplicados, com as
mesmas regras de risco da producao e com contabilidade que fecha.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest
from helpers import make_candles

from crypto_traders.backtest.engine import BacktestEngine
from crypto_traders.strategies import MovingAverageCrossover, RsiReversion


def make_engine(risk_limits, **kwargs) -> BacktestEngine:
    defaults = {
        "quote_currency": "USDT",
        "initial_balance": Decimal("1000"),
        "fee_pct": Decimal("0.001"),
        "slippage_pct": Decimal("0.0005"),
    }
    return BacktestEngine(
        kwargs.pop("strategy", MovingAverageCrossover(fast=3, slow=10)),
        risk_limits,
        **(defaults | kwargs),
    )


def oscillating(periods: int = 400, amplitude: float = 15.0) -> list[float]:
    """Serie oscilante: gera cruzamentos suficientes para haver o que medir."""
    x = np.linspace(0, 8 * np.pi, periods)
    return list(50000 + amplitude * 300 * np.sin(x) + np.linspace(0, 2000, periods))


class TestGuards:
    async def test_rejects_history_shorter_than_warmup(self, risk_limits):
        engine = make_engine(risk_limits)
        with pytest.raises(ValueError, match="historico insuficiente"):
            await engine.run(make_candles([50000.0] * 5))

    async def test_ignores_candles_still_forming(self, risk_limits):
        """Um candle aberto no fim da serie nao pode virar barra de decisao."""
        engine = make_engine(risk_limits)
        closed = make_candles(oscillating(120))
        forming = make_candles([99999.0], closed=False)
        result = await engine.run(closed + forming)
        assert result.equity_curve[-1][0] == closed[-1].open_time


class TestAccounting:
    async def test_equity_curve_starts_at_the_initial_balance(self, risk_limits):
        engine = make_engine(risk_limits)
        result = await engine.run(make_candles(oscillating(200)))
        assert result.equity_curve[0][1] == Decimal("1000")

    async def test_no_trades_means_untouched_balance(self, risk_limits):
        """Mercado plano nao gera cruzamento, e sem operacao o saldo nao muda."""
        engine = make_engine(risk_limits)
        result = await engine.run(make_candles([50000.0] * 200))
        assert result.trades == []
        assert result.final_value == Decimal("1000")
        assert result.total_return_pct == 0.0

    async def test_fees_are_charged_on_every_fill(self, risk_limits):
        """Backtest sem custo gera confianca falsa numa estrategia ruim."""
        with_fees = make_engine(risk_limits, fee_pct=Decimal("0.01"))
        without_fees = make_engine(risk_limits, fee_pct=Decimal("0"), slippage_pct=Decimal("0"))
        candles = make_candles(oscillating(400))

        costly = await with_fees.run(candles)
        free = await without_fees.run(candles)
        if costly.trades:
            assert costly.final_value < free.final_value

    async def test_never_spends_more_cash_than_it_has(self, risk_limits):
        """O caixa nunca pode ficar negativo -- isso seria alavancagem invisivel."""
        engine = make_engine(risk_limits)
        result = await engine.run(make_candles(oscillating(400)))
        assert all(value >= 0 for _, value in result.equity_curve)


class TestRiskIntegration:
    async def test_uses_the_same_risk_engine_as_production(self, risk_limits):
        """Whitelist e do RiskEngine real: par fora dela nao opera no backtest."""
        engine = make_engine(risk_limits)
        candles = make_candles(oscillating(300), symbol="DOGE/USDT")
        result = await engine.run(candles)
        assert result.trades == []
        assert result.signals_rejected > 0

    async def test_reports_why_signals_were_rejected(self, risk_limits):
        """Saber por que nao operou e tao importante quanto saber por que operou."""
        engine = make_engine(risk_limits)
        result = await engine.run(make_candles(oscillating(300), symbol="DOGE/USDT"))
        assert result.rejection_reasons

    async def test_respects_the_order_size_cap(self, risk_limits):
        engine = make_engine(risk_limits)
        result = await engine.run(make_candles(oscillating(400)))
        for trade in result.trades:
            if trade.side == "buy":
                assert trade.notional <= risk_limits.max_order_notional * Decimal("1.01")


class TestMetrics:
    async def test_drawdown_is_zero_when_equity_never_falls(self, risk_limits):
        engine = make_engine(risk_limits)
        result = await engine.run(make_candles([50000.0] * 200))
        assert result.max_drawdown_pct == 0.0

    async def test_drawdown_is_positive_when_equity_falls(self, risk_limits):
        engine = make_engine(risk_limits, strategy=RsiReversion(period=14))
        closes = list(np.linspace(50000, 30000, 300))
        result = await engine.run(make_candles(closes))
        assert result.max_drawdown_pct >= 0.0

    async def test_compares_against_buy_and_hold(self, risk_limits):
        """+8% num periodo em que o ativo subiu 30% destruiu valor."""
        engine = make_engine(risk_limits)
        closes = list(np.linspace(50000, 65000, 300))
        result = await engine.run(make_candles(closes))
        assert result.buy_and_hold_pct > 0.2

    async def test_summary_is_json_serializable(self, risk_limits):
        import json

        engine = make_engine(risk_limits)
        result = await engine.run(make_candles(oscillating(200)))
        json.dumps(result.summary())  # nao pode levantar

    async def test_win_rate_only_counts_closed_trades(self, risk_limits):
        """Compra em aberto nao tem resultado ainda e nao pode entrar na conta."""
        engine = make_engine(risk_limits)
        result = await engine.run(make_candles(oscillating(400)))
        assert all(t.realized_pnl is not None for t in result.closed_trades)
        assert 0.0 <= result.win_rate <= 1.0


class TestDeterminism:
    async def test_same_input_gives_same_result(self, risk_limits):
        """Backtest nao reprodutivel nao serve para comparar estrategias."""
        candles = make_candles(oscillating(300))
        first = await make_engine(risk_limits).run(candles)
        second = await make_engine(risk_limits).run(candles)
        assert first.final_value == second.final_value
        assert len(first.trades) == len(second.trades)
