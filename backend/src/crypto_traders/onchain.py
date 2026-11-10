"""Fonte de dados on-chain (MVRV Z-Score), com cache no banco.

Diferente do OHLCV, este dado **nao vem da exchange**: depende de um provedor
externo que pode sair do ar, mudar formato ou limitar acesso. Duas consequencias
que moldaram o desenho:

1. **Cache em banco.** A serie e diaria e imutavel no passado, entao buscar uma
   vez e guardar e correto -- e permite backtest sem depender da rede.
2. **Falha nao bloqueia negociacao.** Se o provedor cair, o filtro de regime
   simplesmente nao se aplica e o sistema volta a se comportar como antes,
   registrando um alerta. Essa e a unica excecao ao "falhar fechado" do projeto,
   e a razao e concreeta: sem o dado, o estado alternativo nao e "arriscado", e
   o mesmo estado testado que o sistema teve durante todo o desenvolvimento.
   Travar toda a operacao porque um site de terceiros ficou lento seria uma
   fragilidade nova, nao uma protecao.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx

from .config import Settings
from .db.repositories import OnChainMetricRepository
from .db.session import session_scope
from .indicators.onchain import MvrvReading, zone_by_percentile
from .logging_setup import get_logger

log = get_logger(__name__)

#: Fonte publica da serie diaria de MVRV Z-Score.
MVRV_URL = "https://bitcoin-data.com/v1/mvrv-zscore"

METRIC_MVRV = "mvrv_zscore"

#: A serie e diaria: rebuscar mais de uma vez ao dia e desperdicio.
REFRESH_INTERVAL = timedelta(hours=12)

#: Idade maxima do ultimo ponto para ele ainda valer como "o mercado de agora".
#: O ponto do dia D so sai depois do fim de D (e o provedor publica horas
#: depois), entao na manha de D+1 o mais novo e o de D-1: 2 dias pela data. Tres
#: dias de folga cobrem um atraso do provedor sem deixar semanas passarem.
IDADE_MAXIMA_MVRV = timedelta(days=3)


class OnChainProvider:
    """Busca e cacheia metricas on-chain."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._last_fetch: datetime | None = None
        self._velho_avisado = False
        """O `onchain.mvrv_stale` ja saiu neste episodio de serie envelhecida."""

    async def refresh_mvrv(self, force: bool = False) -> int:
        """Baixa a serie e grava o que ainda nao existe. Devolve quantos pontos novos."""
        now = datetime.now(UTC)
        if (
            not force
            and self._last_fetch is not None
            and now - self._last_fetch < REFRESH_INTERVAL
        ):
            return 0

        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.get(MVRV_URL)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            log.error("onchain.mvrv_fetch_failed", error=str(exc), url=MVRV_URL)
            return 0

        pontos: list[tuple[date, float]] = []
        for row in payload if isinstance(payload, list) else []:
            try:
                pontos.append(
                    (
                        datetime.strptime(row["d"], "%Y-%m-%d").date(),
                        float(row["mvrvZscore"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                # Um ponto malformado nao invalida a serie inteira.
                continue

        if not pontos:
            log.error("onchain.mvrv_empty_payload", url=MVRV_URL)
            return 0

        async with session_scope(self._settings) as session:
            novos = await OnChainMetricRepository(session).upsert_many(METRIC_MVRV, pontos)

        self._last_fetch = now
        log.info(
            "onchain.mvrv_refreshed",
            pontos=len(pontos),
            novos=novos,
            primeiro=str(pontos[0][0]),
            ultimo=str(pontos[-1][0]),
            valor_atual=pontos[-1][1],
        )
        return novos

    async def mvrv_series(self) -> list[tuple[date, float]]:
        async with session_scope(self._settings) as session:
            return await OnChainMetricRepository(session).series(METRIC_MVRV)

    async def mvrv_reading(self, moment: datetime | None = None) -> MvrvReading | None:
        """Leitura interpretada na data pedida (ou a mais recente disponivel).

        Devolve `None` quando nao ha dado -- e o chamador precisa tratar isso
        como "sem filtro", nao como "bloqueado".

        O percentil e calculado **apenas com o passado** em relacao ao momento
        pedido. Usar a serie inteira daria ao backtest conhecimento do futuro: um
        Z-Score de 3,0 em 2023 pareceria mediano por causa de valores que so
        aconteceriam em 2025.

        E o ultimo ponto precisa ser RECENTE (`IDADE_MAXIMA_MVRV`). Sem limite,
        com o provedor fora do ar (ou `mvrv_empty_payload`) por 3 semanas, o
        ultimo percentil em cache (0,85) seguia valendo: o Risk recusava toda
        compra com "mercado caro" sem o aviso `risk.mvrv_unavailable`, e o
        percentil baixo congelado deixava comprar num mercado que esquentou
        (auditoria de 09/11/2026, dados M3). Dado velho que parece atual e pior
        que dado nenhum: velho vira `None`, e o filtro se desliga de forma visivel.
        """
        serie = await self.mvrv_series()
        alvo = moment or datetime.now(UTC)
        passado = sorted((d, v) for d, v in serie if d <= _dia_utc(alvo))
        if passado:
            idade = _dia_utc(alvo) - passado[-1][0]
            if idade > IDADE_MAXIMA_MVRV:
                if not self._velho_avisado:
                    # Um aviso por episodio: o Risk le isto a cada lote de sinais.
                    self._velho_avisado = True
                    log.error(
                        "onchain.mvrv_stale",
                        ultimo_ponto=str(passado[-1][0]),
                        idade_dias=idade.days,
                        limite_dias=IDADE_MAXIMA_MVRV.days,
                        detail="filtro de regime desligado ate o provedor voltar",
                    )
                return None
            self._velho_avisado = False
        return reading_from_series(serie, alvo)

    async def ultimo_ponto_mvrv(self) -> date | None:
        """Data do ponto mais novo em cache, para o aviso de serie envelhecida."""
        serie = await self.mvrv_series()
        return serie[-1][0] if serie else None


def reading_from_series(
    serie: list[tuple[date, float]], moment: datetime
) -> MvrvReading | None:
    """Versao pura, para backtest: sem banco e sem rede.

    Recebe a serie ja carregada e respeita o mesmo cuidado com o futuro -- so
    usa pontos anteriores ou iguais ao momento avaliado.

    Nao aplica `IDADE_MAXIMA_MVRV`: a serie historica do backtest e diaria e
    completa, e quem precisa do limite e a leitura ao vivo (`mvrv_reading`).

    Ordena a serie em vez de supor que ela chega ordenada: `passado[-1]` e "o
    ponto mais novo" so numa lista em ordem, e uma serie montada a mao (teste,
    backtest com fonte trocada) daria o percentil de um dia qualquer.
    """
    alvo = _dia_utc(moment)
    passado = sorted((d, v) for d, v in serie if d <= alvo)
    if not passado:
        return None
    return zone_by_percentile(passado[-1][1], [v for _, v in passado])


def _dia_utc(moment: datetime) -> date:
    """O dia UTC do instante: a serie e por dia UTC.

    `moment.date()` sem normalizar pega o dia do fuso do objeto: 22:00 de D em
    Brasilia ja e D+1 em UTC. Instante sem fuso e tratado como UTC, a convencao
    do projeto.
    """
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(UTC).date()
