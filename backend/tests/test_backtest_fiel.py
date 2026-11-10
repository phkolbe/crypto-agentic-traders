"""O backtest mede o sistema que roda? (frente F1 da auditoria de 09/11/2026)

O alvo de 8% do dry_run foi escolhido com `PortfolioBacktestEngine`. A auditoria
achou que o motor media OUTRO sistema: escolhia sinais por confianca (a producao
atende por ordem de chegada), media a queda em 1 instante de cada 4, lia stop e
alvo no OHLC como se a producao visse cada tick, contava o aquecimento nos dias,
misturava periodos no comprar-e-segurar e carimbava o cooldown no inicio do
candle. Cada classe abaixo reproduz um desses desvios com o menor cenario que o
mostra -- e falhava no motor anterior.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_traders.backtest.engine import BacktestEngine
from crypto_traders.backtest.portfolio import PortfolioBacktestEngine
from crypto_traders.backtest.protecao import (
    FOLGAS_PADRAO,
    SEM_FOLGA,
    FolgasDeProtecao,
    saida_de_protecao,
)
from crypto_traders.config import RiskSettings, TradingSettings
from crypto_traders.domain.enums import ExchangeName, SignalDirection
from crypto_traders.domain.models import Candle, Signal
from crypto_traders.strategies.base import MarketFrame, Strategy

INICIO = datetime(2026, 1, 1, tzinfo=UTC)
DIA = timedelta(days=1)


def candle(symbol: str, i: int, o, h, low, c, inicio: datetime = INICIO) -> Candle:
    return Candle(
        exchange=ExchangeName.BINANCE,
        symbol=symbol,
        timeframe="1d",
        open_time=inicio + i * DIA,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        volume=Decimal("10"),
    )


def plano(symbol: str, n: int, preco: float = 100.0, inicio: datetime = INICIO) -> list[Candle]:
    return [candle(symbol, i, preco, preco, preco, preco, inicio) for i in range(n)]


def depois(symbol: str, i: int, preco: float, n: int = 2) -> list[Candle]:
    """Candles planos depois do evento: o motor recusa serie de aquecimento + 2."""
    return [candle(symbol, i + k, preco, preco, preco, preco) for k in range(n)]


class Roteiro(Strategy):
    """Emite sinais roteirizados: {(simbolo, open_time do candle): (direcao, confianca)}."""

    name = "roteiro"
    description = "teste"
    min_candles = 3

    def __init__(self, roteiro: dict, name: str = "roteiro") -> None:
        self.roteiro = roteiro
        self.name = name

    def evaluate(self, market: MarketFrame) -> Signal | None:
        quando = market.frame.index[-1].to_pydatetime()
        pedido = self.roteiro.get((market.symbol, quando))
        if pedido is None:
            return None
        direcao, confianca = pedido
        return Signal(
            exchange=market.exchange,
            symbol=market.symbol,
            timeframe=market.timeframe,
            strategy=self.name,
            direction=direcao,
            confidence=confianca,
            reason="roteiro",
            reference_price=Decimal(str(market.frame["close"].iloc[-1])),
        )


def limites(symbols: list[str], **extra) -> RiskSettings:
    base = dict(
        max_order_notional=Decimal("1000"),
        max_order_pct_portfolio=0.45,
        max_asset_exposure_pct=0.9,
        max_open_positions=3,
        min_order_notional=Decimal("10"),
        stop_loss_pct=0.05,
        take_profit_pct=0.10,
        daily_loss_limit_pct=0.9,
        weekly_loss_limit_pct=0.9,
        min_signal_confidence=0.5,
        cooldown_seconds=0,
        symbol_whitelist=symbols,
        asset_whitelist=[s.split("/")[0] for s in symbols],
    )
    base.update(extra)
    return RiskSettings(**base)


def carteira(estrategias, risco, **extra) -> PortfolioBacktestEngine:
    padrao = dict(
        quote_currency="USDT",
        initial_balance=Decimal("1000"),
        fee_pct=Decimal("0"),
        slippage_pct=Decimal("0"),
        lookback=50,
    )
    padrao.update(extra)
    return PortfolioBacktestEngine(estrategias, risco, **padrao)


def compras(resultado) -> list[str]:
    return [t.symbol for t in resultado.trades if t.side == "buy"]


# ---------------------------------------------------------------------------
# 1. Selecao de sinais
# ---------------------------------------------------------------------------
class TestSelecaoIgualAProducao:
    """Producao: `signal_batch_window_seconds=0`, cada sinal sozinho, por chegada.

    Cenario do relatorio: no mesmo fechamento ETH (confianca 0,58) e SOL (0,80)
    cruzam juntos e so cabe uma posicao. A producao compra quem o Market Data
    publicou primeiro (ETH, primeiro na lista de pares); o motor comprava SOL.
    """

    PARES = ("ETH/USDT", "SOL/USDT")

    def _motor(self, **extra):
        quando = INICIO + 3 * DIA
        roteiro = {
            ("ETH/USDT", quando): (SignalDirection.LONG, 0.58),
            ("SOL/USDT", quando): (SignalDirection.LONG, 0.80),
        }
        risco = limites(list(self.PARES), max_open_positions=1)
        return carteira([Roteiro(roteiro)], risco, **extra)

    def _candles(self):
        # A ORDEM do dicionario e a ordem de publicacao do Market Data.
        return {par: plano(par, 8) for par in self.PARES}

    async def test_o_padrao_e_o_da_producao(self):
        assert self._motor().signal_batch_window_seconds == (
            TradingSettings.model_fields["signal_batch_window_seconds"].default
        )

    async def test_janela_zero_atende_por_ordem_de_chegada(self):
        r = await self._motor().run(self._candles())
        assert compras(r) == ["ETH/USDT"]
        # E o segundo sinal foi avaliado contra o estado JA com a compra do
        # primeiro: recusado pelo limite de posicoes, nao aprovado em dobro.
        assert r.signals_rejected == 1
        assert any("posicoes abertas" in m for m in r.rejection_reasons)

    async def test_a_ordem_de_chegada_e_a_ordem_dos_pares(self):
        candles = self._candles()
        invertido = {par: candles[par] for par in reversed(self.PARES)}
        r = await self._motor().run(invertido)
        assert compras(r) == ["SOL/USDT"]

    async def test_janela_positiva_decide_o_lote_por_confianca(self):
        r = await self._motor(signal_batch_window_seconds=5).run(self._candles())
        assert compras(r) == ["SOL/USDT"]


# ---------------------------------------------------------------------------
# 2. Queda maxima
# ---------------------------------------------------------------------------
class TestQuedaMedidaEmTodoInstante:
    """O motor gravava o patrimonio so quando `step % 4 == 0`.

    Compra no dia 3 (passo 3), cai 10% nos passos 5 a 7 e volta no passo 8. Os
    passos amostrados (4 e 8) estao no pico: queda reportada 0%, real ~9%.
    """

    async def test_a_queda_entre_amostras_aparece(self):
        quando = INICIO + 3 * DIA
        roteiro = {("BTC/USDT", quando): (SignalDirection.LONG, 0.9)}
        risco = limites(["BTC/USDT"], max_order_pct_portfolio=0.9,
                        stop_loss_pct=0.5, take_profit_pct=1.0)
        precos = [100, 100, 100, 100, 100, 90, 90, 90, 100, 100]
        candles = [candle("BTC/USDT", i, p, p, p, p) for i, p in enumerate(precos)]
        r = await carteira([Roteiro(roteiro)], risco).run({"BTC/USDT": candles})
        assert compras(r) == ["BTC/USDT"]
        assert r.max_drawdown_pct == pytest.approx(0.09, abs=0.005)


# ---------------------------------------------------------------------------
# 3. Protecao nao otimista
# ---------------------------------------------------------------------------
class TestProtecaoNaoOtimista:
    """Stop no nivel exato e alvo no pavio eram otimistas contra o ticker de 60 s."""

    def _limites(self):
        return limites(["BTC/USDT"], max_order_pct_portfolio=0.9)

    def _carteira(self, **extra):
        quando = INICIO + 3 * DIA
        roteiro = {("BTC/USDT", quando): (SignalDirection.LONG, 0.9)}
        return carteira([Roteiro(roteiro)], self._limites(), **extra)

    def _um_par(self, **extra):
        quando = INICIO + 3 * DIA
        roteiro = {("BTC/USDT", quando): (SignalDirection.LONG, 0.9)}
        return BacktestEngine(
            Roteiro(roteiro), self._limites(), quote_currency="USDT",
            initial_balance=Decimal("1000"), fee_pct=Decimal("0"),
            slippage_pct=Decimal("0"), lookback=50, **extra,
        )

    def _queda(self):
        # Compra a 100 no dia 3; stop em 95. Dia 4 cai a 92.
        return [
            *plano("BTC/USDT", 4), candle("BTC/USDT", 4, 100, 100, 92, 93),
            *depois("BTC/USDT", 5, 93),
        ]

    def _pavio_no_alvo(self):
        # Alvo em 110; a maxima do dia 4 toca 110 exatamente e o dia fecha a 104.
        return [
            *plano("BTC/USDT", 4), candle("BTC/USDT", 4, 100, 110, 100, 104),
            *depois("BTC/USDT", 5, 104),
        ]

    async def test_o_stop_preenche_alem_do_nivel(self):
        r = await self._carteira().run({"BTC/USDT": self._queda()})
        saida = next(t for t in r.trades if t.exit_reason == "stop")
        esperado = Decimal("95") * (1 - FOLGAS_PADRAO.stop)
        assert saida.price == pytest.approx(esperado)
        assert saida.price < Decimal("95")

    async def test_o_stop_nao_preenche_abaixo_da_minima(self):
        """A folga nao inventa preco: a minima do candle e o piso."""
        candles = [
            *plano("BTC/USDT", 4), candle("BTC/USDT", 4, 100, 100, 94.99, 96),
            *depois("BTC/USDT", 5, 96),
        ]
        folgas = FolgasDeProtecao(
            stop=Decimal("0.01"), alvo=Decimal(0), pavio_stop=Decimal(0)
        )
        r = await self._carteira(folgas=folgas).run({"BTC/USDT": candles})
        saida = next(t for t in r.trades if t.exit_reason == "stop")
        assert saida.price == pytest.approx(Decimal("94.99"))

    async def test_pavio_que_so_toca_o_alvo_nao_vende(self):
        r = await self._carteira().run({"BTC/USDT": self._pavio_no_alvo()})
        assert r.exits["alvo"] == 0

    async def test_pavio_que_so_toca_o_stop_tambem_nao_vende(self):
        """O lado simetrico: o ticker de 60 s tambem nao ve pavio no stop.

        Medido: 7% dos rompimentos do stop pelo OHLC diario nao tiveram leitura
        de 1 minuto abaixo do nivel. O motor vendia nesses dias; a producao nao.
        """
        candles = [
            *plano("BTC/USDT", 4), candle("BTC/USDT", 4, 100, 100, 95, 99),
            *depois("BTC/USDT", 5, 99),
        ]
        r = await self._carteira().run({"BTC/USDT": candles})
        assert r.exits["stop"] == 0

    async def test_os_dois_motores_usam_a_mesma_regra(self):
        cart = await self._carteira().run({"BTC/USDT": self._queda()})
        par = await self._um_par().run(self._queda())
        p_cart = next(t.price for t in cart.trades if t.exit_reason == "stop")
        p_par = next(t.price for t in par.trades if t.exit_reason == "stop")
        assert p_cart == p_par < Decimal("95")
        par_alvo = await self._um_par().run(self._pavio_no_alvo())
        assert par_alvo.exits["alvo"] == 0

    async def test_as_folgas_sao_configuraveis(self):
        r = await self._carteira(folgas=SEM_FOLGA).run({"BTC/USDT": self._pavio_no_alvo()})
        assert r.exits["alvo"] == 1
        r = await self._um_par(folgas=SEM_FOLGA).run(self._queda())
        assert next(t.price for t in r.trades if t.exit_reason == "stop") == Decimal("95")

    def test_folga_invalida_e_recusada(self):
        with pytest.raises(ValueError):
            FolgasDeProtecao(stop=Decimal("-0.01"))

    def test_o_stop_continua_vencendo_o_empate(self):
        saida = saida_de_protecao(
            Decimal(100), Decimal(130), Decimal(90), Decimal(95), Decimal(110)
        )
        assert saida is not None and saida[0] == "stop"


# ---------------------------------------------------------------------------
# 4. Periodo medido e comprar-e-segurar
# ---------------------------------------------------------------------------
class TestPeriodoComecaNoFimDoAquecimento:
    """`days` contava o aquecimento: taxa/mes e operacoes/mes subestimadas."""

    async def test_start_e_days_contam_do_primeiro_instante_operavel(self):
        r = await carteira([Roteiro({})], limites(["BTC/USDT"])).run(
            {"BTC/USDT": plano("BTC/USDT", 13)}
        )
        # Aquecimento de 3 candles: o primeiro instante operavel e o dia 3.
        assert r.start == INICIO + 3 * DIA
        assert r.days == pytest.approx(9)
        assert r.equity_curve[0][0] == r.start

    async def test_o_motor_de_um_par_tambem(self):
        motor = BacktestEngine(
            Roteiro({}), limites(["BTC/USDT"]), quote_currency="USDT",
            initial_balance=Decimal("1000"),
        )
        r = await motor.run(plano("BTC/USDT", 13))
        assert r.start == INICIO + 3 * DIA

    async def test_par_descartado_por_historico_curto_aparece_no_resultado(self):
        candles = {"BTC/USDT": plano("BTC/USDT", 13), "ETH/USDT": plano("ETH/USDT", 4)}
        r = await carteira([Roteiro({})], limites(list(candles))).run(candles)
        assert r.symbols == ["BTC/USDT"]
        assert "ETH/USDT" in r.pares_descartados
        assert "historico" in r.pares_descartados["ETH/USDT"]

    async def test_lista_vazia_de_um_par_nao_derruba_o_motor(self):
        """`reference = next(iter(...))[0]` levantava IndexError com o 1o par vazio."""
        candles = {"ETH/USDT": [], "BTC/USDT": plano("BTC/USDT", 13)}
        r = await carteira([Roteiro({})], limites(list(candles))).run(candles)
        assert r.symbols == ["BTC/USDT"]
        assert "ETH/USDT" in r.pares_descartados


# ---------------------------------------------------------------------------
# 5. Pares que entram tarde (vies de sobrevivencia declarado)
# ---------------------------------------------------------------------------
class TestParesQueEntramTarde:
    """Par listado no meio da janela entrava no comprar-e-segurar com outro periodo.

    BTC existe desde o dia 0; SOL so a partir do dia 10. O motor media o
    comprar-e-segurar de BTC desde o dia 3 e o de SOL desde o dia 13 e tirava a
    media dos dois como se fossem o mesmo periodo.
    """

    def _candles(self):
        btc = plano("BTC/USDT", 30)
        sol_inicio = INICIO + 10 * DIA
        sol = [
            candle("SOL/USDT", i, 10 + i, 10 + i, 10 + i, 10 + i, sol_inicio)
            for i in range(20)
        ]
        return {"BTC/USDT": btc, "SOL/USDT": sol}

    async def test_o_par_tardio_e_declarado_com_a_data_em_que_entrou(self):
        candles = self._candles()
        r = await carteira([Roteiro({})], limites(list(candles))).run(candles)
        assert r.pares_tardios == {"SOL/USDT": INICIO + 13 * DIA}
        assert set(r.symbols) == {"BTC/USDT", "SOL/USDT"}

    async def test_o_comprar_e_segurar_usa_uma_data_comum(self):
        candles = self._candles()
        r = await carteira([Roteiro({})], limites(list(candles))).run(candles)
        # Quem segurou desde o inicio nao podia ter comprado SOL: ele fica fora
        # da referencia, que mede todos os pares a partir da mesma data.
        assert set(r.first_prices) == {"BTC/USDT"}
        assert r.buy_and_hold_pct == pytest.approx(0.0)

    async def test_o_par_tardio_opera_depois_do_proprio_aquecimento(self):
        candles = self._candles()
        roteiro = {
            ("SOL/USDT", INICIO + 13 * DIA): (SignalDirection.LONG, 0.9),
        }
        r = await carteira([Roteiro(roteiro)], limites(list(candles))).run(candles)
        assert compras(r) == ["SOL/USDT"]


# ---------------------------------------------------------------------------
# 6. Cooldown carimbado no fechamento
# ---------------------------------------------------------------------------
class TestCooldownNoFechamento:
    """O stop do candle e o sinal do fechamento do MESMO candle tinham o mesmo `now`.

    Em producao o stop dispara durante o candle (horas antes do fechamento em
    1d) e o sinal sai no fechamento: com cooldown de 900 s a reentrada passa. O
    motor carimbava os dois com `open_time` e recusava por cooldown.
    """

    def _cenario(self):
        compra = Roteiro({("BTC/USDT", INICIO + 3 * DIA): (SignalDirection.LONG, 0.9)})
        recompra = Roteiro(
            {("BTC/USDT", INICIO + 4 * DIA): (SignalDirection.LONG, 0.9)}, name="outra"
        )
        risco = limites(["BTC/USDT"], max_order_pct_portfolio=0.45, cooldown_seconds=900)
        candles = [
            *plano("BTC/USDT", 4), candle("BTC/USDT", 4, 100, 100, 92, 93),
            *depois("BTC/USDT", 5, 93),
        ]
        return compra, recompra, risco, candles

    async def test_reentrada_apos_stop_no_mesmo_candle_passa_na_carteira(self):
        compra, recompra, risco, candles = self._cenario()
        r = await carteira([compra, recompra], risco).run({"BTC/USDT": candles})
        assert r.exits["stop"] == 1
        assert len(compras(r)) == 2, r.rejection_reasons

    async def test_reentrada_apos_stop_no_mesmo_candle_passa_no_motor_de_um_par(self):
        _, _, risco, candles = self._cenario()

        class CompraERecompra(Strategy):
            name = "compra_e_recompra"
            description = "teste"
            min_candles = 3

            def evaluate(self, market):
                quando = market.frame.index[-1].to_pydatetime()
                if quando not in (INICIO + 3 * DIA, INICIO + 4 * DIA):
                    return None
                return Signal(
                    exchange=market.exchange, symbol=market.symbol,
                    timeframe=market.timeframe, strategy=self.name,
                    direction=SignalDirection.LONG, confidence=0.9, reason="t",
                    reference_price=Decimal(str(market.frame["close"].iloc[-1])),
                )

        motor = BacktestEngine(
            CompraERecompra(), risco, quote_currency="USDT",
            initial_balance=Decimal("1000"), fee_pct=Decimal("0"),
            slippage_pct=Decimal("0"), lookback=50,
        )
        r = await motor.run(candles)
        assert r.exits["stop"] == 1
        assert sum(1 for t in r.trades if t.side == "buy") == 2, r.rejection_reasons

    async def test_duas_ordens_no_mesmo_fechamento_ainda_esbarram_no_cooldown(self):
        """O cooldown continua valendo entre ordens de verdade no mesmo instante."""
        compra, _, risco, _ = self._cenario()
        dobro = Roteiro(
            {("BTC/USDT", INICIO + 3 * DIA): (SignalDirection.LONG, 0.9)}, name="dobro"
        )
        r = await carteira([compra, dobro], risco).run({"BTC/USDT": plano("BTC/USDT", 6)})
        assert len(compras(r)) == 1
        assert any("cooldown" in m for m in r.rejection_reasons)
