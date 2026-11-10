"""O ensaio da testnet e a separacao entre a chave real e a da testnet.

O ensaio em si so roda com a chave que o operador cria; aqui se prova, com a
exchange dublada, que ele (a) recusa rodar fora da testnet antes de qualquer
ordem, (b) reprova cada falha que existe para pegar e (c) mede a idempotencia
do clientOrderId em vez de supor.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_traders.config import ExchangeCredentials, Settings
from crypto_traders.domain.enums import ExchangeName, OrderStatus, Side
from crypto_traders.domain.models import OrderResult
from crypto_traders.ensaio_testnet import FALHOU, OK, aponta_para_testnet, ensaiar
from crypto_traders.exchanges import build_broker
from crypto_traders.exchanges.base import ExchangeError
from crypto_traders.exchanges.filters import FONTE_AO_VIVO, MarketFilter

TESTNET_URLS = {"public": "https://testnet.binance.vision/api/v3",
                "private": "https://testnet.binance.vision/api/v3"}
REAL_URLS = {"public": "https://api.binance.com/api/v3",
             "private": "https://api.binance.com/api/v3"}


class _Ccxt:
    def __init__(self, urls):
        self.urls = {"api": urls}

    async def load_markets(self):
        return {}


class _Ticker:
    def __init__(self, price):
        self.price = price


class ExchangeDublada:
    """Spot minimo: saldo, filtro, ticker e ordens a mercado executadas na hora."""

    def __init__(self, urls=TESTNET_URLS, aceita_repetida=False, preco_execucao=None,
                 consulta_acha=True):
        self._client = _Ccxt(urls)
        self.consulta_acha = consulta_acha
        self.resultados: dict[str, OrderResult] = {}
        self.saldos = {"USDT": Decimal(1000), "BTC": Decimal("0.5")}
        self.preco = Decimal(50000)
        self.preco_execucao = preco_execucao or self.preco
        self.aceita_repetida = aceita_repetida
        self.ordens: list = []
        self._vistos: set[str] = set()
        self.fechado = False

    async def _with_retry(self, _op, func, *a, **k):
        return await func(*a, **k)

    def _exchange_enum(self):
        return ExchangeName.BINANCE

    async def fetch_balances(self):
        return dict(self.saldos)

    def market_filter(self, symbol):
        return MarketFilter(symbol, Decimal("0.00001"), Decimal(5), Decimal("0.00001"),
                            FONTE_AO_VIVO)

    async def fetch_ticker(self, symbol):
        return _Ticker(self.preco)

    async def place_order(self, req):
        self.ordens.append(req)
        if req.client_order_id in self._vistos and not self.aceita_repetida:
            raise ExchangeError("binance.create_order: Duplicate order sent. (-2010)")
        self._vistos.add(req.client_order_id)
        if req.quantity * self.preco_execucao < 5:
            return OrderResult(order_request_id=req.id, client_order_id=req.client_order_id,
                               exchange_order_id=None, status=OrderStatus.FAILED,
                               error="Filter failure: NOTIONAL")
        sinal = 1 if req.side is Side.BUY else -1
        self.saldos["BTC"] += sinal * req.quantity
        self.saldos["USDT"] -= sinal * req.quantity * self.preco_execucao
        resultado = OrderResult(order_request_id=req.id, client_order_id=req.client_order_id,
                                exchange_order_id="1", status=OrderStatus.FILLED,
                                filled_quantity=req.quantity, average_price=self.preco_execucao)
        self.resultados[req.client_order_id] = resultado
        return resultado

    async def consultar_ordem(self, req):
        return self.resultados.get(req.client_order_id) if self.consulta_acha else None

    async def close(self):
        self.fechado = True


def _estados(relatorio):
    return {e.nome: e.estado for e in relatorio.etapas}


async def test_ensaio_completo_aprovado():
    ex = ExchangeDublada()
    relatorio = await ensaiar(Settings(_env_file=None), cliente=ex)
    estados = _estados(relatorio)
    assert relatorio.ok, relatorio.etapas
    for nome in ("url da testnet", "autenticacao", "filtro do par", "compra a mercado",
                 "consulta por clientOrderId", "clientOrderId repetido", "venda a mercado",
                 "recusa abaixo do minimo", "saldo final"):
        assert estados[nome] == OK, nome
    assert ex.saldos["BTC"] == Decimal("0.5"), "o ensaio tem de devolver a posicao"
    assert ex.fechado


async def test_cliente_fora_da_testnet_nao_envia_nenhuma_ordem():
    ex = ExchangeDublada(urls=REAL_URLS)
    relatorio = await ensaiar(Settings(_env_file=None), cliente=ex)
    assert not relatorio.ok
    assert _estados(relatorio)["url da testnet"] == FALHOU
    assert ex.ordens == []


def test_url_misturada_nao_conta_como_testnet():
    misturado = _Ccxt({**TESTNET_URLS, "sapi": "https://api.binance.com/sapi/v1"})
    assert aponta_para_testnet(misturado) is False
    assert aponta_para_testnet(_Ccxt(TESTNET_URLS)) is True


async def test_clientorderid_aceito_duas_vezes_reprova_e_a_sobra_e_vendida():
    """Duplicata aceita REPROVA o ensaio (antes era AVISO e saia "APROVADO").

    E a prova, no servidor, de que o `clientOrderId` nao protege um reenvio: a
    MARKET preenchida deixa de ser ordem aberta e o mesmo id compra de novo
    (auditoria de 09/11/2026, M1).
    """
    ex = ExchangeDublada(aceita_repetida=True)
    relatorio = await ensaiar(Settings(_env_file=None), cliente=ex)
    assert _estados(relatorio)["clientOrderId repetido"] == FALHOU
    assert not relatorio.ok
    assert ex.saldos["BTC"] == Decimal("0.5"), "as duas compras tem de ser vendidas"


async def test_consulta_que_nao_acha_a_ordem_executada_reprova():
    """Sem a consulta pelo clientOrderId, o adaptador nao tem como descobrir o
    desfecho de um envio sem resposta -- e o mecanismo que impede a compra dupla."""
    ex = ExchangeDublada(consulta_acha=False)
    relatorio = await ensaiar(Settings(_env_file=None), cliente=ex)
    assert _estados(relatorio)["consulta por clientOrderId"] == FALHOU
    assert not relatorio.ok
    assert ex.saldos["BTC"] == Decimal("0.5"), "a compra tem de ser vendida mesmo assim"


async def test_execucao_longe_do_ticker_reprova():
    ex = ExchangeDublada(preco_execucao=Decimal(55000))
    relatorio = await ensaiar(Settings(_env_file=None), cliente=ex)
    assert _estados(relatorio)["compra a mercado"] == FALHOU
    assert not relatorio.ok


async def test_sem_chave_da_testnet_nem_tenta(monkeypatch):
    """A chave REAL presente nao serve de chave da testnet."""
    s = Settings(_env_file=None).model_copy(
        update={"binance": ExchangeCredentials(api_key="real", api_secret="real")}
    )
    relatorio = await ensaiar(s)
    assert _estados(relatorio) == {"credencial": FALHOU}


class TestChavesSeparadas:
    def test_variavel_de_ambiente_da_testnet(self, monkeypatch):
        monkeypatch.setenv("BINANCE_TESTNET__API_KEY", "kt")
        monkeypatch.setenv("BINANCE_TESTNET__API_SECRET", "st")
        s = Settings(_env_file=None)
        assert s.binance_testnet.configured
        assert not s.binance.configured

    def test_testnet_nao_usa_a_chave_real(self):
        s = Settings(_env_file=None, trading_mode="testnet").model_copy(
            update={"binance": ExchangeCredentials(api_key="real", api_secret="real")}
        )
        with pytest.raises(ExchangeError, match="BINANCE_TESTNET__API_KEY"):
            build_broker(s)

    def test_live_nao_usa_a_chave_da_testnet(self):
        s = Settings(
            _env_file=None, trading_mode="live", live_trading_confirmed=True
        ).model_copy(
            update={"binance_testnet": ExchangeCredentials(api_key="t", api_secret="t")}
        )
        with pytest.raises(ExchangeError, match="BINANCE__API_KEY"):
            build_broker(s)

    def test_testnet_usa_a_chave_da_testnet(self):
        s = Settings(_env_file=None, trading_mode="testnet").model_copy(
            update={"binance_testnet": ExchangeCredentials(api_key="t", api_secret="t")}
        )
        broker = build_broker(s)
        assert broker._testnet is True
        assert broker._client.apiKey == "t"
