"""Logging estruturado (structlog).

Todo evento sensivel do sistema passa por aqui. Em producao (`LOG_JSON=true`) a
saida e JSON, o que permite indexar e auditar depois; em desenvolvimento a saida
e colorida e legivel.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_configured = False

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


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    global _configured
    if _configured:
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
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(level=level.upper(), stream=sys.stderr, format="%(message)s")
    for noisy in ("ccxt", "urllib3", "asyncio", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    configure_logging()
    return structlog.get_logger(name)
