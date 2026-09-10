"""Ataques do CRITICO ao item 2 (estrategias e indicadores).

Estes testes FALHAM contra o codigo atual de proposito: cada um prova uma
brecha que a rodada 1 de implementacao nao fechou. Nao devem ser "consertados"
mexendo no teste -- o que precisa mudar e a estrategia.

Contexto: a rodada 1 corrigiu a compra de confianca 0,90 em par morto colocando
uma guarda de **aritmetica exata** (`avg_gain == 0.0`) em `RsiReversion`. A
guarda fecha o caso sintetico (fechamentos identicos ao ultimo bit) e deixa
aberto o caso de verdade: um par sem liquidez tem movimento MINUSCULO, nao
movimento ZERO. Basta um tique de alta em qualquer barra da janela para
`avg_gain` deixar de ser exatamente zero, e a compra de 0,90 volta inteira.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from crypto_traders.domain.enums import ExchangeName, SignalDirection
from crypto_traders.domain.models import Candle
from crypto_traders.indicators import bollinger_bands, rsi_detail
from crypto_traders.strategies.base import MarketFrame
from crypto_traders.strategies.builtin import BollingerReversion, RsiReversion

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _candles(closes: list[str], symbol: str = "MORTO/USDC") -> list[Candle]:
    """Candles com preco em `Decimal` -- string, nunca float (D7)."""
    return [
        Candle(
            exchange=ExchangeName.BINANCE,
            symbol=symbol,
            timeframe="1d",
            open_time=BASE + timedelta(days=index),
            open=Decimal(value),
            high=Decimal(value),
            low=Decimal(value),
            close=Decimal(value),
            volume=Decimal("10"),
            closed=True,
        )
        for index, value in enumerate(closes)
    ]


def _signals(strategy, closes: list[str], direction: SignalDirection) -> list:
    """Varre a serie candle a candle e devolve os sinais da direcao pedida."""
    candles = _candles(closes)
    out = []
    for end in range(strategy.min_candles, len(candles) + 1):
        signal = strategy.evaluate(MarketFrame.from_candles(candles[:end]))
        if signal is not None and signal.direction is direction:
            out.append(signal)
    return out


# ---------------------------------------------------------------------------
# BRECHA 1 -- a guarda de janela degenerada e contornada por um unico tique
# ---------------------------------------------------------------------------
class TestGuardaDeJanelaDegeneradaEContornadaPorUmTique:
    """Par morto com UM tique de alta volta a comprar com confianca 0,90.

    A serie e a mesma que motivou a correcao (40 fechamentos identicos, um
    tique de baixa, a volta ao mesmo preco) com uma unica diferenca: um tique
    de alta de 1e-08 na barra 20. Isso tira `avg_gain` do zero exato, o
    `one_sided` fica False, e a estrategia emite a compra de confianca maxima
    sobre uma janela que continua sem informacao nenhuma.
    """

    #: 100,00000000 -> um tique de alta na barra 20 -> queda de um centavo -> volta.
    QUASE_MORTO = (
        ["100.00000000"] * 20
        + ["100.00000001"]
        + ["100.00000000"] * 19
        + ["99.99000000", "100.00000000"]
    )

    def test_a_janela_continua_sem_informacao(self):
        """Contraprova: a janela e tao morta quanto a que a guarda recusa.

        Sem esta parte, uma falha do teste seguinte poderia ser "a serie tem
        mercado de verdade", e nao "a guarda foi contornada". O movimento total
        da janela e de um centavo num preco de 100, e o unico tique de alta
        vale 1e-08.
        """
        closes = pd.Series([float(value) for value in self.QUASE_MORTO])
        reading = rsi_detail(closes, 14)

        # avg_gain nao e mais zero -- mas e 1e-10, sete ordens de grandeza
        # abaixo do avg_loss. A janela nao ficou informativa: so deixou de ser
        # exatamente zero.
        avg_gain = float(reading.avg_gain.iloc[-2])
        assert 0.0 < avg_gain < 1e-8
        assert not bool(reading.one_sided.iloc[-2]), "a guarda nao vai enxergar"
        # E a regra de D9 continua satisfeita, igual ao caso corrigido.
        assert reading.value.iloc[-2] <= 30.0 < reading.value.iloc[-1]

    def test_volta_a_emitir_a_compra_de_confianca_maxima(self):
        """FALHA HOJE: emite LONG com 0,90 sobre um par morto.

        Este e o defeito que a rodada 1 declarou corrigido. Ele nao esta
        corrigido: esta contornavel com um tique de 1e-08.
        """
        longs = _signals(RsiReversion(period=14), self.QUASE_MORTO, SignalDirection.LONG)
        assert longs == [], (
            "par morto voltou a comprar: "
            + ", ".join(
                f"conf={s.confidence:.2f} rsi={s.indicators.values['rsi']:.1f}" for s in longs
            )
        )

    def test_o_caso_realista_e_pior_ainda_porque_o_rsi_parece_legitimo(self):
        """FALHA HOJE: altcoin barato, e o RSI anterior nem parece degenerado.

        Par cotado a 0,00123 USDC (precisao normal de altcoin em /USDC) com um
        tique de 1e-08. O RSI anterior le 13,9 -- nao 0 -- entao nem uma
        inspecao humana do log identificaria a janela como degenerada. E a
        confianca emitida e 0,90, a maxima da estrategia.
        """
        preco = Decimal("0.00123000")
        tique = Decimal("0.00000001")
        closes = (
            [str(preco)] * 18
            + [str(preco + tique)]
            + [str(preco)] * 21
            + [str(preco - tique), str(preco)]
        )
        longs = _signals(RsiReversion(period=14), closes, SignalDirection.LONG)
        assert longs == [], (
            "altcoin morto comprou com "
            + ", ".join(
                f"conf={s.confidence:.2f} "
                f"rsi_anterior={s.indicators.values['rsi_previous']:.1f}"
                for s in longs
            )
        )


# ---------------------------------------------------------------------------
# BRECHA 2 -- bollinger_reversion tem o mesmo defeito, sem guarda nenhuma
# ---------------------------------------------------------------------------
class TestBollingerCompraOMesmoParMorto:
    """`bollinger_reversion` compra o par morto + um centavo com 0,62.

    Mesma familia da brecha do RSI, em outra estrategia do mesmo arquivo. Com
    19 fechamentos identicos e um de 99,99 na janela de 20, o desvio padrao
    fica minusculo, a banda tem 0,0087% de largura, e o %B salta de -0,59 para
    +0,56 com um movimento de um centavo -- que a estrategia le como
    "reingresso acima da banda inferior".
    """

    MORTO = ["100.00"] * 110 + ["99.99", "100.00"]

    def test_a_banda_e_larga_como_um_centavo(self):
        """Contraprova em numero: a banda nao tem largura economica."""
        closes = pd.Series([float(value) for value in self.MORTO])
        bands = bollinger_bands(closes, 20, 2.0)
        assert float(bands.bandwidth.iloc[-1]) < 1e-4  # 0,0087% da banda media
        assert bands.percent_b.iloc[-2] <= 0.0 < bands.percent_b.iloc[-1]

    def test_compra_o_par_morto(self):
        """FALHA HOJE: LONG com confianca 0,62 sobre um centavo de movimento."""
        longs = _signals(BollingerReversion(period=20), self.MORTO, SignalDirection.LONG)
        assert longs == [], (
            "bollinger comprou o par morto: "
            + ", ".join(
                f"conf={s.confidence:.2f} "
                f"bandwidth={s.indicators.values['bandwidth']:.2e}"
                for s in longs
            )
        )


# ---------------------------------------------------------------------------
# O que JA esta certo -- fixado para que uma correcao nao regrida
# ---------------------------------------------------------------------------
class TestOQueNaoPodeRegredirAoFecharAsBrechas:
    """A correcao das brechas acima nao pode calar a estrategia nem travar o
    fechamento de posicao (D4)."""

    def test_fechamento_nunca_pode_ser_bloqueado(self):
        """D4: duvida sobre o dado recusa ABRIR, jamais recusa FECHAR."""
        closes = [f"{100.0 + index:.2f}" for index in range(41)] + ["120.00"]
        flats = _signals(RsiReversion(period=14), closes, SignalDirection.FLAT)
        assert flats, "a estrategia perdeu a capacidade de fechar posicao"

    def test_sobrevenda_legitima_continua_comprando(self):
        """Mercado de verdade (queda com repiques e recuperacao) segue operando."""
        falling = [
            f"{100.0 - index * 1.5 + (1.0 if index % 3 == 0 else -0.4):.2f}"
            for index in range(40)
        ]
        recovering = [f"{float(falling[-1]) + index * 2.0:.2f}" for index in range(1, 12)]
        longs = _signals(RsiReversion(period=14), falling + recovering, SignalDirection.LONG)
        assert longs, "a guarda calou a estrategia num mercado de verdade"
        assert longs[0].indicators.values["rsi"] > 30.0


# ---------------------------------------------------------------------------
# Ataque 4 do dono, adaptado ao escopo: o agente nao alcanca a exchange
# ---------------------------------------------------------------------------
def test_chave_de_api_vazia_nao_muda_nada_para_a_estrategia():
    """Chave de API vazia e irrelevante aqui -- e isso e a propriedade desejada.

    O agente de estrategia nao tem broker, nao importa `exchanges` e publica
    unicamente em `Topics.SIGNALS`. O teste fixa a razao pela qual o ataque da
    chave vazia nao se aplica ao item 2: nao existe caminho da estrategia para
    a exchange que uma credencial pudesse habilitar.
    """
    import ast
    import pathlib

    proibidos = ("exchanges", "agents.execution", "broker", "ccxt")
    for caminho in [
        pathlib.Path("src/crypto_traders/agents/strategy.py"),
        *pathlib.Path("src/crypto_traders/strategies").glob("*.py"),
        *pathlib.Path("src/crypto_traders/indicators").glob("*.py"),
    ]:
        arvore = ast.parse(caminho.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            nomes: list[str] = []
            if isinstance(no, ast.Import):
                nomes = [alias.name for alias in no.names]
            elif isinstance(no, ast.ImportFrom):
                nomes = [f"{no.module or ''}.{alias.name}" for alias in no.names]
            for nome in nomes:
                assert not any(p in nome.lower() for p in proibidos), f"{caminho}: importa {nome}"


@pytest.mark.parametrize("strategy", [RsiReversion(), BollingerReversion()], ids=lambda s: s.name)
def test_nenhuma_estrategia_dimensiona_ordem(strategy):
    """A estrategia responde direcao e conviccao -- nunca tamanho nem stop."""
    tamanho = max(strategy.min_candles + 5, 130)
    closes = [f"{100.0 + (index % 7) * 0.5:.2f}" for index in range(tamanho)]
    for end in range(strategy.min_candles, len(closes) + 1):
        signal = strategy.evaluate(MarketFrame.from_candles(_candles(closes[:end])))
        if signal is None:
            continue
        for campo in ("quantity", "stop_loss", "take_profit", "notional"):
            assert not hasattr(signal, campo), f"Signal carrega {campo}"
