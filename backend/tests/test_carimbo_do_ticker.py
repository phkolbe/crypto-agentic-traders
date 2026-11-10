"""O ticker leva o instante do preco segundo a EXCHANGE (rodada 3, consolidacao).

A frente de carteira passou a medir o frescor do preco corrente pelo carimbo do
ticker, e nao pela hora de recebimento: um preco velho entregue atrasado parecia
recente. Faltava o outro lado -- o adaptador ccxt nunca preenchia o carimbo, e o
modelo caia sempre na hora local.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from crypto_traders.api.schemas import HealthOut
from crypto_traders.exchanges.ccxt_adapter import CcxtExchange

_INSTANTE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z


class Cliente:
    def __init__(self, resposta):
        self.resposta = resposta

    async def fetch_ticker(self, symbol):
        return self.resposta

    async def fetch_tickers(self, symbols):
        return {s: self.resposta for s in symbols}

    async def close(self):
        return None


@pytest.fixture
async def exchange():
    ex = CcxtExchange("binance", credentials=None, max_retries=1)
    verdadeiro = ex._client
    yield ex
    await verdadeiro.close()


async def test_fetch_ticker_usa_o_instante_da_exchange(exchange):
    exchange._client = Cliente({"last": 100.0, "timestamp": _INSTANTE_MS})
    ticker = await exchange.fetch_ticker("BTC/USDC")
    assert ticker.timestamp == datetime(2026, 1, 1, tzinfo=UTC)


async def test_fetch_tickers_usa_o_instante_da_exchange(exchange):
    exchange._client = Cliente({"last": 100.0, "timestamp": _INSTANTE_MS})
    tickers = await exchange.fetch_tickers(["BTC/USDC", "ETH/USDC"])
    assert {t.timestamp for t in tickers.values()} == {datetime(2026, 1, 1, tzinfo=UTC)}


async def test_sem_carimbo_vale_a_hora_local(exchange):
    exchange._client = Cliente({"last": 100.0})
    antes = datetime.now(UTC)
    ticker = await exchange.fetch_ticker("BTC/USDC")
    assert ticker.timestamp >= antes


def test_saude_expoe_a_trava_de_instancia_unica():
    """O orquestrador ja informava `instancia_unica`; o esquema da API a descartava."""
    campos = HealthOut.model_fields
    assert "instancia_unica" in campos
