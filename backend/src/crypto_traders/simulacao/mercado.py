"""Exchange roteirizada: candles fechados, ticker mutavel e falhas sob encomenda.

Implementa `MarketDataSource` -- o mesmo contrato que o Market Data Agent recebe
em producao --, entao nada no caminho do preco e substituido: filtro de candle
fechado, chave de preco, ticker corrente e fallback para o fechamento rodam como
rodam contra a Binance.

Os candles sao ancorados no relogio REAL, lido uma vez na criacao do simulador
(ver `__init__`: relido a cada consulta, a virada do periodo no meio do cenario
movia a serie inteira). `_closed_confiaveis` recusa candle cujo
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
        #: (maxima, minima) roteirizadas do periodo; None = a faixa sintetica.
        self.extremos: list[tuple[Decimal, Decimal] | None] = [None] * len(self.fechamentos)
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
        # O periodo corrente e lido UMA vez, na criacao. Relido a cada consulta,
        # um cenario que cruzasse a virada (21:00 de Brasilia em 1d; a cada 15
        # min em 15m) via todos os `open_time` andarem um periodo no meio do
        # roteiro -- um candle "novo" que ninguem fechou. Ancorado, a serie
        # continua toda fechada pelo relogio real depois da virada, e o candle
        # em formacao (marcado aberto) continua descartado pelo agente.
        self._ancora = self._periodo_corrente_real()
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

    def fechar_candle(
        self,
        symbol: str,
        fechamento: Decimal | str | float | int,
        *,
        maxima: Decimal | str | float | int | None = None,
        minima: Decimal | str | float | int | None = None,
        preco: Decimal | str | float | int | None = None,
    ) -> Candle:
        """Fecha o proximo periodo reservado com `fechamento` e o torna visivel.

        **O ticker nao se move**, salvo `preco`: o preco corrente e o fechamento
        sao coisas diferentes de proposito (a compra executa no ticker, nao no
        fechamento da vespera). Fechar a 110 sem mover o ticker deixa o sinal com
        referencia 110 e o dimensionamento (no preco corrente) no ticker antigo.

        `maxima`/`minima` roteirizam o pavio do periodo. Sem elas a faixa e
        sintetica (+-0,2% da abertura e do fechamento), estreita demais para um
        pavio que o ticker nao viu -- o que o backtest le e a producao nao.
        """
        serie = self._series[symbol]
        if serie.visiveis >= len(serie.fechamentos):
            raise RuntimeError(
                f"{symbol}: periodos reservados esgotados; aumente `reservados`"
            )
        valor = _dec(fechamento)
        if maxima is not None or minima is not None:
            abertura = serie.fechamentos[serie.visiveis - 1] or valor
            alta = _dec(maxima) if maxima is not None else max(abertura, valor)
            baixa = _dec(minima) if minima is not None else min(abertura, valor)
            if alta < max(abertura, valor) or baixa > min(abertura, valor):
                # O agente recusaria o candle como impossivel, e o cenario
                # mediria o filtro em vez do que pretendia.
                raise ValueError(
                    f"{symbol}: faixa [{baixa}, {alta}] nao contem abertura {abertura} "
                    f"e fechamento {valor}"
                )
            serie.extremos[serie.visiveis] = (alta, baixa)
        serie.fechamentos[serie.visiveis] = valor
        serie.visiveis += 1
        if preco is not None:
            self.definir_preco(symbol, preco)
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

    def _periodo_corrente_real(self) -> datetime:
        agora = datetime.now(UTC)
        segundos = int(self._duracao.total_seconds())
        inicio = int(agora.timestamp()) // segundos * segundos
        return datetime.fromtimestamp(inicio, tz=UTC)

    def _periodo_corrente(self) -> datetime:
        """O periodo corrente de quando o simulador foi criado (ver `__init__`)."""
        return self._ancora

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
                self._candle(
                    symbol,
                    primeiro + indice * self._duracao,
                    abertura,
                    fechamento,
                    extremos=serie.extremos[indice],
                )
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
        extremos: tuple[Decimal, Decimal] | None = None,
    ) -> Candle:
        # Faixa coerente por construcao: open e close DENTRO de [low, high],
        # senao o agente recusa o candle como impossivel. A roteirizada ja foi
        # conferida em `fechar_candle`.
        if extremos is not None:
            alta, baixa = extremos
        else:
            alta = max(abertura, fechamento) * Decimal("1.002")
            baixa = min(abertura, fechamento) * Decimal("0.998")
        return Candle(
            exchange=self.exchange,
            symbol=symbol,
            timeframe=self.timeframe,
            open_time=abertura_em,
            open=abertura,
            high=alta,
            low=baixa,
            close=fechamento,
            volume=Decimal("100"),
            closed=fechado,
        )
