"""WebSocket de tempo real.

Reencaminha eventos do event bus interno para o dashboard: novo sinal, decisao
de risco, ordem executada, snapshot de portfolio e alertas.

Uma unica tarefa consome o bus e faz fan-out para os navegadores conectados --
em vez de uma assinatura por aba aberta, que multiplicaria o trabalho do bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect

from ...bus import Topics
from ...logging_setup import get_logger

if TYPE_CHECKING:
    from ...agents.orchestrator import Orchestrator

log = get_logger(__name__)
router = APIRouter()

#: Topicos espelhados no WebSocket, com o nome do evento visto pelo frontend.
STREAMED_TOPICS = {
    Topics.SIGNALS: "signal",
    Topics.RISK_ASSESSMENTS: "risk_assessment",
    Topics.ORDER_RESULTS: "order_result",
    Topics.PORTFOLIO_SNAPSHOTS: "portfolio",
    Topics.ALERTS: "alert",
}


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)
        log.info("ws.connected", clients=len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, event: str, payload: Any) -> None:
        if not self._connections:
            return
        message = json.dumps({"event": event, "data": _serialize(payload)}, default=str)
        for websocket in list(self._connections):
            try:
                await websocket.send_text(message)
            except Exception:
                # Aba fechada sem handshake de saida: remover e o suficiente,
                # nao ha o que registrar.
                self.disconnect(websocket)

    @property
    def count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()


def _serialize(payload: Any) -> Any:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    return payload


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            # O cliente nao envia comandos; o receive serve para detectar o
            # fechamento da conexao.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


def attach_broadcaster(app: FastAPI, orchestrator: Orchestrator) -> None:
    """Liga o event bus interno ao fan-out do WebSocket."""

    async def relay(topic: str, event: str) -> None:
        async for payload in orchestrator.bus.subscribe(topic):
            await manager.broadcast(event, payload)

    app.state.ws_tasks = [
        asyncio.create_task(relay(topic, event), name=f"ws-relay-{event}")
        for topic, event in STREAMED_TOPICS.items()
    ]


async def detach_broadcaster(app: FastAPI) -> None:
    for task in getattr(app.state, "ws_tasks", []):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    app.state.ws_tasks = []
