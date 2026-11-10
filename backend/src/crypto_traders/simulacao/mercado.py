"""Exchange roteirizada: candles fechados, ticker mutavel e falhas sob encomenda.

Implementa `MarketDataSource` -- o mesmo contrato que o Market Data Agent recebe
em producao --, entao nada no caminho do preco e substituido: filtro de candle
fechado, chave de preco, ticker corrente e fallback para o fechamento rodam como
rodam contra a Binance.

Os candles sao ancorados no relogio REAL. `_closed_confiaveis` recusa candle cujo
periodo ainda nao terminou pelo relogio da maquina, entao uma serie com datas
inventadas no passado distante ou no futuro testaria o filtro, nao o cenario. A
serie termina no periodo anterior ao corrente; os ultimos periodos ficam
RESERVADOS e invisiveis, e `fechar_candle` revela o proximo. E assim que se
simula "o candle do dia fechou" sem mexer no relogio.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from ..domain.enums import ExchangeName
from ..domain.models import Candle, Ticker
from ..exchanges.base import ExchangeError, MarketDataSource, timeframe_seconds

#: Quantos periodos reservar por par para `fechar_candle`. Cada cenario revela
#: poucos; reservar demais so deixa a serie visivel mais "velha" (o que gera o
#: aviso de serie estagnada no log, sem mudar decisao nenhuma).
PERIODOS_RESERVADOS = 5


class TickerIndisponivel(ExchangeError):
    """Falha de ticker provocada pelo roteiro (rede, 429, exchange fora)."""


def _dec(valor: Decimal | str | float | int) -> Decimal:
    return valor if isinstance(valor, Decimal) else Decimal(str(valor))


class _Serie:
    """Os periodos de um par: os ja fechados e visiveis, e os reservados."""

    def __init__(self, fechamentos: list[Decimal], reservados: int) -> None:
        self.fechamentos: list[Decimal | None] = [*fechamentos, *([None] * reservados)]
        self.visiveis = len(fechamentos)


class MercadoSimulado(MarketDataSource):
    """Fonte de mercado roteirizada, sem rede."""

    name = "simulado"

    def __init__(
        self,
        *,
        timeframe: str = "1d",
        exchange: ExchangeName = ExchangeName.BINANCE,
        reservados: int = PERIODOS_RESERVADOS,
    ) -> None:
        self.timeframe = timeframe
        self.exchange = exchange
        self._duracao = timedelta(seconds=timeframe_seconds(timeframe))
        self._reservados = reservados
        self._series: dict[str, _Serie] = {}
        self._precos: dict[str, Decimal] = {}
        self._falhas_restantes = 0
        self.chamadas_de_ticker = 0
        """Leituras de ticker recebidas, falhas inclusive (prova de que houve tentativa)."""

        self.falhas_de_ticker = 0

    # ------------------------------------------------------------------
    # Roteiro
    # ------------------------------------------------------------------
    def adicionar_par(
        self,
        symbol: str,
        fechamentos: list[Decimal | str | float | int],
        *,
        preco: Decimal | str | float | int | None = None,
    ) -> None:
        """Cria a serie de um par. O ticker nasce no ultimo fechamento, salvo `preco`."""
        valores = [_dec(v) for v in fechamentos]
        if not valores:
            raise ValueError("a serie precisa de ao menos um fechamento")
        self._series[symbol] = _Serie(valores, self._reservados)
        self._precos[symbol] = _dec(preco) if preco is not None else valores[-1]

    def fechar_candle(self, symbol: str, fechamento: Decimal | str | float | int) -> Candle:
        """Fecha o proximo periodo reservado com `fechamento` e o torna visivel."""
        serie = self._series[symbol]
        if serie.visiveis >= len(serie.fechamentos):
            raise RuntimeError(
                f"{symbol}: periodos reservados esgotados; aumente `reservados`"
            )
        serie.fechamentos[serie.visiveis] = _dec(fechamento)
        serie.visiveis += 1
        return self._candles_fechados(symbol)[-1]

    def definir_preco(self, symbol: str, preco: Decimal | str | float | int) -> None:
        """Move o preco corrente (ticker). Candle nenhum muda."""
        self._precos[symbol] = _dec(preco)

    def preco(self, symbol: str) -> Decimal:
        return self._precos[symbol]

    def ultimo_fechamento(self, symbol: str) -> Decimal:
        return self._candles_fechados(symbol)[-1].close

    def falhar_tickers(self, chamadas: int | None = None) -> None:
        """As proximas `chamadas` leituras de ticker levantam `TickerIndisponivel`.

        Sem `chamadas`, falham ate `restaurar_tickers()` -- o roteiro nao precisa
        saber quantas leituras o agente faz por ciclo (retentativa conta).
        """
        self._falhas_restantes = -1 if chamadas is None else chamadas

    def restaurar_tickers(self) -> None:
        self._falhas_restantes = 0

    # ------------------------------------------------------------------
    # MarketDataSource
    # ------------------------------------------------------------------
    async def fetch_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        if timeframe != self.timeframe:
            raise ExchangeError(f"simulador em {self.timeframe}, pedido {timeframe}")
        fechados = self._candles_fechados(symbol)[-limit:]
        # O candle em formacao vem junto, como na exchange de verdade: e o
        # agente que tem de saber descarta-lo.
        return [*fechados, self._candle_em_formacao(symbol)]

    async def fetch_ticker(self, symbol: str) -> Ticker:
        self._consumir_chamada_de_ticker()
        return Ticker(exchange=self.exchange, symbol=symbol, price=self._precos[symbol])

    async def fetch_tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        self._consumir_chamada_de_ticker()
        return {
            s: Ticker(exchange=self.exchange, symbol=s, price=self._precos[s])
            for s in symbols
            if s in self._precos
        }

    async def fetch_markets_and_tickers(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        mercados: dict[str, dict[str, Any]] = {}
        tickers: dict[str, dict[str, Any]] = {}
        for symbol, preco in self._precos.items():
            base, _, quote = symbol.partition("/")
            mercados[symbol] = {
                "symbol": symbol,
                "base": base,
                "quote": quote,
                "active": True,
                "spot": True,
            }
            tickers[symbol] = {"symbol": symbol, "last": float(preco), "quoteVolume": 1e9}
        return mercados, tickers

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------
    def _consumir_chamada_de_ticker(self) -> None:
        self.chamadas_de_ticker += 1
        if self._falhas_restantes != 0:
            if self._falhas_restantes > 0:
                self._falhas_restantes -= 1
            self.falhas_de_ticker += 1
            raise TickerIndisponivel("ticker indisponivel (falha roteirizada)")

    def _periodo_corrente(self) -> datetime:
        agora = datetime.now(UTC)
        segundos = int(self._duracao.total_seconds())
        inicio = int(agora.timestamp()) // segundos * segundos
        return datetime.fromtimestamp(inicio, tz=UTC)

    def _candles_fechados(self, symbol: str) -> list[Candle]:
        serie = self._series[symbol]
        total = len(serie.fechamentos)
        # O ULTIMO periodo reservado abre um periodo antes do corrente: toda a
        # serie, revelada ou nao, ja fechou pelo relogio real.
        primeiro = self._periodo_corrente() - total * self._duracao
        candles: list[Candle] = []
        anterior: Decimal | None = None
        for indice in range(serie.visiveis):
            fechamento = serie.fechamentos[indice]
            assert fechamento is not None
            abertura = anterior if anterior is not None else fechamento
            candles.append(
                self._candle(symbol, primeiro + indice * self._duracao, abertura, fechamento)
            )
            anterior = fechamento
        return candles

    def _candle_em_formacao(self, symbol: str) -> Candle:
        preco = self._precos[symbol]
        abertura = self._candles_fechados(symbol)[-1].close
        return self._candle(symbol, self._periodo_corrente(), abertura, preco, fechado=False)

    def _candle(
        self,
        symbol: str,
        abertura_em: datetime,
        abertura: Decimal,
        fechamento: Decimal,
        *,
        fechado: bool = True,
    ) -> Candle:
        # Faixa coerente por construcao: open e close DENTRO de [low, high],
        # senao o agente recusa o candle como impossivel.
        return Candle(
            exchange=self.exchange,
            symbol=symbol,
            timeframe=self.timeframe,
            open_time=abertura_em,
            open=abertura,
            high=max(abertura, fechamento) * Decimal("1.002"),
            low=min(abertura, fechamento) * Decimal("0.998"),
            close=fechamento,
            volume=Decimal("100"),
            closed=fechado,
        )
