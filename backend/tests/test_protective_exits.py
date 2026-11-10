"""Stop-loss e take-profit no backtest.

Estes testes existem por causa de um defeito que nao se manifestava como erro:
os niveis eram calculados pelo Risk Manager, gravados na ordem e **nunca
comparados com preco nenhum**. Toda posicao andava ate a estrategia mandar sair,
e as quedas maximas medidas eram o retrato de um sistema sem stop -- justamente
o numero que o stop existe para limitar.

Um parametro que nao faz nada e invisivel no teste tradicional: a suite passava
inteira com o stop inerte. O que denunciou foi uma varredura de 768 combinacoes
em que mudar `stop_loss_pct` nao alterou **um unico** resultado.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_traders.backtest.portfolio import PortfolioBacktestEngine
from crypto_traders.config import RiskSettings
from crypto_traders.domain.enums import ExchangeName, SignalDirection
from crypto_traders.domain.models import Candle, Signal
from crypto_traders.strategies.base import MarketFrame, Strategy

INICIO = datetime(2026, 1, 1, tzinfo=UTC)


def candle(i: int, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(
        exchange=ExchangeName.BINANCE,
        symbol="BTC/USDT",
        timeframe="1d",
        open_time=INICIO + timedelta(days=i),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        volume=Decimal("10"),
    )


class CompraUmaVez(Strategy):
    """Compra no primeiro candle apos o aquecimento e nunca mais opina.

    Isola a protecao: se a posicao fechar, foi o stop ou o alvo -- nao existe
    outra saida possivel.
    """

    name = "compra_uma_vez"
    description = "teste"
    min_candles = 3

    def __init__(self) -> None:
        self._comprou = False

    def evaluate(self, market: MarketFrame) -> Signal | None:
        if self._comprou:
            return None
        self._comprou = True
        return Signal(
            exchange=market.exchange,
            symbol=market.symbol,
            timeframe=market.timeframe,
            strategy=self.name,
            direction=SignalDirection.LONG,
            confidence=0.9,
            reason="teste",
            reference_price=Decimal(str(market.frame["close"].iloc[-1])),
        )


def limites(stop: float, alvo: float) -> RiskSettings:
    return RiskSettings(
        max_order_notional=Decimal("1000"),
        max_order_pct_portfolio=0.9,
        max_asset_exposure_pct=0.9,
        max_open_positions=3,
        min_order_notional=Decimal("10"),
        stop_loss_pct=stop,
        take_profit_pct=alvo,
        daily_loss_limit_pct=0.9,
        weekly_loss_limit_pct=0.9,
        min_signal_confidence=0.5,
        cooldown_seconds=0,
        symbol_whitelist=["BTC/USDT"],
        asset_whitelist=["BTC"],
    )


def motor(stop: float = 0.05, alvo: float = 0.10) -> PortfolioBacktestEngine:
    return PortfolioBacktestEngine(
        [CompraUmaVez()],
        limites(stop, alvo),
        quote_currency="USDT",
        initial_balance=Decimal("1000"),
        fee_pct=Decimal("0"),
        slippage_pct=Decimal("0"),
        lookback=50,
    )


#: Candles planos em 100. A compra sai no quarto (indice 3, o primeiro apos o
#: aquecimento de 3); os demais existem porque o motor recusa series curtas.
AQUECIMENTO = [candle(i, 100, 100, 100, 100) for i in range(6)]
PRIMEIRO = len(AQUECIMENTO)  # indice do primeiro candle de teste


class TestStopLoss:
    async def test_fires_when_the_low_breaches_it(self):
        """Queda de 8% com stop em 5%: a posicao tem de fechar."""
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 100, 100, 92, 93)]
        r = await motor(stop=0.05).run({"BTC/USDT": candles})
        assert r.exits["stop"] == 1
        saida = next(t for t in r.trades if t.exit_reason == "stop")
        assert saida.price == pytest.approx(Decimal("95"))

    async def test_does_not_fire_above_the_level(self):
        """Queda de 3% com stop em 5%: a posicao continua aberta."""
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 100, 100, 97, 98)]
        r = await motor(stop=0.05).run({"BTC/USDT": candles})
        assert r.exits["stop"] == 0

    async def test_a_gap_fills_at_the_open_not_at_the_level(self):
        """Abriu a 80 com stop em 95: executa a 80.

        Executar no nivel do stop seria creditar ao sistema um preco que nunca
        existiu naquele candle. E exatamente assim que stops machucam de verdade.
        """
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 80, 82, 78, 81)]
        r = await motor(stop=0.05).run({"BTC/USDT": candles})
        saida = next(t for t in r.trades if t.exit_reason == "stop")
        assert saida.price == pytest.approx(Decimal("80"))

    async def test_tighter_stop_fires_earlier(self):
        """O parametro precisa MUDAR o resultado -- era o que nao acontecia."""
        candles = [
            *AQUECIMENTO,
            candle(PRIMEIRO, 100, 100, 96, 97),
            candle(PRIMEIRO + 1, 97, 98, 90, 91),
        ]
        # Mesmo alvo nos dois (e longe demais para disparar) para isolar o stop:
        # `take_profit` tem de ser maior que `stop_loss`, senao a validacao
        # recusa a configuracao.
        apertado = await motor(stop=0.03, alvo=0.30).run({"BTC/USDT": candles})
        largo = await motor(stop=0.15, alvo=0.30).run({"BTC/USDT": candles})
        assert apertado.exits["stop"] == 1
        assert largo.exits["stop"] == 0
        assert apertado.total_return_pct != largo.total_return_pct


class TestTakeProfit:
    async def test_fires_when_the_high_reaches_it(self):
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 100, 112, 100, 110)]
        r = await motor(alvo=0.10).run({"BTC/USDT": candles})
        assert r.exits["alvo"] == 1
        saida = next(t for t in r.trades if t.exit_reason == "alvo")
        assert saida.price == pytest.approx(Decimal("110"))

    async def test_a_favourable_gap_gets_no_credit(self):
        """Abriu a 130 com alvo em 110: executa em 110, nao em 130.

        Assimetria deliberada em relacao ao gap contrario do stop: o backtest
        erra sempre para o lado pessimista.
        """
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 130, 135, 128, 132)]
        r = await motor(alvo=0.10).run({"BTC/USDT": candles})
        saida = next(t for t in r.trades if t.exit_reason == "alvo")
        assert saida.price == pytest.approx(Decimal("110"))


class TestBothInTheSameCandle:
    async def test_the_stop_wins(self):
        """O OHLC nao diz qual preco veio primeiro.

        Supor o alvo seria escolher o desfecho bom com base em informacao que
        nao existe -- e um backtest de risco nao pode errar para o otimista.
        """
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 100, 115, 90, 100)]
        r = await motor(stop=0.05, alvo=0.10).run({"BTC/USDT": candles})
        assert r.exits["stop"] == 1
        assert r.exits["alvo"] == 0


class TestDrawdownIsNowBounded:
    async def test_the_stop_limits_the_fall(self):
        """A razao de tudo isto: sem stop a posicao ia ao fundo do periodo.

        A serie cai 45% de forma continua. Com stop em 5% a perda fica perto
        disso; sem protecao, o prejuizo seria a queda inteira.
        """
        queda = [candle(PRIMEIRO + i, 100 - i * 5, 100 - i * 5, 95 - i * 5, 95 - i * 5)
                 for i in range(9)]
        r = await motor(stop=0.05).run({"BTC/USDT": AQUECIMENTO + queda})
        assert r.exits["stop"] == 1
        # Sem protecao a carteira teria seguido a queda de 45%; com stop em 5%
        # o prejuizo fica em uma fracao disso.
        assert r.total_return_pct > -0.10, r.total_return_pct
        assert r.max_drawdown_pct < 0.10, r.max_drawdown_pct


class TestBookkeeping:
    async def test_the_exit_is_recorded_as_a_closed_trade(self):
        candles = [*AQUECIMENTO, candle(PRIMEIRO, 100, 100, 92, 93)]
        r = await motor(stop=0.05).run({"BTC/USDT": candles})
        fechadas = r.closed_trades
        assert len(fechadas) == 1
        assert fechadas[0].realized_pnl is not None
        assert fechadas[0].realized_pnl < 0

    async def test_the_position_is_fully_closed(self):
        """Meia posicao sobrando viraria exposicao invisivel."""
        candles = [
            *AQUECIMENTO,
            candle(PRIMEIRO, 100, 100, 92, 93),
            candle(PRIMEIRO + 1, 93, 94, 92, 93),
        ]
        r = await motor(stop=0.05).run({"BTC/USDT": candles})
        # Nada mais a vender: uma segunda passagem nao gera nova saida.
        assert r.exits["stop"] == 1

    async def test_no_exit_without_a_position(self):
        """Serie que so cai, sem compra nenhuma: nada a proteger."""

        class NuncaCompra(Strategy):
            name = "nunca"
            description = "teste"
            min_candles = 3

            def evaluate(self, market):
                return None

        engine = PortfolioBacktestEngine(
            [NuncaCompra()], limites(0.05, 0.10),
            quote_currency="USDT", initial_balance=Decimal("1000"),
            fee_pct=Decimal("0"), slippage_pct=Decimal("0"), lookback=50,
        )
        r = await engine.run({"BTC/USDT": [*AQUECIMENTO, candle(PRIMEIRO, 100, 100, 50, 55)]})
        assert r.exits["stop"] == 0
        assert r.total_return_pct == 0.0


class TestProductionProtectiveExits:
    """A protecao em producao, no Risk Manager.

    Escolhida em vez de OCO na exchange, com uma limitacao aceita
    deliberadamente: morre junto com o processo. Cobre oscilacao de mercado, nao
    queda de infraestrutura.
    """

    async def _agente(self, settings, stop=0.05, alvo=0.10):
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        bus = InMemoryEventBus()
        await bus.start()
        configurado = settings.model_copy(
            update={"risk": settings.risk.model_copy(update={
                "stop_loss_pct": stop, "take_profit_pct": alvo,
            })}
        )
        return RiskManagerAgent(bus, configurado)

    def _snapshot(self, medio: str | None, atual: str, quantidade: str = "0.01"):
        from crypto_traders.domain.models import PortfolioSnapshot, Position

        return PortfolioSnapshot(
            total_value=Decimal("1000"),
            cash_value=Decimal("500"),
            positions_value=Decimal("500"),
            positions=[
                Position(
                    exchange=ExchangeName.BINANCE,
                    asset="BTC",
                    quantity=Decimal(quantidade),
                    average_price=Decimal(medio) if medio is not None else None,
                    current_price=Decimal(atual),
                ),
            ],
        )

    async def test_closes_the_position_below_the_stop(self, settings):
        agente = await self._agente(settings, stop=0.05)
        ordens = await agente.enforce_protective_exits(self._snapshot("100", "94"))
        assert len(ordens) == 1
        assert ordens[0].symbol == "BTC/USDT"
        assert str(ordens[0].side) == "sell"
        assert ordens[0].quantity == Decimal("0.01")

    async def test_closes_the_position_above_the_target(self, settings):
        agente = await self._agente(settings, alvo=0.10)
        ordens = await agente.enforce_protective_exits(self._snapshot("100", "111"))
        assert len(ordens) == 1

    async def test_does_nothing_between_the_levels(self, settings):
        agente = await self._agente(settings, stop=0.05, alvo=0.10)
        assert await agente.enforce_protective_exits(self._snapshot("100", "102")) == []

    async def test_does_not_reemit_while_the_order_is_in_flight(self, settings):
        """Cada snapshot chega a cada 60s; sem trava, venderia a posicao varias vezes."""
        agente = await self._agente(settings, stop=0.05)
        snapshot = self._snapshot("100", "94")
        assert len(await agente.enforce_protective_exits(snapshot)) == 1
        assert await agente.enforce_protective_exits(snapshot) == []

    async def test_the_lock_clears_when_the_position_is_gone(self, settings):
        """Posicao liquidada libera a trava, senao uma reentrada ficaria sem stop."""
        from crypto_traders.domain.models import PortfolioSnapshot

        agente = await self._agente(settings, stop=0.05)
        assert len(await agente.enforce_protective_exits(self._snapshot("100", "94"))) == 1

        vazio = PortfolioSnapshot(
            total_value=Decimal("1000"), cash_value=Decimal("1000"),
            positions_value=Decimal(0), positions=[],
        )
        await agente.enforce_protective_exits(vazio)
        assert len(await agente.enforce_protective_exits(self._snapshot("100", "94"))) == 1

    async def test_a_position_without_average_price_is_skipped(self, settings):
        """Sem preco medio nao existe nivel. Chutar um seria pior que nao agir."""
        agente = await self._agente(settings, stop=0.05)
        assert await agente.enforce_protective_exits(self._snapshot(None, "10")) == []

    async def test_the_quote_currency_is_never_closed(self, settings):
        """O caixa nao e posicao: vender USDT contra USDT nao existe."""
        from crypto_traders.domain.models import PortfolioSnapshot, Position

        agente = await self._agente(settings, stop=0.05)
        snapshot = PortfolioSnapshot(
            total_value=Decimal("1000"), cash_value=Decimal("1000"),
            positions_value=Decimal(0),
            positions=[Position(
                exchange=ExchangeName.BINANCE, asset="USDT",
                quantity=Decimal("1000"), average_price=Decimal("1"),
                current_price=Decimal("1"),
            )],
        )
        assert await agente.enforce_protective_exits(snapshot) == []

    async def test_a_manually_bought_position_is_also_protected(self, settings):
        """O preco medio vem do historico de trades, que inclui lancamento manual.

        O Risk Manager guarda a carteira, nao apenas as ordens que ele originou.
        """
        agente = await self._agente(settings, stop=0.05)
        ordens = await agente.enforce_protective_exits(self._snapshot("100", "90"))
        assert len(ordens) == 1
        assert ordens[0].signal_id is None
        assert ordens[0].strategy == "protecao"

    async def test_the_order_carries_a_risk_event_id(self, settings):
        """O Execution Agent recusa `OrderRequest` sem ele."""
        agente = await self._agente(settings, stop=0.05)
        ordens = await agente.enforce_protective_exits(self._snapshot("100", "94"))
        assert ordens[0].risk_event_id


class TestTheExitLockIsNotAPrison:
    """A trava que impede vender duas vezes nao pode impedir vender uma vez.

    Ela caia por um unico criterio -- o ativo desaparecer da carteira -- e isso
    confunde "a saida saiu" com "a saida foi tentada". Medido: com a ordem de
    protecao recusada pela exchange, a posicao continuava aberta, a trava
    continuava armada e nenhum outro stop era tentado para aquele ativo enquanto
    o processo vivesse, com um `risk.protective_exit` no log dizendo que a
    protecao agiu.
    """

    async def _agente(self, settings, stop=0.05, alvo=0.10):
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        bus = InMemoryEventBus()
        await bus.start()
        configurado = settings.model_copy(
            update={"risk": settings.risk.model_copy(update={
                "stop_loss_pct": stop, "take_profit_pct": alvo,
            })}
        )
        return RiskManagerAgent(bus, configurado)

    def _snapshot(self, medio, atual, quantidade="0.01", quando=None):
        from crypto_traders.domain.models import PortfolioSnapshot, Position

        agora = quando or datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
        return PortfolioSnapshot(
            total_value=Decimal("1000"),
            cash_value=Decimal("500"),
            positions_value=Decimal("500"),
            timestamp=agora,
            positions=[
                Position(
                    exchange=ExchangeName.BINANCE,
                    asset="BTC",
                    quantity=Decimal(quantidade),
                    average_price=Decimal(medio) if medio is not None else None,
                    current_price=Decimal(atual),
                ),
            ],
        )

    async def _desfecho(self, settings, request, status):
        """Grava a ordem emitida com o desfecho que a exchange teria dado."""
        from crypto_traders.db.repositories import OrderRepository
        from crypto_traders.db.session import session_scope

        async with session_scope(settings) as session:
            order = await OrderRepository(session).create_pending(request, "dry_run")
            order.status = str(status)

    async def test_a_refused_exit_is_tried_again(self, settings):
        """IP fora da whitelist, valor abaixo do minimo, credencial sem spot."""
        from crypto_traders.domain.enums import OrderStatus

        agente = await self._agente(settings, stop=0.05)
        primeira = await agente.enforce_protective_exits(self._snapshot("100", "94"))
        assert len(primeira) == 1
        await self._desfecho(settings, primeira[0], OrderStatus.REJECTED)

        depois = self._snapshot(
            "100", "90", quando=datetime(2026, 9, 9, 12, 1, tzinfo=UTC)
        )
        assert len(await agente.enforce_protective_exits(depois)) == 1

    async def test_a_refused_exit_alerts_that_the_position_is_open(self, settings):
        """A posicao segue exposta: e uma protecao que nao agiu, nao um detalhe."""
        import asyncio

        from crypto_traders.bus import Topics
        from crypto_traders.domain.enums import OrderStatus

        agente = await self._agente(settings, stop=0.05)
        primeira = await agente.enforce_protective_exits(self._snapshot("100", "94"))
        await self._desfecho(settings, primeira[0], OrderStatus.FAILED)

        alertas: list[dict] = []

        async def escuta():
            async for alerta in agente.bus.subscribe(Topics.ALERTS):
                alertas.append(alerta)

        tarefa = asyncio.create_task(escuta())
        await asyncio.sleep(0)
        await agente.enforce_protective_exits(
            self._snapshot("100", "90", quando=datetime(2026, 9, 9, 12, 1, tzinfo=UTC))
        )
        await asyncio.sleep(0.05)
        tarefa.cancel()

        assert "protective_exit_failed" in [a["type"] for a in alertas]

    async def test_a_partial_fill_leaves_the_rest_protected(self, settings):
        """Vendeu parte: o restante e posicao aberta como qualquer outra.

        Quantidades maiores que as dos vizinhos de proposito: com 0,004 a 90 o
        resto valia 0,36, abaixo do minimo negociavel -- poeira, que desde
        09/11/2026 nao recebe stop (`TestPoeiraDepoisDoStop`).
        """
        from crypto_traders.domain.enums import OrderStatus

        agente = await self._agente(settings, stop=0.05)
        primeira = await agente.enforce_protective_exits(
            self._snapshot("100", "94", quantidade="1")
        )
        await self._desfecho(settings, primeira[0], OrderStatus.PARTIALLY_FILLED)

        resto = await agente.enforce_protective_exits(
            self._snapshot(
                "100", "90", quantidade="0.4",
                quando=datetime(2026, 9, 9, 12, 1, tzinfo=UTC),
            )
        )
        assert len(resto) == 1
        assert resto[0].quantity == Decimal("0.4")

    async def test_an_order_without_an_outcome_is_never_resold(self, settings):
        """Revender por cima de uma ordem viva duplicaria a venda."""
        from crypto_traders.domain.enums import OrderStatus

        agente = await self._agente(settings, stop=0.05)
        primeira = await agente.enforce_protective_exits(self._snapshot("100", "94"))
        await self._desfecho(settings, primeira[0], OrderStatus.PENDING)

        for minutos in (1, 5, 30, 600):
            quando = datetime(2026, 9, 9, 12, 0, tzinfo=UTC) + timedelta(minutes=minutos)
            assert await agente.enforce_protective_exits(
                self._snapshot("100", "90", quando=quando)
            ) == []

    async def test_a_stalled_order_keeps_the_lock(self, settings):
        """Nao solta a trava, mas nao deixa a duvida sem dono (ver o log)."""
        from crypto_traders.domain.enums import OrderStatus

        agente = await self._agente(settings, stop=0.05)
        primeira = await agente.enforce_protective_exits(self._snapshot("100", "94"))
        await self._desfecho(settings, primeira[0], OrderStatus.OPEN)

        await agente.enforce_protective_exits(
            self._snapshot("100", "90", quando=datetime(2026, 9, 9, 12, 30, tzinfo=UTC))
        )
        assert "BTC" in agente._exiting

    async def test_a_filled_exit_releases_the_lock_for_a_reentry(self, settings):
        """Posicao liquidada: a saida cumpriu, e uma reentrada tem stop novo."""
        from crypto_traders.domain.models import PortfolioSnapshot

        agente = await self._agente(settings, stop=0.05)
        await agente.enforce_protective_exits(self._snapshot("100", "94"))

        vazio = PortfolioSnapshot(
            total_value=Decimal("1000"), cash_value=Decimal("1000"),
            positions_value=Decimal(0), positions=[],
            timestamp=datetime(2026, 9, 9, 12, 1, tzinfo=UTC),
        )
        await agente.enforce_protective_exits(vazio)
        assert agente._exiting == set()
        assert agente._exits_in_flight == {}


class TestPositionsTheSoftwareStopCannotSee:
    """Sem preco medio nao existe nivel -- e isso nao pode ser silencioso.

    O caso e concreto e atual: o preco medio e reconstruido do historico de
    trades na moeda de cotacao CORRENTE, entao a troca de BRL para USDC (D24)
    deixa a posicao herdada sem base de custo. O codigo pulava a posicao sem uma
    linha de log, e a tabela da secao 12 do `docs/SEGURANCA.md` nao lista este
    limite entre os que o stop em software nao cobre.
    """

    async def _agente(self, settings):
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        bus = InMemoryEventBus()
        await bus.start()
        return RiskManagerAgent(bus, settings)

    def _snapshot(self, *ativos_sem_base: str):
        from crypto_traders.domain.models import PortfolioSnapshot, Position

        return PortfolioSnapshot(
            total_value=Decimal("1000"), cash_value=Decimal("500"),
            positions_value=Decimal("500"),
            positions=[
                Position(
                    exchange=ExchangeName.BINANCE, asset=asset,
                    quantity=Decimal("1"), average_price=None,
                    current_price=Decimal("50"),
                )
                for asset in ativos_sem_base
            ],
        )

    async def _alertas(self, agente, *snapshots):
        import asyncio

        from crypto_traders.bus import Topics

        recebidos: list[dict] = []

        async def escuta():
            async for alerta in agente.bus.subscribe(Topics.ALERTS):
                recebidos.append(alerta)

        tarefa = asyncio.create_task(escuta())
        await asyncio.sleep(0)
        for snapshot in snapshots:
            await agente.enforce_protective_exits(snapshot)
        await asyncio.sleep(0.05)
        tarefa.cancel()
        return recebidos

    async def test_it_says_which_positions_have_no_stop(self, settings):
        agente = await self._agente(settings)
        alertas = await self._alertas(agente, self._snapshot("BTC"))
        assert [a["type"] for a in alertas] == ["position_without_cost_basis"]
        assert "BTC" in alertas[0]["title"]

    async def test_it_warns_once_per_transition_not_once_per_snapshot(self, settings):
        """A cada 60s seriam 1.440 avisos por dia sobre a mesma posicao."""
        agente = await self._agente(settings)
        alertas = await self._alertas(agente, *[self._snapshot("BTC")] * 5)
        assert len(alertas) == 1

    async def test_a_new_asset_without_a_basis_is_a_new_warning(self, settings):
        agente = await self._agente(settings)
        alertas = await self._alertas(
            agente, self._snapshot("BTC"), self._snapshot("BTC", "ETH")
        )
        assert len(alertas) == 2
        assert "ETH" in alertas[1]["title"]

    async def test_no_warning_when_every_position_has_a_basis(self, settings):
        from crypto_traders.domain.models import PortfolioSnapshot, Position

        agente = await self._agente(settings)
        com_base = PortfolioSnapshot(
            total_value=Decimal("1000"), cash_value=Decimal("500"),
            positions_value=Decimal("500"),
            positions=[Position(
                exchange=ExchangeName.BINANCE, asset="BTC", quantity=Decimal("1"),
                average_price=Decimal("50"), current_price=Decimal("50"),
            )],
        )
        assert await self._alertas(agente, com_base) == []


class TestTheProtectiveExitIsAuditable:
    """Uma venda a mercado de dinheiro real nao sai sem registro de risco.

    O `risk_event_id` da saida protetiva era um texto montado na hora
    ("protecao-stop_loss-BTC") que nao era chave de nada -- e a garantia no topo
    do modulo, de que todo `OrderRequest` aponta para uma linha ja gravada em
    `risk_events`, era falsa exatamente neste caminho.
    """

    async def _agente(self, settings, stop=0.05):
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        bus = InMemoryEventBus()
        await bus.start()
        configurado = settings.model_copy(
            update={"risk": settings.risk.model_copy(update={"stop_loss_pct": stop})}
        )
        return RiskManagerAgent(bus, configurado)

    def _snapshot(self, medio="100", atual="94"):
        from crypto_traders.domain.models import PortfolioSnapshot, Position

        return PortfolioSnapshot(
            total_value=Decimal("1000"), cash_value=Decimal("500"),
            positions_value=Decimal("500"),
            positions=[Position(
                exchange=ExchangeName.BINANCE, asset="BTC", quantity=Decimal("0.01"),
                average_price=Decimal(medio), current_price=Decimal(atual),
            )],
        )

    async def test_the_risk_event_id_is_a_row_that_exists(self, settings):
        from crypto_traders.db.repositories import RiskEventRepository
        from crypto_traders.db.session import session_scope

        agente = await self._agente(settings)
        ordens = await agente.enforce_protective_exits(self._snapshot())

        async with session_scope(settings) as session:
            eventos = await RiskEventRepository(session).list()
        assert ordens[0].risk_event_id in {e.id for e in eventos}

    async def test_the_row_records_the_level_that_was_breached(self, settings):
        from crypto_traders.db.repositories import RiskEventRepository
        from crypto_traders.db.session import session_scope

        agente = await self._agente(settings, stop=0.05)
        ordens = await agente.enforce_protective_exits(self._snapshot("100", "94"))

        async with session_scope(settings) as session:
            eventos = await RiskEventRepository(session).list()
        evento = next(e for e in eventos if e.id == ordens[0].risk_event_id)
        assert evento.snapshot["motivo"] == "stop_loss"
        assert evento.snapshot["nivel"] == "95.00"
        assert evento.decision == "approved"

    async def test_it_leaves_a_line_in_the_audit_log(self, settings):
        from crypto_traders.db.repositories import AuditLogRepository
        from crypto_traders.db.session import session_scope

        agente = await self._agente(settings)
        await agente.enforce_protective_exits(self._snapshot())

        async with session_scope(settings) as session:
            entradas = await AuditLogRepository(session).list()
        assert any(e.action == "protective_exit_emitted" for e in entradas)

    async def test_the_circuit_breaker_never_blocks_a_protective_exit(self, settings):
        """D4 neste caminho: a trava bloqueia ABRIR, nunca FECHAR."""
        agente = await self._agente(settings)
        await agente._trip("perda diaria de 9% do capital")
        assert agente.circuit_breaker_active
        assert len(await agente.enforce_protective_exits(self._snapshot())) == 1


class TestWhatTheStopSkipsAndWhy:
    """Cada `continue` de `enforce_protective_exits` e uma posicao nao protegida.

    Documentacao errada sobre protecao e pior que ausencia dela, entao cada
    motivo para pular uma posicao esta aqui, com o que o sistema faz a respeito.
    """

    async def _agente(self, settings):
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        bus = InMemoryEventBus()
        await bus.start()
        return RiskManagerAgent(bus, settings)

    def _posicao(self, **campos):
        from crypto_traders.domain.models import Position

        base = {
            "exchange": ExchangeName.BINANCE,
            "asset": "BTC",
            "quantity": Decimal("1"),
            "average_price": Decimal("100"),
            "current_price": Decimal("50"),
        }
        return Position(**{**base, **campos})

    def _snapshot(self, *posicoes):
        from crypto_traders.domain.models import PortfolioSnapshot

        return PortfolioSnapshot(
            total_value=Decimal("1000"), cash_value=Decimal("500"),
            positions_value=Decimal("500"), positions=list(posicoes),
        )

    async def test_a_position_without_a_current_price_is_skipped(self, settings):
        """Sem preco corrente nao ha o que comparar com o nivel.

        Nao entra no aviso de "sem preco medio": e outra falta, do Market Data,
        e o Portfolio Agent ja registra `portfolio.unpriced_assets`.
        """
        agente = await self._agente(settings)
        assert await agente.enforce_protective_exits(
            self._snapshot(self._posicao(current_price=None))
        ) == []
        assert agente._sem_base_de_custo == set()

    async def test_a_zero_quantity_position_is_skipped(self, settings):
        agente = await self._agente(settings)
        assert await agente.enforce_protective_exits(
            self._snapshot(self._posicao(quantity=Decimal(0), average_price=None))
        ) == []
        assert agente._sem_base_de_custo == set()

    async def test_a_non_positive_average_price_counts_as_no_basis(self, settings):
        """Medio zero daria nivel zero: nenhum preco jamais o romperia."""
        agente = await self._agente(settings)
        assert await agente.enforce_protective_exits(
            self._snapshot(self._posicao(average_price=Decimal(0)))
        ) == []
        assert agente._sem_base_de_custo == {"BTC"}

    async def test_the_warning_rearms_when_the_basis_appears(self, settings):
        """Lancar a compra que faltava resolve, e o aviso volta a poder sair."""
        agente = await self._agente(settings)
        await agente.enforce_protective_exits(
            self._snapshot(self._posicao(average_price=None))
        )
        assert agente._sem_base_de_custo == {"BTC"}

        await agente.enforce_protective_exits(
            self._snapshot(self._posicao(average_price=Decimal("100"),
                                         current_price=Decimal("100")))
        )
        assert agente._sem_base_de_custo == set()


class TestRetryingHasABrake:
    """Tentar de novo cobre a recusa transitoria; desistir cobre a permanente.

    As duas metades sao necessarias. Sem retentativa, uma recusa de rede deixava
    a posicao sem stop para sempre. Sem freio, poeira abaixo do MIN_NOTIONAL da
    exchange -- que nao vende hoje nem nunca -- geraria uma ordem recusada e um
    alerta por retrato do portfolio: 1.440 por dia, a patologia do achado 1 do
    ensaio, no aviso que menos pode ser ignorado.
    """

    async def _agente(self, settings, stop=0.05):
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        bus = InMemoryEventBus()
        await bus.start()
        configurado = settings.model_copy(
            update={"risk": settings.risk.model_copy(update={"stop_loss_pct": stop})}
        )
        return RiskManagerAgent(bus, configurado)

    def _snapshot(self, minuto: int, quantidade="0.01"):
        from crypto_traders.domain.models import PortfolioSnapshot, Position

        return PortfolioSnapshot(
            total_value=Decimal("1000"), cash_value=Decimal("500"),
            positions_value=Decimal("500"),
            timestamp=datetime(2026, 9, 9, 12, 0, tzinfo=UTC) + timedelta(minutes=minuto),
            positions=[Position(
                exchange=ExchangeName.BINANCE, asset="BTC",
                quantity=Decimal(quantidade), average_price=Decimal("100"),
                current_price=Decimal("90"),
            )],
        )

    async def _recusar(self, settings, request):
        from crypto_traders.db.repositories import OrderRepository
        from crypto_traders.db.session import session_scope
        from crypto_traders.domain.enums import OrderStatus

        async with session_scope(settings) as session:
            order = await OrderRepository(session).create_pending(request, "dry_run")
            order.status = str(OrderStatus.REJECTED)

    async def _ciclos(self, agente, settings, quantos: int):
        """Roda N retratos, recusando toda ordem de protecao emitida."""
        emitidas = 0
        for minuto in range(quantos):
            for request in await agente.enforce_protective_exits(self._snapshot(minuto)):
                emitidas += 1
                await self._recusar(settings, request)
        return emitidas

    async def test_it_stops_trying_instead_of_ordering_forever(self, settings):
        from crypto_traders.agents.risk_manager import MAX_TENTATIVAS_DE_SAIDA

        agente = await self._agente(settings)
        emitidas = await self._ciclos(agente, settings, 20)
        assert emitidas == MAX_TENTATIVAS_DE_SAIDA

    async def test_the_alerts_are_bounded_too(self, settings):
        """Duas mensagens no total: a primeira recusa e a desistencia."""
        import asyncio

        from crypto_traders.bus import Topics

        agente = await self._agente(settings)
        alertas: list[dict] = []

        async def escuta():
            async for alerta in agente.bus.subscribe(Topics.ALERTS):
                alertas.append(alerta)

        tarefa = asyncio.create_task(escuta())
        await asyncio.sleep(0)
        await self._ciclos(agente, settings, 20)
        await asyncio.sleep(0.05)
        tarefa.cancel()

        tipos = [a["type"] for a in alertas]
        assert tipos.count("protective_exit_failed") == 1
        assert tipos.count("protective_exit_impossible") == 1

    async def test_giving_up_says_the_position_has_no_stop(self, settings):
        """Desistir em silencio seria pior que insistir."""
        import asyncio

        from crypto_traders.bus import Topics

        agente = await self._agente(settings)
        alertas: list[dict] = []

        async def escuta():
            async for alerta in agente.bus.subscribe(Topics.ALERTS):
                alertas.append(alerta)

        tarefa = asyncio.create_task(escuta())
        await asyncio.sleep(0)
        await self._ciclos(agente, settings, 20)
        await asyncio.sleep(0.05)
        tarefa.cancel()

        desistencia = next(a for a in alertas if a["type"] == "protective_exit_impossible")
        assert "BTC" in desistencia["title"]
        assert "sem stop-loss" in desistencia["message"]

    async def test_a_transient_refusal_does_not_burn_the_budget(self, settings):
        """Uma recusa, depois a ordem passa: a contagem volta ao zero."""
        from crypto_traders.agents.risk_manager import MAX_TENTATIVAS_DE_SAIDA
        from crypto_traders.domain.models import PortfolioSnapshot

        agente = await self._agente(settings)
        primeira = await agente.enforce_protective_exits(self._snapshot(0))
        await self._recusar(settings, primeira[0])

        # Segunda tentativa: a ordem vai e a posicao sai da carteira.
        segunda = await agente.enforce_protective_exits(self._snapshot(1))
        assert len(segunda) == 1
        vazio = PortfolioSnapshot(
            total_value=Decimal("1000"), cash_value=Decimal("1000"),
            positions_value=Decimal(0), positions=[],
            timestamp=datetime(2026, 9, 9, 12, 2, tzinfo=UTC),
        )
        await agente.enforce_protective_exits(vazio)
        assert agente._exit_failures == {}

        # A carteira volta a ter a posicao: o orcamento de tentativas e inteiro.
        emitidas = await self._ciclos(agente, settings, 20)
        assert emitidas == MAX_TENTATIVAS_DE_SAIDA


class TestOAbandonoDestrava:
    """Ativo abandonado nao pode ficar sem stop para sempre (auditoria 09/11, P0 #5).

    A docstring de `_handle_failed_exit` prometia que a trava caia "quando o
    saldo mudar", e nada fazia isso: o stop vendia, sobrava poeira, a poeira era
    recusada tres vezes, o ativo era abandonado com a trava armada -- e a
    posicao NOVA que a estrategia comprava dias depois nunca era avaliada contra
    stop nem alvo enquanto o processo vivesse.
    """

    INICIO = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

    async def _agente(self, settings, stop=0.05, alvo=0.10):
        import asyncio

        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus, Topics

        bus = InMemoryEventBus()
        await bus.start()
        configurado = settings.model_copy(
            update={"risk": settings.risk.model_copy(update={
                "stop_loss_pct": stop, "take_profit_pct": alvo,
            })}
        )
        agente = RiskManagerAgent(bus, configurado)
        agente.alertas = []

        async def escuta():
            async for alerta in bus.subscribe(Topics.ALERTS):
                agente.alertas.append(alerta)

        agente._escuta = asyncio.create_task(escuta())
        await asyncio.sleep(0)
        return agente

    def _snapshot(self, minuto, quantidade, atual="90", medio="100"):
        from crypto_traders.domain.models import PortfolioSnapshot, Position

        return PortfolioSnapshot(
            total_value=Decimal("1000"), cash_value=Decimal("500"),
            positions_value=Decimal("500"),
            timestamp=self.INICIO + timedelta(minutes=minuto),
            positions=[Position(
                exchange=ExchangeName.BINANCE, asset="BTC",
                quantity=Decimal(quantidade), average_price=Decimal(medio),
                current_price=Decimal(atual),
            )],
        )

    async def _desfecho(self, settings, request, status, preenchido="0", minuto=None):
        from crypto_traders.db.repositories import OrderRepository
        from crypto_traders.db.session import session_scope

        async with session_scope(settings) as session:
            order = await OrderRepository(session).create_pending(request, "dry_run")
            order.status = str(status)
            order.filled_quantity = Decimal(preenchido)
            await session.flush()
            if minuto is not None:
                order.updated_at = self.INICIO + timedelta(minutes=minuto, seconds=30)

    async def _abandonar(self, agente, settings, quantidade, atual="90"):
        """Recusa toda saida ate o sistema desistir. Devolve o proximo minuto."""
        from crypto_traders.domain.enums import OrderStatus

        for minuto in range(10):
            for request in await agente.enforce_protective_exits(
                self._snapshot(minuto, quantidade, atual)
            ):
                await self._desfecho(settings, request, OrderStatus.REJECTED)
        assert "BTC" in agente._exit_abandoned
        return 10

    async def test_a_posicao_nova_maior_que_a_tentativa_volta_a_ter_stop(self, settings):
        agente = await self._agente(settings)
        minuto = await self._abandonar(agente, settings, "0.01")

        # A estrategia compra de novo: a posicao passa da quantidade abandonada.
        ordens = await agente.enforce_protective_exits(self._snapshot(minuto, "0.5"))
        assert len(ordens) == 1, "a posicao nova precisa ser avaliada contra o stop"
        assert ordens[0].quantity == Decimal("0.5")
        assert agente._exit_failures == {}
        assert "BTC" not in agente._exit_abandoned

    async def test_passar_a_valer_acima_do_minimo_destrava(self, settings):
        """Abandonado valendo 0,90 (minimo 10); o mesmo saldo passa a valer 12."""
        agente = await self._agente(settings, alvo=0.10)
        minuto = await self._abandonar(agente, settings, "0.01")

        ordens = await agente.enforce_protective_exits(
            self._snapshot(minuto, "0.01", atual="1200")
        )
        assert len(ordens) == 1, "acima do minimo, o alvo volta a ser avaliado"

    async def test_abandonado_acima_do_minimo_nao_vira_laco(self, settings):
        """Recusa permanente de outra natureza (credencial sem spot) com 90 em BTC.

        Ja valia acima do minimo ao desistir: "valer acima" nao e transicao, e
        destravar a cada retrato devolveria uma ordem recusada e um alerta por
        ciclo.
        """
        agente = await self._agente(settings)
        minuto = await self._abandonar(agente, settings, "1")
        for m in range(minuto, minuto + 10):
            assert await agente.enforce_protective_exits(self._snapshot(m, "1")) == []

    async def test_destravar_avisa_que_o_stop_voltou(self, settings):
        import asyncio

        agente = await self._agente(settings)
        minuto = await self._abandonar(agente, settings, "0.01")
        await agente.enforce_protective_exits(self._snapshot(minuto, "0.5"))
        await asyncio.sleep(0.05)
        tipos = [a["type"] for a in agente.alertas]
        assert "protective_exit_resumed" in tipos


class TestPoeiraDepoisDoStop:
    """O stop vende e sobra poeira (taxa no ativo + LOT_SIZE): nao e posicao a proteger.

    Antes, a poeira disparava o stop de novo a cada retrato: recusada pela
    exchange (abaixo do minimo), tres tentativas, alerta de "acao manual
    necessaria" -- e o abandono que prendia a trava para sempre.
    """

    async def test_a_poeira_nao_dispara_stop_nem_alerta(self, settings):
        import asyncio

        from crypto_traders.domain.enums import OrderStatus

        base = TestOAbandonoDestrava()
        agente = await base._agente(settings)
        primeira = await agente.enforce_protective_exits(base._snapshot(0, "1"))
        assert len(primeira) == 1
        await base._desfecho(settings, primeira[0], OrderStatus.FILLED, preenchido="0.99999012")

        for minuto in range(1, 20):
            assert await agente.enforce_protective_exits(
                base._snapshot(minuto, "0.00000988")
            ) == []
        await asyncio.sleep(0.05)
        tipos = {a["type"] for a in agente.alertas}
        assert "protective_exit_failed" not in tipos
        assert "protective_exit_impossible" not in tipos
        assert "BTC" not in agente._exit_abandoned

    async def test_recomprar_por_cima_da_poeira_volta_a_ter_stop(self, settings):
        from crypto_traders.domain.enums import OrderStatus

        base = TestOAbandonoDestrava()
        agente = await base._agente(settings)
        primeira = await agente.enforce_protective_exits(base._snapshot(0, "1"))
        await base._desfecho(settings, primeira[0], OrderStatus.FILLED, preenchido="0.99999012")
        await agente.enforce_protective_exits(base._snapshot(1, "0.00000988"))

        ordens = await agente.enforce_protective_exits(base._snapshot(2, "0.50000988"))
        assert len(ordens) == 1
        assert ordens[0].quantity == Decimal("0.50000988")


class TestSaidaPreenchidaEPosicaoQueVoltou:
    """Segundo caminho do P0 #5: a saida preencheu e a posicao voltou a crescer.

    Compra manual (ou da estrategia) entre o preenchimento e o retrato seguinte:
    `restante >= voo.quantity`, o ramo de preenchimento so soltava a trava com
    `restante < voo.quantity`, e a trava ficava armada para sempre, sem aviso.
    """

    async def test_a_trava_cai_quando_um_retrato_posterior_mostra_a_posicao(self, settings):
        from crypto_traders.domain.enums import OrderStatus

        base = TestOAbandonoDestrava()
        agente = await base._agente(settings)
        primeira = await agente.enforce_protective_exits(base._snapshot(0, "1"))
        # Preencheu 30s depois do retrato do minuto 0.
        await base._desfecho(settings, primeira[0], OrderStatus.FILLED, "1", minuto=0)

        # Retrato do minuto 1: carimbado depois do preenchimento, mas o saldo
        # pode ter sido lido antes dele. Nao solta: venderia duas vezes.
        assert await agente.enforce_protective_exits(base._snapshot(1, "1.2")) == []
        # Retrato do minuto 2: montado inteiro depois. A posicao e nova.
        ordens = await agente.enforce_protective_exits(base._snapshot(2, "1.2"))
        assert len(ordens) == 1
        assert ordens[0].quantity == Decimal("1.2")


class TestVendaParcialQueExpirou:
    """M1: preencheu 60% e expirou (`CANCELED` com fill) nao e "nada vendido".

    Antes, o status vinha antes da quantidade: `CANCELED` caia em
    `SAIDA_SEM_VENDA`, o aviso dizia "NAO executada... a posicao continua
    aberta" (falso: 60% saiu) e uma das tres tentativas era gasta -- tres
    expiracoes parciais levavam ao abandono.
    """

    async def test_cancelada_com_preenchimento_e_saida_parcial(self, settings):
        import asyncio

        from crypto_traders.domain.enums import OrderStatus

        base = TestOAbandonoDestrava()
        agente = await base._agente(settings)
        primeira = await agente.enforce_protective_exits(base._snapshot(0, "1"))
        await base._desfecho(settings, primeira[0], OrderStatus.CANCELED, preenchido="0.6")

        resto = await agente.enforce_protective_exits(base._snapshot(1, "0.4"))
        await asyncio.sleep(0.05)
        assert len(resto) == 1 and resto[0].quantity == Decimal("0.4")
        assert agente._exit_failures == {}
        assert "protective_exit_failed" not in [a["type"] for a in agente.alertas]
