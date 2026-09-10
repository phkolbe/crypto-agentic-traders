"""Market Data Agent: sem credenciais, publicacao unica e resiliencia de rede.

Tres grupos de prova, cada um respondendo a uma pergunta que ler o codigo nao
responde:

1. **Poder.** O agente que le mercado nao pode ter poder de gastar. Aqui nao
   basta "a fonte e construida com `credentials=None`": o teste vai atras do
   cliente ccxt de verdade e cobra que a chave esteja vazia, e cobra que enviar
   ordem por essa fonte levante erro.
2. **Publicacao unica.** Candle e publicado UMA vez no bus. Republicar o mesmo
   candle faz a estrategia reavaliar -- e, com cooldown vencido, reemitir -- o
   mesmo sinal indefinidamente. Os testes cobrem repeticao, candle fora de
   ordem, oscilacao entre dois candles, relogio da exchange adiantado, buraco na
   serie e resposta truncada.
3. **Rede.** Timeout, 429, 5xx e desconexao no meio da rajada dos 16 pares. Um
   par que falha nao pode impedir os outros 15, e um par que PENDURA nao pode
   impedir nenhum.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt
import pytest
from helpers import com_negocio

from crypto_traders.agents import market_data as md
from crypto_traders.agents.market_data import MarketDataAgent
from crypto_traders.bus import InMemoryEventBus, Topics
from crypto_traders.domain.enums import ExchangeName, OrderType, Side
from crypto_traders.domain.models import Candle, OrderRequest, Ticker
from crypto_traders.exchanges.base import Broker, ExchangeError, MarketDataSource, timeframe_seconds
from crypto_traders.exchanges.ccxt_adapter import CcxtExchange, build_market_data_source

TF = "1d"
PERIODO = timedelta(days=1)


def candle(
    symbol: str = "BTC/USDT",
    *,
    open_time: datetime,
    close: str = "100",
    closed: bool = True,
    timeframe: str = TF,
) -> Candle:
    valor = Decimal(close)
    return Candle(
        exchange=ExchangeName.BINANCE,
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        open=valor,
        high=valor * Decimal("1.01"),
        low=valor * Decimal("0.99"),
        close=valor,
        volume=Decimal("7"),
        closed=closed,
    )


def fechado_em(dias_atras: int) -> datetime:
    """`open_time` de um candle diario aberto `dias_atras` dias atras.

    Com `dias_atras >= 1` o periodo dele ja terminou, entao e um candle
    legitimamente FECHADO. `dias_atras == 0` e o candle de hoje -- ainda em
    formacao --, e valor negativo e candle do futuro.
    """
    hoje = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return hoje - timedelta(days=dias_atras)


class BusEspiao(InMemoryEventBus):
    """Bus real, com registro do que foi publicado em cada topico."""

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
    """Fonte de mercado programavel: por simbolo, uma fila de respostas.

    Resposta pode ser uma lista de candles (sucesso), uma excecao (falha de
    rede) ou um float (segundos de espera antes de responder, para simular
    conexao pendurada).
    """

    name = "falsa"

    def __init__(self, respostas: dict[str, list[Any]]) -> None:
        self._respostas = respostas
        self.chamadas: list[str] = []
        self.fechada = False

    async def fetch_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        self.chamadas.append(symbol)
        fila = self._respostas.get(symbol) or [[]]
        resposta = fila[0] if len(fila) == 1 else fila.pop(0)
        if isinstance(resposta, float):
            await asyncio.sleep(resposta)
            return []
        if isinstance(resposta, BaseException):
            raise resposta
        return list(resposta)

    async def fetch_ticker(self, symbol: str) -> Ticker:
        return Ticker(exchange=ExchangeName.BINANCE, symbol=symbol, price=Decimal("1"))

    async def fetch_markets_and_tickers(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        return {}, {}

    async def close(self) -> None:
        self.fechada = True


async def montar(settings, respostas: dict[str, list[Any]], timeframe: str = TF, **negocio):
    """Agente pronto para `refresh()`, com bus espiao e fonte programavel."""
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
# 1. Poder: dados de mercado sao publicos, este agente nao gasta dinheiro
# ======================================================================
class TestSemCredenciais:
    async def test_fonte_publica_nasce_sem_chave_no_cliente_ccxt(self):
        """Nao basta passar `credentials=None`: o cliente ccxt tem que estar vazio."""
        fonte = build_market_data_source("binance")
        try:
            assert fonte.authenticated is False
            # `apiKey`/`secret` sao os campos que o ccxt assina nas requisicoes
            # privadas. Vazios, nenhuma chamada autenticada pode ser assinada.
            assert not fonte._client.apiKey
            assert not fonte._client.secret
            assert not getattr(fonte._client, "password", None)
        finally:
            await fonte.close()

    async def test_build_market_data_source_nao_aceita_credencial(self):
        """Nao existe parametro por onde uma chave entrar nesta fabrica."""
        parametros = inspect.signature(build_market_data_source).parameters
        assert "credentials" not in parametros
        assert set(parametros) == {"exchange_id", "testnet"}

    async def test_fonte_publica_recusa_enviar_ordem(self):
        """A protecao AGE: nao e um campo preenchido, e um erro levantado."""
        fonte = build_market_data_source("binance")
        pedido = OrderRequest(
            client_order_id="cli-1",
            signal_id="sig-1",
            risk_event_id="risk-1",
            exchange=ExchangeName.BINANCE,
            symbol="BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            price=Decimal("100"),
            notional=Decimal("100"),
            stop_loss=Decimal("90"),
            take_profit=Decimal("110"),
        )
        try:
            with pytest.raises(ExchangeError, match="sem credenciais"):
                await fonte.place_order(pedido)
        finally:
            await fonte.close()

    async def test_contrato_de_leitura_nao_expoe_metodo_que_gasta(self):
        """`MarketDataSource` e `Broker` sao interfaces separadas de proposito."""
        leitura = set(MarketDataSource.__abstractmethods__)
        gastos = set(Broker.__abstractmethods__)
        assert leitura == {
            "fetch_candles",
            "fetch_ticker",
            "fetch_markets_and_tickers",
            "close",
        }
        assert leitura & gastos == {"close"}
        assert "place_order" not in leitura

    async def test_agente_nao_menciona_credencial_no_codigo(self):
        """Nenhum caminho no agente pede chave, saldo ou envio de ordem."""
        fonte_do_agente = inspect.getsource(md)
        for proibido in ("api_key", "apiKey", "secret", "place_order", "fetch_balance"):
            assert proibido not in fonte_do_agente, f"market_data.py menciona {proibido}"

    async def test_orquestrador_usa_fonte_publica_separada_do_broker(self):
        """O agente recebe `build_market_data_source`, nunca `build_broker`."""
        from crypto_traders.agents import orchestrator as orq

        codigo = inspect.getsource(orq.Orchestrator.start)
        assert "self._source = build_market_data_source(" in codigo
        assert "MarketDataAgent(\n            self.bus, self._source" in codigo


# ======================================================================
# 2. Publicacao unica de candle fechado e inedito
# ======================================================================
class TestPublicacaoUnica:
    async def test_publica_o_candle_fechado_uma_unica_vez(self, settings):
        serie = [
            candle(open_time=fechado_em(2), close="100"),
            candle(open_time=fechado_em(1), close="110"),
            candle(open_time=fechado_em(0), close="115", closed=False),
        ]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]})

        await agente.refresh()
        await agente.refresh()
        await agente.refresh()

        assert len(bus.candles) == 1
        assert bus.candles[0].open_time == fechado_em(1)
        assert bus.candles[0].close == Decimal("110")
        assert agente.latest_prices["BTC"] == Decimal("110")

    async def test_candle_em_formacao_nunca_e_publicado(self, settings):
        """Resposta truncada com um unico candle -- o aberto -- publica nada."""
        serie = [candle(open_time=fechado_em(0), close="999", closed=False)]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]})

        await agente.refresh()

        assert bus.candles == []
        assert "BTC" not in agente.latest_prices

    async def test_resposta_vazia_nao_derruba_nem_publica(self, settings):
        agente, bus, _ = await montar(settings, {"BTC/USDT": [[]]})

        await agente.refresh()

        assert bus.candles == []
        assert agente.latest_prices == {}

    async def test_candle_novo_publica_de_novo(self, settings):
        primeiro = [
            candle(open_time=fechado_em(3), close="100"),
            candle(open_time=fechado_em(2), close="110"),
            candle(open_time=fechado_em(1), close="112", closed=False),
        ]
        segundo = [
            candle(open_time=fechado_em(2), close="110"),
            candle(open_time=fechado_em(1), close="120"),
        ]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [primeiro, segundo]})

        await agente.refresh()
        await agente.refresh()

        assert [c.open_time for c in bus.candles] == [fechado_em(2), fechado_em(1)]
        assert agente.latest_prices["BTC"] == Decimal("120")

    async def test_candle_fora_de_ordem_nao_e_publicado(self, settings):
        """A exchange devolve um candle ANTERIOR ao ultimo publicado.

        Com comparacao por igualdade (`!=`) esse candio velho passa: a estrategia
        recebe um candle do passado como se fosse novidade.
        """
        novo = [candle(open_time=fechado_em(1), close="110")]
        velho = [candle(open_time=fechado_em(5), close="50")]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [novo, velho]})

        await agente.refresh()
        await agente.refresh()

        assert [c.open_time for c in bus.candles] == [fechado_em(1)]
        # E o preco corrente nao pode andar para tras.
        assert agente.latest_prices["BTC"] == Decimal("110")

    async def test_oscilacao_entre_dois_candles_nao_reemite_sinal(self, settings):
        """O caso que faz a estrategia reemitir o mesmo sinal para sempre.

        A exchange alterna entre dois candles (replica atrasada, cache, failover).
        Guardar apenas "o ultimo publicado" e comparar por igualdade republica os
        dois em ciclo infinito: A, B, A, B...
        """
        a = [candle(open_time=fechado_em(2), close="100")]
        b = [candle(open_time=fechado_em(1), close="110")]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [a, b, a, b, a, b]})

        for _ in range(6):
            await agente.refresh()

        assert [c.open_time for c in bus.candles] == [fechado_em(2), fechado_em(1)]

    async def test_relogio_da_exchange_adiantado_nao_publica_candle_do_futuro(self, settings):
        """Candle cujo periodo ainda nao terminou nao esta fechado, diga o que disser.

        Se um candle do futuro fosse aceito, ele iria para o banco (poluindo a
        janela que a estrategia le) e travaria o simbolo: nenhum candle real
        seria "mais novo" que ele.
        """
        futuro = [
            candle(open_time=fechado_em(2), close="110"),
            candle(open_time=fechado_em(-3), close="900"),
        ]
        agente, bus, fonte = await montar(settings, {"BTC/USDT": [futuro]})

        await agente.refresh()

        assert [c.open_time for c in bus.candles] == [fechado_em(2)]
        assert agente.latest_prices["BTC"] == Decimal("110")
        # E o candle do futuro nao pode ter entrado no banco.
        gravados = [c.open_time for c in await agente.history("BTC/USDT")]
        assert fechado_em(-3) not in gravados

        # Depois do futuro rejeitado, o simbolo NAO travou: o candle real
        # seguinte ainda e aceito.
        fonte._respostas["BTC/USDT"] = [[candle(open_time=fechado_em(1), close="120")]]
        await agente.refresh()
        assert [c.open_time for c in bus.candles] == [fechado_em(2), fechado_em(1)]
        assert agente.latest_prices["BTC"] == Decimal("120")

    async def test_candle_sem_preco_nao_publica_nem_grava(self, settings):
        """Preco zero viraria avaliacao zero no Portfolio e no Risk."""
        serie = [
            candle(open_time=fechado_em(2), close="100"),
            candle(open_time=fechado_em(1), close="0"),
        ]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]})

        await agente.refresh()

        assert [c.open_time for c in bus.candles] == [fechado_em(2)]
        assert agente.latest_prices["BTC"] == Decimal("100")
        gravados = [c.open_time for c in await agente.history("BTC/USDT")]
        assert gravados == [fechado_em(2)]

    async def test_buraco_na_serie_publica_o_candle_seguinte(self, settings):
        """Par sem negociacao por dias: a serie pula periodos e continua valida."""
        antes = [candle("SHIB/USDT", open_time=fechado_em(9), close="100")]
        depois = [
            candle("SHIB/USDT", open_time=fechado_em(9), close="100"),
            candle("SHIB/USDT", open_time=fechado_em(1), close="130"),
        ]
        agente, bus, _ = await montar(settings, {"SHIB/USDT": [antes, depois]})

        await agente.refresh()
        await agente.refresh()

        assert [c.open_time for c in bus.candles] == [fechado_em(9), fechado_em(1)]
        assert agente.latest_prices["SHIB"] == Decimal("130")

    async def test_timeframe_invalido_recusa_publicar(self, settings):
        """Diante de configuracao que nao da para interpretar, nao se opera."""
        serie = [candle(open_time=fechado_em(1), close="110", timeframe="xx")]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]}, timeframe="xx")


        await agente.refresh()

        assert bus.candles == []


    async def test_rajada_de_16_pares_publica_cada_par_uma_unica_vez(self, settings):
        """A rajada real: 16 pares em ~0,4s, tres ciclos, 16 publicacoes."""
        respostas = {
            f"P{i:02d}/USDC": [[candle(f"P{i:02d}/USDC", open_time=fechado_em(1), close="10")]]
            for i in range(16)
        }
        agente, bus, _ = await montar(settings, respostas)

        for _ in range(3):
            await agente.refresh()

        assert len(bus.candles) == 16
        assert sorted(c.symbol for c in bus.candles) == sorted(respostas)

    async def test_agente_de_leitura_nunca_publica_pedido_de_ordem(self, settings):
        """Market data alimenta a cadeia; nao a atalha.

        Se este agente publicasse em `ORDER_REQUESTS`, uma ordem chegaria ao
        Execution Agent sem passar pelo Risk Manager.
        """
        serie = [candle(open_time=fechado_em(1), close="110")]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]})

        await agente.refresh()

        topicos = {t for t, _ in bus.publicados}
        assert topicos == {Topics.CANDLES}
        assert Topics.ORDER_REQUESTS not in topicos
        assert Topics.SIGNALS not in topicos


# ======================================================================
# 3. Resiliencia de rede
# ======================================================================
class TestResilienciaDeRede:
    def _dezesseis(self, quebrado: str | None, erro: BaseException | None):
        pares = [f"P{i:02d}/USDC" for i in range(16)]
        respostas: dict[str, list[Any]] = {}
        for indice, par in enumerate(pares):
            if par == quebrado and erro is not None:
                respostas[par] = [erro]
            else:
                respostas[par] = [[candle(par, open_time=fechado_em(1), close=str(100 + indice))]]
        return pares, respostas

    async def test_um_par_que_falha_nao_impede_os_outros_quinze(self, settings):
        pares, respostas = self._dezesseis("P07/USDC", ExchangeError("binance.fetch_ohlcv: 503"))
        agente, bus, fonte = await montar(settings, respostas)

        await agente.refresh()

        assert len(bus.candles) == 15
        assert {c.symbol for c in bus.candles} == set(pares) - {"P07/USDC"}
        # Todos os 16 foram tentados: a falha nao abortou a rajada.
        assert fonte.chamadas == pares

    @pytest.mark.parametrize(
        "erro",
        [
            TimeoutError("estourou o tempo"),
            ccxt.RateLimitExceeded("429 too many requests"),
            ccxt.ExchangeNotAvailable("502 bad gateway"),
            ccxt.NetworkError("conexao encerrada no meio da rajada"),
        ],
        ids=["timeout", "429", "5xx", "desconexao"],
    )
    async def test_falha_de_rede_em_um_par_e_isolada(self, settings, erro):
        _pares, respostas = self._dezesseis("P00/USDC", erro)
        agente, bus, fonte = await montar(settings, respostas)

        await agente.refresh()

        assert len(bus.candles) == 15
        assert "P00/USDC" not in {c.symbol for c in bus.candles}
        assert len(fonte.chamadas) == 16

    async def test_par_pendurado_nao_trava_a_rajada(self, settings, monkeypatch):
        """Sem teto de tempo, uma conexao que nunca responde para o agente inteiro.

        E o modo de falha mais perigoso: nada no log, nenhum erro, o agente
        simplesmente deixa de coletar -- e sem heartbeat o watchdog o reinicia.
        """
        # Teto folgado de proposito: o que se prova aqui e que EXISTE teto, nao
        # o valor dele. Apertado, o teste ficaria refem da carga da maquina e
        # cortaria a coleta legitima dos outros 15 pares.
        monkeypatch.setattr(md, "SYMBOL_FETCH_TIMEOUT_SECONDS", 2.0)
        _pares, respostas = self._dezesseis(None, None)
        respostas["P03/USDC"] = [600.0]
        agente, bus, fonte = await montar(settings, respostas)

        inicio = asyncio.get_running_loop().time()
        await agente.refresh()
        decorrido = asyncio.get_running_loop().time() - inicio

        assert decorrido < 30.0, f"a rajada ficou presa por {decorrido:.1f}s"
        assert len(bus.candles) == 15
        assert "P03/USDC" not in {c.symbol for c in bus.candles}
        assert len(fonte.chamadas) == 16

    async def test_par_que_falha_volta_a_publicar_no_ciclo_seguinte(self, settings):
        """Falha transitoria nao pode marcar o candle como ja publicado."""
        serie = [candle(open_time=fechado_em(1), close="110")]
        agente, bus, _ = await montar(
            settings, {"BTC/USDT": [ccxt.NetworkError("caiu"), serie]}
        )

        await agente.refresh()
        assert bus.candles == []

        await agente.refresh()
        assert [c.open_time for c in bus.candles] == [fechado_em(1)]

    async def test_falha_de_banco_nao_marca_candle_como_publicado(self, settings, monkeypatch):
        """Se a gravacao falha, o candle tem que ser retentado, nao perdido."""
        serie = [candle(open_time=fechado_em(1), close="110")]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie, serie]})

        chamadas = {"n": 0}
        original = md.CandleRepository.upsert_many

        async def upsert_quebrado(self, candles):
            chamadas["n"] += 1
            if chamadas["n"] == 1:
                raise RuntimeError("banco indisponivel")
            return await original(self, candles)

        monkeypatch.setattr(md.CandleRepository, "upsert_many", upsert_quebrado)

        await agente.refresh()
        assert bus.candles == []

        await agente.refresh()
        assert [c.open_time for c in bus.candles] == [fechado_em(1)]


class LogEspiao:
    """Captura as chamadas de log do agente, preservando nivel e evento."""

    def __init__(self) -> None:
        self.linhas: list[tuple[str, str]] = []

    def _registra(self, nivel):
        def anotar(evento, **campos):
            self.linhas.append((nivel, evento))

        return anotar

    def __getattr__(self, nivel):
        return self._registra(nivel)

    def eventos(self, evento: str) -> int:
        return sum(1 for _, e in self.linhas if e == evento)


class TestRuidoDeLog:
    """D25 mostrou que ruido enterra diagnostico: 1.440 alertas por dia.

    Com 16 pares a cada 60s, qualquer aviso repetido por ciclo produz 23.040
    linhas por dia. Aviso de estado se da na MUDANCA de estado.
    """

    async def test_par_ocioso_nao_repete_o_aviso_a_cada_ciclo(self, settings):
        serie = [candle(open_time=fechado_em(1), close="110")]
        agente, _bus, fonte = await montar(settings, {"BTC/USDT": [[]]})
        espiao = LogEspiao()
        agente.log = espiao

        for _ in range(5):
            await agente.refresh()

        assert espiao.eventos("market_data.resposta_vazia") == 1

        # E a volta ao normal se diz -- uma vez.
        fonte._respostas["BTC/USDT"] = [serie]
        await agente.refresh()
        await agente.refresh()
        assert espiao.eventos("market_data.coleta_normalizada") == 1

    async def test_falha_de_rede_persistente_avisa_uma_vez_por_causa(self, settings):
        agente, _bus, fonte = await montar(
            settings, {"BTC/USDT": [ccxt.NetworkError("connection reset")]}
        )
        espiao = LogEspiao()
        agente.log = espiao

        for _ in range(4):
            await agente.refresh()
        assert espiao.eventos("market_data.symbol_failed") == 1

        # Erro DIFERENTE e informacao nova: avisa de novo.
        fonte._respostas["BTC/USDT"] = [ccxt.ExchangeNotAvailable("503")]
        await agente.refresh()
        await agente.refresh()
        assert espiao.eventos("market_data.symbol_failed") == 2

    async def test_relogio_torto_persistente_avisa_uma_vez(self, settings):
        serie = [
            candle(open_time=fechado_em(2), close="110"),
            candle(open_time=fechado_em(-3), close="900"),
        ]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]})
        espiao = LogEspiao()
        agente.log = espiao

        for _ in range(5):
            await agente.refresh()

        assert espiao.eventos("market_data.candle_no_futuro") == 1
        assert len(bus.candles) == 1


# ======================================================================
# 4. Adaptador ccxt: normalizacao do que a exchange devolve
# ======================================================================
class ClienteFalso:
    """Cliente ccxt de mentira: responde `fetch_ohlcv` a partir de uma fila."""

    def __init__(self, respostas: list[Any]) -> None:
        self._respostas = list(respostas)
        self.chamadas = 0

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.chamadas += 1
        resposta = self._respostas[0] if len(self._respostas) == 1 else self._respostas.pop(0)
        if isinstance(resposta, BaseException):
            raise resposta
        return resposta

    async def close(self):
        return None


@pytest.fixture
async def adaptador():
    """`CcxtExchange` real com o cliente ccxt substituivel."""
    exchange = CcxtExchange("binance", credentials=None, max_retries=3)
    verdadeiro = exchange._client
    yield exchange
    await verdadeiro.close()


def linha(minuto: int, close: float) -> list[Any]:
    base = 1_767_225_600_000  # 2026-01-01T00:00:00Z
    return [base + minuto * 60_000, close, close + 1, close - 1, close, 10.0]


class TestAdaptadorCcxt:
    async def test_ultimo_candle_vem_aberto_e_precos_sao_decimal(self, adaptador):
        adaptador._client = ClienteFalso([[linha(0, 100), linha(15, 101), linha(30, 102)]])

        candles = await adaptador.fetch_candles("BTC/USDT", "15m", 3)

        assert [c.closed for c in candles] == [True, True, False]
        assert all(isinstance(c.close, Decimal) for c in candles)
        assert candles[0].close == Decimal("100.0")

    async def test_ordem_da_exchange_nao_decide_quem_esta_fechado(self, adaptador):
        """Resposta fora de ordem marcaria o candle ERRADO como aberto.

        Com a marcacao puramente posicional, uma resposta decrescente entrega o
        candle mais antigo como "em formacao" e o mais novo como fechado -- e o
        agente publicaria um candle velho como se fosse o atual.
        """
        adaptador._client = ClienteFalso([[linha(30, 102), linha(0, 100), linha(15, 101)]])

        candles = await adaptador.fetch_candles("BTC/USDT", "15m", 3)

        assert [c.open_time.minute for c in candles] == [0, 15, 30]
        assert [c.closed for c in candles] == [True, True, False]

    async def test_linha_truncada_ou_incompleta_e_descartada(self, adaptador):
        """Resposta truncada da API nao pode virar candle com preco zero."""
        adaptador._client = ClienteFalso(
            [
                [
                    linha(0, 100),
                    [1_767_226_500_000, None, None, None, None, None],
                    [1_767_227_400_000],  # truncada
                    linha(45, 103),
                    linha(60, 104),
                ]
            ]
        )

        candles = await adaptador.fetch_candles("BTC/USDT", "15m", 5)

        assert [c.close for c in candles] == [Decimal("100"), Decimal("103"), Decimal("104")]
        assert all(c.close > 0 for c in candles)

    async def test_timestamp_duplicado_nao_fecha_candle_em_formacao(self, adaptador):
        """Duplicata do candle atual promoveria o aberto a fechado."""
        adaptador._client = ClienteFalso([[linha(0, 100), linha(15, 101), linha(15, 101)]])

        candles = await adaptador.fetch_candles("BTC/USDT", "15m", 3)

        assert len(candles) == 2
        assert [c.closed for c in candles] == [True, False]

    async def test_429_e_retentado_e_depois_tem_sucesso(self, adaptador, monkeypatch):
        esperas: list[float] = []

        async def sem_dormir(segundos):
            esperas.append(segundos)

        monkeypatch.setattr(asyncio, "sleep", sem_dormir)
        cliente = ClienteFalso(
            [
                ccxt.RateLimitExceeded("429"),
                ccxt.RateLimitExceeded("429"),
                [linha(0, 100), linha(15, 101)],
            ]
        )
        adaptador._client = cliente

        candles = await adaptador.fetch_candles("BTC/USDT", "15m", 2)

        assert cliente.chamadas == 3
        assert len(candles) == 2
        assert esperas == [1.0, 2.0], "backoff exponencial entre as tentativas"

    @pytest.mark.parametrize(
        "erro",
        [
            ccxt.ExchangeNotAvailable("503"),
            ccxt.RequestTimeout("timeout"),
            ccxt.NetworkError("connection reset"),
        ],
        ids=["5xx", "timeout", "desconexao"],
    )
    async def test_falha_persistente_vira_ExchangeError(self, adaptador, monkeypatch, erro):
        async def sem_dormir(segundos):
            return None

        monkeypatch.setattr(asyncio, "sleep", sem_dormir)
        cliente = ClienteFalso([erro])
        adaptador._client = cliente

        with pytest.raises(ExchangeError, match="apos 3 tentativas"):
            await adaptador.fetch_candles("BTC/USDT", "15m", 2)

        assert cliente.chamadas == 3


class TestTimeframeSeconds:
    @pytest.mark.parametrize(
        ("texto", "segundos"),
        [
            ("1m", 60),
            ("15m", 900),
            ("4h", 14_400),
            ("1d", 86_400),
            ("1w", 604_800),
            ("1M", 2_592_000),
        ],
    )
    def test_converte_os_formatos_do_ccxt(self, texto, segundos):
        assert timeframe_seconds(texto) == segundos

    def test_minuto_e_mes_nao_se_confundem(self):
        """`m` e `M` diferem so pela caixa: normalizar para minusculo seria 43.200x errado."""
        assert timeframe_seconds("1M") == 30 * timeframe_seconds("1d")
        assert timeframe_seconds("1m") == 60

    @pytest.mark.parametrize("texto", ["", "d", "0d", "-1d", "1y", "abc", "15", "1.5h"])
    def test_recusa_o_que_nao_sabe_interpretar(self, texto):
        with pytest.raises(ValueError):
            timeframe_seconds(texto)


# ======================================================================
# 5. Publicacao interrompida: o candle nao pode ser dado por entregue
# ======================================================================
class FonteLenta(FonteFalsa):
    """Fonte que cede o event loop no meio do fetch, como a rede faz.

    E onde duas coletas concorrentes do MESMO par se cruzam.
    """

    async def fetch_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        self.chamadas.append(symbol)
        await asyncio.sleep(0.02)
        fila = self._respostas.get(symbol) or [[]]
        resposta = fila[0] if len(fila) == 1 else fila.pop(0)
        return list(resposta)


class TestPublicacaoInterrompida:
    """O inverso da publicacao dupla, e igualmente grave: a publicacao PERDIDA.

    A marca de "ja publiquei" existe para nao republicar, e a comparacao e por
    MAIOR QUE. Logo, candle marcado sem ter sido publicado nao volta nunca --
    com timeframe 1d, o sinal daquele par se perde por um dia inteiro, sem uma
    linha no log. Tres interrupcoes reais entre marcar e publicar:

    - o cancelamento da tarefa no `restart()` do watchdog (achado 2 de D25);
    - o teto de tempo por par, que converte esse cancelamento em `TimeoutError`;
    - a excecao do bus de Redis quando a conexao cai (`xadd` awaita rede).

    Em todas, o estado seguro e a duvida: reenviar. O filtro de monotonicidade
    absorve a republicacao; o silencio, nao.
    """

    @staticmethod
    def _bus_que_pendura(bus: BusEspiao, entrou: asyncio.Event, espera: float):
        """Substitui `publish` por uma versao que trava no meio da publicacao."""
        original = bus.publish

        async def publish_lento(topic: str, payload: Any) -> None:
            if topic == Topics.CANDLES:
                entrou.set()
                await asyncio.sleep(espera)
            await original(topic, payload)

        bus.publish = publish_lento  # type: ignore[method-assign]
        return original

    async def test_cancelamento_no_meio_da_publicacao_nao_perde_o_candle(self, settings):
        """Exatamente o que o watchdog faz ao reiniciar o agente: cancelar a tarefa."""
        serie = [candle(open_time=fechado_em(1), close="110")]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie, serie, serie]})
        entrou = asyncio.Event()
        original = self._bus_que_pendura(bus, entrou, 5.0)

        tarefa = asyncio.create_task(agente.refresh())
        await entrou.wait()
        tarefa.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa

        # Pre-condicao: a publicacao nao chegou a acontecer...
        assert bus.candles == []
        # ...e por isso o candle NAO pode estar marcado como entregue.
        assert "BTC/USDT" not in agente._last_published

        # Bus saudavel, mesmo candle na exchange: ele tem que ser reenviado.
        bus.publish = original  # type: ignore[method-assign]
        await agente.refresh()
        assert [c.open_time for c in bus.candles] == [fechado_em(1)]

    async def test_teto_de_tempo_por_par_no_meio_da_publicacao_nao_perde_o_candle(
        self, settings, monkeypatch
    ):
        """O teto por par vira `TimeoutError`, que `refresh` engole -- sem perder o candle."""
        monkeypatch.setattr(md, "SYMBOL_FETCH_TIMEOUT_SECONDS", 0.2)
        serie = [candle(open_time=fechado_em(1), close="110")]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie, serie, serie]})
        original = self._bus_que_pendura(bus, asyncio.Event(), 5.0)

        # O proprio `refresh` corta o par: nada sobe, nada e publicado.
        await agente.refresh()
        assert bus.candles == []
        assert "BTC/USDT" not in agente._last_published

        bus.publish = original  # type: ignore[method-assign]
        await agente.refresh()
        assert [c.open_time for c in bus.candles] == [fechado_em(1)]

    async def test_falha_do_bus_nao_marca_candle_como_entregue(self, settings):
        """Mesmo tratamento que a falha de gravacao no banco ja tinha."""
        serie = [candle(open_time=fechado_em(1), close="110")]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie, serie, serie]})

        original = bus.publish
        quedas = {"n": 0}

        async def publish_instavel(topic: str, payload: Any) -> None:
            if topic == Topics.CANDLES and quedas["n"] == 0:
                quedas["n"] += 1
                raise ConnectionError("conexao com o bus caiu no meio da publicacao")
            await original(topic, payload)

        bus.publish = publish_instavel  # type: ignore[method-assign]

        await agente.refresh()
        assert bus.candles == []
        assert "BTC/USDT" not in agente._last_published

        await agente.refresh()
        assert [c.open_time for c in bus.candles] == [fechado_em(1)]

    async def test_falha_ao_publicar_restaura_a_marca_anterior(self, settings):
        """Desfazer a marca nao pode ser apaga-la: isso republicaria o candle antigo.

        Sinal reemitido e justamente o dano que o filtro de monotonicidade
        existe para evitar. O rollback devolve o valor anterior, nao o vazio.
        """
        velho = [candle(open_time=fechado_em(2), close="100")]
        novo = [
            candle(open_time=fechado_em(2), close="100"),
            candle(open_time=fechado_em(1), close="110"),
        ]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [velho, novo, novo]})

        await agente.refresh()
        assert [c.open_time for c in bus.candles] == [fechado_em(2)]

        original = bus.publish
        quedas = {"n": 0}

        async def publish_instavel(topic: str, payload: Any) -> None:
            if topic == Topics.CANDLES and quedas["n"] == 0:
                quedas["n"] += 1
                raise ConnectionError("bus caiu")
            await original(topic, payload)

        bus.publish = publish_instavel  # type: ignore[method-assign]
        await agente.refresh()

        # A marca voltou para o candle ANTERIOR, nao para "nunca publiquei".
        assert agente._last_published["BTC/USDT"] == fechado_em(2)

        await agente.refresh()
        # O novo foi reenviado; o velho NAO foi republicado.
        assert [c.open_time for c in bus.candles] == [fechado_em(2), fechado_em(1)]

    async def test_publicacao_desfeita_deixa_rastro_no_log(self, settings):
        """Candle sumindo em silencio foi o modo de falha; a linha e o antidoto."""
        serie = [candle(open_time=fechado_em(1), close="110")]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]})
        espiao = LogEspiao()
        agente.log = espiao

        async def publish_quebrado(topic: str, payload: Any) -> None:
            raise ConnectionError("bus caiu")

        bus.publish = publish_quebrado  # type: ignore[method-assign]
        await agente.refresh()

        assert espiao.eventos("market_data.publicacao_desfeita") == 1
        assert espiao.eventos("market_data.candle_published") == 0

    async def test_duas_coletas_concorrentes_publicam_o_candle_uma_unica_vez(self, settings):
        """O outro lado da moeda: desfazer a marca nao pode reabrir a porta da duplicata.

        O orquestrador faz exatamente isso na subida -- cria a tarefa do laco de
        coleta e, sem esperar, chama `refresh()` de novo para aquecer o
        portfolio. Se existisse `await` entre ler `_last_published` e escrever
        nele, as duas coletas veriam "nunca publiquei" e publicariam o mesmo
        candle duas vezes.
        """
        serie = [candle(open_time=fechado_em(1), close="110")]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]})
        agente._source = FonteLenta({"BTC/USDT": [serie]})

        await asyncio.gather(agente.refresh(), agente.refresh())

        publicados = [c.open_time for c in bus.candles]
        assert publicados == [fechado_em(1)], f"candle publicado {len(publicados)}x"


# ======================================================================
# 6. Identidade do candle: par e periodo pedidos, nao os devolvidos
# ======================================================================
class TestIdentidadeDoCandle:
    """`latest_prices` e indexado pelo simbolo PEDIDO, e o julgamento "fechou?"
    usa a duracao do timeframe PEDIDO. Candle recebido de outro par ou de outro
    periodo deixa os dois numeros errados -- e este e o unico ponto por onde
    preco de mercado entra no sistema.
    """

    async def test_candle_de_outro_par_nao_define_o_preco_do_par_pedido(self, settings):
        """Medido na versao anterior: `latest_prices["BTC"]` virava 0.4 de um DOGE."""
        serie = [candle("DOGE/USDT", open_time=fechado_em(1), close="0.4")]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]})

        await agente.refresh()

        assert agente.latest_prices == {}
        assert bus.candles == []
        assert await agente.history("BTC/USDT") == []

    async def test_candle_de_outro_par_nao_contamina_a_serie_do_par_pedido(self, settings):
        """O impostor e recusado; o candle legitimo do mesmo ciclo continua valendo.

        O impostor e o MAIS NOVO de proposito: e ele que `max` escolheria, e
        portanto ele que definiria o preco publicado se nao fosse recusado.
        """
        serie = [
            candle(open_time=fechado_em(2), close="110"),
            candle("DOGE/USDT", open_time=fechado_em(1), close="0.4"),
        ]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]})

        await agente.refresh()

        assert [c.symbol for c in bus.candles] == ["BTC/USDT"]
        assert agente.latest_prices == {"BTC": Decimal("110")}

    async def test_candle_de_timeframe_divergente_nao_e_publicado_nem_gravado(self, settings):
        """Candle de 1m gravado como diario envenena a janela que a estrategia le."""
        serie = [
            candle(open_time=fechado_em(2), close="100"),
            candle(open_time=fechado_em(1), close="110", timeframe="1m"),
        ]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]})

        await agente.refresh()

        assert [c.timeframe for c in bus.candles] == [TF]
        assert [c.open_time for c in bus.candles] == [fechado_em(2)]
        gravados = await agente.history("BTC/USDT")
        assert [c.open_time for c in gravados] == [fechado_em(2)]
        assert all(c.timeframe == TF for c in gravados)

    async def test_identidade_divergente_nao_repete_o_aviso_a_cada_ciclo(self, settings):
        """Fonte errada e estado que dura; o aviso se da na mudanca (D25)."""
        serie = [candle("DOGE/USDT", open_time=fechado_em(1), close="0.4")]
        agente, _bus, _ = await montar(settings, {"BTC/USDT": [serie]})
        espiao = LogEspiao()
        agente.log = espiao

        for _ in range(5):
            await agente.refresh()

        assert espiao.eventos("market_data.candle_de_outra_identidade") == 1


# ======================================================================
# 7. Tolerancia de relogio proporcional ao periodo
# ======================================================================
class TestToleranciaDeRelogio:
    """`CLOCK_SKEW_TOLERANCE` sozinha e cega ao timeframe.

    Dois minutos sao 0,14% de um candle diario e DOIS candles de um minuto -- e
    timeframe e variavel de negocio editavel pela interface (D15), entao muda
    sem ninguem revisar a constante.
    """

    async def test_candle_de_1m_em_formacao_nao_passa_pela_tolerancia(self, settings):
        """Determinista: o candle de 999 fecha em 45s, muito alem de qualquer folga."""
        agora = datetime.now(UTC)
        serie = [
            # Fechou 30s atras: legitimo.
            candle(open_time=agora - timedelta(seconds=90), close="100", timeframe="1m"),
            # Fecha daqui a 45s: ainda em formacao, diga o que disser.
            candle(open_time=agora - timedelta(seconds=15), close="999", timeframe="1m"),
        ]
        agente, bus, _ = await montar(settings, {"BTC/USDT": [serie]}, timeframe="1m")

        await agente.refresh()

        assert [c.close for c in bus.candles] == [Decimal("100")]
        assert agente.latest_prices["BTC"] == Decimal("100")

    @pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "4h", "1d", "1w"])
    def test_a_folga_nunca_alcanca_um_periodo_inteiro(self, timeframe):
        """A regra, e nao um caso: a folga e sempre uma fracao pequena do periodo."""
        duracao = timedelta(seconds=timeframe_seconds(timeframe))
        folga = min(md.CLOCK_SKEW_TOLERANCE, duracao / md.CLOCK_SKEW_MAX_FRACTION)
        assert folga < duracao
        assert folga <= duracao / 10

    def test_o_timeframe_de_producao_mantem_os_dois_minutos(self):
        """A correcao aperta o caso de minutos sem mexer no 1d que roda hoje."""
        dia = timedelta(seconds=timeframe_seconds("1d"))
        assert min(md.CLOCK_SKEW_TOLERANCE, dia / md.CLOCK_SKEW_MAX_FRACTION) == timedelta(
            minutes=2
        )
        minuto = timedelta(seconds=timeframe_seconds("1m"))
        assert min(md.CLOCK_SKEW_TOLERANCE, minuto / md.CLOCK_SKEW_MAX_FRACTION) == timedelta(
            seconds=6
        )


# ======================================================================
# 8. Ruido de log: a chave de supressao e o TIPO, nao o texto do erro
# ======================================================================
class TestRuidoComMensagemVariavel:
    async def test_mensagem_de_erro_variavel_nao_multiplica_o_aviso(self, settings):
        """O 429 real da Binance carrega o instante do ban -- texto novo a cada ciclo.

        Chaveando a supressao pelo texto, a MESMA falha persistente volta a
        logar a cada ciclo: 16 pares x 1.440 ciclos por dia. Medido antes da
        correcao: 5 avisos em 5 ciclos.
        """

        class FonteComIdDeRequisicao(FonteFalsa):
            def __init__(self) -> None:
                super().__init__({"BTC/USDT": [[]]})
                self.n = 0

            async def fetch_candles(self, symbol, timeframe, limit=500):
                self.n += 1
                raise ccxt.RateLimitExceeded(
                    'binance GET /api/v3/klines 429 {"code":-1003,"msg":"Way too many '
                    f'requests; IP banned until 17672256{10 + self.n}000."'
                )

        agente, _bus, _ = await montar(settings, {"BTC/USDT": [[]]})
        agente._source = FonteComIdDeRequisicao()
        espiao = LogEspiao()
        agente.log = espiao

        for _ in range(5):
            await agente.refresh()

        assert espiao.eventos("market_data.symbol_failed") == 1

    async def test_causa_de_tipo_diferente_ainda_avisa(self, settings):
        """Suprimir repeticao nao pode virar surdez: falha NOVA e informacao."""
        agente, _bus, fonte = await montar(
            settings, {"BTC/USDT": [ccxt.RateLimitExceeded("429 IP banned until 1767225611000")]}
        )
        espiao = LogEspiao()
        agente.log = espiao

        for _ in range(3):
            await agente.refresh()
        assert espiao.eventos("market_data.symbol_failed") == 1

        fonte._respostas["BTC/USDT"] = [ccxt.ExchangeNotAvailable("503 service unavailable")]
        await agente.refresh()
        await agente.refresh()
        assert espiao.eventos("market_data.symbol_failed") == 2
