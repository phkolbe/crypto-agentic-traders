"""Risk Manager: recarga de limites contra a alteracao, e falhas de saida que sobram.

Achados low da auditoria de 09/11/2026 (risco-e-execucao) que ainda existiam na
rodada 3:

* **L1** -- `_load_state` nao usava a `_limits_lock`. Um `update_limits` que
  terminava entre a leitura do banco e a atribuicao em `_load_state` era
  sobrescrito na memoria pelo valor antigo: a API respondia o `stop_loss_pct`
  novo e `enforce_protective_exits` seguia com o antigo ate o proximo sinal (com
  1d, ate um dia).
* **L2** -- `_exit_failures` so zerava dentro de `_reconcile_exits`. Se a posicao
  sumia (FLAT da estrategia, venda manual) depois de uma recusa ja liberada, a
  contagem ficava, e o episodio seguinte comecava com as 3 tentativas ja gastas.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from decimal import Decimal

from crypto_traders.agents import risk_manager as modulo
from crypto_traders.agents.risk_manager import RiskManagerAgent
from crypto_traders.bus import InMemoryEventBus
from crypto_traders.domain.enums import ExchangeName
from crypto_traders.domain.models import PortfolioSnapshot, Position


async def _agente(settings) -> RiskManagerAgent:
    bus = InMemoryEventBus()
    await bus.start()
    return RiskManagerAgent(bus, settings)


def _retrato(**posicoes: str) -> PortfolioSnapshot:
    lista = [
        Position(
            exchange=ExchangeName.BINANCE, asset=asset, quantity=Decimal(qtd),
            average_price=Decimal(100), current_price=Decimal(100),
        )
        for asset, qtd in posicoes.items()
    ]
    lista.append(
        Position(
            exchange=ExchangeName.BINANCE, asset="USDT", quantity=Decimal(1000),
            current_price=Decimal(1),
        )
    )
    aplicado = sum((p.quantity * Decimal(100) for p in lista[:-1]), start=Decimal(0))
    return PortfolioSnapshot(
        total_value=aplicado + 1000,
        cash_value=Decimal(1000),
        positions_value=aplicado,
        positions=lista,
        timestamp=datetime.now(UTC),
    )


class TestRecargaNaoApagaAlteracao:
    async def test_update_no_meio_da_recarga_vale_no_fim(self, settings, monkeypatch):
        rm = await _agente(settings)
        await rm._load_state()
        antigo = rm.limits.stop_loss_pct

        leu = asyncio.Event()
        solta = asyncio.Event()
        pausar = [True]
        original = modulo.session_scope

        @contextlib.asynccontextmanager
        async def escopo(cfg):
            # Pausa a PRIMEIRA sessao (a da recarga) depois de ela ler e fechar:
            # e a janela entre a leitura do banco e a atribuicao na memoria.
            primeira = bool(pausar) and pausar.pop()
            async with original(cfg) as session:
                yield session
            if primeira:
                leu.set()
                await solta.wait()

        monkeypatch.setattr(modulo, "session_scope", escopo)
        recarga = asyncio.create_task(rm._load_state())
        await leu.wait()
        alteracao = asyncio.create_task(rm.update_limits({"stop_loss_pct": antigo + 0.02}))
        await asyncio.sleep(0.2)
        solta.set()
        await asyncio.gather(recarga, alteracao)

        # Antes: a recarga atribuia o valor que tinha lido (o antigo) por cima.
        assert rm.limits.stop_loss_pct == antigo + 0.02


class TestFalhasDeSaidaNaoSobram:
    async def test_posicao_que_sumiu_zera_a_contagem(self, settings):
        """A recusa ja liberada (trava solta) ficava contada para sempre."""
        rm = await _agente(settings)
        # Estado deixado por `_handle_failed_exit` com 2 recusas: trava solta,
        # contagem guardada para a proxima tentativa.
        rm._exit_failures["BTC"] = 2
        rm.observe_snapshot(_retrato(ETH="1"))

        await rm.enforce_protective_exits(_retrato(ETH="1"))

        assert "BTC" not in rm._exit_failures

    async def test_posicao_que_continua_guarda_a_contagem(self, settings):
        """E a contagem que separa recusa transitoria de permanente: nao zera a toa."""
        rm = await _agente(settings)
        rm._exit_failures["BTC"] = 2

        await rm.enforce_protective_exits(_retrato(BTC="0.5"))

        assert rm._exit_failures.get("BTC") == 2
