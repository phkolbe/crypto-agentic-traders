"""Agente de cenario: sobe o orquestrador REAL e o conduz passo a passo.

O que e real aqui: `Orchestrator.start()` inteiro, os cinco agentes, o bus em
memoria, o `PaperBroker` montado por `build_broker`, o banco (SQLite descartavel)
e a configuracao de negocio lida do banco. O que e substituido, e so isto: a
exchange (por `MercadoSimulado`) e a serie on-chain (por um provedor sem rede).

Os lacos de fundo continuam de pe, com intervalo longo: eles rodam uma vez na
subida, como em producao, e depois dormem. Cada passo do cenario e explicito --
`ciclo()` faz o que um ciclo de coleta seguido de um snapshot faz -- e termina
so quando o sistema aquietou, entao o desfecho de cada passo e deterministico.

O relogio dos precos correntes e simulado: cada ciclo envelhece o ultimo ticker
lido em um intervalo de coleta, como se o tempo tivesse passado entre os ciclos.
Com ticker funcionando isso nao muda nada (o ciclo le um novo); com ticker fora
do ar, e o que faz o preco corrente vencer e a avaliacao cair no fechamento do
candle -- o comportamento que se quer provar, sem esperar minutos de verdade.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

from ..bus import Topics
from ..config import Settings
from ..db.repositories import (
    AuditLogRepository,
    OrderRepository,
    RiskConfigRepository,
    RiskEventRepository,
    SignalRepository,
    TradeRepository,
    TradingConfigRepository,
)
from ..db.session import dispose_engine, init_db, session_scope
from ..domain.enums import ExchangeName, SignalDirection
from ..domain.models import PortfolioSnapshot, Signal
from ..exchanges.paper import PaperBroker
from ..onchain import OnChainProvider
from .mercado import MercadoSimulado

#: Quanto tempo esperar o sistema aquietar depois de um passo.
TEMPO_MAXIMO_PARA_AQUIETAR = 10.0

#: Quantas leituras seguidas sem atividade contam como "aquietou". Uma so nao
#: basta: entre o `publish` e o alimentador da caixa de entrada acordar existe
#: um instante em que todo mundo parece ocioso com o evento ainda no bus.
LEITURAS_QUIETAS = 3
INTERVALO_DE_LEITURA = 0.01


class ConexaoOnChainDesligada(OnChainProvider):
    """Provedor on-chain sem rede: nunca busca, serie vazia (filtro nao se aplica)."""

    async def refresh_mvrv(self, force: bool = False) -> int:
        return 0


class AgenteDeCenario:
    """Conduz o sistema real contra o `MercadoSimulado`."""

    def __init__(self, settings: Settings, mercado: MercadoSimulado) -> None:
        self.settings = settings
        self.mercado = mercado
        # Importado aqui para nao criar ciclo de importacao com o pacote de agentes.
        from ..agents.orchestrator import Orchestrator

        self.orquestrador = Orchestrator(
            settings,
            fonte_de_mercado=mercado,
            onchain=ConexaoOnChainDesligada(settings),
        )
        self.alertas: list[dict[str, Any]] = []
        """Todo alerta publicado no bus, na ordem -- o que chegaria ao operador."""
        self.orquestrador.bus.observar(self._anotar_alerta)

    def _anotar_alerta(self, topico: str, carga: Any) -> None:
        if topico == Topics.ALERTS and isinstance(carga, dict):
            self.alertas.append(carga)

    def alertas_do_tipo(self, tipo: str) -> list[dict[str, Any]]:
        return [a for a in self.alertas if a.get("type") == tipo]

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    async def iniciar(self) -> None:
        """Grava a configuracao de negocio no banco e sobe o orquestrador.

        Gravar ANTES e obrigatorio: `start()` le negocio do banco e, com o banco
        vazio, semeia os padroes do modelo -- descoberta de pares, 15m, duas
        estrategias --, e o cenario rodaria com outra configuracao que a pedida.
        """
        await init_db(self.settings)
        async with session_scope(self.settings) as sessao:
            await TradingConfigRepository(sessao).save(
                self.settings.trading.model_dump(mode="json")
            )
            await RiskConfigRepository(sessao).update_values(
                self.settings.risk.model_dump(mode="json")
            )
        await self.orquestrador.start()
        await self.aquietar()

    async def encerrar(self) -> None:
        await self.orquestrador.stop()
        await dispose_engine()

    async def __aenter__(self) -> AgenteDeCenario:
        await self.iniciar()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.encerrar()

    # ------------------------------------------------------------------
    # Passos
    # ------------------------------------------------------------------
    def definir_preco(self, symbol: str, preco: Decimal | str | float | int) -> None:
        self.mercado.definir_preco(symbol, preco)

    def fechar_candle(self, symbol: str, fechamento: Decimal | str | float | int) -> None:
        self.mercado.fechar_candle(symbol, fechamento)

    async def ciclo(self) -> PortfolioSnapshot:
        """Um ciclo de coleta, a cadeia que ele dispara, e um snapshot do portfolio.

        O snapshot passa por `Orchestrator._on_snapshot` (e o callback do
        Portfolio Agent): stop, alvo e circuit breaker sao avaliados ali, como
        em producao. O ciclo so termina com o sistema quieto -- inclusive a
        venda de protecao que o snapshot tenha emitido.
        """
        self._passar_tempo(timedelta(seconds=self.settings.trading.market_data_interval_seconds))
        await self.orquestrador.market_data.refresh()
        await self.aquietar()
        snapshot = await self.orquestrador.portfolio.build_snapshot()
        await self.aquietar()
        return snapshot

    async def publicar_sinal(
        self,
        symbol: str,
        direcao: SignalDirection = SignalDirection.LONG,
        *,
        confianca: float = 0.8,
        estrategia: str = "ma_crossover",
    ) -> Signal:
        """Publica um sinal como o Strategy Agent publicaria (gravado, depois no bus).

        Para cenarios cuja pergunta e o que Risk Manager e Execution fazem com
        um sinal, e nao se a estrategia o emitiria.
        """
        sinal = Signal(
            exchange=ExchangeName(self.settings.exchange),
            symbol=symbol,
            timeframe=self.settings.trading.timeframe,
            strategy=estrategia,
            direction=direcao,
            confidence=confianca,
            reason="sinal roteirizado pelo cenario",
            reference_price=self.mercado.ultimo_fechamento(symbol),
        )
        async with session_scope(self.settings) as sessao:
            await SignalRepository(sessao).save(sinal)
        await self.orquestrador.bus.publish(Topics.SIGNALS, sinal)
        await self.aquietar()
        return sinal

    def reproduzir_fiacao_antiga(self) -> None:
        """CONTRAPROVA: religa o sistema como ele era antes da correcao do preco.

        Antes: Portfolio, Risk e a sincronizacao do papel liam
        `market_data.latest_prices` (fechamento do ultimo candle fechado), e o
        `PaperBroker` executava com o preco da ultima sincronizacao. Aqui os
        dois caminhos voltam a ser exatamente esses -- para que um cenario possa
        mostrar que a fiacao antiga NAO protegia (ou executava errado) no mesmo
        roteiro em que a nova protege.
        """
        dados = self.orquestrador.market_data

        def fechamentos() -> dict[str, Decimal]:
            return dict(dados.latest_prices)

        dados.precos_para_avaliacao = fechamentos  # type: ignore[method-assign]
        # O Risk Manager pode ter recebido a fonte de preco na subida (metodo
        # ligado, que a troca acima nao alcanca): religa ela tambem.
        definir = getattr(self.orquestrador.risk_manager, "definir_precos_de_mercado", None)
        if definir is not None:
            dados.precos_de_mercado = fechamentos  # type: ignore[method-assign]
            definir(fechamentos)
        # Fonte vazia: `_reference_price` cai no ultimo preco sincronizado.
        self.broker.set_price_source(dict)

    def _passar_tempo(self, quanto: timedelta) -> None:
        """Envelhece o ultimo ticker lido, como se `quanto` tivesse passado."""
        lidos = self.orquestrador.market_data._precos_correntes_em
        for chave in lidos:
            lidos[chave] -= quanto

    async def aquietar(self, tempo_maximo: float = TEMPO_MAXIMO_PARA_AQUIETAR) -> None:
        """Espera ate nenhum agente ter evento pendente nem trabalho em curso.

        Agente pausado conta como quieto: ele esta parado no portao de pausa e
        nada mais vai acontecer com o que estiver com ele ate alguem retoma-lo.
        """
        loop = asyncio.get_running_loop()
        limite = loop.time() + tempo_maximo
        quietas = 0
        while loop.time() < limite:
            await asyncio.sleep(INTERVALO_DE_LEITURA)
            if self._quieto():
                quietas += 1
                if quietas >= LEITURAS_QUIETAS:
                    return
            else:
                quietas = 0
        ocupados = {
            nome: {"idle": a.is_idle, "pendentes": a.pending_events, "estado": str(a.state)}
            for nome, a in self.orquestrador.agents.items()
        }
        raise TimeoutError(f"o sistema nao aquietou em {tempo_maximo}s: {ocupados}")

    def _quieto(self) -> bool:
        for agente in self.orquestrador.agents.values():
            if agente.pending_events or not agente.is_idle:
                return False
        filas = getattr(self.orquestrador.bus, "_subscribers", {})
        return not any(
            fila.qsize()
            for topico in (Topics.CANDLES, Topics.SIGNALS, Topics.ORDER_REQUESTS)
            for fila in filas.get(topico, [])
        )

    # ------------------------------------------------------------------
    # Leitura do que aconteceu
    # ------------------------------------------------------------------
    @property
    def broker(self) -> PaperBroker:
        broker = self.orquestrador._broker
        assert isinstance(broker, PaperBroker), "cenario so roda em dry_run"
        return broker

    async def trades(self) -> list[Any]:
        """Trades em ordem cronologica."""
        async with session_scope(self.settings) as sessao:
            linhas = await TradeRepository(sessao).list(limit=1000)
        return sorted(linhas, key=lambda t: t.executed_at)

    async def ordens(self) -> list[Any]:
        async with session_scope(self.settings) as sessao:
            linhas = await OrderRepository(sessao).list(limit=1000)
        return sorted(linhas, key=lambda o: o.created_at)

    async def eventos_de_risco(self) -> list[Any]:
        async with session_scope(self.settings) as sessao:
            linhas = await RiskEventRepository(sessao).list(limit=1000)
        return sorted(linhas, key=lambda e: e.created_at)

    async def saidas_de_protecao(self) -> list[Any]:
        """Avaliacoes de saida de protecao gravadas pelo Risk Manager."""
        return [
            e
            for e in await self.eventos_de_risco()
            if (e.snapshot or {}).get("origem") == "protecao"
        ]

    async def auditoria(self) -> list[Any]:
        async with session_scope(self.settings) as sessao:
            linhas = await AuditLogRepository(sessao).list(limit=1000)
        return sorted(linhas, key=lambda r: r.timestamp)

    async def saude(self) -> dict[str, Any]:
        return await self.orquestrador.health()

    async def saldo(self, ativo: str) -> Decimal:
        return (await self.broker.fetch_balances()).get(ativo, Decimal(0))
