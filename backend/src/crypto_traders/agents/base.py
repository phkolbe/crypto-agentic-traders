"""Infraestrutura comum dos agentes.

Cuida do ciclo de vida (start/pause/resume/stop), heartbeat e tratamento de erro,
para que cada agente concreto contenha apenas a sua logica de negocio.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
from datetime import UTC, datetime

from ..bus import EventBus, Topics
from ..domain.enums import AgentState
from ..domain.models import Heartbeat
from ..logging_setup import get_logger


class BaseAgent(abc.ABC):
    """Agente com ciclo de vida gerenciado.

    Subclasses implementam `_run()`. O `pause()` e cooperativo: o agente termina
    o ciclo corrente e so entao para. Interromper um agente no meio de um ciclo
    poderia deixar uma ordem enviada e nao registrada.
    """

    name: str

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.log = get_logger(f"agent.{self.name}")
        self.state = AgentState.STOPPED
        self.last_error: str | None = None
        self.last_beat: datetime | None = None
        self._task: asyncio.Task[None] | None = None
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
        self._task = asyncio.create_task(self._guarded_run(), name=f"agent-{self.name}")
        self.log.info("agent.started")

    async def stop(self) -> None:
        self._stopping.set()
        self._resumed.set()  # libera quem estiver parado no gate de pausa
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.state = AgentState.STOPPED
        await self.on_stop()
        self.log.info("agent.stopped")

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

    # ------------------------------------------------------------------
    # Para as subclasses
    # ------------------------------------------------------------------
    @abc.abstractmethod
    async def _run(self) -> None:
        """Loop principal do agente."""

    async def on_stop(self) -> None:  # noqa: B027 - gancho opcional, nem todo agente tem recursos
        """Liberacao de recursos (conexoes, clientes HTTP)."""

    async def wait_if_paused(self) -> None:
        """Gate de pausa. Chamar no inicio de cada ciclo, nunca no meio."""
        await self._resumed.wait()

    async def sleep(self, seconds: float) -> bool:
        """Dorme, mas acorda na hora se o agente for parado.

        Devolve False quando o motivo do despertar foi o shutdown -- assim o loop
        do agente sai imediatamente em vez de esperar o ciclo inteiro.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
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
    async def _guarded_run(self) -> None:
        """Envolve `_run()` para que uma excecao nao derrube o processo inteiro.

        Um agente que morre em silencio e o pior cenario: o sistema pareceria
        saudavel enquanto uma etapa da cadeia deixou de existir. Registramos o
        estado ERROR, que o orquestrador enxerga e usa para reiniciar.
        """
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
