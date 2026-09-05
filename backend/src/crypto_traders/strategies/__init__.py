"""Registro de estrategias.

Estrategias sao plugins resolvidos por nome (o que vem em `STRATEGIES` no
`.env`). Adicionar uma nova e escrever a classe e registra-la aqui -- nenhum
agente muda.
"""

from __future__ import annotations

from .base import MarketFrame, Strategy
from .builtin import BollingerReversion, MacdTrend, MovingAverageCrossover, RsiReversion

__all__ = [
    "BollingerReversion",
    "MacdTrend",
    "MarketFrame",
    "MovingAverageCrossover",
    "RsiReversion",
    "Strategy",
    "available_strategies",
    "build_strategies",
    "get_strategy",
]

_REGISTRY: dict[str, type[Strategy]] = {
    MovingAverageCrossover.name: MovingAverageCrossover,
    RsiReversion.name: RsiReversion,
    MacdTrend.name: MacdTrend,
    BollingerReversion.name: BollingerReversion,
}


def available_strategies() -> dict[str, str]:
    """Nome -> descricao, para a API e o dashboard listarem as opcoes."""
    return {name: cls.description for name, cls in _REGISTRY.items()}


def get_strategy(name: str, **params: object) -> Strategy:
    try:
        strategy_class = _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"estrategia desconhecida: '{name}'. Disponiveis: {', '.join(sorted(_REGISTRY))}"
        ) from None
    return strategy_class(**params)  # type: ignore[arg-type]


def build_strategies(names: list[str]) -> list[Strategy]:
    """Instancia as estrategias configuradas.

    Falha alto se um nome nao existir: subir com uma estrategia a menos por causa
    de um typo no `.env` seria pior do que nao subir -- o sistema pareceria
    saudavel enquanto opera diferente do configurado.
    """
    return [get_strategy(name) for name in names]


def register_strategy(strategy_class: type[Strategy]) -> type[Strategy]:
    """Decorator para registrar estrategias definidas fora deste modulo."""
    _REGISTRY[strategy_class.name] = strategy_class
    return strategy_class
