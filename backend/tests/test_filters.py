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
    MarketFilter,
    check_all,
    check_order_viability,
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
        assert "RISK_MIN_ORDER_NOTIONAL" in output

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
