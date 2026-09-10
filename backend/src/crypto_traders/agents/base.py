"""Infraestrutura comum dos agentes.

Cuida do ciclo de vida (start/pause/resume/stop/restart), heartbeat e tratamento
de erro, para que cada agente concreto contenha apenas a sua logica de negocio.

Tres coisas aqui existem por causa de defeitos medidos, e nao por gosto de
arquitetura:

* **A batida de ocioso.** Um agente orientado a evento so batia heartbeat quando
  recebia trabalho. Com candle diario ele fica legitimamente parado por horas, e
  o watchdog o declarou travado e o reiniciou 17 vezes em 16 minutos (D25). O
  sinal correto de vitalidade e "estou vivo esperando", nao "recebi trabalho".
* **A caixa de entrada duravel.** O reinicio era `stop()` + `start()`, e entre os
  dois o agente nao estava assinando o topico. Candle e publicado uma unica vez;
  o que caia nessa janela sumia sem uma linha no log.
* **A vitalidade e por TAREFA, nao por agente.** A primeira versao da batida de
  ocioso guardava um unico "estou esperando" por agente. O Risk Manager tem duas
  tarefas -- uma que alimenta a fila de sinais e outra que avalia os lotes --, e
  quem marcava a espera era a que alimenta. Com o laco de avaliacao pendurado
  para sempre, o alimentador seguia ocioso, o agente seguia batendo e o watchdog
  via verde: o guardiao pelo qual toda ordem passa podia travar indefinidamente.
  Era o defeito de D25 espelhado -- antes o ocioso parecia travado, depois o
  travado parecia ocioso. Agora cada tarefa vigiada reivindica a propria espera e
  o agente so bate como ocioso quando **todas** estao esperando.

Por isso tarefa interna de agente se cria com `self.spawn()`, nunca com
`asyncio.create_task`: o que nao e vigiado nao conta para a vitalidade, e uma
tarefa fora do registro e exatamente o ponto cego descrito acima. Existe teste
que varre o pacote `agents` cobrando essa regra.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from typing import Any

from ..bus import EventBus, Topics
from ..domain.enums import AgentState
from ..domain.models import Heartbeat
from ..logging_setup import get_logger

#: De quanto em quanto tempo um agente que esta apenas ESPERANDO diz que esta
#: vivo. Tem que ficar confortavelmente abaixo do `HEARTBEAT_TIMEOUT` do
#: orquestrador, senao a batida de vitalidade chega depois do diagnostico.
#:
#: E infraestrutura, nao negocio (D15): nao vem do `.env` nem do banco. Quem
#: mexe nisto esta mexendo em vigilancia de processo, nao em como se opera.
IDLE_PULSE_SECONDS = 60.0

#: Topicos cujo evento e publicado UMA UNICA VEZ -- perder um e perder trabalho
#: para sempre. Candle so e publicado quando esta fechado e inedito; sinal e
#: pedido de ordem existem uma vez cada. Sao exatamente estes que ganham caixa
#: de entrada duravel.
#:
#: Ficam de fora os topicos de estado repetido (preco, snapshot, heartbeat,
#: alerta): neles a proxima publicacao substitui a anterior, e perder uma nao
#: apaga informacao.
ONCE_ONLY_TOPICS = frozenset({Topics.CANDLES, Topics.SIGNALS, Topics.ORDER_REQUESTS})

#: Teto da caixa de entrada, igual ao do bus in-process.
#:
#: Uma fila sem teto guardando eventos para um consumidor que nunca volta cresce
#: sem limite. Com teto, a transferencia do bus para a caixa para de avancar e a
#: pressao volta para o bus, que ja descarta com aviso (`event_bus.queue_full`).
#: Perder com aviso e melhor que crescer em silencio.
INBOX_MAX_SIZE = 1000

log = get_logger(__name__)


def describe_event(event: Any) -> str:
    """Identifica um evento no log sem despejar o objeto inteiro."""
    symbol = getattr(event, "symbol", None)
    when = getattr(event, "open_time", None) or getattr(event, "timestamp", None)
    if symbol and when:
        return f"{type(event).__name__} {symbol} {when}"
    if symbol:
        return f"{type(event).__name__} {symbol}"
    return type(event).__name__


class _Inbox:
    """Assinatura de topico que pertence ao AGENTE, nao a tarefa que a consome.

    A assinatura e a fila vivem aqui, fora do ciclo de vida da tarefa de
    processamento. Trocar a tarefa (`BaseAgent.restart()`) nao desassina nada: o
    que e publicado durante a troca fica na fila e e processado quando o laco
    volta. Era esta a janela que descartava candle em silencio.
    """

    def __init__(self, agent: BaseAgent, bus: EventBus, topic: str) -> None:
        self._agent = agent
        self._bus = bus
        self._topic = topic
        self._stream: AsyncIterator[Any] | None = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=INBOX_MAX_SIZE)
        self._feeder: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()
        """Sinaliza que a assinatura no bus ja esta registrada."""

        self.received = 0
        self.handled = 0
        self.broken: str | None = None
        """Motivo da ultima queda da assinatura. Enquanto preenchido, o agente
        NAO esta ouvindo este topico -- e isso nunca pode ser silencioso."""

        self.stream_failures = 0
        self.in_flight: Any = None
        """Evento entregue ao laco e ainda nao concluido.

        Se a tarefa morrer com algo aqui, esse evento esta perdido de verdade --
        o processamento parou no meio. Reentrega-lo poderia duplicar efeito, mas
        ele nao pode desaparecer calado.
        """

    @property
    def pending(self) -> int:
        """Eventos esperando na fila. Cresce enquanto a tarefa esta trocando."""
        return self._queue.qsize()

    @property
    def alive(self) -> bool:
        """True enquanto esta caixa esta de fato recebendo do bus.

        Nao basta perguntar se o stream estourou: um alimentador que termina --
        cancelado, ou porque o gerador do bus acabou -- deixa o agente igualmente
        surdo, e sem `broken` preenchido ele pareceria saudavel. O que importa e
        se alguem ainda esta transferindo do bus para a fila.
        """
        return self.broken is None and self._feeder is not None and not self._feeder.done()

    def ensure_feeding(self) -> None:
        """Garante a tarefa que transfere do bus para a fila do agente.

        Reassina o topico quando o alimentador anterior nao existe mais -- caso
        do stream que morreu com excecao. A assinatura antiga ja se desfez junto
        com o gerador, entao insistir nela deixaria o agente de pe e surdo.
        """
        if self._feeder is not None and not self._feeder.done():
            return
        self._stream = self._bus.subscribe(self._topic)
        self.broken = None
        self.ready.clear()
        self._feeder = self._agent.spawn(
            self._feed(), name=f"inbox-{self._topic}", vigiada=True
        )

    async def _feed(self) -> None:
        # `ready` e marcado ANTES do primeiro `anext`: o gerador do bus registra
        # a assinatura antes da sua primeira suspensao, entao quem esperava por
        # este evento so volta a rodar com a assinatura ja no lugar. E o que
        # torna "os consumidores antes dos produtores" verificavel em vez de
        # depender de um `sleep(0)` bem colocado.
        self.ready.set()
        assert self._stream is not None
        iterator = self._stream.__aiter__()
        while True:
            # A espera pelo proximo evento do bus e espera legitima, e esta
            # tarefa precisa dizer isso: o agente so bate como ocioso quando
            # TODAS as suas tarefas vigiadas estao esperando.
            self._agent.enter_idle(f"aguardando {self._topic}")
            try:
                event = await iterator.__anext__()
            except StopAsyncIteration:
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # O `xread` do Redis levanta aqui quando perde a conexao
                # (`bus/redis_streams.py`). Antes da caixa de entrada a excecao
                # subia pelo `async for` do `_run` e virava state=ERROR; deixar
                # ela morrer nesta tarefa trocaria uma falha detectada por um
                # agente surdo, verde e batendo -- a familia do stop-loss que
                # nao disparava.
                self.broken = str(exc)
                self.stream_failures += 1
                self.ready.clear()
                await self._agent.report_inbox_failure(self._topic, exc, self.pending)
                return
            finally:
                self._agent.leave_idle()
            self.received += 1
            await self._queue.put(event)

    async def stream(self) -> AsyncIterator[Any]:
        """Fluxo consumido pelo laco do agente.

        A espera pela fila e o que marca o agente como ocioso -- e ocioso e o
        estado que a batida de vitalidade cobre.
        """
        while True:
            self._agent.enter_idle(f"aguardando {self._topic}")
            try:
                event = await self._queue.get()
            finally:
                # Tambem no cancelamento: um agente morto nao pode continuar
                # marcado como "esperando", ou passaria a bater como se vivo.
                self._agent.leave_idle()
            self.in_flight = event
            yield event
            self.in_flight = None
            self.handled += 1

    async def close(self) -> None:
        """Desfaz a assinatura. So no `stop()`, nunca no reinicio.

        Engole QUALQUER excecao do alimentador, e nao apenas o cancelamento: um
        alimentador que morreu com `ConnectionResetError` relancava a excecao
        aqui, `_close_inboxes` abortava no meio e `stop()` nunca chegava a
        `STOPPED` nem a `on_stop()` -- broker e sessao HTTP ficavam abertos e as
        outras caixas vazavam a assinatura. Como o orquestrador envolve cada
        `agent.stop()` num `suppress(Exception)`, o desligamento ainda se
        declarava limpo.
        """
        if self._feeder is not None:
            self._feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._feeder
            self._feeder = None
        if self._stream is not None:
            with contextlib.suppress(Exception):
                await self._stream.aclose()  # type: ignore[attr-defined]
            self._stream = None
        self.ready.clear()


class _AgentBus(EventBus):
    """O bus visto de dentro de um agente.

    Repassa tudo para o bus real, com uma diferenca: assinar um topico de evento
    unico devolve a caixa de entrada duravel do agente em vez de uma assinatura
    presa a tarefa que esta iterando. Os agentes concretos nao precisam saber
    disso -- continuam escrevendo `async for x in self.bus.subscribe(T)`.
    """

    def __init__(self, bus: EventBus, agent: BaseAgent) -> None:
        self._bus = bus
        self._agent = agent

    @property
    def raw(self) -> EventBus:
        """O bus de verdade, para quem precisa publicar sem passar pela caixa."""
        return self._bus

    async def start(self) -> None:
        await self._bus.start()

    async def stop(self) -> None:
        await self._bus.stop()

    async def publish(self, topic: str, payload: Any) -> None:
        await self._bus.publish(topic, payload)

    def subscribe(self, topic: str) -> AsyncIterator[Any]:
        if topic not in ONCE_ONLY_TOPICS:
            return self._bus.subscribe(topic)
        return self._agent.inbox(topic).stream()

    def __getattr__(self, item: str) -> Any:
        # Repassa o resto do backend (por exemplo `subscriber_count`). O corte
        # no underscore evita recursao antes de `_bus` existir.
        if item.startswith("_"):
            raise AttributeError(item)
        return getattr(self._bus, item)


class BaseAgent(abc.ABC):
    """Agente com ciclo de vida gerenciado.

    Subclasses implementam `_run()`. O `pause()` e cooperativo: o agente termina
    o ciclo corrente e so entao para. Interromper um agente no meio de um ciclo
    poderia deixar uma ordem enviada e nao registrada.
    """

    name: str

    def __init__(self, bus: EventBus) -> None:
        self.bus: EventBus = _AgentBus(bus, self)
        self.log = get_logger(f"agent.{self.name}")
        self.state = AgentState.STOPPED
        self.last_error: str | None = None
        self.last_beat: datetime | None = None
        self.started_at: datetime | None = None
        """Subida da tarefa corrente. Referencia de vitalidade antes da 1a batida."""

        self.restarts = 0
        self.lost_events = 0
        """Eventos interrompidos no meio do processamento por um reinicio."""

        self.inbox_failures = 0
        """Quantas vezes uma assinatura deste agente caiu sob os pes dele."""

        self._task: asyncio.Task[None] | None = None
        self._pulse: asyncio.Task[None] | None = None
        self._inboxes: dict[str, _Inbox] = {}
        self._vigiadas: set[asyncio.Task[Any]] = set()
        """Tarefas cuja espera conta para a vitalidade do agente.

        O laco principal e toda tarefa criada por `spawn()`. Uma tarefa fora
        deste conjunto pode travar sem que ninguem perceba -- por isso `spawn`
        e obrigatorio dentro de agente.
        """

        self._idle: dict[asyncio.Task[Any], tuple[datetime, str]] = {}
        """Quais tarefas vigiadas estao esperando, e desde quando."""

        self._resumed = asyncio.Event()
        self._resumed.set()
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._resumed.set()
        self.state = AgentState.RUNNING
        self.started_at = datetime.now(UTC)
        self._task = asyncio.create_task(self._guarded_run(), name=f"agent-{self.name}")
        self._start_pulse()
        self.log.info("agent.started")

    async def stop(self) -> None:
        """Desligamento completo. Nenhuma etapa pode impedir as seguintes.

        O `try/finally` nao e decoracao: quando `_close_inboxes` estourava no
        meio, o agente ficava em `RUNNING` e `on_stop()` -- que fecha broker e
        sessao HTTP -- simplesmente nao rodava, enquanto o orquestrador
        registrava um desligamento limpo.
        """
        self._stopping.set()
        self._resumed.set()  # libera quem estiver parado no gate de pausa
        for etapa in (self._cancel_task, self._cancel_pulse, self._close_inboxes):
            try:
                await etapa()
            except Exception as exc:  # pragma: no cover - defesa em profundidade
                self.log.error(
                    "agent.stop_step_failed", step=etapa.__name__, error=str(exc)
                )
        self.state = AgentState.STOPPED
        self._idle.clear()
        self._vigiadas.clear()
        await self.on_stop()
        self.log.info("agent.stopped")

    async def restart(self) -> str | None:
        """Recicla a tarefa de processamento SEM soltar as assinaturas.

        E isto que o watchdog usa, e nao `stop()` + `start()`. Entre um `stop()`
        e o `start()` seguinte o agente deixa de assinar o topico, e candle e
        publicado uma unica vez: um reinicio caindo na rajada dos 16 pares
        descartava sinais em silencio. Aqui a caixa de entrada continua
        recebendo enquanto a tarefa e trocada.

        Devolve a descricao do evento que estava em processamento e nao terminou
        (perda real, impossivel de evitar) ou None.
        """
        was_paused = self.is_paused
        await self._cancel_task()
        await self._cancel_pulse()
        lost = self._drop_in_flight()
        self.restarts += 1
        await self.start()
        if was_paused:
            # Reiniciar nao pode desfazer uma pausa pedida pelo operador:
            # `start()` libera o gate de pausa, entao a pausa e reposta aqui.
            self.pause()
        return lost

    def pause(self) -> None:
        self._resumed.clear()
        self.state = AgentState.PAUSED
        self.log.warning("agent.paused")

    def resume(self) -> None:
        self._resumed.set()
        self.state = AgentState.RUNNING
        self.log.info("agent.resumed")

    @property
    def is_paused(self) -> bool:
        return not self._resumed.is_set()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def is_idle(self) -> bool:
        """True quando TODAS as tarefas vigiadas estao esperando.

        Esperar e o estado normal de um agente orientado a evento, e e a unica
        situacao em que a batida de vitalidade sai. Ocupado nao bate: um agente
        pendurado no meio do processamento tem que envelhecer aos olhos do
        watchdog, que e a deteccao que o watchdog existe para fazer.

        O "todas" e o ponto. Com uma unica flag por agente, bastava a tarefa que
        alimenta a fila estar esperando para o agente inteiro parecer ocioso --
        e o Risk Manager podia ficar pendurado no laco que avalia ordens, para
        sempre, com watchdog e dashboard verdes.
        """
        if not self.is_running:
            return False
        vigiadas = self._live_watched()
        if not vigiadas:
            return False
        return all(task in self._idle for task in vigiadas)

    @property
    def idle_detail(self) -> str:
        """O que o agente esta esperando, para o log e o dashboard."""
        entradas = [self._idle[t] for t in self._live_watched() if t in self._idle]
        if not entradas:
            return ""
        return min(entradas, key=lambda item: item[0])[1]

    def _live_watched(self) -> list[asyncio.Task[Any]]:
        """Tarefas vigiadas ainda vivas, limpando as que ja morreram."""
        mortas = [task for task in self._vigiadas if task.done()]
        for task in mortas:
            self._vigiadas.discard(task)
            self._idle.pop(task, None)
        return list(self._vigiadas)

    @property
    def pending_events(self) -> int:
        """Eventos recebidos e ainda nao processados, somando as caixas."""
        return sum(inbox.pending for inbox in self._inboxes.values())

    @property
    def subscriptions_ready(self) -> bool:
        """True quando as assinaturas deste agente ja estao registradas no bus."""
        return bool(self._inboxes) and all(
            inbox.ready.is_set() and inbox.broken is None
            for inbox in self._inboxes.values()
        )

    @property
    def deaf_topics(self) -> list[str]:
        """Topicos que este agente deveria estar ouvindo e nao esta.

        Surdo e o estado mais perigoso que existe aqui, porque e o que mais se
        parece com saudavel: de pe, sem erro, sem evento pendente e -- se
        ninguem olhar por este atributo -- esperando, logo batendo.
        """
        return [topic for topic, box in self._inboxes.items() if not box.alive]

    # ------------------------------------------------------------------
    # Para as subclasses
    # ------------------------------------------------------------------
    @abc.abstractmethod
    async def _run(self) -> None:
        """Loop principal do agente."""

    async def on_stop(self) -> None:  # noqa: B027 - gancho opcional, nem todo agente tem recursos
        """Liberacao de recursos (conexoes, clientes HTTP)."""

    async def wait_if_paused(self) -> None:
        """Gate de pausa. Chamar no inicio de cada ciclo, nunca no meio.

        Ficar parado no gate tambem e espera, e nao trabalho pendurado: sem
        marcar, um agente pausado apareceria como "processando" no dashboard.
        """
        if self._resumed.is_set():
            return
        self.enter_idle("pausado")
        try:
            await self._resumed.wait()
        finally:
            self.leave_idle()

    async def sleep(self, seconds: float) -> bool:
        """Dorme, mas acorda na hora se o agente for parado.

        Devolve False quando o motivo do despertar foi o shutdown -- assim o loop
        do agente sai imediatamente em vez de esperar o ciclo inteiro.

        Dormir entre ciclos tambem e espera, e portanto tambem e sinal de vida:
        um intervalo de coleta maior que o timeout do watchdog daria o mesmo
        diagnostico errado que o agente de evento dava.
        """
        self.enter_idle(f"proximo ciclo em {seconds:g}s")
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        finally:
            self.leave_idle()
        return not self._stopping.is_set()

    async def heartbeat(self, detail: str | None = None) -> None:
        self.last_beat = datetime.now(UTC)
        await self.bus.publish(
            Topics.HEARTBEATS,
            Heartbeat(
                agent=self.name,
                state=str(self.state),
                timestamp=self.last_beat,
                detail=detail,
            ),
        )

    # ------------------------------------------------------------------
    # Vitalidade e caixa de entrada
    # ------------------------------------------------------------------
    def inbox(self, topic: str) -> _Inbox:
        """Caixa de entrada do agente para um topico, criada uma unica vez."""
        existing = self._inboxes.get(topic)
        if existing is None:
            existing = _Inbox(self, self.bus.raw, topic)
            self._inboxes[topic] = existing
        existing.ensure_feeding()
        return existing

    def spawn(
        self, coro: Coroutine[Any, Any, Any], *, name: str, vigiada: bool = True
    ) -> asyncio.Task[Any]:
        """Cria uma tarefa interna do agente, vigiada e supervisionada.

        Duas garantias que `asyncio.create_task` nao da:

        1. A tarefa entra na conta da vitalidade -- se ela estiver trabalhando (ou
           travada), o agente NAO bate como ocioso.
        2. Se ela morrer com excecao, o agente vai para `ERROR` em vez de seguir
           de pe com uma perna a menos. Uma tarefa que morre calada e o pior
           cenario que existe aqui.
        """
        task = asyncio.create_task(coro, name=f"{self.name}-{name}")
        if vigiada:
            self._vigiadas.add(task)
        task.add_done_callback(self._worker_finished)
        return task

    def _worker_finished(self, task: asyncio.Task[Any]) -> None:
        self._vigiadas.discard(task)
        self._idle.pop(task, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        self.state = AgentState.ERROR
        self.last_error = f"tarefa interna '{task.get_name()}' morreu: {error}"
        self.log.error(
            "agent.worker_died", task=task.get_name(), error=str(error)
        )

    def enter_idle(self, detail: str) -> None:
        """A tarefa CORRENTE declara que esta esperando, e nao trabalhando."""
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - so fora de event loop
            return
        self._idle[task] = (datetime.now(UTC), detail)

    def leave_idle(self) -> None:
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - so fora de event loop
            return
        self._idle.pop(task, None)

    async def report_inbox_failure(self, topic: str, error: Exception, pending: int) -> None:
        """A assinatura de um topico caiu: o agente esta surdo ate reassinar.

        Vai para `ERROR` de proposito -- e o que faz o watchdog reiniciar e
        reassinar --, e sai um alerta ALTO, porque o que se perde aqui e evento
        de publicacao unica: candle, sinal, pedido de ordem.
        """
        self.inbox_failures += 1
        self.state = AgentState.ERROR
        self.last_error = f"assinatura de {topic} caiu: {error}"
        self.log.error(
            "agent.inbox_stream_failed",
            topic=topic,
            error=str(error),
            pending=pending,
            detail="o agente parou de ouvir este topico; eventos publicados agora somem",
        )
        with contextlib.suppress(Exception):
            await self.bus.publish(
                Topics.ALERTS,
                {
                    "type": "inbox_stream_failed",
                    "title": f"Agente '{self.name}' perdeu a assinatura de {topic}",
                    "message": (
                        f"erro: {error}\n\n"
                        f"Eventos de '{topic}' publicados a partir de agora NAO chegam "
                        f"a este agente ate o watchdog reassinar, e este topico e de "
                        f"publicacao unica -- o que passar nessa janela esta perdido. "
                        f"{pending} evento(s) ainda na fila local."
                    ),
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )

    async def _beat_while_idle(self) -> None:
        """Bate heartbeat porque esta VIVO esperando, nao porque recebeu trabalho.

        E o coracao da correcao de D25. O agente `strategy` batia so dentro do
        laco `async for candle in bus.subscribe(CANDLES)`; com timeframe 1d ele
        passa horas legitimamente parado, e o watchdog -- que so sabia olhar "ha
        quanto tempo nao bate" -- o reiniciou 17 vezes em 16 minutos.

        A alternativa obvia, aumentar o timeout, nao resolve nada: apenas atrasa
        o mesmo diagnostico errado e enfraquece a deteccao de travamento real.
        """
        while True:
            await asyncio.sleep(IDLE_PULSE_SECONDS)
            if not self.is_idle or self.is_paused or self.deaf_topics:
                # Ocupado, morto, pausado ou surdo: nada bate. A batida so cobre
                # espera legitima -- um agente que perdeu a assinatura esta
                # esperando um evento que nunca vai chegar, e isso e travamento.
                continue
            try:
                await self.heartbeat(detail=f"ocioso: {self.idle_detail}")
            except Exception as exc:  # pragma: no cover - bus indisponivel
                self.log.warning("agent.idle_beat_failed", error=str(exc))

    def _start_pulse(self) -> None:
        if self._pulse is None or self._pulse.done():
            self._pulse = asyncio.create_task(
                self._beat_while_idle(), name=f"pulse-{self.name}"
            )

    def _drop_in_flight(self) -> str | None:
        """Registra em ALTO o evento que morreu no meio do processamento."""
        lost: str | None = None
        for topic, box in self._inboxes.items():
            if box.in_flight is None:
                continue
            self.lost_events += 1
            lost = f"{topic}: {describe_event(box.in_flight)}"
            self.log.error("agent.event_lost_in_restart", topic=topic, lost=lost)
            box.in_flight = None
        return lost

    async def _cancel_task(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        # Tambem engolimos a excecao do proprio agente: se ele morreu sozinho, a
        # tarefa guarda a falha e aguardar por ela a relancaria aqui -- o erro ja
        # esta em `last_error` e no log, e derrubar quem esta reiniciando seria
        # perder o reinicio por causa da falha que o motivou.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    async def _cancel_pulse(self) -> None:
        if self._pulse is None:
            return
        self._pulse.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._pulse
        self._pulse = None

    async def _close_inboxes(self) -> None:
        for topic, box in list(self._inboxes.items()):
            # Uma caixa que falha ao fechar nao pode impedir o fechamento das
            # outras: era assim que uma assinatura vazava sem ninguem notar.
            try:
                await box.close()
            except Exception as exc:  # pragma: no cover - defesa em profundidade
                self.log.error("agent.inbox_close_failed", topic=topic, error=str(exc))
        # Esvaziar o registro e o que permite um `start()` posterior assinar de
        # novo: a caixa fechada nao serve mais, e devolve-la deixaria o agente
        # de pe e surdo.
        self._inboxes.clear()

    # ------------------------------------------------------------------
    async def _guarded_run(self) -> None:
        """Envolve `_run()` para que uma excecao nao derrube o processo inteiro.

        Um agente que morre em silencio e o pior cenario: o sistema pareceria
        saudavel enquanto uma etapa da cadeia deixou de existir. Registramos o
        estado ERROR, que o orquestrador enxerga e usa para reiniciar.
        """
        # O laco principal e a primeira tarefa vigiada. Sem isto o agente
        # comecaria "ocioso por vacuidade": nenhuma tarefa a cobrar, logo todas
        # esperando.
        principal = asyncio.current_task()
        if principal is not None:
            self._vigiadas.add(principal)
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state = AgentState.ERROR
            self.last_error = str(exc)
            self.log.exception("agent.crashed", error=str(exc))
            await self.heartbeat(detail=f"crash: {exc}")
            raise
