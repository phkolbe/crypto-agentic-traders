"""Stop e alvo contra um candle: a regra unica dos dois motores de backtest.

A producao nao ve o candle. Ela le o ticker a cada `market_data_interval_seconds`
(60 s) e vende a mercado quando a leitura passa do nivel (`RiskManagerAgent`,
protecao em software). O backtest so tem o OHLC do candle, e ler o OHLC como se
a producao visse cada negocio erra nas duas direcoes:

- **Stop, preenchimento (otimista):** o motor vendia exatamente no nivel (ou na
  abertura, se o candle abrisse abaixo -- o que em cripto 24/7 nao acontece: a
  abertura e o fechamento anterior). A producao so vende na primeira leitura de
  60 s ja abaixo do nivel, e numa queda essa leitura esta alem dele.
- **Alvo, disparo (otimista):** o motor disparava quando a maxima tocava o nivel.
  Um pavio que passa do alvo e volta entre duas leituras nao e visto pela
  producao, que segue com a posicao aberta.
- **Stop, disparo (pessimista):** pelo mesmo motivo, um pavio que toca o stop e
  volta entre duas leituras tambem nao vende em producao, e o motor vendia.

As tres folgas abaixo corrigem os tres, explicitas e configuraveis. A regra
morava duplicada em `engine.py` e `portfolio.py`; agora os dois motores chamam
`saida_de_protecao`, e uma convencao nova nao tem como valer so num lado.

Calibracao (09/11/2026), com candles publicos de 1 minuto da Binance -- o
fechamento de cada minuto faz o papel da leitura de 60 s. BTC, ETH, SOL, DOGE e
XRP contra USDT, em abr/2025, out/2025, mar/2026 e jul/2026 (20 pares-mes, ~600
dias). Scripts e saida no relatorio da frente F1.

- **Quanto alem do stop vende a producao.** Posicao aberta no fechamento de cada
  dia, stop a 3%, caminhando dia a dia ate o OHLC romper o stop: 366 disparos. Em
  342 deles houve leitura de 60 s abaixo do nivel naquele dia, e a primeira
  estava, em media, 0,13% abaixo do stop (mediana 0,08%, p90 0,31%, maximo
  0,84%; BTC 0,08%, DOGE 0,18%). Nos outros 24 (7%) o rompimento foi so pavio: a
  producao nao vendeu naquele dia. Nenhum candle abriu abaixo do stop.
- **Pavio que o ticker nao ve.** Niveis a 2, 3, 4, 5, 6, 8 e 10% do fechamento
  anterior, no dia seguinte, regra do OHLC com folga contra a verdade da leitura
  de 1 minuto. Alvo: sem folga, 759 disparos contra 718 verdadeiros (41 falsos);
  com 0,15%, 715 contra 718 (13 falsos, 16 perdidos). Stop: sem folga, 731 contra
  697; com 0,15%, 692 contra 697. A folga em que as contagens empatam fica entre
  0,10% e 0,15% nos dois lados.

Efeito no conjunto, na mesma caminhada (stop 3%; vies = retorno medio por
operacao do backtest menos o da leitura de 60 s):

======================  ===========  ===========
folgas                  alvo 6%      alvo 8%
======================  ===========  ===========
nenhuma (motor antigo)  -0,07%       -0,08%
as tres calibradas      -0,19%       -0,15%
======================  ===========  ===========

O motor antigo ja saia pessimista NO AGREGADO, porque o pavio no stop
(pessimista) pesava mais que o stop no nivel e o alvo no pavio (otimistas). Isso
nao autorizava as duas convencoes otimistas: elas favorecem justamente o que se
quer comparar (alvo mais longe depende mais de pavio). Com as tres folgas
nenhuma convencao e otimista e o resto do vies continua do lado pessimista: o
alvo preenche NO nivel (a leitura de producao fica em media 0,17% acima, credito
que o backtest nao toma) e o stop vence o empate no mesmo candle.

As folgas sao fenomenos do minuto, nao do candle: valem para qualquer timeframe
de 1m para cima. Foram medidas em pares grandes; par menos liquido tem pavio e
salto maiores (DOGE ja mostra o dobro do BTC).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FolgasDeProtecao:
    """Quanto a leitura de 60 s da producao difere do que o OHLC sugere.

    `stop`: fracao abaixo do nivel em que a venda do stop e preenchida
    (`stop x (1 - folga)`), limitada pela minima do candle -- nenhum preco abaixo
    dela existiu.

    `alvo`: fracao acima do nivel que a maxima precisa passar para o alvo contar
    como visto. O preenchimento continua NO nivel: a producao venderia na leitura,
    um pouco acima, e esse credito o backtest nao toma.

    `pavio_stop`: fracao abaixo do nivel que a minima precisa passar para o stop
    contar como visto. Simetrica a do alvo.
    """

    stop: Decimal = Decimal("0.0013")
    alvo: Decimal = Decimal("0.0015")
    pavio_stop: Decimal = Decimal("0.0015")

    def __post_init__(self) -> None:
        for nome in ("stop", "alvo", "pavio_stop"):
            valor = getattr(self, nome)
            if not Decimal(0) <= valor < Decimal(1):
                raise ValueError(f"folga de {nome} fora de [0, 1): {valor}")


#: Valores calibrados (ver docstring do modulo).
FOLGAS_PADRAO = FolgasDeProtecao()

#: Sem folga: o OHLC lido como se a producao visse cada negocio. So para testes
#: que isolam as outras convencoes (stop vence empate, gap conta contra).
SEM_FOLGA = FolgasDeProtecao(stop=Decimal(0), alvo=Decimal(0), pavio_stop=Decimal(0))


def saida_de_protecao(
    abertura: Decimal,
    alta: Decimal,
    baixa: Decimal,
    stop: Decimal,
    alvo: Decimal,
    folgas: FolgasDeProtecao = FOLGAS_PADRAO,
) -> tuple[str, Decimal] | None:
    """(`"stop"` ou `"alvo"`, preco de saida), ou None se o candle nao dispara.

    Convencoes, na ordem em que sao aplicadas:

    1. **Stop e alvo no mesmo candle: o stop vence.** O OHLC nao diz qual preco
       veio primeiro; supor o alvo seria escolher o desfecho bom com informacao
       que nao existe.
    2. **Stop:** dispara se a minima passou de `stop x (1 - pavio_stop)`.
       Preenche em `min(abertura, max(minima, stop x (1 - folga_stop)))`: gap
       conta contra, e a folga reproduz a leitura de 60 s ja alem do nivel.
    3. **Alvo:** so dispara se a maxima passou de `alvo x (1 + folga_alvo)`, e
       preenche no nivel, sem credito pelo gap ou pela leitura acima.

    O deslizamento do `PaperBroker` vem por cima, nas duas pontas.
    """
    if baixa <= stop * (Decimal(1) - folgas.pavio_stop):
        alem = stop * (Decimal(1) - folgas.stop)
        return "stop", min(abertura, max(baixa, alem))
    if alta >= alvo * (Decimal(1) + folgas.alvo):
        return "alvo", alvo
    return None
