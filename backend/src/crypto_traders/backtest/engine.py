"""Backtest orientado a eventos.

Reproduz a cadeia de producao **usando os mesmos objetos**: a mesma classe de
estrategia, o mesmo `RiskEngine` e o mesmo `PaperBroker` que rodam em `dry_run`.
Nao ha reimplementacao vetorizada da logica.

Esse foi o motivo de nao usar `vectorbt`/`backtrader`: uma engine externa exige
reescrever a estrategia e as regras de risco no vocabulario dela, e ai o
backtest valida a reescrita, nao o sistema. Aqui, um bug no Risk Manager aparece
no backtest porque e literalmente o mesmo codigo.

Cuidados contra viés de antecipação (*look-ahead bias*):
- Cada barra so enxerga candles ate ela propria (`candles[: i + 1]`).
- Apenas candles **fechados** entram.
- A ordem e preenchida ao preco de fechamento da barra que gerou o sinal, com
  taxa e slippage aplicados pelo PaperBroker -- nunca no melhor preco da barra.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..config import RiskSettings
from ..domain.enums import (
    ExchangeName,
    OrderStatus,
    OrderType,
    RiskDecision,
    Side,
    SignalDirection,
)
from ..domain.models import Candle, OrderRequest
from ..exchanges.paper import PaperBroker
from ..logging_setup import get_logger
from ..risk.rules import PortfolioState, RiskEngine
from ..strategies import MarketFrame, Strategy

log = get_logger(__name__)


@dataclass
class BacktestTrade:
    timestamp: datetime
    side: str
    quantity: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    reason: str
    realized_pnl: Decimal | None = None


@dataclass
class BacktestResult:
    strategy: str
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    initial_balance: Decimal
    final_value: Decimal
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    signals_generated: int = 0
    signals_rejected: int = 0
    rejection_reasons: Counter = field(default_factory=Counter)

    #: Preco no inicio e no fim da janela, base da comparacao buy-and-hold.
    first_price: Decimal | None = None
    last_price: Decimal | None = None

    @property
    def total_return_pct(self) -> float:
        if self.initial_balance <= 0:
            return 0.0
        return float((self.final_value - self.initial_balance) / self.initial_balance)

    @property
    def buy_and_hold_pct(self) -> float:
        """Referencia honesta: bater o mercado e o unico resultado que interessa.

        Uma estrategia com +8% num periodo em que o ativo subiu 30% destruiu
        valor, e so a comparacao mostra isso.
        """
        if len(self.equity_curve) < 2 or not self.first_price or not self.last_price:
            return 0.0
        return float((self.last_price - self.first_price) / self.first_price)

    @property
    def max_drawdown_pct(self) -> float:
        """Maior queda do pico ate o vale -- a dor real de carregar a estrategia."""
        peak = Decimal(0)
        worst = 0.0
        for _, value in self.equity_curve:
            peak = max(peak, value)
            if peak > 0:
                drawdown = float((peak - value) / peak)
                worst = max(worst, drawdown)
        return worst

    @property
    def closed_trades(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.realized_pnl is not None]

    @property
    def win_rate(self) -> float:
        closed = self.closed_trades
        if not closed:
            return 0.0
        wins = sum(1 for t in closed if (t.realized_pnl or Decimal(0)) > 0)
        return wins / len(closed)

    @property
    def profit_factor(self) -> float:
        """Soma dos ganhos / soma das perdas. Abaixo de 1 a estrategia perde dinheiro."""
        results = [float(t.realized_pnl or 0) for t in self.closed_trades]
        gains = sum(value for value in results if value > 0)
        losses = -sum(value for value in results if value < 0)
        if losses == 0:
            return math.inf if gains > 0 else 0.0
        return gains / losses

    def summary(self) -> dict:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "initial_balance": float(self.initial_balance),
            "final_value": float(self.final_value),
            "total_return_pct": self.total_return_pct,
            "buy_and_hold_pct": self.buy_and_hold_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "trades": len(self.trades),
            "closed_trades": len(self.closed_trades),
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "signals_generated": self.signals_generated,
            "signals_rejected": self.signals_rejected,
            "top_rejection_reasons": self.rejection_reasons.most_common(5),
        }


class BacktestEngine:
    def __init__(
        self,
        strategy: Strategy,
        risk_limits: RiskSettings,
        *,
        quote_currency: str = "USDT",
        initial_balance: Decimal = Decimal("1000"),
        fee_pct: Decimal = Decimal("0.001"),
        slippage_pct: Decimal = Decimal("0.0005"),
        lookback: int = 500,
    ) -> None:
        self.strategy = strategy
        self.risk = RiskEngine(risk_limits, quote_currency)
        self.quote_currency = quote_currency
        self.initial_balance = initial_balance
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        # Mesma janela que o agente usa em producao: se o backtest enxergasse
        # historico ilimitado, os indicadores teriam sementes diferentes das
        # reais e o resultado nao seria reprodutivel ao vivo.
        self.lookback = lookback

    async def run(self, candles: list[Candle]) -> BacktestResult:
        closed = [c for c in candles if c.closed]
        if len(closed) < self.strategy.min_candles + 2:
            raise ValueError(
                f"historico insuficiente: {len(closed)} candles fechados, "
                f"a estrategia '{self.strategy.name}' precisa de {self.strategy.min_candles + 2}"
            )

        reference = closed[-1]
        base = reference.symbol.partition("/")[0]
        broker = PaperBroker(
            quote_currency=self.quote_currency,
            initial_balance=self.initial_balance,
            fee_pct=self.fee_pct,
            slippage_pct=self.slippage_pct,
        )

        result = BacktestResult(
            strategy=self.strategy.name,
            symbol=reference.symbol,
            timeframe=reference.timeframe,
            start=closed[0].open_time,
            end=reference.open_time,
            initial_balance=self.initial_balance,
            final_value=self.initial_balance,
        )
        result.first_price = closed[self.strategy.min_candles].close
        result.last_price = reference.close

        # Custo medio de aquisicao, para calcular PnL realizado no fechamento.
        cost_basis = Decimal(0)
        held = Decimal(0)
        last_order_at: dict[str, datetime] = {}

        for index in range(self.strategy.min_candles, len(closed)):
            window = closed[max(0, index + 1 - self.lookback) : index + 1]
            current = window[-1]
            price = current.close
            broker.set_price(base, price)

            market = MarketFrame.from_candles(window)
            signal = self.strategy.evaluate(market)

            if signal is not None:
                result.signals_generated += 1
                balances = await broker.fetch_balances()
                cash = balances.get(self.quote_currency, Decimal(0))
                held = balances.get(base, Decimal(0))

                state = PortfolioState(
                    total_value=cash + held * price,
                    cash=cash,
                    positions={base: held},
                    prices={base: price},
                    last_order_at=dict(last_order_at),
                )
                assessment = self.risk.evaluate(signal, state, now=current.open_time)

                if assessment.decision is RiskDecision.REJECTED:
                    result.signals_rejected += 1
                    for reason in assessment.reasons:
                        # Normaliza o texto para agrupar: motivos com numeros
                        # variaveis (segundos restantes) viram uma categoria so.
                        result.rejection_reasons[reason.split(":")[0]] += 1
                elif assessment.approved_quantity:
                    side = Side.BUY if signal.direction is SignalDirection.LONG else Side.SELL
                    request = OrderRequest(
                        client_order_id=f"bt-{index}-{signal.id[:8]}",
                        signal_id=signal.id,
                        risk_event_id=assessment.id,
                        exchange=ExchangeName.PAPER,
                        symbol=signal.symbol,
                        side=side,
                        order_type=OrderType.MARKET,
                        quantity=assessment.approved_quantity,
                        notional=assessment.approved_notional or Decimal(1),
                        stop_loss=assessment.stop_loss,
                        take_profit=assessment.take_profit,
                        strategy=self.strategy.name,
                    )
                    order_result = await broker.place_order(request)

                    if order_result.status is OrderStatus.FILLED:
                        fill_price = order_result.average_price or price
                        quantity = order_result.filled_quantity
                        last_order_at[signal.symbol] = current.open_time

                        realized: Decimal | None = None
                        if side is Side.BUY:
                            cost_basis += quantity * fill_price + order_result.fee
                            held += quantity
                        else:
                            average_cost = (cost_basis / held) if held > 0 else Decimal(0)
                            realized = (
                                quantity * fill_price - order_result.fee - quantity * average_cost
                            )
                            cost_basis -= quantity * average_cost
                            held -= quantity

                        result.trades.append(
                            BacktestTrade(
                                timestamp=current.open_time,
                                side=str(side),
                                quantity=quantity,
                                price=fill_price,
                                notional=quantity * fill_price,
                                fee=order_result.fee,
                                reason=signal.reason,
                                realized_pnl=realized,
                            )
                        )

            balances = await broker.fetch_balances()
            equity = balances.get(self.quote_currency, Decimal(0)) + balances.get(
                base, Decimal(0)
            ) * price
            result.equity_curve.append((current.open_time, equity))

        result.final_value = (
            result.equity_curve[-1][1] if result.equity_curve else self.initial_balance
        )
        log.info(
            "backtest.finished",
            strategy=self.strategy.name,
            symbol=result.symbol,
            trades=len(result.trades),
            return_pct=round(result.total_return_pct, 4),
        )
        return result
