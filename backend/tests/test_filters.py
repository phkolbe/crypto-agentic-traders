"""Testes dos filtros da exchange (LOT_SIZE + MIN_NOTIONAL).

A armadilha que motivou isto só aparece em ordem pequena: o ccxt trunca a
quantidade para o passo do lote e **só então** a Binance aplica o valor mínimo.
Em BTC, com passo de 0,00001 a 79 mil, cada passo vale ~0,79 USDT — uma ordem
mirando 5,50 vira 4,74 e é recusada, enquanto o mesmo valor passa em ETH ou SOL.

Os números aqui são os medidos na Binance real.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_traders.exchanges.filters import (
    FONTE_AO_VIVO,
    FONTE_BASELINE,
    MarketFilter,
    baseline_filters,
    baseline_market_filter,
    check_all,
    check_order_viability,
    check_quantity_viability,
)

#: Preços e passos reais da Binance no momento da medição.
BTC = MarketFilter("BTC/USDT", Decimal("0.00001"), Decimal("5"), Decimal("0.00001"))
ETH = MarketFilter("ETH/USDT", Decimal("0.0001"), Decimal("5"), Decimal("0.0001"))
DOGE = MarketFilter("DOGE/USDT", Decimal("1"), Decimal("1"), Decimal("1"))

BTC_PRICE = Decimal("79045.71")
ETH_PRICE = Decimal("2485.60")
DOGE_PRICE = Decimal("0.0909")


class TestTruncationTrap:
    def test_btc_order_of_5_50_is_rejected_after_rounding(self):
        """O caso real: 5,50 USDT em BTC vira 4,74 e a Binance recusa."""
        result = check_order_viability(BTC, Decimal("5.50"), BTC_PRICE)
        assert not result.viable
        assert result.effective_notional < Decimal("5")
        assert "minimo" in result.reason

    def test_the_same_amount_passes_on_eth(self):
        """O passo fino do ETH absorve o truncamento sem derrubar o valor."""
        result = check_order_viability(ETH, Decimal("5.50"), ETH_PRICE)
        assert result.viable
        assert result.effective_notional >= Decimal("5")

    def test_btc_passes_with_a_bigger_order(self):
        result = check_order_viability(BTC, Decimal("6.75"), BTC_PRICE)
        assert result.viable
        assert result.effective_notional == pytest.approx(Decimal("6.32"), abs=Decimal("0.02"))

    def test_suggestion_actually_works(self):
        """Sugerir um valor que também falha seria pior que não sugerir nada."""
        rejected = check_order_viability(BTC, Decimal("5.50"), BTC_PRICE)
        retry = check_order_viability(BTC, rejected.suggested_notional, BTC_PRICE)
        assert retry.viable

    def test_suggestion_includes_headroom_for_price_movement(self):
        """Uma ordem exatamente na fronteira é recusada por qualquer variação."""
        rejected = check_order_viability(BTC, Decimal("5.50"), BTC_PRICE)
        step_value = BTC.amount_step * BTC_PRICE
        assert rejected.suggested_notional >= BTC.min_cost + step_value


class TestCoarseSteps:
    def test_doge_whole_unit_step_is_handled(self):
        """DOGE só aceita quantidade inteira; o truncamento é grosseiro em valor."""
        result = check_order_viability(DOGE, Decimal("5.50"), DOGE_PRICE)
        assert result.viable
        assert result.effective_notional <= Decimal("5.50")

    def test_amount_rounding_to_zero_is_caught(self):
        """Ordem menor que um passo inteiro não existe."""
        result = check_order_viability(DOGE, Decimal("0.05"), DOGE_PRICE)
        assert not result.viable

    def test_below_minimum_amount_is_caught(self):
        market = MarketFilter("X/USDT", Decimal("1"), Decimal("0"), Decimal("10"))
        result = check_order_viability(market, Decimal("5"), Decimal("1"))
        assert not result.viable
        assert "lote minimo" in result.reason


class TestDegenerateInputs:
    def test_invalid_price_is_refused(self):
        result = check_order_viability(BTC, Decimal("100"), Decimal("0"))
        assert not result.viable
        assert "preco" in result.reason


class TestMarketFilterParsing:
    def test_reads_decimal_step_from_ccxt(self):
        market = {
            "symbol": "BTC/USDT",
            "precision": {"amount": 0.00001},
            "limits": {"cost": {"min": 5}, "amount": {"min": 0.00001}},
        }
        assert MarketFilter.from_ccxt(market).amount_step == Decimal("0.00001")

    def test_reads_decimal_places_notation(self):
        """Alguns modos do ccxt devolvem casas decimais em vez do passo."""
        market = {"symbol": "BTC/USDT", "precision": {"amount": 5}, "limits": {}}
        assert MarketFilter.from_ccxt(market).amount_step == Decimal("0.00001")

    def test_missing_precision_falls_back_to_fine_step(self):
        """Sem informação, assumir passo fino evita reprovar par válido à toa."""
        assert MarketFilter.from_ccxt({"symbol": "X/Y"}).amount_step == Decimal("0.00000001")

    def test_missing_limits_do_not_crash(self):
        parsed = MarketFilter.from_ccxt({"symbol": "X/Y", "limits": {}})
        assert parsed.min_cost == Decimal(0)


class TestCheckAll:
    def _markets(self):
        return {
            "BTC/USDT": {
                "symbol": "BTC/USDT",
                "precision": {"amount": 0.00001},
                "limits": {"cost": {"min": 5}, "amount": {"min": 0.00001}},
            },
            "ETH/USDT": {
                "symbol": "ETH/USDT",
                "precision": {"amount": 0.0001},
                "limits": {"cost": {"min": 5}, "amount": {"min": 0.0001}},
            },
        }

    def _tickers(self):
        return {
            "BTC/USDT": {"last": float(BTC_PRICE)},
            "ETH/USDT": {"last": float(ETH_PRICE)},
        }

    def test_separates_viable_from_blocked(self):
        results = check_all(
            self._markets(), self._tickers(), ["BTC/USDT", "ETH/USDT"], Decimal("5.50")
        )
        by_symbol = {r.symbol: r.viable for r in results}
        assert by_symbol == {"BTC/USDT": False, "ETH/USDT": True}

    def test_all_viable_with_a_bigger_order(self):
        results = check_all(
            self._markets(), self._tickers(), ["BTC/USDT", "ETH/USDT"], Decimal("6.75")
        )
        assert all(r.viable for r in results)

    def test_unknown_symbol_is_skipped_not_failed(self):
        """Símbolo inexistente é problema do Market Data Agent, não daqui."""
        results = check_all(self._markets(), self._tickers(), ["NAOEXISTE/USDT"], Decimal("10"))
        assert results == []

    def test_symbol_without_price_is_skipped(self):
        results = check_all(self._markets(), {}, ["BTC/USDT"], Decimal("10"))
        assert results == []


class TestCheckCommand:
    def _markets_and_tickers(self):
        return (
            {
                "BTC/USDT": {
                    "symbol": "BTC/USDT",
                    "precision": {"amount": 0.00001},
                    "limits": {"cost": {"min": 5}, "amount": {"min": 0.00001}},
                }
            },
            {"BTC/USDT": {"last": float(BTC_PRICE)}},
        )

    def _sizing(self, notional: str):
        from crypto_traders.risk.rules import SizingFeasibility

        return SizingFeasibility(
            feasible=True,
            portfolio_value=Decimal("19.29"),
            max_possible_order=Decimal(notional),
            min_order_notional=Decimal("5"),
            binding_limit="teste",
            minimum_portfolio=Decimal("0"),
        )

    def test_fails_when_an_order_would_not_survive_rounding(self, settings, capsys):
        """Aprovar aqui deixaria toda ordem ser recusada pela exchange depois."""
        from crypto_traders.cli import _check_market_filters

        markets, tickers = self._markets_and_tickers()
        ok = _check_market_filters(
            settings, markets, tickers, ["BTC/USDT"], self._sizing("5.50")
        )
        assert ok is False
        output = capsys.readouterr().out
        assert "arredondamento" in output
        assert "subir a ordem minima" in output

    def test_passes_with_a_viable_order(self, settings, capsys):
        from crypto_traders.cli import _check_market_filters

        markets, tickers = self._markets_and_tickers()
        assert _check_market_filters(
            settings, markets, tickers, ["BTC/USDT"], self._sizing("6.75")
        )
        assert "OK " in capsys.readouterr().out

    def test_no_symbols_is_not_a_failure(self, settings):
        """Sem par configurado, quem reclama é a checagem de dimensionamento."""
        from crypto_traders.cli import _check_market_filters

        assert _check_market_filters(settings, {}, {}, [], self._sizing("10"))


#: BNB/USDC do ensaio em curso (D24): passo de 0,001 BNB, minimo de 5 USDC.
BNB = MarketFilter("BNB/USDC", Decimal("0.001"), Decimal("5"), Decimal("0"))

#: Valor por ordem medido no ensaio de hoje: 20% de 29,29 USDC autorizados.
ORDEM_DO_ENSAIO = Decimal("5.86")


class TestOEnsaioDeHoje:
    """Os numeros exatos que estao rodando agora, em USDC.

    Existe para que uma mudanca de sizing ou de par nao passe sem que alguem
    veja o que ela faz com a fronteira do MIN_NOTIONAL.
    """

    def test_bnb_a_862_passa_de_raspao(self):
        """5,86 vira 0,006 BNB = 5,175 USDC. Sobra 0,175 acima do minimo."""
        r = check_order_viability(BNB, ORDEM_DO_ENSAIO, Decimal("862.50"))
        assert r.viable
        assert r.effective_amount == Decimal("0.006")
        assert r.effective_notional == Decimal("5.175")

    def test_um_passo_de_bnb_vale_quase_um_setimo_da_ordem(self):
        """A margem e fina porque o passo e grosso: 0,8625 USDC por passo."""
        passo = BNB.amount_step * Decimal("862.50")
        assert passo == Decimal("0.86250")
        assert ORDEM_DO_ENSAIO - Decimal("5.175") < passo

    def test_bnb_subindo_para_1200_joga_a_mesma_ordem_abaixo_do_minimo(self):
        """Nada muda na configuracao; so o preco sobe -- e a ordem morre."""
        r = check_order_viability(BNB, ORDEM_DO_ENSAIO, Decimal("1200"))
        assert not r.viable
        assert r.effective_amount == Decimal("0.004")
        assert r.effective_notional == Decimal("4.800")
        assert "abaixo do minimo de 5" in r.reason

    def test_a_sugestao_para_bnb_a_1200_realmente_passa(self):
        r = check_order_viability(BNB, ORDEM_DO_ENSAIO, Decimal("1200"))
        segunda = check_order_viability(BNB, r.suggested_notional, Decimal("1200"))
        assert segunda.viable

    def test_a_margem_de_cada_par_do_ensaio_fica_registrada(self):
        """Tres pares com passos bem diferentes, mesmo valor de ordem.

        Medido: dos 5,86 sobram 5,175 no BNB, 5,5332 no BTC e 5,7169 no ETH.
        O BNB e o par apertado da lista -- 0,175 USDC acima do minimo de 5,
        contra 0,53 do BTC e 0,72 do ETH. E por isso que o BNB e o par que
        estoura primeiro se o preco subir.
        """
        casos = [
            (BNB, Decimal("862.50"), Decimal("5.175")),
            (
                MarketFilter("BTC/USDC", Decimal("0.00001"), Decimal("5"), Decimal("0")),
                Decimal("79045.71"),
                Decimal("5.5331997"),
            ),
            (
                MarketFilter("ETH/USDC", Decimal("0.0001"), Decimal("5"), Decimal("0")),
                Decimal("2485.60"),
                Decimal("5.716880"),
            ),
        ]
        for market, preco, esperado in casos:
            r = check_order_viability(market, ORDEM_DO_ENSAIO, preco)
            assert r.viable, market.symbol
            assert r.effective_notional == esperado, market.symbol


class TestTruncate:
    """`truncate` tem que reproduzir o `amount_to_precision` do ccxt (TRUNCATE)."""

    def test_trunca_para_baixo_nunca_para_cima(self):
        assert BNB.truncate(Decimal("0.004883")) == Decimal("0.004")
        assert BNB.truncate(Decimal("0.0049999")) == Decimal("0.004")

    def test_multiplo_exato_nao_muda(self):
        assert BNB.truncate(Decimal("0.006")) == Decimal("0.006")

    def test_menos_de_um_passo_vira_zero(self):
        assert BNB.truncate(Decimal("0.0009")) == Decimal(0)

    def test_passo_invalido_nao_altera_a_quantidade(self):
        livre = MarketFilter("X/USDT", Decimal(0), Decimal(0), Decimal(0))
        assert livre.truncate(Decimal("1.23456789")) == Decimal("1.23456789")


class TestPelaQuantidade:
    """`check_quantity_viability` e a porta do caminho de ENVIO."""

    def test_concorda_com_a_checagem_por_valor(self):
        pelo_valor = check_order_viability(BNB, Decimal("5.86"), Decimal("1200"))
        pela_qtd = check_quantity_viability(
            BNB, Decimal("5.86") / Decimal("1200"), Decimal("1200")
        )
        assert pela_qtd.viable == pelo_valor.viable
        assert pela_qtd.effective_amount == pelo_valor.effective_amount
        assert pela_qtd.effective_notional == pelo_valor.effective_notional

    def test_quantidade_ja_no_passo_passa(self):
        r = check_quantity_viability(BNB, Decimal("0.006"), Decimal("862.50"))
        assert r.viable
        assert r.requested_notional == Decimal("5.17500")

    def test_preco_invalido_e_recusado(self):
        r = check_quantity_viability(BNB, Decimal("0.006"), Decimal(0))
        assert not r.viable
        assert "preco" in r.reason

    def test_quantidade_que_zera_diz_que_zerou(self):
        """Mensagem que descreve a causa, nao o sintoma."""
        r = check_quantity_viability(BNB, Decimal("0.0009"), Decimal("862.50"))
        assert not r.viable
        assert "arredonda para zero" in r.reason


class TestCatalogoVersionado:
    """O catalogo que faz a checagem AGIR sem depender de rede nem de setter.

    Motivo de existir, medido em 2026-09-09: `MarketFilterSource` estava
    implementado e testado, e `set_market_filter` nao tinha UM chamador em
    `src/`. Em dry_run o `PaperBroker` subia sem conhecer filtro de par nenhum,
    `market_filter()` devolvia `None`, o `_preflight` devolvia `None` -- e a
    ordem abaixo do MIN_NOTIONAL continuava sendo enviada e recusada pela
    exchange. Codigo que existe e nao roda nao protege ninguem.

    Os numeros abaixo sao os da Binance real, capturados por
    `load_markets()` (endpoint publico, sem credencial) em 2026-09-09.
    """

    def test_o_par_do_mandato_esta_no_catalogo_com_os_numeros_reais(self):
        bnb = baseline_market_filter("BNB/USDC")
        assert bnb is not None, "BNB/USDC fora do catalogo: a checagem nao teria dado"
        assert bnb.amount_step == Decimal("0.001")
        assert bnb.min_cost == Decimal("5")
        assert bnb.min_amount == Decimal("0.001")
        assert bnb.source == FONTE_BASELINE

    def test_o_caso_do_mandato_e_recusado_usando_so_o_catalogo(self):
        """5,86 USDC com BNB a 1.200 -> 0,004 BNB = 4,80: abaixo do minimo de 5.

        Nenhum filtro escrito a mao neste teste: o dado vem do catalogo, que e
        o que o sistema realmente usa quando ninguem carregou nada.
        """
        bnb = baseline_market_filter("BNB/USDC")
        assert bnb is not None
        r = check_quantity_viability(bnb, Decimal("5.86") / Decimal("1200"), Decimal("1200"))
        assert not r.viable
        assert r.effective_amount == Decimal("0.004")
        assert r.effective_notional == Decimal("4.800")
        assert r.source == FONTE_BASELINE
        assert "minimo de 5" in r.reason

    def test_o_mesmo_valor_passa_onde_o_passo_e_fino(self):
        """A recusa e do par, nao do valor: em ETH os mesmos 5,86 passam."""
        eth = baseline_market_filter("ETH/USDC")
        assert eth is not None and eth.amount_step == Decimal("0.0001")
        r = check_quantity_viability(eth, Decimal("5.86") / Decimal("3000"), Decimal("3000"))
        assert r.viable

    def test_par_inexistente_nao_e_inventado(self):
        assert baseline_market_filter("MOEDAINEXISTENTE/USDC") is None

    def test_exchange_fora_do_catalogo_nao_herda_os_filtros_da_binance(self):
        """Aplicar o passo de lote da Binance a outra exchange seria inventar dado."""
        assert baseline_filters("coinbase") == {}
        assert baseline_market_filter("BTC/USDC", "coinbase") is None

    def test_o_catalogo_e_grande_e_todo_passo_e_positivo(self):
        catalogo = baseline_filters("binance")
        assert len(catalogo) > 1000, f"catalogo com apenas {len(catalogo)} pares"
        assert all(f.amount_step > 0 for f in catalogo.values())
        assert all(f.source == FONTE_BASELINE for f in catalogo.values())

    def test_filtro_ao_vivo_do_ccxt_e_marcado_como_ao_vivo(self):
        """A fonte tem que viajar com o filtro: e ela que diz o quanto confiar."""
        vivo = MarketFilter.from_ccxt(
            {
                "symbol": "BNB/USDC",
                "precision": {"amount": 0.001},
                "limits": {"cost": {"min": 5}, "amount": {"min": 0.001}},
            }
        )
        assert vivo.source == FONTE_AO_VIVO

    def test_catalogo_ilegivel_volta_vazio_em_vez_de_derrubar_o_processo(
        self, monkeypatch, tmp_path
    ):
        """Arquivo corrompido nao pode impedir o sistema de subir.

        Sem catalogo o comportamento volta a ser o antigo (envia e a exchange
        decide), que e ruim mas conhecido; levantar aqui mataria a subida do
        processo por causa de um dado auxiliar.
        """
        from crypto_traders.exchanges import filters as mod

        mod.baseline_filters.cache_clear()
        monkeypatch.setitem(mod._ARQUIVOS_BASELINE, "quebrada", "nao_existe.json")
        try:
            assert mod.baseline_filters("quebrada") == {}
        finally:
            mod.baseline_filters.cache_clear()

    def test_o_catalogo_e_somente_leitura(self):
        """Cacheado por processo: quem mutasse mudaria o filtro de todo mundo."""
        with pytest.raises(TypeError):
            baseline_filters("binance")["BTC/USDC"] = None  # type: ignore[index]
