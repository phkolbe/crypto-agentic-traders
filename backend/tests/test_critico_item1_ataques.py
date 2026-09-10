"""Ataques do CRITICO contra o item 1 (Market Data Agent).

Cada teste aqui nasce VERMELHO contra o codigo atual e descreve uma brecha
aberta. Nao e opiniao estetica: cada um mostra numero de mercado falso chegando
a `latest_prices` (o preco que o Risk e o Portfolio usam para avaliar posicao e
disparar stop) ou candle publicado duas vezes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from helpers import com_negocio

from crypto_traders.agents.market_data import MarketDataAgent
from crypto_traders.bus import InMemoryEventBus, Topics
from crypto_traders.domain.enums import ExchangeName
from crypto_traders.domain.models import Candle, Ticker
from crypto_traders.exchanges.base import Broker, MarketDataSource
from crypto_traders.exchanges.ccxt_adapter import build_market_data_source

pytestmark = pytest.mark.asyncio

TF = "1d"


class BusEspiao(InMemoryEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.publicados: list[tuple[str, Any]] = []

    async def publish(self, topic: str, payload: Any) -> None:
        self.publicados.append((topic, payload))
        await super().publish(topic, payload)

    @property
    def candles(self) -> list[Candle]:
        return [p for t, p in self.publicados if t == Topics.CANDLES]


class FonteFalsa(MarketDataSource):
    name = "falsa"

    def __init__(self, respostas: dict[str, list[Any]]) -> None:
        self._respostas = respostas
        self.chamadas: list[str] = []

    async def fetch_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        self.chamadas.append(symbol)
        fila = self._respostas.get(symbol) or [[]]
        resposta = fila[0] if len(fila) == 1 else fila.pop(0)
        if isinstance(resposta, BaseException):
            raise resposta
        return list(resposta)

    async def fetch_ticker(self, symbol: str) -> Ticker:
        return Ticker(exchange=ExchangeName.BINANCE, symbol=symbol, price=Decimal("1"))

    async def fetch_markets_and_tickers(self):
        return {}, {}

    async def close(self) -> None:
        return None


def fechado_em(dias_atras: int) -> datetime:
    hoje = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return hoje - timedelta(days=dias_atras)


def cru(
    *,
    open_time: datetime,
    o: str,
    h: str,
    low: str,
    c: str,
    symbol: str = "BTC/USDT",
    timeframe: str = TF,
    exchange: ExchangeName = ExchangeName.BINANCE,
) -> Candle:
    """Candle com OHLC arbitrario -- e assim que uma resposta corrompida chega."""
    return Candle(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal("7"),
        closed=True,
    )


async def montar(settings, respostas, timeframe: str = TF, **negocio):
    configurado = com_negocio(
        settings,
        symbols=list(respostas),
        timeframe=timeframe,
        candle_history_limit=50,
        **negocio,
    )
    bus = BusEspiao()
    await bus.start()
    fonte = FonteFalsa(respostas)
    return MarketDataAgent(bus, fonte, configurado), bus, fonte


# ======================================================================
# ATAQUE A: close FORA da faixa [low, high]
# ======================================================================
class TestAtaqueCloseForaDaFaixa:
    """`_closed_confiaveis` diz conferir "se os precos fazem sentido".

    Ele confere `min(o,h,l,c) <= 0` e `high < low`. Nao confere que o `close`
    -- o UNICO campo que viaja para `latest_prices` -- esteja dentro de
    [low, high]. Um candle com low=99, high=101 e close=1.000.000 e
    internamente impossivel, passa por fechado e confiavel, e define o preco
    que o Portfolio usa para avaliar a posicao e o Risk para disparar stop.
    """

    async def test_close_muito_acima_do_high_nao_deveria_virar_preco_corrente(self, settings):
        serie = [
            cru(open_time=fechado_em(1), o="100", h="101", low="99", c="1000000"),
        ]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]})

        await agente.refresh()

        assert agente.latest_prices == {}, (
            f"close=1.000.000 fora de [99,101] virou preco corrente: {agente.latest_prices}"
        )
        assert bus.candles == []

    async def test_close_muito_abaixo_do_low_nao_deveria_virar_preco_corrente(self, settings):
        serie = [
            cru(open_time=fechado_em(1), o="100", h="101", low="99", c="0.000001"),
        ]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]})

        await agente.refresh()

        assert agente.latest_prices == {}, (
            f"close abaixo do low virou preco corrente: {agente.latest_prices}"
        )
        assert bus.candles == []


# ======================================================================
# ATAQUE B: o rollback anda para TRAS por cima de uma publicacao que DEU CERTO
# ======================================================================
class TestAtaqueRollbackClobberaMarcaMaisNova:
    """O rollback de `_fetch_symbol` grava `anterior` sem conferir se a marca
    ainda e a dele.

    Duas coletas concorrentes do mesmo par -- o que o orquestrador faz na
    subida -- podem ver candles diferentes se um candle fechar no meio. A
    coleta B publica T2 com sucesso e marca T2. A coleta A, cuja publicacao de
    T1 falha, restaura `anterior` = T0 EM CIMA da marca T2. No ciclo seguinte
    T2 e republicado, e republicacao e exatamente o sinal reemitido que o
    filtro de monotonicidade existe para evitar.
    """

    async def test_rollback_nao_pode_apagar_marca_de_publicacao_bem_sucedida(self, settings):
        t0, t1, t2 = fechado_em(3), fechado_em(2), fechado_em(1)
        serie_t1 = [cru(open_time=t1, o="100", h="101", low="99", c="100")]
        serie_t2 = [cru(open_time=t2, o="110", h="111", low="109", c="110")]

        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie_t1]})
        # Estado inicial: T0 ja publicado em algum ciclo anterior.
        agente._last_published["BTC/USDT"] = t0

        original = bus.publish
        entrou = asyncio.Event()

        async def publish_de_A(topic: str, payload: Any) -> None:
            # A publicacao de T1 (coleta A) pendura e depois falha.
            entrou.set()
            await asyncio.sleep(0.05)
            raise ConnectionError("bus caiu no meio da publicacao de T1")

        bus.publish = publish_de_A  # type: ignore[method-assign]
        tarefa_a = asyncio.create_task(agente._fetch_symbol("BTC/USDT"))
        await entrou.wait()

        # Enquanto A esta pendurada, a coleta B publica T2 com SUCESSO.
        bus.publish = original  # type: ignore[method-assign]
        agente._source = FonteFalsa({"BTC/USDT": [serie_t2]})
        await agente._fetch_symbol("BTC/USDT")
        assert [c.open_time for c in bus.candles] == [t2]
        assert agente._last_published["BTC/USDT"] == t2

        with pytest.raises(ConnectionError):
            await tarefa_a

        assert agente._last_published["BTC/USDT"] == t2, (
            "o rollback de A andou para tras por cima da marca de T2, que FOI "
            f"publicado: marca ficou {agente._last_published['BTC/USDT']}"
        )

        # Consequencia pratica: T2 e republicado no ciclo seguinte.
        agente._source = FonteFalsa({"BTC/USDT": [serie_t2]})
        await agente.refresh()
        assert [c.open_time for c in bus.candles] == [t2], "T2 foi publicado duas vezes"


# ======================================================================
# ATAQUE C: exchange divergente -- publicado, mas invisivel para history()
# ======================================================================
class TestAtaqueExchangeDivergente:
    """A recusa por identidade confere `symbol` e `timeframe`, nao `exchange`.

    `history()` le por `settings.exchange`, entao um candle carimbado com outra
    exchange e gravado numa linha que a estrategia NUNCA le -- mas e publicado
    no bus e define `latest_prices`. Fica o pior dos dois mundos: evento com
    preco de outra exchange e janela historica vazia.
    """

    async def test_candle_de_outra_exchange_e_publicado_mas_nao_entra_na_janela(self, settings):
        serie = [
            cru(
                open_time=fechado_em(1),
                o="100",
                h="101",
                low="99",
                c="100",
                exchange=ExchangeName.COINBASE,
            )
        ]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]})

        await agente.refresh()

        publicados = [c.exchange for c in bus.candles]
        gravados = await agente.history("BTC/USDT")
        assert not publicados or gravados, (
            f"candle publicado ({publicados}) sem entrar na janela que a "
            f"estrategia le (history={gravados}); preco={agente.latest_prices}"
        )


# ======================================================================
# ATAQUE D: dois pares com o MESMO ativo base disputam uma unica chave
# ======================================================================
class TestAtaqueColisaoDeAtivoBase:
    """`latest_prices[symbol.partition("/")[0]]` joga fora a moeda de cotacao.

    BTC/USDC e BTC/USDT gravam na MESMA chave "BTC", e o ultimo par da rajada
    vence. O preco que o Portfolio usa para avaliar a posicao passa a ser o de
    um mercado que nao e o da posicao -- sem uma linha no log dizendo isso. Hoje
    os 16 pares sao todos /USDC, mas `SYMBOLS` e variavel de NEGOCIO editavel
    pela interface (D15): a colisao nasce de uma edicao na tela.
    """

    async def test_dois_quotes_do_mesmo_base_nao_deveriam_compartilhar_a_chave(self, settings):
        serie_usdc = [cru(symbol="BTC/USDC", open_time=fechado_em(1), o="100", h="101",
                          low="99", c="100")]
        serie_usdt = [cru(symbol="BTC/USDT", open_time=fechado_em(1), o="60000", h="60001",
                          low="59999", c="60000")]
        agente, _bus, _ = await montar(
            settings, {"BTC/USDC": [serie_usdc], "BTC/USDT": [serie_usdt]}
        )

        await agente.refresh()

        # Uma chave so para dois mercados: um dos dois precos foi perdido em
        # silencio, e nao ha como saber qual sobrou.
        assert len(agente.latest_prices) == 2, (
            "os dois pares colidiram numa chave unica: "
            f"{agente.latest_prices}"
        )


# ======================================================================
# ATAQUE E: poder de gastar chegando ao agente pela porta de servico
# ======================================================================
class TestAgenteDeMercadoNaoGasta:
    """Dado de mercado e publico, e este agente nao deve poder gastar dinheiro.

    `test_market_data.py::TestSemCredenciais` prova o lado da FABRICA:
    `build_market_data_source` nao tem parametro de credencial e o cliente ccxt
    nasce sem chave. Falta o lado do agente: `CcxtExchange` implementa
    `MarketDataSource` **e** `Broker`, entao nada na assinatura de
    `MarketDataAgent` impede que um dia alguem passe a instancia autenticada --
    a mesma que envia ordem -- no lugar da publica. O ataque aqui e por
    dentro: depois de construido, nenhum objeto que o agente segura pode ter
    poder de gastar.
    """

    async def test_nenhum_atributo_do_agente_tem_poder_de_gastar(self, settings):
        agente, _bus, _ = await montar(settings, {"BTC/USDT": [[]]})

        # `isinstance` pega a heranca declarada; `hasattr` pega o objeto que
        # so anda como broker (dublê, adaptador, mock deixado em producao).
        gastadores = [
            nome
            for nome, valor in vars(agente).items()
            if isinstance(valor, Broker) or hasattr(valor, "place_order")
        ]
        assert gastadores == [], (
            f"o agente de leitura segura objeto com poder de gastar: {gastadores}"
        )
        assert isinstance(agente._source, MarketDataSource)

    async def test_a_fonte_que_o_orquestrador_entrega_ao_agente_nao_esta_autenticada(self):
        """A fabrica publica e a unica origem da fonte do agente (ver orchestrator)."""
        fonte = build_market_data_source("binance")
        try:
            assert fonte.authenticated is False
            assert isinstance(fonte, MarketDataSource)
        finally:
            await fonte.close()
