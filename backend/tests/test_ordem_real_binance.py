"""A ordem real: adaptador ccxt de verdade -> ExecutionAgent, contra uma Binance dublada.

Auditoria de 09/11/2026 (P0 #1, #2 e #3). Toda a idempotencia do envio estava
provada contra o `PaperBroker`, que guarda todo `clientOrderId` para sempre. A
Binance nao da essa garantia: segundo a doc do `newClientOrderId`, o id so e unico
entre ordens ABERTAS. Uma MARKET preenche na hora e deixa de estar aberta, entao um
reenvio com o mesmo id preenche DE NOVO.

O dublê aqui substitui so a borda da rede (`CcxtExchange._client`), e modela a
Binance no que importa ao envio:

* aceita a ordem e a resposta se perde (`RequestTimeout`, `-1007`);
* deduplica `clientOrderId` so entre ordens abertas (MARKET preenchida nao conta);
* `-1006` "Execution status unknown" (`OperationFailed` no ccxt 4.5.78);
* `-2015` chega como `DDoSProtection` depois da primeira chamada privada que deu
  certo (`async_support/binance.py:12433`, `hasAlreadyAuthenticatedSuccessfully`);
* `-1021` (relogio atrasado) ate alguem chamar `load_time_difference`;
* consulta por `origClientOrderId` (`fetch_order`), com `-2013` quando nao existe.

O que se prova e a cadeia inteira -- `place_order` real, classificacao real,
`ExecutionAgent` real, banco e alertas --, nao o adaptador isolado.
"""

from __future__ import annotations

import itertools
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt
import pytest

from crypto_traders.agents.execution import ExecutionAgent
from crypto_traders.bus import InMemoryEventBus, Topics
from crypto_traders.config import ExchangeCredentials
from crypto_traders.db.repositories import OrderRepository, TradeRepository
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import ExchangeName, OrderStatus, OrderType, Side
from crypto_traders.domain.models import OrderRequest
from crypto_traders.exchanges.base import ApiAccessDenied, DesfechoDesconhecido
from crypto_traders.exchanges.ccxt_adapter import CcxtExchange

_ERRO_2015 = 'binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}'

# Acoes do roteiro de cada `create_order`.
EXECUTA = "executa"
EXECUTA_E_PERDE_RESPOSTA = "executa e a resposta se perde"
NAO_CHEGA = "o pedido nao chega ao motor"
STATUS_1006_EXECUTOU = "-1006 depois de executar"
STATUS_1006_NAO_EXECUTOU = "-1006 sem executar"
LIMITE_DE_PESO = "429"
MERCADO_FECHADO = "-2010 Market is closed"


class BinanceDublada:
    """Cliente ccxt dublado com a semantica da Binance spot no envio de ordem."""

    def __init__(self, roteiro: list[str] | None = None, consulta_falha: bool = False):
        self.roteiro = list(roteiro or [])
        self.consulta_falha = consulta_falha
        self.ordens: list[dict[str, Any]] = []
        """Ordens que existem no motor da exchange (o que de fato gastou dinheiro)."""
        self.envios = 0
        self.consultas = 0
        self.ressincronizacoes = 0
        self.relogio_atrasado = False
        self.ip_fora_da_whitelist = False
        self.options: dict[str, Any] = {"hasAlreadyAuthenticatedSuccessfully": False}
        self.sandbox = False
        self._ids = itertools.count(1)

    def set_sandbox_mode(self, ligado: bool) -> None:
        self.sandbox = ligado

    def _assinada(self) -> None:
        """Checagens da Binance antes de qualquer chamada privada."""
        if self.relogio_atrasado:
            raise ccxt.InvalidNonce(
                'binance {"code":-1021,"msg":"Timestamp for this request is outside of '
                'the recvWindow."}'
            )
        if self.ip_fora_da_whitelist:
            # O ccxt 4.5.78 troca a classe depois da 1a chamada privada que deu certo.
            if self.options["hasAlreadyAuthenticatedSuccessfully"]:
                raise ccxt.DDoSProtection(_ERRO_2015)
            raise ccxt.AuthenticationError(_ERRO_2015)

    def _cria(self, symbol, type_, side, amount, params) -> dict[str, Any]:
        cid = params.get("clientOrderId")
        if any(o["clientOrderId"] == cid and o["status"] == "open" for o in self.ordens):
            raise ccxt.InvalidOrder('binance {"code":-2010,"msg":"Duplicate order sent."}')
        ordem = {
            "id": str(next(self._ids)),
            "clientOrderId": cid,
            "symbol": symbol,
            "type": type_,
            "side": side,
            "amount": amount,
            # MARKET preenche na hora: deixa de ser ordem aberta, e a deduplicacao
            # da Binance para de valer para este id.
            "status": "closed",
            "filled": amount,
            "average": 50000.0,
            "fee": {"cost": 0.05, "currency": "USDC"},
            "info": {"origem": "dublê"},
        }
        self.ordens.append(ordem)
        return dict(ordem)

    async def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.envios += 1
        self._assinada()
        acao = self.roteiro.pop(0) if self.roteiro else EXECUTA
        params = params or {}
        if acao == EXECUTA:
            resposta = self._cria(symbol, type_, side, amount, params)
            self.options["hasAlreadyAuthenticatedSuccessfully"] = True
            return resposta
        if acao == EXECUTA_E_PERDE_RESPOSTA:
            self._cria(symbol, type_, side, amount, params)
            raise ccxt.RequestTimeout("binance POST https://api.binance.com/api/v3/order 10000ms")
        if acao == NAO_CHEGA:
            raise ccxt.RequestTimeout("binance POST https://api.binance.com/api/v3/order 10000ms")
        if acao in (STATUS_1006_EXECUTOU, STATUS_1006_NAO_EXECUTOU):
            if acao == STATUS_1006_EXECUTOU:
                self._cria(symbol, type_, side, amount, params)
            raise ccxt.OperationFailed(
                'binance {"code":-1006,"msg":"An unexpected response was received from the '
                'message bus. Execution status unknown."}'
            )
        if acao == LIMITE_DE_PESO:
            raise ccxt.RateLimitExceeded('binance {"code":-1003,"msg":"Too many requests."}')
        if acao == MERCADO_FECHADO:
            raise ccxt.InvalidOrder('binance {"code":-2010,"msg":"Market is closed."}')
        raise AssertionError(f"acao desconhecida no roteiro: {acao}")

    async def fetch_order(self, id_, symbol=None, params=None):
        self.consultas += 1
        self._assinada()
        if self.consulta_falha:
            raise ccxt.RequestTimeout("binance GET https://api.binance.com/api/v3/order 10000ms")
        cid = (params or {}).get("origClientOrderId")
        achadas = [o for o in self.ordens if o["clientOrderId"] == cid]
        if not achadas:
            raise ccxt.OrderNotFound('binance {"code":-2013,"msg":"Order does not exist."}')
        return dict(achadas[-1])

    async def fetch_balance(self):
        self._assinada()
        self.options["hasAlreadyAuthenticatedSuccessfully"] = True
        return {"total": {"USDC": 1000.0}}

    async def load_time_difference(self, params=None):
        self.ressincronizacoes += 1
        self.relogio_atrasado = False
        return 12_000

    async def close(self) -> None:
        return None


class BusEspiao(InMemoryEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.publicados: list[tuple[str, Any]] = []

    async def publish(self, topic: str, payload: Any) -> None:
        self.publicados.append((topic, payload))
        await super().publish(topic, payload)

    def alertas(self, tipo: str) -> list[dict]:
        return [p for t, p in self.publicados if t == Topics.ALERTS and p.get("type") == tipo]


@pytest.fixture(autouse=True)
def sem_esperar(monkeypatch):
    """O backoff e a espera antes da consulta nao precisam de tempo real aqui."""

    async def nada(_segundos):
        return None

    monkeypatch.setattr("crypto_traders.exchanges.ccxt_adapter.asyncio.sleep", nada)


@asynccontextmanager
async def _adaptador(binance: BinanceDublada):
    adaptador = CcxtExchange(
        "binance",
        ExchangeCredentials(api_key="chave-de-teste", api_secret="segredo-de-teste"),
        testnet=True,
        max_retries=3,
    )
    real = adaptador._client
    adaptador._client = binance
    try:
        yield adaptador
    finally:
        await real.close()


def _compra(cid: str = "cat-real-1") -> OrderRequest:
    return OrderRequest(
        client_order_id=cid,
        signal_id=None,
        risk_event_id="risco-1",
        exchange=ExchangeName.BINANCE,
        symbol="BTC/USDC",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        notional=Decimal("50"),
        stop_loss=Decimal("48000"),
        strategy="teste",
    )


async def _executar(settings, binance: BinanceDublada, pedido: OrderRequest | None = None):
    bus = BusEspiao()
    async with _adaptador(binance) as adaptador:
        agente = ExecutionAgent(bus, adaptador, settings)
        resultado = await agente._execute(pedido or _compra())
    async with session_scope(settings) as session:
        ordens = await OrderRepository(session).list()
        trades = await TradeRepository(session).list()
    return resultado, bus, ordens, trades


class TestTimeoutNaoCompraDuasVezes:
    """P0 #1: o retry de `create_order` nao pode reenviar as cegas."""

    async def test_timeout_depois_de_executar_devolve_a_ordem_real_sem_reenviar(
        self, settings
    ):
        """A Binance executa, a resposta se perde: o adaptador CONSULTA, nao reenvia.

        Antes: o `_with_retry` reenviava com o mesmo `clientOrderId`, a MARKET ja
        preenchida nao estava mais aberta, e a segunda compra preenchia -- 0,002 BTC
        na exchange, um trade de 0,001 no banco.
        """
        binance = BinanceDublada([EXECUTA_E_PERDE_RESPOSTA])

        resultado, bus, ordens, trades = await _executar(settings, binance)

        assert binance.envios == 1, "o create_order foi reenviado depois do timeout"
        assert len(binance.ordens) == 1, "a exchange executou a compra duas vezes"
        assert binance.consultas >= 1, "o desfecho nao foi consultado pelo clientOrderId"
        assert resultado is not None and resultado.status is OrderStatus.FILLED
        assert resultado.filled_quantity == Decimal("0.001")
        assert resultado.exchange_order_id == binance.ordens[0]["id"]
        assert len(trades) == 1 and Decimal(str(trades[0].quantity)) == Decimal("0.001")
        assert ordens[0].status == str(OrderStatus.FILLED)
        assert not bus.alertas("order_outcome_unknown")

    async def test_minus_1006_com_a_ordem_executada_devolve_o_estado_real(self, settings):
        binance = BinanceDublada([STATUS_1006_EXECUTOU])

        resultado, bus, _ordens, trades = await _executar(settings, binance)

        assert binance.envios == 1
        assert len(binance.ordens) == 1
        assert resultado is not None and resultado.status is OrderStatus.FILLED
        assert len(trades) == 1
        assert not bus.alertas("order_outcome_unknown")

    async def test_limite_de_peso_e_reenviado_porque_a_binance_nao_processou(
        self, settings
    ):
        """O caso em que reenviar E seguro: 429 e recusa na porta, nada foi criado."""
        binance = BinanceDublada([LIMITE_DE_PESO])

        resultado, _bus, _ordens, trades = await _executar(settings, binance)

        assert binance.envios == 2
        assert len(binance.ordens) == 1
        assert resultado is not None and resultado.status is OrderStatus.FILLED
        assert len(trades) == 1


class TestDesfechoDesconhecidoNuncaViraFalha:
    """P0 #2: sem confirmacao, o desfecho e DESCONHECIDO -- PENDING + alerta."""

    async def test_timeout_e_consulta_sem_a_ordem_nao_reenvia_e_fica_pending(
        self, settings
    ):
        """Nao achou a ordem logo depois de um "status unknown" nao prova que ela
        nao vai executar: o adaptador nao reenvia e devolve desconhecido.

        Antes: retentava e, se a Binance processasse a primeira mais tarde, comprava
        duas vezes; ou esgotava e gravava FAILED.
        """
        binance = BinanceDublada([NAO_CHEGA])

        resultado, bus, ordens, trades = await _executar(settings, binance)

        assert binance.envios == 1, "reenvio as cegas depois de desfecho desconhecido"
        assert resultado is not None and resultado.status is OrderStatus.PENDING
        assert ordens[0].status == str(OrderStatus.PENDING)
        assert trades == []
        assert bus.alertas("order_outcome_unknown")
        assert "nao encontrada" in (resultado.error or "")

    async def test_minus_1006_sem_a_ordem_fica_pending_e_nao_failed(self, settings):
        """`-1006` e `OperationFailed` no ccxt 4.5.78: antes caia no `BaseError` e
        virava FAILED definitivo, sem alerta de reconciliacao."""
        binance = BinanceDublada([STATUS_1006_NAO_EXECUTOU])

        resultado, bus, _ordens, _trades = await _executar(settings, binance)

        assert binance.envios == 1
        assert resultado is not None and resultado.status is OrderStatus.PENDING
        assert bus.alertas("order_outcome_unknown")

    async def test_timeout_com_a_consulta_tambem_falhando_fica_pending(self, settings):
        """O cenario do relatorio: a ordem executou e nenhuma resposta volta.

        Antes: 3 envios, FAILED "falhou apos 3 tentativas", filled 0 -- sem trade,
        sem preco medio, sem stop, e a tela dizendo "falhou".
        """
        binance = BinanceDublada([EXECUTA_E_PERDE_RESPOSTA], consulta_falha=True)

        resultado, bus, ordens, trades = await _executar(settings, binance)

        assert binance.envios == 1
        assert len(binance.ordens) == 1
        assert resultado is not None and resultado.status is OrderStatus.PENDING
        assert resultado.status is not OrderStatus.FAILED
        assert ordens[0].status == str(OrderStatus.PENDING)
        assert trades == []
        assert bus.alertas("order_outcome_unknown")

    async def test_o_adaptador_levanta_desfecho_desconhecido(self):
        """Contrato do adaptador: nao devolve OrderResult FAILED quando nao sabe."""
        binance = BinanceDublada([NAO_CHEGA])
        async with _adaptador(binance) as adaptador:
            with pytest.raises(DesfechoDesconhecido):
                await adaptador.place_order(_compra())


class TestAcessoNegadoQualquerQueSejaAClasse:
    """P0 #3: `-2015` vira `ApiAccessDenied` mesmo chegando como `DDoSProtection`."""

    async def test_minus_2015_depois_da_primeira_chamada_alerta_acesso_negado(
        self, settings
    ):
        """O IP muda depois de o processo ja ter falado com a exchange.

        Antes: `DDoSProtection` estava em `_RETRYABLE` -- 3 envios, FAILED generico
        "falhou apos 3 tentativas" e nenhum alerta `api_access_denied`.
        """
        binance = BinanceDublada()
        await binance.fetch_balance()  # o Portfolio ja leu o saldo nesta instancia
        binance.ip_fora_da_whitelist = True

        resultado, bus, _ordens, _trades = await _executar(settings, binance)

        assert binance.envios == 1, "acesso negado nao se resolve retentando"
        assert resultado is not None and resultado.status is OrderStatus.FAILED
        assert bus.alertas("api_access_denied")

    async def test_minus_2015_na_leitura_de_saldo_tambem_e_acesso_negado(self):
        binance = BinanceDublada()
        await binance.fetch_balance()
        binance.ip_fora_da_whitelist = True
        async with _adaptador(binance) as adaptador:
            with pytest.raises(ApiAccessDenied, match="whitelist"):
                await adaptador.fetch_balances()


class TestRecusaExplicitaNaoEDesconhecido:
    """`-2010` e o NEW_ORDER_REJECTED generico da Binance, nao so "duplicate"."""

    async def test_mercado_fechado_e_rejected_sem_alerta_de_reconciliacao(self, settings):
        """Antes: `_ORDEM_JA_EXISTE` casava `-2010` e marcava PENDING "a ordem FOI
        criada", com alerta de reconciliacao falso para uma ordem que nunca existiu."""
        binance = BinanceDublada([MERCADO_FECHADO])

        resultado, bus, _ordens, trades = await _executar(settings, binance)

        assert binance.envios == 1
        assert resultado is not None and resultado.status is OrderStatus.REJECTED
        assert trades == []
        assert not bus.alertas("order_outcome_unknown")


class TestRelogio:
    """`-1021` depois de suspender o notebook: ressincroniza em vez de falhar."""

    def test_o_ajuste_de_relogio_do_ccxt_esta_ligado(self):
        adaptador = CcxtExchange(
            "binance",
            ExchangeCredentials(api_key="chave-de-teste", api_secret="segredo-de-teste"),
            testnet=True,
        )
        assert adaptador._client.options["adjustForTimeDifference"] is True

    async def test_relogio_atrasado_ressincroniza_e_a_ordem_sai_uma_vez(self, settings):
        """`-1021` e recusa na validacao do timestamp: nada entrou no motor, entao
        reenviar depois de ressincronizar e seguro.

        Antes: o `InvalidNonce` era retentado 3x sem ressincronizar e virava FAILED.
        """
        binance = BinanceDublada()
        binance.relogio_atrasado = True

        resultado, _bus, _ordens, trades = await _executar(settings, binance)

        assert binance.ressincronizacoes == 1
        assert binance.envios == 2
        assert len(binance.ordens) == 1
        assert resultado is not None and resultado.status is OrderStatus.FILLED
        assert len(trades) == 1
