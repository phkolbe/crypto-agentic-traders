"""Ponto de entrada do sistema: sobe orquestrador e API juntos.

Um processo so, com desligamento limpo -- os agentes terminam o ciclo corrente
antes de parar, para que nenhuma ordem fique enviada sem registro.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from .agents.orchestrator import Orchestrator
from .config import get_settings
from .logging_setup import get_logger

log = get_logger(__name__)


async def run_system(with_api: bool = True) -> int:
    settings = get_settings()
    orchestrator = Orchestrator(settings)

    stop_signal = asyncio.Event()

    def request_stop() -> None:
        log.info("runtime.shutdown_requested")
        stop_signal.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            # No Windows o loop asyncio nao registra sinais; o KeyboardInterrupt
            # abaixo cobre esse caso.
            loop.add_signal_handler(sig, request_stop)

    await orchestrator.start()

    api_task: asyncio.Task[None] | None = None
    if with_api:
        api_task = asyncio.create_task(_serve_api(orchestrator, stop_signal), name="api")

    banner(settings, with_api)

    try:
        await stop_signal.wait()
    except KeyboardInterrupt:  # pragma: no cover - interativo
        pass
    finally:
        if api_task is not None:
            api_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await api_task
        await orchestrator.stop()

    return 0


async def _serve_api(orchestrator: Orchestrator, stop_signal: asyncio.Event) -> None:
    import uvicorn

    from .api.app import create_app

    settings = orchestrator.settings
    config = uvicorn.Config(
        create_app(orchestrator),
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(config)
    # Uvicorn instala os proprios handlers de sinal; desligamos para que o
    # controle de shutdown continue sendo do runtime.
    server.install_signal_handlers = lambda: None

    try:
        await server.serve()
    finally:
        stop_signal.set()


def banner(settings, with_api: bool) -> None:
    mode = str(settings.trading_mode).upper()
    print("\n" + "=" * 66)
    print(f"  crypto-agentic-traders  |  modo: {mode}")
    if settings.is_live:
        print("  " + "!" * 60)
        print("  ORDENS SERAO ENVIADAS COM DINHEIRO REAL")
        print("  " + "!" * 60)
    else:
        print("  Nenhuma ordem real sera enviada.")
    print(f"  exchange   : {settings.exchange}")
    print(f"  pares      : {', '.join(settings.trading.symbols)} ({settings.trading.timeframe})")
    print(f"  estrategias: {', '.join(settings.trading.strategies)}")
    if with_api:
        print(f"  API        : http://{settings.api_host}:{settings.api_port}")
        print(f"  docs       : http://{settings.api_host}:{settings.api_port}/docs")
    print("  Ctrl+C para encerrar.")
    print("=" * 66 + "\n")
