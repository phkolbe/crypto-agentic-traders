"""Ataques do critico ao item 3 (Risk Manager, o guardiao).

Cada teste aqui descreve uma brecha que o critico encontrou NO CODIGO ATUAL,
depois da rodada 1 de correcoes. Se um deles passar, a brecha foi fechada.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import structlog.testing

from crypto_traders.agents.risk_manager import RiskManagerAgent
from crypto_traders.bus import InMemoryEventBus, Topics
from crypto_traders.domain.enums import ExchangeName, SignalDirection
from crypto_traders.domain.models import PortfolioSnapshot, Position, Signal

AGORA = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


async def agente(settings, stop=0.05, alvo=0.10) -> RiskManagerAgent:
    bus = InMemoryEventBus()
    await bus.start()
    configurado = settings.model_copy(
        update={
            "risk": settings.risk.model_copy(
                update={"stop_loss_pct": stop, "take_profit_pct": alvo}
            )
        }
    )
    return RiskManagerAgent(bus, configurado)


def snapshot(medio, atual, quantidade="0.01", quando=None) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        total_value=Decimal("1000"),
        cash_value=Decimal("500"),
        positions_value=Decimal("500"),
        timestamp=quando or AGORA,
        positions=[
            Position(
                exchange=ExchangeName.BINANCE,
                asset="BTC",
                quantity=Decimal(quantidade),
                average_price=Decimal(medio) if medio is not None else None,
                current_price=Decimal(atual),
            )
        ],
    )


class TestAtaqueOrdemRecusadaSemLinhaEmOrders:
    """A recusa que NAO grava linha em `orders` prende a posicao para sempre.

    O implementador nomeou tres causas de recusa da saida de protecao: "IP fora
    da whitelist, valor abaixo do minimo, credencial sem permissao de spot". A
    do meio -- valor abaixo do minimo da exchange -- e recusada pelo Execution
    Agent em `_refuse_by_filter`, que registra em `audit_log` e NAO grava linha
    em `orders` ("Recusa ANTES de gravar PENDING", execution.py:186). Nenhum
    dos sete testes de `TestTheExitLockIsNotAPrison` cobre esse desfecho: todos
    chamam `create_pending` antes de conferir.

    Sem linha em `orders`, `_reconcile_exits` ve `status = None`, que nao esta
    em `SAIDA_SEM_VENDA` nem em (FILLED, PARTIALLY_FILLED), e cai no ramo
    "sem desfecho" -- que NAO solta a trava, NAO conta tentativa, NAO desiste e
    NAO alerta. A posicao fica aberta, sem stop, travada, para sempre.
    """

    async def test_recusa_sem_linha_em_orders_nunca_tenta_de_novo(self, settings):
        a = await agente(settings, stop=0.05)
        primeira = await a.enforce_protective_exits(snapshot("100", "94"))
        assert len(primeira) == 1, "a primeira saida tem de ser emitida"

        # A exchange recusou por MIN_NOTIONAL. Execution nao gravou linha em
        # `orders` -- so em `audit_log`. Nada mais acontece.
        emitidas: list[int] = []
        for minutos in (1, 5, 20, 60, 240, 1440):
            quando = AGORA + timedelta(minutes=minutos)
            ordens = await a.enforce_protective_exits(
                snapshot("100", "80", quando=quando)
            )
            emitidas.append(len(ordens))

        assert sum(emitidas) > 0, (
            "posicao 20% abaixo do stop, ordem de protecao recusada sem linha em "
            "`orders`, e o sistema nunca tentou de novo nem em 24h: "
            f"{sum(emitidas)} novas ordens. A trava e uma prisao neste caminho."
        )

    async def test_recusa_sem_linha_em_orders_avisa_o_dono(self, settings):
        a = await agente(settings, stop=0.05)
        await a.enforce_protective_exits(snapshot("100", "94"))

        alertas: list[dict] = []

        async def escuta():
            async for alerta in a.bus.subscribe(Topics.ALERTS):
                alertas.append(alerta)

        tarefa = asyncio.create_task(escuta())
        await asyncio.sleep(0)
        for minutos in (1, 20, 60, 1440):
            await a.enforce_protective_exits(
                snapshot("100", "80", quando=AGORA + timedelta(minutes=minutos))
            )
        await asyncio.sleep(0.05)
        tarefa.cancel()

        tipos = {al.get("type") for al in alertas}
        assert tipos & {"protective_exit_failed", "protective_exit_impossible"}, (
            "a posicao esta travada e sem stop, e o unico alerta publicado pelo "
            f"guardiao foi o de que a saida foi EMITIDA: {tipos}"
        )

    async def test_a_ordem_parada_nao_alerta_1440_vezes_por_dia(self, settings):
        """`risk.protective_exit_stalled` sai a cada retrato, sem dedup.

        E a patologia do achado 1 do ensaio (1.440 avisos/dia), no mesmo agente
        que deduplicou `position_without_cost_basis` e
        `loss_not_confirmed_by_equity` exatamente por causa dela.
        """
        a = await agente(settings, stop=0.05)
        await a.enforce_protective_exits(snapshot("100", "94"))

        with structlog.testing.capture_logs() as logs:
            for minutos in range(11, 31):
                await a.enforce_protective_exits(
                    snapshot("100", "80", quando=AGORA + timedelta(minutes=minutos))
                )

        parados = [ev for ev in logs if ev.get("event") == "risk.protective_exit_stalled"]
        assert len(parados) <= 2, (
            f"20 retratos produziram {len(parados)} linhas de "
            "`risk.protective_exit_stalled` -- uma por retrato, para sempre"
        )


class TestAtaqueDepositoSilenciaOCircuitBreaker:
    """A perda confirmada pelas DUAS medidas engole a perda real de um deposito.

    `perda = min(queda_do_resultado, queda_do_patrimonio)`. Um deposito no mesmo
    periodo levanta o patrimonio, a queda de patrimonio vira negativa, e a trava
    nao dispara -- mesmo com o capital AUTORIZADO inteiro perdido em negociacao.
    """

    async def _referencias(self, settings, resultado, patrimonio, quando):
        from crypto_traders.db.repositories import PortfolioSnapshotRepository
        from crypto_traders.db.session import session_scope

        async with session_scope(settings) as session:
            await PortfolioSnapshotRepository(session).save(
                PortfolioSnapshot(
                    total_value=Decimal(patrimonio),
                    cash_value=Decimal(patrimonio),
                    positions_value=Decimal(0),
                    realized_pnl=Decimal(resultado),
                    unrealized_pnl=Decimal(0),
                    timestamp=quando,
                    positions=[],
                ),
                "dry_run",
            )

    async def test_perda_total_do_autorizado_com_deposito_no_mesmo_dia(self, settings):
        limites = settings.risk.model_copy(
            update={
                "authorized_capital": Decimal("100"),
                "daily_loss_limit_pct": 0.05,
            }
        )
        a = await agente(settings)
        a._limits = limites

        # Comeco do dia: nenhum resultado, patrimonio 100.
        await self._referencias(
            settings, "0", "100", datetime(2026, 9, 9, 0, 5, tzinfo=UTC)
        )

        # Agora: perdeu 50 negociando (metade do capital autorizado, dez vezes o
        # limite diario de 5%) e o dono depositou 200 no mesmo dia.
        agora = PortfolioSnapshot(
            total_value=Decimal("250"),
            cash_value=Decimal("250"),
            positions_value=Decimal(0),
            realized_pnl=Decimal("-50"),
            unrealized_pnl=Decimal(0),
            timestamp=datetime(2026, 9, 9, 18, 0, tzinfo=UTC),
            positions=[],
        )
        motivo = await a.check_circuit_breaker(agora)

        assert motivo is not None, (
            "resultado de negociacao caiu 50 sobre 100 autorizados (50%, limite "
            "5%) e a trava nao disparou porque um deposito mascarou a queda de "
            "patrimonio"
        )


class TestAtaqueLoteComDoisSinaisNoMesmoAtivo:
    """Dois sinais simultaneos no mesmo ativo, sem cooldown."""

    async def test_dois_longs_no_mesmo_ativo_nao_estouram_a_exposicao(self, settings):
        from crypto_traders.config import RiskSettings
        from crypto_traders.risk.rules import PortfolioState, RiskEngine

        limites = RiskSettings(
            max_order_notional=None,
            max_order_pct_portfolio=0.20,
            max_asset_exposure_pct=0.45,
            max_open_positions=None,
            min_order_notional=Decimal("5"),
            stop_loss_pct=0.05,
            take_profit_pct=0.10,
            daily_loss_limit_pct=0.05,
            weekly_loss_limit_pct=0.12,
            min_signal_confidence=0.55,
            cooldown_seconds=0,
            symbol_whitelist=["BTC/USDT"],
            asset_whitelist=["BTC", "USDT"],
            authorized_capital=Decimal("100"),
        )
        motor = RiskEngine(limites, "USDT")
        estado = PortfolioState(
            total_value=Decimal("100"),
            cash=Decimal("100"),
            positions={},
            prices={"BTC": Decimal("100")},
        )
        sinais = [
            Signal(
                exchange=ExchangeName.BINANCE,
                symbol="BTC/USDT",
                timeframe="1d",
                strategy="a",
                direction=SignalDirection.LONG,
                confidence=0.9,
                reason="um",
                reference_price=Decimal("100"),
            ),
            Signal(
                exchange=ExchangeName.BINANCE,
                symbol="BTC/USDT",
                timeframe="1d",
                strategy="b",
                direction=SignalDirection.LONG,
                confidence=0.8,
                reason="dois",
                reference_price=Decimal("100"),
            ),
        ]
        resultados = motor.evaluate_batch(sinais, estado, AGORA)
        aprovados = [a for _, a in resultados if a.decision.value == "approved"]
        total = sum((a.approved_notional or Decimal(0)) for a in aprovados)
        assert total <= Decimal("45"), (
            f"dois sinais no mesmo ativo aprovaram {total} contra o teto de "
            "exposicao de 45"
        )


class TestFluxoExternoNaoMexeNaMedicao:
    """Regressao do conserto: a testemunha nao depoe sobre dinheiro que entrou.

    O conserto do circuit breaker trocou patrimonio bruto por resultado de
    negociacao para nao ler um SAQUE como prejuizo, e passou a exigir que o
    patrimonio confirmasse a queda. A confirmacao abriu o buraco simetrico
    (`TestAtaqueDepositoSilenciaOCircuitBreaker`). Estes testes fixam as duas
    bordas do conserto de uma vez: nem o saque inventa perda, nem o aporte a
    apaga, e o artefato de contabilidade -- que nao move dinheiro -- continua
    protegido.
    """

    async def _referencia(self, settings, resultado, patrimonio, quando):
        from crypto_traders.db.repositories import PortfolioSnapshotRepository
        from crypto_traders.db.session import session_scope

        async with session_scope(settings) as session:
            await PortfolioSnapshotRepository(session).save(
                PortfolioSnapshot(
                    total_value=Decimal(patrimonio),
                    cash_value=Decimal(patrimonio),
                    positions_value=Decimal(0),
                    realized_pnl=Decimal(resultado),
                    unrealized_pnl=Decimal(0),
                    timestamp=quando,
                    positions=[],
                ),
                "dry_run",
            )

    async def _agente_com_portao(self, settings, autorizado="100"):
        a = await agente(settings)
        a._limits = settings.risk.model_copy(
            update={
                "authorized_capital": Decimal(autorizado),
                "daily_loss_limit_pct": 0.05,
            }
        )
        await self._referencia(
            settings, "0", "100", datetime(2026, 9, 9, 0, 5, tzinfo=UTC)
        )
        return a

    def _retrato(self, total, resultado, posicoes="0"):
        return PortfolioSnapshot(
            total_value=Decimal(total),
            cash_value=Decimal(total) - Decimal(posicoes),
            positions_value=Decimal(posicoes),
            realized_pnl=Decimal(resultado),
            unrealized_pnl=Decimal(0),
            timestamp=datetime(2026, 9, 9, 18, 0, tzinfo=UTC),
            positions=[],
        )

    async def test_saque_no_mesmo_dia_nao_esconde_a_perda(self, settings):
        """O outro fluxo externo: sair dinheiro nao pode mascarar nem inventar."""
        a = await self._agente_com_portao(settings)
        # Perdeu 10 negociando (10% de 100 autorizados) e sacou 30.
        motivo = await a.check_circuit_breaker(self._retrato("60", "-10"))
        assert motivo is not None, "saque no mesmo dia escondeu a perda de negociacao"

    async def test_ruido_de_marcacao_nao_anula_a_testemunha(self, settings):
        """Alta pequena com posicao aberta e preco, nao aporte.

        A testemunha existe para o artefato de contabilidade (ordem em voo,
        lancamento apagado), que NAO move dinheiro. Se qualquer centavo de alta
        anulasse a testemunha, o falso disparo voltaria pela porta da frente.
        """
        a = await self._agente_com_portao(settings)
        # +5 de patrimonio com 20 em posicao aberta: cabe no ruido de marcacao
        # (min_order_notional = 10), entao a testemunha continua valendo e a
        # queda do resultado de negociacao NAO esta confirmada.
        assert await a.check_circuit_breaker(self._retrato("105", "-10", "20")) is None
        assert not a.circuit_breaker_active

    async def test_alta_maior_que_o_ruido_e_dinheiro_de_fora(self, settings):
        """Com posicao aberta tambem: 30 de alta nao e marcacao de 20 em posicao."""
        a = await self._agente_com_portao(settings)
        motivo = await a.check_circuit_breaker(self._retrato("130", "-10", "20"))
        assert motivo is not None, (
            "patrimonio subiu 30 com 20 em posicao aberta -- mais do que a "
            "marcacao pode explicar -- e a perda de negociacao foi engolida"
        )

    @pytest.mark.xfail(
        reason=(
            "aporte MENOR que a perda ainda mascara a parte que ele cobre: com "
            "so duas series (patrimonio e resultado de negociacao) um aporte de "
            "8 contra uma perda de 10 e aritmeticamente identico a um artefato "
            "de contabilidade de 8, e o artefato tem de continuar protegido. "
            "Fechar exige um livro de fluxos (deposito/saque registrados), que "
            "nao existe no sistema e nao mora nos arquivos deste grupo."
        ),
        strict=True,
    )
    async def test_aporte_menor_que_a_perda_ainda_mascara_a_diferenca(self, settings):
        a = await self._agente_com_portao(settings)
        # Perdeu 10 (10% de 100 autorizados, limite 5%) e aportou 8: 100-10+8.
        motivo = await a.check_circuit_breaker(self._retrato("98", "-10"))
        assert motivo is not None


class TestOAvisoDaOrdemParadaSaiUmaVezPorTransicao:
    """A ordem que EXISTE em `orders` e nao tem desfecho: trava mantida, aviso um.

    Aqui a trava fica armada de proposito -- revender por cima de uma ordem viva
    duplicaria a venda -- e por isso o estado pode durar horas. O aviso e a unica
    coisa que impede a duvida de ficar sem dono, e um aviso por retrato (1.440
    por dia) e a forma de garantir que ninguem o leia.
    """

    async def _gravar_pendente(self, settings, request):
        from crypto_traders.db.repositories import OrderRepository
        from crypto_traders.db.session import session_scope

        async with session_scope(settings) as session:
            await OrderRepository(session).create_pending(request, "dry_run")

    async def test_a_ordem_viva_e_parada_avisa_uma_vez(self, settings):
        a = await agente(settings, stop=0.05)
        primeira = await a.enforce_protective_exits(snapshot("100", "94"))
        await self._gravar_pendente(settings, primeira[0])

        with structlog.testing.capture_logs() as logs:
            for minutos in range(11, 31):
                assert await a.enforce_protective_exits(
                    snapshot("100", "80", quando=AGORA + timedelta(minutes=minutos))
                ) == [], "reemitir por cima de uma ordem viva duplicaria a venda"

        parados = [
            ev for ev in logs if ev.get("event") == "risk.protective_exit_stalled"
        ]
        assert len(parados) == 1, (
            f"20 retratos com a MESMA ordem parada produziram {len(parados)} avisos"
        )
        assert "BTC" in a._exiting
