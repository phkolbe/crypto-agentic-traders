"""Risk Manager Agent — o guardiao.

Todo sinal passa por aqui antes de virar ordem. O agente e a casca com estado
(banco, circuit breaker, limites vigentes) em volta do `RiskEngine`, que contem a
logica pura e testada.

Duas garantias estruturais:

1. Apenas este agente constroi `OrderRequest`, e todo `OrderRequest` carrega um
   `risk_event_id` -- a chave da linha ja gravada em `risk_events`. O Execution
   Agent nao aceita outra coisa, entao nao existe atalho de sinal para ordem.
   Isto vale para os DOIS caminhos que publicam ordem: o sinal avaliado
   (`_persist_and_publish`) e a saida de protetiva (`enforce_protective_exits`).
   O segundo montava um texto na hora -- "protecao-stop_loss-BTC" -- que nao era
   chave de nada, e a garantia era falsa exatamente onde dinheiro real saia da
   posicao sem registro de risco.
2. A avaliacao e persistida **antes** de a ordem ser publicada. Se o processo
   morrer no meio, sobra o registro do que foi decidido e por que.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ..bus import EventBus, Topics
from ..config import RiskSettings, Settings
from ..db.repositories import (
    AuditLogRepository,
    OrderRepository,
    PortfolioSnapshotRepository,
    RiskConfigRepository,
    RiskEventRepository,
)
from ..db.session import session_scope
from ..domain.enums import (
    ExchangeName,
    OrderStatus,
    OrderType,
    RiskDecision,
    RiskEventType,
    Side,
    SignalDirection,
)
from ..domain.models import OrderRequest, PortfolioSnapshot, Position, RiskAssessment, Signal
from ..exchanges.filters import MarketFilter
from ..onchain import OnChainProvider
from ..risk.rules import PortfolioState, RiskEngine
from .base import BaseAgent

#: Desfechos em que a ordem de protecao NAO vendeu nada. A posicao segue aberta e
#: desprotegida, entao a trava tem de cair para o stop ser tentado de novo.
SAIDA_SEM_VENDA = frozenset(
    {
        str(OrderStatus.REJECTED),
        str(OrderStatus.FAILED),
        str(OrderStatus.CANCELED),
    }
)

#: Tentativas de saida protetiva por ativo antes de o sistema desistir e dizer
#: que desistiu. Recusa transitoria (IP, rede) passa em uma ou duas; recusa
#: permanente (valor abaixo do minimo da exchange) nunca passa, e insistir a cada
#: retrato daria uma ordem recusada e um alerta por minuto.
MAX_TENTATIVAS_DE_SAIDA = 3

#: Ordem de protecao MARKET sem desfecho por mais tempo que isto e anomalia:
#: nao soltamos a trava (revender por cima de uma ordem viva duplicaria a venda),
#: mas alguem precisa saber que a posicao pode estar sem protecao.
SAIDA_SEM_DESFECHO = timedelta(minutes=10)

#: Prazo para a ordem de protecao APARECER em `orders`. O Execution Agent grava
#: a linha PENDING antes de falar com a exchange, no mesmo processo -- questao de
#: milissegundos. Passado este prazo sem linha nenhuma, a ordem nao existe: ou
#: foi recusada ANTES de ser gravada (`_refuse_by_filter`, que registra so em
#: `audit_log`), ou o pedido se perdeu. Nos dois casos nada foi enviado, entao
#: nao ha venda para duplicar -- e a trava tem de cair. Uma unica volta do
#: Portfolio Agent (60s) nao basta como prazo: dois retratos dao a margem.
SAIDA_SEM_REGISTRO = timedelta(minutes=2)


@dataclass(frozen=True)
class _SaidaEmVoo:
    """Ordem de protecao emitida e ainda sem desfecho conhecido."""

    client_order_id: str
    quantity: Decimal
    emitted_at: datetime
    reason: str


@dataclass(frozen=True)
class _Reserva:
    """Ordem aprovada e publicada que o retrato do portfolio ainda nao mostra.

    A compra reserva caixa, quantidade (exposicao e vagas) e o cooldown; a venda
    reserva so a QUANTIDADE, como "em voo" (`PortfolioState.em_voo`): o retrato
    ainda mostra a posicao inteira, e um segundo FLAT a venderia de novo.
    """

    client_order_id: str
    symbol: str
    asset: str
    quantity: Decimal
    notional: Decimal
    price: Decimal
    emitted_at: datetime
    venda: bool = False


#: Desfechos em que a ordem terminou. Com algo preenchido, o dinheiro saiu e a
#: reserva espera o retrato mostrar a posicao; sem nada preenchido, cai na hora.
#: `PARTIALLY_FILLED` entra porque e o desfecho final que o Execution grava para a
#: ordem a mercado que preencheu so uma parte (`execution.py:_classificar`).
ORDEM_TERMINADA = SAIDA_SEM_VENDA | {
    str(OrderStatus.FILLED),
    str(OrderStatus.PARTIALLY_FILLED),
}


class RiskManagerAgent(BaseAgent):
    name = "risk_manager"

    def __init__(
        self, bus: EventBus, settings: Settings, onchain: OnChainProvider | None = None
    ) -> None:
        super().__init__(bus)
        self._settings = settings
        self._onchain = onchain
        """Provedor on-chain opcional. Ausente significa "sem filtro de regime"."""

        self._limits = settings.risk
        self._engine = RiskEngine(self._limits, settings.trading.quote_currency)
        self._snapshot: PortfolioSnapshot | None = None
        self._circuit_breaker_active = False
        self._circuit_breaker_reason: str | None = None
        self._capital_alerted = False
        """Aviso de saldo nao autorizado sai uma vez por transicao."""

        self._precos_de_mercado: Callable[[], dict[str, Decimal]] | None = None
        """Preco corrente recente por ativo (o orquestrador liga ao Market Data).

        Quando ligado, stop e alvo so sao avaliados para ativo com preco aqui, e a
        ordem e dimensionada nele. Desligado (testes de unidade), vale o preco do
        snapshot e o do sinal, como antes.
        """

        self._exiting: set[str] = set()
        """Ativos com ordem de protecao em voo.

        Sem esta trava, cada snapshot reemitiria o mesmo fechamento enquanto a
        ordem anterior nao tivesse liquidado -- vendendo a posicao varias vezes.
        """

        self._exits_in_flight: dict[str, _SaidaEmVoo] = {}
        """Ativo -> qual pedido de protecao a trava acima esta esperando.

        A trava precisa saber QUAL ordem espera, e nao apenas que espera algo:
        antes disto ela caia por um unico criterio -- o ativo desaparecer da
        carteira -- e uma ordem recusada pela exchange prendia a posicao sem
        stop para sempre. Ver `_reconcile_exits`.
        """

        self._sem_base_de_custo: set[str] = set()
        """Ativos sem preco medio, ja avisados. Vazio = nada pendente de aviso."""

        self._exit_failures: dict[str, int] = {}
        """Ativo -> saidas de protecao recusadas em sequencia."""

        self._exit_abandoned: set[str] = set()
        """Ativos cuja saida o sistema parou de tentar, e ja avisou."""

        self._saida_parada_avisada: set[str] = set()
        """Ativos com ordem de protecao viva e sem desfecho, ja avisados.

        Mesmo criterio do portao de capital e do aviso de posicao sem base de
        custo: uma vez por TRANSICAO. Sem ele o aviso saia a cada retrato --
        1.440 por dia, a patologia do achado 1 do ensaio, e no unico aviso que
        diz "esta posicao talvez esteja sem stop".
        """

        self._loss_unconfirmed_alerted: set[str] = set()
        """Periodos cuja divergencia entre as duas medidas de perda ja foi avisada.

        Por PERIODO, e nao um booleano so: com uma flag unica, a avaliacao
        semanal (dentro do limite) zerava o aviso da diaria a cada retrato, e a
        mensagem voltava a sair de 60 em 60 segundos -- a mesma patologia de
        1.440 avisos por dia que o resto deste agente evita.
        """

        self._reservas: dict[str, _Reserva] = {}
        """Ordens publicadas e ainda nao refletidas num retrato, por `client_order_id`.

        Compras e, desde a rodada 3, vendas (`_Reserva.venda`): a venda nao mexe
        no caixa, so marca a quantidade como em voo para um segundo FLAT nao
        vende-la de novo.

        A simulacao de caixa e vagas consumidas (`_apply_fill`) so vale DENTRO de
        um lote. Com a janela de lote em 0 (o padrao), cada sinal e um lote, e
        todos os sinais entre dois retratos (60s) viam o mesmo caixa. Medido na
        auditoria de 09/11/2026 com a configuracao de producao: tres LONG no
        fechamento diario, cada um vendo 5,89 USDC utilizaveis, tres aprovados --
        exposicao total 41,0 sobre 29,29 autorizados (+40%). `_build_state`
        desconta estas reservas do retrato; `_conciliar_reservas` as solta.
        """

        self._retratos_em: tuple[datetime | None, datetime | None] = (None, None)
        """Carimbo do retrato anterior e do atual (o do fim da montagem).

        O carimbo de um retrato e posto DEPOIS de ler o saldo, entao uma ordem
        terminada antes dele pode nao estar no saldo lido. Mas o retrato seguinte
        so comeca a ler depois de o anterior ser publicado: uma ordem terminada
        antes do carimbo do retrato ANTERIOR esta, com certeza, no atual.
        """

        self._sem_preco_avisados: set[str] = set()
        """Posicoes sem preco corrente recente, ja avisadas (uma vez por transicao)."""

        self._poeira: dict[str, Decimal] = {}
        """Ativo -> quantidade de poeira que nao recebe stop.

        Entra aqui o resto de uma saida de protecao abaixo do minimo negociavel
        (ver `_reconcile_exits`) e, quando o filtro do par e conhecido, qualquer
        sobra que a exchange nao aceitaria vender (ver `_e_poeira_invendavel`).
        O stop volta a valer quando a posicao cresce acima dela.
        """

        self._filtro_de_mercado: Callable[[str], MarketFilter | None] | None = None
        """De onde ler o passo e o minimo do par (o broker), para reconhecer poeira."""

        self._abaixo_do_minimo_ao_desistir: set[str] = set()
        """Ativos abandonados que valiam menos que o minimo da exchange na desistencia.

        So para estes "passar a valer acima do minimo" destrava: um ativo
        abandonado por recusa permanente de outra natureza (credencial sem spot)
        ja valia acima, e destrava-lo a cada retrato reabriria o laco de
        recusa e alerta que o abandono existe para fechar.
        """

        self._limits_lock = asyncio.Lock()
        """Serializa a alteracao de limites. Ver `update_limits`."""

        self._discovered_universe: tuple[list[str], list[str]] | None = None
        """Whitelist vinda da descoberta automatica: (simbolos, ativos).

        Guardada separada dos limites porque `_load_state()` recarrega os
        limites do banco a cada sinal -- se o universo morasse dentro deles,
        cada recarga apagaria o resultado da ultima varredura.
        """

    @property
    def limits(self) -> RiskSettings:
        """Limites em vigor, ja com o universo descoberto aplicado."""
        return getattr(self, "_effective_limits", self._limits)

    @property
    def configured_limits(self) -> RiskSettings:
        """Limites como configurados, sem o universo da descoberta."""
        return self._limits

    @property
    def last_snapshot(self) -> PortfolioSnapshot | None:
        """Ultimo retrato recebido do Portfolio Agent."""
        return self._snapshot

    @property
    def circuit_breaker_active(self) -> bool:
        return self._circuit_breaker_active

    # ------------------------------------------------------------------
    async def _run(self) -> None:
        await self._load_state()

        # Uma tarefa alimenta a fila; o loop principal a drena em lotes. Iterar
        # a assinatura direto bloquearia ate o proximo sinal, e sem poder
        # esperar pela rajada nao ha o que comparar.
        queue: asyncio.Queue[Signal] = asyncio.Queue()

        async def feed() -> None:
            async for signal in self.bus.subscribe(Topics.SIGNALS):
                await queue.put(signal)

        # `spawn` e nao `create_task`: o alimentador precisa entrar na conta da
        # vitalidade do agente. Enquanto so ele declarava espera, o laco de
        # avaliacao abaixo podia ficar pendurado para sempre e o watchdog via um
        # guardiao saudavel -- e toda ordem passa por este laco.
        feeder = self.spawn(feed(), name="signal-feed")
        try:
            while True:
                batch = await self._collect_batch(queue)
                await self.wait_if_paused()
                try:
                    await self._on_batch(batch)
                except Exception as exc:
                    # Falhar ao avaliar NAO pode virar "aprovado". A excecao e
                    # registrada e os sinais simplesmente nao geram ordem.
                    self.log.exception("risk.evaluation_failed", error=str(exc))
                await self.heartbeat()
        finally:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feeder
            if not queue.empty():
                # A caixa de entrada duravel protege ate aqui: o que ja foi
                # transferido para esta fila local morre com a tarefa. Poucos
                # sinais, mas nao pode ser em silencio.
                self.log.error(
                    "risk.signals_lost_in_restart",
                    quantidade=queue.qsize(),
                    detail="sinais na fila local descartados na troca da tarefa",
                )

    async def _collect_batch(self, queue: asyncio.Queue[Signal]) -> list[Signal]:
        """Espera o primeiro sinal e junta os que chegarem na janela.

        A janela e o que transforma "quem chegou primeiro" em "qual e o melhor".
        Quanto maior, melhor a escolha e mais atrasada a execucao; o padrao de 2s
        cobre a rajada de um ciclo de coleta sem atrasar de forma perceptivel.
        """
        # A espera pelo PRIMEIRO sinal e o estado normal deste laco -- com
        # candle diario ele passa horas aqui. Declarar a espera e o que separa
        # "esperando trabalho" de "pendurado no meio do trabalho": sem esta
        # marcacao o laco de avaliacao nunca contaria como ocioso e o watchdog
        # reiniciaria o Risk Manager a cada 10 minutos (o defeito de D25); com
        # ela marcada no lugar errado, um travamento em `_on_batch` passaria por
        # ocioso. Por isso ela cobre o `get`, e nada alem dele.
        self.enter_idle("aguardando sinal")
        try:
            batch = [await queue.get()]
        finally:
            self.leave_idle()

        window = self._settings.trading.signal_batch_window_seconds
        if window <= 0:
            return batch

        loop = asyncio.get_running_loop()
        deadline = loop.time() + window
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(queue.get(), timeout=remaining))
            except TimeoutError:
                break
        return batch

    async def _on_batch(self, batch: list[Signal]) -> None:
        """Decide o lote inteiro de uma vez, por confianca."""
        # O motor dimensiona a quantidade e ancora stop e alvo no preco do sinal;
        # o sinal sai com o fechamento do candle. O mesmo sinal (mesmo id), com o
        # preco do mercado de agora, e o que segue daqui em diante.
        batch = [self._no_preco_corrente(signal) for signal in batch]
        if self._snapshot is None:
            for signal in batch:
                await self._persist_and_publish(
                    RiskAssessment(
                        signal_id=signal.id,
                        decision=RiskDecision.REJECTED,
                        reasons=["portfolio ainda nao apurado (aguardando primeiro snapshot)"],
                    ),
                    signal,
                )
            return

        # Abertura sem preco corrente recente nao e dimensionada: cair no
        # fechamento do candle (com `1d`, o de ontem) deixava a compra passar do
        # teto por ordem e de exposicao na proporcao da distancia -- fechamento
        # 100, mercado 108, gasto 8% acima do teto (auditoria 09/11/2026, M3). O
        # FLAT continua passando: fechar nunca e travado, e quantidade de saida
        # vem da carteira, nao do preco.
        sem_preco = [s for s in batch if self._abertura_sem_preco_corrente(s)]
        for signal in sem_preco:
            await self._persist_and_publish(
                RiskAssessment(
                    signal_id=signal.id,
                    decision=RiskDecision.REJECTED,
                    reasons=[
                        f"sem preco corrente recente de {signal.symbol}; abertura nao "
                        "dimensionada no fechamento do candle"
                    ],
                ),
                signal,
            )
        recusados = {s.id for s in sem_preco}
        batch = [s for s in batch if s.id not in recusados]
        if not batch:
            return

        await self._load_state()
        state = await self._build_state(batch[0], {s.symbol for s in batch})
        for symbol in {s.symbol for s in batch}:
            base = symbol.partition("/")[0]
            for signal in batch:
                if signal.symbol == symbol:
                    state.prices[base] = self._preco_de_dimensionamento(signal)
                    break

        decisoes = self._engine.evaluate_batch(batch, state)
        if len(batch) > 1:
            self.log.info(
                "risk.batch_evaluated",
                sinais=len(batch),
                aprovados=sum(
                    1 for _, a in decisoes if a.decision is RiskDecision.APPROVED
                ),
                ranking=[
                    f"{s.symbol}@{s.confidence:.2f}"
                    for s, _ in decisoes
                    if s.direction is SignalDirection.LONG
                ],
            )
        for signal, assessment in decisoes:
            await self._persist_and_publish(assessment, signal)

    async def _load_state(self) -> None:
        """Carrega limites e estado do circuit breaker do banco.

        O `.env` fornece os valores iniciais; a partir da primeira alteracao pela
        interface, o banco e a fonte da verdade.

        Sob a `_limits_lock`, como `update_limits`: ler o banco e atribuir na
        memoria tem `await` no meio, e uma alteracao que terminasse entre os dois
        era sobrescrita aqui pelo valor ANTIGO lido -- a API respondia o
        `stop_loss_pct` novo e o stop seguia com o antigo ate a recarga seguinte
        (com 1d, ate um dia; auditoria de 09/11/2026, L1).
        """
        async with self._limits_lock:
            async with session_scope(self._settings) as session:
                config = await RiskConfigRepository(session).get_or_create(
                    self._limits.model_dump(mode="json")
                )
                self._circuit_breaker_active = config.circuit_breaker_active
                self._circuit_breaker_reason = config.circuit_breaker_reason
                stored = dict(config.values or {})

            if stored:
                try:
                    self._limits = RiskSettings.model_validate(stored)
                except Exception as exc:
                    # Config invalida no banco nao pode derrubar o guardiao:
                    # seguimos com os limites do .env, conservadores por padrao.
                    self.log.error("risk.stored_limits_invalid", error=str(exc))

            self._rebuild_engine()

    def _rebuild_engine(self) -> None:
        """Aplica o universo descoberto por cima dos limites vigentes.

        Divisao de responsabilidade: o banco (e a interface) governam os numeros
        -- tamanho de ordem, stop, exposicao; a descoberta governa QUAIS pares
        entram. Sem essa separacao, salvar um limite pela interface reverteria a
        whitelist para a lista estatica do `.env`.
        """
        limits = self._limits
        if self._discovered_universe is not None:
            symbols, assets = self._discovered_universe
            limits = limits.model_copy(
                update={"symbol_whitelist": list(symbols), "asset_whitelist": list(assets)}
            )
        self._effective_limits = limits
        self._engine = RiskEngine(limits, self._settings.trading.quote_currency)

    async def apply_discovered_universe(self, symbols: list[str], assets: list[str]) -> None:
        """Adota o resultado da descoberta como whitelist efetiva.

        Toda mudanca vai para o audit log: no modo "mar aberto" a whitelist deixa
        de ser digitada a mao, mas nao deixa de ser rastreavel -- em qualquer
        instante da para saber o que o sistema podia negociar e desde quando.
        """
        previous = self._discovered_universe[0] if self._discovered_universe else []
        if sorted(previous) == sorted(symbols):
            return

        self._discovered_universe = (list(symbols), list(assets))
        self._rebuild_engine()

        async with session_scope(self._settings) as session:
            await AuditLogRepository(session).append(
                action="trading_universe_discovered",
                actor="discovery",
                target="risk_config",
                before={"symbols": previous},
                after={"symbols": list(symbols)},
                detail="whitelist efetiva definida por descoberta automatica de mercado",
            )
        self.log.warning("risk.universe_updated", symbols=symbols)

    # ------------------------------------------------------------------
    def observe_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        """Recebe o retrato mais recente do portfolio, publicado pelo Portfolio Agent."""
        self._snapshot = snapshot
        self._marcar_retrato(snapshot)

    def _marcar_retrato(self, snapshot: PortfolioSnapshot) -> None:
        """Guarda o carimbo do retrato anterior e do atual (ver `_retratos_em`).

        Idempotente por carimbo: o orquestrador entrega o MESMO retrato a
        `observe_snapshot` e a `enforce_protective_exits`, e contar duas vezes
        faria o "anterior" ser o proprio atual.
        """
        quando = _aware(snapshot.timestamp)
        atual = self._retratos_em[1]
        if atual is not None and quando <= atual:
            return
        self._retratos_em = (atual, quando)

    def _retrato_mostra(self, terminada_em: datetime) -> bool:
        """O retrato atual ja inclui uma ordem que terminou neste instante?"""
        anterior = self._retratos_em[0]
        return anterior is not None and _aware(terminada_em) < anterior

    async def check_capital_authorization(self, snapshot: PortfolioSnapshot) -> Decimal:
        """Avisa quando existe saldo que o sistema nao pode usar.

        Um deposito nao e uma ordem. Dinheiro que entra na conta por qualquer
        motivo -- venda de outro ativo, transferencia, reserva para outra
        finalidade -- nao deveria virar exposicao sem alguem dizer que sim. O
        portao existe para essa distincao.

        Mas dinheiro autorizado e parado tambem e um problema, e o oposto do
        anterior: o sistema ficaria de pe com caixa ocioso sem ninguem perceber.
        Por isso o aviso e ativo, e nao apenas uma linha no `check`.

        O alerta sai **uma vez por transicao**, nao a cada snapshot: com o
        Portfolio Agent rodando a cada 60s, avisar sempre seriam 1.440 mensagens
        por dia sobre o mesmo saldo, e um alerta que chega todo minuto deixa de
        ser lido.
        """
        autorizado = self._limits.authorized_capital
        if autorizado is None:
            return Decimal(0)

        nao_autorizado = max(Decimal(0), snapshot.total_value - autorizado)
        moeda = self._settings.trading.quote_currency
        # Tolerancia: variacao de preco das posicoes move o patrimonio para cima
        # sem que tenha entrado dinheiro. Avisar por causa disso seria ruido.
        relevante = nao_autorizado >= self._limits.min_order_notional

        if relevante and not self._capital_alerted:
            self._capital_alerted = True
            self.log.warning(
                "risk.unauthorized_capital",
                patrimonio=str(snapshot.total_value),
                autorizado=str(autorizado),
                parado=str(nao_autorizado),
            )
            await self.bus.publish(
                Topics.ALERTS,
                {
                    "type": "unauthorized_capital",
                    "title": f"{nao_autorizado:.2f} {moeda} disponiveis e nao autorizados",
                    "detail": (
                        f"O patrimonio e {snapshot.total_value:.2f} {moeda} e o capital "
                        f"autorizado a operar e {autorizado:.2f} {moeda}. "
                        f"O sistema NAO vai usar a diferenca ate voce autorizar. "
                        f"Autorize em Risco, na interface, para por esse saldo a trabalhar."
                    ),
                },
            )
        elif not relevante:
            # Saldo autorizado ou consumido: rearma o aviso para o proximo aporte.
            self._capital_alerted = False

        return nao_autorizado

    async def authorize_all_capital(self, actor: str = "user") -> RiskSettings:
        """Autoriza o patrimonio inteiro a operar, no valor apurado agora.

        Deliberadamente grava um NUMERO em vez de desligar o portao: desligar
        autorizaria tambem todo deposito futuro, que e exatamente o que o portao
        existe para impedir. Autorizar e um ato sobre o saldo de hoje.
        """
        if self._snapshot is None:
            raise ValueError(
                "portfolio ainda nao apurado; aguarde o primeiro snapshot para autorizar"
            )
        return await self.update_limits(
            {"authorized_capital": str(self._snapshot.total_value)}, actor=actor
        )

    async def enforce_protective_exits(self, snapshot: PortfolioSnapshot) -> list[OrderRequest]:
        """Fecha posicoes que romperam stop-loss ou take-profit.

        Ate aqui esses niveis eram calculados, gravados na ordem e nunca
        comparados com preco nenhum -- em producao **nem em backtest**. Uma
        posicao aberta so fechava se a estrategia emitisse sinal de saida.

        Esta e a versao em software, deliberadamente escolhida em vez de mandar
        uma OCO para a exchange. O que ela **nao** cobre, e precisa estar claro:
        morre junto com o processo, e nao age se o sistema perder acesso a
        exchange (o cenario de IP residencial descrito na secao 1 do
        `docs/SEGURANCA.md`). Protecao que depende do processo estar vivo cobre
        oscilacao de mercado, nao queda de infraestrutura.

        Ha um terceiro limite, que nao e escolha e sim consequencia: **posicao
        sem preco medio nao tem nivel, e portanto nao tem stop.** O preco medio
        e reconstruido do historico de trades na moeda de cotacao corrente, logo
        uma posicao herdada de outra cotacao (a troca BRL -> USDC de D24) ou
        chegada por transferencia fica de fora. Isso nao pode ser silencioso, e
        por isso `_report_missing_cost_basis` avisa.

        Reage a cada snapshot -- por padrao 60s -- comparando o preco corrente
        com os niveis. Nao ve o pavio dentro do intervalo: uma queda que desce
        abaixo do stop e volta antes do proximo snapshot passa batida. O backtest,
        que le a minima do candle, e nesse ponto mais severo que a producao.

        O nivel vem do **preco medio** da posicao, reconstruido pelo Portfolio
        Agent a partir do historico de trades -- o que inclui lancamentos
        manuais. Uma posicao comprada fora do sistema tambem passa a ser
        protegida, o que e o comportamento desejado: o Risk Manager guarda a
        carteira, nao apenas as ordens que ele originou.
        """
        limits = self._limits
        emitidas: list[OrderRequest] = []
        self._marcar_retrato(snapshot)
        mercado = self._precos_de_mercado() if self._precos_de_mercado is not None else None

        # Antes de decidir qualquer coisa nova: o que aconteceu com as ordens de
        # protecao anteriores? Sem isto a trava vira uma prisao (ver o metodo).
        await self._reconcile_exits(snapshot, mercado)

        sem_base: set[str] = set()
        sem_preco: set[str] = set()
        em_carteira = {p.asset for p in snapshot.positions if p.quantity > 0}
        for asset in set(self._poeira) - em_carteira:
            del self._poeira[asset]
        for asset in set(self._exit_failures) - em_carteira - self._exiting:
            # A posicao saiu da carteira por outro caminho (FLAT, venda manual)
            # depois de uma recusa ja liberada: o episodio acabou. A contagem so
            # zerava em `_reconcile_exits`, e o proximo episodio comecava com as
            # tentativas ja gastas (auditoria de 09/11/2026, L2).
            self._esquecer_falhas(asset)
        for position in snapshot.positions:
            if position.asset == self._settings.trading.quote_currency:
                continue
            if position.quantity <= 0:
                continue
            poeira = self._poeira.get(position.asset)
            if poeira is not None:
                if position.quantity <= poeira:
                    # Resto de uma saida de protecao abaixo do minimo negociavel:
                    # a exchange recusaria a venda, e insistir era o caminho para
                    # o abandono (ver `_reconcile_exits`). Ja foi dito uma vez.
                    continue
                # Cresceu acima da poeira: compra nova, e o stop volta a valer.
                del self._poeira[position.asset]
            if position.average_price is None or position.average_price <= 0:
                # Sem preco medio nao existe nivel, e chutar um seria pior que
                # nao agir -- mas ficar em silencio e pior que os dois. Uma
                # posicao aqui NAO TEM STOP NENHUM, e o caso e concreto: apos a
                # troca da moeda de cotacao (D24) o historico em BRL deixa de
                # entrar no custo medio em USDC, e a posicao herdada fica sem
                # base. Tambem cai aqui o que chegou por transferencia ou
                # airdrop, sem nenhuma compra correspondente.
                sem_base.add(position.asset)
                continue
            if mercado is not None:
                # So o preco corrente recente decide stop e alvo. Ativo sem ele
                # fica sem avaliacao neste snapshot; comparar com o fechamento do
                # candle venderia por engano. Mas nao em silencio: o Market Data
                # so alertava sobre os pares do universo, e um ativo em carteira
                # cujo par saiu da lista (revarredura, edicao de SYMBOLS) ficava
                # sem stop e sem aviso -- o contrario do que o SEGURANCA §12
                # promete ("sem avaliacao e com alerta").
                corrente = mercado.get(position.asset)
                if corrente is None:
                    sem_preco.add(position.asset)
                    continue
                position = position.model_copy(update={"current_price": corrente})
            if position.current_price is None:
                continue
            if self._e_poeira_invendavel(position):
                continue
            if position.asset in self._exiting:
                # Ordem de protecao ja em voo: reemitir a cada snapshot venderia
                # a posicao varias vezes.
                continue

            stop = position.average_price * (Decimal(1) - Decimal(str(limits.stop_loss_pct)))
            alvo = position.average_price * (Decimal(1) + Decimal(str(limits.take_profit_pct)))

            if position.current_price <= stop:
                motivo = "stop_loss"
            elif position.current_price >= alvo:
                motivo = "take_profit"
            else:
                continue

            symbol = f"{position.asset}/{self._settings.trading.quote_currency}"
            nivel = stop if motivo == "stop_loss" else alvo
            # A linha em `risk_events` vem ANTES da ordem, igual ao caminho do
            # sinal: `risk_event_id` tem de ser a chave de uma linha que existe.
            # Enquanto era um texto montado na hora ("protecao-stop_loss-BTC"),
            # uma venda a mercado de dinheiro real saia sem nenhum registro de
            # risco -- e a garantia 1 no topo deste modulo era falsa.
            avaliacao = RiskAssessment(
                signal_id=None,
                decision=RiskDecision.APPROVED,
                reasons=[
                    f"saida de protecao ({motivo}): preco {position.current_price} "
                    f"contra nivel {nivel} (medio {position.average_price})"
                ],
                approved_quantity=position.quantity,
                approved_notional=position.quantity * position.current_price,
                snapshot={
                    "symbol": symbol,
                    "origem": "protecao",
                    "motivo": motivo,
                    "nivel": str(nivel),
                    "average_price": str(position.average_price),
                    "current_price": str(position.current_price),
                    "quantity": str(position.quantity),
                    "stop_loss_pct": limits.stop_loss_pct,
                    "take_profit_pct": limits.take_profit_pct,
                    "closing": True,
                },
            )
            request = OrderRequest(
                client_order_id=_client_order_id(),
                signal_id=None,
                risk_event_id=avaliacao.id,
                exchange=ExchangeName(self._settings.exchange),
                symbol=symbol,
                side=Side.SELL,
                order_type=OrderType.MARKET,
                quantity=position.quantity,
                notional=position.quantity * position.current_price,
                strategy="protecao",
            )

            async with session_scope(self._settings) as session:
                await RiskEventRepository(session).save_assessment(avaliacao)
                await AuditLogRepository(session).append(
                    action="protective_exit_emitted",
                    actor="risk_manager",
                    target=symbol,
                    detail=avaliacao.reasons[0],
                    after={
                        "client_order_id": request.client_order_id,
                        "risk_event_id": request.risk_event_id,
                        "quantity": str(request.quantity),
                        "notional": str(request.notional),
                    },
                )

            self._exiting.add(position.asset)
            self._exits_in_flight[position.asset] = _SaidaEmVoo(
                client_order_id=request.client_order_id,
                quantity=position.quantity,
                emitted_at=_aware(snapshot.timestamp),
                reason=motivo,
            )
            self.log.warning(
                "risk.protective_exit",
                motivo=motivo,
                symbol=symbol,
                preco=str(position.current_price),
                nivel=str(nivel),
                medio=str(position.average_price),
            )
            await self.bus.publish(Topics.ORDER_REQUESTS, request)
            await self.bus.publish(
                Topics.ALERTS,
                {
                    "type": "protective_exit",
                    "title": f"{'Stop-loss' if motivo == 'stop_loss' else 'Take-profit'} "
                    f"acionado em {symbol}",
                    "detail": (
                        f"preco {position.current_price} contra nivel {nivel} "
                        f"(medio {position.average_price}). Fechamento EMITIDO -- "
                        "a confirmacao e a ordem preenchida."
                    ),
                },
            )
            emitidas.append(request)

        await self._report_missing_cost_basis(sem_base)
        await self._avisar_sem_preco_corrente(sem_preco)
        return emitidas

    async def _avisar_sem_preco_corrente(self, sem_preco: set[str]) -> None:
        """Avisa sobre posicao cujo stop nao esta sendo avaliado por falta de preco.

        Uma vez por transicao, como `_report_missing_cost_basis`: o estado pode
        durar horas, e um aviso por retrato seriam 1.440 por dia.
        """
        if sem_preco == self._sem_preco_avisados:
            return
        novos = sorted(sem_preco - self._sem_preco_avisados)
        voltaram = sorted(self._sem_preco_avisados - sem_preco)
        self._sem_preco_avisados = set(sem_preco)
        if voltaram:
            self.log.info("risk.preco_corrente_restabelecido", assets=voltaram)
        if not novos:
            return
        self.log.error(
            "risk.position_without_current_price",
            assets=novos,
            detail="sem preco corrente recente: stop e alvo destas posicoes nao sao avaliados",
        )
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "position_without_current_price",
                "title": f"Stop-loss sem avaliacao: {', '.join(novos)}",
                "message": (
                    f"Nao ha preco corrente recente para {', '.join(novos)}, entao "
                    "stop-loss e take-profit destas posicoes NAO estao sendo "
                    "avaliados (comparar com o fechamento do candle venderia por "
                    "engano). Se o par saiu da lista negociada, a posicao continua "
                    "na carteira: confira a coleta de precos ou feche a mao."
                ),
            },
        )

    async def _reconcile_exits(
        self, snapshot: PortfolioSnapshot, mercado: dict[str, Decimal] | None = None
    ) -> None:
        """Solta a trava de saida quando a ordem de protecao nao vendeu nada.

        A trava anterior caia por um unico criterio: o ativo desaparecer da
        carteira. Isso confunde duas coisas muito diferentes -- "a saida saiu" e
        "a saida foi tentada". Consequencia medida: com a ordem de protecao
        recusada pela exchange (IP fora da whitelist, valor abaixo do minimo,
        credencial sem permissao de spot), a posicao continuava na carteira, a
        trava continuava armada e **nenhum outro stop era tentado para aquele
        ativo enquanto o processo vivesse** -- com um `risk.protective_exit` no
        log dizendo que a protecao agiu.

        Os desfechos e os tratamentos:

        - **terminou e a posicao encolheu** (preenchida, em parte, ou cancelada
          DEPOIS de preencher parte): o restante e posicao aberta e merece
          protecao propria. Solta a trava. A quantidade decide ANTES do status:
          venda a mercado em livro fino que preenche 60% e expira vem como
          `CANCELED`, e lida pelo status contava como "nada vendido" -- aviso
          falso de "posicao continua aberta" e uma tentativa gasta (auditoria de
          09/11/2026, M1). Se o restante e poeira (abaixo do minimo negociavel),
          ele nao recebe stop: ver `_poeira`.
        - **preenchida e a posicao NAO encolheu**: ou o retrato ainda nao mostra
          a venda, ou a posicao voltou a crescer (compra entre o preenchimento e
          o retrato). Solta quando um retrato montado depois do desfecho chega
          (`_retrato_mostra`); antes disso, soltar venderia duas vezes.
        - **recusada / falhou / cancelada sem nada preenchido**: nada foi
          vendido. Solta a trava e avisa, porque o proximo snapshot tem de
          tentar de novo.
        - **abandonada** (ver `_handle_failed_exit`): destrava quando a posicao
          cresce acima da quantidade tentada ou passa a valer acima do minimo da
          exchange. Antes ficava armada para sempre, e a posicao NOVA que a
          estrategia comprava no mesmo ativo nunca tinha stop (P0 #5).
        - **sem linha nenhuma em `orders`**: o pedido nao chegou a existir como
          ordem. Passado `SAIDA_SEM_REGISTRO`, vale o mesmo que recusada -- e
          precisa valer, porque este e o caminho mais comum de recusa que existe:
          o Execution Agent nega por filtro de mercado ANTES de gravar PENDING.
          Enquanto ele caia em "sem desfecho", a posicao ficava aberta, sem stop e
          travada para sempre, com um alerta por minuto dizendo que era anomalia.
        - **sem desfecho, com a ordem gravada**: nao solta. Revender por cima de
          uma ordem viva duplicaria a venda, e o estado seguro aqui nao e
          "arrisca menos", e "nao arrisca". Passado `SAIDA_SEM_DESFECHO`, avisa
          uma vez por transicao.
        """
        if not self._exiting:
            return

        quantidades = {p.asset: p.quantity for p in snapshot.positions}
        precos = {
            p.asset: p.current_price for p in snapshot.positions if p.current_price is not None
        }
        precos.update(mercado or {})
        agora = _aware(snapshot.timestamp)
        minimo = self._limits.min_order_notional

        async with session_scope(self._settings) as session:
            orders = OrderRepository(session)
            pendentes = {
                asset: (voo, await orders.find_by_client_id(voo.client_order_id))
                for asset, voo in self._exits_in_flight.items()
            }

        for asset, (voo, order) in pendentes.items():
            restante = quantidades.get(asset, Decimal(0))
            if restante <= 0:
                # Posicao liquidada: a saida cumpriu o que prometeu.
                self._liberar(asset)
                self._esquecer_falhas(asset)
                continue
            preco = precos.get(asset)
            valor = restante * preco if preco is not None else None

            if asset in self._exit_abandoned:
                # Ja desistimos deste ativo e ja avisamos; insistir com o MESMO
                # saldo seria uma ordem recusada e um alerta por ciclo. Mas o
                # saldo mudar e outra historia: posicao maior que a tentada e
                # compra nova, e poeira que passou a valer acima do minimo ficou
                # vendavel. Nos dois casos o stop volta a valer.
                cresceu = restante > voo.quantity
                ficou_vendavel = (
                    asset in self._abaixo_do_minimo_ao_desistir
                    and valor is not None
                    and valor >= minimo
                )
                if cresceu or ficou_vendavel:
                    await self._retomar_protecao(asset, voo, restante, valor)
                continue

            status = order.status if order is not None else None
            preenchido = (order.filled_quantity or Decimal(0)) if order is not None else Decimal(0)

            if status in ORDEM_TERMINADA and restante < voo.quantity:
                # Vendeu parte. O resto e uma posicao aberta como qualquer
                # outra, e precisa de nivel proprio -- salvo se for poeira.
                self._liberar(asset)
                self._esquecer_falhas(asset)
                self.log.warning(
                    "risk.protective_exit_partial",
                    asset=asset,
                    motivo=voo.reason,
                    status=status,
                    emitida=str(voo.quantity),
                    restante=str(restante),
                )
                if valor is not None and valor < minimo / 2:
                    # O caso concreto: o stop vende 0,001 BTC, a taxa da compra
                    # foi cobrada em BTC e a venda e truncada no LOT_SIZE -- sobra
                    # 0,00000988 BTC. Disparar stop nisso era uma venda recusada
                    # por minimo, tres tentativas e o abandono. O limiar e o mesmo
                    # que o motor usa para "poeira" (`RiskEngine`, metade do
                    # minimo de ordem), e nao o minimo inteiro: `min_order_notional`
                    # e configuravel acima do minimo da exchange, e tratar como
                    # poeira uma posicao vendavel a deixaria sem stop.
                    self._poeira[asset] = restante
                    self.log.warning(
                        "risk.protective_exit_dust",
                        asset=asset,
                        quantidade=str(restante),
                        valor=str(valor),
                        detail="resto abaixo do minimo negociavel; sem stop ate a posicao crescer",
                    )
                continue

            if status in (str(OrderStatus.FILLED), str(OrderStatus.PARTIALLY_FILLED)) or (
                status in SAIDA_SEM_VENDA and preenchido > 0
            ):
                # Vendeu, e a posicao nao encolheu. Ou o retrato ainda nao mostra
                # a venda (soltar agora venderia duas vezes), ou a posicao voltou
                # a crescer depois do desfecho -- compra manual ou da estrategia.
                # Antes este ramo so soltava com `restante < voo.quantity`, e o
                # segundo caso prendia a trava para sempre, sem aviso.
                if order is not None and self._retrato_mostra(order.updated_at):
                    self._liberar(asset)
                    self._esquecer_falhas(asset)
                    self.log.warning(
                        "risk.protective_exit_position_regrew",
                        asset=asset,
                        motivo=voo.reason,
                        emitida=str(voo.quantity),
                        restante=str(restante),
                    )
                continue

            if status in SAIDA_SEM_VENDA:
                await self._handle_failed_exit(asset, voo, status, order, valor)
                continue

            if status is None and agora - voo.emitted_at >= SAIDA_SEM_REGISTRO:
                # Nao e "sem desfecho": e SEM ORDEM. Nao existe linha em `orders`
                # para este pedido, e a causa concreta e o Execution Agent
                # recusando por filtro de mercado (valor abaixo do MIN_NOTIONAL)
                # antes de gravar PENDING -- registra em `audit_log` e nada mais.
                # Nada foi enviado a exchange, logo nao ha venda para duplicar, e
                # tratar isto como "ordem viva" prendia a posicao sem stop para
                # sempre, com um alerta por retrato dizendo que era anomalia.
                await self._handle_failed_exit(asset, voo, None, None, valor)
                continue

            if (
                agora - voo.emitted_at >= SAIDA_SEM_DESFECHO
                and asset not in self._saida_parada_avisada
            ):
                # Uma vez por transicao. A trava fica armada e o estado pode
                # durar horas: repetir a cada retrato seriam 1.440 linhas por dia.
                self._saida_parada_avisada.add(asset)
                self.log.error(
                    "risk.protective_exit_stalled",
                    asset=asset,
                    motivo=voo.reason,
                    status=status,
                    esperando_ha=str(agora - voo.emitted_at),
                    detail="trava mantida para nao vender duas vezes",
                )
                await self.bus.publish(
                    Topics.ALERTS,
                    {
                        "type": "protective_exit_stalled",
                        "title": f"Saida de protecao de {asset} sem desfecho",
                        "message": (
                            f"A ordem de {voo.reason} de {asset} foi emitida ha "
                            f"{agora - voo.emitted_at} e a exchange nao devolveu "
                            f"desfecho (ultimo estado conhecido '{status}'). O "
                            "sistema NAO reemite -- revender por cima de uma ordem "
                            "viva duplicaria a venda -- entao esta posicao pode "
                            "estar sem stop-loss agora."
                        ),
                    },
                )

    async def _handle_failed_exit(
        self,
        asset: str,
        voo: _SaidaEmVoo,
        status: str | None,
        order: object,
        valor: Decimal | None = None,
    ) -> None:
        """Decide entre tentar de novo e desistir dizendo que desistiu.

        As duas metades sao necessarias e nenhuma serve sozinha:

        - **Tentar de novo** cobre a recusa transitoria -- IP que trocou, rede,
          credencial que voltou. Sem isso a posicao ficava sem stop para sempre.
        - **Desistir depois de algumas tentativas** cobre a recusa PERMANENTE,
          que existe e e comum: poeira abaixo do MIN_NOTIONAL da exchange nao
          vende hoje nem nunca. Reemitir a cada retrato daria uma ordem recusada
          e um alerta por ciclo -- 1.440 por dia, a patologia do achado 1 do
          ensaio, no aviso que menos pode ser ignorado.

        Ao desistir a trava fica ARMADA, e cai quando alguem fechar a posicao a
        mao ou o saldo mudar de um jeito que muda a resposta: a posicao crescer
        acima da quantidade tentada (compra nova) ou, se ela valia menos que o
        minimo da exchange ao desistir, passar a valer acima dele. Ai a contagem
        zera e o stop volta a ser tentado (`_reconcile_exits`). Ate 09/11/2026
        esta frase era promessa: nada soltava a trava, e a recompra do mesmo
        ativo ficava sem stop enquanto o processo vivesse.

        `status is None` chega aqui pelo caminho da ordem que NAO tem linha em
        `orders` (ver `_reconcile_exits`), e conta como recusa pelo mesmo motivo
        das outras: nada foi vendido. O texto do aviso muda porque "terminou como
        'None'" nao serve para quem vai agir a mao.
        """
        falhas = self._exit_failures.get(asset, 0) + 1
        self._exit_failures[asset] = falhas
        erro = getattr(order, "error", None)
        # `status` nulo nao e desfecho desconhecido: e ausencia de ordem. Escrever
        # "terminou como 'None'" no aviso que menos pode ser ignorado nao diz nada
        # a quem vai agir a mao.
        desfecho = (
            f"terminou como '{status}'"
            if status is not None
            else "nao chegou a existir em `orders` (recusada antes de ser gravada)"
        )

        if falhas >= MAX_TENTATIVAS_DE_SAIDA:
            self._exit_abandoned.add(asset)
            if valor is not None and valor < self._limits.min_order_notional:
                self._abaixo_do_minimo_ao_desistir.add(asset)
            self.log.error(
                "risk.protective_exit_abandoned",
                asset=asset,
                motivo=voo.reason,
                status=status,
                tentativas=falhas,
                erro=erro,
                detail="posicao sem protecao possivel; precisa de acao manual",
            )
            await self.bus.publish(
                Topics.ALERTS,
                {
                    "type": "protective_exit_impossible",
                    "title": f"{asset} nao pode ser protegido: acao manual necessaria",
                    "message": (
                        f"A saida de {voo.reason} de {asset} foi recusada "
                        f"{falhas} vezes (ultima: {desfecho}"
                        + (f": {erro}" if erro else "")
                        + "). O caso tipico e valor abaixo do minimo da exchange, "
                        "que nao muda com o tempo. O sistema PAROU de tentar para "
                        "nao gerar uma ordem e um alerta por minuto: esta posicao "
                        "esta sem stop-loss ate alguem fecha-la a mao (ou ela "
                        "crescer com uma compra nova, quando o stop volta a valer)."
                    ),
                },
            )
            return

        self._liberar(asset)
        self.log.error(
            "risk.protective_exit_failed",
            asset=asset,
            motivo=voo.reason,
            status=status,
            tentativas=falhas,
            erro=erro,
            detail="posicao segue aberta; o stop sera tentado no proximo retrato",
        )
        if falhas > 1:
            # A primeira recusa avisa; as intermediarias ficam so no log, senao
            # uma unica falha transitoria renderia tres mensagens.
            return
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "protective_exit_failed",
                "title": f"Saida de protecao NAO executada em {asset}",
                "message": (
                    f"A ordem de {voo.reason} de {asset} {desfecho} e a posicao "
                    f"continua aberta. O sistema vai tentar de novo no proximo "
                    f"retrato do portfolio, no maximo "
                    f"{MAX_TENTATIVAS_DE_SAIDA} vezes."
                ),
            },
        )

    def _liberar(self, asset: str) -> None:
        """Solta a trava de saida do ativo, nas duas estruturas de uma vez.

        NAO zera a contagem de falhas: e ela que separa uma recusa transitoria,
        que merece nova tentativa, de uma permanente, que merece que o sistema
        pare e diga isso. Zerar aqui faria a contagem nunca chegar ao limite.
        """
        self._exiting.discard(asset)
        self._exits_in_flight.pop(asset, None)
        # A duvida sobre ESTA ordem terminou: se a proxima tambem ficar parada,
        # e um episodio novo e merece ser dito de novo.
        self._saida_parada_avisada.discard(asset)

    def _esquecer_falhas(self, asset: str) -> None:
        """A saida funcionou: a proxima recusa comeca a contagem do zero."""
        self._exit_abandoned.discard(asset)
        self._abaixo_do_minimo_ao_desistir.discard(asset)
        self._exit_failures.pop(asset, None)

    async def _retomar_protecao(
        self, asset: str, voo: _SaidaEmVoo, restante: Decimal, valor: Decimal | None
    ) -> None:
        """Destrava um ativo abandonado cujo saldo mudou, e diz isso uma vez.

        O aviso de abandono disse "sem stop-loss ate alguem fecha-la a mao"; a
        volta da protecao merece o mesmo canal, senao o operador continua
        achando que precisa agir.
        """
        self._liberar(asset)
        self._esquecer_falhas(asset)
        self.log.warning(
            "risk.protective_exit_resumed",
            asset=asset,
            tentada=str(voo.quantity),
            restante=str(restante),
            valor=str(valor) if valor is not None else None,
        )
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "protective_exit_resumed",
                "title": f"{asset} voltou a ter stop-loss",
                "message": (
                    f"A saida de {asset} tinha sido abandonada com {voo.quantity}; "
                    f"o saldo agora e {restante}"
                    + (f" (vale {valor:.2f})" if valor is not None else "")
                    + ". Stop-loss e take-profit voltaram a ser avaliados."
                ),
            },
        )

    async def _report_missing_cost_basis(self, sem_base: set[str]) -> None:
        """Avisa sobre posicao que o stop em software nao alcanca.

        Uma linha no log por snapshot seriam 1.440 por dia, e um aviso que chega
        sempre deixa de ser lido -- mesmo motivo do portao de capital. Sai uma
        vez por transicao, e a transicao inclui um ativo NOVO entrar na lista.
        """
        if sem_base == self._sem_base_de_custo:
            return
        novos = sorted(sem_base - self._sem_base_de_custo)
        self._sem_base_de_custo = set(sem_base)
        if not novos:
            return

        self.log.error(
            "risk.position_without_cost_basis",
            assets=novos,
            detail="sem preco medio nao existe nivel: estas posicoes nao tem stop",
        )
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "position_without_cost_basis",
                "title": f"{len(novos)} posicao(oes) sem stop-loss: {', '.join(novos)}",
                "message": (
                    "O stop em software compara o preco corrente com o preco medio "
                    "reconstruido do historico de trades. Sem historico na moeda de "
                    "cotacao atual nao existe preco medio, e estas posicoes ficam "
                    f"SEM protecao nenhuma: {', '.join(novos)}. Lance a compra "
                    "correspondente em Operacoes, ou feche a posicao a mao."
                ),
            },
        )

    async def _on_signal(self, signal: Signal) -> None:
        """Avalia um sinal sozinho, delegando ao caminho de lote.

        E um atalho de conveniencia, e **precisa** ser exatamente isso: o laco
        de producao passa por `_on_batch`, e enquanto este metodo tinha uma
        implementacao propria -- construir o estado, chamar `evaluate`, publicar
        -- existiam duas versoes da avaliacao no mesmo agente. Nenhum chamador de
        producao usava esta, e era ela que seis testes exercitavam, incluindo os
        dois que provam a assimetria do circuit breaker. A protecao estava
        demonstrada num caminho que o sistema nao percorre, que e o defeito que
        este projeto ja encontrou tres vezes de outras formas.
        """
        await self._on_batch([signal])

    def definir_precos_de_mercado(self, fonte: Callable[[], dict[str, Decimal]]) -> None:
        self._precos_de_mercado = fonte

    def definir_filtros_de_mercado(self, fonte: Callable[[str], MarketFilter | None]) -> None:
        self._filtro_de_mercado = fonte

    def _e_poeira_invendavel(self, position: Position) -> bool:
        """Sobra que a exchange recusaria vender: sem stop, sem tentativas, sem alerta.

        Com a taxa da compra cobrada no proprio ativo (Binance sem BNB) e a venda
        truncada ao passo do par, TODA venda do saldo inteiro deixa um resto menor
        que um passo -- a da estrategia tambem, nao so a de protecao. Sem isto, o
        resto chegava ao stop, a venda era recusada pelo filtro tres vezes e saia
        o alerta "acao manual necessaria" por centavos (auditoria de 09/11/2026).

        Poeira aqui e o criterio da propria exchange (truncado ao passo da zero,
        ou fica abaixo do MIN_NOTIONAL) E vale menos que metade do minimo de
        ordem do sistema -- o mesmo corte do `RiskEngine`. Uma posicao invendavel
        de valor relevante continua no caminho normal, que termina no aviso de
        posicao sem protecao: o operador precisa saber dela. Sem filtro conhecido
        (testes, broker sem catalogo) nada muda.
        """
        if self._filtro_de_mercado is None or position.current_price is None:
            return False
        quote = self._settings.trading.quote_currency
        market = self._filtro_de_mercado(f"{position.asset}/{quote}")
        if market is None:
            return False
        vendavel = market.truncate(position.quantity)
        if vendavel > 0 and vendavel * position.current_price >= market.min_cost:
            return False
        valor = position.quantity * position.current_price
        if valor >= self._limits.min_order_notional / 2:
            return False
        if self._poeira.get(position.asset) != position.quantity:
            self._poeira[position.asset] = position.quantity
            self.log.info(
                "risk.poeira_sem_stop",
                asset=position.asset,
                quantidade=str(position.quantity),
                valor=str(valor),
                detail="abaixo do passo ou do minimo da exchange; sem stop ate a posicao crescer",
            )
        return True

    def _no_preco_corrente(self, signal: Signal) -> Signal:
        preco = self._preco_de_dimensionamento(signal)
        if preco == signal.reference_price:
            return signal
        self.log.info(
            "risk.preco_corrente_no_lugar_do_fechamento",
            symbol=signal.symbol,
            fechamento=str(signal.reference_price),
            corrente=str(preco),
        )
        return signal.model_copy(update={"reference_price": preco})

    def _abertura_sem_preco_corrente(self, signal: Signal) -> bool:
        """LONG sem preco corrente recente, com a fonte de mercado ligada.

        Desligada (testes de unidade), vale o preco do sinal, como antes.
        """
        if signal.direction is not SignalDirection.LONG or self._precos_de_mercado is None:
            return False
        corrente = self._precos_de_mercado().get(signal.symbol.partition("/")[0])
        return corrente is None or corrente <= 0

    def _preco_de_dimensionamento(self, signal: Signal) -> Decimal:
        """O preco corrente do par; sem ele, o de referencia do sinal.

        O sinal carrega o fechamento do candle (com `1d`, o de ontem). A ordem
        executa no mercado de agora: dimensionar no fechamento deixava a compra
        passar do teto por ordem e de exposicao na mesma proporcao da distancia
        (medido pelo critico: 20,00 aprovados, 21,58 gastos, +7,9%).

        O recuo para o preco do sinal so chega a dimensionar FECHAMENTO: a
        abertura sem preco corrente e recusada antes (`_abertura_sem_preco_corrente`).
        """
        if self._precos_de_mercado is not None:
            corrente = self._precos_de_mercado().get(signal.symbol.partition("/")[0])
            if corrente is not None and corrente > 0:
                return corrente
        return signal.reference_price

    async def _build_state(
        self, signal: Signal, simbolos: set[str] | None = None
    ) -> PortfolioState:
        """Retrato + o que foi publicado e o retrato ainda nao mostra.

        `simbolos` sao os pares do lote inteiro: o cooldown e consultado para
        cada um. Com o lote ligado, so o par do primeiro sinal era consultado, e
        um BTC com ordem ha 5 minutos passava por estar em segundo lugar
        (auditoria de 09/11/2026, M2).
        """
        snapshot = self._snapshot
        assert snapshot is not None

        positions = {p.asset: p.quantity for p in snapshot.positions}
        prices = {
            p.asset: p.current_price for p in snapshot.positions if p.current_price is not None
        }
        base = signal.symbol.partition("/")[0]
        prices[base] = self._preco_de_dimensionamento(signal)

        last_order_at: dict[str, datetime] = {}
        async with session_scope(self._settings) as session:
            orders = OrderRepository(session)
            for simbolo in sorted({signal.symbol} | (simbolos or set())):
                ultima = await orders.last_order_time(simbolo)
                if ultima is not None:
                    last_order_at[simbolo] = ultima
            await self._conciliar_reservas(orders)

        # O que ja foi aprovado e publicado e o retrato ainda nao mostra: o mesmo
        # que `_apply_fill` faz dentro do lote, valendo entre lotes. Sem preco no
        # retrato (ativo novo), vale o da aprovacao -- sem ele a posicao reservada
        # valeria zero e nao contaria nem na exposicao nem nas vagas.
        cash = snapshot.cash_value
        em_voo: dict[str, Decimal] = {}
        for reserva in self._reservas.values():
            # Compra ou venda, a quantidade fica "em voo": nao pode ser vendida
            # (de novo, ou antes de existir na conta) ate o retrato mostra-la.
            em_voo[reserva.asset] = em_voo.get(reserva.asset, Decimal(0)) + reserva.quantity
            if not reserva.venda:
                cash -= reserva.notional
                positions[reserva.asset] = (
                    positions.get(reserva.asset, Decimal(0)) + reserva.quantity
                )
                prices.setdefault(reserva.asset, reserva.price)
            anterior = last_order_at.get(reserva.symbol)
            if anterior is None or _aware(anterior) < reserva.emitted_at:
                last_order_at[reserva.symbol] = reserva.emitted_at
        for asset, voo in self._exits_in_flight.items():
            # A saida de protecao em voo vende a posicao inteira: um FLAT da
            # estrategia por cima dela era a segunda venda do mesmo saldo. O
            # abandonado nao conta -- nada esta em voo, so a trava ficou armada.
            if asset not in self._exit_abandoned:
                em_voo[asset] = em_voo.get(asset, Decimal(0)) + voo.quantity

        # Leitura de regime. Falha ou ausencia => None => filtro nao se aplica.
        mvrv_percentile = None
        if self._onchain is not None and self._limits.mvrv_max_percentile < 1.0:
            try:
                leitura = await self._onchain.mvrv_reading()
                mvrv_percentile = leitura.percentile if leitura else None
                if leitura is None:
                    self.log.warning("risk.mvrv_unavailable", detail="filtro nao aplicado")
            except Exception as exc:
                self.log.error("risk.mvrv_read_failed", error=str(exc))

        return PortfolioState(
            total_value=snapshot.total_value,
            cash=cash,
            positions=positions,
            prices=prices,
            last_order_at=last_order_at,
            circuit_breaker_active=self._circuit_breaker_active,
            circuit_breaker_reason=self._circuit_breaker_reason,
            mvrv_percentile=mvrv_percentile,
            em_voo=em_voo,
        )

    async def _conciliar_reservas(self, orders: OrderRepository) -> None:
        """Solta a reserva da ordem que o retrato ja mostra ou que nao moveu nada.

        Pelo banco, e nao pelo topico de resultados, pelo mesmo motivo de
        `_reconcile_exits`: a linha em `orders` e gravada pelo Execution antes de
        o resultado ser publicado, nao depende de uma assinatura viva neste agente
        e cobre a recusa que nunca chega a ser ordem.

        - **terminou sem preencher nada**: nada foi gasto, a reserva cai na hora;
        - **terminou com algo preenchido**: cai quando um retrato montado depois
          do desfecho chegar (`_retrato_mostra`); antes disso o saldo lido pode
          ser o de antes da compra;
        - **sem linha em `orders`** depois de `SAIDA_SEM_REGISTRO`: recusada por
          filtro antes de existir, nada foi enviado;
        - **sem desfecho** (PENDING/OPEN): continua reservada. Desfecho
          desconhecido pode ter gasto o dinheiro, e liberar no escuro e o furo
          que esta reserva fecha.
        """
        if not self._reservas:
            return
        agora = datetime.now(UTC)
        for client_order_id, reserva in list(self._reservas.items()):
            order = await orders.find_by_client_id(client_order_id)
            if order is None:
                if agora - reserva.emitted_at >= SAIDA_SEM_REGISTRO:
                    del self._reservas[client_order_id]
                continue
            if order.status not in ORDEM_TERMINADA:
                continue
            preenchido = order.filled_quantity or Decimal(0)
            if preenchido <= 0 or self._retrato_mostra(order.updated_at):
                del self._reservas[client_order_id]

    async def _persist_and_publish(self, assessment: RiskAssessment, signal: Signal) -> None:
        # Persistir primeiro: um `OrderRequest` nunca pode referenciar um
        # risk_event que nao existe no banco.
        async with session_scope(self._settings) as session:
            await RiskEventRepository(session).save_assessment(assessment)

        await self.bus.publish(Topics.RISK_ASSESSMENTS, assessment)

        if assessment.decision is RiskDecision.REJECTED:
            self.log.info(
                "risk.rejected",
                symbol=signal.symbol,
                strategy=signal.strategy,
                reasons=assessment.reasons,
            )
            return

        abertura = signal.direction is SignalDirection.LONG
        quantidade = assessment.approved_quantity or Decimal(0)
        # Ultima barreira antes de a ordem sair, e ela vale pelo que o resto do
        # sistema PROMETE, nao pelo que o motor faz hoje: toda abertura carrega
        # stop-loss e take-profit, e nenhuma ordem sai com quantidade zero. Se um
        # caminho futuro produzir uma aprovacao incompleta, ela morre aqui em vez
        # de virar posicao desprotegida -- e com registro, porque uma ordem que
        # deixou de existir em silencio e indistinguivel de uma que nunca houve.
        faltando: list[str] = []
        if quantidade <= 0:
            faltando.append("quantidade aprovada nao positiva")
        if (assessment.approved_notional or Decimal(0)) <= 0:
            # O `OrderRequest` exige notional > 0: sem esta linha a aprovacao
            # levantava `ValidationError` dentro do laco de `_on_batch`, e os
            # sinais seguintes do lote nao eram gravados nem publicados (L7).
            faltando.append("notional aprovado nao positivo")
        if abertura and assessment.stop_loss is None:
            faltando.append("abertura sem stop-loss")
        if abertura and assessment.take_profit is None:
            faltando.append("abertura sem take-profit")
        if faltando:
            self.log.error(
                "risk.incomplete_approval_blocked",
                symbol=signal.symbol,
                risk_event_id=assessment.id,
                faltando=faltando,
            )
            async with session_scope(self._settings) as session:
                await AuditLogRepository(session).append(
                    action="incomplete_approval_blocked",
                    actor="risk_manager",
                    target=signal.symbol,
                    detail="; ".join(faltando),
                    before={"risk_event_id": assessment.id},
                )
            return

        request = OrderRequest(
            client_order_id=_client_order_id(),
            signal_id=signal.id,
            risk_event_id=assessment.id,
            exchange=signal.exchange,
            symbol=signal.symbol,
            side=Side.BUY if abertura else Side.SELL,
            order_type=OrderType.MARKET,
            quantity=quantidade,
            notional=assessment.approved_notional or Decimal(0),
            stop_loss=assessment.stop_loss,
            take_profit=assessment.take_profit,
            strategy=signal.strategy,
        )
        self.log.info(
            "risk.approved",
            symbol=request.symbol,
            side=str(request.side),
            quantity=str(request.quantity),
            notional=str(request.notional),
            stop_loss=str(request.stop_loss) if request.stop_loss else None,
        )
        # Reservada ANTES de publicar: o proximo sinal pode ser avaliado enquanto
        # o Execution ainda nem gravou a ordem. A compra reserva caixa e vaga; a
        # venda reserva so a quantidade (nao credita caixa nem libera vaga ate o
        # retrato mostrar o dinheiro -- o lado conservador: atrasa uma abertura,
        # nunca a financia com uma venda que pode ser recusada). Sem a reserva da
        # venda, dois FLAT do mesmo ativo antes do retrato seguinte eram duas
        # vendas do mesmo saldo.
        self._reservas[request.client_order_id] = _Reserva(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            asset=request.symbol.partition("/")[0],
            quantity=request.quantity,
            notional=request.notional,
            price=signal.reference_price,
            emitted_at=datetime.now(UTC),
            venda=not abertura,
        )
        await self.bus.publish(Topics.ORDER_REQUESTS, request)

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------
    async def check_circuit_breaker(self, snapshot: PortfolioSnapshot) -> str | None:
        """Dispara a trava quando a NEGOCIACAO perdeu demais no dia ou na semana.

        Retorna o motivo se a trava disparou agora, `None` caso contrario.
        Uma vez disparada, permanece ativa ate rearme **manual**: se o sistema
        perdeu dinheiro rapido o bastante para chegar aqui, a causa precisa ser
        entendida por uma pessoa antes de voltar a operar.

        **Mede resultado de negociacao, nao patrimonio bruto**, e a diferenca nao
        e teorica. Comparando patrimonio, um SAQUE da conta e indistinguivel de
        uma perda catastrofica: tirar R$100 de uma conta de R$150 dispara "perda
        diaria de 67%" e pausa tudo, culpando um prejuizo que nao existiu. Foi o
        que aconteceu no primeiro ensaio em dry_run, quando mudar o saldo
        simulado de 1000 para 150 disparou "perda de 85%" em tres minutos.

        `realized_pnl` (acumulado) + `unrealized_pnl` nao se mexem com deposito
        nem com saque -- apenas com o resultado das operacoes. E a base do
        percentual e o **capital autorizado**, nao o saldo total: com o portao em
        R$150 sobre uma conta de R$650, medir contra 650 tornaria a trava quatro
        vezes mais frouxa do que o configurado.

        **A queda precisa ser confirmada pelas DUAS medidas.** Medir so o
        resultado de negociacao troca um falso disparo por outros, porque esse
        numero e reconstruido de um historico mutavel e de uma marcacao a
        mercado, e ambos ficam momentaneamente ou retroativamente inconsistentes.
        Dois casos medidos, os dois com a configuracao de producao (29,29 USDC
        autorizados) e os dois disparando "perda diaria de 10,24%":

        - **ordem em voo.** A venda preencheu na exchange -- o BTC saiu, o caixa
          entrou -- e a linha em `trades` ainda nao foi gravada. Na janela, o
          nao realizado cai a zero e o realizado ainda nao subiu.
        - **lancamento manual apagado.** `DELETE /trades/{id}` esta na
          interface; apagar uma venda lucrativa digitada errada derruba o
          realizado acumulado no meio do dia.

        Nos dois, e tambem na realizacao de lucro, o **patrimonio nao se move**:
        e artefato de contabilidade, nao prejuizo. Num prejuizo de verdade as
        duas medidas caem juntas. Entao a perda considerada e o MENOR entre a
        queda do resultado de negociacao e a queda do patrimonio -- a parte que
        as duas confirmam. Isto nao volta a medir patrimonio bruto (a imunidade a
        saque continua vindo do resultado de negociacao, que um saque nao move):
        o patrimonio entra apenas como testemunha.

        **A testemunha nao depoe sobre dinheiro que entrou.** A primeira versao
        desta confirmacao usava `min(queda_do_resultado, queda_do_patrimonio)` e
        nada mais, e isso abriu o buraco simetrico ao que ela fechava: um
        **deposito** no mesmo periodo levanta o patrimonio, a "queda de
        patrimonio" vira negativa, e a trava nao dispara nem com o capital
        autorizado inteiro perdido em negociacao -- medido: 50 perdidos sobre 100
        autorizados, dez vezes o limite diario, em silencio, porque o dono
        aportou 200 no mesmo dia. Um saque nao podia ser lido como prejuizo, e um
        aporte nao pode apagar um prejuizo: nenhum fluxo externo, de entrada ou
        de saida, tem direito de mexer nesta medicao.

        A correcao vem da aritmetica do patrimonio, que e exata:
        `patrimonio = capital externo + realizado + nao realizado`. Se o
        resultado de negociacao CAIU e o patrimonio SUBIU, nao existe operacao
        que explique as duas coisas -- entrou dinheiro de fora. Nesse caso o
        patrimonio nao e testemunha de nada e a medicao vale pelo resultado de
        negociacao, que e imune a fluxo por construcao. O artefato de
        contabilidade, ao contrario, NAO move dinheiro: nos quatro casos medidos
        o patrimonio fica parado, e ali a testemunha continua valendo.

        A margem entre "parado" e "subiu" e o ruido de marcacao, e ele tem teto:
        alta de patrimonio sem dinheiro novo so pode vir da marcacao das posicoes
        ABERTAS. Sem posicao aberta -- carteira toda em caixa, como no ataque do
        deposito -- nao existe ruido possivel, e qualquer centavo de alta e
        dinheiro de fora. Por isso a tolerancia e
        `min(min_order_notional, positions_value)`, e nao um numero fixo: e a
        mesma pergunta que o portao de capital faz ("deposito ou oscilacao de
        preco?"), respondida com o que limita a resposta.
        """
        if self._circuit_breaker_active:
            return None

        now = snapshot.timestamp
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())

        async with session_scope(self._settings) as session:
            repository = PortfolioSnapshotRepository(session)
            day_reference = await repository.first_result_since(day_start)
            week_reference = await repository.first_result_since(week_start)
            # Do MESMO retrato de referencia: as duas consultas ordenam pelo
            # timestamp e pegam o primeiro do periodo. O patrimonio e a
            # testemunha da queda do resultado de negociacao.
            day_equity = await repository.first_value_since(day_start)
            week_equity = await repository.first_value_since(week_start)

        atual = snapshot.realized_pnl + snapshot.unrealized_pnl
        base = self._capital_em_risco(snapshot)
        if base <= 0:
            # Sem base nao existe percentual, entao a trava nao pode disparar.
            # Isso e correto quando nao ha capital autorizado (nada a proteger),
            # e e um estado que precisa aparecer quando NAO e esse o caso: um
            # portfolio sem preco apurado tem `total_value` zero e desarma a
            # trava em silencio, que e a forma exata como este projeto ja perdeu
            # protecao tres vezes.
            self.log.warning(
                "risk.circuit_breaker_without_base",
                patrimonio=str(snapshot.total_value),
                autorizado=(
                    str(self._limits.authorized_capital)
                    if self._limits.authorized_capital is not None
                    else None
                ),
                detail="sem capital em risco apurado, a trava nao tem base para medir",
            )
            return None

        for label, reference, equity_reference, limit in (
            ("diaria", day_reference, day_equity, self._limits.daily_loss_limit_pct),
            ("semanal", week_reference, week_equity, self._limits.weekly_loss_limit_pct),
        ):
            if reference is None:
                continue
            perda_negociacao = reference - atual
            if perda_negociacao <= 0:
                # Nao ha queda nenhuma: nada divergente pendente de aviso, e o
                # proximo episodio volta a poder falar.
                self._loss_unconfirmed_alerted.discard(label)
                continue

            # A testemunha. Sem retrato de patrimonio a queda nao esta
            # confirmada, e nao confirmada nao dispara.
            queda_patrimonio = (
                equity_reference - snapshot.total_value
                if equity_reference is not None
                else Decimal(0)
            )
            # Ruido de marcacao: alta de patrimonio sem dinheiro novo so pode vir
            # do preco das posicoes ABERTAS, entao ela nao passa do valor delas.
            # Carteira toda em caixa => teto zero => qualquer alta e fluxo externo.
            ruido_de_marcacao = min(
                self._limits.min_order_notional, max(Decimal(0), snapshot.positions_value)
            )
            entrada_externa = -queda_patrimonio

            if entrada_externa > ruido_de_marcacao:
                # O patrimonio SUBIU enquanto o resultado de negociacao CAIU:
                # entrou dinheiro de fora. Uma testemunha que viu o deposito nao
                # pode depor sobre a perda -- e deixa-la depor era o que engolia a
                # perda total do capital autorizado.
                perda = perda_negociacao
                testemunha = (
                    f"patrimonio SUBIU {entrada_externa:.2f} no periodo "
                    f"(entrada de dinheiro externo, nao lucro), entao a medicao "
                    f"vale pelo resultado de negociacao"
                )
                self._loss_unconfirmed_alerted.discard(label)
            else:
                perda = min(perda_negociacao, queda_patrimonio)
                if perda <= 0:
                    self._report_unconfirmed_loss(
                        label, perda_negociacao, queda_patrimonio, base, limit
                    )
                    continue
                testemunha = (
                    f"patrimonio {equity_reference:.2f} -> {snapshot.total_value:.2f}"
                    if equity_reference is not None
                    else "sem retrato de patrimonio no periodo"
                )

            fracao = perda / base
            if fracao >= Decimal(str(limit)):
                reason = (
                    f"perda {label} de {fracao:.2%} do capital (limite {limit:.2%}); "
                    f"resultado de negociacao {reference:.2f} -> {atual:.2f} "
                    f"e {testemunha} "
                    f"sobre {base:.2f} {self._settings.trading.quote_currency}"
                )
                await self._trip(reason)
                return reason
            self._loss_unconfirmed_alerted.discard(label)
        return None

    def _report_unconfirmed_loss(
        self,
        label: str,
        perda_negociacao: Decimal,
        queda_patrimonio: Decimal,
        base: Decimal,
        limit: float,
    ) -> None:
        """Registra a divergencia entre as duas medidas de perda.

        So interessa quando a queda do resultado de negociacao SOZINHA teria
        disparado a trava: e ali que a confirmacao pelo patrimonio muda o
        desfecho, e e ali que um erro de contabilidade fica visivel em vez de
        virar uma pausa inexplicada. Sai uma vez por transicao, porque o estado
        pode durar o dia inteiro e um aviso a cada 60s deixa de ser lido.
        """
        if perda_negociacao / base < Decimal(str(limit)):
            self._loss_unconfirmed_alerted.discard(label)
            return
        if label in self._loss_unconfirmed_alerted:
            return
        self._loss_unconfirmed_alerted.add(label)
        self.log.warning(
            "risk.loss_not_confirmed_by_equity",
            periodo=label,
            perda_negociacao=str(perda_negociacao),
            queda_patrimonio=str(queda_patrimonio),
            limite=limit,
            detail=(
                "o resultado de negociacao caiu o bastante para a trava, mas o "
                "patrimonio nao caiu: artefato de contabilidade, nao prejuizo"
            ),
        )

    def _capital_em_risco(self, snapshot: PortfolioSnapshot) -> Decimal:
        """Base do percentual da trava: o que o sistema pode comprometer."""
        autorizado = self._limits.authorized_capital
        if autorizado is None:
            return snapshot.total_value
        return min(snapshot.total_value, autorizado)

    async def _trip(self, reason: str) -> None:
        self._circuit_breaker_active = True
        self._circuit_breaker_reason = reason
        async with session_scope(self._settings) as session:
            await RiskConfigRepository(session).trip_circuit_breaker(reason)
            await RiskEventRepository(session).save_event(
                RiskEventType.CIRCUIT_BREAKER_TRIPPED, [reason]
            )
            await AuditLogRepository(session).append(
                action="circuit_breaker_tripped", target="risk_config", detail=reason
            )
        self.log.error("risk.circuit_breaker_tripped", reason=reason)
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "circuit_breaker",
                "title": "Circuit breaker acionado",
                "message": (
                    f"{reason}\n\nOs agentes de decisao foram pausados. O rearme e "
                    "manual, pela interface."
                ),
                "reason": reason,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def reset_circuit_breaker(self, actor: str = "user") -> None:
        """Rearme manual, sempre auditado."""
        previous = self._circuit_breaker_reason
        self._circuit_breaker_active = False
        self._circuit_breaker_reason = None
        async with session_scope(self._settings) as session:
            await RiskConfigRepository(session).reset_circuit_breaker()
            await RiskEventRepository(session).save_event(
                RiskEventType.CIRCUIT_BREAKER_RESET, [previous or "sem motivo registrado"]
            )
            await AuditLogRepository(session).append(
                action="circuit_breaker_reset",
                actor=actor,
                target="risk_config",
                before={"reason": previous},
            )
        self.log.warning("risk.circuit_breaker_reset", actor=actor)

    # ------------------------------------------------------------------
    async def update_limits(self, values: dict, actor: str = "user") -> RiskSettings:
        """Aplica novos limites vindos da interface, com trilha de auditoria.

        Tudo sob a trava, e a trava nao e zelo: isto e ler-alterar-gravar com
        `await` no meio. Sem serializar, dois PUT simultaneos leem o MESMO estado
        inicial e o segundo a gravar apaga a alteracao do primeiro -- que recebeu
        200 e uma resposta AFIRMANDO o valor novo. Apertar um teto de risco e
        receber a confirmacao de um aperto que nao ficou de pe e a pior forma de
        perder uma alteracao, porque nada no sistema fica em desacordo: o banco,
        a memoria e a resposta contam historias coerentes e uma delas e falsa.
        """
        async with self._limits_lock:
            # A leitura do estado ATUAL mora dentro da trava: reler aqui e o que
            # faz a segunda alteracao ser aplicada em cima da primeira, em vez
            # de em cima do que existia antes das duas.
            anteriores = self._limits
            merged = anteriores.model_dump(mode="json") | values
            updated = RiskSettings.model_validate(merged)  # falha alto se incoerente

            async with session_scope(self._settings) as session:
                await RiskConfigRepository(session).update_values(
                    updated.model_dump(mode="json")
                )
                await AuditLogRepository(session).append(
                    action="risk_limits_updated",
                    actor=actor,
                    target="risk_config",
                    before=anteriores.model_dump(mode="json"),
                    after=updated.model_dump(mode="json"),
                )

            self._limits = updated
            self._rebuild_engine()

            # Mantem `settings.risk` em sincronia: o `check`, o backtest e a
            # leitura de regime consultam de la. Sem isto haveria duas versoes
            # dos limites no mesmo processo -- a divergencia que a separacao
            # ambiente/negocio existe para eliminar, reintroduzida por dentro.
            self._settings.with_business_config(self._settings.trading, updated)

            self.log.warning("risk.limits_updated", actor=actor, changed=sorted(values))
            return self.limits


def _aware(moment: datetime) -> datetime:
    """Normaliza para UTC. O SQLite devolve datetime sem fuso, e subtrair um
    ingenuo de um consciente levanta TypeError -- no meio da reconciliacao da
    saida de protecao, que e o pior lugar possivel para uma excecao."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _client_order_id() -> str:
    """ID de idempotencia.

    Prefixo curto + UUID: a Binance limita o tamanho do `clientOrderId`, e o
    prefixo ajuda a reconhecer ordens deste sistema no extrato da exchange.
    """
    return f"cat-{uuid.uuid4().hex[:20]}"
