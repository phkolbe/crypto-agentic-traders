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

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ..bus import EventBus, Topics
from ..config import Settings
from ..db.repositories import (
    AgentRunRepository,
    AuditLogRepository,
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
        positions = await self._broker.fetch_positions(prices)

        async with session_scope(self._settings) as session:
            trades = await TradeRepository(session).for_cost_basis()
            ancora = await PortfolioSnapshotRepository(session).latest(mode=modo)
            razao = _ler_razao(
                await PortfolioLedgerRepository(session).movements(ACOES_DO_RAZAO), modo
            )

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

        apuracao = self._apurar(trades)
        saldos = {p.asset: p.quantity for p in positions}
        creditos, avisos_razao = self._reconciliar(
            apuracao, saldos, prices, ancora, razao
        )

        cash = Decimal(0)
        positions_value = Decimal(0)
        priced: list[Position] = []
        unpriced: list[str] = []

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
            priced.append(
                position.model_copy(
                    update={
                        "current_price": price,
                        "average_price": apuracao.custo_medio(position.asset),
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
            await self._registrar(session, apuracao.avisos + avisos_razao)
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

    # ------------------------------------------------------------------
    # Apuracao do historico
    # ------------------------------------------------------------------
    def _apurar(self, trades: Iterable[Any]) -> _Apuracao:
        """Custo medio movel e PnL realizado, derivados do historico de trades.

        Percorre em ordem cronologica: compra soma ao custo (com a taxa, que e
        dinheiro que saiu para adquirir o ativo), venda realiza contra o custo
        medio vigente e retira do custo a parte proporcional. O medio nao se move
        numa venda parcial -- e isso que faz `2 * (500 - 175) = 650` de realizado
        conviver com `175` de medio nas duas unidades que sobraram.

        Nao usa `SUM()` no banco por dois motivos independentes. O primeiro e
        precisao: no SQLite estas colunas sao texto (ver `db.models.Money`), e
        deixar o banco somar forcaria float, reintroduzindo pela agregacao o erro
        que o tipo evita no armazenamento. O segundo e que nao existe coluna a
        somar: `trades.realized_pnl` so e preenchida quando o Execution Agent
        fecha a posicao que ele mesmo abriu.
        """
        quote = self._settings.trading.quote_currency
        modo = str(self._settings.trading_mode)

        quantidades: dict[str, Decimal] = {}
        custos: dict[str, Decimal] = {}
        realizado = Decimal(0)

        outra_cotacao: dict[str, int] = {}
        outro_modo: dict[str, int] = {}
        taxa_descartada: dict[str, Decimal] = {}
        sem_base: dict[str, Decimal] = {}

        for trade in _em_ordem(trades):
            asset, _, cotacao = trade.symbol.partition("/")
            if (cotacao or quote) != quote:
                # 401.128 BRL nao entra num medio em USDT: seria erro de unidade,
                # e a posicao herdada da troca de cotacao (D24) fica sem base de
                # custo -- portanto sem stop -- o que o Risk Manager avisa.
                outra_cotacao[cotacao or "?"] = outra_cotacao.get(cotacao or "?", 0) + 1
                continue

            trade_modo = trade.mode or modo
            if trade_modo not in (modo, MODO_MANUAL):
                outro_modo[trade_modo] = outro_modo.get(trade_modo, 0) + 1
                continue

            quantidade = _decimal(trade.quantity)
            preco = _decimal(trade.price)
            taxa = _decimal(trade.fee)
            moeda_da_taxa = trade.fee_currency or quote
            if taxa > 0 and moeda_da_taxa != quote:
                # 0,01 BNB nao e 0,01 USDT. Converter exigiria a cotacao do
                # instante do trade, que nao esta guardada; somar como se fosse a
                # mesma moeda inventaria custo.
                taxa_descartada[moeda_da_taxa] = (
                    taxa_descartada.get(moeda_da_taxa, Decimal(0)) + taxa
                )
                taxa = Decimal(0)

            mantido = quantidades.get(asset, Decimal(0))
            custo = custos.get(asset, Decimal(0))

            if trade.side == str(Side.BUY):
                quantidades[asset] = mantido + quantidade
                custos[asset] = custo + quantidade * preco + taxa
                continue

            # Venda: a taxa e dinheiro que saiu, entao entra no realizado mesmo
            # quando nao ha base de custo para a quantidade vendida.
            realizado -= taxa
            if mantido <= 0:
                # Saldo preexistente vendido: sem custo conhecido nao ha lucro a
                # declarar. Declarar o preco cheio como lucro empurraria o
                # resultado de negociacao para cima e esconderia prejuizo real do
                # circuit breaker.
                sem_base[asset] = sem_base.get(asset, Decimal(0)) + quantidade
                continue

            medio = custo / mantido
            vendido = min(quantidade, mantido)
            realizado += vendido * (preco - medio)
            restante = mantido - vendido
            quantidades[asset] = restante
            # Fechamento total zera o custo EXATO: `custo - vendido * medio`
            # deixaria residuo de dizima (1E-27) onde o correto e zero.
            custos[asset] = custo - vendido * medio if restante > 0 else Decimal(0)
            if quantidade > vendido:
                sem_base[asset] = sem_base.get(asset, Decimal(0)) + (quantidade - vendido)

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
                    f"{total} {moeda} de taxa descartados do custo em {quote}: "
                    f"sem a cotacao do instante do trade, somar seria erro de unidade",
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
           anterior JA publicou.
        3. **O historico nunca foi confirmado pela exchange** (divergencia
           herdada, ativo que nunca apareceu no saldo). Nao ha evidencia de que a
           quantidade tenha saido de lugar nenhum, entao nao ha o que reconciliar:
           fica registrada com credito zero e pede acao humana.

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
            apuracao, publicado, creditos, pendentes
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
    ) -> tuple[Decimal, list[tuple[str, Decimal, Decimal, Decimal]]]:
        """Sustenta o realizado ja publicado quando o historico foi editado atras.

        Um retrato publicado e um fato: o operador viu o numero e a trava mediu
        por ele. Apagar depois um lancamento manual lucrativo nao pode derrubar o
        acumulado -- seria a trava lendo a correcao de um digito como prejuizo, e
        exigindo rearme manual por causa de um typo.

        A sustentacao vale somente para a parte do fantasma que apareceu SEM que
        o saldo mudasse (a assinatura da edicao retroativa) e somente ate o nivel
        que o retrato anterior publicou. Ela e liberada junto com o fantasma: se o
        lancamento voltar, ou se a venda de verdade for gravada, o realizado
        derivado assume.
        """
        if not pendentes:
            return creditos, []
        falta = Decimal(0)
        if publicado is not None:
            falta = max(Decimal(0), publicado - (apuracao.realizado + creditos))
        total = sum((quantidade for _, quantidade, _ in pendentes), start=Decimal(0))
        movimentos: list[tuple[str, Decimal, Decimal, Decimal]] = []
        for asset, quantidade, credito_novo in pendentes:
            sustentado = falta * quantidade / total if total > 0 else Decimal(0)
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
