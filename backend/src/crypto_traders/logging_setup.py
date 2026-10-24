"""Logging estruturado (structlog).

Todo evento sensivel do sistema passa por aqui. Em producao (`LOG_JSON=true`) a
saida e JSON, o que permite indexar e auditar depois; em desenvolvimento a saida
e colorida e legivel.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Callable
from typing import Any

import structlog

#: (nivel, json) da configuracao em vigor; None antes da primeira.
_configured: tuple[str, bool] | None = None

_SENSITIVE_KEYS = {
    "api_key",
    "api_secret",
    "apikey",
    "secret",
    "passphrase",
    "password",
    "token",
    "authorization",
}


def _redact_secrets(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Ultima linha de defesa contra chave de API em arquivo de log.

    Se algum agente logar um dict de credenciais por descuido, o valor vira
    `***`. Melhor um log menos util do que uma chave vazada em disco.
    """
    for key in list(event_dict):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "***"
    return event_dict


#: Para onde vai uma copia de cada linha, alem do console: a observabilidade
#: (`observabilidade.instalar`) liga isto ao coletor OpenTelemetry. Vazio, nada.
_espelho: Callable[[str, dict[str, Any]], None] | None = None


def definir_espelho(espelho: Callable[[str, dict[str, Any]], None] | None) -> None:
    global _espelho
    _espelho = espelho


def _espelhar(_logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Copia a linha, ja redigida e com a excecao formatada, para o espelho.

    Fica depois de `_redact_secrets` de proposito: o que sai da maquina passa
    pela mesma ultima linha de defesa que o que vai ao disco.
    """
    if _espelho is not None:
        # Log nunca derruba quem loga.
        with contextlib.suppress(Exception):
            _espelho(method_name, event_dict)
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    global _configured
    # Reconfigura quando o pedido muda. Antes era "configura uma vez so": o
    # primeiro `get_logger` da importacao configurava com o padrao (console,
    # INFO) e a chamada do `cli.main` com o `.env` era ignorada -- LOG_JSON=true
    # nunca teve efeito. Mesmo defeito de sempre: configurado e inerte.
    if _configured == (level.upper(), json_output):
        return

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _espelhar,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(level=level.upper(), stream=sys.stderr, format="%(message)s")
    logging.getLogger().setLevel(level.upper())
    for noisy in ("ccxt", "urllib3", "asyncio", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = (level.upper(), json_output)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    if _configured is None:
        configure_logging()
    return structlog.get_logger(name)
