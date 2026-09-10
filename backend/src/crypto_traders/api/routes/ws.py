"""WebSocket de tempo real.

Reencaminha eventos do event bus interno para o dashboard: novo sinal, decisao
de risco, ordem executada, snapshot de portfolio e alertas.

Uma unica tarefa consome o bus e faz fan-out para os navegadores conectados --
em vez de uma assinatura por aba aberta, que multiplicaria o trabalho do bus.

O fan-out **nunca espera por socket**: ele enfileira e segue. Cada navegador tem
a sua fila e a sua tarefa de envio, porque `send_text` fica pendurado quando o
outro lado parou de ler -- aba suspensa, maquina hibernando, celular sem sinal.
Enviando em serie dentro do fan-out, um unico cliente nesse estado congelava o
dashboard de todos: medido, com um cliente travado o cliente saudavel recebeu
1 de 30 eventos publicados, e os outros 29 foram descartados pelo bus quando a
fila do relay encheu. Ninguem no navegador ficaria sabendo.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect

from ...bus import Topics
from ...config import get_settings
from ...logging_setup import get_logger
from ..deps import origem_confiavel

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

#: Mensagens em espera por navegador antes de a conexao ser derrubada.
#:
#: Folga generosa para engasgo de rede (a rajada de 16 pares cabe varias vezes),
#: e ainda assim um teto: cliente que nao le nunca vai ler o que ficou para tras,
#: e acumular so troca um dashboard atrasado por memoria crescente. Derrubar faz
#: o navegador reconectar e buscar estado fresco pelas rotas REST.
MAX_PENDING_PER_CLIENT = 200

#: Prazo do fechamento de cortesia de um cliente derrubado. O `close` viaja pelo
#: mesmo socket entupido que causou a queda, entao ele tambem pode ficar
#: pendurado -- e nada no caminho do fan-out pode esperar por rede.
CLOSE_TIMEOUT_SECONDS = 5.0

#: Espera antes de reassinar um topico cuja assinatura no bus quebrou.
RELAY_RESUBSCRIBE_SECONDS = 1.0

#: Reassinaturas consecutivas sem nenhum evento entregue antes de desistir.
#:
#: Reassinar existe porque com o bus em Redis uma queda de conexao nao pode
#: cegar o dashboard para sempre. O teto existe porque um bus que rejeita a
#: assinatura na hora produziria um erro por segundo indefinidamente -- e log
#: infinito e a forma de esconder o proprio erro.
MAX_RESUBSCRIBE_ATTEMPTS = 5


class _Client:
    """Uma conexao aberta e a fila de saida dela."""

    __slots__ = ("queue", "websocket", "writer")

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=MAX_PENDING_PER_CLIENT)
        self.writer: asyncio.Task[None] | None = None


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: dict[WebSocket, _Client] = {}
        # Tarefas de fechamento em voo. Sem guardar a referencia, o coletor de
        # lixo pode recolher a tarefa antes de ela rodar, e o socket derrubado
        # ficaria aberto sem nunca receber o frame de close.
        self._closings: set[asyncio.Task[None]] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        client = _Client(websocket)
        self._clients[websocket] = client
        client.writer = asyncio.create_task(self._drain(client), name="ws-writer")
        log.info("ws.connected", clients=len(self._clients))

    def disconnect(self, websocket: WebSocket) -> None:
        client = self._forget(websocket)
        if client is not None and client.writer is not None:
            client.writer.cancel()

    async def broadcast(self, event: str, payload: Any) -> None:
        """Entrega o evento a cada navegador, sem esperar por nenhum deles.

        Serializa uma vez e apenas enfileira. Esta funcao roda dentro do relay
        do event bus: se ela esperasse a rede, o assinante do bus pararia de
        consumir e os eventos seriam descartados na fila do bus -- perdidos para
        todo mundo por causa de um cliente so.
        """
        if not self._clients:
            return
        message = json.dumps({"event": event, "data": _serialize(payload)}, default=str)
        for client in list(self._clients.values()):
            try:
                client.queue.put_nowait(message)
            except asyncio.QueueFull:
                log.warning(
                    "ws.client_too_slow",
                    # `event` e palavra reservada do structlog: o nome do evento
                    # do frontend vai como `stream`.
                    stream=event,
                    pending=client.queue.qsize(),
                    detail="conexao derrubada; o navegador reconecta e le o estado atual",
                )
                self._drop(client)

        # Da a vez as tarefas de envio antes do proximo evento.
        #
        # Nao e cosmetico: `await queue.get()` no relay do bus **nao suspende**
        # quando ja ha evento na fila, entao uma rajada acumulada (a fila do bus
        # guarda ate 1000) seria drenada inteira sem o loop nunca rodar nenhum
        # writer -- e a fila de um cliente perfeitamente saudavel estouraria o
        # teto e o derrubaria. Medido: 220 broadcasts seguidos sem esta linha
        # derrubaram os DOIS clientes, inclusive o que estava lendo normalmente.
        # `sleep(0)` cede o controle sem nunca esperar por rede.
        await asyncio.sleep(0)

    async def _drain(self, client: _Client) -> None:
        """Envia, em serie, o que foi enfileirado para UM navegador."""
        while True:
            message = await client.queue.get()
            try:
                await client.websocket.send_text(message)
            except Exception:
                # Aba fechada sem handshake de saida: esquecer a conexao e o
                # suficiente, nao ha o que registrar. Nao chamamos `disconnect`
                # para nao cancelar a propria tarefa que esta rodando aqui.
                self._forget(client.websocket)
                return

    def _drop(self, client: _Client) -> None:
        self.disconnect(client.websocket)
        # Tarefa a parte, com prazo: ver CLOSE_TIMEOUT_SECONDS.
        tarefa = asyncio.create_task(_close_quietly(client.websocket), name="ws-close")
        self._closings.add(tarefa)
        tarefa.add_done_callback(self._closings.discard)

    def _forget(self, websocket: WebSocket) -> _Client | None:
        return self._clients.pop(websocket, None)

    async def close_all(self) -> None:
        """Derruba toda conexao e nao deixa tarefa viva para tras.

        Chamado no encerramento da API. Sem isto, `detach_broadcaster` cancelava
        os 5 relays e deixava uma tarefa `ws-writer` por navegador pendurada em
        `await queue.get()` -- medido, 1 tarefa por cliente conectado. Com o
        processo saindo o custo e ruido ("Task was destroyed but it is
        pending"); com dois `create_app()` no mesmo processo, ou num reload, os
        writers antigos seguem vivos segurando socket morto, e como o `manager`
        e singleton de modulo eles compartilham a lista.
        """
        clientes = [
            cliente
            for cliente in (self._forget(ws) for ws in list(self._clients))
            if cliente is not None
        ]
        # Fechamentos de cortesia ja em voo: o socket deles ja saiu da lista e o
        # processo esta descendo, entao cancelar e melhor que esperar o prazo.
        fechamentos = list(self._closings)
        self._closings.clear()

        tarefas: list[asyncio.Task[None]] = [
            cliente.writer for cliente in clientes if cliente.writer is not None
        ]
        tarefas.extend(fechamentos)
        for tarefa in tarefas:
            tarefa.cancel()
        if tarefas:
            await asyncio.gather(*tarefas, return_exceptions=True)

        # Todos de uma vez: cada `close` tem o seu prazo (CLOSE_TIMEOUT_SECONDS)
        # e um socket entupido nao pode somar espera aos outros.
        if clientes:
            await asyncio.gather(
                *(_close_quietly(cliente.websocket) for cliente in clientes),
                return_exceptions=True,
            )
        log.info("ws.closed_all", clients=len(clientes))

    @property
    def count(self) -> int:
        return len(self._clients)


manager = ConnectionManager()


async def _close_quietly(websocket: WebSocket) -> None:
    with contextlib.suppress(Exception):
        # 1013 = "try again later", o codigo certo para "voce nao esta
        # acompanhando"; o navegador reconecta em vez de tratar como erro.
        await asyncio.wait_for(websocket.close(code=1013), timeout=CLOSE_TIMEOUT_SECONDS)


def _serialize(payload: Any) -> Any:
    if hasattr(payload, "model_dump"):
        # `mode="json"` e o que faz Decimal virar string (D7): dinheiro em
        # number seria centavo errado no navegador.
        return payload.model_dump(mode="json")
    return payload


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    # WebSocket nao e protegido por CORS: qualquer pagina aberta na maquina
    # consegue abrir `ws://127.0.0.1:8000/ws` e LER tudo que passa por aqui --
    # patrimonio, ordens, alertas. O `Origin` e a unica coisa que separa o
    # dashboard local de uma aba estranha, e a pagina nao pode falsifica-lo.
    origem = websocket.headers.get("origin")
    if origem and not origem_confiavel(
        origem, getattr(websocket.app.state, "settings", None) or get_settings()
    ):
        log.warning(
            "ws.origem_recusada",
            origem=origem,
            detail="tentativa de assinar o tempo real de outra origem",
        )
        # 1008 = "policy violation". Fechado antes do `accept`, o handshake e
        # recusado (o servidor responde 403) e nenhum evento sai daqui.
        await websocket.close(code=1008)
        return

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
        tentativas = 0
        while True:
            try:
                async for payload in orchestrator.bus.subscribe(topic):
                    tentativas = 0
                    try:
                        await manager.broadcast(event, payload)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        # Um evento que nao serializa derruba O EVENTO, nunca o
                        # canal. O `return` que estava aqui matava a tarefa: o
                        # topico nao entregava mais nada ate a API reiniciar.
                        # Medido: um alerta com chave nao-string calou ALERTS, e
                        # o alerta de circuit breaker publicado em seguida nunca
                        # chegou ao navegador. Uma tela sem alerta e
                        # indistinguivel de "nada aconteceu" -- exatamente o
                        # estado que este modulo existe para evitar.
                        log.error(
                            "ws.event_dropped",
                            topic=topic,
                            stream=event,
                            error=str(error),
                            detail="evento descartado; o canal segue entregando",
                        )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Aqui a assinatura em si quebrou (com Redis, tipicamente queda
                # de conexao). Reassinar, com teto: ver MAX_RESUBSCRIBE_ATTEMPTS.
                tentativas += 1
                if tentativas > MAX_RESUBSCRIBE_ATTEMPTS:
                    log.error(
                        "ws.relay_failed",
                        topic=topic,
                        stream=event,
                        error=str(error),
                        tentativas=tentativas,
                        detail="o dashboard nao recebe mais este evento ate a API reiniciar",
                    )
                    return
                log.warning(
                    "ws.relay_resubscribing",
                    topic=topic,
                    stream=event,
                    error=str(error),
                    tentativas=tentativas,
                )
                await asyncio.sleep(RELAY_RESUBSCRIBE_SECONDS)
                continue

            log.warning(
                "ws.relay_ended",
                topic=topic,
                stream=event,
                detail="o dashboard nao recebe mais este evento ate a API reiniciar",
            )
            return

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
    # Os relays param de produzir, mas cada navegador tem a sua tarefa de envio:
    # sem isto elas ficam vivas depois do desligamento. Ver `close_all`.
    await manager.close_all()
