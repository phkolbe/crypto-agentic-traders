"""Aritmetica do Portfolio Agent: preco medio, taxas e resultado realizado.

Estes numeros alimentam o circuit breaker (`realized_pnl + unrealized_pnl` em
`RiskManagerAgent.check_circuit_breaker`), entao errar a conta aqui desliga ou
deixa de desligar o sistema na hora errada. Cada teste confere um VALOR, nao a
presenca de um campo, e os dois ultimos veem a trava disparar (e nao disparar).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update

from crypto_traders.agents.portfolio import PortfolioAgent
from crypto_traders.agents.risk_manager import RiskManagerAgent
from crypto_traders.bus import InMemoryEventBus
from crypto_traders.db import models as orm
from crypto_traders.db.repositories import (
    AuditLogRepository,
    PortfolioSnapshotRepository,
    TradeRepository,
)
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import Side, TradeOrigin, TradingMode
from crypto_traders.domain.models import PortfolioSnapshot
from crypto_traders.exchanges.paper import PaperBroker
from crypto_traders.risk.rules import PortfolioState

INICIO = datetime(2026, 9, 1, tzinfo=UTC)


async def registrar(
    settings,
    side: Side,
    quantidade: str,
    preco: str,
    *,
    minuto: int = 0,
    taxa: str = "0",
    moeda_da_taxa: str | None = "USDT",
    simbolo: str = "BTC/USDT",
    origem: TradeOrigin = TradeOrigin.AGENT,
    quando: datetime | None = None,
    modo: str = "dry_run",
) -> None:
    """Grava um trade do mesmo jeito que o Execution Agent grava."""
    async with session_scope(settings) as session:
        await TradeRepository(session).record(
            executed_at=quando or (INICIO + timedelta(minutes=minuto)),
            exchange="paper",
            symbol=simbolo,
            side=str(side),
            quantity=Decimal(quantidade),
            price=Decimal(preco),
            fee=Decimal(taxa),
            fee_currency=moeda_da_taxa,
            origin=origem,
            mode=modo,
        )


def agente(settings, broker: PaperBroker, precos: dict[str, str] | None = None):
    fonte = {asset: Decimal(valor) for asset, valor in (precos or {}).items()}
    return PortfolioAgent(
        InMemoryEventBus(), broker, settings, price_source=lambda: dict(fonte)
    )


def carteira(caixa: str, **ativos: str) -> PaperBroker:
    broker = PaperBroker(initial_balance=Decimal(caixa))
    for asset, quantidade in ativos.items():
        broker.credit(asset, Decimal(quantidade))
    return broker


class TestPrecoMedio:
    """Custo medio movel, com compras a precos diferentes e vendas parciais."""

    async def test_duas_compras_a_precos_diferentes(self, settings):
        await registrar(settings, Side.BUY, "1", "100", minuto=0)
        await registrar(settings, Side.BUY, "3", "200", minuto=1)

        snapshot = await agente(
            settings, carteira("0", BTC="4"), {"BTC": "300"}
        ).build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        # (1*100 + 3*200) / 4 = 175
        assert btc.average_price == Decimal("175")
        assert snapshot.unrealized_pnl == Decimal("500")  # (300 - 175) * 4

    async def test_venda_parcial_nao_move_o_preco_medio(self, settings):
        """Custo medio: vender metade tira metade do custo e mantem o medio."""
        await registrar(settings, Side.BUY, "1", "100", minuto=0)
        await registrar(settings, Side.BUY, "3", "200", minuto=1)
        await registrar(settings, Side.SELL, "2", "500", minuto=2)

        snapshot = await agente(
            settings, carteira("1000", BTC="2"), {"BTC": "300"}
        ).build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("175")
        assert snapshot.realized_pnl == Decimal("650")  # 2 * (500 - 175)
        assert snapshot.unrealized_pnl == Decimal("250")  # 2 * (300 - 175)

    async def test_compra_parcial_depois_da_venda_recomeca_do_novo_custo(self, settings):
        """Fechar tudo e comprar de novo: o preco medio e so o da recompra."""
        await registrar(settings, Side.BUY, "1", "100", minuto=0)
        await registrar(settings, Side.SELL, "1", "150", minuto=1)
        await registrar(settings, Side.BUY, "2", "400", minuto=2)

        snapshot = await agente(
            settings, carteira("0", BTC="2"), {"BTC": "400"}
        ).build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("400")
        assert snapshot.unrealized_pnl == Decimal("0")
        assert snapshot.realized_pnl == Decimal("50")

    async def test_fechamento_total_nao_deixa_residuo_de_arredondamento(self, settings):
        """Custo com dizima: fechar tudo tem que zerar exato, nao 1E-27."""
        await registrar(settings, Side.BUY, "3", "10", minuto=0)  # custo 30 / 3 ativos
        await registrar(settings, Side.SELL, "1", "10", minuto=1)
        await registrar(settings, Side.SELL, "2", "10", minuto=2)

        snapshot = await agente(settings, carteira("30"), {"BTC": "10"}).build_snapshot()

        assert snapshot.realized_pnl == Decimal("0")
        assert not [p for p in snapshot.positions if p.asset == "BTC"]

    async def test_compra_antes_de_venda_quando_o_instante_empata(self, settings):
        """Lancamento manual com data sem hora: o desempate nao pode ser do banco.

        Gravado na ordem compra-100, venda-150, compra-200, os tres no mesmo
        instante. Se a venda for apurada antes da segunda compra, o preco medio
        vira 200 e aparecem 50 de lucro que nao existem.
        """
        empate = INICIO + timedelta(days=1)
        await registrar(settings, Side.BUY, "1", "100", quando=INICIO)
        await registrar(settings, Side.BUY, "1", "200", quando=empate)
        await registrar(settings, Side.SELL, "1", "150", quando=empate)

        snapshot = await agente(
            settings, carteira("150", BTC="1"), {"BTC": "150"}
        ).build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("150")  # (100 + 200) / 2
        assert snapshot.realized_pnl == Decimal("0")  # vendido a 150, medio 150


class TestTaxas:
    async def test_taxa_da_compra_entra_no_preco_medio(self, settings):
        await registrar(settings, Side.BUY, "1", "100", minuto=0, taxa="1")

        snapshot = await agente(
            settings, carteira("899", BTC="1"), {"BTC": "100"}
        ).build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("101")
        assert snapshot.unrealized_pnl == Decimal("-1")

    async def test_taxa_da_venda_entra_no_resultado_realizado(self, settings):
        """Comprar e vender pelo mesmo preco, com taxa nas duas pontas: -2."""
        await registrar(settings, Side.BUY, "1", "100", minuto=0, taxa="1")
        await registrar(settings, Side.SELL, "1", "100", minuto=1, taxa="1")

        snapshot = await agente(settings, carteira("998"), {"BTC": "100"}).build_snapshot()

        assert snapshot.realized_pnl == Decimal("-2")

    async def test_taxa_em_outra_moeda_nao_polui_o_custo_na_cotacao(self, settings):
        """0,01 BNB nao e 0,01 USDT: somar ao custo em USDT e erro de unidade."""
        await registrar(
            settings, Side.BUY, "1", "100", minuto=0, taxa="0.01", moeda_da_taxa="BNB"
        )

        snapshot = await agente(
            settings, carteira("900", BTC="1"), {"BTC": "100"}
        ).build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("100")
        assert snapshot.unrealized_pnl == Decimal("0")


class TestTradeManual:
    async def test_manual_entra_no_mesmo_consolidado(self, settings):
        """Compra do agente e compra digitada na interface, mesmo preco medio."""
        await registrar(settings, Side.BUY, "1", "100", minuto=0)
        await registrar(
            settings, Side.BUY, "1", "300", minuto=1, origem=TradeOrigin.MANUAL
        )

        snapshot = await agente(
            settings, carteira("0", BTC="2"), {"BTC": "250"}
        ).build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("200")
        assert snapshot.unrealized_pnl == Decimal("100")  # 2 * (250 - 200)

    async def test_venda_manual_realiza_contra_a_compra_do_agente(self, settings):
        """A venda digitada fecha a posicao aberta pelo agente, sem corromper nada."""
        await registrar(settings, Side.BUY, "2", "100", minuto=0)
        await registrar(
            settings, Side.SELL, "1", "150", minuto=1, origem=TradeOrigin.MANUAL
        )

        snapshot = await agente(
            settings, carteira("150", BTC="1"), {"BTC": "150"}
        ).build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert snapshot.realized_pnl == Decimal("50")
        assert btc.average_price == Decimal("100")
        assert snapshot.unrealized_pnl == Decimal("50")

    async def test_venda_sem_base_de_custo_nao_inventa_lucro(self, settings):
        """Saldo preexistente vendido: sem custo conhecido, nao ha lucro a declarar.

        Declarar 150 de lucro aqui empurraria o resultado de negociacao para cima
        e esconderia prejuizo real do circuit breaker.
        """
        await registrar(settings, Side.SELL, "1", "150", minuto=0)

        snapshot = await agente(settings, carteira("150"), {"BTC": "150"}).build_snapshot()

        assert snapshot.realized_pnl == Decimal("0")


class TestCotacoes:
    async def test_trade_em_outra_cotacao_fica_fora(self, settings):
        """Migracao BRL -> USDC (D24): 401.128 BRL nao entra num medio em USDT."""
        await registrar(
            settings, Side.BUY, "0.00005611", "401128.464", minuto=0, simbolo="BTC/BRL"
        )
        await registrar(
            settings,
            Side.SELL,
            "0.00005611",
            "385275.674",
            minuto=1,
            simbolo="BTC/BRL",
            moeda_da_taxa="BRL",
            taxa="0.0216",
        )
        await registrar(settings, Side.BUY, "1", "100", minuto=2)

        snapshot = await agente(
            settings, carteira("0", BTC="1"), {"BTC": "100"}
        ).build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("100")
        assert snapshot.realized_pnl == Decimal("0")


class TestSaldosEstranhos:
    """Ativo fora da whitelist e po (dust), contra o que o Risk Manager assume."""

    def _estado(self, settings, snapshot: PortfolioSnapshot) -> PortfolioState:
        """Reproduz exatamente o que `RiskManagerAgent._build_state` monta."""
        return PortfolioState(
            total_value=snapshot.total_value,
            cash=snapshot.cash_value,
            positions={p.asset: p.quantity for p in snapshot.positions},
            prices={
                p.asset: p.current_price
                for p in snapshot.positions
                if p.current_price is not None
            },
        )

    async def test_ativo_fora_da_whitelist_conta_no_patrimonio_e_ocupa_vaga(
        self, settings
    ):
        """Dinheiro e dinheiro: entra no total, e o Risk ve a posicao ocupada.

        Ocupar a vaga e o lado seguro: um saldo de DOGE que o sistema nao sabe
        vender reduz quantas posicoes novas ele abre, em vez de somar exposicao
        invisivel ao limite.
        """
        broker = carteira("100", DOGE="500")
        snapshot = await agente(
            settings, broker, {"DOGE": "0.4", "BTC": "50000"}
        ).build_snapshot()

        assert snapshot.total_value == Decimal("300")
        assert snapshot.positions_value == Decimal("200")
        assert "DOGE" in snapshot.allocations

        estado = self._estado(settings, snapshot)
        dust = settings.risk.min_order_notional / 2
        assert estado.open_positions("USDT", dust) == 1

    async def test_po_conta_no_patrimonio_mas_nao_ocupa_vaga(self, settings):
        """O po soma no valor (existe), e nao conta como posicao (o Risk ignora)."""
        broker = carteira("100", BTC="0.00000001")
        snapshot = await agente(settings, broker, {"BTC": "50000"}).build_snapshot()

        assert snapshot.total_value == Decimal("100.0005")
        estado = self._estado(settings, snapshot)
        dust = settings.risk.min_order_notional / 2
        assert estado.open_positions("USDT", dust) == 0

    async def test_ativo_sem_preco_nao_entra_no_total_nem_ocupa_vaga(self, settings):
        """Sem preco nao ha valor, e sem valor o Risk nao conta a posicao."""
        broker = carteira("100", XYZ="1000")
        snapshot = await agente(settings, broker, {}).build_snapshot()

        assert snapshot.total_value == Decimal("100")
        estado = self._estado(settings, snapshot)
        assert estado.open_positions("USDT", settings.risk.min_order_notional / 2) == 0


class TestSerieTemporal:
    async def test_snapshot_gravado_volta_como_decimal_sem_perder_casas(self, settings):
        """D7: a serie que alimenta o grafico e Decimal na ida e na volta."""
        await registrar(settings, Side.BUY, "3", "0.000000000000000007", minuto=0)

        broker = carteira("0", BTC="3")
        snapshot = await agente(
            settings, broker, {"BTC": "0.000000000000000011"}
        ).build_snapshot()

        assert isinstance(snapshot.total_value, Decimal)
        assert isinstance(snapshot.realized_pnl, Decimal)
        assert isinstance(snapshot.unrealized_pnl, Decimal)
        assert snapshot.total_value == Decimal("0.000000000000000033")

        async with session_scope(settings) as session:
            historico = await PortfolioSnapshotRepository(session).history()
        gravado = historico[-1]
        assert isinstance(gravado.total_value, Decimal)
        assert gravado.total_value == Decimal("0.000000000000000033")
        assert gravado.unrealized_pnl == Decimal("0.000000000000000012")
        posicao = gravado.positions[0]
        assert Decimal(str(posicao["average_price"])) == Decimal("0.000000000000000007")

    async def test_nenhum_float_no_dinheiro_do_snapshot(self, settings):
        await registrar(settings, Side.BUY, "1", "100", minuto=0)
        snapshot = await agente(
            settings, carteira("10", BTC="1"), {"BTC": "150"}
        ).build_snapshot()

        for campo in ("total_value", "cash_value", "positions_value",
                      "realized_pnl", "unrealized_pnl"):
            valor = getattr(snapshot, campo)
            assert isinstance(valor, Decimal), campo
            assert not isinstance(valor, float), campo
        for posicao in snapshot.positions:
            assert isinstance(posicao.quantity, Decimal)
            assert posicao.average_price is None or isinstance(posicao.average_price, Decimal)


class TestCircuitBreakerVeOResultadoRealizado:
    """A protecao AGINDO, e nao agindo, sobre o numero que este agente produz."""

    async def _primeiro_retrato_do_dia(self, settings, snapshot: PortfolioSnapshot, quando):
        async with session_scope(settings) as session:
            await session.execute(
                update(orm.PortfolioSnapshot)
                .where(orm.PortfolioSnapshot.id == snapshot.id)
                .values(timestamp=quando)
            )

    @pytest.fixture
    def producao(self, settings):
        """Configuracao do ensaio: 29,29 USDC autorizados, trava diaria de 5%."""
        return settings.model_copy(
            update={
                "risk": settings.risk.model_copy(
                    update={"authorized_capital": Decimal("29.29")}
                )
            }
        )

    async def test_realizar_lucro_nao_dispara_a_trava(self, producao):
        """Antes, fechar no lucro era lido como 'perda diaria de 10.24%'."""
        agora = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        bus = InMemoryEventBus()
        await bus.start()
        risco = RiskManagerAgent(bus, producao)

        await registrar(producao, Side.BUY, "1", "10", quando=agora - timedelta(days=1))
        primeiro = await agente(
            producao, carteira("19", BTC="1"), {"BTC": "13"}
        ).build_snapshot()
        assert primeiro.unrealized_pnl == Decimal("3")
        await self._primeiro_retrato_do_dia(
            producao, primeiro, agora.replace(hour=0, minute=1)
        )

        await registrar(producao, Side.SELL, "1", "13", quando=agora - timedelta(minutes=5))
        depois = (
            await agente(producao, carteira("32"), {"BTC": "13"}).build_snapshot()
        ).model_copy(update={"timestamp": agora})

        assert depois.realized_pnl == Decimal("3")
        assert depois.unrealized_pnl == Decimal("0")
        assert await risco.check_circuit_breaker(depois) is None
        assert not risco.circuit_breaker_active

    async def test_prejuizo_realizado_dispara_a_trava(self, producao):
        """Tres stops de -0,60 sobre 29,29 autorizados: 6,1% > 5%, trava."""
        agora = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        bus = InMemoryEventBus()
        await bus.start()
        risco = RiskManagerAgent(bus, producao)

        primeiro = await agente(producao, carteira("29.29"), {}).build_snapshot()
        assert primeiro.realized_pnl == Decimal("0")
        await self._primeiro_retrato_do_dia(
            producao, primeiro, agora.replace(hour=0, minute=1)
        )

        for i in range(3):
            await registrar(
                producao, Side.BUY, "1", "6", quando=agora - timedelta(hours=5 - i)
            )
            await registrar(
                producao,
                Side.SELL,
                "1",
                "5.40",
                quando=agora - timedelta(hours=5 - i, minutes=-30),
            )

        depois = (
            await agente(producao, carteira("27.49"), {"BTC": "5.40"}).build_snapshot()
        ).model_copy(update={"timestamp": agora})

        assert depois.realized_pnl == Decimal("-1.80")
        motivo = await risco.check_circuit_breaker(depois)
        assert motivo is not None, "prejuizo realizado tem que disparar a trava"
        assert "perda diaria" in motivo
        assert risco.circuit_breaker_active


# ----------------------------------------------------------------------
# O modo separa o dinheiro
# ----------------------------------------------------------------------
@pytest.fixture
def modo_real(settings):
    """Modo em que as ordens saem da maquina de verdade.

    TESTNET, e nao LIVE, de proposito: expressa "o modo corrente e o que manda
    ordem para fora" sem que nenhum teste construa configuracao de dinheiro
    real. O codigo apurado nao distingue os dois -- o filtro e uma igualdade
    sobre `trades.mode` --, entao o que vale aqui vale na virada para LIVE.
    """
    return settings.model_copy(update={"trading_mode": TradingMode.TESTNET})


async def acoes_de_auditoria(settings) -> list[str]:
    async with session_scope(settings) as session:
        return [row.action for row in await AuditLogRepository(session).list()]


class TestModoSeparaODinheiro:
    """`trades.mode` nao e coluna de relatorio: e a fronteira do dinheiro.

    O ensaio roda em dry_run com 16 pares /USDC e existe para virar LIVE. Se o
    papel entrar no preco medio das posicoes reais, contamina a UNICA entrada do
    nivel de stop-loss (`risk_manager.enforce_protective_exits`).
    """

    async def test_trade_de_outro_modo_fica_fora_do_preco_medio(self, modo_real):
        """Papel a 100 + real a 50 = medio 50, nao 75."""
        await registrar(modo_real, Side.BUY, "1", "100", minuto=0, modo="dry_run")
        await registrar(modo_real, Side.BUY, "1", "50", minuto=1, modo="testnet")

        # Na exchange existe UM BTC: o comprado no modo corrente, a 50.
        snapshot = await agente(
            modo_real, carteira("0", BTC="1"), {"BTC": "50"}
        ).build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("50")
        assert snapshot.unrealized_pnl == Decimal("0")

    async def test_stop_nao_e_emitido_por_causa_de_trade_de_outro_modo(self, modo_real):
        """A protecao AGINDO: com o papel dentro, ela vendia sem perda nenhuma.

        Medio real 50, preco 50, stop 3% => nivel 48,50, nada a fazer. Com a
        compra de papel a 100 o medio virava 75, o nivel 72,75, e o Risk Manager
        EMITIA `sell 1 BTC/USDT` -- venda a mercado de dinheiro real, de graca.
        """
        await registrar(modo_real, Side.BUY, "1", "100", minuto=0, modo="dry_run")
        await registrar(modo_real, Side.BUY, "1", "50", minuto=1, modo="testnet")

        snapshot = await agente(
            modo_real, carteira("0", BTC="1"), {"BTC": "50"}
        ).build_snapshot()

        bus = InMemoryEventBus()
        await bus.start()
        risco = RiskManagerAgent(bus, modo_real)
        emitidas = await risco.enforce_protective_exits(snapshot)

        assert emitidas == [], "; ".join(
            f"{o.side} {o.quantity} {o.symbol}" for o in emitidas
        )
        # E o espelho: a posicao real NAO ficou sem nivel (esse e o outro lado do
        # mesmo defeito -- papel mais barato deixaria a posicao sem stop util).
        btc = next(p for p in snapshot.positions if p.asset == "BTC")
        assert btc.average_price == Decimal("50")

    async def test_realizado_de_outro_modo_fica_fora_do_numero_da_trava(self, modo_real):
        """O ensaio perdeu 40 no papel; o modo corrente nunca fechou posicao."""
        await registrar(modo_real, Side.BUY, "1", "100", minuto=0, modo="dry_run")
        await registrar(modo_real, Side.SELL, "1", "60", minuto=1, modo="dry_run")

        snapshot = await agente(modo_real, carteira("60"), {"BTC": "60"}).build_snapshot()

        assert snapshot.realized_pnl == Decimal("0")
        assert "portfolio_trade_de_outro_modo" in await acoes_de_auditoria(modo_real)

    async def test_lancamento_manual_conta_no_modo_corrente(self, modo_real):
        """`mode='manual'` e operacao REAL feita fora do sistema: vale em todo modo.

        Se o manual tivesse modo proprio, ficaria de fora de toda apuracao -- e a
        compra digitada em Operacoes e justamente o caminho oferecido para dar
        base de custo (e portanto stop) a uma posicao herdada.
        """
        await registrar(
            modo_real,
            Side.BUY,
            "1",
            "100",
            minuto=0,
            origem=TradeOrigin.MANUAL,
            modo="manual",
        )

        snapshot = await agente(
            modo_real, carteira("0", BTC="1"), {"BTC": "120"}
        ).build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("100")
        assert snapshot.unrealized_pnl == Decimal("20")

    async def test_o_ensaio_em_dry_run_continua_com_preco_medio_e_stop(self, settings):
        """O espelho do filtro, e por isso ele nao pode ser "ignorar papel".

        Excluir dry_run sempre deixaria o ensaio -- que roda AGORA -- com toda
        posicao sem preco medio, ou seja SEM STOP NENHUM (`average_price is None`
        e tratado como posicao sem nivel). O filtro e por modo CORRENTE, e no
        ensaio o modo corrente e dry_run.
        """
        await registrar(settings, Side.BUY, "1", "100", minuto=0, modo="dry_run")

        snapshot = await agente(
            settings, carteira("0", BTC="1"), {"BTC": "90"}
        ).build_snapshot()
        btc = next(p for p in snapshot.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("100")

        bus = InMemoryEventBus()
        await bus.start()
        emitidas = await RiskManagerAgent(bus, settings).enforce_protective_exits(snapshot)
        assert [str(o.side) for o in emitidas] == ["sell"]  # 90 < 97: o stop AGE

    async def test_retrato_de_outro_modo_nao_ancora_o_realizado(self, settings, modo_real):
        """O realizado acumulado tambem nao atravessa a virada de modo.

        O retrato do ensaio carrega o realizado de papel. Ancorar o modo real
        nele seria a mesma contaminacao entrando pela porta de tras.
        """
        await registrar(settings, Side.BUY, "1", "100", minuto=0, modo="dry_run")
        await registrar(settings, Side.SELL, "1", "60", minuto=1, modo="dry_run")
        papel = await agente(settings, carteira("60"), {"BTC": "60"}).build_snapshot()
        assert papel.realized_pnl == Decimal("-40")

        real = await agente(modo_real, carteira("60"), {"BTC": "60"}).build_snapshot()
        assert real.realized_pnl == Decimal("0")


# ----------------------------------------------------------------------
# Saldo e historico sao de instantes diferentes
# ----------------------------------------------------------------------
class TestReconciliacaoDeSaldoComHistorico:
    """A trava obedece ao numero daqui, entao ele nao pode misturar instantes."""

    @pytest.fixture
    def producao(self, settings):
        return settings.model_copy(
            update={
                "risk": settings.risk.model_copy(
                    update={"authorized_capital": Decimal("29.29")}
                )
            }
        )

    async def _referencia_do_dia(self, settings, snapshot, quando):
        async with session_scope(settings) as session:
            await session.execute(
                update(orm.PortfolioSnapshot)
                .where(orm.PortfolioSnapshot.id == snapshot.id)
                .values(timestamp=quando)
            )

    async def test_venda_em_voo_nao_derruba_o_resultado_de_negociacao(self, producao):
        """A venda preencheu na exchange; a linha em `trades` ainda nao existe.

        `execution.py` grava o trade DEPOIS de aplicar o resultado. Nessa janela
        o nao realizado ja foi a zero e o realizado ainda era zero: a trava lia a
        diferenca como "perda diaria de 10.24% do capital", sem perda nenhuma.
        """
        agora = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        bus = InMemoryEventBus()
        await bus.start()
        risco = RiskManagerAgent(bus, producao)

        await registrar(producao, Side.BUY, "1", "10", quando=agora - timedelta(days=1))
        primeiro = await agente(
            producao, carteira("19", BTC="1"), {"BTC": "13"}
        ).build_snapshot()
        assert primeiro.unrealized_pnl == Decimal("3")
        await self._referencia_do_dia(producao, primeiro, agora.replace(hour=0, minute=1))

        em_voo = (
            await agente(producao, carteira("32"), {"BTC": "13"}).build_snapshot()
        ).model_copy(update={"timestamp": agora})

        # O credito repoe exatamente o nao realizado que desapareceu.
        assert em_voo.realized_pnl == Decimal("3")
        assert em_voo.unrealized_pnl == Decimal("0")
        assert await risco.check_circuit_breaker(em_voo) is None
        assert not risco.circuit_breaker_active
        assert "portfolio_saldo_reconciliado" in await acoes_de_auditoria(producao)

    async def test_o_credito_da_reconciliacao_nao_balanca_com_o_preco(self, producao):
        """Credita UMA vez. Remarcado a cada retrato, o preco mandaria no numero.

        Um credito recalculado do preco corrente cairia junto com o mercado e
        armaria a trava sozinho -- o mesmo defeito que ele existe para corrigir,
        na direcao oposta.
        """
        agora = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        await registrar(producao, Side.BUY, "1", "10", quando=agora - timedelta(days=1))
        await agente(producao, carteira("19", BTC="1"), {"BTC": "13"}).build_snapshot()

        primeiro = await agente(producao, carteira("32"), {"BTC": "13"}).build_snapshot()
        assert primeiro.realized_pnl == Decimal("3")

        # Mesmo saldo, preco derretendo: o realizado nao se move.
        for preco in ("9", "6", "1"):
            seguinte = await agente(
                producao, carteira("32"), {"BTC": preco}
            ).build_snapshot()
            assert seguinte.realized_pnl == Decimal("3"), preco

    async def test_apagar_lancamento_manual_nao_move_o_acumulado(self, producao):
        """Corrigir digitacao nao pode armar a trava.

        `DELETE /trades/{id}` e oferecido na interface. Com o realizado saindo de
        uma nova soma do historico, apagar uma venda manual lucrativa derrubava o
        acumulado no meio do dia e a trava lia o typo como prejuizo.
        """
        agora = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        bus = InMemoryEventBus()
        await bus.start()
        risco = RiskManagerAgent(bus, producao)

        await registrar(producao, Side.BUY, "1", "10", quando=agora - timedelta(days=2))
        async with session_scope(producao) as session:
            venda = await TradeRepository(session).record(
                executed_at=agora - timedelta(days=1),
                exchange="paper",
                symbol="BTC/USDT",
                side=str(Side.SELL),
                quantity=Decimal("1"),
                price=Decimal("13"),
                origin=TradeOrigin.MANUAL,
                mode="manual",
            )
            venda_id = venda.id

        primeiro = await agente(producao, carteira("32"), {"BTC": "13"}).build_snapshot()
        assert primeiro.realized_pnl == Decimal("3")
        await self._referencia_do_dia(producao, primeiro, agora.replace(hour=0, minute=1))

        async with session_scope(producao) as session:
            assert await TradeRepository(session).delete(venda_id)

        depois = (
            await agente(producao, carteira("32"), {"BTC": "13"}).build_snapshot()
        ).model_copy(update={"timestamp": agora})

        assert depois.realized_pnl == Decimal("3")
        assert await risco.check_circuit_breaker(depois) is None
        assert not risco.circuit_breaker_active

    async def test_historico_que_a_exchange_nao_confirma_vira_audit_log(self, settings):
        """Historico com um BTC que a exchange nao tem: fantasma com trilha.

        O patrimonio segue contando so o que a exchange devolve, mas o preco
        medio sai do historico: esse fantasma contaminaria o nivel de stop da
        proxima compra do ativo. Precisa de acao humana, e portanto de registro.
        """
        await registrar(settings, Side.BUY, "1", "10", minuto=0)

        snapshot = await agente(settings, carteira("32"), {"BTC": "13"}).build_snapshot()

        assert not [p for p in snapshot.positions if p.asset == "BTC"]
        assert snapshot.total_value == Decimal("32")
        assert "portfolio_saldo_divergente_do_historico" in await acoes_de_auditoria(
            settings
        )

    async def test_carteira_vazia_recusa_produzir_retrato(self, settings):
        """Leitura vazia onde havia posicao e leitura que FALHOU, nao carteira zerada.

        Publicar esse zero mandaria a trava obedecer a um patrimonio inexistente.
        O estado seguro nao e "arrisca menos com numero ruim": e nao produzir
        numero.
        """
        await registrar(settings, Side.BUY, "1", "10", minuto=0)
        await agente(settings, carteira("19", BTC="1"), {"BTC": "13"}).build_snapshot()

        with pytest.raises(RuntimeError, match="carteira vazia"):
            await agente(settings, carteira("0"), {"BTC": "13"}).build_snapshot()

        async with session_scope(settings) as session:
            assert len(await PortfolioSnapshotRepository(session).history()) == 1


class TestTrilhaDasDecisoesQueMudamDinheiro:
    """Descartar taxa, cotacao ou base de custo muda dinheiro: vai para audit_log."""

    async def test_taxa_descartada_deixa_registro(self, settings):
        await registrar(
            settings, Side.BUY, "1", "100", minuto=0, taxa="0.01", moeda_da_taxa="BNB"
        )
        await agente(settings, carteira("900", BTC="1"), {"BTC": "100"}).build_snapshot()

        assert "portfolio_taxa_descartada" in await acoes_de_auditoria(settings)

    async def test_venda_sem_base_de_custo_deixa_registro(self, settings):
        await registrar(settings, Side.SELL, "1", "150", minuto=0)
        await agente(settings, carteira("150"), {"BTC": "150"}).build_snapshot()

        assert "portfolio_venda_sem_base_de_custo" in await acoes_de_auditoria(settings)

    async def test_trade_de_outra_cotacao_deixa_registro(self, settings):
        await registrar(settings, Side.BUY, "1", "401128", minuto=0, simbolo="BTC/BRL")
        await agente(settings, carteira("100"), {}).build_snapshot()

        assert "portfolio_trade_de_outra_cotacao" in await acoes_de_auditoria(settings)

    async def test_o_mesmo_aviso_nao_se_repete_a_cada_retrato(self, settings):
        """1.440 linhas por dia num registro append-only o tornariam ilegivel."""
        await registrar(settings, Side.SELL, "1", "150", minuto=0)
        agent = agente(settings, carteira("150"), {"BTC": "150"})
        for _ in range(3):
            await agent.build_snapshot()

        acoes = await acoes_de_auditoria(settings)
        assert acoes.count("portfolio_venda_sem_base_de_custo") == 1


class TestVitalidade:
    async def test_heartbeat_nao_bate_quando_a_apuracao_falha(self, settings):
        """Chave vazia / exchange fora do ar: o agente nao pode parecer saudavel.

        Antes, a excecao era engolida e o heartbeat batia logo depois, FORA do
        try: o watchdog via saude onde nao havia retrato, e o Risk Manager
        seguia dimensionando ordens contra um retrato de horas atras.
        """

        class BrokerSemChave(PaperBroker):
            async def fetch_positions(self, prices):
                raise RuntimeError("leitura de saldo indisponivel")

        agent = PortfolioAgent(
            InMemoryEventBus(),
            BrokerSemChave(initial_balance=Decimal("100")),
            settings,
            price_source=dict,
        )
        ciclos = 0

        async def uma_volta(_seconds):
            nonlocal ciclos
            ciclos += 1
            return False

        agent.sleep = uma_volta  # type: ignore[method-assign]
        await agent._run()

        assert ciclos == 1
        assert agent.last_beat is None, "heartbeat batido sem retrato produzido"
        assert agent.latest is None


class TestOQueAAncoraNaoPodeCongelar:
    """A ancora vale para o realizado acumulado, e NAO para a base de custo.

    A separacao e deliberada. O realizado precisa ser imune a mudanca retroativa
    (senao um typo apagado arma a trava), mas o preco medio precisa OBEDECER a
    mudanca retroativa -- e o caminho de recuperacao que o proprio Risk Manager
    anuncia quando uma posicao esta sem stop e "lance a compra correspondente em
    Operacoes", e esse lancamento tem data no passado.
    """

    async def test_lancamento_retroativo_devolve_base_de_custo_e_stop(self, settings):
        broker = carteira("0", BTC="1")
        sem_base = await agente(settings, broker, {"BTC": "100"}).build_snapshot()
        assert next(p for p in sem_base.positions if p.asset == "BTC").average_price is None

        # A compra digitada em Operacoes, com data anterior a qualquer retrato.
        await registrar(
            settings,
            Side.BUY,
            "1",
            "80",
            minuto=0,
            origem=TradeOrigin.MANUAL,
            modo="manual",
        )
        depois = await agente(settings, broker, {"BTC": "100"}).build_snapshot()
        btc = next(p for p in depois.positions if p.asset == "BTC")

        assert btc.average_price == Decimal("80"), "posicao continuaria sem stop"
        # E lancar uma COMPRA nao inventa resultado realizado nenhum.
        assert depois.realized_pnl == Decimal("0")

    async def test_ancora_no_futuro_nao_congela_o_realizado(self, settings):
        """Relogio corrigido para tras deixaria o incremento parado em silencio.

        Com o retrato no futuro, nenhum trade e "posterior" a ele: o realizado
        pararia de andar e a trava mediria um numero que nunca muda.
        """
        await registrar(settings, Side.BUY, "2", "10", minuto=0)
        primeiro = await agente(
            settings, carteira("0", BTC="2"), {"BTC": "10"}
        ).build_snapshot()
        async with session_scope(settings) as session:
            await session.execute(
                update(orm.PortfolioSnapshot)
                .where(orm.PortfolioSnapshot.id == primeiro.id)
                .values(timestamp=datetime.now(UTC) + timedelta(days=1))
            )

        await registrar(settings, Side.SELL, "1", "13", minuto=1)
        depois = await agente(
            settings, carteira("13", BTC="1"), {"BTC": "20"}
        ).build_snapshot()

        # 1 * (13 - 10) = 3. Ancorado no futuro, o numero viria da reconciliacao
        # de saldo (1 * (20 - 10) = 10), que aqui seria contar duas vezes.
        assert depois.realized_pnl == Decimal("3")
        assert depois.unrealized_pnl == Decimal("10")
