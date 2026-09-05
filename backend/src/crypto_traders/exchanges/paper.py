"""Broker simulado (paper trading).

Executa a cadeia completa de decisao sem que nenhuma ordem saia da maquina.
E o broker usado quando `TRADING_MODE=dry_run` -- o padrao de fabrica -- e
tambem o motor de preenchimento do backtest, o que garante que backtest, paper
trading e producao compartilhem exatamente as mesmas regras de execucao.

Aplica taxa e slippage propositalmente: um paper trading otimista demais gera
confianca falsa em uma estrategia que perde dinheiro no mundo real.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..domain.enums import ExchangeName, OrderStatus, OrderType, Side
from ..domain.models import OrderRequest, OrderResult, Position
from ..logging_setup import get_logger
from .base import Broker, InsufficientFunds

log = get_logger(__name__)


class PaperBroker(Broker):
    """Carteira simulada com preenchimento imediato, taxa e slippage."""

    name = "paper"

    def __init__(
        self,
        *,
        quote_currency: str = "USDT",
        initial_balance: Decimal = Decimal("1000"),
        fee_pct: Decimal = Decimal("0.001"),
        slippage_pct: Decimal = Decimal("0.0005"),
    ) -> None:
        self.quote_currency = quote_currency
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self._balances: dict[str, Decimal] = {quote_currency: initial_balance}
        self._last_prices: dict[str, Decimal] = {}
        self._filled_client_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Estado
    # ------------------------------------------------------------------
    def set_price(self, asset: str, price: Decimal) -> None:
        self._last_prices[asset] = price

    def set_prices(self, prices: dict[str, Decimal]) -> None:
        self._last_prices.update(prices)

    @property
    def balances(self) -> dict[str, Decimal]:
        return dict(self._balances)

    def credit(self, asset: str, amount: Decimal) -> None:
        self._balances[asset] = self._balances.get(asset, Decimal(0)) + amount

    # ------------------------------------------------------------------
    # Broker
    # ------------------------------------------------------------------
    async def place_order(self, request: OrderRequest) -> OrderResult:
        # Idempotencia: o mesmo `client_order_id` nunca preenche duas vezes,
        # espelhando a garantia que a exchange real oferece.
        if request.client_order_id in self._filled_client_ids:
            log.warning("paper.duplicate_order_ignored", client_order_id=request.client_order_id)
            return OrderResult(
                order_request_id=request.id,
                client_order_id=request.client_order_id,
                exchange_order_id=f"paper-{request.client_order_id}",
                status=OrderStatus.REJECTED,
                error="ordem duplicada (mesmo client_order_id)",
            )

        base, quote = _split_symbol(request.symbol)
        reference = self._reference_price(request, base)
        if reference is None or reference <= 0:
            return OrderResult(
                order_request_id=request.id,
                client_order_id=request.client_order_id,
                exchange_order_id=None,
                status=OrderStatus.FAILED,
                error=f"sem preco de referencia para {request.symbol}",
            )

        # Slippage sempre contra o operador: compra um pouco mais cara, venda um
        # pouco mais barata. Simular a favor seria enganar a si mesmo.
        direction = Decimal(1) if request.side is Side.BUY else Decimal(-1)
        fill_price = reference * (Decimal(1) + direction * self.slippage_pct)
        gross = request.quantity * fill_price
        fee = gross * self.fee_pct

        try:
            if request.side is Side.BUY:
                self._debit(quote, gross + fee)
                self.credit(base, request.quantity)
            else:
                self._debit(base, request.quantity)
                self.credit(quote, gross - fee)
        except InsufficientFunds as exc:
            return OrderResult(
                order_request_id=request.id,
                client_order_id=request.client_order_id,
                exchange_order_id=None,
                status=OrderStatus.REJECTED,
                error=str(exc),
            )

        self._filled_client_ids.add(request.client_order_id)
        self._last_prices[base] = reference

        return OrderResult(
            order_request_id=request.id,
            client_order_id=request.client_order_id,
            exchange_order_id=f"paper-{request.client_order_id}",
            status=OrderStatus.FILLED,
            filled_quantity=request.quantity,
            average_price=fill_price,
            fee=fee,
            fee_currency=quote,
            raw={"simulated": True, "reference_price": str(reference)},
        )

    async def fetch_balances(self) -> dict[str, Decimal]:
        return {asset: amount for asset, amount in self._balances.items() if amount > 0}

    async def fetch_positions(self, prices: dict[str, Decimal]) -> list[Position]:
        merged = {**self._last_prices, **prices}
        return [
            Position(
                exchange=ExchangeName.PAPER,
                asset=asset,
                quantity=quantity,
                current_price=(
                    Decimal(1) if asset == self.quote_currency else merged.get(asset)
                ),
            )
            for asset, quantity in self._balances.items()
            if quantity > 0
        ]

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------
    def _debit(self, asset: str, amount: Decimal) -> None:
        available = self._balances.get(asset, Decimal(0))
        if available < amount:
            raise InsufficientFunds(
                f"saldo insuficiente de {asset}: disponivel {available}, necessario {amount}"
            )
        self._balances[asset] = available - amount

    def _reference_price(self, request: OrderRequest, base: str) -> Decimal | None:
        if request.order_type is OrderType.LIMIT and request.price is not None:
            return request.price
        return self._last_prices.get(base)

    def snapshot(self) -> dict[str, Any]:
        return {
            "balances": {k: str(v) for k, v in self._balances.items()},
            "prices": {k: str(v) for k, v in self._last_prices.items()},
        }


def _split_symbol(symbol: str) -> tuple[str, str]:
    """`BTC/USDT` -> `("BTC", "USDT")`."""
    base, _, quote = symbol.partition("/")
    return base, quote or "USDT"
