"""Indicadores on-chain. Hoje: MVRV Z-Score.

## O que e

**MVRV** = Market Value to Realized Value. O *market value* e a capitalizacao
(preco x oferta em circulacao); o *realized value* soma o preco em que cada
moeda se moveu por ultimo -- uma aproximacao do preco medio pago pelo mercado.

O **Z-Score** normaliza a diferenca entre os dois pelo desvio padrao da
capitalizacao:

    z = (market_cap - realized_cap) / std(market_cap)

Le-se como "quao caro o mercado esta em relacao ao que os detentores pagaram".
Alto significa lucro nao realizado grande (historicamente, proximidade de topo
de ciclo); negativo significa que o mercado agregado esta no prejuizo
(historicamente, fundo).

## Diferencas em relacao aos indicadores de preco deste projeto

Tres, e todas mudam como ele pode ser usado:

1. **E do Bitcoin.** Nao existe MVRV por par negociado. Serve como leitura do
   regime do mercado inteiro, apostando na correlacao do restante com o BTC --
   correlacao alta, mas nao perfeita.
2. **E lento.** Move-se em meses, nao em candles. Em 4h e praticamente
   constante, entao nao gera sinal de entrada: serve de **filtro de regime**.
3. **Nao vem da exchange.** Exige dado on-chain, de uma fonte externa que pode
   ficar indisponivel -- diferente de OHLCV, que vem da propria Binance.

## Sobre os limiares classicos

A literatura cita `z > 7` como topo de ciclo e `z < 0` como fundo. Nos 4 anos de
serie disponiveis (2022-09 a 2026-09) o **maximo foi 3,35** e nao houve um unico
dia acima de 4. Um filtro com o limiar classico nunca dispararia.

Por isso `zone_by_percentile` existe: classifica pela posicao dentro da historia
observada, em vez de num numero absoluto que pode nunca ser alcancado.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pandas as pd

#: Limiares citados na literatura. Mantidos como referencia -- e como aviso: nos
#: dados de 2022-2026 nenhum dia atingiu `EUPHORIA`, muito menos `TOP`.
CLASSIC_BOTTOM = 0.0
CLASSIC_EUPHORIA = 4.0
CLASSIC_TOP = 7.0


def mvrv_zscore(
    market_cap: pd.Series, realized_cap: pd.Series, window: int | None = None
) -> pd.Series:
    """Calcula o Z-Score a partir das capitalizacoes.

    `window=None` usa desvio padrao **expansivo** (toda a historia ate cada
    ponto), que e a definicao original. Um desvio de janela movel produziria
    valores diferentes e nao comparaveis com os graficos publicados.

    Usa `ddof=0` (populacional) pelo mesmo motivo das Bandas de Bollinger neste
    projeto: e o que as plataformas usam.
    """
    if len(market_cap) != len(realized_cap):
        raise ValueError("market_cap e realized_cap precisam ter o mesmo tamanho")

    diff = market_cap - realized_cap
    if window is None:
        desvio = market_cap.expanding(min_periods=2).std(ddof=0)
    else:
        desvio = market_cap.rolling(window=window, min_periods=window).std(ddof=0)

    return diff / desvio.replace(0, np.nan)


@dataclass(frozen=True)
class MvrvReading:
    """Leitura interpretada do Z-Score num instante."""

    value: float
    percentile: float
    """Posicao na historia observada, de 0 a 1. 0,9 = mais caro que 90% dos dias."""

    zone: str

    @property
    def expensive(self) -> bool:
        """True quando o mercado esta historicamente caro."""
        return self.percentile >= 0.80

    @property
    def cheap(self) -> bool:
        return self.percentile <= 0.20

    def explain(self) -> str:
        return (
            f"MVRV Z-Score {self.value:.2f} — {self.zone} "
            f"(mais alto que {self.percentile:.0%} dos dias observados)"
        )


def zone_by_percentile(value: float, history: list[float]) -> MvrvReading:
    """Classifica o Z-Score pela posicao dentro da historia observada.

    Preferido aos limiares absolutos porque estes dependem do ciclo: o topo de
    2021 marcou z acima de 7, e desde 2022 o maximo foi 3,35. Um filtro fixo em
    7 ficaria inerte; um percentil se adapta ao regime que de fato existe.

    O custo dessa escolha, que precisa ficar explicito: percentil e **relativo a
    amostra**. Se a serie disponivel cobre so um mercado de baixa, "caro" ali
    pode ser barato em termos absolutos.
    """
    if not history:
        return MvrvReading(value=value, percentile=0.5, zone="sem historico")

    ordenado = np.sort(np.asarray(history, dtype=float))
    percentil = float(np.searchsorted(ordenado, value, side="right") / len(ordenado))

    if percentil >= 0.95:
        zona = "euforia (topo do observado)"
    elif percentil >= 0.80:
        zona = "caro"
    elif percentil >= 0.50:
        zona = "neutro-alto"
    elif percentil >= 0.20:
        zona = "neutro-baixo"
    else:
        zona = "barato (fundo do observado)"

    return MvrvReading(value=value, percentile=percentil, zone=zona)


def zone_by_classic_threshold(value: float) -> str:
    """Classificacao pelos limiares da literatura.

    Mantida para leitura e comparacao. **Nao usar como filtro** sem antes
    conferir que a faixa recente do indicador alcanca os limiares -- desde 2022
    nao alcanca.
    """
    if value >= CLASSIC_TOP:
        return "topo de ciclo (classico)"
    if value >= CLASSIC_EUPHORIA:
        return "euforia (classico)"
    if value >= CLASSIC_BOTTOM:
        return "neutro (classico)"
    return "fundo (classico)"


def to_decimal(value: float) -> Decimal:
    return Decimal(str(value))
