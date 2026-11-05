"""Cenarios de ponta a ponta: provocar cada protecao e ver que ela AGIU.

Sobe o `Orchestrator` real (cinco agentes, bus, `PaperBroker`, banco descartavel)
contra o `MercadoSimulado` e conduz o mercado passo a passo com o
`AgenteDeCenario`. Nenhum teste aqui confere valor configurado: cada um faz o
preco andar, espera o sistema reagir e le o desfecho onde ele fica gravado --
trades, ordens, `risk_events`, saldo do broker.

Onde da, o cenario traz a contraprova: o mesmo roteiro com a fiacao de antes da
correcao (`reproduzir_fiacao_antiga`) mostra o sistema antigo errando, o que
prova que o teste distingue um do outro.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from crypto_traders.config import RiskSettings, Settings, TradingSettings
from crypto_traders.db.repositories import TradeRepository
from crypto_traders.db.session import dispose_engine, session_scope
from crypto_traders.domain.enums import (
    RiskEventType,
    Side,
    SignalDirection,
    TradeOrigin,
)
from crypto_traders.simulacao import AgenteDeCenario, MercadoSimulado

pytestmark = pytest.mark.cenario

SIMBOLO = "BTC/USDT"

#: Queda lenta e regular: a media rapida fica abaixo da lenta a serie inteira.
FECHAMENTOS = [Decimal(56000 - 100 * i) for i in range(61)]  # 56000 -> 50000
FECHAMENTO_ANTERIOR = FECHAMENTOS[-1]

#: Fechamento que faz a MA9 cruzar a MA21 para cima NESTE candle (medido com a
#: propria `ma_crossover`: 56000 tambem cruza, 55000 nao).
FECHAMENTO_DO_CRUZAMENTO = Decimal("57000")

#: Ticker logo depois do fechamento: o preco a que uma ordem a mercado executa.
PRECO_CORRENTE = Decimal("57100")

SLIPPAGE = Decimal("0.0005")
TAXA = Decimal("0.001")


def _limites(**mudancas: object) -> RiskSettings:
    base = {
        "max_order_notional": Decimal("100"),
        "max_order_pct_portfolio": 0.10,
        "max_asset_exposure_pct": 0.50,
        "max_open_positions": 3,
        "min_order_notional": Decimal("10"),
        "stop_loss_pct": 0.03,
        "take_profit_pct": 0.06,
        "daily_loss_limit_pct": 0.05,
        "weekly_loss_limit_pct": 0.12,
        "min_signal_confidence": 0.55,
        "asset_whitelist": ["BTC", "USDT"],
        "symbol_whitelist": [SIMBOLO],
        # Sem cooldown: o cenario do circuit breaker reabre depois do rearme, e o
        # cooldown recusaria por outro motivo, escondendo o que se mede.
        "cooldown_seconds": 0,
    }
    return RiskSettings(**{**base, **mudancas})


def _settings(tmp_path, risco: RiskSettings) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'cenario.db').as_posix()}",
        trading=TradingSettings(
            symbols=[SIMBOLO],
            timeframe="1d",
            strategies=["ma_crossover"],
            quote_currency="USDT",
            candle_history_limit=100,
            # Intervalos longos: os lacos de fundo rodam uma vez na subida e
            # dormem; cada passo do cenario e explicito.
            market_data_interval_seconds=3600,
            portfolio_interval_seconds=3600,
            signal_batch_window_seconds=0,
            paper_initial_balance=Decimal("1000"),
            paper_fee_pct=TAXA,
            paper_slippage_pct=SLIPPAGE,
        ),
        risk=risco,
    )


@asynccontextmanager
async def _cenario(
    tmp_path, *, risco: RiskSettings | None = None, tickers_fora_desde_o_inicio: bool = False
) -> AsyncIterator[AgenteDeCenario]:
    await dispose_engine()
    mercado = MercadoSimulado(timeframe="1d")
    mercado.adicionar_par(SIMBOLO, FECHAMENTOS)
    if tickers_fora_desde_o_inicio:
        mercado.falhar_tickers()
    async with AgenteDeCenario(_settings(tmp_path, risco or _limites()), mercado) as cenario:
        yield cenario


async def _abrir_posicao(cenario: AgenteDeCenario, preco: Decimal = PRECO_CORRENTE):
    """Fecha o candle do cruzamento com o mercado em `preco` e roda um ciclo."""
    cenario.definir_preco(SIMBOLO, preco)
    cenario.fechar_candle(SIMBOLO, FECHAMENTO_DO_CRUZAMENTO)
    await cenario.ciclo()
    compras = [t for t in await cenario.trades() if t.side == str(Side.BUY)]
    assert len(compras) == 1, f"o cruzamento nao virou compra: {await cenario.eventos_de_risco()}"
    return compras[0]


def _medio(compra) -> Decimal:
    """Preco medio como o Portfolio o reconstroi: preco + taxa sobre a quantidade."""
    custo = Decimal(str(compra.quantity)) * Decimal(str(compra.price)) + Decimal(str(compra.fee))
    return custo / Decimal(str(compra.quantity))


def _vendas(trades) -> list:
    return [t for t in trades if t.side == str(Side.SELL)]


# ======================================================================
# 1. A compra executa no preco CORRENTE
# ======================================================================
async def test_compra_executa_no_preco_corrente_e_nao_no_fechamento_da_vespera(tmp_path):
    """Defeito 2 (broker de papel com preco velho), cenario medido no XRP.

    O sinal sai no fechamento do candle (57000) com o mercado a 57100; o
    fechamento ANTERIOR e 50000. A compra tem de executar no ticker (mais
    slippage), nunca no fechamento anterior -- e o ciclo seguinte nao pode
    produzir o "take profit" instantaneo que o preco errado fabricava.
    """
    async with _cenario(tmp_path) as cenario:
        compra = await _abrir_posicao(cenario)

        assert Decimal(str(compra.price)) == PRECO_CORRENTE * (1 + SLIPPAGE)
        assert Decimal(str(compra.price)) > FECHAMENTO_ANTERIOR * Decimal("1.1")
        assert compra.strategy == "ma_crossover"

        await cenario.ciclo()
        await cenario.ciclo()
        assert _vendas(await cenario.trades()) == []
        assert await cenario.saidas_de_protecao() == []
        assert cenario.alertas_do_tipo("protective_exit") == []
        assert await cenario.saldo("BTC") == Decimal(str(compra.quantity))


async def test_contraprova_fiacao_antiga_compra_na_vespera_e_fabrica_take_profit(tmp_path):
    """Contraprova do cenario 1: o roteiro reproduz o defeito medido.

    Com a fiacao de antes (broker no preco da ultima sincronizacao, avaliacao no
    fechamento do candle), a MESMA compra sai a 50000 -- fechamento da vespera
    -- e o snapshot seguinte "realiza" um take profit que nao existiu. Se este
    teste deixar de falhar o sistema antigo, o de cima nao prova nada.
    """
    async with _cenario(tmp_path) as cenario:
        cenario.reproduzir_fiacao_antiga()
        cenario.definir_preco(SIMBOLO, PRECO_CORRENTE)
        cenario.fechar_candle(SIMBOLO, FECHAMENTO_DO_CRUZAMENTO)
        await cenario.ciclo()

        trades = await cenario.trades()
        compras = [t for t in trades if t.side == str(Side.BUY)]
        assert len(compras) == 1
        assert Decimal(str(compras[0].price)) == FECHAMENTO_ANTERIOR * (1 + SLIPPAGE)
        vendas = _vendas(trades)
        assert [v.strategy for v in vendas] == ["protecao"]
        motivos = [e.snapshot["motivo"] for e in await cenario.saidas_de_protecao()]
        assert motivos == ["take_profit"], "o falso take profit do XRP nao se reproduziu"


# ======================================================================
# 2. Stop intradiario: o ticker rompe o stop SEM candle novo
# ======================================================================
async def test_stop_intradiario_dispara_no_ticker_sem_candle_novo(tmp_path):
    """Defeito 1 (stop avaliado no fechamento diario).

    Posicao aberta; o ticker cai abaixo do stop e nenhum candle fecha. Num unico
    ciclo a venda de protecao tem de sair, no preco caido. Contraprova no mesmo
    teste: `latest_prices` (o que a avaliacao antiga lia) continua no
    fechamento, ACIMA do stop.
    """
    async with _cenario(tmp_path) as cenario:
        compra = await _abrir_posicao(cenario)
        stop = _medio(compra) * (1 - Decimal("0.03"))
        caido = Decimal("55000")
        assert caido < stop

        cenario.definir_preco(SIMBOLO, caido)
        await cenario.ciclo()

        vendas = _vendas(await cenario.trades())
        assert len(vendas) == 1, "o stop nao agiu dentro do ciclo"
        assert vendas[0].strategy == "protecao"
        assert Decimal(str(vendas[0].price)) == caido * (1 - SLIPPAGE)
        assert Decimal(str(vendas[0].quantity)) == Decimal(str(compra.quantity))
        assert [e.snapshot["motivo"] for e in await cenario.saidas_de_protecao()] == [
            "stop_loss"
        ]
        assert await cenario.saldo("BTC") == 0

        # Nenhum candle fechou: a avaliacao antiga via o fechamento, acima do stop.
        assert cenario.mercado.ultimo_fechamento(SIMBOLO) == FECHAMENTO_DO_CRUZAMENTO
        assert cenario.orquestrador.market_data.latest_prices["BTC"] == FECHAMENTO_DO_CRUZAMENTO
        assert cenario.orquestrador.market_data.latest_prices["BTC"] > stop


async def test_contraprova_fiacao_antiga_nao_ve_o_stop_intradiario(tmp_path):
    """Contraprova do cenario 2: com a avaliacao no fechamento, a mesma queda passa batida."""
    async with _cenario(tmp_path) as cenario:
        compra = await _abrir_posicao(cenario)
        cenario.reproduzir_fiacao_antiga()

        cenario.definir_preco(SIMBOLO, Decimal("55000"))
        await cenario.ciclo()
        await cenario.ciclo()

        assert _vendas(await cenario.trades()) == []
        assert await cenario.saidas_de_protecao() == []
        assert await cenario.saldo("BTC") == Decimal(str(compra.quantity))


# ======================================================================
# 3. Take profit intradiario
# ======================================================================
async def test_take_profit_intradiario_dispara_no_ticker_sem_candle_novo(tmp_path):
    """Defeito 1, lado do alvo: o ticker passa do take profit sem candle novo."""
    async with _cenario(tmp_path) as cenario:
        compra = await _abrir_posicao(cenario)
        alvo = _medio(compra) * (1 + Decimal("0.06"))
        subido = Decimal("61000")
        assert subido > alvo

        cenario.definir_preco(SIMBOLO, subido)
        await cenario.ciclo()

        vendas = _vendas(await cenario.trades())
        assert len(vendas) == 1, "o take profit nao agiu dentro do ciclo"
        assert vendas[0].strategy == "protecao"
        assert Decimal(str(vendas[0].price)) == subido * (1 - SLIPPAGE)
        assert [e.snapshot["motivo"] for e in await cenario.saidas_de_protecao()] == [
            "take_profit"
        ]
        # Contraprova: o fechamento, que a avaliacao antiga lia, esta abaixo do alvo.
        assert cenario.orquestrador.market_data.latest_prices["BTC"] < alvo


# ======================================================================
# 4 e 5. Circuit breaker
# ======================================================================
#: Posicao grande e stop largo: a perda do dia passa do limite de 5% ANTES de o
#: stop de 10% romper, entao a trava arma com a posicao ainda aberta.
LIMITES_DA_TRAVA = {
    "max_order_notional": Decimal("800"),
    "max_order_pct_portfolio": 0.80,
    "max_asset_exposure_pct": 0.90,
    "stop_loss_pct": 0.10,
    "take_profit_pct": 0.50,
}


async def _armar_a_trava(cenario: AgenteDeCenario):
    compra = await _abrir_posicao(cenario)
    stop = _medio(compra) * (1 - Decimal("0.10"))
    queda = Decimal("53000")
    assert queda > stop
    cenario.definir_preco(SIMBOLO, queda)
    await cenario.ciclo()
    return compra, stop


async def test_circuit_breaker_arma_barra_abertura_e_so_solta_no_rearme_manual(tmp_path):
    """Circuit breaker de ponta a ponta, pelo caminho de producao (`_on_snapshot`).

    A perda do dia passa do limite -> a trava arma, decisao pausa, abertura
    barrada na execucao. Sinal LONG novo nao vira ordem; com o Risk Manager
    retomado individualmente (rota por agente, que a trava nao guarda) o sinal e
    REJEITADO com o motivo. A trava nao se solta sozinha com o preco voltando; o
    rearme manual devolve a abertura -- e a mesma compra passa (contraprova de
    que a recusa era a trava, e nao o caminho entupido).
    """
    async with _cenario(tmp_path, risco=_limites(**LIMITES_DA_TRAVA)) as cenario:
        orq = cenario.orquestrador
        compra, _ = await _armar_a_trava(cenario)

        assert orq.risk_manager.circuit_breaker_active
        disparos = [
            e
            for e in await cenario.eventos_de_risco()
            if e.event_type == str(RiskEventType.CIRCUIT_BREAKER_TRIPPED)
        ]
        assert len(disparos) == 1 and "perda diaria" in disparos[0].reasons[0]
        assert len(cenario.alertas_do_tipo("circuit_breaker")) == 1
        assert orq.strategy.is_paused and orq.risk_manager.is_paused
        assert not orq.execution.is_paused, "D4: a execucao nunca pausa"
        assert "perda diaria" in (orq.execution.openings_blocked or "")
        assert (await cenario.saude())["circuit_breaker_active"] is True
        # A trava armou com a posicao aberta: o stop (10%) nao rompeu.
        assert _vendas(await cenario.trades()) == []

        # Sinal novo com a decisao pausada: nada chega ao broker.
        ordens_antes = len(await cenario.ordens())
        sinal = await cenario.publicar_sinal(SIMBOLO, SignalDirection.LONG)
        assert len(await cenario.ordens()) == ordens_antes
        assert await cenario.saldo("BTC") == Decimal(str(compra.quantity))

        # Operador retoma o Risk Manager sozinho: o motor rejeita, com motivo.
        await orq.resume_agent("risk_manager", actor="operador")
        await cenario.aquietar()
        avaliacoes = [e for e in await cenario.eventos_de_risco() if e.signal_id == sinal.id]
        assert len(avaliacoes) == 1
        assert avaliacoes[0].decision == "rejected"
        assert any("circuit breaker ativo" in r for r in avaliacoes[0].reasons)
        assert len(await cenario.ordens()) == ordens_antes
        assert orq.execution.openings_blocked is not None

        # O preco volta acima da referencia do dia: a trava NAO se solta sozinha.
        cenario.definir_preco(SIMBOLO, Decimal("58000"))
        await cenario.ciclo()
        await cenario.ciclo()
        assert orq.risk_manager.circuit_breaker_active
        assert orq.execution.openings_blocked is not None

        # Rearme manual, como a rota /risk/circuit-breaker/reset faz.
        await orq.risk_manager.reset_circuit_breaker(actor="operador")
        await orq.resume_all(actor="operador")
        assert orq.execution.openings_blocked is None

        sinal = await cenario.publicar_sinal(SIMBOLO, SignalDirection.LONG)
        avaliacoes = [e for e in await cenario.eventos_de_risco() if e.signal_id == sinal.id]
        assert [a.decision for a in avaliacoes] == ["approved"]
        compras = [t for t in await cenario.trades() if t.side == str(Side.BUY)]
        assert len(compras) == 2, "a abertura nao voltou depois do rearme manual"
        acoes = [r.action for r in await cenario.auditoria()]
        assert "circuit_breaker_tripped" in acoes and "circuit_breaker_reset" in acoes


async def test_stop_dispara_com_o_circuit_breaker_armado(tmp_path):
    """Passo 6 da secao 15: o stop DURANTE a trava.

    Trava armada, decisao pausada, posicao aberta. O preco rompe o stop: a venda
    de protecao tem de sair mesmo assim -- bloquear a abertura nunca pode
    bloquear o fechamento (D4).
    """
    async with _cenario(tmp_path, risco=_limites(**LIMITES_DA_TRAVA)) as cenario:
        orq = cenario.orquestrador
        compra, stop = await _armar_a_trava(cenario)
        assert orq.risk_manager.circuit_breaker_active
        assert orq.risk_manager.is_paused and orq.strategy.is_paused

        rompido = Decimal("50000")
        assert rompido < stop
        cenario.definir_preco(SIMBOLO, rompido)
        await cenario.ciclo()

        vendas = _vendas(await cenario.trades())
        assert len(vendas) == 1, "a venda de protecao NAO saiu com a trava armada"
        assert vendas[0].strategy == "protecao"
        assert Decimal(str(vendas[0].price)) == rompido * (1 - SLIPPAGE)
        assert Decimal(str(vendas[0].quantity)) == Decimal(str(compra.quantity))
        assert await cenario.saldo("BTC") == 0
        # A trava continua armada e a abertura continua barrada.
        assert orq.risk_manager.circuit_breaker_active
        assert orq.execution.openings_blocked is not None


# ======================================================================
# 6. Ticker fora do ar
# ======================================================================
async def test_ticker_fora_do_ar_avalia_no_ultimo_ticker_bom_e_avisa_uma_vez(tmp_path):
    """Queda do ticker com posicao aberta.

    Durante a falha a avaliacao usa o ULTIMO TICKER BOM (57100) e nunca o
    fechamento do candle (57000); o par aparece em `pares_sem_preco_corrente`; o
    alerta `preco_corrente_ausente` sai UMA vez; nenhuma saida de protecao sai do
    preco ausente -- nem com o mercado de verdade ja abaixo do stop, que o
    sistema nao tem como ver. Quando o ticker volta, a avaliacao volta ao preco
    corrente e o stop age no primeiro ciclo.
    """
    async with _cenario(tmp_path) as cenario:
        dados = cenario.orquestrador.market_data
        compra = await _abrir_posicao(cenario)
        stop = _medio(compra) * (1 - Decimal("0.03"))

        cenario.mercado.falhar_tickers()
        cenario.definir_preco(SIMBOLO, Decimal("54000"))  # invisivel durante a falha
        for _ in range(4):  # 4 intervalos de coleta: passa da idade maxima (3)
            snapshot = await cenario.ciclo()
            posicao = next(p for p in snapshot.positions if p.asset == "BTC")
            assert posicao.current_price == PRECO_CORRENTE, (
                "durante a falha a avaliacao deixou o ultimo ticker bom: "
                f"{posicao.current_price}"
            )
            assert dados.precos_para_avaliacao()["BTC"] == PRECO_CORRENTE
        assert cenario.mercado.falhas_de_ticker >= 4

        assert dados.pares_sem_preco_corrente == ["BTC"]
        assert len(cenario.alertas_do_tipo("preco_corrente_ausente")) == 1
        assert _vendas(await cenario.trades()) == []
        assert await cenario.saidas_de_protecao() == []

        cenario.mercado.restaurar_tickers()
        await cenario.ciclo()
        assert dados.pares_sem_preco_corrente == []
        vendas = _vendas(await cenario.trades())
        assert len(vendas) == 1 and vendas[0].strategy == "protecao"
        assert Decimal(str(vendas[0].price)) == Decimal("54000") * (1 - SLIPPAGE)
        assert Decimal("54000") < stop


async def test_sem_ticker_nenhum_o_fechamento_do_candle_nao_dispara_stop(tmp_path):
    """Posicao cujo unico preco e o fechamento do candle nao recebe saida de protecao.

    Ticker fora do ar desde a subida; posicao lancada a mao a 60000 e candle a
    50000 (16% abaixo, bem alem do stop de 3%). Sem preco corrente nenhum, o
    fechamento nao basta para vender: nenhuma saida sai, o par fica listado e o
    operador e avisado. Quando o ticker aparece -- e confirma a queda --, o stop
    age.
    """
    async with _cenario(tmp_path, tickers_fora_desde_o_inicio=True) as cenario:
        dados = cenario.orquestrador.market_data
        quantidade = Decimal("0.002")
        cenario.broker.credit("BTC", quantidade)
        async with session_scope(cenario.settings) as sessao:
            await TradeRepository(sessao).record(
                executed_at=datetime.now(UTC),
                exchange="binance",
                symbol=SIMBOLO,
                side=str(Side.BUY),
                quantity=quantidade,
                price=Decimal("60000"),
                origin=TradeOrigin.MANUAL,
                mode="manual",
            )

        await cenario.ciclo()
        await cenario.ciclo()

        assert dados.pares_sem_preco_corrente == ["BTC"]
        assert await cenario.saidas_de_protecao() == [], (
            "saida de protecao emitida com o fechamento do candle como unico preco"
        )
        assert await cenario.saldo("BTC") == quantidade
        assert cenario.alertas_do_tipo("preco_corrente_ausente")

        cenario.mercado.restaurar_tickers()
        cenario.definir_preco(SIMBOLO, Decimal("50000"))
        await cenario.ciclo()
        vendas = _vendas(await cenario.trades())
        assert len(vendas) == 1 and vendas[0].strategy == "protecao"
        assert await cenario.saldo("BTC") == 0


# ======================================================================
# 7. Dimensionamento no preco corrente
# ======================================================================
async def test_compra_dimensionada_no_preco_corrente(tmp_path):
    """A quantidade aprovada sai do TICKER, nao do fechamento do sinal.

    O mercado subiu depois do fechamento (candle a 57000, ticker a 60000).
    Dimensionar pelo fechamento compra 100/57000 BTC e paga 105 -- 5% acima do
    teto de 100 por ordem. A quantidade tem de caber no notional aprovado ao
    preco corrente: o executado nao passa do aprovado mais o slippage.
    """
    async with _cenario(tmp_path) as cenario:
        corrente = Decimal("60000")
        compra = await _abrir_posicao(cenario, preco=corrente)

        aprovadas = [
            e
            for e in await cenario.eventos_de_risco()
            if e.decision == "approved" and e.signal_id is not None
        ]
        assert len(aprovadas) == 1
        aprovado = aprovadas[0]
        notional_aprovado = Decimal(str(aprovado.approved_notional))
        quantidade_aprovada = Decimal(str(aprovado.approved_quantity))
        assert notional_aprovado <= Decimal("100")
        # A quantidade aprovada vale o notional aprovado AO PRECO CORRENTE.
        assert abs(quantidade_aprovada * corrente - notional_aprovado) <= notional_aprovado * (
            Decimal("0.001")
        ), f"dimensionado em outro preco: {quantidade_aprovada} x {corrente}"

        executado = Decimal(str(compra.quantity)) * Decimal(str(compra.price))
        teto = notional_aprovado * (1 + SLIPPAGE) * Decimal("1.000001")
        assert executado <= teto, (
            f"executado {executado} passou do aprovado {notional_aprovado} + slippage"
        )
