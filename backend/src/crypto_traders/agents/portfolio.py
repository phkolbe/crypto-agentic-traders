"""Portfolio / Reporting Agent.

Apura saldos, posicoes, PnL e grava snapshots da serie temporal que alimenta o
grafico do dashboard e a avaliacao do circuit breaker.

Nada aqui e numero de relatorio. O **preco medio** e a UNICA entrada do nivel de
stop-loss e de take-profit (`RiskManagerAgent.enforce_protective_exits`), e o
**resultado de negociacao** (`realized_pnl + unrealized_pnl`) e o numero que
arma ou desarma a trava (`RiskManagerAgent.check_circuit_breaker`). Errar a conta
aqui vende dinheiro real sem perda nenhuma, ou deixa de proteger diante de
prejuizo de verdade.

Tres decisoes que existem por causa de defeitos medidos:

* **O realizado e DERIVADO do historico por custo medio**, e nao a soma da
  coluna `trades.realized_pnl`. Aquela coluna so e preenchida pelo Execution
  Agent quando ele mesmo fecha a posicao; lancamento manual, venda parcial e
  posicao herdada deixam a coluna vazia, e o realizado publicado ficava zero com
  650 de lucro no historico.
* **O modo separa o dinheiro.** A apuracao considera apenas os trades do modo
  CORRENTE, mais os `manual` (que sao operacao real feita fora do sistema). O
  ensaio em dry_run roda contra o MESMO banco que sera usado em LIVE: sem o
  filtro, uma compra de papel a 100 contamina o medio da compra real a 50 e o
  stop dispara numa posicao que nao perdeu nada. O filtro e por modo corrente e
  nao "ignorar papel", senao o ensaio -- que roda AGORA -- ficaria com toda
  posicao sem preco medio, ou seja SEM STOP NENHUM.
* **Saldo e historico sao de instantes diferentes.** A exchange responde o saldo
  de agora; a tabela `trades` recebe a linha depois. Na janela entre os dois, a
  quantidade que saiu do saldo e ainda consta no historico e um FANTASMA, e o
  nao realizado dela desaparece antes de o realizado aparecer. O fantasma e
  reconciliado uma unica vez, com o rastro no `audit_log`, e o credito e
  desfeito quando ele deixa de existir (o trade chegou, ou o ativo voltou da
  carteira fria).

A ancora de tempo NAO e usada para o realizado. Cortar por
`trade.executed_at > ancora.timestamp` parece imunizar contra edicao retroativa,
mas `executed_at` nao e monotonico: lancamento com data no futuro fica
"posterior" a toda ancora seguinte e soma o mesmo lucro 1.440 vezes por dia, e um
relogio corrigido para tras produz o mesmo efeito sem que ninguem digite nada
errado. Aqui o realizado e sempre reapurado do historico inteiro, e a imunidade
a edicao retroativa vem do razao de fantasmas, que sabe distinguir "o historico
perdeu uma venda" (sobra fantasma) de "o historico ganhou uma venda" (o fantasma
some).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from ..bus import EventBus, Topics
from ..config import Settings
from ..db.repositories import (
    AgentRunRepository,
    AuditLogRepository,
    CandleRepository,
    PortfolioLedgerRepository,
    PortfolioSnapshotRepository,
    TradeRepository,
)
from ..db.session import session_scope
from ..domain.enums import Side
from ..domain.models import PortfolioSnapshot, Position
from ..exchanges.base import Broker
from .base import BaseAgent

#: Modo dos lancamentos digitados em Operacoes. NAO e um modo de execucao: e
#: operacao REAL feita fora do sistema (pelo app da exchange), e por isso conta
#: em qualquer modo corrente. Se tivesse modo proprio ficaria fora de toda
#: apuracao -- e a compra digitada e justamente o caminho oferecido para dar base
#: de custo, e portanto stop, a uma posicao herdada.
MODO_MANUAL = "manual"

# Acoes do audit_log. Descartar taxa, cotacao ou base de custo muda dinheiro, e
# reconciliar saldo com historico muda o numero que a trava obedece: nada disso
# pode acontecer em silencio.
ACAO_MODO = "portfolio_trade_de_outro_modo"
ACAO_COTACAO = "portfolio_trade_de_outra_cotacao"
ACAO_TAXA = "portfolio_taxa_descartada"
ACAO_SEM_BASE = "portfolio_venda_sem_base_de_custo"
ACAO_RECONCILIADO = "portfolio_saldo_reconciliado"
ACAO_DIVERGENTE = "portfolio_saldo_divergente_do_historico"
ACAO_SALDO_SEM_BASE = "portfolio_saldo_sem_base_de_custo"

#: O que `DELETE /trades/{id}` grava, com o lancamento inteiro no `before`. E a
#: evidencia de edicao retroativa que `_sustentar` exige (ver la).
ACAO_LANCAMENTO_APAGADO = "manual_trade_deleted"

#: Folga entre o carimbo do retrato anterior e a exclusao. O carimbo e posto
#: DEPOIS da leitura do historico e do saldo (a latencia do `fetch_balance`):
#: uma exclusao gravada nessa janela nao foi vista pelo retrato anterior, mas tem
#: horario anterior ao dele. Sem a folga, a correcao do digito seria lida como
#: prejuizo -- a direcao segura, mas e a trava armando por um typo.
FOLGA_DA_ANCORA = timedelta(minutes=5)

#: Quanto antes do trade o candle da moeda da taxa (BNB) pode ter fechado para
#: valer como a cotacao do instante. Com o timeframe diario, o ultimo candle
#: fechado e de ate 24 h antes; dois dias cobrem uma coleta perdida. A taxa e
#: ~0,075% do valor, entao 5% de oscilacao do BNB nesse intervalo muda o custo em
#: 0,004% -- e semanas de distancia ja nao seriam "o instante".
TOLERANCIA_COTACAO_DA_TAXA = timedelta(days=2)

#: As duas acoes que formam o **razao de fantasmas**: quantidade reconhecida como
#: fora do saldo e credito ja atribuido a ela. O razao mora no `audit_log` porque
#: precisa de tres propriedades ao mesmo tempo -- durar entre reinicios, ser
#: append-only e ser auditavel por uma pessoa --, e o registro do ajuste E o
#: proprio razao. Ler o append-only nao afrouxa o contrato dele: nada aqui faz
#: update nem delete.
ACOES_DO_RAZAO = (ACAO_RECONCILIADO, ACAO_DIVERGENTE)


def _decimal(value: Any) -> Decimal:
    """Normaliza para Decimal (D7): o SQLite devolve `Numeric` como texto/float."""
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _instante(value: datetime | None) -> datetime:
    """Reanexa UTC: o SQLite perde o timezone na ida e volta."""
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class _Aviso:
    """Uma linha de audit_log a gravar, com o dado que o razao le de volta."""

    action: str
    target: str
    detail: str
    after: dict[str, str] = field(default_factory=dict)
    unico: bool = True
    """`True` avisa uma vez por vida do agente; movimento de razao e sempre novo."""


@dataclass
class _Apuracao:
    """O que o historico de trades diz, na moeda de cotacao e no modo corrente."""

    quantidades: dict[str, Decimal]
    custos: dict[str, Decimal]
    realizado: Decimal
    avisos: list[_Aviso]

    def custo_medio(self, asset: str) -> Decimal | None:
        """`None` significa "sem base de custo", e nao zero.

        A diferenca decide stop: `average_price is None` e tratado pelo Risk
        Manager como posicao sem nivel (e avisado); um zero seria lido como
        preco medio zero e nunca disparia stop nenhum.
        """
        quantidade = self.quantidades.get(asset, Decimal(0))
        custo = self.custos.get(asset, Decimal(0))
        if quantidade <= 0 or custo <= 0:
            return None
        return custo / quantidade


class PortfolioAgent(BaseAgent):
    name = "portfolio"

    def __init__(
        self,
        bus: EventBus,
        broker: Broker,
        settings: Settings,
        price_source: Callable[[], dict[str, Decimal]],
        on_snapshot: Callable[[PortfolioSnapshot], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(bus)
        self._broker = broker
        self._settings = settings
        self._price_source = price_source
        """Precos correntes, fornecidos pelo Market Data Agent."""

        self._on_snapshot = on_snapshot
        self.latest: PortfolioSnapshot | None = None
        self._avisados: set[tuple[str, str]] = set()
        """Avisos ja gravados: 1.440 linhas iguais por dia tornariam o registro ilegivel."""

        self._cotacoes_da_taxa: dict[str, Decimal] = {}
        """Cotacao da taxa em terceira moeda ja achada, por id de trade.

        Ela nao muda depois de achada (so vale candle que fechou ANTES do trade),
        entao nao precisa voltar ao banco a cada retrato. O que nao foi achado e
        procurado de novo: um par recem-coletado traz o historico de candles.
        """

    async def _run(self) -> None:
        while True:
            await self.wait_if_paused()
            try:
                await self.build_snapshot()
                # A batida fica DENTRO do try, depois do retrato: chave vazia ou
                # exchange fora do ar tem de aparecer como agente sem vitalidade.
                # Batendo fora do try, o watchdog via saude onde nao havia
                # retrato e o Risk Manager seguia dimensionando ordens contra um
                # retrato de horas atras.
                await self.heartbeat()
            except Exception as exc:
                self.log.exception("portfolio.snapshot_failed", error=str(exc))
            if not await self.sleep(self._settings.trading.portfolio_interval_seconds):
                return

    async def build_snapshot(self) -> PortfolioSnapshot:
        quote = self._settings.trading.quote_currency
        modo = str(self._settings.trading_mode)
        prices = self._price_source()

        # O historico e lido ANTES do saldo. Na ordem inversa, uma venda que
        # preenchia depois do `fetch_balance` e era gravada antes desta sessao
        # entrava duas vezes: no realizado e no nao realizado do saldo velho (o
        # -0,20 de um stop aparecia como -0,40; auditoria de 09/11/2026, M1).
        # Nesta ordem a unica corrida possivel e a outra -- saldo ja sem o
        # ativo, trade ainda nao gravado --, que e o fantasma que o razao trata.
        async with session_scope(self._settings) as session:
            trades = await TradeRepository(session).for_cost_basis()
            ancora = await PortfolioSnapshotRepository(session).latest(mode=modo)
            razao = _ler_razao(
                await PortfolioLedgerRepository(session).movements(ACOES_DO_RAZAO), modo
            )
            apagadas = _vendas_apagadas(
                await PortfolioLedgerRepository(session).movements(
                    (ACAO_LANCAMENTO_APAGADO,)
                ),
                ancora,
            )
            cotacoes = await self._cotacoes_das_taxas(session, [*trades, *apagadas])

        positions = await self._broker.fetch_positions(prices)

        saldo_anterior = _quantidades_do_retrato(ancora)
        if not positions and saldo_anterior:
            # Leitura vazia onde havia posicao e leitura que FALHOU, nao carteira
            # zerada. Publicar esse zero mandaria a trava obedecer a um
            # patrimonio inexistente -- o estado seguro nao e "arrisca menos com
            # numero ruim", e nao produzir numero.
            raise RuntimeError(
                "leitura de carteira vazia com posicao no retrato anterior: "
                f"{', '.join(sorted(saldo_anterior))}; retrato NAO produzido"
            )

        apuracao = self._apurar(trades, cotacoes)
        # O historico como era antes das exclusoes recentes: so ele diz quanto
        # realizado e quanta quantidade a correcao do operador tirou.
        antes_da_edicao = self._apurar([*trades, *apagadas], cotacoes) if apagadas else None
        saldos = {p.asset: p.quantity for p in positions}
        creditos, avisos_razao = self._reconciliar(
            apuracao, saldos, prices, ancora, razao, antes_da_edicao
        )

        cash = Decimal(0)
        positions_value = Decimal(0)
        priced: list[Position] = []
        unpriced: list[str] = []
        avisos_saldo: list[_Aviso] = []

        for position in positions:
            if position.asset == quote:
                cash += position.quantity
                priced.append(position.model_copy(update={"current_price": Decimal(1)}))
                continue

            price = position.current_price or prices.get(position.asset)
            if price is None:
                # Sem preco, o ativo nao entra no total. Chutar um valor
                # inflaria o patrimonio e poderia desarmar o circuit breaker
                # justamente quando ele deveria disparar.
                unpriced.append(position.asset)
                priced.append(position)
                continue

            value = position.quantity * price
            positions_value += value
            medio = apuracao.custo_medio(position.asset)
            com_base: Decimal | None = None
            historico = apuracao.quantidades.get(position.asset, Decimal(0))
            if medio is not None and position.quantity > historico:
                # Saldo acima do historico: o excedente entrou de fora (deposito)
                # e nao tem custo conhecido. Fica no patrimonio, mas fora do nao
                # realizado -- senao o medio do historico aplicado ao deposito
                # move o numero que a trava obedece (auditoria de 09/11/2026,
                # dados M2). O saque ja era coberto pelo razao de fantasmas.
                com_base = historico
                excedente = position.quantity - historico
                avisos_saldo.append(
                    _Aviso(
                        ACAO_SALDO_SEM_BASE,
                        position.asset,
                        f"{excedente} {position.asset} no saldo alem do historico "
                        f"(deposito externo?): sem base de custo, fora do resultado "
                        f"de negociacao; lance a compra para dar base a eles",
                        {
                            "asset": position.asset,
                            "saldo": str(position.quantity),
                            "historico": str(historico),
                        },
                    )
                )
            priced.append(
                position.model_copy(
                    update={
                        "current_price": price,
                        "average_price": medio,
                        "cost_basis_quantity": com_base,
                    }
                )
            )

        if unpriced:
            self.log.warning("portfolio.unpriced_assets", assets=unpriced)

        total = cash + positions_value
        unrealized = sum(
            (p.unrealized_pnl for p in priced if p.asset != quote), start=Decimal(0)
        )
        realized = apuracao.realizado + creditos

        snapshot = PortfolioSnapshot(
            total_value=total,
            cash_value=cash,
            positions_value=positions_value,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            allocations=_allocations(priced, total, quote),
            positions=priced,
        )

        async with session_scope(self._settings) as session:
            await self._registrar(session, apuracao.avisos + avisos_razao + avisos_saldo)
            await PortfolioSnapshotRepository(session).save(snapshot, modo)
            await AgentRunRepository(session).heartbeat(
                self.name, str(self.state), f"patrimonio {total:.2f} {quote}"
            )

        self.latest = snapshot
        await self.bus.publish(Topics.PORTFOLIO_SNAPSHOTS, snapshot)
        if self._on_snapshot is not None:
            await self._on_snapshot(snapshot)

        self.log.info(
            "portfolio.snapshot",
            total=str(round(total, 2)),
            cash=str(round(cash, 2)),
            positions=len([p for p in priced if p.asset != quote]),
            realized_pnl=str(round(realized, 2)),
        )
        return snapshot

    async def _cotacoes_das_taxas(
        self, session: Any, trades: Sequence[Any]
    ) -> dict[str, Decimal]:
        """`cotacoes_das_taxas` com memoria: so procura o que ainda nao achou."""
        quote = self._settings.trading.quote_currency
        faltam = [
            t
            for t in trades
            if str(t.id) not in self._cotacoes_da_taxa
            and _taxa_em_terceira_moeda(t, quote) is not None
        ]
        if faltam:
            self._cotacoes_da_taxa.update(await cotacoes_das_taxas(session, faltam, quote))
        return self._cotacoes_da_taxa

    # ------------------------------------------------------------------
    # Apuracao do historico
    # ------------------------------------------------------------------
    def _apurar(
        self, trades: Iterable[Any], cotacoes_da_taxa: Mapping[str, Decimal] | None = None
    ) -> _Apuracao:
        """Custo medio movel e PnL realizado, derivados do historico de trades.

        A conta mora em `custo_medio_movel`, que a tela Portfolio
        (`analise.resultado_por_par`) usa tambem: uma regra so para o numero que
        decide stop e para o que o operador le por par. Aqui ficam os avisos do
        que foi descartado.

        Nao usa `SUM()` no banco por dois motivos independentes. O primeiro e
        precisao: no SQLite estas colunas sao texto (ver `db.models.Money`), e
        deixar o banco somar forcaria float, reintroduzindo pela agregacao o erro
        que o tipo evita no armazenamento. O segundo e que nao existe coluna a
        somar: `trades.realized_pnl` so e preenchida quando o Execution Agent
        fecha a posicao que ele mesmo abriu.
        """
        quote = self._settings.trading.quote_currency
        modo = str(self._settings.trading_mode)
        percurso = custo_medio_movel(trades, quote, modo, cotacoes_da_taxa)
        quantidades, custos, realizado = (
            percurso.quantidades, percurso.custos, percurso.realizado
        )
        outra_cotacao, outro_modo = percurso.outra_cotacao, percurso.outro_modo
        taxa_descartada, sem_base = percurso.taxa_descartada, percurso.sem_base

        avisos: list[_Aviso] = []
        for cotacao, quantos in sorted(outra_cotacao.items()):
            avisos.append(
                _Aviso(
                    ACAO_COTACAO,
                    cotacao,
                    f"{quantos} trade(s) cotados em {cotacao} fora da apuracao em "
                    f"{quote}: posicao herdada fica sem base de custo, e portanto sem stop",
                    {"cotacao": cotacao, "trades": str(quantos)},
                )
            )
        for outro, quantos in sorted(outro_modo.items()):
            avisos.append(
                _Aviso(
                    ACAO_MODO,
                    outro,
                    f"{quantos} trade(s) do modo {outro} fora da apuracao do modo "
                    f"{modo}: dinheiro de papel nao entra em preco medio nem em "
                    f"resultado de dinheiro real",
                    {"modo_do_trade": outro, "modo_corrente": modo, "trades": str(quantos)},
                )
            )
        for moeda, total in sorted(taxa_descartada.items()):
            avisos.append(
                _Aviso(
                    ACAO_TAXA,
                    moeda,
                    f"{total} {moeda} de taxa descartados do custo em {quote}: sem "
                    f"candle de {moeda}/{quote} fechado ate "
                    f"{TOLERANCIA_COTACAO_DA_TAXA} antes do trade, nao ha cotacao do "
                    f"instante e somar seria erro de unidade",
                    {"moeda": moeda, "total": str(total)},
                )
            )
        for asset, quantidade in sorted(sem_base.items()):
            avisos.append(
                _Aviso(
                    ACAO_SEM_BASE,
                    asset,
                    f"{quantidade} {asset} vendidos sem base de custo no historico: "
                    f"realizado NAO declarado para essa parte, para nao inventar lucro",
                    {"asset": asset, "quantidade": str(quantidade)},
                )
            )

        return _Apuracao(quantidades, custos, realizado, avisos)

    # ------------------------------------------------------------------
    # Reconciliacao do saldo com o historico
    # ------------------------------------------------------------------
    def _reconciliar(
        self,
        apuracao: _Apuracao,
        saldos: dict[str, Decimal],
        precos: dict[str, Decimal],
        ancora: Any,
        razao: dict[str, tuple[Decimal, Decimal]],
        antes_da_edicao: _Apuracao | None = None,
    ) -> tuple[Decimal, list[_Aviso]]:
        """Credito acumulado dos fantasmas, e o movimento do razao desta volta.

        FANTASMA e a quantidade que o historico diz que temos e a exchange nao
        confirma. Ele nasce de tres jeitos, e o tratamento e diferente em cada um:

        1. **A quantidade saiu do saldo e o trade ainda nao foi gravado** (ordem
           em voo: `execution.py` grava o trade DEPOIS de aplicar o resultado, e
           um crash nessa janela a torna permanente). O nao realizado da posicao
           desaparece antes de o realizado aparecer, e a trava lia a diferenca
           como perda do dia sem perda nenhuma. Aqui o fantasma e marcado a
           mercado UMA VEZ, no instante em que aparece, e o credito e guardado no
           razao. Remarcar a cada retrato faria o preco mandar no realizado: o
           credito cairia junto com o mercado e armaria a trava sozinho.
        2. **O historico perdeu uma venda** (`DELETE /trades/{id}`, que a
           interface oferece para corrigir digitacao). O saldo nao mudou, mas o
           realizado derivado caiu -- e a trava leria a correcao de um digito
           como prejuizo. O fantasma que sobrou sustenta o nivel que o retrato
           anterior JA publicou, ate o realizado que a venda apagada produzia
           (`antes_da_edicao`, ver `_sustentar`).
        3. **O historico nunca foi confirmado pela exchange** (divergencia
           herdada, ativo que nunca apareceu no saldo, compra manual de um ativo
           que esta em outra corretora). Nao ha evidencia de que a quantidade
           tenha saido de lugar nenhum, entao nao ha o que reconciliar: fica
           registrada com credito zero e pede acao humana.

        E o credito e DESFEITO quando o fantasma deixa de existir -- o trade
        verdadeiro chegou (e o realizado derivado, com o preco real do
        preenchimento, passa a valer) ou o ativo voltou da carteira fria. Sem o
        estorno, cada ida e volta entre o spot e o Binance Earn somaria o mesmo
        ganho de novo, e a promessa de imunidade a deposito e saque da trava seria
        falsa.
        """
        quote = self._settings.trading.quote_currency
        publicado = _decimal(ancora.realized_pnl) if ancora is not None else None
        saldo_anterior = _quantidades_do_retrato(ancora)

        creditos = Decimal(0)
        avisos: list[_Aviso] = []
        pendentes: list[tuple[str, Decimal, Decimal]] = []
        """(asset, quantidade retroativa, credito ja lancado nesta volta)."""

        for asset in sorted(set(apuracao.quantidades) | set(razao)):
            if asset == quote:
                continue
            historico = apuracao.quantidades.get(asset, Decimal(0))
            saldo = saldos.get(asset, Decimal(0))
            fantasma = max(Decimal(0), historico - saldo)
            registrada, credito_no_razao = razao.get(asset, (Decimal(0), Decimal(0)))
            delta = fantasma - registrada

            if delta == 0:
                creditos += credito_no_razao
                continue

            if delta < 0:
                # O fantasma encolheu: libera a fatia proporcional do credito.
                proporcao = -delta / registrada
                liberado = credito_no_razao * proporcao
                creditos += credito_no_razao - liberado
                avisos.append(
                    _movimento(
                        asset, delta, -liberado, precos.get(asset), apuracao, quote,
                        str(self._settings.trading_mode),
                        f"fantasma de {asset} reduzido em {-delta}: credito de "
                        f"{liberado} estornado porque o historico ou o saldo o explicou",
                    )
                )
                continue

            saiu_do_saldo = max(
                Decimal(0), saldo_anterior.get(asset, Decimal(0)) - saldo
            )
            preco = precos.get(asset)
            creditavel = min(delta, saiu_do_saldo) if preco is not None else Decimal(0)
            medio = apuracao.custo_medio(asset)
            credito_novo = (
                creditavel * (preco - medio)
                if creditavel > 0 and preco is not None and medio is not None
                else Decimal(0)
            )
            creditos += credito_no_razao + credito_novo
            retroativo = delta - creditavel
            if retroativo > 0:
                pendentes.append((asset, retroativo, credito_novo))
                continue
            avisos.append(
                _movimento(
                    asset, delta, credito_novo, preco, apuracao, quote,
                    str(self._settings.trading_mode),
                    f"{creditavel} {asset} sairam do saldo sem trade gravado: "
                    f"marcados a mercado uma unica vez em {credito_novo} {quote}",
                )
            )

        creditos, sustentacao = self._sustentar(
            apuracao, publicado, creditos, pendentes, antes_da_edicao
        )
        for asset, retroativo, credito_novo, sustentado in sustentacao:
            avisos.append(
                _movimento(
                    asset, retroativo, credito_novo + sustentado, precos.get(asset),
                    apuracao, quote, str(self._settings.trading_mode),
                    (
                        f"{retroativo} {asset} apareceram no historico sem sair do "
                        f"saldo: {sustentado} {quote} sustentam o realizado que o "
                        f"retrato anterior ja publicou"
                    )
                    if sustentado != 0
                    else (
                        f"{retroativo} {asset} no historico que a exchange nao "
                        f"confirma: sem evidencia de saida, nada a reconciliar"
                    ),
                )
            )
        return creditos, avisos

    def _sustentar(
        self,
        apuracao: _Apuracao,
        publicado: Decimal | None,
        creditos: Decimal,
        pendentes: list[tuple[str, Decimal, Decimal]],
        antes_da_edicao: _Apuracao | None = None,
    ) -> tuple[Decimal, list[tuple[str, Decimal, Decimal, Decimal]]]:
        """Sustenta o realizado ja publicado quando o historico foi editado atras.

        Um retrato publicado e um fato: o operador viu o numero e a trava mediu
        por ele. Apagar depois um lancamento manual lucrativo nao pode derrubar o
        acumulado -- seria a trava lendo a correcao de um digito como prejuizo, e
        exigindo rearme manual por causa de um typo.

        "Fantasma que apareceu sem o saldo mudar" NAO basta como assinatura da
        edicao: uma compra manual de XRP que a exchange nao tem (operacao em
        outra corretora) tem a mesma cara, e recebia a `falta` inteira do ciclo
        -- no dia em que o stop vendia SOL com -1,60, o realizado publicado nao
        caia e a trava nao via a perda. Por isso a sustentacao exige a evidencia
        da edicao, `antes_da_edicao`: o historico com as VENDAS manuais apagadas
        depois do retrato anterior (o `manual_trade_deleted` guarda o lancamento
        inteiro). E vale somente:

        * para a quantidade que a exclusao devolveu ao ativo (o resto do
          pendente fica com credito zero, como divergencia);
        * ate o realizado que as vendas apagadas produziam -- a perda de verdade
          no mesmo ciclo nao pode pegar carona na correcao;
        * ate o nivel que o retrato anterior publicou.

        Ela e liberada junto com o fantasma: se o lancamento voltar, ou se a
        venda de verdade for gravada, o realizado derivado assume.
        """
        if not pendentes:
            return creditos, []
        falta = Decimal(0)
        elegivel: dict[str, Decimal] = {}
        if publicado is not None and antes_da_edicao is not None:
            falta = max(Decimal(0), publicado - (apuracao.realizado + creditos))
            produzido = antes_da_edicao.realizado - apuracao.realizado
            falta = min(falta, max(Decimal(0), produzido))
            for asset, _, _ in pendentes:
                devolvido = apuracao.quantidades.get(
                    asset, Decimal(0)
                ) - antes_da_edicao.quantidades.get(asset, Decimal(0))
                elegivel[asset] = max(Decimal(0), devolvido)
        pesos = {
            asset: min(quantidade, elegivel.get(asset, Decimal(0)))
            for asset, quantidade, _ in pendentes
        }
        total = sum(pesos.values(), start=Decimal(0))
        movimentos: list[tuple[str, Decimal, Decimal, Decimal]] = []
        for asset, quantidade, credito_novo in pendentes:
            sustentado = falta * pesos[asset] / total if total > 0 else Decimal(0)
            creditos += sustentado
            movimentos.append((asset, quantidade, credito_novo, sustentado))
        return creditos, movimentos

    async def _registrar(self, session: Any, avisos: Sequence[_Aviso]) -> None:
        """Grava o audit_log, sem repetir o mesmo aviso a cada retrato."""
        log = AuditLogRepository(session)
        for aviso in avisos:
            chave = (aviso.action, aviso.target)
            if aviso.unico and chave in self._avisados:
                continue
            self._avisados.add(chave)
            if aviso.action == ACAO_TAXA:
                # Taxa em terceira moeda (BNB) sem candle gravado fica fora do
                # custo: o medio sai subestimado e o stop um pouco mais baixo.
                # Alem do audit_log, sai como aviso no log de operacao, para nao
                # passar calado.
                self.log.warning(
                    "portfolio.taxa_descartada", moeda=aviso.target, detail=aviso.detail
                )
            await log.append(
                action=aviso.action,
                actor=self.name,
                target=aviso.target,
                after=dict(aviso.after),
                detail=aviso.detail,
            )


def _movimento(
    asset: str,
    quantidade: Decimal,
    credito: Decimal,
    preco: Decimal | None,
    apuracao: _Apuracao,
    quote: str,
    modo: str,
    detail: str,
) -> _Aviso:
    """Uma linha do razao de fantasmas.

    Credito zero recebe a acao de DIVERGENCIA, e nao de reconciliacao: nada foi
    reconciliado ali, e o operador precisa distinguir "o sistema corrigiu a
    contabilidade" de "o historico e o saldo discordam e ninguem sabe por que".
    """
    medio = apuracao.custo_medio(asset)
    acao = ACAO_DIVERGENTE if credito == 0 and quantidade > 0 else ACAO_RECONCILIADO
    return _Aviso(
        acao,
        f"{modo}:{asset}",
        detail,
        {
            "mode": modo,
            "asset": asset,
            "quantidade": str(quantidade),
            "credito": str(credito),
            "preco": str(preco) if preco is not None else "",
            "custo_medio": str(medio) if medio is not None else "",
            "cotacao": quote,
        },
        unico=False,
    )


@dataclass(frozen=True)
class _Passo:
    """O efeito de UM trade na apuracao -- o que a tela soma por periodo."""

    trade: Any
    compra: bool
    taxa: Decimal
    """Na moeda de cotacao (zero quando descartada)."""

    realizado: Decimal
    """Da venda: `vendido x (preco - medio) - taxa`. Zero na compra."""

    sem_base: bool
    """Venda sem custo nenhum no historico: nao e ganho nem perda."""


@dataclass
class _Percurso:
    """O historico percorrido por custo medio movel, com o que ficou de fora."""

    quantidades: dict[str, Decimal] = field(default_factory=dict)
    custos: dict[str, Decimal] = field(default_factory=dict)
    realizado: Decimal = Decimal(0)
    passos: list[_Passo] = field(default_factory=list)
    outra_cotacao: dict[str, int] = field(default_factory=dict)
    outro_modo: dict[str, int] = field(default_factory=dict)
    taxa_descartada: dict[str, Decimal] = field(default_factory=dict)
    sem_base: dict[str, Decimal] = field(default_factory=dict)


def custo_medio_movel(
    trades: Iterable[Any],
    quote: str,
    modo: str,
    cotacoes_da_taxa: Mapping[str, Decimal] | None = None,
) -> _Percurso:
    """A regra de custo medio, UMA vez: o retrato e a tela Portfolio usam esta.

    Eram duas copias linha a linha (`_apurar` e `analise.resultado_por_par`), e a
    proxima correcao entraria numa so -- a regra que decide stop divergindo da
    que o operador le por par (auditoria de 09/11/2026, dados).

    Percorre em ordem cronologica: compra soma ao custo (com a taxa, que e
    dinheiro que saiu para adquirir o ativo), venda realiza contra o custo
    medio vigente e retira do custo a parte proporcional. O medio nao se move
    numa venda parcial -- e isso que faz `2 * (500 - 175) = 650` de realizado
    conviver com `175` de medio nas duas unidades que sobraram.

    `cotacoes_da_taxa` (id do trade -> preco da moeda da taxa na cotacao, ver
    `cotacoes_das_taxas`) converte a taxa em terceira moeda (BNB).
    """
    cotacoes = cotacoes_da_taxa or {}
    p = _Percurso()
    for trade in _em_ordem(trades):
        asset, _, cotacao = trade.symbol.partition("/")
        if (cotacao or quote) != quote:
            # 401.128 BRL nao entra num medio em USDT: seria erro de unidade,
            # e a posicao herdada da troca de cotacao (D24) fica sem base de
            # custo -- portanto sem stop -- o que o Risk Manager avisa.
            p.outra_cotacao[cotacao or "?"] = p.outra_cotacao.get(cotacao or "?", 0) + 1
            continue

        trade_modo = trade.mode or modo
        if trade_modo not in (modo, MODO_MANUAL):
            p.outro_modo[trade_modo] = p.outro_modo.get(trade_modo, 0) + 1
            continue

        preco = _decimal(trade.price)
        quantidade, taxa, descartada = _quantidade_e_taxa(
            trade, asset, quote, cotacoes.get(str(trade.id))
        )
        if descartada is not None:
            moeda_da_taxa, valor = descartada
            p.taxa_descartada[moeda_da_taxa] = (
                p.taxa_descartada.get(moeda_da_taxa, Decimal(0)) + valor
            )

        mantido = p.quantidades.get(asset, Decimal(0))
        custo = p.custos.get(asset, Decimal(0))

        if trade.side == str(Side.BUY):
            p.quantidades[asset] = mantido + quantidade
            p.custos[asset] = custo + quantidade * preco + taxa
            p.passos.append(_Passo(trade, True, taxa, Decimal(0), False))
            continue

        # Venda: a taxa e dinheiro que saiu, entao entra no realizado mesmo
        # quando nao ha base de custo para a quantidade vendida.
        realizado_da_venda = -taxa
        if mantido <= 0:
            # Saldo preexistente vendido: sem custo conhecido nao ha lucro a
            # declarar. Declarar o preco cheio como lucro empurraria o
            # resultado de negociacao para cima e esconderia prejuizo real do
            # circuit breaker.
            p.sem_base[asset] = p.sem_base.get(asset, Decimal(0)) + quantidade
            p.realizado += realizado_da_venda
            p.passos.append(_Passo(trade, False, taxa, realizado_da_venda, True))
            continue

        medio = custo / mantido
        vendido = min(quantidade, mantido)
        realizado_da_venda += vendido * (preco - medio)
        restante = mantido - vendido
        p.quantidades[asset] = restante
        # Fechamento total zera o custo EXATO: `custo - vendido * medio`
        # deixaria residuo de dizima (1E-27) onde o correto e zero.
        p.custos[asset] = custo - vendido * medio if restante > 0 else Decimal(0)
        if quantidade > vendido:
            p.sem_base[asset] = p.sem_base.get(asset, Decimal(0)) + (quantidade - vendido)
        p.realizado += realizado_da_venda
        p.passos.append(_Passo(trade, False, taxa, realizado_da_venda, False))
    return p


def _taxa_em_terceira_moeda(trade: Any, quote: str) -> str | None:
    """A moeda da taxa quando ela nao e nem a cotacao nem o ativo do trade."""
    asset, _, cotacao = str(trade.symbol).partition("/")
    moeda = trade.fee_currency or quote
    if (cotacao or quote) != quote or moeda in (quote, asset):
        return None
    if _decimal(trade.fee) <= 0:
        return None
    return moeda


async def cotacoes_das_taxas(
    session: Any, trades: Iterable[Any], quote: str
) -> dict[str, Decimal]:
    """Id do trade -> preco da moeda da taxa (BNB) na cotacao, no instante do trade.

    O preco e o fechamento do ultimo candle de `<moeda>/<quote>` que JA tinha
    fechado no instante do trade, de qualquer timeframe gravado, e so vale se
    fechou ate `TOLERANCIA_COTACAO_DA_TAXA` antes. Trade sem cotacao fica fora do
    dicionario: a taxa dele e descartada com aviso, como antes.

    So existe candle de BNB se o par BNB/<quote> for coletado (universo de
    negociacao ou `SYMBOLS`). Fora dele, nada muda em relacao ao descarte.
    """
    cotacoes: dict[str, Decimal] = {}
    repositorio = CandleRepository(session)
    for trade in trades:
        moeda = _taxa_em_terceira_moeda(trade, quote)
        if moeda is None:
            continue
        instante = _instante(trade.executed_at)
        achado = await repositorio.ultimo_fechamento_ate(f"{moeda}/{quote}", instante)
        if achado is None:
            continue
        fechou, preco = achado
        if instante - fechou <= TOLERANCIA_COTACAO_DA_TAXA and preco > 0:
            cotacoes[str(trade.id)] = preco
    return cotacoes


def _quantidade_e_taxa(
    trade: Any, asset: str, quote: str, cotacao_da_taxa: Decimal | None = None
) -> tuple[Decimal, Decimal, tuple[str, Decimal] | None]:
    """(quantidade que moveu o saldo do ativo, taxa na cotacao, taxa descartada).

    A Binance spot sem BNB cobra a taxa da COMPRA no ativo recebido: `filled` e
    0,001 BTC, a taxa e 0,000001 BTC e o saldo sobe 0,000999. Tratada como
    "outra moeda" (descartada), a quantidade bruta ficava no historico e a
    diferenca virava um fantasma sem saida de saldo, que `_sustentar` lia como
    edicao retroativa e usava para apagar o prejuizo realizado do dia
    (auditoria de 09/11/2026, H2). Aqui a taxa no ativo base sai da quantidade
    (compra entra liquida; venda tira a taxa junto) e entra no dinheiro pelo
    preco do PROPRIO trade, que e a cotacao exata do instante. Na compra isso da
    `custo = quantidade bruta x preco`: a taxa fica embutida no medio.

    Taxa em terceira moeda (BNB) nao mexe na quantidade do ativo e entra no
    dinheiro pela `cotacao_da_taxa` -- o fechamento do candle gravado no instante
    do trade (`cotacoes_das_taxas`). Ate 10/11/2026 ela era sempre descartada
    ("a cotacao do instante nao e guardada"), mas os candles de BNB estao no
    banco: o medio saia subestimado e a taxa da venda ficava fora do realizado.
    Sem cotacao, continua descartada -- somar 0,01 BNB como 0,01 USDT inventaria
    custo -- e volta como descartada para o chamador deixar o rastro.
    """
    quantidade = _decimal(trade.quantity)
    taxa = _decimal(trade.fee)
    moeda = trade.fee_currency or quote
    if taxa <= 0 or moeda == quote:
        return quantidade, taxa, None
    if moeda == asset:
        compra = trade.side == str(Side.BUY)
        no_saldo = quantidade - taxa if compra else quantidade + taxa
        return no_saldo, taxa * _decimal(trade.price), None
    if cotacao_da_taxa is not None and cotacao_da_taxa > 0:
        return quantidade, taxa * cotacao_da_taxa, None
    return quantidade, Decimal(0), (moeda, taxa)


def _ler_razao(
    entries: Iterable[Any], modo: str
) -> dict[str, tuple[Decimal, Decimal]]:
    """Soma o razao de fantasmas por ativo: (quantidade, credito).

    Filtra pelo modo: o realizado nao atravessa a virada de modo, e ancorar o
    modo real no razao do ensaio seria a contaminacao entrando pela porta de tras.
    """
    razao: dict[str, tuple[Decimal, Decimal]] = {}
    for entry in entries:
        dados = entry.after or {}
        if dados.get("mode") != modo:
            continue
        asset = dados.get("asset")
        if not asset:
            continue
        quantidade, credito = razao.get(asset, (Decimal(0), Decimal(0)))
        razao[asset] = (
            quantidade + _decimal(dados.get("quantidade")),
            credito + _decimal(dados.get("credito")),
        )
    return razao


def _vendas_apagadas(entries: Iterable[Any], ancora: Any) -> list[Any]:
    """VENDAS manuais apagadas depois do retrato anterior, como trades de novo.

    O `before` do `manual_trade_deleted` e o lancamento inteiro
    (`api/routes/trades.py`), entao da para reapurar o historico como ele era
    antes da correcao. Exclusao antiga nao entra: o retrato que a viu ja
    publicou o realizado sem ela. Linha sem os campos (o registro antigo
    guardava so o id) fica de fora -- sem ela a sustentacao nao acontece, que e
    a direcao segura.
    """
    if ancora is None:
        return []
    desde = _instante(ancora.timestamp) - FOLGA_DA_ANCORA
    vendas: list[Any] = []
    for entry in entries:
        if _instante(entry.timestamp) <= desde:
            continue
        dados = entry.before or {}
        if dados.get("side") != str(Side.SELL):
            continue
        try:
            vendas.append(
                SimpleNamespace(
                    id=str(dados.get("id") or entry.target or ""),
                    symbol=str(dados["symbol"]),
                    side=str(Side.SELL),
                    quantity=dados["quantity"],
                    price=dados["price"],
                    fee=dados.get("fee"),
                    fee_currency=dados.get("fee_currency"),
                    mode=dados.get("mode"),
                    executed_at=datetime.fromisoformat(str(dados["executed_at"])),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return vendas


def _quantidades_do_retrato(ancora: Any) -> dict[str, Decimal]:
    """Saldo que a exchange respondeu no retrato anterior, por ativo."""
    if ancora is None:
        return {}
    quantidades: dict[str, Decimal] = {}
    for posicao in ancora.positions or []:
        asset = posicao.get("asset")
        if asset:
            quantidades[asset] = _decimal(posicao.get("quantity"))
    return quantidades


def _em_ordem(trades: Iterable[Any]) -> list[Any]:
    """Ordem cronologica, com COMPRA antes de VENDA quando o instante empata.

    Lancamento manual com data sem hora empata com facilidade, e o desempate nao
    pode ser do banco: apurada a venda antes da compra do mesmo instante, o preco
    medio vira o da compra restante e aparece lucro que nao existe.
    """
    return sorted(
        trades,
        key=lambda t: (
            _instante(t.executed_at),
            0 if t.side == str(Side.BUY) else 1,
            t.id or "",
        ),
    )


def _allocations(
    positions: list[Position], total: Decimal, quote: str
) -> dict[str, float]:
    """Percentual do patrimonio por ativo, para o grafico de pizza."""
    if total <= 0:
        return {}
    result: dict[str, float] = {}
    for position in positions:
        value = position.quantity if position.asset == quote else position.market_value
        if value > 0:
            result[position.asset] = float(value / total)
    return result
