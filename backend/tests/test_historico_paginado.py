"""Historico longo: paginado na exchange e com teto explicito no backtest.

O defeito (rodada 3): o `fetch_candles` do adaptador ccxt fazia UMA chamada, e a
Binance devolve no maximo 1000 klines por chamada -- o ccxt corta o `limit` em
silencio (`min(limit, maxLimit)` em `binance.fetch_ohlcv`). Em cima disso, o
`POST /backtest` cortava o pedido em `MAX_CANDLES = 1000`, tambem calado: os 90
dias padrao em 1h sao 2160 candles, e o backtest media 41 dias dizendo que eram 90.

Agora a busca pagina por `since` (sem duplicar nem pular candle) e a rota tem um
teto por MEMORIA, que devolve 422 dizendo o que fazer em vez de cortar.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from crypto_traders.api.app import create_app
from crypto_traders.api.routes import backtest as rota
from crypto_traders.domain.enums import ExchangeName
from crypto_traders.domain.models import Candle
from crypto_traders.exchanges import ccxt_adapter
from crypto_traders.exchanges.ccxt_adapter import CcxtExchange

HORA_MS = 3_600_000
#: "Agora" fixo, no meio de um candle de 1h: o candle em formacao abre em AGORA_CANDLE.
AGORA_MS = 1_767_225_600_000 + 5_000 * HORA_MS + 1_234_567
AGORA_CANDLE = AGORA_MS - AGORA_MS % HORA_MS


class ExchangeDeKlines:
    """Imita o `fetch_ohlcv` da Binance spot: no maximo 1000 linhas por chamada.

    `since` devolve as linhas com abertura >= since; sem `since`, as ultimas
    `limit`. `buraco` remove aberturas (manutencao da exchange); `recuo` devolve
    linhas ANTERIORES ao `since` pedido (exchange que nao respeita o cursor), o
    que testa a desduplicacao entre paginas.
    """

    MAXIMO = 1000

    def __init__(self, *, buraco: range | None = None, recuo: int = 0, travada: bool = False):
        primeira = AGORA_CANDLE - 6_000 * HORA_MS
        self.aberturas = [
            primeira + i * HORA_MS
            for i in range(6_001)
            if buraco is None or i not in buraco
        ]
        self.recuo = recuo
        self.travada = travada
        self.chamadas: list[tuple[int | None, int | None]] = []

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.chamadas.append((since, limit))
        limite = min(limit or 500, self.MAXIMO)
        if since is None:
            escolhidas = self.aberturas[-limite:]
        elif self.travada:
            escolhidas = self.aberturas[:limite]
        else:
            inicio = since - self.recuo * HORA_MS
            escolhidas = [t for t in self.aberturas if t >= inicio][:limite]
        return [[t, 100.0, 101.0, 99.0, 100.5, 3.0] for t in escolhidas]

    async def close(self):
        return None


@pytest.fixture
async def adaptador(monkeypatch):
    monkeypatch.setattr(ccxt_adapter, "_agora_ms", lambda: AGORA_MS)
    exchange = CcxtExchange("binance", credentials=None, max_retries=3)
    verdadeiro = exchange._client
    yield exchange
    await verdadeiro.close()


def _aberturas(candles: list[Candle]) -> list[int]:
    return [int(c.open_time.timestamp() * 1000) for c in candles]


class TestHistoricoPaginado:
    async def test_90_dias_em_1h_vem_inteiros_e_em_ordem(self, adaptador):
        """2160 candles pedidos, 2160 entregues; antes vinham 1000."""
        cliente = ExchangeDeKlines()
        adaptador._client = cliente

        candles = await adaptador.fetch_candles("BTC/USDC", "1h", 2160)

        aberturas = _aberturas(candles)
        assert len(candles) == 2160
        assert aberturas == sorted(set(aberturas)), "sem duplicata e em ordem"
        assert all(b - a == HORA_MS for a, b in pairwise(aberturas)), "sem pular"
        assert aberturas[-1] == AGORA_CANDLE
        assert [c.closed for c in candles[-2:]] == [True, False], "so o ultimo em formacao"
        assert all(limite <= 1000 for _, limite in cliente.chamadas)
        assert len(cliente.chamadas) == 3

    async def test_buraco_da_exchange_nao_para_a_paginacao(self, adaptador):
        """Manutencao sem klines no meio da janela: o resto continua vindo."""
        cliente = ExchangeDeKlines(buraco=range(4_500, 4_700))
        adaptador._client = cliente

        candles = await adaptador.fetch_candles("BTC/USDC", "1h", 2160)

        aberturas = _aberturas(candles)
        assert aberturas[-1] == AGORA_CANDLE
        assert aberturas == sorted(set(aberturas))
        # A janela pedida tem 2160 aberturas, 200 delas no buraco.
        assert len(candles) == 2160 - 200

    async def test_pagina_que_volta_para_tras_nao_duplica(self, adaptador):
        cliente = ExchangeDeKlines(recuo=7)
        adaptador._client = cliente

        candles = await adaptador.fetch_candles("BTC/USDC", "1h", 2160)

        aberturas = _aberturas(candles)
        assert len(aberturas) == 2160
        assert aberturas == sorted(set(aberturas))
        assert aberturas[-1] == AGORA_CANDLE

    async def test_exchange_que_ignora_o_cursor_nao_prende_o_laco(self, adaptador):
        """Uma exchange que devolve sempre a mesma pagina nao pode virar laco infinito."""
        cliente = ExchangeDeKlines(travada=True)
        adaptador._client = cliente

        candles = await adaptador.fetch_candles("BTC/USDC", "1h", 2160)

        assert len(cliente.chamadas) <= 5
        # O comeco do historico nao e "os ultimos 2160": nada fora da janela.
        assert candles == []

    async def test_ate_1000_continua_uma_chamada_sem_since(self, adaptador):
        """O caminho do Market Data (`candle_history_limit`) nao muda."""
        cliente = ExchangeDeKlines()
        adaptador._client = cliente

        candles = await adaptador.fetch_candles("BTC/USDC", "1h", 500)

        assert cliente.chamadas == [(None, 500)]
        assert len(candles) == 500


# ----------------------------------------------------------------------
# POST /backtest
# ----------------------------------------------------------------------
class FonteQueAnota:
    pedidos: list[int] = []  # noqa: RUF012 - de proposito: so este modulo usa
    criadas = 0

    def __init__(self) -> None:
        FonteQueAnota.criadas += 1

    async def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        FonteQueAnota.pedidos.append(limit)
        inicio = datetime(2026, 1, 1, tzinfo=UTC)
        return [
            Candle(
                exchange=ExchangeName.BINANCE,
                symbol=symbol,
                timeframe=timeframe,
                open_time=inicio + timedelta(hours=i),
                open=Decimal(100),
                high=Decimal(101),
                low=Decimal(99),
                close=Decimal(100),
                volume=Decimal(1),
            )
            for i in range(300)
        ]

    async def close(self) -> None:
        return None


@pytest.fixture
async def http(settings, monkeypatch):
    FonteQueAnota.pedidos = []
    FonteQueAnota.criadas = 0
    monkeypatch.setattr(rota, "build_market_data_source", lambda _exchange: FonteQueAnota())
    app = create_app()
    app.state.settings = settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _pedido(**kw: Any) -> dict[str, Any]:
    return {"symbol": "BTC/USDT", "strategy": "ma_crossover", "timeframe": "1h", **kw}


class TestTetoDoBacktest:
    async def test_90_dias_em_1h_pede_os_2160_candles(self, http):
        resposta = await http.post("/api/backtest", json=_pedido(days=90))

        assert resposta.status_code == 200, resposta.text
        # Antes: 1000, cortados em silencio.
        assert FonteQueAnota.pedidos == [2160]

    async def test_acima_do_teto_e_422_e_nao_busca_nada(self, http):
        """365 dias em 1m sao 525.600 candles: recusa clara, sem cortar."""
        resposta = await http.post("/api/backtest", json=_pedido(days=365, timeframe="1m"))

        assert resposta.status_code == 422
        detalhe = resposta.json()["detail"]
        assert "525600" in detalhe
        assert str(rota.MAX_CANDLES) in detalhe
        assert FonteQueAnota.criadas == 0, "nao abre cliente da exchange para recusar"

    async def test_o_teto_cabe_um_ano_em_1h(self):
        """O teto e por memoria, mas nao pode recusar o caso de uso comum."""
        assert rota.MAX_CANDLES >= 365 * 24
