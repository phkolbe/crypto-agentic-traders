"""Broker simulado (paper trading).

Executa a cadeia completa de decisao sem que nenhuma ordem saia da maquina.
E o broker usado quando `TRADING_MODE=dry_run` -- o padrao de fabrica -- e
tambem o motor de preenchimento do backtest, o que garante que backtest, paper
trading e producao compartilhem exatamente as mesmas regras de execucao.

Aplica taxa e slippage propositalmente: um paper trading otimista demais gera
confianca falsa em uma estrategia que perde dinheiro no mundo real. Pelo mesmo
motivo aplica os filtros da exchange: sem isso o dry_run preenche alegremente
uma ordem que a Binance recusaria por MIN_NOTIONAL, e o ensaio mede uma
estrategia que nao existe.

Os filtros vem de duas fontes, nesta ordem: o que foi informado por
`set_market_filter`/`set_market_filters` (catalogo ao vivo do ccxt) e, na falta
disso, o catalogo versionado do repositorio (`filters.baseline_filters`).
A segunda fonte existe porque a primeira, medida em 2026-09-09, NAO TINHA UM
CHAMADOR em `src/`: o simulador subia sem conhecer filtro de par nenhum e
preenchia tudo, exatamente o problema que este docstring dizia ter resolvido.
Depender de alguem chamar um setter na subida e depender de nada.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..domain.enums import ExchangeName, OrderStatus, OrderType, Side
from ..domain.models import OrderRequest, OrderResult, Position
from ..logging_setup import get_logger
from .base import Broker, InsufficientFunds
from .filters import MarketFilter, baseline_filters, check_quantity_viability

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
        simulated_exchange: str = "binance",
    ) -> None:
        self.quote_currency = quote_currency
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self.simulated_exchange = simulated_exchange
        """Qual exchange o simulador imita, para saber de quem sao os filtros.

        Aplicar o passo de lote da Binance a um par de outra exchange seria
        inventar dado, entao o catalogo e consultado por exchange e devolve
        vazio para quem nao esta versionado.
        """
        self._balances: dict[str, Decimal] = {quote_currency: initial_balance}
        self._last_prices: dict[str, Decimal] = {}
        self._results: dict[str, OrderResult] = {}
        """Resposta original por `client_order_id`.

        Guardar o RESULTADO, e nao apenas o id, e o que torna o reenvio
        realmente inofensivo: a exchange consultada por `clientOrderId` responde
        o estado da ordem que ela ja tem, entao reenviar devolve "foi preenchida
        assim" -- nao "sua ordem falhou". A diferenca custa dinheiro: uma ordem
        preenchida devolvida como falha nao gera trade, e sem trade o Portfolio
        Agent nao reconstroi o preco medio, e sem preco medio o Risk Manager
        pula a posicao ao emitir stop-loss.
        """

        self._market_filters: dict[str, MarketFilter] = {}

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

    def set_market_filter(self, market: MarketFilter) -> None:
        """Ensina ao simulador os filtros reais de um par."""
        self._market_filters[market.symbol] = market

    def set_market_filters(self, markets: dict[str, dict[str, Any]]) -> None:
        """Carrega os filtros a partir do catalogo cru do ccxt."""
        for symbol, market in markets.items():
            self._market_filters[symbol] = MarketFilter.from_ccxt(
                {**market, "symbol": market.get("symbol") or symbol}
            )

    def market_filter(self, symbol: str) -> MarketFilter | None:
        """Implementa `MarketFilterSource`: devolve `None` quando nao sabe.

        Filtro informado tem precedencia sobre o catalogo versionado -- dado ao
        vivo da exchange vale mais do que uma foto datada. `None` significa
        "nao sei", nunca "esta liberado".
        """
        conhecido = self._market_filters.get(symbol)
        if conhecido is not None:
            return conhecido
        return baseline_filters(self.simulated_exchange).get(symbol)

    # ------------------------------------------------------------------
    # Broker
    # ------------------------------------------------------------------
    async def place_order(self, request: OrderRequest) -> OrderResult:
        # Idempotencia: o mesmo `client_order_id` nunca preenche duas vezes.
        # Reenviar devolve a resposta ORIGINAL, que e o que a exchange real
        # responde quando consultada pelo `clientOrderId` -- e nao uma rejeicao,
        # que mentiria dizendo que a ordem nao aconteceu.
        anterior = self._results.get(request.client_order_id)
        if anterior is not None:
            log.warning(
                "paper.duplicate_order_ignored",
                client_order_id=request.client_order_id,
                original_status=str(anterior.status),
            )
            return anterior.model_copy(
                update={"raw": {**anterior.raw, "duplicate": True}}
            )

        base, quote = _split_symbol(request.symbol)
        reference = self._reference_price(request, base)
        if reference is None or reference <= 0:
            # Nao memoriza: sem preco nao houve ordem na exchange, e a tentativa
            # seguinte precisa poder acontecer de verdade.
            return OrderResult(
                order_request_id=request.id,
                client_order_id=request.client_order_id,
                exchange_order_id=None,
                status=OrderStatus.FAILED,
                error=f"sem preco de referencia para {request.symbol}",
            )

        recusa = self._check_filters(request, reference)
        if recusa is not None:
            return recusa

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
            # Nao memoriza: a exchange recusa ANTES de criar a ordem, entao o
            # `clientOrderId` continua livre e uma nova tentativa (depois de um
            # deposito, por exemplo) precisa poder acontecer de verdade.
            return OrderResult(
                order_request_id=request.id,
                client_order_id=request.client_order_id,
                exchange_order_id=None,
                status=OrderStatus.REJECTED,
                error=str(exc),
            )

        self._last_prices[base] = reference

        return self._remember(
            OrderResult(
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
        )

    def _remember(self, result: OrderResult) -> OrderResult:
        """Registra o desfecho para que o reenvio devolva a MESMA resposta.

        So entra aqui o desfecho que CRIOU ordem na exchange. Rejeicao por saldo
        ou por filtro nao cria nada, e memoriza-la faria um reenvio legitimo
        receber para sempre a recusa antiga.
        """
        self._results[result.client_order_id] = result
        return result

    def _check_filters(self, request: OrderRequest, reference: Decimal) -> OrderResult | None:
        """Aplica LOT_SIZE/MIN_NOTIONAL como a exchange faria, quando conhecidos.

        Par desconhecido em qualquer das fontes devolve `None` (segue o
        comportamento historico do simulador). Com filtro, o dry_run recusa o
        que a Binance recusaria -- que e o unico jeito de o ensaio medir a
        estrategia que vai rodar. Vale igual para o backtest, que usa este mesmo
        broker (D3).
        """
        market = self.market_filter(request.symbol)
        if market is None:
            return None
        viability = check_quantity_viability(market, request.quantity, reference)
        if viability.viable:
            return None
        log.warning(
            "paper.rejected_by_exchange_filter",
            symbol=request.symbol,
            quantity=str(request.quantity),
            effective_notional=str(viability.effective_notional),
            filter_source=market.source,
            reason=viability.reason,
        )
        return OrderResult(
            order_request_id=request.id,
            client_order_id=request.client_order_id,
            exchange_order_id=None,
            status=OrderStatus.REJECTED,
            error=f"filtro da exchange: {viability.reason}",
            raw={"simulated": True, "filter": viability.reason},
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
