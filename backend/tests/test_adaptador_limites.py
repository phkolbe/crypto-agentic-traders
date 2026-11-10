"""Adaptador ccxt: limite de requisicoes, ticker sem preco e a dica de IP.

Achados low da auditoria de 09/11/2026 (segmento exchanges) que ainda existiam
na rodada 3:

* **L2 -- rate limit e ban.** 429 e 418 eram retentados com 1 s e 2 s fixos, sem
  olhar o `Retry-After`. A Binance documenta que nao recuar depois de um 429 leva
  ao ban por IP (418), e que o ban cresce para quem reincide (de 2 minutos a 3
  dias). Reenviar durante o ban e exatamente reincidir -- e o ban por IP derruba
  tambem as chamadas privadas, inclusive a venda do stop.
* **L1 -- ticker sem preco.** `fetch_ticker` devolvia preco ZERO quando faltavam
  `last`, `close` e `bid`, o contrario do que `fetch_tickers` faz (deixa o par de
  fora). Zero e lido como "o ativo nao vale nada".
* **Dica de IP.** O comentario de `_IP_HINT` dizia "o `ip` usa limite de palavra
  de proposito"; a expressao nao usava, e "multiple" mandava conferir o IP.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt
import pytest

from crypto_traders.config import ExchangeCredentials
from crypto_traders.domain.enums import ExchangeName, OrderStatus, OrderType, Side
from crypto_traders.domain.models import OrderRequest
from crypto_traders.exchanges.base import ExchangeError
from crypto_traders.exchanges.ccxt_adapter import CcxtExchange, _access_denied_message

_BAN = (
    "binance 418 I'm a teapot "
    '{"code":-1003,"msg":"Way too much request weight used; IP banned until 1767225611000."}'
)
_PESO = 'binance 429 Too Many Requests {"code":-1003,"msg":"Too much request weight used."}'


class ClienteComCabecalhos:
    """Cliente ccxt dublado: responde do roteiro e guarda o cabecalho da resposta."""

    def __init__(self, roteiro: list[tuple[Any, dict[str, str] | None]]) -> None:
        self.roteiro = list(roteiro)
        self.chamadas = 0
        self.last_response_headers: dict[str, str] | None = None

    def _proxima(self) -> Any:
        self.chamadas += 1
        resposta, cabecalhos = self.roteiro.pop(0) if len(self.roteiro) > 1 else self.roteiro[0]
        self.last_response_headers = cabecalhos
        if isinstance(resposta, BaseException):
            raise resposta
        return resposta

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        return self._proxima()

    async def fetch_ticker(self, symbol):
        return self._proxima()

    async def create_order(self, *args):
        return self._proxima()

    async def close(self) -> None:
        return None


@pytest.fixture
def esperas(monkeypatch) -> list[float]:
    registro: list[float] = []

    async def sem_dormir(segundos):
        registro.append(segundos)

    monkeypatch.setattr(asyncio, "sleep", sem_dormir)
    return registro


@pytest.fixture
async def publico():
    exchange = CcxtExchange("binance", credentials=None, max_retries=3)
    verdadeiro = exchange._client
    yield exchange
    await verdadeiro.close()


@pytest.fixture
async def autenticado():
    credencial = ExchangeCredentials(api_key="chave-de-teste", api_secret="segredo-de-teste")
    exchange = CcxtExchange("binance", credentials=credencial, max_retries=3)
    verdadeiro = exchange._client
    yield exchange
    await verdadeiro.close()


def _linha() -> list[Any]:
    return [1_767_225_600_000, 100.0, 101.0, 99.0, 100.0, 1.0]


class TestRetryAfter:
    async def test_ban_418_nao_e_retentado(self, publico, esperas):
        """Antes: 3 chamadas em 3 s durante o ban -- reincidencia que alonga o ban."""
        cliente = ClienteComCabecalhos([(ccxt.DDoSProtection(_BAN), {"Retry-After": "120"})])
        publico._client = cliente

        with pytest.raises(ExchangeError, match="120"):
            await publico.fetch_candles("BTC/USDC", "1h", 2)

        assert cliente.chamadas == 1
        assert esperas == []

    async def test_418_sem_cabecalho_tambem_para(self, publico, esperas):
        """O ban minimo da Binance e de 2 minutos: nenhum retry de 1-2 s cabe nele."""
        cliente = ClienteComCabecalhos([(ccxt.DDoSProtection(_BAN), None)])
        publico._client = cliente

        with pytest.raises(ExchangeError, match="418"):
            await publico.fetch_candles("BTC/USDC", "1h", 2)

        assert cliente.chamadas == 1

    async def test_429_espera_o_que_a_exchange_pediu(self, publico, esperas):
        cliente = ClienteComCabecalhos(
            [
                (ccxt.RateLimitExceeded(_PESO), {"Retry-After": "4"}),
                ([_linha(), _linha()], {}),
            ]
        )
        publico._client = cliente

        candles = await publico.fetch_candles("BTC/USDC", "1h", 2)

        assert len(candles) == 1
        assert esperas == [4.0], "o Retry-After manda, e nao o backoff de 1 s"

    async def test_429_com_espera_longa_nao_segura_o_chamador(self, publico, esperas):
        """Esperar minutos dentro do retry prenderia o ciclo do Market Data; desiste."""
        cliente = ClienteComCabecalhos([(ccxt.RateLimitExceeded(_PESO), {"retry-after": "90"})])
        publico._client = cliente

        with pytest.raises(ExchangeError, match="90"):
            await publico.fetch_candles("BTC/USDC", "1h", 2)

        assert cliente.chamadas == 1

    async def test_429_sem_cabecalho_segue_o_backoff(self, publico, esperas):
        """Sem o cabecalho, o comportamento de antes (1 s, 2 s) continua."""
        cliente = ClienteComCabecalhos(
            [
                (ccxt.RateLimitExceeded(_PESO), None),
                (ccxt.RateLimitExceeded(_PESO), None),
                ([_linha()], {}),
            ]
        )
        publico._client = cliente

        await publico.fetch_candles("BTC/USDC", "1h", 1)

        assert esperas == [1.0, 2.0]

    async def test_ordem_recusada_por_ban_nao_e_reenviada(self, autenticado, esperas):
        """418 e recusa na porta (nada foi criado), mas reenviar no ban alonga o ban."""
        cliente = ClienteComCabecalhos([(ccxt.DDoSProtection(_BAN), {"Retry-After": "300"})])
        autenticado._client = cliente
        pedido = OrderRequest(
            client_order_id="cat-0123456789abcdef0123",
            signal_id=None,
            risk_event_id="risco-1",
            exchange=ExchangeName.BINANCE,
            symbol="BTC/USDC",
            side=Side.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.001"),
            notional=Decimal("50"),
        )

        resultado = await autenticado.place_order(pedido)

        assert cliente.chamadas == 1
        assert resultado.status is OrderStatus.FAILED
        assert "300" in (resultado.error or "")


class TestTickerSemPreco:
    async def test_sem_last_close_bid_e_erro_e_nao_preco_zero(self, publico):
        publico._client = ClienteComCabecalhos([({"symbol": "BTC/USDC"}, {})])

        with pytest.raises(ExchangeError, match="sem preco"):
            await publico.fetch_ticker("BTC/USDC")

    async def test_com_preco_continua_igual(self, publico):
        publico._client = ClienteComCabecalhos([({"last": 50000.5}, {})])

        ticker = await publico.fetch_ticker("BTC/USDC")

        assert ticker.price == Decimal("50000.5")


class TestDicaDeIp:
    def test_ip_solto_em_outra_palavra_nao_aponta_o_ip(self):
        mensagem = _access_denied_message(
            "binance", "fetch_balance", "binance multiple signatures description invalid"
        )
        assert "IP desta maquina" not in mensagem

    @pytest.mark.parametrize(
        "erro",
        [
            'binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}',
            "binance IP not in whitelist",
            # Sem `-2015` e sem "whitelist": so o limite de palavra acha o IP. Com
            # o backspace gravado no lugar do `\b`, esta mensagem apontava a chave.
            'binance 401 {"msg":"Request from this IP address is not allowed."}',
        ],
    )
    def test_ip_de_verdade_continua_apontando(self, erro):
        assert "IP desta maquina" in _access_denied_message("binance", "create_order", erro)
