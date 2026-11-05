"""Ensaio automatico contra a Binance Spot Testnet: o encanamento real das ordens.

    crypto-traders ensaio-testnet [--par BTC/USDT] [--valor 20]

Nenhuma ordem deste sistema chegou ainda a um servidor da Binance. Os testes
provam a logica com a exchange dublada; este ensaio prova o que so a exchange de
verdade responde, com dinheiro ficticio:

1. a chave da testnet (`BINANCE_TESTNET__*`) autentica, e o cliente aponta para a
   URL da testnet -- conferido ANTES de qualquer ordem;
2. o catalogo e o filtro do par (passo de lote, valor minimo) vem da exchange;
3. uma compra a mercado e executada, com quantidade, preco medio e taxa lidos
   como o sistema le;
4. o MESMO `clientOrderId` reenviado (o que o retry de rede faz) e MEDIDO: se a
   exchange aceitar, uma queda de rede no meio de uma compra pode virar duas;
5. a venda a mercado -- o caminho da saida protetora -- devolve a posicao;
6. uma ordem abaixo do minimo volta como falha da ordem, e nao derruba o agente.

So usa a chave da testnet. A chave real nunca e lida aqui, e o ensaio se recusa a
rodar se o cliente nao estiver apontando para a testnet.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .config import Settings
from .domain.enums import OrderStatus, OrderType, Side
from .domain.models import OrderRequest, OrderResult
from .exchanges.base import ExchangeError

OK = "ok"
FALHOU = "falhou"
AVISO = "aviso"

#: Distancia maxima aceita entre o preco medio da execucao e o ticker lido antes.
DESVIO_MAXIMO = Decimal("0.02")


@dataclass
class Etapa:
    nome: str
    estado: str
    detalhe: str


@dataclass
class Relatorio:
    etapas: list[Etapa] = field(default_factory=list)

    def registrar(self, nome: str, estado: str, detalhe: str) -> Etapa:
        etapa = Etapa(nome, estado, detalhe)
        self.etapas.append(etapa)
        return etapa

    @property
    def ok(self) -> bool:
        return bool(self.etapas) and all(e.estado != FALHOU for e in self.etapas)


def aponta_para_testnet(cliente_ccxt: Any) -> bool:
    """True so se TODAS as URLs de API do cliente forem da testnet."""
    urls = (getattr(cliente_ccxt, "urls", None) or {}).get("api")
    valores = list(urls.values()) if isinstance(urls, dict) else [urls]
    textos = [str(v) for v in valores if v]
    return bool(textos) and all("testnet" in t for t in textos)


def _pedido(cliente: Any, par: str, lado: Side, quantidade: Decimal, preco: Decimal,
            client_order_id: str | None = None) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id or f"ensaio-{uuid.uuid4().hex[:20]}",
        signal_id=None,
        risk_event_id="ensaio-testnet",
        exchange=cliente._exchange_enum(),
        symbol=par,
        side=lado,
        order_type=OrderType.MARKET,
        quantity=quantidade,
        notional=quantidade * preco,
        strategy="ensaio-testnet",
    )


def _executada(resultado: OrderResult) -> bool:
    return resultado.status is OrderStatus.FILLED and resultado.filled_quantity > 0


async def ensaiar(
    settings: Settings,
    par: str = "BTC/USDT",
    valor: Decimal = Decimal(20),
    cliente: Any | None = None,
) -> Relatorio:
    """Roda o ensaio e devolve o relatorio. Para na primeira etapa que impede as seguintes."""
    relatorio = Relatorio()
    base, cotacao = par.split("/")

    if cliente is None:
        credencial = settings.credentials_for(settings.exchange, testnet=True)
        if not credencial.configured:
            relatorio.registrar(
                "credencial", FALHOU,
                f"{settings.exchange.upper()}_TESTNET__API_KEY e _API_SECRET ausentes no .env "
                "(crie a chave em https://testnet.binance.vision).",
            )
            return relatorio
        from .exchanges import CcxtExchange

        cliente = CcxtExchange(settings.exchange, credencial, testnet=True)

    try:
        if not aponta_para_testnet(cliente._client):
            relatorio.registrar(
                "url da testnet", FALHOU,
                "o cliente NAO aponta para a testnet; nenhuma ordem foi enviada.",
            )
            return relatorio
        relatorio.registrar("url da testnet", OK, "todas as URLs de API sao da testnet")

        # 1. Autenticacao e saldo.
        try:
            saldos = await cliente.fetch_balances()
        except Exception as exc:
            relatorio.registrar("autenticacao", FALHOU, str(exc))
            return relatorio
        saldo_cotacao = saldos.get(cotacao, Decimal(0))
        base_inicial = saldos.get(base, Decimal(0))
        if saldo_cotacao < valor * 2:
            relatorio.registrar(
                "autenticacao", FALHOU,
                f"saldo de {cotacao} na testnet ({saldo_cotacao}) menor que 2x o valor "
                f"do ensaio ({valor})",
            )
            return relatorio
        relatorio.registrar(
            "autenticacao", OK, f"saldo {cotacao}={saldo_cotacao}, {base}={base_inicial}"
        )

        # 2. Catalogo e filtro do par, vindos da exchange.
        await cliente._with_retry("load_markets", cliente._client.load_markets)
        filtro = cliente.market_filter(par)
        if filtro is None or filtro.source != "live":
            relatorio.registrar(
                "filtro do par", FALHOU, f"{par} sem filtro ao vivo na testnet ({filtro})"
            )
            return relatorio
        relatorio.registrar(
            "filtro do par", OK,
            f"passo {filtro.amount_step}, minimo {filtro.min_cost} {cotacao}",
        )

        # 3. Compra a mercado.
        preco = (await cliente.fetch_ticker(par)).price
        if preco <= 0:
            relatorio.registrar("ticker", FALHOU, f"preco {preco}")
            return relatorio
        quantidade = filtro.truncate(valor / preco)
        compra = _pedido(cliente, par, Side.BUY, quantidade, preco)
        resultado = await cliente.place_order(compra)
        if not _executada(resultado):
            relatorio.registrar(
                "compra a mercado", FALHOU, f"{resultado.status}: {resultado.error}"
            )
            return relatorio
        desvio = abs(resultado.average_price - preco) / preco if resultado.average_price else None
        relatorio.registrar(
            "compra a mercado",
            OK if desvio is not None and desvio <= DESVIO_MAXIMO else FALHOU,
            f"{resultado.filled_quantity} {base} a {resultado.average_price} "
            f"(ticker {preco}, desvio {desvio:.4%})" if desvio is not None
            else f"{resultado.filled_quantity} {base} sem preco medio na resposta",
        )
        relatorio.registrar(
            "taxa da compra", OK,
            f"{resultado.fee} {resultado.fee_currency or '(moeda nao informada)'}",
        )

        # 4. Idempotencia do clientOrderId (o retry de rede reenvia o mesmo).
        try:
            repetida = await cliente.place_order(compra)
        except ExchangeError as exc:
            repetida = None
            relatorio.registrar("clientOrderId repetido", OK, f"recusado: {exc}")
        if repetida is not None:
            if _executada(repetida):
                relatorio.registrar(
                    "clientOrderId repetido", AVISO,
                    "a exchange ACEITOU o mesmo clientOrderId de uma ordem ja executada: "
                    "uma queda de rede durante a compra, seguida do retry, pode comprar duas "
                    "vezes. Ver docs/SEGURANCA.md (idempotencia).",
                )
            else:
                relatorio.registrar(
                    "clientOrderId repetido", OK,
                    f"recusado: {repetida.status} {repetida.error}",
                )

        # 5. Venda a mercado de tudo o que o ensaio comprou (caminho da saida protetora).
        saldos = await cliente.fetch_balances()
        comprado = saldos.get(base, Decimal(0)) - base_inicial
        # A taxa da compra, quando cobrada no proprio ativo, ja saiu do saldo.
        venda_qtd = filtro.truncate(comprado)
        if venda_qtd <= 0:
            relatorio.registrar("venda a mercado", FALHOU, f"nada a vender ({comprado} {base})")
            return relatorio
        venda = await cliente.place_order(_pedido(cliente, par, Side.SELL, venda_qtd, preco))
        relatorio.registrar(
            "venda a mercado",
            OK if _executada(venda) else FALHOU,
            f"{venda.filled_quantity} {base} a {venda.average_price}"
            if _executada(venda) else f"{venda.status}: {venda.error}",
        )

        # 6. Ordem abaixo do minimo: falha da ORDEM, nao do agente.
        minuscula = filtro.amount_step
        try:
            recusa = await cliente.place_order(_pedido(cliente, par, Side.BUY, minuscula, preco))
            relatorio.registrar(
                "recusa abaixo do minimo",
                OK if recusa.status in (OrderStatus.FAILED, OrderStatus.REJECTED) else FALHOU,
                f"{recusa.status}: {recusa.error}",
            )
        except ExchangeError as exc:
            relatorio.registrar(
                "recusa abaixo do minimo", AVISO,
                f"subiu como excecao ({type(exc).__name__}), nao como resultado: {exc}",
            )

        # Saldo final do ativo: de volta ao inicial, a menos de poeira.
        final = (await cliente.fetch_balances()).get(base, Decimal(0))
        sobra = final - base_inicial
        relatorio.registrar(
            "saldo final",
            OK if abs(sobra) <= filtro.amount_step * 2 else AVISO,
            f"{base}: inicial {base_inicial}, final {final} (diferenca {sobra})",
        )
        return relatorio
    finally:
        await cliente.close()


def imprimir(relatorio: Relatorio) -> None:
    marcas = {OK: "OK    ", FALHOU: "FALHOU", AVISO: "AVISO "}
    print("\nEnsaio contra a Binance Spot Testnet\n")
    for etapa in relatorio.etapas:
        print(f"  [{marcas[etapa.estado]}] {etapa.nome}: {etapa.detalhe}")
    print(
        "\nResultado: "
        + ("APROVADO" if relatorio.ok else "REPROVADO")
        + (" (com avisos)" if any(e.estado == AVISO for e in relatorio.etapas) else "")
    )
