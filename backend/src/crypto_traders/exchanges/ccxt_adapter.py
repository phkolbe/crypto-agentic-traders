"""Adaptador ccxt para Binance e Coinbase.

ccxt normaliza as diferencas entre exchanges (formato de simbolo, paginacao,
codigos de erro), o que permite adicionar uma terceira exchange depois sem tocar
nos agentes.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt

from ..config import ExchangeCredentials
from ..domain.enums import ExchangeName, OrderStatus, OrderType, Side
from ..domain.models import Candle, OrderRequest, OrderResult, Position, Ticker
from ..logging_setup import get_logger
from .base import ApiAccessDenied, Broker, ExchangeError, InsufficientFunds, MarketDataSource
from .filters import MarketFilter, baseline_market_filter

log = get_logger(__name__)

#: ccxt normaliza os status; mapeamos para o nosso enum.
_STATUS_MAP = {
    "open": OrderStatus.OPEN,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}

#: Erros de rede/limite: transitorios, valem retry com backoff.
_RETRYABLE = (
    ccxt.NetworkError,
    ccxt.RequestTimeout,
    ccxt.ExchangeNotAvailable,
    ccxt.DDoSProtection,
    ccxt.RateLimitExceeded,
)

#: Credencial recusada. Retentar nunca resolve, e o operador precisa saber AGORA.
_ACCESS_DENIED = (
    ccxt.AuthenticationError,
    ccxt.PermissionDenied,
)

#: Sinais de que o problema e o IP, e nao a chave em si. Na Binance, `-2015`
#: cobre "chave invalida, IP ou permissao" -- os tres casos caem na mesma
#: mensagem, e num setup residencial a causa mais provavel e o IP.
#:
#: `-2014` ("API-key format invalid") NAO entra aqui de proposito: e um problema
#: da chave, e apontar o IP nesse caso mandaria o operador investigar o lugar
#: errado.
#:
#: O `ip` usa limite de palavra de proposito: "ip" como substring solta
#: casaria com "multiple", "description" e praticamente qualquer mensagem.
_IP_HINT = re.compile(r"-2015|whitelist|ip", re.IGNORECASE)


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal(0)
    return Decimal(str(value))


def _linhas_ohlcv_confiaveis(
    exchange: str, symbol: str, raw: Any
) -> list[tuple[Any, Any, Any, Any, Any, Any]]:
    """Filtra, ordena e desduplica as linhas cruas de `fetch_ohlcv`.

    Tres coisas que a resposta da exchange nao garante, e que quebram de formas
    diferentes se assumidas:

    * **Linha completa.** Resposta truncada no meio (conexao cortada, proxy) traz
      linha curta ou com `None`. Desempacotar direto levanta `ValueError` e
      derruba o par inteiro; deixar o `None` virar `Decimal(0)` e pior ainda,
      porque produz candle com preco ZERO -- e preco zero viaja para
      `latest_prices`, que o Portfolio e o Risk usam para avaliar posicao.
    * **Ordem cronologica.** Quem esta "em formacao" e decidido por posicao. Se a
      resposta vier decrescente (ou embaralhada por um failover de replica), o
      candle mais antigo seria marcado como aberto e o mais NOVO como fechado --
      e o agente publicaria um candle velho como se fosse o atual.
    * **Timestamp unico.** Uma duplicata do candle em formacao promove a copia
      anterior a "fechada", entregando a estrategia um preco que ainda vai mudar.

    Descartar linha ruim e correto aqui: o candle volta na proxima coleta, e o
    custo de esperar um ciclo e infinitamente menor que decidir com preco falso.
    """
    validas: dict[Any, tuple[Any, Any, Any, Any, Any, Any]] = {}
    for row in raw or []:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            log.warning(
                "exchange.ohlcv_linha_truncada",
                exchange=exchange,
                symbol=symbol,
                linha=str(row)[:120],
            )
            continue
        timestamp, open_, high, low, close, volume = row[:6]
        if timestamp is None or any(v is None for v in (open_, high, low, close)):
            log.warning(
                "exchange.ohlcv_linha_incompleta",
                exchange=exchange,
                symbol=symbol,
                linha=str(row)[:120],
            )
            continue
        # A duplicata mais tardia na resposta vence: e a leitura mais recente da
        # mesma janela de tempo.
        validas[timestamp] = (timestamp, open_, high, low, close, volume)
    return [validas[chave] for chave in sorted(validas)]


class CcxtExchange(MarketDataSource, Broker):
    """Implementa leitura de mercado e execucao sobre um cliente ccxt."""

    def __init__(
        self,
        exchange_id: str,
        credentials: ExchangeCredentials | None = None,
        *,
        testnet: bool = False,
        max_retries: int = 3,
    ) -> None:
        self.name = exchange_id
        self._testnet = testnet
        self._max_retries = max_retries

        config: dict[str, Any] = {
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
        if credentials and credentials.configured:
            config["apiKey"] = credentials.api_key.get_secret_value()
            config["secret"] = credentials.api_secret.get_secret_value()
            if credentials.passphrase is not None:
                config["password"] = credentials.passphrase.get_secret_value()

        self._client: Any = getattr(ccxt, exchange_id)(config)
        if testnet:
            # Nem toda exchange tem sandbox; falhar aqui e melhor do que
            # descobrir em producao que "testnet" era live.
            self._client.set_sandbox_mode(True)

        self._authenticated = bool(credentials and credentials.configured)

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    async def _with_retry(self, operation: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Retry com backoff exponencial, apenas para erros transitorios.

        Erros de negocio (saldo insuficiente, ordem invalida) sobem na hora:
        repetir nao muda o resultado e so atrasa o diagnostico.
        """
        delay = 1.0
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except ccxt.InsufficientFunds as exc:
                raise InsufficientFunds(str(exc)) from exc
            except _ACCESS_DENIED as exc:
                raise ApiAccessDenied(
                    _access_denied_message(self.name, operation, str(exc)),
                    exchange=self.name,
                    operation=operation,
                ) from exc
            except _RETRYABLE as exc:
                last_error = exc
                log.warning(
                    "exchange.retry",
                    exchange=self.name,
                    operation=operation,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(delay)
                    delay *= 2
            except ccxt.BaseError as exc:
                raise ExchangeError(f"{self.name}.{operation}: {exc}") from exc
        raise ExchangeError(
            f"{self.name}.{operation} falhou apos {self._max_retries} tentativas: {last_error}"
        )

    # ------------------------------------------------------------------
    # MarketDataSource
    # ------------------------------------------------------------------
    async def fetch_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        raw = await self._with_retry(
            "fetch_ohlcv", self._client.fetch_ohlcv, symbol, timeframe, None, limit
        )
        exchange_name = self._exchange_enum()
        linhas = _linhas_ohlcv_confiaveis(self.name, symbol, raw)
        candles: list[Candle] = []
        for index, (timestamp, open_, high, low, close, volume) in enumerate(linhas):
            candles.append(
                Candle(
                    exchange=exchange_name,
                    # Simbolo e timeframe sao os PEDIDOS, nunca o que a resposta
                    # sugerir: a linha do ccxt e so [ts, o, h, l, c, v], e o
                    # Market Data Agent recusa candle cuja identidade nao seja a
                    # pedida. Se algum dia esta origem mudar, essa recusa la e
                    # que impede o preco de um par virar o preco de outro.
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=datetime.fromtimestamp(timestamp / 1000, tz=UTC),
                    open=_dec(open_),
                    high=_dec(high),
                    low=_dec(low),
                    close=_dec(close),
                    volume=_dec(volume),
                    # O candle mais RECENTE ainda esta em formacao: marcamos como
                    # aberto para que nenhuma estrategia decida com base em um
                    # preco que ainda vai mudar. Isso so vale porque
                    # `_linhas_ohlcv_confiaveis` ja ordenou por tempo -- posicao
                    # crua na resposta nao e garantia de ordem cronologica.
                    closed=index < len(linhas) - 1,
                )
            )
        return candles

    async def fetch_markets_and_tickers(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        markets = await self._with_retry("load_markets", self._client.load_markets)
        # `fetch_tickers` sem argumentos traz o resumo de 24h de todos os pares
        # em UMA chamada; pedir par a par seriam centenas de requisicoes e o
        # rate limit da exchange derrubaria a varredura.
        tickers = await self._with_retry("fetch_tickers", self._client.fetch_tickers)
        return markets, tickers

    async def fetch_ticker(self, symbol: str) -> Ticker:
        raw = await self._with_retry("fetch_ticker", self._client.fetch_ticker, symbol)
        price = raw.get("last") or raw.get("close") or raw.get("bid")
        return Ticker(exchange=self._exchange_enum(), symbol=symbol, price=_dec(price))

    # ------------------------------------------------------------------
    # Broker
    # ------------------------------------------------------------------
    async def place_order(self, request: OrderRequest) -> OrderResult:
        if not self._authenticated:
            raise ExchangeError(
                f"{self.name}: envio de ordem sem credenciais configuradas. "
                "Verifique BINANCE__API_KEY / COINBASE__API_KEY no .env."
            )

        params: dict[str, Any] = {"clientOrderId": request.client_order_id}
        try:
            raw = await self._with_retry(
                "create_order",
                self._client.create_order,
                request.symbol,
                str(request.order_type),
                str(request.side),
                float(request.quantity),
                float(request.price) if request.order_type is OrderType.LIMIT else None,
                params,
            )
        except ApiAccessDenied:
            # Propositalmente NAO virando um OrderResult genérico: quem chama
            # precisa poder distinguir "esta ordem falhou" de "o sistema perdeu
            # acesso a exchange", que exige alerta imediato.
            raise
        except ExchangeError as exc:
            return OrderResult(
                order_request_id=request.id,
                client_order_id=request.client_order_id,
                exchange_order_id=None,
                status=OrderStatus.FAILED,
                error=str(exc),
            )

        return OrderResult(
            order_request_id=request.id,
            client_order_id=request.client_order_id,
            exchange_order_id=str(raw.get("id")) if raw.get("id") else None,
            status=_STATUS_MAP.get(raw.get("status") or "", OrderStatus.OPEN),
            filled_quantity=_dec(raw.get("filled")),
            average_price=_dec(raw["average"]) if raw.get("average") else None,
            fee=_dec((raw.get("fee") or {}).get("cost")),
            fee_currency=(raw.get("fee") or {}).get("currency"),
            raw={k: v for k, v in raw.items() if k != "info"},
        )

    def market_filter(self, symbol: str) -> MarketFilter | None:
        """Implementa `MarketFilterSource` com o catalogo da propria exchange.

        Sem isto o `_preflight` do Execution Agent saia `None` na primeira linha
        em LIVE e TESTNET (medido em 2026-09-09: `issubclass(CcxtExchange,
        MarketFilterSource)` era False), e a ordem que o truncamento de lote
        joga abaixo do MIN_NOTIONAL era enviada e recusada pela exchange.

        `self._client.markets` so esta populado depois de um `load_markets`
        nesta instancia -- o que acontece dentro do primeiro `create_order`.
        Antes disso vale o catalogo versionado, para que a PRIMEIRA ordem do
        processo tambem seja checada.
        """
        mercados = getattr(self._client, "markets", None) or {}
        market = mercados.get(symbol)
        if market:
            return MarketFilter.from_ccxt({**market, "symbol": symbol})
        return baseline_market_filter(symbol, self.name)

    async def fetch_balances(self) -> dict[str, Decimal]:
        raw = await self._with_retry("fetch_balance", self._client.fetch_balance)
        totals = raw.get("total", {})
        return {asset: _dec(amount) for asset, amount in totals.items() if _dec(amount) > 0}

    async def fetch_positions(self, prices: dict[str, Decimal]) -> list[Position]:
        """Posicoes spot derivadas do saldo.

        Spot nao tem "posicao" no sentido de derivativos: ter 0.01 BTC ja e a
        posicao. O preco medio de entrada nao vem da exchange e e reconstruido
        pelo Portfolio Agent a partir do historico de trades.
        """
        balances = await self.fetch_balances()
        exchange_name = self._exchange_enum()
        return [
            Position(
                exchange=exchange_name,
                asset=asset,
                quantity=quantity,
                current_price=prices.get(asset),
            )
            for asset, quantity in balances.items()
        ]

    async def close(self) -> None:
        await self._client.close()

    def _exchange_enum(self) -> ExchangeName:
        try:
            return ExchangeName(self.name)
        except ValueError:
            return ExchangeName.BINANCE


def _access_denied_message(exchange: str, operation: str, raw: str) -> str:
    """Mensagem que aponta para a causa provável, em vez de repetir o erro cru."""
    looks_like_ip = bool(_IP_HINT.search(raw))
    detail = (
        "Causa mais provavel: o IP desta maquina mudou e nao esta mais na "
        "whitelist da API. Confira o IP atual e atualize a whitelist na "
        "exchange. Verifique tambem se a permissao de negociacao (spot) "
        "continua habilitada."
        if looks_like_ip
        else "Verifique a chave, o segredo e as permissoes da API."
    )
    return f"{exchange}.{operation}: acesso negado pela exchange. {detail} (erro original: {raw})"


def build_market_data_source(exchange_id: str, testnet: bool = False) -> CcxtExchange:
    """Fonte de mercado publica: nenhuma credencial e passada aqui, de proposito."""
    return CcxtExchange(exchange_id, credentials=None, testnet=testnet)


__all__ = ["ApiAccessDenied", "CcxtExchange", "Side", "build_market_data_source"]
