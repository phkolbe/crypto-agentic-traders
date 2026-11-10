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
from .base import (
    ApiAccessDenied,
    Broker,
    DesfechoDesconhecido,
    ExchangeError,
    InsufficientFunds,
    MarketDataSource,
    timeframe_seconds,
)
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

#: Erros de rede/limite: transitorios, valem retry com backoff -- nas LEITURAS. O
#: envio de ordem tem regra propria (`_criar_ordem`).
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

#: `-2015` ("Invalid API-key, IP, or permissions for action") e classificado pelo
#: CODIGO, nao pela classe. No ccxt 4.5.78 a classe muda no meio do processo:
#: depois da primeira chamada privada que deu certo (`hasAlreadyAuthenticated
#: Successfully`, `async_support/binance.py:12433`), o `-2015` chega como
#: `DDoSProtection` -- que esta em `_RETRYABLE`. Cenario da auditoria de
#: 09/11/2026: o Portfolio le o saldo, o IP residencial muda de madrugada, o stop
#: dispara e recebia 3 retries e um FAILED generico, sem o alerta de acesso negado.
_CODIGO_ACESSO_NEGADO = re.compile(r"(?<!\d)-2015(?!\d)")

#: Recusas na porta: a Binance devolveu 429/418 sem processar o pedido. Sao as
#: unicas falhas transitorias em que REENVIAR uma ordem e seguro -- nada entrou no
#: motor. (`InvalidNonce`, o `-1021`, tambem e recusa na porta, mas pede
#: ressincronizar o relogio antes; ver `_criar_ordem`.)
_RECUSA_NA_PORTA = (
    ccxt.RateLimitExceeded,
    ccxt.DDoSProtection,
)

#: 418 da Binance: o IP esta banido (ver `_espera_pedida`).
_HTTP_BAN = re.compile(r"(?<!\d)418(?!\d)")

#: Maior `Retry-After` que o retry espera dentro da chamada. Acima disto quem
#: chamou recebe o erro na hora, e o proximo ciclo dele e a nova tentativa.
_ESPERA_MAXIMA_NO_RETRY = 30.0

#: Espera antes de consultar o desfecho de um envio sem resposta: da ao motor da
#: exchange tempo de processar a mensagem que ainda pode estar na fila.
_ESPERA_ANTES_DA_CONSULTA = 1.0

#: Sinais de que o problema e o IP, e nao a chave em si. Na Binance, `-2015`
#: cobre "chave invalida, IP ou permissao" -- os tres casos caem na mesma
#: mensagem, e num setup residencial a causa mais provavel e o IP.
#:
#: `-2014` ("API-key format invalid") NAO entra aqui de proposito: e um problema
#: da chave, e apontar o IP nesse caso mandaria o operador investigar o lugar
#: errado.
#:
#: O `\bip\b` usa limite de palavra de proposito: "ip" como substring solta
#: casaria com "multiple", "description" e praticamente qualquer mensagem.
#:
#: Ate a rodada 3 os dois `\b` estavam gravados como o CARACTERE de controle
#: backspace (0x08) dentro da string crua: a expressao procurava "<BS>ip<BS>", que
#: nao aparece em mensagem nenhuma, e um "IP address not allowed" sem `-2015` nem
#: "whitelist" mandava conferir a chave em vez do IP.
_IP_HINT = re.compile(r"-2015|whitelist|\bip\b", re.IGNORECASE)


#: Maximo de klines por chamada na Binance spot (`GET /api/v3/klines`). Acima
#: disto o ccxt corta o `limit` sem avisar; `fetch_candles` pagina.
_CANDLES_POR_PAGINA = 1000


def _agora_ms() -> int:
    """Instante atual em ms (funcao propria para o teste fixar o relogio)."""
    return int(datetime.now(UTC).timestamp() * 1000)


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


def _carimbo(raw: dict[str, Any]) -> dict[str, datetime]:
    """O instante do preco segundo a EXCHANGE (`timestamp` do ccxt, em ms).

    O Market Data mede o frescor do preco corrente por este carimbo: a hora de
    RECEBIMENTO fazia um preco velho, entregue atrasado, parecer recente. Sem o
    campo (ou com um valor no futuro, relogio errado de algum lado), volta ao
    padrao do modelo -- a hora local --, e o Market Data ja trata carimbo no
    futuro como agora.
    """
    ms = raw.get("timestamp")
    if not isinstance(ms, (int, float)) or ms <= 0:
        return {}
    return {"timestamp": datetime.fromtimestamp(ms / 1000, tz=UTC)}


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
            # Chamada assinada leva o relogio local; com mais de `recvWindow` (10 s)
            # de diferenca a Binance devolve `-1021`. O host Windows volta da
            # suspensao com a VM do Docker atrasada (SEGURANCA §17.3), e ai toda
            # chamada assinada falhava, inclusive a saida protetora. Ligado so com
            # credencial: a fonte publica nao assina nada, e o `load_markets` dela
            # nao precisa de uma chamada a mais ao `/time`.
            config["options"]["adjustForTimeDifference"] = True
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

        So para LEITURA (candles, tickers, saldo, consulta de ordem): repetir uma
        leitura nao muda nada na exchange. O envio de ordem NAO passa por aqui --
        tem regra propria em `_criar_ordem`, porque reenviar uma MARKET que ja
        preencheu compra de novo.

        Erros de negocio (saldo insuficiente, ordem invalida) sobem na hora:
        repetir nao muda o resultado e so atrasa o diagnostico.
        """
        delay = 1.0
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except ccxt.BaseError as exc:
                self._levantar_se_definitivo(operation, exc)
                if not isinstance(exc, _RETRYABLE):
                    raise ExchangeError(f"{self.name}.{operation}: {exc}") from exc
                if isinstance(exc, ccxt.InvalidNonce):
                    await self._ressincronizar_relogio(operation)
                espera = self._espera_pedida(operation, exc)
                last_error = exc
                log.warning(
                    "exchange.retry",
                    exchange=self.name,
                    operation=operation,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(max(delay, espera or 0.0))
                    delay *= 2
        raise ExchangeError(
            f"{self.name}.{operation} falhou apos {self._max_retries} tentativas: {last_error}"
        )

    def _espera_pedida(self, operation: str, exc: Exception) -> float | None:
        """Quanto a exchange mandou esperar num 429/418; levanta se nao vale retentar.

        O backoff fixo de 1 s e 2 s ignorava o `Retry-After`, e a Binance documenta
        que nao recuar depois de um 429 leva ao ban do IP (418), que cresce para
        quem reincide -- de 2 minutos a 3 dias. Reenviar durante o ban e
        reincidir, e o ban por IP derruba tambem as chamadas privadas, inclusive
        a venda do stop (auditoria de 09/11/2026, exchanges L2). Entao:

        * **418** (ban), com ou sem cabecalho: nao retenta. Nenhum retry de
          segundos cabe num ban de minutos;
        * **429 com espera acima de `_ESPERA_MAXIMA_NO_RETRY`**: nao retenta.
          Dormir minutos aqui prenderia o ciclo de quem chamou (Market Data,
          Portfolio, a ordem), e o proximo ciclo ja e a nova tentativa;
        * **429 com espera curta**: espera o que foi pedido (no minimo o backoff);
        * **sem cabecalho**: `None`, e vale o backoff de antes.
        """
        if not isinstance(exc, _RECUSA_NA_PORTA):
            return None
        cabecalhos = getattr(self._client, "last_response_headers", None) or {}
        bruto = next(
            (valor for chave, valor in cabecalhos.items() if str(chave).lower() == "retry-after"),
            None,
        )
        try:
            espera = float(bruto) if bruto is not None else None
        except (TypeError, ValueError):
            espera = None
        banido = isinstance(exc, ccxt.DDoSProtection) and bool(_HTTP_BAN.search(str(exc)))
        if banido or (espera is not None and espera > _ESPERA_MAXIMA_NO_RETRY):
            pedido = f"{espera:.0f}s" if espera is not None else "nao informada"
            raise ExchangeError(
                f"{self.name}.{operation}: limite de requisicoes da exchange "
                f"(HTTP {'418, IP banido' if banido else '429'}; espera pedida {pedido}). "
                "Nao retentado: reenviar dentro da espera e o que faz a Binance banir o "
                f"IP e alongar o ban. Erro original: {exc}"
            ) from exc
        return espera

    def _levantar_se_definitivo(self, operation: str, exc: Exception) -> None:
        """Traduz as respostas que nenhum retry resolve, qualquer que seja a operacao.

        O acesso negado vem PRIMEIRO e e decidido pelo codigo `-2015` alem da
        classe: ver `_CODIGO_ACESSO_NEGADO`.
        """
        if isinstance(exc, _ACCESS_DENIED) or _CODIGO_ACESSO_NEGADO.search(str(exc)):
            raise ApiAccessDenied(
                _access_denied_message(self.name, operation, str(exc)),
                exchange=self.name,
                operation=operation,
            ) from exc
        if isinstance(exc, ccxt.InsufficientFunds):
            raise InsufficientFunds(str(exc)) from exc

    async def _ressincronizar_relogio(self, operation: str) -> None:
        """Rele a diferenca de relogio depois de um `-1021`.

        O `adjustForTimeDifference` do ccxt so mede a diferenca no `load_markets`,
        uma vez por processo. A deriva que importa nasce DEPOIS (o host suspende e
        volta), entao sem isto o retry repetia o mesmo timestamp errado 3 vezes.
        Falhar aqui so registra: o retry seguinte decide.
        """
        sincronizar = getattr(self._client, "load_time_difference", None)
        if sincronizar is None:
            return
        try:
            diferenca = await sincronizar()
        except Exception as exc:
            log.warning(
                "exchange.relogio_nao_ressincronizado",
                exchange=self.name,
                operation=operation,
                error=str(exc),
            )
            return
        log.warning(
            "exchange.relogio_ressincronizado",
            exchange=self.name,
            operation=operation,
            diferenca_ms=diferenca,
        )

    async def _criar_ordem(self, request: OrderRequest, *args: Any) -> Any:
        """`create_order` sem reenvio as cegas (auditoria de 09/11/2026, P0 #1 e #2).

        O `clientOrderId` NAO protege o reenvio: na Binance ele e unico so entre
        ordens ABERTAS, e uma MARKET preenche na hora. O `_with_retry` reenviava
        com o mesmo id depois de um timeout, e a segunda compra preenchia de novo
        -- 40% da carteira em vez de 20%, com um trade so no banco.

        Por isso cada falha transitoria cai em um de dois grupos:

        * **recusa na porta** (429/418 sem `-2015`, e `-1021` depois de
          ressincronizar o relogio): a Binance respondeu sem processar o pedido,
          nada entrou no motor, e reenviar e seguro -- mas nao dentro do ban: o
          418, e o 429 com `Retry-After` longo, nao sao reenviados
          (`_espera_pedida`);
        * **desfecho desconhecido** (timeout, `-1007`, `-1006`, `-1000`/`-1001`,
          HTTP 5xx, conexao caida, resposta ilegivel -- tudo `OperationFailed` no
          ccxt 4.5): consulta a ordem pelo `clientOrderId`. Achou, devolve o
          estado real. Nao achou ou nao conseguiu consultar, levanta
          `DesfechoDesconhecido` -- e NAO reenvia. Um `-2013` logo depois de um
          "execution status unknown" nao prova que a ordem nao vai executar (a
          mensagem pode estar na fila do motor), e o custo e assimetrico: nao
          reenviar custa um alerta de reconciliacao; reenviar errado custa uma
          compra dobrada.
        """
        delay = 1.0
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await self._client.create_order(*args)
            except ccxt.BaseError as exc:
                self._levantar_se_definitivo("create_order", exc)
                espera = None
                if isinstance(exc, ccxt.InvalidNonce):
                    # `-1021`: timestamp recusado na validacao, antes do motor.
                    await self._ressincronizar_relogio("create_order")
                elif isinstance(exc, _RECUSA_NA_PORTA):
                    # Seguro reenviar, mas nao dentro do ban (`_espera_pedida`).
                    espera = self._espera_pedida("create_order", exc)
                elif isinstance(exc, ccxt.OperationFailed):
                    return await self._descobrir_desfecho(request, exc)
                else:
                    # Recusa explicita da exchange (4xx com codigo): definitiva.
                    raise ExchangeError(f"{self.name}.create_order: {exc}") from exc
                last_error = exc
                log.warning(
                    "exchange.retry",
                    exchange=self.name,
                    operation="create_order",
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(max(delay, espera or 0.0))
                    delay *= 2
        # Todas as tentativas foram recusadas NA PORTA: nenhuma ordem foi criada, e
        # FAILED aqui e verdade.
        raise ExchangeError(
            f"{self.name}.create_order recusado {self._max_retries} vezes antes de chegar "
            f"ao motor (nenhuma ordem criada): {last_error}"
        )

    async def _descobrir_desfecho(self, request: OrderRequest, erro: Exception) -> Any:
        """Consulta pelo `clientOrderId` o envio que ficou sem resposta."""
        cid = request.client_order_id
        log.warning(
            "exchange.desfecho_incerto",
            exchange=self.name,
            client_order_id=cid,
            error=str(erro),
        )
        await asyncio.sleep(_ESPERA_ANTES_DA_CONSULTA)
        try:
            raw = await self._buscar_ordem(request)
        except Exception as exc:
            # Inclusive acesso negado na consulta: o desfecho do ENVIO continua
            # desconhecido, e e isso que precisa chegar ao ExecutionAgent.
            raise DesfechoDesconhecido(
                f"{self.name}.create_order sem resposta ({erro}); a consulta pelo "
                f"clientOrderId {cid} tambem falhou ({exc}). Nao reenviada."
            ) from erro
        if raw is None:
            raise DesfechoDesconhecido(
                f"{self.name}.create_order sem resposta ({erro}); consulta pelo "
                f"clientOrderId {cid}: ordem nao encontrada. Nao reenviada: logo depois "
                "de um desfecho incerto, nao achar nao prova que ela nao vai executar."
            ) from erro
        log.warning(
            "exchange.desfecho_descoberto",
            exchange=self.name,
            client_order_id=cid,
            status=raw.get("status"),
            filled=raw.get("filled"),
        )
        return raw

    async def _buscar_ordem(self, request: OrderRequest) -> Any:
        """A ordem pelo `clientOrderId` (`origClientOrderId` na Binance), ou None.

        `-2013` ("Order does not exist") vira None; qualquer outra falha sobe.
        """

        async def consultar() -> Any:
            try:
                return await self._client.fetch_order(
                    None, request.symbol, {"origClientOrderId": request.client_order_id}
                )
            except ccxt.OrderNotFound:
                return None

        return await self._with_retry("fetch_order", consultar)

    async def consultar_ordem(self, request: OrderRequest) -> OrderResult | None:
        """O estado atual da ordem na exchange, pelo `clientOrderId`; None se nao existe.

        E o mecanismo de que o envio depende para nao reenviar as cegas; existe
        publico para que o ensaio da testnet o prove contra o servidor de verdade.
        """
        raw = await self._buscar_ordem(request)
        return None if raw is None else self._resultado(request, raw)

    # ------------------------------------------------------------------
    # MarketDataSource
    # ------------------------------------------------------------------
    async def fetch_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        """Os `limit` candles mais recentes, o ultimo ainda em formacao.

        Acima de `_CANDLES_POR_PAGINA` a busca e paginada (`_paginar_ohlcv`): a
        Binance devolve no maximo 1000 klines por chamada e o ccxt corta o
        `limit` em silencio. Ate a rodada 3 isto era uma chamada so, e os 90 dias
        do backtest em 1h (2160 candles) chegavam como 1000 -- 41 dias medidos
        como se fossem 90.
        """
        if limit <= _CANDLES_POR_PAGINA:
            raw = await self._with_retry(
                "fetch_ohlcv", self._client.fetch_ohlcv, symbol, timeframe, None, limit
            )
        else:
            raw = await self._paginar_ohlcv(symbol, timeframe, limit)
        exchange_name = self._exchange_enum()
        linhas = _linhas_ohlcv_confiaveis(self.name, symbol, raw)[-limit:]
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

    async def _paginar_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Any]:
        """Linhas cruas da janela dos ultimos `limit` candles, pagina a pagina.

        O cursor anda pela ABERTURA da ultima linha recebida mais 1 ms, e nao por
        "mais um timeframe": em `1M` o mes tem 28 a 31 dias, e somar a duracao
        nominal pulava fevereiro. Com "> ultima abertura" nada se repete e nada
        se pula, mesmo com buraco na exchange (manutencao sem klines) ou pagina
        que volta para tras -- a desduplicacao final (`_linhas_ohlcv_confiaveis`)
        cobre o resto. Para quando chega ao candle em formacao, quando a pagina
        vem vazia, quando o cursor nao avanca, ou no teto de paginas: uma exchange
        que ignora o `since` nao pode virar laco infinito.
        """
        passo_ms = timeframe_seconds(timeframe) * 1000
        agora = _agora_ms()
        em_formacao = agora - agora % passo_ms
        inicio = desde = em_formacao - (limit - 1) * passo_ms
        maximo_de_paginas = limit // _CANDLES_POR_PAGINA + 3
        linhas: list[Any] = []
        for _ in range(maximo_de_paginas):
            pagina = await self._with_retry(
                "fetch_ohlcv",
                self._client.fetch_ohlcv,
                symbol,
                timeframe,
                desde,
                _CANDLES_POR_PAGINA,
            )
            com_abertura = [
                row
                for row in pagina or []
                if isinstance(row, (list, tuple)) and row and row[0] is not None
            ]
            if not com_abertura:
                break
            # So o que cai na janela pedida: uma exchange que ignora o cursor e
            # devolve o comeco do historico nao pode virar "os ultimos candles".
            linhas.extend(row for row in com_abertura if int(row[0]) >= inicio)
            ultima = max(int(row[0]) for row in com_abertura)
            if ultima >= em_formacao or ultima < desde:
                break
            desde = ultima + 1
        return linhas

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
        if price is None:
            # Como em `fetch_tickers`: sem preco na resposta nao existe ticker. Antes
            # saia preco ZERO, que e "o ativo nao vale nada" -- so o Market Data
            # recusar preco <= 0 impedia o stop de ler isso (exchanges L1).
            raise ExchangeError(f"{self.name}.fetch_ticker: resposta sem preco para {symbol}")
        return Ticker(
            exchange=self._exchange_enum(), symbol=symbol, price=_dec(price),
            **_carimbo(raw),
        )

    async def fetch_tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        """Preco corrente de varios pares numa chamada so (o ciclo do Market Data).

        Par sem preco na resposta fica de fora, e nao com zero: zero seria lido
        como "o ativo nao vale nada" e dispararia stop.
        """
        raw = await self._with_retry("fetch_tickers", self._client.fetch_tickers, symbols)
        tickers: dict[str, Ticker] = {}
        for symbol in symbols:
            item = raw.get(symbol) or {}
            price = item.get("last") or item.get("close") or item.get("bid")
            if price is None:
                continue
            tickers[symbol] = Ticker(
                exchange=self._exchange_enum(), symbol=symbol, price=_dec(price),
                **_carimbo(item),
            )
        return tickers

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
            raw = await self._criar_ordem(
                request,
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
        except InsufficientFunds:
            # Mesmo motivo do bloco acima, e o mesmo cuidado com a ORDEM destes
            # `except`: `InsufficientFunds` herda de `ExchangeError`, entao sem
            # este bloco vindo ANTES ela era engolida pelo generico e voltava
            # como FAILED. O `except InsufficientFunds` do ExecutionAgent --
            # escrito para dizer "a exchange respondeu, e a resposta foi nao" --
            # era codigo morto com os dois brokers que a producao usa, e a mesma
            # recusa produzia desfecho diferente conforme o broker: o
            # `PaperBroker` devolvia REJECTED e este devolvia FAILED.
            #
            # A diferenca gasta dinheiro pelo cooldown: `last_order_time`
            # (repositories.py:249) exclui apenas REJECTED, entao uma recusa que
            # NUNCA chegou ao mercado queimava 900s do par. Nao ha risco de laco
            # apertado em retentar: uma ordem nova exige um sinal novo, que exige
            # um candle FECHADO -- em 1d, uma vez por dia por par.
            raise
        except DesfechoDesconhecido:
            # Tambem vem ANTES do generico: sem resposta e sem achar a ordem, o
            # FAILED afirmaria que nada aconteceu. O ExecutionAgent grava PENDING
            # e alerta `order_outcome_unknown`.
            raise
        except ExchangeError as exc:
            return OrderResult(
                order_request_id=request.id,
                client_order_id=request.client_order_id,
                exchange_order_id=None,
                status=OrderStatus.FAILED,
                error=str(exc),
            )

        return self._resultado(request, raw)

    def _resultado(self, request: OrderRequest, raw: dict[str, Any]) -> OrderResult:
        """`OrderResult` a partir da ordem no formato do ccxt (envio ou consulta)."""
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
