"""Backtest de carteira: todos os pares competindo pelo mesmo dinheiro.

O `BacktestEngine` de um par so responde "essa estrategia funciona nesse par".
Ele nao responde a pergunta que aparece quando se liberam 16 pares: **quantas
operacoes isso gera de fato, e quanto a taxa come?**

Somar 16 execucoes independentes daria a resposta errada, e para cima:

- cada execucao usaria a carteira inteira, entao 16 delas gastariam 16x o
  dinheiro que existe;
- `max_open_positions` nao seria respeitado, porque cada par ignoraria os outros;
- a exposicao por ativo tambem nao, pela mesma razao.

Aqui existe **um** `PaperBroker` e **um** `RiskEngine` para todos os pares. Os
candles sao percorridos em ordem cronologica global, e cada sinal disputa o mesmo
caixa com os demais -- que e exatamente o que acontece em producao.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import pandas as pd

from ..config import RiskSettings
from ..domain.enums import ExchangeName, OrderStatus, OrderType, RiskDecision, Side, SignalDirection
from ..domain.models import Candle, OrderRequest
from ..exchanges.paper import PaperBroker
from ..logging_setup import get_logger
from ..risk.rules import PortfolioState, RiskEngine
from ..strategies import MarketFrame, Strategy

log = get_logger(__name__)


@dataclass
class PortfolioTrade:
    timestamp: datetime
    symbol: str
    strategy: str
    side: str
    quantity: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    realized_pnl: Decimal | None = None


@dataclass
class PortfolioBacktestResult:
    quote_currency: str
    start: datetime
    end: datetime
    initial_balance: Decimal
    final_value: Decimal
    symbols: list[str]
    trades: list[PortfolioTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    signals_generated: int = 0
    signals_rejected: int = 0
    rejection_reasons: Counter = field(default_factory=Counter)
    trades_per_symbol: Counter = field(default_factory=Counter)

    #: Preco no inicio e no fim da janela, por par. Base do comprar-e-segurar.
    first_prices: dict[str, Decimal] = field(default_factory=dict)
    last_prices: dict[str, Decimal] = field(default_factory=dict)

    @property
    def buy_and_hold_pct(self) -> float:
        """Comprar e segurar em partes iguais nos mesmos pares, no mesmo periodo.

        E a referencia que decide se valeu operar. Sem ela, um "+4,88%" parece
        bom -- e pode ser metade do que o mercado entregou sozinho.
        """
        pares = [s for s in self.first_prices if s in self.last_prices]
        if not pares:
            return 0.0
        retornos = [
            (self.last_prices[s] - self.first_prices[s]) / self.first_prices[s]
            for s in pares
            if self.first_prices[s] > 0
        ]
        if not retornos:
            return 0.0
        return float(sum(retornos) / len(retornos))

    @property
    def beat_the_market(self) -> bool:
        return self.total_return_pct > self.buy_and_hold_pct

    @property
    def days(self) -> float:
        span = (self.end - self.start).total_seconds() / 86400
        return max(span, 1 / 24)

    @property
    def total_fees(self) -> Decimal:
        return sum((t.fee for t in self.trades), start=Decimal(0))

    @property
    def trades_per_month(self) -> float:
        return len(self.trades) / self.days * 30

    @property
    def fees_per_month(self) -> Decimal:
        """Taxa mensal projetada, na moeda de cotacao."""
        if not self.trades:
            return Decimal(0)
        return self.total_fees / Decimal(str(self.days)) * 30

    @property
    def fees_pct_of_capital_per_month(self) -> float:
        """A metrica que importa: quanto do patrimonio a taxa consome por mes."""
        if self.initial_balance <= 0:
            return 0.0
        return float(self.fees_per_month / self.initial_balance)

    @property
    def total_return_pct(self) -> float:
        if self.initial_balance <= 0:
            return 0.0
        return float((self.final_value - self.initial_balance) / self.initial_balance)

    @property
    def max_drawdown_pct(self) -> float:
        peak = Decimal(0)
        worst = 0.0
        for _, value in self.equity_curve:
            peak = max(peak, value)
            if peak > 0:
                worst = max(worst, float((peak - value) / peak))
        return worst

    @property
    def closed_trades(self) -> list[PortfolioTrade]:
        return [t for t in self.trades if t.realized_pnl is not None]

    @property
    def win_rate(self) -> float:
        closed = self.closed_trades
        if not closed:
            return 0.0
        return sum(1 for t in closed if (t.realized_pnl or Decimal(0)) > 0) / len(closed)

    @property
    def realized_pnl(self) -> Decimal:
        return sum((t.realized_pnl or Decimal(0) for t in self.closed_trades), start=Decimal(0))


class PortfolioBacktestEngine:
    """Percorre varios pares sobre uma carteira unica."""

    def __init__(
        self,
        strategies: list[Strategy],
        risk_limits: RiskSettings,
        *,
        quote_currency: str = "USDT",
        initial_balance: Decimal = Decimal("1000"),
        fee_pct: Decimal = Decimal("0.001"),
        slippage_pct: Decimal = Decimal("0.0005"),
        lookback: int = 500,
    ) -> None:
        self.strategies = strategies
        self.limits = risk_limits
        self.engine = RiskEngine(risk_limits, quote_currency)
        self.quote_currency = quote_currency
        self.initial_balance = initial_balance
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self.lookback = lookback
        self._warmup = max((s.min_candles for s in strategies), default=50)

    async def run(
        self, candles_by_symbol: dict[str, list[Candle]]
    ) -> PortfolioBacktestResult:
        frames = {
            symbol: _to_frame(candles)
            for symbol, candles in candles_by_symbol.items()
            if len([c for c in candles if c.closed]) > self._warmup + 2
        }
        if not frames:
            raise ValueError("historico insuficiente em todos os pares")

        # Linha do tempo unica: os pares compartilham a carteira, entao precisam
        # ser avaliados na ordem em que os candles realmente fecharam.
        timeline = sorted({ts for frame in frames.values() for ts in frame.index})
        reference = next(iter(candles_by_symbol.values()))[0]
        broker = PaperBroker(
            quote_currency=self.quote_currency,
            initial_balance=self.initial_balance,
            fee_pct=self.fee_pct,
            slippage_pct=self.slippage_pct,
        )

        result = PortfolioBacktestResult(
            quote_currency=self.quote_currency,
            start=timeline[0],
            end=timeline[-1],
            initial_balance=self.initial_balance,
            final_value=self.initial_balance,
            symbols=sorted(frames),
        )

        # Preco de referencia do comprar-e-segurar: primeiro candle apos o
        # aquecimento, que e quando a estrategia poderia ter operado pela
        # primeira vez -- usar o inicio da serie daria vantagem artificial.
        for symbol, frame in frames.items():
            if len(frame) > self._warmup:
                result.first_prices[symbol] = Decimal(str(frame["close"].iloc[self._warmup]))
                result.last_prices[symbol] = Decimal(str(frame["close"].iloc[-1]))

        prices: dict[str, Decimal] = {}
        cost_basis: dict[str, Decimal] = {}
        last_order_at: dict[str, datetime] = {}
        order_seq = 0

        for step, now in enumerate(timeline):
            # 1) Atualiza precos de tudo que fechou candle neste instante.
            for symbol, frame in frames.items():
                if now not in frame.index:
                    continue
                base = symbol.partition("/")[0]
                price = Decimal(str(frame.at[now, "close"]))
                prices[base] = price
                broker.set_price(base, price)

            # 2) Avalia estrategias par a par, na mesma carteira.
            for symbol, frame in frames.items():
                if now not in frame.index:
                    continue
                position = frame.index.get_loc(now)
                if position < self._warmup:
                    continue

                window = frame.iloc[max(0, position + 1 - self.lookback) : position + 1]
                market = MarketFrame(
                    exchange=reference.exchange,
                    symbol=symbol,
                    timeframe=reference.timeframe,
                    frame=window,
                )
                for strategy in self.strategies:
                    signal = strategy.evaluate(market)
                    if signal is None:
                        continue
                    result.signals_generated += 1
                    order_seq += 1
                    await self._handle_signal(
                        signal, broker, prices, cost_basis, last_order_at,
                        result, now, order_seq,
                    )

            # 3) Patrimonio ao fim do instante. Amostrado, nao a cada passo:
            #    16 pares x milhares de candles produziriam uma curva gigante
            #    sem ganho de informacao.
            if step % 4 == 0 or now == timeline[-1]:
                equity = await _equity(broker, prices, self.quote_currency)
                result.equity_curve.append((now, equity))

        result.final_value = await _equity(broker, prices, self.quote_currency)
        log.info(
            "portfolio_backtest.finished",
            symbols=len(frames),
            trades=len(result.trades),
            return_pct=round(result.total_return_pct, 4),
        )
        return result

    async def _handle_signal(
        self, signal, broker, prices, cost_basis, last_order_at, result, now, order_seq
    ) -> None:
        base = signal.symbol.partition("/")[0]
        balances = await broker.fetch_balances()
        cash = balances.get(self.quote_currency, Decimal(0))
        positions = {
            asset: amount for asset, amount in balances.items() if asset != self.quote_currency
        }
        total = cash + sum(
            (amount * prices.get(asset, Decimal(0)) for asset, amount in positions.items()),
            start=Decimal(0),
        )

        state = PortfolioState(
            total_value=total,
            cash=cash,
            positions=positions,
            prices=dict(prices),
            last_order_at=dict(last_order_at),
        )
        assessment = self.engine.evaluate(signal, state, now=now)

        if assessment.decision is RiskDecision.REJECTED:
            result.signals_rejected += 1
            for reason in assessment.reasons:
                result.rejection_reasons[reason.split(":")[0]] += 1
            return
        if not assessment.approved_quantity:
            return

        side = Side.BUY if signal.direction is SignalDirection.LONG else Side.SELL
        request = OrderRequest(
            client_order_id=f"pbt-{order_seq}",
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
            strategy=signal.strategy,
        )
        order = await broker.place_order(request)
        if order.status is not OrderStatus.FILLED:
            return

        fill = order.average_price or signal.reference_price
        quantity = order.filled_quantity
        last_order_at[signal.symbol] = now

        realized: Decimal | None = None
        held = (await broker.fetch_balances()).get(base, Decimal(0))
        if side is Side.BUY:
            cost_basis[base] = cost_basis.get(base, Decimal(0)) + quantity * fill + order.fee
        else:
            previous = held + quantity
            average = (cost_basis.get(base, Decimal(0)) / previous) if previous > 0 else Decimal(0)
            realized = quantity * fill - order.fee - quantity * average
            cost_basis[base] = cost_basis.get(base, Decimal(0)) - quantity * average

        result.trades.append(
            PortfolioTrade(
                timestamp=now,
                symbol=signal.symbol,
                strategy=signal.strategy,
                side=str(side),
                quantity=quantity,
                price=fill,
                notional=quantity * fill,
                fee=order.fee,
                realized_pnl=realized,
            )
        )
        result.trades_per_symbol[signal.symbol] += 1


def _to_frame(candles: list[Candle]) -> pd.DataFrame:
    closed = [c for c in candles if c.closed]
    return pd.DataFrame(
        {
            "open": [float(c.open) for c in closed],
            "high": [float(c.high) for c in closed],
            "low": [float(c.low) for c in closed],
            "close": [float(c.close) for c in closed],
            "volume": [float(c.volume) for c in closed],
        },
        index=pd.DatetimeIndex([c.open_time for c in closed], name="open_time"),
    ).sort_index()


async def _equity(broker: PaperBroker, prices: dict[str, Decimal], quote: str) -> Decimal:
    balances = await broker.fetch_balances()
    total = balances.get(quote, Decimal(0))
    for asset, amount in balances.items():
        if asset != quote:
            total += amount * prices.get(asset, Decimal(0))
    return total
