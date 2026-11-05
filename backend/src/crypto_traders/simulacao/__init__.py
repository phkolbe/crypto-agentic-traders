"""Simulador de mercado e agente de cenario.

Sobe o sistema REAL (orquestrador, agentes, broker de papel, banco) contra uma
exchange roteirizada, para PROVOCAR cada protecao e ver se ela agiu. Ler que uma
protecao esta configurada nao prova nada -- foi essa diferenca que deixou o
stop-loss inerte (docs/SEGURANCA.md, secao 12).
"""

from .cenario import AgenteDeCenario, ConexaoOnChainDesligada
from .mercado import MercadoSimulado, TickerIndisponivel

__all__ = [
    "AgenteDeCenario",
    "ConexaoOnChainDesligada",
    "MercadoSimulado",
    "TickerIndisponivel",
]
