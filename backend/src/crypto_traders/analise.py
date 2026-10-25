"""Analise do portfolio para a tela Portfolio: resultado por par e motivos de risco.

Funcoes puras, sem banco: quem chama traz os trades e os eventos. Ficam aqui, e
nao na rota, para o teste poder medir a regra com os mesmos objetos que o sistema
usa.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .agents.portfolio import MODO_MANUAL, _decimal, _em_ordem, _instante
from .domain.enums import Side


@dataclass
class ResultadoDoPar:
    """O que um par rendeu no periodo, na moeda de cotacao dele."""

    simbolo: str
    moeda: str
    realizado: Decimal = Decimal(0)
    vendas: int = 0
    ganhos: int = 0
    perdas: int = 0
    compras: int = 0
    taxas: Decimal = Decimal(0)
    vendas_sem_base: int = 0


def resultado_por_par(
    trades: Iterable[Any],
    quote: str,
    modo: str,
    inicio: datetime,
    fim: datetime,
) -> list[ResultadoDoPar]:
    """Realizado por par no periodo, pela MESMA regra de `PortfolioAgent._apurar`.

    Custo medio movel percorrendo o historico INTEIRO (a base de custo de uma venda
    de hoje pode vir de uma compra de meses atras), mas so as vendas, compras e
    taxas dentro de [inicio, fim] contam no resultado do periodo. As mesmas
    exclusoes da apuracao, pelo mesmo motivo: trade de outra cotacao nao entra
    num medio desta (erro de unidade), trade de outro modo nao entra (papel nao e
    dinheiro real; lancamento manual conta sempre), taxa em outra moeda e
    descartada (converter exigiria a cotacao do instante, que nao esta guardada).

    "Ganho" e "perda" sao por VENDA: uma venda que realizou acima do custo medio
    mais a taxa. Venda sem base de custo nao e ganho nem perda -- e contada a
    parte, porque declarar o preco cheio como lucro mentiria sobre o resultado.
    """
    quantidades: dict[str, Decimal] = {}
    custos: dict[str, Decimal] = {}
    pares: dict[str, ResultadoDoPar] = {}

    def par(simbolo: str) -> ResultadoDoPar:
        if simbolo not in pares:
            pares[simbolo] = ResultadoDoPar(simbolo=simbolo, moeda=quote)
        return pares[simbolo]

    for trade in _em_ordem(trades):
        asset, _, cotacao = trade.symbol.partition("/")
        if (cotacao or quote) != quote:
            continue
        if (trade.mode or modo) not in (modo, MODO_MANUAL):
            continue

        quantidade = _decimal(trade.quantity)
        preco = _decimal(trade.price)
        taxa = _decimal(trade.fee)
        if taxa > 0 and (trade.fee_currency or quote) != quote:
            taxa = Decimal(0)
        no_periodo = inicio <= _instante(trade.executed_at) <= fim

        mantido = quantidades.get(asset, Decimal(0))
        custo = custos.get(asset, Decimal(0))

        if trade.side == str(Side.BUY):
            quantidades[asset] = mantido + quantidade
            custos[asset] = custo + quantidade * preco + taxa
            if no_periodo:
                resultado = par(trade.symbol)
                resultado.compras += 1
                resultado.taxas += taxa
            continue

        realizado_da_venda = -taxa
        sem_base = mantido <= 0
        if not sem_base:
            medio = custo / mantido
            vendido = min(quantidade, mantido)
            realizado_da_venda += vendido * (preco - medio)
            restante = mantido - vendido
            quantidades[asset] = restante
            custos[asset] = custo - vendido * medio if restante > 0 else Decimal(0)

        if no_periodo:
            resultado = par(trade.symbol)
            resultado.vendas += 1
            resultado.taxas += taxa
            resultado.realizado += realizado_da_venda
            if sem_base:
                resultado.vendas_sem_base += 1
            elif realizado_da_venda > 0:
                resultado.ganhos += 1
            else:
                resultado.perdas += 1

    return sorted(pares.values(), key=lambda r: r.realizado, reverse=True)


# ---------------------------------------------------------------------------
# Motivos de rejeicao do Risk Manager
# ---------------------------------------------------------------------------
_PAR = re.compile(r"\b[A-Z0-9]{2,12}/[A-Z0-9]{2,12}\b")
_NUMERO = re.compile(r"-?\d+(?:[.,]\d+)*%?")


def motivo_canonico(motivo: str, ativos: Iterable[str]) -> str:
    """O motivo sem o que varia de um evento para outro: par, ativo e numeros.

    "nao ha posicao em AVAX para fechar" e "nao ha posicao em SUI para fechar" sao
    o MESMO motivo; agrupados pelo texto cru, cada ativo viraria uma barra e o
    grafico diria que nenhum motivo se repete. So vira marcador o ativo que o
    sistema conhece (e nao qualquer palavra em maiusculas): uma sigla do proprio
    texto, como MVRV, continua legivel.
    """
    texto = _PAR.sub("<par>", motivo)
    conhecidos = sorted({a for a in ativos if a}, key=len, reverse=True)
    if conhecidos:
        texto = re.sub(r"\b(" + "|".join(map(re.escape, conhecidos)) + r")\b", "<ativo>", texto)
    return _NUMERO.sub("<n>", texto).strip()


@dataclass
class ResumoDeRisco:
    aprovadas: int = 0
    rejeitadas: int = 0
    motivos: list[tuple[str, int, str]] = field(default_factory=list)
    """(motivo canonico, quantas vezes, um exemplo real) -- do mais frequente."""


def resumo_de_risco(
    eventos: Iterable[Any], ativos: Iterable[str], limite: int = 8
) -> ResumoDeRisco:
    """Aprovadas, rejeitadas e os motivos de rejeicao mais frequentes.

    So conta decisao sobre sinal (`decision` preenchida): evento de circuit
    breaker ou de limite alterado nao e aprovacao nem rejeicao de ordem.
    """
    ativos = list(ativos)
    resumo = ResumoDeRisco()
    contagem: dict[str, list[Any]] = {}
    for evento in eventos:
        if evento.decision == "approved":
            resumo.aprovadas += 1
        elif evento.decision == "rejected":
            resumo.rejeitadas += 1
            for motivo in evento.reasons or ["sem motivo registrado"]:
                chave = motivo_canonico(str(motivo), ativos)
                contagem.setdefault(chave, [0, str(motivo)])[0] += 1
    ordenados = sorted(contagem.items(), key=lambda item: (-item[1][0], item[0]))
    resumo.motivos = [(chave, total, exemplo) for chave, (total, exemplo) in ordenados[:limite]]
    return resumo
