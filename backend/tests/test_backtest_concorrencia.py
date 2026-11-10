"""`POST /backtest` nao pode parar o sistema que guarda o stop.

O backtest roda no MESMO processo dos agentes (512 MB, 1 CPU em producao). Ate
09/11/2026 a rota rodava o laco do motor -- ate 1000 iteracoes montando
`DataFrame` e avaliando a estrategia, sincrono, porque os `await` do PaperBroker
nao cedem a vez de fato -- no proprio laco de eventos, sem limite de pedidos
simultaneos. Cenario: dois cliques em "Rodar" com 365 dias/1h -> dois lacos de
CPU em serie no laco de eventos; enquanto isso o Risk Manager nao processa
retrato (stop), o WebSocket nao drena e, com dois `load_markets` na memoria, o
OOM-killer derruba agentes e stop junto.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import pytest
from httpx import ASGITransport, AsyncClient

from crypto_traders.api.app import create_app
from crypto_traders.api.routes import backtest as rota
from crypto_traders.backtest.engine import BacktestEngine
from crypto_traders.domain.enums import ExchangeName
from crypto_traders.domain.models import Candle

PEDIDO = {
    "symbol": "BTC/USDT",
    "strategy": "ma_crossover",
    "timeframe": "15m",
    "days": 5,
    "initial_balance": "1000",
}


def _candles(quantidade: int = 300) -> list[Candle]:
    inicio = datetime(2026, 1, 1, tzinfo=UTC)
    precos = [Decimal(50000 + (indice % 40) * 25) for indice in range(quantidade)]
    return [
        Candle(
            exchange=ExchangeName.BINANCE,
            symbol="BTC/USDT",
            timeframe="15m",
            open_time=inicio + timedelta(minutes=15 * indice),
            open=preco,
            high=preco * Decimal("1.004"),
            low=preco * Decimal("0.996"),
            close=preco,
            volume=Decimal("10"),
        )
        for indice, preco in enumerate(precos)
    ]


class FonteFalsa:
    """Fonte de candles sem rede, que registra se foi fechada."""

    criadas: ClassVar[list[FonteFalsa]] = []

    def __init__(self, *, falha: Exception | None = None) -> None:
        self.falha = falha
        self.fechada = False
        FonteFalsa.criadas.append(self)

    async def fetch_candles(self, symbol, timeframe, limit):
        if self.falha is not None:
            raise self.falha
        return _candles()[-limit:]

    async def close(self):
        self.fechada = True


@pytest.fixture
async def http(settings, monkeypatch):
    FonteFalsa.criadas = []
    monkeypatch.setattr(rota, "build_market_data_source", lambda _exchange: FonteFalsa())
    app = create_app()
    app.state.settings = settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestBacktestNaoTravaOLacoDeEventos:
    async def test_o_laco_segue_girando_durante_o_backtest(self, http, monkeypatch):
        """O motor ocupa a CPU por 0,5 s; o laco de eventos nao pode parar junto.

        `time.sleep` dentro do `run` faz o papel do laco pandas: e trabalho
        sincrono, que nenhum `await` interrompe. Rodando no laco de eventos, o
        relogio abaixo ficava 0,5 s sem dar um passo.
        """

        class MotorPesado(BacktestEngine):
            async def run(self, candles):
                time.sleep(0.5)
                return await super().run(candles)

        monkeypatch.setattr(rota, "BacktestEngine", MotorPesado)

        pausas: list[float] = []
        terminou = asyncio.Event()

        async def relogio():
            ultimo = time.perf_counter()
            while not terminou.is_set():
                await asyncio.sleep(0.01)
                agora = time.perf_counter()
                pausas.append(agora - ultimo)
                ultimo = agora

        tarefa = asyncio.create_task(relogio())
        try:
            resposta = await http.post("/api/backtest", json=PEDIDO)
        finally:
            terminou.set()
            await tarefa

        assert resposta.status_code == 200, resposta.text
        assert pausas, "o relogio nao girou nenhuma vez"
        assert max(pausas) < 0.25, (
            f"o laco de eventos ficou {max(pausas):.2f} s parado durante o backtest"
        )

    async def test_segundo_pedido_simultaneo_recebe_429(self, http, monkeypatch):
        """Um backtest por vez; o segundo e recusado na hora, sem fila.

        Fila esconderia o custo: dois pedidos em espera seriam dois historicos e
        dois motores na memoria de 512 MB, um depois do outro, sem o operador
        saber por que a tela demora.
        """
        entrou = threading.Event()
        liberar = threading.Event()

        class MotorQueEspera(BacktestEngine):
            async def run(self, candles):
                entrou.set()
                # Com timeout: no codigo antigo (sem thread) esta espera trava o
                # laco de eventos, e o teste precisa falhar, nao pendurar.
                liberar.wait(timeout=2)
                return await super().run(candles)

        monkeypatch.setattr(rota, "BacktestEngine", MotorQueEspera)

        primeiro = asyncio.create_task(http.post("/api/backtest", json=PEDIDO))
        try:
            for _ in range(200):
                if entrou.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entrou.is_set(), "o primeiro backtest nao chegou ao motor"

            segundo = await http.post("/api/backtest", json=PEDIDO)
            assert segundo.status_code == 429, segundo.text
            assert "backtest" in segundo.json()["detail"]
        finally:
            liberar.set()
            resposta = await primeiro
        assert resposta.status_code == 200, resposta.text

        # Terminado o primeiro, a vaga volta: o semaforo nao fica preso.
        terceiro = await http.post("/api/backtest", json=PEDIDO)
        assert terceiro.status_code == 200, terceiro.text

    async def test_falha_no_motor_libera_a_vaga(self, http, monkeypatch):
        """Erro no meio do backtest nao pode deixar o semaforo tomado para sempre."""

        class MotorQueFalha(BacktestEngine):
            async def run(self, candles):
                raise ValueError("historico insuficiente: teste")

        monkeypatch.setattr(rota, "BacktestEngine", MotorQueFalha)
        primeiro = await http.post("/api/backtest", json=PEDIDO)
        assert primeiro.status_code == 422

        monkeypatch.setattr(rota, "BacktestEngine", BacktestEngine)
        segundo = await http.post("/api/backtest", json=PEDIDO)
        assert segundo.status_code == 200, segundo.text


class TestClienteDeMercadoDoBacktest:
    async def test_a_fonte_e_fechada_ao_fim(self, http):
        resposta = await http.post("/api/backtest", json=PEDIDO)
        assert resposta.status_code == 200, resposta.text
        assert FonteFalsa.criadas and all(f.fechada for f in FonteFalsa.criadas)

    async def test_erro_da_exchange_nao_vaza_para_o_cliente(self, http, monkeypatch):
        """O 502 devolvia `str(exc)` do ccxt: URL, corpo da resposta da Binance."""
        monkeypatch.setattr(
            rota,
            "build_market_data_source",
            lambda _exchange: FonteFalsa(
                falha=RuntimeError("binance GET https://api.binance.com/api/v3/klines 418 banido")
            ),
        )
        resposta = await http.post("/api/backtest", json=PEDIDO)
        assert resposta.status_code == 502
        assert "api.binance.com" not in resposta.json()["detail"]
        assert FonteFalsa.criadas[-1].fechada

    async def test_campo_desconhecido_e_recusado(self, http):
        """`BacktestIn` sem `extra="forbid"`: `dias` (em vez de `days`) era ignorado
        e o backtest rodava 90 dias calado."""
        resposta = await http.post("/api/backtest", json=PEDIDO | {"dias": 30})
        assert resposta.status_code == 422
