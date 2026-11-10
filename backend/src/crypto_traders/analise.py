"""Analise do portfolio para a tela Portfolio: resultado por par e motivos de risco.

Funcoes puras, sem banco: quem chama traz os trades e os eventos. Ficam aqui, e
nao na rota, para o teste poder medir a regra com os mesmos objetos que o sistema
usa.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .agents.portfolio import _instante, custo_medio_movel


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
    *,
    cotacoes_da_taxa: Mapping[str, Decimal] | None = None,
) -> list[ResultadoDoPar]:
    """Realizado por par no periodo, pela MESMA regra do retrato da carteira.

    A conta e `agents.portfolio.custo_medio_movel`, a mesma que o
    `PortfolioAgent._apurar` usa para o preco medio que decide stop. Eram duas
    copias linha a linha, e a proxima correcao (como a taxa em BNB) entraria numa
    so. O custo medio percorre o historico INTEIRO (a base de custo de uma venda de
    hoje pode vir de uma compra de meses atras), mas so as vendas, compras e taxas
    dentro de [inicio, fim] contam no resultado do periodo. As exclusoes sao as da
    apuracao: outra cotacao, outro modo (lancamento manual conta sempre), e a taxa
    em terceira moeda sem `cotacoes_da_taxa` (ver `cotacoes_das_taxas`).

    "Ganho" e "perda" sao por VENDA: uma venda que realizou acima ou abaixo do
    custo medio mais a taxa. A que realizou exatamente zero nao e nenhum dos dois
    (contava como perda). Venda sem base de custo tambem nao -- e contada a parte,
    porque declarar o preco cheio como lucro mentiria sobre o resultado.

    O que este total NAO tem: o credito do razao de fantasmas, que o retrato soma
    ao realizado (`PortfolioAgent._reconciliar`). Por isso a soma por par pode
    diferir do realizado do retrato quando ha reconciliacao.
    """
    pares: dict[str, ResultadoDoPar] = {}

    def par(simbolo: str) -> ResultadoDoPar:
        if simbolo not in pares:
            pares[simbolo] = ResultadoDoPar(simbolo=simbolo, moeda=quote)
        return pares[simbolo]

    percurso = custo_medio_movel(trades, quote, modo, cotacoes_da_taxa)
    for passo in percurso.passos:
        if not inicio <= _instante(passo.trade.executed_at) <= fim:
            continue
        resultado = par(passo.trade.symbol)
        resultado.taxas += passo.taxa
        if passo.compra:
            resultado.compras += 1
            continue
        resultado.vendas += 1
        resultado.realizado += passo.realizado
        if passo.sem_base:
            resultado.vendas_sem_base += 1
        elif passo.realizado > 0:
            resultado.ganhos += 1
        elif passo.realizado < 0:
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
