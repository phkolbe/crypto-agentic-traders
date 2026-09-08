"""Descoberta automatica de pares negociaveis ("mar aberto").

Quando `SYMBOLS` esta vazio, o sistema nao fica parado nem adivinha: ele pergunta
a exchange quais pares existem e escolhe os melhores por criterios objetivos.

## O que isso muda na seguranca -- leia antes de usar

O plano original tratava a **whitelist de ativos** como uma das camadas de
protecao: uma lista curta, aprovada por uma pessoa, que impedia o sistema de
tocar em token ilíquido ou desconhecido. No modo de descoberta essa camada deixa
de ser uma lista aprovada a mao e passa a ser um **conjunto de criterios**.

Isso e uma protecao mais fraca, e a compensacao precisa ser explicita:

- **Piso de liquidez** por volume negociado em 24h. Dos 487 pares spot em USDT da
  Binance, 312 movimentam menos de 1M por dia -- nesses, a propria ordem move o
  preco e o slippage come o resultado.
- **Teto na quantidade** de pares monitorados. Sem isso, "mar aberto" viraria
  dezenas de posicoes simultaneas.
- **Exclusao de stablecoins**: USDC/USDT e USD1/USDT estao entre os maiores
  volumes da Binance, e sao exatamente o que uma estrategia de tendencia nao
  deve negociar -- o preco nao anda, entao todo sinal e ruido e toda operacao e
  taxa pura.
- A lista descoberta **vira a whitelist efetiva** e e gravada no audit log a cada
  mudanca. Em qualquer instante existe uma lista concreta e inspecionavel do que
  o sistema pode negociar; ela so deixou de ser digitada a mao.

Os demais limites do Risk Manager (maximo de posicoes abertas, exposicao por
ativo, tamanho por ordem) continuam valendo e passam a ser a principal defesa.

## Sobre heuristica de nome

Nao ha filtro de "token alavancado" por sufixo aqui, de proposito. Procurar
`UP`/`DOWN`/`BULL`/`BEAR` no nome marca JUP (Jupiter), SYRUP e SUPER como
alavancados -- falso positivo do mesmo tipo que procurar "ip" dentro de
"multiple". Para excluir um ativo especifico existe `DISCOVERY_EXCLUDE_ASSETS`,
que e explicito e nao erra.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .logging_setup import get_logger

log = get_logger(__name__)

#: Stablecoins e moedas fiduciarias tokenizadas. Como base, formam pares que nao
#: se movem (USDC/USDT); como isso rende sinal nenhum e taxa sempre, ficam fora.
#:
#: `USDT` esta na lista por um motivo que so aparece com outra moeda de cotacao:
#: com `QUOTE_CURRENCY=USDT` nao existe par USDT/USDT e a ausencia era inofensiva,
#: mas com `QUOTE_CURRENCY=BRL` o USDT/BRL e o MAIOR volume da Binance no Brasil
#: -- a descoberta o escolheria em primeiro lugar, e negociar stablecoin contra
#: fiat com estrategia de tendencia e so pagar taxa.
DEFAULT_EXCLUDED_ASSETS = frozenset(
    {
        "USDT", "USDC", "USD1", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDS",
        "PYUSD", "EUR", "EURI", "AEUR", "GBP", "TRY", "BRL", "ARS", "JPY",
    }
)


@dataclass(frozen=True)
class DiscoveryCriteria:
    """Regras que um par precisa cumprir para o sistema aceita-lo."""

    quote_currency: str = "USDT"
    min_quote_volume_24h: Decimal = Decimal("50000000")
    """Piso de liquidez, na moeda de cotacao. Abaixo disso a propria ordem move o preco."""

    max_symbols: int = 8
    """Teto de pares monitorados. Sem teto, "mar aberto" vira dezenas de posicoes."""

    exclude_assets: frozenset[str] = DEFAULT_EXCLUDED_ASSETS


@dataclass(frozen=True)
class DiscoveredMarket:
    symbol: str
    base: str
    quote_volume_24h: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "base": self.base,
            "quote_volume_24h": str(self.quote_volume_24h),
        }


@dataclass
class DiscoveryResult:
    """Resultado de uma varredura, com o rastro de por que cada par entrou."""

    markets: list[DiscoveredMarket] = field(default_factory=list)
    considered: int = 0
    rejected_low_volume: int = 0
    rejected_excluded: int = 0

    @property
    def symbols(self) -> list[str]:
        return [m.symbol for m in self.markets]

    @property
    def assets(self) -> list[str]:
        return [m.base for m in self.markets]

    def summary(self) -> dict[str, Any]:
        return {
            "symbols": self.symbols,
            "considered": self.considered,
            "rejected_low_volume": self.rejected_low_volume,
            "rejected_excluded": self.rejected_excluded,
            "markets": [m.as_dict() for m in self.markets],
        }


def select_markets(
    markets: dict[str, dict[str, Any]],
    tickers: dict[str, dict[str, Any]],
    criteria: DiscoveryCriteria,
) -> DiscoveryResult:
    """Escolhe os pares a partir dos dados crus da exchange.

    Funcao pura -- recebe os dicionarios que o ccxt devolve e nao faz rede.
    E o que permite testar a selecao com casos reais (stablecoin no topo do
    volume, par ilíquido, mercado inativo) sem depender da Binance estar no ar.
    """
    result = DiscoveryResult()
    candidates: list[DiscoveredMarket] = []

    for market in markets.values():
        if not (market.get("spot") and market.get("active")):
            continue
        if market.get("quote") != criteria.quote_currency:
            continue

        result.considered += 1
        base = str(market.get("base") or "")

        if base.upper() in criteria.exclude_assets:
            result.rejected_excluded += 1
            continue

        ticker = tickers.get(market["symbol"]) or {}
        raw_volume = ticker.get("quoteVolume")
        if raw_volume is None:
            result.rejected_low_volume += 1
            continue

        volume = Decimal(str(raw_volume))
        if volume < criteria.min_quote_volume_24h:
            result.rejected_low_volume += 1
            continue

        candidates.append(
            DiscoveredMarket(symbol=market["symbol"], base=base, quote_volume_24h=volume)
        )

    # Mais liquido primeiro: o volume e o melhor proxy disponivel para "a ordem
    # sai pelo preco que eu vi".
    candidates.sort(key=lambda m: m.quote_volume_24h, reverse=True)
    result.markets = candidates[: criteria.max_symbols]
    return result


async def discover(source: Any, criteria: DiscoveryCriteria) -> DiscoveryResult:
    """Varre a exchange e devolve os pares aprovados.

    `source` precisa expor `fetch_markets_and_tickers()`. Usa endpoints publicos:
    descoberta nao envolve credencial.
    """
    markets, tickers = await source.fetch_markets_and_tickers()
    result = select_markets(markets, tickers, criteria)
    log.info(
        "discovery.completed",
        selected=result.symbols,
        considered=result.considered,
        rejected_low_volume=result.rejected_low_volume,
        rejected_excluded=result.rejected_excluded,
        min_volume=str(criteria.min_quote_volume_24h),
    )
    return result
