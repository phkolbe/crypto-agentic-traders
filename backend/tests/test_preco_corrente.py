"""Preco corrente (B1/B2), rodada do critico: cada defeito confirmado, provado.

1. Sem preco corrente recente, stop e alvo NAO sao avaliados -- nunca contra o
   fechamento do candle (venda fantasma: medio 1,3969 da execucao no mercado,
   fechamento 1,2957 abaixo do stop 1,3550).
2. Um simbolo ruim no lote (BadSymbol) nao cega os outros pares.
3. Salto grande numa leitura so e suspeito; a leitura seguinte confirma o
   movimento (colapso real e aceito) ou o desfaz (numero corrompido e ignorado).
4. A primeira leitura do processo e julgada contra o candle, nao aceita as cegas.
5. O operador recebe ALERTA, nao so uma linha de log.
6. A ordem e dimensionada no preco corrente, nao no fechamento do sinal.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from helpers import com_negocio
from test_critico_item3_ataques import agente, snapshot
from test_market_data import BusEspiao, FonteFalsa, candle, fechado_em

from crypto_traders.agents.market_data import MarketDataAgent
from crypto_traders.bus import Topics
from crypto_traders.domain.enums import ExchangeName, SignalDirection
from crypto_traders.domain.models import Signal, Ticker
from crypto_traders.exchanges.base import ExchangeError


class FonteComTicker(FonteFalsa):
    """Fonte falsa com ticker programavel por par e lote que pode falhar."""

    def __init__(self, respostas, precos):
        super().__init__(respostas)
        self.precos: dict[str, Decimal | Exception] = precos
        self.lote_falha: Exception | None = None

    async def fetch_ticker(self, symbol):
        valor = self.precos[symbol]
        if isinstance(valor, Exception):
            raise valor
        return Ticker(exchange=ExchangeName.BINANCE, symbol=symbol, price=valor)

    async def fetch_tickers(self, symbols):
        if self.lote_falha is not None:
            raise self.lote_falha
        return {s: await self.fetch_ticker(s) for s in symbols}


async def montar(settings, fechamentos: dict[str, str], precos: dict[str, str]):
    respostas = {s: [[candle(s, open_time=fechado_em(1), close=c)]] for s, c in fechamentos.items()}
    configurado = com_negocio(
        settings, symbols=list(respostas), timeframe="1d", candle_history_limit=50,
        quote_currency="USDT",
    )
    bus = BusEspiao()
    await bus.start()
    fonte = FonteComTicker(respostas, {s: Decimal(p) for s, p in precos.items()})
    return MarketDataAgent(bus, fonte, configurado), bus, fonte


def _envelhecer(agente: MarketDataAgent, chave: str, minutos: int = 10) -> None:
    agente._precos_correntes_em[chave] -= timedelta(minutes=minutos)


class TestSemPrecoCorrenteNaoHaStopFantasma:
    async def test_preco_velho_sai_da_avaliacao_e_nao_volta_ao_fechamento(self, settings):
        dados, _, _ = await montar(settings, {"XRP/USDT": "1.2957"}, {"XRP/USDT": "1.3962"})
        await dados.refresh()
        assert dados.precos_de_mercado() == {"XRP": Decimal("1.3962")}

        _envelhecer(dados, "XRP")
        assert dados.precos_de_mercado() == {}, "preco velho nao decide stop"
        # A valorizacao usa o ULTIMO preco de mercado, nunca o fechamento.
        assert dados.precos_para_avaliacao()["XRP"] == Decimal("1.3962")
        assert dados.pares_sem_preco_corrente == ["XRP"]
        # Contraprova: o fechamento estava abaixo do stop de 3% do medio 1,3969.
        assert dados.latest_prices["XRP"] <= Decimal("1.3969") * Decimal("0.97")

    async def test_risk_manager_nao_avalia_ativo_sem_preco_corrente(self, settings):
        rm = await agente(settings, stop=0.03, alvo=0.10)
        rm.definir_precos_de_mercado(dict)  # ninguem com preco corrente
        assert await rm.enforce_protective_exits(snapshot("1.3969", "1.2957")) == []

    async def test_contraprova_sem_a_fonte_o_mesmo_snapshot_vende(self, settings):
        rm = await agente(settings, stop=0.03, alvo=0.10)
        assert len(await rm.enforce_protective_exits(snapshot("1.3969", "1.2957"))) == 1

    async def test_stop_usa_o_preco_corrente_e_nao_o_do_snapshot(self, settings):
        rm = await agente(settings, stop=0.03, alvo=0.10)
        rm.definir_precos_de_mercado(lambda: {"BTC": Decimal("1.30")})
        ordens = await rm.enforce_protective_exits(snapshot("1.3969", "1.40"))
        assert len(ordens) == 1, "o mercado (1,30) rompeu o stop; o snapshot (1,40) nao"


class TestUmSimboloRuimNaoCegaOsOutros:
    async def test_lote_com_badsymbol_cai_para_par_a_par(self, settings):
        dados, _, fonte = await montar(
            settings,
            {"BTC/USDT": "100", "ETH/USDT": "10"},
            {"BTC/USDT": "101", "ETH/USDT": "10.1"},
        )
        fonte.lote_falha = ExchangeError("binance does not have market symbol FOO/USDT")
        fonte.precos["ETH/USDT"] = ExchangeError("BadSymbol")
        await dados.refresh()
        assert dados.precos_de_mercado() == {"BTC": Decimal("101")}
        assert dados.pares_sem_preco_corrente == ["ETH"]


class TestSaltoPrecisaDeConfirmacao:
    async def test_decimal_trocado_numa_leitura_e_ignorado(self, settings):
        dados, _, fonte = await montar(settings, {"ADA/USDT": "1.0"}, {"ADA/USDT": "1.05"})
        await dados.refresh()
        fonte.precos["ADA/USDT"] = Decimal("0.105")  # passava no filtro antigo de 10x
        await dados.refresh()
        assert dados.precos_correntes["ADA"] == Decimal("1.05")
        fonte.precos["ADA/USDT"] = Decimal("1.04")
        await dados.refresh()
        assert dados.precos_correntes["ADA"] == Decimal("1.04")
        assert dados._suspeitos == {}

    async def test_colapso_real_e_aceito_na_segunda_leitura(self, settings):
        """-91%: o filtro antigo recusava para sempre e mascarava o stop."""
        dados, _, fonte = await montar(settings, {"SOL/USDT": "1.0"}, {"SOL/USDT": "1.0"})
        await dados.refresh()
        fonte.precos["SOL/USDT"] = Decimal("0.09")
        await dados.refresh()
        assert dados.precos_correntes["SOL"] == Decimal("1.0")
        fonte.precos["SOL/USDT"] = Decimal("0.085")
        await dados.refresh()
        assert dados.precos_correntes["SOL"] == Decimal("0.085")

    async def test_primeira_leitura_absurda_nao_vale(self, settings):
        """Antes, a primeira leitura do processo nao tinha referencia e entrava."""
        dados, _, _ = await montar(settings, {"BTC/USDT": "60000"}, {"BTC/USDT": "0.0001"})
        await dados.refresh()
        assert "BTC" not in dados.precos_de_mercado()
        assert dados.latest_prices["BTC"] == Decimal("60000")

    async def test_primeira_leitura_normal_vale_no_mesmo_ciclo(self, settings):
        dados, _, _ = await montar(settings, {"BTC/USDT": "60000"}, {"BTC/USDT": "61000"})
        await dados.refresh()
        assert dados.precos_de_mercado() == {"BTC": Decimal("61000")}


class TestAlerta:
    async def test_alerta_uma_vez_e_aviso_de_volta(self, settings):
        dados, bus, fonte = await montar(settings, {"BTC/USDT": "100"}, {"BTC/USDT": "100"})
        await dados.refresh()
        fonte.precos["BTC/USDT"] = ExchangeError("503")
        fonte.lote_falha = ExchangeError("503")
        _envelhecer(dados, "BTC")
        await dados.refresh()
        await dados.refresh()
        alertas = [p for t, p in bus.publicados if t == Topics.ALERTS]
        assert [a["type"] for a in alertas] == ["preco_corrente_ausente"]

        fonte.lote_falha = None
        fonte.precos["BTC/USDT"] = Decimal("101")
        await dados.refresh()
        alertas = [p for t, p in bus.publicados if t == Topics.ALERTS]
        assert alertas[-1]["type"] == "preco_corrente_restabelecido"


class TestDimensionamentoNoPrecoCorrente:
    async def test_estado_do_risco_usa_o_mercado_e_nao_o_fechamento(self, settings):
        rm = await agente(settings)
        rm._snapshot = snapshot("1.3", "1.3")
        sinal = Signal(
            strategy="ma_crossover",
            exchange=ExchangeName.BINANCE,
            symbol="BTC/USDT",
            timeframe="1d",
            direction=SignalDirection.LONG,
            confidence=0.8,
            reason="cruzamento",
            reference_price=Decimal("1.2957"),
        )
        assert rm._preco_de_dimensionamento(sinal) == Decimal("1.2957")
        rm.definir_precos_de_mercado(lambda: {"BTC": Decimal("1.3962")})
        estado = await rm._build_state(sinal)
        assert estado.prices["BTC"] == Decimal("1.3962")


async def _gravar_carteira(settings, *posicoes) -> None:
    """Grava um retrato com estas posicoes, como o Portfolio Agent faria."""
    from crypto_traders.db.repositories import PortfolioSnapshotRepository
    from crypto_traders.db.session import session_scope
    from crypto_traders.domain.models import PortfolioSnapshot, Position

    async with session_scope(settings) as session:
        await PortfolioSnapshotRepository(session).save(
            PortfolioSnapshot(
                total_value=Decimal("1000"), cash_value=Decimal("900"),
                positions_value=Decimal("100"),
                positions=[
                    Position(
                        exchange=ExchangeName.BINANCE, asset=asset,
                        quantity=Decimal(quantidade),
                        average_price=Decimal(medio) if medio is not None else None,
                        current_price=Decimal(atual),
                    )
                    for asset, quantidade, medio, atual in posicoes
                ],
            ),
            str(settings.trading_mode),
        )


class TestParForaDoUniversoContinuaVigiado:
    """P0 #7: o universo define o que se pode ABRIR, nao o que se protege.

    Carteira com XRP; a revarredura tira XRP/USDT do universo (ou o operador
    edita SYMBOLS). Antes: o ticker de XRP deixava de ser lido, em 3 min sumia
    de `precos_de_mercado`, o Risk pulava o stop -- e o aviso de frescor so
    olhava os pares ativos, entao NENHUM alerta saia.
    """

    async def test_par_removido_do_universo_com_posicao_continua_com_ticker(self, settings):
        dados, _, fonte = await montar(
            settings,
            {"BTC/USDT": "100", "XRP/USDT": "1.30"},
            {"BTC/USDT": "101", "XRP/USDT": "1.31"},
        )
        await _gravar_carteira(settings, ("XRP", "10", "1.30", "1.31"))
        await dados.refresh()
        assert dados.precos_de_mercado()["XRP"] == Decimal("1.31")

        # O operador tira XRP da lista. A posicao continua na carteira.
        dados._settings = com_negocio(dados._settings, symbols=["BTC/USDT"])
        fonte.precos["XRP/USDT"] = Decimal("1.20")
        _envelhecer(dados, "XRP")
        await dados.refresh()
        assert dados.precos_de_mercado()["XRP"] == Decimal("1.20")

    async def test_par_fora_do_universo_sem_preco_gera_alerta(self, settings):
        dados, bus, fonte = await montar(settings, {"BTC/USDT": "100"}, {"BTC/USDT": "101"})
        fonte.precos["XRP/USDT"] = ExchangeError("503")
        await _gravar_carteira(settings, ("XRP", "10", "1.30", "1.31"))
        await dados.refresh()
        alertas = [p for t, p in bus.publicados if t == Topics.ALERTS]
        assert [a["type"] for a in alertas] == ["preco_corrente_ausente"]
        assert "XRP" in alertas[0]["title"]

    async def test_primeira_leitura_do_par_fora_do_universo_vale_no_mesmo_ciclo(self, settings):
        """Sem candle do par, a referencia do salto e o preco do retrato."""
        dados, _, fonte = await montar(settings, {"BTC/USDT": "100"}, {"BTC/USDT": "101"})
        fonte.precos["XRP/USDT"] = Decimal("1.32")
        await _gravar_carteira(settings, ("XRP", "10", "1.30", "1.31"))
        await dados.refresh()
        assert dados.precos_de_mercado()["XRP"] == Decimal("1.32")

    async def test_posicao_sem_preco_medio_nao_entra(self, settings):
        """Sem preco medio nao ha stop: buscar ticker dela so arriscaria o lote."""
        dados, _, _ = await montar(settings, {"BTC/USDT": "100"}, {"BTC/USDT": "101"})
        await _gravar_carteira(settings, ("DOGE", "10", None, "0.1"))
        await dados.refresh()
        assert dados.pares_sem_preco_corrente == []


class TestRiskAvisaPosicaoSemPreco:
    """O `continue` silencioso do Risk para ativo sem preco corrente (P0 #7)."""

    async def test_alerta_uma_vez_por_transicao(self, settings):
        import asyncio

        rm = await agente(settings, stop=0.03, alvo=0.10)
        rm.definir_precos_de_mercado(dict)
        alertas: list[dict] = []

        async def escuta():
            async for alerta in rm.bus.subscribe(Topics.ALERTS):
                alertas.append(alerta)

        tarefa = asyncio.create_task(escuta())
        await asyncio.sleep(0)
        for _ in range(3):
            await rm.enforce_protective_exits(snapshot("1.3969", "1.40"))
        rm.definir_precos_de_mercado(lambda: {"BTC": Decimal("1.40")})
        await rm.enforce_protective_exits(snapshot("1.3969", "1.40"))
        rm.definir_precos_de_mercado(dict)
        await rm.enforce_protective_exits(snapshot("1.3969", "1.40"))
        await asyncio.sleep(0.05)
        tarefa.cancel()

        tipos = [a["type"] for a in alertas]
        assert tipos.count("position_without_current_price") == 2
        assert "BTC" in alertas[0]["title"]


class TestAberturaSemPrecoCorrente:
    """M3: sem ticker recente, a ABERTURA e recusada; o FLAT continua passando.

    Antes, o dimensionamento caia no fechamento do candle: LONG do candle 1d
    com o fechamento de ontem 100 e o mercado a 108 dimensionava a 100 e
    gastava 8% acima do teto -- contra o que o SEGURANCA afirma.
    """

    def _sinal(self, direcao):
        return Signal(
            strategy="ma_crossover", exchange=ExchangeName.BINANCE, symbol="BTC/USDT",
            timeframe="1d", direction=direcao, confidence=0.8, reason="teste",
            reference_price=Decimal("1.30"),
        )

    async def _avaliar(self, rm, sinal):
        import asyncio

        avaliacoes: list = []

        async def escuta():
            async for a in rm.bus.subscribe(Topics.RISK_ASSESSMENTS):
                avaliacoes.append(a)

        tarefa = asyncio.create_task(escuta())
        await asyncio.sleep(0)
        await rm._on_batch([sinal])
        await asyncio.sleep(0.05)
        tarefa.cancel()
        return avaliacoes[0]

    async def test_long_sem_preco_corrente_e_recusado(self, settings):
        from crypto_traders.domain.enums import RiskDecision

        rm = await agente(settings)
        rm.observe_snapshot(snapshot("1.3", "1.3"))
        rm.definir_precos_de_mercado(dict)
        avaliacao = await self._avaliar(rm, self._sinal(SignalDirection.LONG))
        assert avaliacao.decision is RiskDecision.REJECTED
        assert any("preco corrente" in r for r in avaliacao.reasons)

    async def test_flat_sem_preco_corrente_continua_passando(self, settings):
        from crypto_traders.domain.enums import RiskDecision

        rm = await agente(settings)
        rm.observe_snapshot(snapshot("1.3", "1.3", quantidade="100"))
        rm.definir_precos_de_mercado(dict)
        avaliacao = await self._avaliar(rm, self._sinal(SignalDirection.FLAT))
        assert avaliacao.decision is RiskDecision.APPROVED


class TestConfirmacaoDoSaltoPorProximidade:
    """Auditoria de 09/11/2026 (dados, low): o salto era confirmado so pelo LADO.

    Duas leituras absurdas, 0,01x e 0,3x da referencia, distam 30x uma da outra e
    se "confirmavam" por estarem ambas abaixo. E o suspeito nunca expirava: um
    suspeito de horas atras confirmava a leitura nova.
    """

    async def test_leituras_distantes_entre_si_nao_se_confirmam(self, settings):
        dados, _, fonte = await montar(settings, {"ADA/USDT": "1.0"}, {"ADA/USDT": "1.0"})
        await dados.refresh()
        fonte.precos["ADA/USDT"] = Decimal("0.01")
        await dados.refresh()
        fonte.precos["ADA/USDT"] = Decimal("0.3")
        await dados.refresh()
        assert dados.precos_correntes["ADA"] == Decimal("1.0"), (
            "0,3 confirmou 0,01 so por estar do mesmo lado"
        )

    async def test_suspeito_velho_nao_confirma(self, settings):
        dados, _, fonte = await montar(settings, {"SOL/USDT": "1.0"}, {"SOL/USDT": "1.0"})
        await dados.refresh()
        fonte.precos["SOL/USDT"] = Decimal("0.09")
        await dados.refresh()
        preco, em = dados._suspeitos["SOL"]
        dados._suspeitos["SOL"] = (preco, em - dados.idade_maxima_do_preco() - timedelta(seconds=1))
        fonte.precos["SOL/USDT"] = Decimal("0.085")
        await dados.refresh()
        assert dados.precos_correntes["SOL"] == Decimal("1.0")
        # A leitura nova virou o suspeito vigente: a seguinte confirma.
        fonte.precos["SOL/USDT"] = Decimal("0.086")
        await dados.refresh()
        assert dados.precos_correntes["SOL"] == Decimal("0.086")


class TestFrescorPeloCarimboDoTicker:
    """O frescor era medido pela hora de RECEBIMENTO: ticker velho servido por
    cache/proxy, ou de par com negociacao suspensa, contava como fresco."""

    async def test_ticker_com_carimbo_velho_nao_vale_para_stop(self, settings):
        from datetime import UTC, datetime

        dados, _, fonte = await montar(settings, {"BTC/USDT": "100"}, {"BTC/USDT": "101"})
        velho = datetime.now(UTC) - timedelta(hours=1)

        async def ticker_velho(symbol):
            return Ticker(
                exchange=ExchangeName.BINANCE, symbol=symbol, price=Decimal("101"),
                timestamp=velho,
            )

        fonte.fetch_ticker = ticker_velho
        await dados.refresh()
        assert "BTC" not in dados.precos_de_mercado()
        assert dados.pares_sem_preco_corrente == ["BTC"]
        # Continua valendo para a valorizacao (foi o mercado de verdade).
        assert dados.precos_para_avaliacao()["BTC"] == Decimal("101")

    async def test_carimbo_no_futuro_vale_como_agora(self, settings):
        from datetime import UTC, datetime

        dados, _, fonte = await montar(settings, {"BTC/USDT": "100"}, {"BTC/USDT": "101"})
        adiantado = datetime.now(UTC) + timedelta(hours=1)

        async def ticker_adiantado(symbol):
            return Ticker(
                exchange=ExchangeName.BINANCE, symbol=symbol, price=Decimal("101"),
                timestamp=adiantado,
            )

        fonte.fetch_ticker = ticker_adiantado
        await dados.refresh()
        assert dados._precos_correntes_em["BTC"] <= datetime.now(UTC)


class TestListaSemPrecoNaoEMutadaPorGetter:
    async def test_pares_sem_preco_acompanha_o_envelhecimento(self, settings):
        """Era gravada por `precos_para_avaliacao` (um getter) e pelo frescor."""
        dados, _, _ = await montar(settings, {"XRP/USDT": "1.30"}, {"XRP/USDT": "1.31"})
        await dados.refresh()
        assert dados.pares_sem_preco_corrente == []
        _envelhecer(dados, "XRP")
        assert dados.pares_sem_preco_corrente == ["XRP"]
