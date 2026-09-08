"""Filtros que a exchange impoe a cada ordem.

O Risk Manager sabe dimensionar uma ordem segundo os SEUS limites, mas nao
conhece as regras da exchange. Elas existem e derrubam ordem pequena:

- **LOT_SIZE / stepSize**: a quantidade precisa ser multiplo de um passo, e o
  ccxt **trunca** para baixo ao enviar.
- **MIN_NOTIONAL**: o valor final precisa alcancar um minimo (5 USDT na Binance
  para a maioria dos pares spot).

A combinacao das duas cria uma armadilha que so aparece em ordem pequena. Em
BTC/USDT o passo e 0,00001 e, a 79 mil, cada passo vale ~0,79 USDT: uma ordem
mirando 5,50 USDT vira 0,00006 BTC = **4,74 USDT** depois do truncamento, abaixo
do minimo -- e a Binance rejeita. O mesmo valor passa tranquilo em ETH, SOL ou
DOGE, cujos passos sao finos em relacao ao preco.

Sem esta checagem, o diagnostico aprovaria uma configuracao em que toda ordem
seria recusada pela exchange -- e a descoberta viria na primeira tentativa de
operar com dinheiro real.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from typing import Any


@dataclass(frozen=True)
class MarketFilter:
    """Restricoes da exchange para um par."""

    symbol: str
    amount_step: Decimal
    """Menor incremento de quantidade aceito (stepSize do LOT_SIZE)."""

    min_cost: Decimal
    """Valor minimo da ordem na moeda de cotacao (MIN_NOTIONAL)."""

    min_amount: Decimal

    @classmethod
    def from_ccxt(cls, market: dict[str, Any]) -> MarketFilter:
        """Le os filtros do dicionario de mercado do ccxt.

        `precision.amount` vem como passo decimal (0.00001) na maioria das
        exchanges, mas como numero de casas (5) quando o ccxt esta em
        `DECIMAL_PLACES` -- os dois casos precisam virar o mesmo passo.
        """
        precision = (market.get("precision") or {}).get("amount")
        if precision is None:
            step = Decimal("0.00000001")
        elif isinstance(precision, int):
            step = Decimal(1).scaleb(-precision)
        else:
            step = Decimal(str(precision))

        limits = market.get("limits") or {}
        min_cost = (limits.get("cost") or {}).get("min")
        min_amount = (limits.get("amount") or {}).get("min")

        return cls(
            symbol=str(market.get("symbol", "")),
            amount_step=step if step > 0 else Decimal("0.00000001"),
            min_cost=Decimal(str(min_cost)) if min_cost else Decimal(0),
            min_amount=Decimal(str(min_amount)) if min_amount else Decimal(0),
        )


@dataclass(frozen=True)
class OrderViability:
    """Se uma ordem sobrevive aos filtros da exchange."""

    symbol: str
    requested_notional: Decimal
    effective_notional: Decimal
    """Valor que sobra DEPOIS do truncamento ao passo do lote."""

    min_cost: Decimal
    viable: bool
    reason: str
    suggested_notional: Decimal
    """Menor valor que passaria, ja com uma folga de um passo."""

    def explain(self, quote_currency: str = "USDT") -> str:
        if self.viable:
            return (
                f"{self.symbol}: {self.requested_notional:.2f} -> "
                f"{self.effective_notional:.2f} {quote_currency} apos arredondar (OK)"
            )
        return (
            f"{self.symbol}: {self.requested_notional:.2f} vira "
            f"{self.effective_notional:.2f} {quote_currency} apos o arredondamento de "
            f"lote — {self.reason} Seriam necessarios ao menos "
            f"{self.suggested_notional:.2f}."
        )


def check_order_viability(
    market: MarketFilter, notional: Decimal, price: Decimal
) -> OrderViability:
    """Simula o que a exchange fara com uma ordem deste tamanho.

    Reproduz exatamente o caminho real: o ccxt trunca a quantidade para o passo
    (`amount_to_precision`) e so entao a Binance aplica o MIN_NOTIONAL sobre o
    valor resultante.
    """
    if price <= 0:
        return OrderViability(
            symbol=market.symbol,
            requested_notional=notional,
            effective_notional=Decimal(0),
            min_cost=market.min_cost,
            viable=False,
            reason="preco invalido.",
            suggested_notional=Decimal(0),
        )

    step = market.amount_step
    raw_amount = notional / price
    amount = (raw_amount / step).to_integral_value(rounding=ROUND_DOWN) * step
    effective = amount * price

    # Valor de um passo na moeda de cotacao: e a granularidade real do par.
    step_value = step * price
    if market.min_cost > 0:
        steps_needed = (market.min_cost / step_value).to_integral_value(rounding=ROUND_CEILING)
    else:
        steps_needed = Decimal(1)
    # Um passo de folga: o preco se move entre a decisao e o envio, e uma ordem
    # exatamente na fronteira e rejeitada por qualquer variacao contraria.
    suggested = (steps_needed + 1) * step_value

    if amount < market.min_amount:
        return OrderViability(
            symbol=market.symbol,
            requested_notional=notional,
            effective_notional=effective,
            min_cost=market.min_cost,
            viable=False,
            reason=f"quantidade {amount} abaixo do lote minimo {market.min_amount}.",
            suggested_notional=suggested,
        )

    if amount <= 0:
        return OrderViability(
            symbol=market.symbol,
            requested_notional=notional,
            effective_notional=Decimal(0),
            min_cost=market.min_cost,
            viable=False,
            reason="a quantidade arredonda para zero.",
            suggested_notional=suggested,
        )

    if effective < market.min_cost:
        return OrderViability(
            symbol=market.symbol,
            requested_notional=notional,
            effective_notional=effective,
            min_cost=market.min_cost,
            viable=False,
            reason=f"abaixo do minimo de {market.min_cost} exigido pela exchange.",
            suggested_notional=suggested,
        )

    return OrderViability(
        symbol=market.symbol,
        requested_notional=notional,
        effective_notional=effective,
        min_cost=market.min_cost,
        viable=True,
        reason="",
        suggested_notional=suggested,
    )


def check_all(
    markets: dict[str, dict[str, Any]],
    tickers: dict[str, dict[str, Any]],
    symbols: list[str],
    notional: Decimal,
) -> list[OrderViability]:
    """Avalia a ordem em cada par configurado.

    Pares sem mercado ou sem preco sao ignorados em vez de virarem falha: quem
    reclama de simbolo inexistente e o Market Data Agent, e duplicar esse erro
    aqui so confundiria o diagnostico.
    """
    results: list[OrderViability] = []
    for symbol in symbols:
        market = markets.get(symbol)
        ticker = tickers.get(symbol) or {}
        price = ticker.get("last") or ticker.get("close")
        if market is None or not price:
            continue
        results.append(
            check_order_viability(
                MarketFilter.from_ccxt(market), notional, Decimal(str(price))
            )
        )
    return results
