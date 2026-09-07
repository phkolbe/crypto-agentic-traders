"""Testes da descoberta automática de pares ("mar aberto").

A seleção é a única coisa entre "negociar o que faz sentido" e "negociar
qualquer coisa listada". Os casos aqui vêm de dados reais da Binance: stablecoin
no topo do volume, 312 de 487 pares abaixo de 1M/dia, e nomes que enganam
heurística de string.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_traders.discovery import (
    DEFAULT_EXCLUDED_ASSETS,
    DiscoveryCriteria,
    select_markets,
)


def market(symbol: str, base: str, *, spot: bool = True, active: bool = True, quote: str = "USDT"):
    return {"symbol": symbol, "base": base, "quote": quote, "spot": spot, "active": active}


def build(*entries: tuple[str, str, float]) -> tuple[dict, dict]:
    """Monta (markets, tickers) a partir de (symbol, base, volume24h)."""
    markets = {symbol: market(symbol, base) for symbol, base, _ in entries}
    tickers = {symbol: {"quoteVolume": volume} for symbol, _, volume in entries}
    return markets, tickers


@pytest.fixture
def criteria() -> DiscoveryCriteria:
    return DiscoveryCriteria(
        quote_currency="USDT",
        min_quote_volume_24h=Decimal("50000000"),
        max_symbols=3,
        exclude_assets=DEFAULT_EXCLUDED_ASSETS,
    )


class TestLiquidityFloor:
    def test_rejects_pairs_below_the_volume_floor(self, criteria):
        """Em par ilíquido a própria ordem move o preço e o slippage come tudo."""
        markets, tickers = build(
            ("BTC/USDT", "BTC", 900_000_000),
            ("OBSCURO/USDT", "OBSCURO", 120_000),
        )
        result = select_markets(markets, tickers, criteria)
        assert result.symbols == ["BTC/USDT"]
        assert result.rejected_low_volume == 1

    def test_pair_without_volume_data_is_rejected(self, criteria):
        """Sem volume informado não dá para afirmar que é líquido."""
        markets = {"X/USDT": market("X/USDT", "X")}
        result = select_markets(markets, {"X/USDT": {}}, criteria)
        assert result.symbols == []
        assert result.rejected_low_volume == 1

    def test_accepts_exactly_at_the_floor(self, criteria):
        markets, tickers = build(("BTC/USDT", "BTC", 50_000_000))
        assert select_markets(markets, tickers, criteria).symbols == ["BTC/USDT"]


class TestStablecoinExclusion:
    def test_excludes_stablecoin_pairs_despite_huge_volume(self, criteria):
        """USDC/USDT é o MAIOR volume da Binance e o pior par possível aqui.

        O preço não anda, então todo sinal é ruído e toda operação é taxa pura.
        """
        markets, tickers = build(
            ("USDC/USDT", "USDC", 2_059_000_000),
            ("BTC/USDT", "BTC", 914_000_000),
        )
        result = select_markets(markets, tickers, criteria)
        assert result.symbols == ["BTC/USDT"]
        assert result.rejected_excluded == 1

    def test_excludes_tokenized_fiat(self, criteria):
        markets, tickers = build(("EUR/USDT", "EUR", 500_000_000))
        assert select_markets(markets, tickers, criteria).symbols == []

    def test_operator_can_exclude_extra_assets(self):
        custom = DiscoveryCriteria(
            min_quote_volume_24h=Decimal("1"),
            max_symbols=5,
            exclude_assets=DEFAULT_EXCLUDED_ASSETS | {"DOGE"},
        )
        markets, tickers = build(("DOGE/USDT", "DOGE", 100_000_000))
        assert select_markets(markets, tickers, custom).symbols == []


class TestNameHeuristicsAreNotUsed:
    """Filtrar "token alavancado" por sufixo marca nomes legítimos.

    JUP (Jupiter), SYRUP e SUPER terminam em UP. Excluí-los seria o mesmo erro
    de procurar "ip" dentro de "multiple" — por isso a exclusão é por lista
    explícita, não por padrão de nome.
    """

    @pytest.mark.parametrize("base", ["JUP", "SYRUP", "SUPER"])
    def test_legitimate_assets_ending_in_up_are_kept(self, criteria, base):
        markets, tickers = build((f"{base}/USDT", base, 100_000_000))
        assert select_markets(markets, tickers, criteria).symbols == [f"{base}/USDT"]


class TestMarketFiltering:
    def test_ignores_non_spot_markets(self, criteria):
        markets = {"BTC/USDT": market("BTC/USDT", "BTC", spot=False)}
        tickers = {"BTC/USDT": {"quoteVolume": 900_000_000}}
        result = select_markets(markets, tickers, criteria)
        assert result.symbols == []
        assert result.considered == 0

    def test_ignores_inactive_markets(self, criteria):
        markets = {"OLD/USDT": market("OLD/USDT", "OLD", active=False)}
        tickers = {"OLD/USDT": {"quoteVolume": 900_000_000}}
        assert select_markets(markets, tickers, criteria).symbols == []

    def test_ignores_other_quote_currencies(self, criteria):
        """Par cotado em outra moeda quebraria todo o cálculo de portfólio."""
        markets = {"BTC/EUR": market("BTC/EUR", "BTC", quote="EUR")}
        tickers = {"BTC/EUR": {"quoteVolume": 900_000_000}}
        assert select_markets(markets, tickers, criteria).symbols == []


class TestRankingAndCap:
    def test_sorts_by_volume_descending(self, criteria):
        markets, tickers = build(
            ("SOL/USDT", "SOL", 236_000_000),
            ("BTC/USDT", "BTC", 914_000_000),
            ("ETH/USDT", "ETH", 682_000_000),
        )
        assert select_markets(markets, tickers, criteria).symbols == [
            "BTC/USDT",
            "ETH/USDT",
            "SOL/USDT",
        ]

    def test_caps_the_number_of_pairs(self, criteria):
        """Sem teto, "mar aberto" vira dezenas de posições simultâneas."""
        entries = tuple(
            (f"A{i}/USDT", f"A{i}", 100_000_000 + i) for i in range(10)
        )
        markets, tickers = build(*entries)
        result = select_markets(markets, tickers, criteria)
        assert len(result.symbols) == 3

    def test_reports_what_was_rejected_and_why(self, criteria):
        """O operador precisa poder auditar a seleção, não só receber a lista."""
        markets, tickers = build(
            ("BTC/USDT", "BTC", 914_000_000),
            ("USDC/USDT", "USDC", 2_059_000_000),
            ("PEQUENO/USDT", "PEQUENO", 1_000),
        )
        result = select_markets(markets, tickers, criteria)
        assert result.considered == 3
        assert result.rejected_excluded == 1
        assert result.rejected_low_volume == 1

    def test_assets_are_derived_from_the_selected_pairs(self, criteria):
        markets, tickers = build(("BTC/USDT", "BTC", 900_000_000))
        assert select_markets(markets, tickers, criteria).assets == ["BTC"]


class TestEmptyResult:
    def test_returns_empty_when_nothing_qualifies(self, criteria):
        """Piso alto demais deve dar lista vazia, e não um par ruim qualquer."""
        markets, tickers = build(("X/USDT", "X", 10))
        result = select_markets(markets, tickers, criteria)
        assert result.symbols == []
        assert result.summary()["symbols"] == []


class TestSettingsIntegration:
    def test_blank_symbols_enables_discovery(self):
        from crypto_traders.config import Settings

        assert Settings(symbols=[]).discovery_enabled is True

    def test_configured_symbols_disable_discovery(self):
        """Configuração explícita sempre vence descoberta automática."""
        from crypto_traders.config import Settings

        assert Settings(symbols=["BTC/USDT"]).discovery_enabled is False

    def test_unsupported_exchange_is_refused(self):
        """Nesta fase só a Binance; falhar alto evita subir apontando para o nada."""
        from pydantic import ValidationError

        from crypto_traders.config import Settings

        with pytest.raises(ValidationError, match="nao e suportada"):
            Settings(exchange="kraken")


class TestRiskManagerUniverse:
    async def test_discovered_universe_becomes_the_effective_whitelist(self, settings):
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        agent = RiskManagerAgent(InMemoryEventBus(), settings)
        await agent._load_state()
        await agent.apply_discovered_universe(["SOL/USDT", "LINK/USDT"], ["SOL", "LINK"])

        assert agent.limits.symbol_whitelist == ["SOL/USDT", "LINK/USDT"]
        assert agent.limits.asset_whitelist == ["SOL", "LINK"]

    async def test_universe_survives_a_limits_reload(self, settings):
        """`_load_state` roda a cada sinal; sem cuidado, apagaria a descoberta."""
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        agent = RiskManagerAgent(InMemoryEventBus(), settings)
        await agent.apply_discovered_universe(["SOL/USDT"], ["SOL"])
        await agent._load_state()

        assert agent.limits.symbol_whitelist == ["SOL/USDT"]

    async def test_universe_survives_a_limits_update_from_the_ui(self, settings):
        """Salvar um limite pela interface não pode reverter a whitelist."""
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus

        agent = RiskManagerAgent(InMemoryEventBus(), settings)
        await agent.apply_discovered_universe(["SOL/USDT"], ["SOL"])
        await agent.update_limits({"max_order_notional": "25"})

        assert agent.limits.symbol_whitelist == ["SOL/USDT"]
        assert agent.limits.max_order_notional == Decimal("25")

    async def test_universe_change_is_audited(self, settings):
        """No modo automático a whitelist deixa de ser digitada, não de ser rastreável."""
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus
        from crypto_traders.db.repositories import AuditLogRepository
        from crypto_traders.db.session import session_scope

        agent = RiskManagerAgent(InMemoryEventBus(), settings)
        await agent.apply_discovered_universe(["SOL/USDT"], ["SOL"])

        async with session_scope(settings) as session:
            entries = await AuditLogRepository(session).list()
        assert any(e.action == "trading_universe_discovered" for e in entries)

    async def test_no_audit_entry_when_the_universe_is_unchanged(self, settings):
        """A varredura roda periodicamente; registrar toda vez encheria o log."""
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus
        from crypto_traders.db.repositories import AuditLogRepository
        from crypto_traders.db.session import session_scope

        agent = RiskManagerAgent(InMemoryEventBus(), settings)
        await agent.apply_discovered_universe(["SOL/USDT"], ["SOL"])
        await agent.apply_discovered_universe(["SOL/USDT"], ["SOL"])

        async with session_scope(settings) as session:
            entries = await AuditLogRepository(session).list()
        assert sum(1 for e in entries if e.action == "trading_universe_discovered") == 1
