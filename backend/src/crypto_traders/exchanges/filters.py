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

Este modulo nasceu como diagnostico (`crypto check`), e por meses foi SO isso:
medido em 2026-09-09, `check_order_viability` era chamado unicamente pelo
`cli.py`. Ou seja, o caminho de envio nao consultava filtro nenhum, e uma ordem
que o arredondamento jogava abaixo do MIN_NOTIONAL era mandada para a exchange e
recusada por ela. `MarketFilterSource` existe para fechar isso: o Execution
Agent consulta os mesmos filtros antes de enviar, e recusa com motivo.

E ter a checagem nao basta: medido em 2026-09-09, `MarketFilterSource` estava
implementado, testado e **inerte** -- ninguem em `src/` carregava filtro em
broker nenhum, entao `market_filter()` devolvia `None` em toda configuracao real
e o comportamento continuava sendo "manda e deixa a exchange recusar". O
catalogo de referencia (`baseline_filters`) existe para que a checagem tenha
dado na mao no instante em que o processo sobe, sem depender de nenhuma chamada
de rede ter acontecido antes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from ..logging_setup import get_logger

log = get_logger(__name__)

#: Filtro lido da exchange agora: dado exato, vale decidir sobre ele.
FONTE_AO_VIVO = "live"

#: Filtro lido do catalogo de referencia versionado no repositorio. E uma foto
#: datada do `load_markets()`, nao a verdade de agora -- por isso a fonte viaja
#: junto com o filtro e aparece no log e no alerta de recusa.
FONTE_BASELINE = "baseline"

#: Catalogo por exchange. So a Binance esta versionada porque e a exchange do
#: projeto; par de outra exchange devolve `None` de proposito, em vez de aplicar
#: o passo de lote da Binance a um mercado que nao e dela.
_ARQUIVOS_BASELINE = {"binance": "binance_spot_filters.json"}


@dataclass(frozen=True)
class MarketFilter:
    """Restricoes da exchange para um par."""

    symbol: str
    amount_step: Decimal
    """Menor incremento de quantidade aceito (stepSize do LOT_SIZE)."""

    min_cost: Decimal
    """Valor minimo da ordem na moeda de cotacao (MIN_NOTIONAL)."""

    min_amount: Decimal

    source: str = FONTE_AO_VIVO
    """De onde veio este filtro: `live` (exchange agora) ou `baseline` (catalogo).

    Campo com valor padrao e no fim para nao quebrar quem constroi
    `MarketFilter` posicionalmente. Existe porque recusar uma ordem por um passo
    de lote que pode ter mudado e uma afirmacao mais fraca do que recusar pelo
    dado que a exchange acabou de informar, e quem le o alerta precisa saber a
    diferenca.
    """

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

    def truncate(self, quantity: Decimal) -> Decimal:
        """Quantidade truncada ao passo do lote -- exatamente o que o ccxt faz.

        Truncar, nunca arredondar: o ccxt usa `TRUNCATE` em
        `amount_to_precision`, e arredondar para cima aqui faria a simulacao
        prever uma ordem MAIOR do que a exchange aceita.
        """
        if self.amount_step <= 0:
            return quantity
        passos = (quantity / self.amount_step).to_integral_value(rounding=ROUND_DOWN)
        return passos * self.amount_step


@runtime_checkable
class MarketFilterSource(Protocol):
    """Broker capaz de informar os filtros da exchange para um par.

    Protocolo em vez de metodo obrigatorio no `Broker` de proposito: broker que
    nao sabe responder devolve `None` (ou nem implementa), e o Execution Agent
    trata isso como "sem informacao", nunca como "aprovado". A checagem so
    recusa quando tem dado concreto dizendo que a ordem morreria na exchange.
    """

    def market_filter(self, symbol: str) -> MarketFilter | None: ...


@lru_cache(maxsize=len(_ARQUIVOS_BASELINE) + 1)
def baseline_filters(exchange: str = "binance") -> Mapping[str, MarketFilter]:
    """Catalogo de filtros versionado no repositorio, por par.

    E uma foto do `load_markets()` da exchange (endpoint publico, sem
    credencial) guardada em JSON ao lado deste modulo. Serve de piso: o
    Execution Agent e o `PaperBroker` passam a conhecer LOT_SIZE e MIN_NOTIONAL
    no instante em que sobem, sem depender de rede e sem depender de alguem ter
    chamado `set_market_filters` antes -- que foi exatamente o que faltava e
    deixava a checagem inerte.

    Foto datada nao e a verdade de agora, e o campo `source` diz isso a quem
    consome. O erro possivel e assimetrico e conhecido: se o passo real ficou
    MAIS FINO que o do catalogo, a ordem e recusada aqui em vez de recusada pela
    exchange -- recusa com motivo, que e o comportamento pedido. Filtro ao vivo,
    quando existe, sempre tem precedencia (ver `PaperBroker.market_filter` e
    `CcxtExchange.market_filter`).

    Nunca levanta: catalogo ilegivel volta vazio e o sistema segue no
    comportamento antigo (envia e a exchange decide), com o erro no log.
    """
    arquivo = _ARQUIVOS_BASELINE.get(exchange.strip().lower())
    if arquivo is None:
        return MappingProxyType({})
    caminho = Path(__file__).with_name(arquivo)
    try:
        payload = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error(
            "filters.baseline_ilegivel",
            exchange=exchange,
            arquivo=str(caminho),
            error=str(exc),
        )
        return MappingProxyType({})

    catalogo: dict[str, MarketFilter] = {}
    for grupo in payload.get("groups") or []:
        try:
            step = Decimal(str(grupo["amount_step"]))
            min_cost = Decimal(str(grupo["min_cost"]))
            min_amount = Decimal(str(grupo["min_amount"]))
        except (KeyError, TypeError, InvalidOperation) as exc:
            log.error("filters.baseline_grupo_invalido", error=str(exc))
            continue
        if step <= 0:
            continue
        for symbol in grupo.get("symbols") or []:
            catalogo[str(symbol)] = MarketFilter(
                symbol=str(symbol),
                amount_step=step,
                min_cost=min_cost,
                min_amount=min_amount,
                source=FONTE_BASELINE,
            )
    if not catalogo:
        log.error("filters.baseline_vazio", exchange=exchange, arquivo=str(caminho))
    # Somente leitura: o catalogo e cacheado por processo, e um chamador que o
    # mutasse mudaria o filtro visto por todo mundo.
    return MappingProxyType(catalogo)


def baseline_market_filter(symbol: str, exchange: str = "binance") -> MarketFilter | None:
    """Filtro do catalogo versionado para um par, ou `None` se ele nao esta la."""
    return baseline_filters(exchange).get(symbol)


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

    effective_amount: Decimal = Decimal(0)
    """Quantidade que sobra depois do truncamento ao passo do lote.

    Ultimo campo e com valor padrao para nao quebrar quem constroi
    `OrderViability` posicionalmente.
    """

    source: str = FONTE_AO_VIVO
    """Fonte do filtro usado nesta conclusao (ver `MarketFilter.source`)."""

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
    """Simula o que a exchange fara com uma ordem deste VALOR (diagnostico).

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
            source=market.source,
        )
    return check_quantity_viability(
        market, notional / price, price, requested_notional=notional
    )


def check_quantity_viability(
    market: MarketFilter,
    quantity: Decimal,
    price: Decimal,
    *,
    requested_notional: Decimal | None = None,
) -> OrderViability:
    """Mesma checagem, a partir da QUANTIDADE -- a entrada do caminho de envio.

    O `OrderRequest` que chega ao Execution Agent carrega quantidade aprovada,
    nao um valor alvo; comecar pelo valor obrigaria a redividir pelo preco e
    reintroduziria erro de arredondamento na fronteira que estamos justamente
    checando.
    """
    if requested_notional is None:
        requested_notional = quantity * price

    if price <= 0:
        return OrderViability(
            symbol=market.symbol,
            requested_notional=requested_notional,
            effective_notional=Decimal(0),
            min_cost=market.min_cost,
            viable=False,
            reason="preco invalido.",
            suggested_notional=Decimal(0),
            source=market.source,
        )

    step = market.amount_step
    notional = requested_notional
    amount = market.truncate(quantity)
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

    # Zero antes do lote minimo: "arredonda para zero" diz o que aconteceu,
    # enquanto "quantidade 0 abaixo do lote minimo" descreve o sintoma.
    if amount <= 0:
        return OrderViability(
            symbol=market.symbol,
            requested_notional=notional,
            effective_notional=Decimal(0),
            min_cost=market.min_cost,
            viable=False,
            reason="a quantidade arredonda para zero.",
            suggested_notional=suggested,
            effective_amount=Decimal(0),
            source=market.source,
        )

    if amount < market.min_amount:
        return OrderViability(
            symbol=market.symbol,
            requested_notional=notional,
            effective_notional=effective,
            min_cost=market.min_cost,
            viable=False,
            reason=f"quantidade {amount} abaixo do lote minimo {market.min_amount}.",
            suggested_notional=suggested,
            effective_amount=amount,
            source=market.source,
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
            effective_amount=amount,
            source=market.source,
        )

    return OrderViability(
        symbol=market.symbol,
        requested_notional=notional,
        effective_notional=effective,
        min_cost=market.min_cost,
        viable=True,
        reason="",
        suggested_notional=suggested,
        effective_amount=amount,
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
