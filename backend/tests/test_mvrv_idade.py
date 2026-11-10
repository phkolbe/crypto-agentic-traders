"""MVRV com idade maxima (auditoria de 09/11/2026, dados M3).

`mvrv_reading` devolvia o percentil do ULTIMO ponto em cache, de qualquer idade.
Com o bitcoin-data.com fora do ar (ou mudando o formato, `mvrv_empty_payload`)
por 3 semanas, o percentil 0,85 congelado seguia recusando toda compra com
"mercado caro" -- sem o aviso `risk.mvrv_unavailable`, porque para o Risk havia
leitura. O contrato do proprio modulo (`onchain.py`, "se o provedor cair, o
filtro simplesmente nao se aplica") e do orquestrador ("dado velho que parece
atual e pior que dado nenhum") prometiam o contrario.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

from structlog.testing import capture_logs

import crypto_traders.agents.orchestrator as orch_mod
from crypto_traders.bus import Topics
from crypto_traders.db.repositories import OnChainMetricRepository
from crypto_traders.db.session import session_scope
from crypto_traders.onchain import IDADE_MAXIMA_MVRV, METRIC_MVRV, OnChainProvider


async def _gravar_serie(settings, ultimo_ponto_ha_dias: int) -> None:
    """60 dias de serie crescente: o ultimo ponto e o maior (percentil 1,0)."""
    hoje = datetime.now(UTC).date()
    ultimo = hoje - timedelta(days=ultimo_ponto_ha_dias)
    pontos = [(ultimo - timedelta(days=59 - i), float(i)) for i in range(60)]
    async with session_scope(settings) as session:
        await OnChainMetricRepository(session).upsert_many(METRIC_MVRV, pontos)


class TestLeituraTemIdadeMaxima:
    async def test_ponto_de_tres_semanas_nao_vale_como_mercado_de_agora(self, settings):
        await _gravar_serie(settings, ultimo_ponto_ha_dias=21)
        provedor = OnChainProvider(settings)

        with capture_logs() as eventos:
            primeira = await provedor.mvrv_reading()
            segunda = await provedor.mvrv_reading()

        assert primeira is None and segunda is None
        velhos = [e for e in eventos if e["event"] == "onchain.mvrv_stale"]
        # Um aviso por episodio: o Risk le isto a cada lote de sinais.
        assert len(velhos) == 1
        assert velhos[0]["log_level"] == "error"
        assert velhos[0]["idade_dias"] == 21

    async def test_ponto_de_ontem_segue_valendo(self, settings):
        await _gravar_serie(settings, ultimo_ponto_ha_dias=1)

        leitura = await OnChainProvider(settings).mvrv_reading()

        assert leitura is not None and leitura.percentile == 1.0

    async def test_no_limite_ainda_vale(self, settings):
        await _gravar_serie(settings, ultimo_ponto_ha_dias=IDADE_MAXIMA_MVRV.days)

        assert await OnChainProvider(settings).mvrv_reading() is not None

    async def test_o_aviso_rearma_quando_a_serie_volta(self, settings):
        provedor = OnChainProvider(settings)
        await _gravar_serie(settings, ultimo_ponto_ha_dias=10)
        with capture_logs() as eventos:
            assert await provedor.mvrv_reading() is None
            await _gravar_serie(settings, ultimo_ponto_ha_dias=0)
            assert await provedor.mvrv_reading() is not None
            agora = datetime.now(UTC)
            assert await provedor.mvrv_reading(agora + timedelta(days=10)) is None
        velhos = [e for e in eventos if e["event"] == "onchain.mvrv_stale"]
        assert len(velhos) == 2


class TestOrquestradorAvisaOOperador:
    """O log nao chega ao operador: com o filtro LIGADO, serie velha vira alerta."""

    async def _rodar_loop(self, orch, monkeypatch, voltas: int) -> list[dict]:
        publicados: list[dict] = []

        async def publicar(topico, payload):
            if topico == Topics.ALERTS:
                publicados.append(payload)

        async def falso_refresh(self, force=False):
            return 0

        restante = {"n": voltas}

        async def sleep_contado(_segundos):
            restante["n"] -= 1
            if restante["n"] < 0:
                raise asyncio.CancelledError

        monkeypatch.setattr(orch.bus, "publish", publicar)
        monkeypatch.setattr(orch_mod.OnChainProvider, "refresh_mvrv", falso_refresh)
        monkeypatch.setattr(orch_mod.asyncio, "sleep", sleep_contado)
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await orch._refresh_onchain_forever()
        finally:
            monkeypatch.undo()
        return publicados

    def _orquestrador(self, settings, limiar: float):
        configurado = settings.model_copy(
            update={"risk": settings.risk.model_copy(update={"mvrv_max_percentile": limiar})}
        )
        orch = orch_mod.Orchestrator(configurado)
        orch.onchain = OnChainProvider(configurado)
        return orch

    async def test_serie_velha_com_filtro_ligado_vira_um_alerta(self, settings, monkeypatch):
        await _gravar_serie(settings, ultimo_ponto_ha_dias=21)
        orch = self._orquestrador(settings, limiar=0.8)

        alertas = await self._rodar_loop(orch, monkeypatch, voltas=3)

        velhos = [a for a in alertas if a.get("type") == "mvrv_stale"]
        assert len(velhos) == 1, alertas
        assert "21 dias" in velhos[0]["message"]

    async def test_com_filtro_desligado_nao_ha_o_que_avisar(self, settings, monkeypatch):
        await _gravar_serie(settings, ultimo_ponto_ha_dias=21)
        orch = self._orquestrador(settings, limiar=1.0)

        alertas = await self._rodar_loop(orch, monkeypatch, voltas=2)

        assert not [a for a in alertas if a.get("type") == "mvrv_stale"]

    async def test_serie_em_dia_nao_avisa(self, settings, monkeypatch):
        await _gravar_serie(settings, ultimo_ponto_ha_dias=1)
        orch = self._orquestrador(settings, limiar=0.8)

        alertas = await self._rodar_loop(orch, monkeypatch, voltas=2)

        assert not [a for a in alertas if a.get("type") == "mvrv_stale"]


class TestDiaUtcESerieForaDeOrdem:
    """Auditoria de 09/11/2026 (dados, low): `moment.date()` sem normalizar para
    UTC e `reading_from_series` supondo a serie ordenada."""

    def test_instante_em_outro_fuso_le_o_dia_utc(self):
        from datetime import date, timezone

        from crypto_traders.onchain import reading_from_series

        serie = [(date(2026, 11, 1), 1.0), (date(2026, 11, 2), 5.0)]
        # 22:00 de 01/11 em Brasilia ja e 02/11 em UTC: o ponto de 02/11 vale.
        brasilia = timezone(timedelta(hours=-3))
        leitura = reading_from_series(serie, datetime(2026, 11, 1, 22, tzinfo=brasilia))
        assert leitura is not None and leitura.value == 5.0
        # E o mesmo instante em UTC da o mesmo dia.
        em_utc = datetime(2026, 11, 2, 1, tzinfo=UTC)
        assert reading_from_series(serie, em_utc).value == leitura.value

    def test_serie_fora_de_ordem_usa_o_ponto_mais_novo(self):
        from datetime import date

        from crypto_traders.onchain import reading_from_series

        ordenada = [(date(2026, 11, d), float(d)) for d in range(1, 11)]
        embaralhada = list(reversed(ordenada))
        momento = datetime(2026, 11, 10, 12, tzinfo=UTC)
        assert (
            reading_from_series(embaralhada, momento).value
            == reading_from_series(ordenada, momento).value
            == 10.0
        )
