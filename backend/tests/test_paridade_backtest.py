"""Backtest e sistema real sobre os MESMOS candles: as decisoes coincidem?

O alvo de 8% e a regra das folgas (`backtest/protecao.py`) foram decididos pelo
`PortfolioBacktestEngine`. O que faltava (auditoria de 09/11/2026, backtest,
"Concerns" 3) era um teste que pusesse o motor e o sistema real lado a lado na
mesma serie: o simulador de cenario provava as protecoes do sistema real, e os
testes do motor provavam o motor contra ele mesmo.

Aqui o sistema real (`AgenteDeCenario`: orquestrador, cinco agentes, PaperBroker,
banco) anda candle a candle contra o `MercadoSimulado`; dentro de cada candle o
ticker passa pelas "leituras de 60 s" do roteiro. Terminado o roteiro, os candles
que o simulador SERVIU ao sistema vao, tal como estao, ao `PortfolioBacktestEngine`
com a mesma configuracao. Os dois desfechos sao comparados trade a trade.

O que tem de coincidir exatamente: em que candle cada compra e cada saida
acontece, o motivo da saida, a quantidade, e o preco das compras e das saidas
pela estrategia (as duas pontas executam no fechamento).

O que coincide dentro da folga documentada: o preco das saidas de protecao. A
producao vende na primeira leitura alem do nivel; o motor, no nivel menos a
folga calibrada (`FOLGAS_PADRAO.stop`, 0,13%), e o alvo no proprio nivel. A
tolerancia e o p90 medido da distancia entre a primeira leitura e o nivel
(0,31%, docstring de `protecao.py`).

E o que diverge POR DESENHO, cada caso com o seu teste: o pavio que nenhuma
leitura viu (o motor so tem o OHLC) e o salto dentro de uma leitura (a cadencia
de 60 s). A divergencia esperada e a que o modelo de folgas preve, nem mais nem
menos -- e e por isso que ela e conferida pelo valor, e nao so pelo sinal.

Fora do alcance de proposito: cooldown e circuit breaker medem o relogio de
parede no sistema real e o roteiro anda dias em segundos -- ficam desligados
(cooldown 0, limites de perda folgados) nos dois lados.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import pytest

from crypto_traders.backtest.portfolio import PortfolioBacktestEngine
from crypto_traders.backtest.protecao import FOLGAS_PADRAO
from crypto_traders.config import RiskSettings, Settings, TradingSettings
from crypto_traders.db.session import dispose_engine
from crypto_traders.domain.enums import Side
from crypto_traders.simulacao import AgenteDeCenario, MercadoSimulado
from crypto_traders.strategies import get_strategy

pytestmark = pytest.mark.cenario

SIMBOLO = "BTC/USDT"
TAXA = Decimal("0.001")
SLIPPAGE = Decimal("0.0005")
STOP = Decimal("0.05")
ALVO = Decimal("0.08")
HISTORICO_LIMITE = 100

#: Queda lenta e regular: a media rapida fica abaixo da lenta a serie inteira,
#: entao nenhum dos dois lados opera no historico (o sistema real nem o avalia).
HISTORICO = [Decimal(56000 - 100 * i) for i in range(61)]  # 56000 -> 50000

#: p90 da distancia entre a primeira leitura de 60 s alem do nivel e o nivel,
#: medido em candles de 1 minuto da Binance (docstring de `protecao.py`).
TOLERANCIA_DA_LEITURA = Decimal("0.0031")

#: Roteiro principal: uma lista de leituras de 60 s por candle; a ULTIMA e o
#: fechamento. Desenhado com a propria `ma_crossover` (EMA 9/21):
#: - rodada 1: cruza para cima no passo 2 (53500) e para baixo no 10 (51000),
#:   sem tocar stop nem alvo -> saida pela estrategia;
#: - rodada 2: cruza para cima no 19 (53000); no 20 o preco despenca dentro do
#:   candle e a primeira leitura abaixo do stop (~50425) e 50380 -> stop;
#: - rodada 3: cruza para cima no 30 (53000); no 31 sobe ate a leitura 57450,
#:   0,22% acima do alvo (~57325) -> alvo.
ROTEIRO: list[list[int]] = [
    [51500], [52500], [53500], [53000], [52200], [51600], [51400], [51300],
    [51300], [51200], [51000],
    [50800], [50500], [50000], [49500], [49000], [50500], [51500], [52500],
    [53000], [52500, 50380, 49800, 50000],
    [49500], [49000], [48500], [48000], [47500], [47000], [48500], [50000],
    [51500], [53000], [54000, 56000, 57450, 57000],
]

#: (passo, lado, motivo da saida) que o roteiro tem de produzir nos DOIS lados.
EVENTOS_DO_ROTEIRO = [
    (2, "buy", None),
    (10, "sell", "estrategia"),
    (19, "buy", None),
    (20, "sell", "stop"),
    (30, "buy", None),
    (31, "sell", "alvo"),
]


@dataclass(frozen=True)
class Evento:
    passo: int
    lado: str
    motivo: str | None
    quantidade: Decimal
    preco: Decimal
    nivel: Decimal | None = None
    """Nivel de stop/alvo que o Risk Manager real gravou na saida de protecao."""

    @property
    def chave(self) -> tuple[int, str, str | None]:
        return (self.passo, self.lado, self.motivo)


def _risco() -> RiskSettings:
    return RiskSettings(
        max_order_notional=Decimal("100"),
        max_order_pct_portfolio=0.10,
        max_asset_exposure_pct=0.50,
        max_open_positions=3,
        min_order_notional=Decimal("10"),
        stop_loss_pct=float(STOP),
        take_profit_pct=float(ALVO),
        # Relogio de parede no sistema real, dias em segundos no roteiro: fora.
        daily_loss_limit_pct=0.9,
        weekly_loss_limit_pct=0.9,
        cooldown_seconds=0,
        min_signal_confidence=0.55,
        asset_whitelist=["BTC", "USDT"],
        symbol_whitelist=[SIMBOLO],
    )


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'paridade.db').as_posix()}",
        trading=TradingSettings(
            symbols=[SIMBOLO],
            timeframe="1d",
            strategies=["ma_crossover"],
            quote_currency="USDT",
            candle_history_limit=HISTORICO_LIMITE,
            market_data_interval_seconds=3600,
            portfolio_interval_seconds=3600,
            signal_batch_window_seconds=0,
            paper_initial_balance=Decimal("1000"),
            paper_fee_pct=TAXA,
            paper_slippage_pct=SLIPPAGE,
        ),
        risk=_risco(),
    )


_MOTIVO_DA_PRODUCAO = {"stop_loss": "stop", "take_profit": "alvo"}


async def _sistema_real(
    tmp_path, roteiro: Sequence[Sequence[int]], extremos: dict | None = None
) -> tuple[list[Evento], list]:
    """Conduz o sistema real pelo roteiro; devolve os eventos e os candles servidos.

    Em cada candle: uma volta (`ciclo`: coleta + retrato, onde stop e alvo sao
    avaliados) por leitura intermediaria, depois o fechamento com o ticker no
    preco de fechamento -- e outra volta, em que a estrategia decide. A faixa do
    candle e a das leituras, salvo `extremos[passo] = (maxima, minima)`, que
    roteiriza um pavio que nenhuma leitura viu.
    """
    extremos = extremos or {}
    await dispose_engine()
    mercado = MercadoSimulado(timeframe="1d", reservados=len(roteiro))
    mercado.adicionar_par(SIMBOLO, HISTORICO)
    eventos: list[Evento] = []
    async with AgenteDeCenario(_settings(tmp_path), mercado) as cenario:
        vistos = 0
        saidas_vistas = 0
        for passo, leituras in enumerate(roteiro):
            abertura = mercado.ultimo_fechamento(SIMBOLO)
            for leitura in leituras[:-1]:
                cenario.definir_preco(SIMBOLO, leitura)
                await cenario.ciclo()
            fechamento = leituras[-1]
            faixa = [abertura, *map(Decimal, leituras)]
            maxima, minima = extremos.get(passo, (max(faixa), min(faixa)))
            mercado.fechar_candle(
                SIMBOLO, fechamento, maxima=maxima, minima=minima, preco=fechamento
            )
            await cenario.ciclo()

            trades = await cenario.trades()
            saidas = await cenario.saidas_de_protecao()
            motivos = [
                (_MOTIVO_DA_PRODUCAO[s.snapshot["motivo"]], Decimal(s.snapshot["nivel"]))
                for s in saidas[saidas_vistas:]
            ]
            saidas_vistas = len(saidas)
            for trade in trades[vistos:]:
                nivel = None
                if trade.side == str(Side.BUY):
                    motivo = None
                elif trade.strategy == "protecao":
                    motivo, nivel = motivos.pop(0)
                else:
                    motivo = "estrategia"
                eventos.append(
                    Evento(
                        passo, trade.side, motivo,
                        Decimal(str(trade.quantity)), Decimal(str(trade.price)), nivel,
                    )
                )
            vistos = len(trades)
    candles = [c for c in await mercado.fetch_candles(SIMBOLO, "1d", 1000) if c.closed]
    return eventos, candles


async def _backtest(candles) -> list[Evento]:
    """O motor de carteira, com a configuracao do sistema real, nos mesmos candles."""
    motor = PortfolioBacktestEngine(
        [get_strategy("ma_crossover")],
        _risco(),
        quote_currency="USDT",
        initial_balance=Decimal("1000"),
        fee_pct=TAXA,
        slippage_pct=SLIPPAGE,
        lookback=HISTORICO_LIMITE,
        signal_batch_window_seconds=0,
    )
    resultado = await motor.run({SIMBOLO: candles})
    passo_de = {c.open_time: i - len(HISTORICO) for i, c in enumerate(candles)}
    return [
        Evento(passo_de[t.timestamp], t.side, t.exit_reason, t.quantity, t.price)
        for t in resultado.trades
    ]


def _nivel(compra: Evento, fracao: Decimal) -> Decimal:
    """Nivel de protecao da PRIMEIRA compra (sem poeira de rodada anterior).

    A taxa da compra sai no BTC: o custo pago e `quantidade x preco` e a posicao
    e `quantidade x (1 - taxa)`. Depois de uma venda, a poeira que o passo do
    par deixa entra no medio da compra seguinte -- dai o roteiro principal ler o
    nivel que o Risk Manager real gravou, e nao esta conta.
    """
    medio = compra.preco / (1 - TAXA)
    return medio * (1 + fracao)


# ======================================================================
# Mesmo roteiro, mesmas decisoes
# ======================================================================
async def test_mesmos_candles_mesmas_decisoes_e_protecao_dentro_da_folga(tmp_path):
    """Um roteiro so (o sistema real leva segundos para anda-lo), duas perguntas.

    1. Decisoes: o mesmo candle, lado, motivo e quantidade em cada trade; compra
       e saida pela estrategia no mesmo preco (as duas pontas no fechamento).
    2. Protecao: o preco difere, e a diferenca cabe na leitura de 60 s. Stop: a
       producao vendeu na leitura 50380; o motor, no nivel menos 0,13% -- os dois
       alem do nivel, a menos de 0,31% (p90 medido) um do outro. Alvo: a producao
       vendeu na leitura 57450; o motor vende NO nivel, sem o credito da leitura
       -- nunca acima do que a producao recebeu.
    """
    real, candles = await _sistema_real(tmp_path, ROTEIRO)
    simulado = await _backtest(candles)

    assert [e.chave for e in real] == EVENTOS_DO_ROTEIRO, real
    assert [e.chave for e in simulado] == EVENTOS_DO_ROTEIRO, simulado
    for r, s in zip(real, simulado, strict=True):
        assert r.quantidade == s.quantidade, (r, s)
        if r.motivo in (None, "estrategia"):
            assert r.preco == s.preco, (r, s)

    por_chave = {e.chave: e for e in simulado}

    stop_real = next(e for e in real if e.motivo == "stop")
    # O nivel do Risk Manager real (medio com a poeira da rodada 1 incluida):
    # o motor tem de ter chegado ao MESMO nivel para o preco abaixo bater.
    stop_nivel = stop_real.nivel
    stop_bt = por_chave[stop_real.chave]
    assert stop_real.preco < stop_nivel and stop_bt.preco < stop_nivel
    assert stop_bt.preco == pytest.approx(stop_nivel * (1 - FOLGAS_PADRAO.stop) * (1 - SLIPPAGE))
    assert abs(stop_real.preco - stop_bt.preco) / stop_nivel <= TOLERANCIA_DA_LEITURA

    alvo_real = next(e for e in real if e.motivo == "alvo")
    alvo_nivel = alvo_real.nivel
    assert alvo_real.preco > alvo_nivel
    alvo_bt = por_chave[alvo_real.chave]
    assert alvo_bt.preco == pytest.approx(alvo_nivel * (1 - SLIPPAGE))
    assert Decimal(0) <= (alvo_real.preco - alvo_bt.preco) / alvo_nivel <= TOLERANCIA_DA_LEITURA


# ======================================================================
# Divergencias por desenho
# ======================================================================
#: Ate o passo 2: compra no cruzamento a 53500 (stop ~50901, alvo ~57867).
ABERTURA = [[51500], [52500], [53500]]


async def _compra_e_um_candle(tmp_path, leituras, extremos=None):
    roteiro = [*ABERTURA, leituras]
    real, candles = await _sistema_real(tmp_path, roteiro, {3: extremos} if extremos else None)
    simulado = await _backtest(candles)
    compra = real[0]
    assert compra.chave == (2, "buy", None) and simulado[0].chave == compra.chave
    return real, simulado, compra


async def test_pavio_raso_no_stop_nenhum_dos_dois_vende(tmp_path):
    """A minima passa do stop por menos que a folga de pavio (0,15%): coincidem.

    Nenhuma leitura de 60 s viu o pavio, entao a producao nao vende; o motor so
    tem o OHLC, e a folga `pavio_stop` e o que o impede de vender. Sem ela este
    seria o falso stop que a calibracao achou em 7% dos rompimentos.
    """
    minima = Decimal("50880")
    real, simulado, compra = await _compra_e_um_candle(
        tmp_path, [53000], (Decimal("53500"), minima)
    )
    stop = _nivel(compra, -STOP)
    assert stop * (1 - FOLGAS_PADRAO.pavio_stop) < minima < stop
    assert [e.chave for e in real] == [(2, "buy", None)]
    assert [e.chave for e in simulado] == [(2, "buy", None)]


async def test_pavio_fundo_que_o_ticker_nao_viu_o_backtest_vende_e_a_producao_nao(tmp_path):
    """Divergencia esperada, do lado PESSIMISTA: o motor vende um stop que nao houve.

    O candle desce 1,8% abaixo do stop num pavio entre duas leituras. A producao
    segue posicionada; o motor vende no nivel menos a folga. E o caso que a
    calibracao mediu (24 dos 366 rompimentos) e que as folgas nao cobrem -- elas
    so tiram o pavio RASO.
    """
    minima = Decimal("50000")
    real, simulado, compra = await _compra_e_um_candle(
        tmp_path, [53000], (Decimal("53500"), minima)
    )
    stop = _nivel(compra, -STOP)
    assert minima < stop * (1 - FOLGAS_PADRAO.pavio_stop)
    assert [e.chave for e in real] == [(2, "buy", None)], "a producao viu um pavio que nao leu"
    assert [e.chave for e in simulado] == [(2, "buy", None), (3, "sell", "stop")]
    assert simulado[1].preco == pytest.approx(stop * (1 - FOLGAS_PADRAO.stop) * (1 - SLIPPAGE))


async def test_pavio_fundo_no_alvo_o_backtest_realiza_um_lucro_que_a_producao_nao_viu(tmp_path):
    """Divergencia esperada, do lado OTIMISTA: o alvo que so o OHLC viu.

    A folga de 0,15% no alvo tira o pavio raso (como acima); um pavio maior que
    ela, entre duas leituras, ainda vira lucro no motor. Medido na calibracao:
    13 falsos alvos em 718 com a folga. E o vies que mais pesa num alvo distante.
    """
    maxima = Decimal("58500")
    real, simulado, compra = await _compra_e_um_candle(
        tmp_path, [53000], (maxima, Decimal("53000"))
    )
    alvo = _nivel(compra, ALVO)
    assert maxima > alvo * (1 + FOLGAS_PADRAO.alvo)
    assert [e.chave for e in real] == [(2, "buy", None)]
    assert [e.chave for e in simulado] == [(2, "buy", None), (3, "sell", "alvo")]
    assert simulado[1].preco == pytest.approx(alvo * (1 - SLIPPAGE))


async def test_salto_dentro_de_uma_leitura_o_backtest_vende_melhor_que_a_producao(tmp_path):
    """Divergencia esperada da cadencia de 60 s, do lado OTIMISTA.

    O preco salta de 52800 a 49000 entre duas leituras (3,7% abaixo do stop). A
    producao vende na leitura, a 49000; o motor, no nivel menos 0,13%. A folga e
    a MEDIA medida (0,13%, maximo 0,84%): um salto alem do maximo fica fora dela,
    e o motor ganha exatamente a distancia entre o nivel com folga e a leitura.
    """
    real, simulado, compra = await _compra_e_um_candle(tmp_path, [52800, 49000, 49500])
    stop = _nivel(compra, -STOP)
    assert [e.chave for e in real][:2] == [(2, "buy", None), (3, "sell", "stop")]
    assert [e.chave for e in simulado][:2] == [(2, "buy", None), (3, "sell", "stop")]
    venda_real, venda_bt = real[1], simulado[1]
    assert venda_real.preco == Decimal("49000") * (1 - SLIPPAGE)
    esperado = (stop * (1 - FOLGAS_PADRAO.stop) - Decimal("49000")) * (1 - SLIPPAGE)
    assert venda_bt.preco - venda_real.preco == pytest.approx(esperado)
    assert (venda_bt.preco - venda_real.preco) / stop > TOLERANCIA_DA_LEITURA
