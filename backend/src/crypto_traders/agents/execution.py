"""Execution Agent.

Unico componente autorizado a enviar ordens. Nao decide nada: recebe
`OrderRequest` ja aprovados e cuida de enviar, reconciliar e registrar.

Ordem das operacoes, que importa:

1. Recusa o que nao passou pelo Risk Manager (`risk_event_id` vazio).
2. Recusa ABERTURA com a trava do D4 armada, ou sem stop-loss.
3. Recusa o que morreria nos filtros da exchange (LOT_SIZE/MIN_NOTIONAL).
4. Verifica idempotencia (`client_order_id` ja usado?).
5. Grava a ordem como PENDING **antes** de chamar a exchange.
6. Le a trava do D4 **de novo**, agora colada no envio.
7. Envia.
8. Grava o resultado e, se preenchida, o trade no historico consolidado.

O passo 5 antes do 7 e deliberado: se o processo morrer entre o envio e a
confirmacao, existe registro local de que a ordem foi tentada. O inverso
deixaria uma ordem viva na exchange e invisivel aqui.

O passo 6 existe porque o passo 5 tem um `await` no meio: sem ele a trava lida
uma vez no passo 2 nao via o circuit breaker que disparava durante a gravacao, e
a compra saia depois da trava armar (medido em 2026-09-09).

Regra que atravessa este arquivo: **nunca afirmar o que nao se sabe.** Uma
chamada que nao voltou nao prova que a ordem nao aconteceu, e gravar `FAILED`
nesse caso e uma afirmacao falsa com consequencia em dinheiro -- foi medido em
2026-09-09 (ver `_classificar`).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from ..bus import EventBus, Topics
from ..config import Settings
from ..db.repositories import AuditLogRepository, OrderRepository, TradeRepository
from ..db.session import session_scope
from ..domain.enums import OrderStatus, Side, TradeOrigin
from ..domain.models import OrderRequest, OrderResult
from ..exchanges.base import ApiAccessDenied, Broker, InsufficientFunds
from ..exchanges.filters import (
    FONTE_AO_VIVO,
    MarketFilterSource,
    OrderViability,
    check_quantity_viability,
)
from .base import BaseAgent

#: A exchange dizendo que o `clientOrderId` ja existe e PROVA de que a ordem
#: chegou e foi criada -- e nao motivo para registrar falha. Na Binance vem como
#: `-2010 Duplicate order sent`, e e exatamente o que acontece quando o cliente
#: sofre timeout depois de a exchange aceitar e o ccxt reenvia com o mesmo id.
_ORDEM_JA_EXISTE = re.compile(r"duplicate order|duplicada|-2010", re.IGNORECASE)


class ExecutionAgent(BaseAgent):
    name = "execution"

    def __init__(self, bus: EventBus, broker: Broker, settings: Settings) -> None:
        super().__init__(bus)
        self._broker = broker
        self._settings = settings
        self._mode = str(settings.trading_mode)
        self._access_denied_since: datetime | None = None
        """Marca o incidente de acesso negado em curso.

        Existe para alertar **uma vez por incidente**, e nao a cada ordem: com o
        IP fora da whitelist, toda ordem falha, e um alerta por ordem viraria
        spam que faz o operador ignorar justamente o aviso que importa.
        """

        self._openings_blocked: str | None = None
        """Motivo pelo qual ABRIR posicao esta proibido agora (D4).

        Existe porque a trava tem que barrar a abertura sem nunca barrar o
        fechamento, e pausar o agente inteiro nao consegue as duas coisas.
        Medido: com o agente rodando (que e o certo, para o stop-loss sair), uma
        compra ja aprovada e ainda na fila era executada DEPOIS do circuit
        breaker disparar. O lado da ordem decide: `BUY` e recusado, `SELL`
        continua passando.

        Uma leitura so nao bastava, e a primeira versao disto tinha exatamente
        esse furo: `_execute` lia a trava e depois passava por dois `await`
        (gravar o PENDING e enviar) antes de gastar dinheiro, e
        `orchestrator.pause_all` roda no MESMO event loop -- entao a trava podia
        armar durante a gravacao e a compra saia. Medido em 2026-09-09, com a
        trava armada como ultimo ato de `create_pending`: sequencia
        `['trava armada', 'ordem enviada']`, saldo com 0,001 BTC comprado depois
        do disparo. Por isso `_execute` le esta variavel DUAS vezes, e a segunda
        leitura fica colada no `place_order`.
        """

        self._blocked_alerted = False

        self._alertado_por_simbolo: set[tuple[str, str]] = set()
        """Alertas de recusa ja emitidos, por (tipo, simbolo).

        Existe porque recusa por filtro NAO e evento unico: poeira abaixo do
        MIN_NOTIONAL faz o Risk Manager reemitir a venda de protecao a cada
        ciclo, e sem freio cada ciclo publicava um `position_trapped` novo. E a
        mesma patologia dos 17 reinicios em 16 minutos do ensaio (D25), aplicada
        justamente ao alerta que menos pode ser ignorado -- o que avisa que o
        stop-loss nao tem como sair. O registro no `audit_log` continua a cada
        ciclo: o freio e do ALERTA, nunca do registro.

        A chave sai daqui quando uma ordem do mesmo par consegue ser enviada:
        acabou o episodio, e um novo aprisionamento tem que alertar de novo.
        """

    @property
    def broker(self) -> Broker:
        return self._broker

    @property
    def openings_blocked(self) -> str | None:
        return self._openings_blocked

    def block_openings(self, reason: str) -> None:
        """Proibe abertura de posicao. Fechamento continua passando."""
        if self._openings_blocked == reason:
            return
        self._openings_blocked = reason
        self._blocked_alerted = False
        self.log.warning("execution.openings_blocked", reason=reason)

    def allow_openings(self) -> None:
        if self._openings_blocked is None:
            return
        self.log.warning("execution.openings_allowed", before=self._openings_blocked)
        self._openings_blocked = None
        self._blocked_alerted = False

    @property
    def access_denied(self) -> bool:
        """True enquanto a exchange estiver recusando a credencial."""
        return self._access_denied_since is not None

    async def _run(self) -> None:
        async for request in self.bus.subscribe(Topics.ORDER_REQUESTS):
            await self.wait_if_paused()
            try:
                await self._execute(request)
            except Exception as exc:
                self.log.exception(
                    "execution.failed", symbol=request.symbol, error=str(exc)
                )
            await self.heartbeat()

    async def _execute(self, request: OrderRequest) -> OrderResult | None:
        if not request.risk_event_id:
            # Nao deveria acontecer -- o tipo exige o campo -- mas se acontecer,
            # e uma ordem que nao passou pelo guardiao e nao pode ser enviada.
            self.log.error("execution.unapproved_order_blocked", symbol=request.symbol)
            return None

        if self._openings_blocked is not None and request.side is Side.BUY:
            # Ultima barreira da trava, e a unica que pega o pedido que ja
            # estava publicado quando ela disparou. Recusa antes de gravar
            # PENDING: a ordem nao existiu, e nao existiu por um motivo
            # registrado.
            await self._refuse_opening(request)
            return None

        if request.side is Side.BUY and request.stop_loss is None:
            # Ultimo portao do item "toda ordem carrega stop-loss antes de ser
            # enviada". A garantia existia so a montante (`risk/rules.py` sempre
            # preenche na aprovacao de abertura), e garantia a montante nao e
            # portao: bastava um caminho novo publicando `OrderRequest` para uma
            # abertura sem protecao ser enviada e preenchida -- e tres fabricas
            # de teste do repositorio provavam que o caminho aceitava.
            #
            # A recusa e assimetrica de proposito, e a assimetria e a mesma de
            # D4: FECHAMENTO nao tem stop-loss por construcao (a saida de
            # protecao do Risk Manager publica `stop_loss=None`), e barrar venda
            # aqui seria matar exatamente o stop-loss que se quer garantir.
            await self._refuse_without_stop(request)
            return None

        viability = self._preflight(request)
        if viability is not None and not viability.viable:
            # Recusa ANTES de gravar PENDING, pelo mesmo motivo da trava: a
            # ordem nao existiu. O que nao pode acontecer e enviar para a
            # exchange algo que ela vai recusar por filtro -- isso queima o
            # rate limit, polui o historico com falhas e, no caso de uma venda
            # de protecao, esconde o fato de a posicao estar presa.
            await self._refuse_by_filter(request, viability)
            return None

        duplicada = False
        try:
            async with session_scope(self._settings) as session:
                orders = OrderRepository(session)
                if await orders.find_by_client_id(request.client_order_id) is not None:
                    duplicada = True
                else:
                    await orders.create_pending(request, self._mode)
        except IntegrityError:
            # `uq_order_client_id` pegou uma corrida: dois executores tentando o
            # mesmo `client_order_id` ao mesmo tempo. Acontece de verdade --
            # reiniciar o agente deixa, por um instante, dois assinantes do
            # mesmo topico. A trava do banco e a autoridade final, e perder a
            # corrida tem que ser um nao-evento silencioso, nunca uma excecao
            # que sobe e vira "execution.failed" no log.
            duplicada = True

        if duplicada:
            self.log.warning(
                "execution.duplicate_ignored", client_order_id=request.client_order_id
            )
            return None

        if self._openings_blocked is not None and request.side is Side.BUY:
            # SEGUNDA leitura da trava, e a que fecha a janela. Medido em
            # 2026-09-09: entre a primeira leitura e o envio existem dois
            # `await` (a gravacao do PENDING acima e o proprio `place_order`), e
            # `block_openings` roda de dentro de uma corrotina no MESMO event
            # loop (`orchestrator.pause_all`), logo pode armar enquanto este
            # `_execute` esta suspenso no round-trip do aiosqlite. A sequencia
            # medida era `['trava armada', 'ordem enviada']`: a compra saia
            # DEPOIS do circuit breaker disparar, com a ordem ainda em casa.
            #
            # A ordem PENDING gravada agora precisa ser encerrada aqui mesmo.
            # Deixa-la PENDING pediria reconciliacao de uma ordem que nunca
            # existiu na exchange, e PENDING e o status reservado para "tentada,
            # sem confirmacao" -- que nao e o caso.
            await self._refuse_opening(request, pending_gravado=True)
            return None

        # Uma ordem que consegue ser enviada encerra o episodio de recusa deste
        # par: se o mesmo par voltar a ficar preso depois, tem que alertar de
        # novo.
        self._alertado_por_simbolo = {
            chave for chave in self._alertado_por_simbolo if chave[1] != request.symbol
        }

        self.log.info(
            "execution.sending",
            symbol=request.symbol,
            side=str(request.side),
            quantity=str(request.quantity),
            mode=self._mode,
        )

        denied: ApiAccessDenied | None = None
        try:
            result = await self._broker.place_order(request)
        except ApiAccessDenied as exc:
            denied = exc
            result = self._failed(request, str(exc))
        except InsufficientFunds as exc:
            # A exchange respondeu, e a resposta foi "nao". Desfecho definitivo:
            # nenhuma ordem foi criada, e retentar nao muda nada.
            result = self._rejected(request, str(exc))
        except Exception as exc:
            # A chamada nao voltou. Isso NAO prova que a ordem nao aconteceu.
            result = self._unknown(request, f"{type(exc).__name__}: {exc}")

        result = self._classificar(request, result)
        await self._record(request, result)
        await self.bus.publish(Topics.ORDER_RESULTS, result)

        if result.status is OrderStatus.PENDING:
            await self._report_unknown_outcome(request, result)
        elif denied is not None:
            await self._report_access_denied(denied, request)
        elif result.status is not OrderStatus.FAILED:
            # Uma ordem que voltou da exchange prova que o acesso voltou.
            await self._report_access_restored()

        return result

    # ------------------------------------------------------------------
    # Filtros da exchange (LOT_SIZE / MIN_NOTIONAL)
    # ------------------------------------------------------------------
    def _preflight(self, request: OrderRequest) -> OrderViability | None:
        """Simula os filtros da exchange antes de enviar.

        Devolve `None` quando nao ha informacao -- broker que nao implementa
        `MarketFilterSource`, ou par que nem o catalogo versionado conhece.
        Ausencia de dado nunca vira aprovacao: vira "sem checagem", e a ordem
        segue o caminho antigo. So recusa com dado concreto na mao.

        Os dois brokers do projeto respondem: `PaperBroker` e `CcxtExchange`
        implementam `market_filter`, e os dois caem no catalogo versionado
        quando nao tem dado ao vivo. Isso importa mais do que parece -- a
        checagem existiu por um dia inteiro sem UM chamador que carregasse
        filtro, o que a deixava passando `None` aqui em toda configuracao real.
        """
        broker = self._broker
        if not isinstance(broker, MarketFilterSource):
            return None
        market = broker.market_filter(request.symbol)
        if market is None:
            return None

        # Preco de referencia: o limite, quando existe; senao o preco implicito
        # na aprovacao (notional/quantidade), que e o preco que o Risk Manager
        # usou para dimensionar.
        price = request.price
        if price is None and request.quantity > 0:
            price = request.notional / request.quantity
        if price is None or price <= 0:
            return None
        return check_quantity_viability(market, request.quantity, price)

    async def _refuse_by_filter(
        self, request: OrderRequest, viability: OrderViability
    ) -> None:
        """Recusa a ordem que morreria nos filtros da exchange, com o motivo.

        A assimetria com a venda e deliberada. Compra recusada e problema de
        configuracao: o valor por ordem esta pequeno demais para o passo do
        lote daquele par. Venda recusada e muito pior -- significa posicao
        PRESA: a exchange nao aceita vender esse tamanho, entao stop-loss e
        take-profit nao tem como sair, e isso precisa de alerta proprio.
        """
        presa = request.side is Side.SELL
        tipo = "position_trapped" if presa else "order_below_exchange_filter"
        motivo = viability.explain(self._settings.trading.quote_currency)
        self.log.error(
            "execution.refused_by_exchange_filter",
            symbol=request.symbol,
            side=str(request.side),
            client_order_id=request.client_order_id,
            quantity=str(request.quantity),
            effective_amount=str(viability.effective_amount),
            effective_notional=str(viability.effective_notional),
            min_cost=str(viability.min_cost),
            filter_source=viability.source,
            posicao_presa=presa,
            reason=viability.reason,
        )
        await self._audit(
            action=tipo,
            request=request,
            detail=motivo,
            extra={
                "effective_notional": str(viability.effective_notional),
                "min_cost": str(viability.min_cost),
                "suggested_notional": str(viability.suggested_notional),
                "filter_source": viability.source,
            },
        )

        if not self._deve_alertar(tipo, request.symbol):
            # Mesmo par, mesmo episodio: o registro acima ja foi gravado, e
            # repetir o alerta a cada ciclo so ensina o operador a ignora-lo.
            return

        if presa:
            titulo = f"Posicao presa em {request.symbol} — a venda nao passa na exchange"
            corpo = (
                f"{motivo}\n\n"
                "ATENCAO: esta venda foi recusada AQUI porque a exchange a "
                "recusaria pelos filtros de lote/valor minimo. Enquanto isso "
                "valer, stop-loss e take-profit dessa posicao NAO tem como ser "
                "executados pelo sistema. Feche manualmente na exchange ou "
                "agregue a posicao."
            )
        else:
            titulo = f"Ordem recusada antes do envio — {request.symbol}"
            corpo = (
                f"{motivo}\n\n"
                "A ordem nao foi enviada: depois do truncamento ao passo do "
                "lote ela ficaria abaixo do minimo da exchange e voltaria "
                "recusada. Aumente o valor por ordem ou remova este par."
            )
        if viability.source != FONTE_AO_VIVO:
            corpo += (
                "\n\nO filtro usado veio do catalogo versionado do projeto (uma "
                "foto do load_markets da exchange), nao do catalogo ao vivo. Se "
                "a exchange mudou o passo de lote deste par desde a captura, "
                "confira na exchange antes de mudar a configuracao."
            )
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": tipo,
                "title": titulo,
                "message": corpo,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def _refuse_opening(
        self, request: OrderRequest, *, pending_gravado: bool = False
    ) -> None:
        """Recusa uma abertura de posicao com a trava ativa, e deixa registro.

        O registro vai para o `audit_log` (append-only) porque uma ordem que
        deixou de existir sem rastro e indistinguivel de uma ordem que nunca foi
        gerada -- e a diferenca entre as duas e justamente a protecao agindo.

        `pending_gravado` distingue os dois instantes em que a trava pega o
        pedido: antes de gravar (a ordem nunca existiu nem localmente) e depois
        de gravar (a linha PENDING existe e precisa ser encerrada, senao fica
        pedindo reconciliacao de uma ordem que nunca chegou a exchange).
        """
        motivo = self._openings_blocked or "operacao suspensa"
        self.log.error(
            "execution.opening_refused",
            symbol=request.symbol,
            client_order_id=request.client_order_id,
            quantity=str(request.quantity),
            notional=str(request.notional),
            pending_encerrado=pending_gravado,
            reason=motivo,
        )
        await self._audit(
            action="opening_refused",
            request=request,
            detail=motivo,
            extra={"pending_encerrado": pending_gravado},
        )

        if pending_gravado:
            # REJECTED, e nao CANCELED nem FAILED: nada foi criado na exchange, e
            # `last_order_time` (o cooldown do Risk Manager) ignora REJECTED de
            # proposito -- uma ordem que nao aconteceu nao pode consumir a
            # janela de espera do par.
            recusada = self._rejected(request, f"recusada antes do envio: {motivo}")
            await self._record(request, recusada)
            await self.bus.publish(Topics.ORDER_RESULTS, recusada)

        if self._blocked_alerted:
            return
        self._blocked_alerted = True
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "opening_refused",
                "title": "Abertura de posicao recusada -- trava ativa",
                "message": (
                    f"{motivo}\n\n"
                    f"O pedido de compra de {request.symbol} ja estava aprovado e na "
                    "fila quando a trava disparou, e foi recusado aqui. Fechamentos "
                    "(inclusive stop-loss) continuam sendo executados normalmente. "
                    "Novas recusas do mesmo episodio ficam so no log e no audit_log."
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def _refuse_without_stop(self, request: OrderRequest) -> None:
        """Recusa a ABERTURA que chegou sem stop-loss, e diz que chegou assim.

        Nao e um caso esperado: o Risk Manager preenche stop-loss e take-profit
        em toda aprovacao de abertura. Chegar aqui sem o campo significa que
        algum caminho novo esta publicando ordem sem passar por aquele calculo
        -- que e uma ordem sem protecao, o pior desfecho possivel, e por isso o
        registro precisa dizer isso em vez de a ordem simplesmente sumir.

        O portao exige o STOP-LOSS, nao o take-profit, e isso e uma decisao e
        nao um esquecimento: falta de stop-loss e perda sem fundo, falta de
        take-profit e lucro nao realizado. Recusar por take-profit ausente
        barraria ordem que nao arrisca nada. O campo entra no log e no
        `audit_log` de qualquer forma, para que a ausencia apareca.
        """
        detalhe = (
            f"abertura de {request.symbol} sem stop-loss: o Execution Agent nao "
            "envia compra desprotegida"
        )
        self.log.error(
            "execution.opening_without_stop_loss_refused",
            symbol=request.symbol,
            client_order_id=request.client_order_id,
            risk_event_id=request.risk_event_id,
            quantity=str(request.quantity),
            notional=str(request.notional),
            take_profit=str(request.take_profit) if request.take_profit else None,
        )
        await self._audit(
            action="opening_without_stop_loss",
            request=request,
            detail=detalhe,
            extra={"take_profit": str(request.take_profit) if request.take_profit else None},
        )
        if not self._deve_alertar("opening_without_stop_loss", request.symbol):
            return
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "opening_without_stop_loss",
                "title": f"Compra recusada em {request.symbol} — chegou sem stop-loss",
                "message": (
                    f"{detalhe}.\n\n"
                    "Toda aprovacao de abertura do Risk Manager carrega stop-loss "
                    "e take-profit, entao um pedido sem o campo indica um caminho "
                    "publicando ordem fora dele. A ordem NAO foi enviada. "
                    "Fechamentos seguem passando: venda de protecao nao tem "
                    "stop-loss por construcao."
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    def _deve_alertar(self, tipo: str, symbol: str) -> bool:
        """True na primeira recusa deste tipo para este par no episodio atual."""
        chave = (tipo, symbol)
        if chave in self._alertado_por_simbolo:
            return False
        self._alertado_por_simbolo.add(chave)
        return True

    async def _audit(
        self,
        *,
        action: str,
        request: OrderRequest,
        detail: str | None,
        extra: dict[str, object] | None = None,
    ) -> None:
        """Grava a recusa no `audit_log`, e GRITA se nao conseguir gravar.

        Antes, as tres gravacoes de auditoria deste arquivo estavam dentro de um
        `contextlib.suppress(Exception)`: auditoria que falhava virava silencio
        total, e a ordem era recusada sem sobrar rastro em lugar nenhum. Isso
        troca "nao consegui registrar" por "nada aconteceu" -- a mesma classe de
        erro que `_classificar` corrige do lado da exchange. Engolir a excecao
        continua certo (falhar aqui nao pode desfazer uma recusa que ja e o
        estado seguro), mas engolir em SILENCIO nao: o payload inteiro vai para
        o log, que passa a ser o rastro de ultimo recurso.
        """
        payload: dict[str, object] = {
            "client_order_id": request.client_order_id,
            "risk_event_id": request.risk_event_id,
            "side": str(request.side),
            "quantity": str(request.quantity),
            "notional": str(request.notional),
            "stop_loss": str(request.stop_loss) if request.stop_loss else None,
        }
        if extra:
            payload.update(extra)
        try:
            async with session_scope(self._settings) as session:
                await AuditLogRepository(session).append(
                    action=action,
                    actor="execution",
                    target=request.symbol,
                    detail=detail,
                    before=payload,
                )
        except Exception as exc:
            self.log.error(
                "execution.audit_write_failed",
                action=action,
                symbol=request.symbol,
                detail=detail,
                payload=payload,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _failed(self, request: OrderRequest, error: str) -> OrderResult:
        return OrderResult(
            order_request_id=request.id,
            client_order_id=request.client_order_id,
            exchange_order_id=None,
            status=OrderStatus.FAILED,
            error=error,
        )

    def _rejected(self, request: OrderRequest, error: str) -> OrderResult:
        return OrderResult(
            order_request_id=request.id,
            client_order_id=request.client_order_id,
            exchange_order_id=None,
            status=OrderStatus.REJECTED,
            error=error,
        )

    def _unknown(self, request: OrderRequest, error: str) -> OrderResult:
        """Desfecho que o sistema NAO conhece.

        Fica em `PENDING` de proposito: e o unico status que significa "tentada,
        sem confirmacao". `FAILED` afirmaria que nada aconteceu, e afirmar isso
        sem saber e o erro caro -- ver `_classificar`.
        """
        return OrderResult(
            order_request_id=request.id,
            client_order_id=request.client_order_id,
            exchange_order_id=None,
            status=OrderStatus.PENDING,
            error=f"DESFECHO DESCONHECIDO, precisa reconciliacao: {error}",
        )

    def _classificar(self, request: OrderRequest, result: OrderResult) -> OrderResult:
        """Reclassifica como desconhecido o desfecho que a exchange nao negou.

        Medido em 2026-09-09 com o caminho real do `ccxt`: a exchange aceita a
        ordem, a resposta se perde (`RequestTimeout`), o `_with_retry` reenvia
        com o MESMO `clientOrderId` e a Binance responde `-2010 Duplicate order
        sent`. O `client_order_id` fez o seu trabalho -- nao houve segunda
        ordem, foi comprado 0,001 BTC e nao 0,002. Mas o resultado voltava como
        `FAILED` com `filled=0`, enquanto 50 USDT tinham saido do saldo:

            saldo real       : USDT 949.92 | BTC 0.001
            ordem no banco   : failed, filled 0
            trades no banco  : 0

        A consequencia nao para no relatorio errado. Sem trade, o Portfolio
        Agent nao reconstroi o preco medio (`_average_costs` le a tabela de
        trades); sem preco medio, `_emit_protective_exits` pula a posicao. A
        posicao existe, custou dinheiro, e nunca teria stop-loss.

        Uma mensagem de ordem duplicada e, portanto, o oposto de uma falha: e a
        exchange provando que a ordem EXISTE.
        """
        if result.status not in (OrderStatus.FAILED, OrderStatus.REJECTED):
            return result
        if result.filled_quantity > 0:
            # Ja veio com o preenchimento: a exchange respondeu o que faltava
            # saber, e nao ha nada a reconciliar. Mas o STATUS tambem precisa
            # mudar. Deixa-lo em FAILED gravava dois registros que se
            # contradizem: a ordem como "failed" no banco com um trade
            # preenchido pendurado nela, e o log saindo como
            # `execution.partially_filled status=failed`. Quem decide o status
            # aqui e a mesma autoridade que decide se existe trade -- a moeda
            # ter mudado de mao -- e uma ordem que preencheu nao e uma falha.
            preenchido = (
                OrderStatus.FILLED
                if result.filled_quantity >= request.quantity
                else OrderStatus.PARTIALLY_FILLED
            )
            self.log.warning(
                "execution.status_normalizado",
                client_order_id=request.client_order_id,
                de=str(result.status),
                para=str(preenchido),
                preenchido=str(result.filled_quantity),
                pedido=str(request.quantity),
                error=result.error,
            )
            # O `error` fica: e ele que conta POR QUE a exchange respondeu com
            # cara de falha, e essa informacao nao pode se perder na correcao.
            return result.model_copy(update={"status": preenchido})
        if not _ORDEM_JA_EXISTE.search(result.error or ""):
            return result
        return self._unknown(
            request,
            f"a exchange respondeu que o client_order_id ja existe, logo a ordem "
            f"FOI criada: {result.error}",
        )

    async def _report_unknown_outcome(
        self, request: OrderRequest, result: OrderResult
    ) -> None:
        """Alerta e audita uma ordem cujo desfecho o sistema nao conhece.

        Sem deduplicacao, ao contrario do acesso negado: cada ordem sem
        confirmacao e uma quantia distinta que pode estar viva na exchange sem
        stop-loss. Agrupar isso economizaria alerta e custaria dinheiro.
        """
        self.log.error(
            "execution.outcome_unknown",
            symbol=request.symbol,
            side=str(request.side),
            client_order_id=request.client_order_id,
            quantity=str(request.quantity),
            notional=str(request.notional),
            error=result.error,
        )
        await self._audit(
            action="order_outcome_unknown",
            request=request,
            detail=result.error,
        )
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "order_outcome_unknown",
                "title": f"Desfecho desconhecido em {request.symbol} — reconciliar",
                "message": (
                    f"{result.error}\n\n"
                    f"A ordem ({request.side} {request.quantity} {request.symbol}, "
                    f"~{request.notional}) ficou como PENDING no banco porque o "
                    "sistema nao recebeu confirmacao. Ela pode estar viva ou "
                    "preenchida na exchange. Confira o extrato pelo "
                    f"client_order_id {request.client_order_id}: se preencheu, o "
                    "trade precisa ser lancado manualmente, senao a posicao fica "
                    "sem preco medio e o stop-loss nao e emitido para ela."
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def _report_access_denied(
        self, error: ApiAccessDenied, request: OrderRequest
    ) -> None:
        """Alerta que o sistema perdeu acesso de escrita a exchange.

        Este e o cenario mais perigoso do sistema, e o menos visivel: os dados de
        mercado sao publicos e continuam chegando, entao o dashboard segue
        atualizando normalmente enquanto nenhuma ordem consegue mais sair. Com
        posicao aberta, o sinal de fechamento e aprovado pelo Risk Manager e a
        ordem morre na exchange -- stop-loss e take-profit deixam de existir na
        pratica.
        """
        self.log.error(
            "execution.api_access_denied",
            exchange=error.exchange or request.exchange,
            operation=error.operation,
            symbol=request.symbol,
            error=str(error),
        )

        if self._access_denied_since is not None:
            return  # incidente ja alertado; nao repetir a cada ordem

        self._access_denied_since = datetime.now(UTC)
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "api_access_denied",
                "title": "Exchange recusou a credencial — nenhuma ordem sai",
                "message": (
                    f"{error}\n\n"
                    "ATENCAO: os dados de mercado continuam chegando, entao o "
                    "dashboard parece normal, mas o sistema NAO consegue mais "
                    "enviar ordens. Se houver posicao aberta, o stop-loss nao "
                    "sera executado. Verifique a whitelist de IP e as permissoes "
                    "da chave, e considere fechar posicoes manualmente pela "
                    "exchange enquanto isso nao for resolvido."
                ),
                "timestamp": self._access_denied_since.isoformat(),
            },
        )

    async def _report_access_restored(self) -> None:
        if self._access_denied_since is None:
            return
        down_for = datetime.now(UTC) - self._access_denied_since
        self._access_denied_since = None
        self.log.info("execution.api_access_restored", seconds=int(down_for.total_seconds()))
        await self.bus.publish(
            Topics.ALERTS,
            {
                "type": "api_access_restored",
                "title": "Acesso a exchange restabelecido",
                "message": (
                    f"Ordens voltaram a ser aceitas apos {int(down_for.total_seconds() / 60)} "
                    "minuto(s) sem acesso."
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def _record(self, request: OrderRequest, result: OrderResult) -> None:
        # O que decide se ha trade e a moeda ter mudado de mao, NAO o status da
        # ordem. Medido em 2026-09-09: com `status is FILLED` como condicao, uma
        # execucao parcial (0,0004 BTC, ordem ainda OPEN) e um cancelamento
        # depois de preencher 60% (0,0006 BTC) gravavam ZERO trades -- e as duas
        # coisas movem cripto de verdade. `PARTIALLY_FILLED` nao era produzido
        # por nenhum broker do projeto, o que escondia o caso; a Binance produz,
        # e ordem a mercado em livro fino preenche parcial com frequencia.
        #
        # Trade nao registrado nao e so PnL errado: sem trade nao ha preco medio
        # (o Portfolio Agent o reconstroi da tabela de trades) e sem preco medio
        # o Risk Manager nao emite stop-loss para a posicao.
        filled = result.filled_quantity > 0
        price = result.average_price or request.price

        if filled and price is None:
            # Ordem preenchida sem preco indica bug no adaptador da exchange.
            # Gravar o trade com preco zero contaminaria PnL e historico fiscal
            # de forma silenciosa; e melhor deixar so a ordem, que carrega o
            # payload bruto para reconciliacao manual.
            self.log.error(
                "execution.filled_without_price",
                symbol=request.symbol,
                client_order_id=request.client_order_id,
                raw=result.raw,
            )
            filled = False

        async with session_scope(self._settings) as session:
            await OrderRepository(session).apply_result(result)

            if filled and price is not None:
                await TradeRepository(session).record(
                    executed_at=result.timestamp,
                    exchange=str(request.exchange),
                    symbol=request.symbol,
                    side=str(request.side),
                    quantity=result.filled_quantity,
                    price=price,
                    fee=result.fee,
                    fee_currency=result.fee_currency,
                    origin=TradeOrigin.AGENT,
                    order_id=request.id,
                    signal_id=request.signal_id,
                    strategy=request.strategy,
                    mode=self._mode,
                )

        if result.status is OrderStatus.FILLED:
            self.log.info(
                "execution.filled",
                symbol=request.symbol,
                side=str(request.side),
                quantity=str(result.filled_quantity),
                price=str(result.average_price),
                fee=str(result.fee),
            )
        elif filled:
            # Preencheu em parte: precisa aparecer como preenchimento, e nao
            # some no `not_filled` que descreve o contrario do que aconteceu.
            faltou = request.quantity - result.filled_quantity
            self.log.warning(
                "execution.partially_filled",
                symbol=request.symbol,
                side=str(request.side),
                status=str(result.status),
                pedido=str(request.quantity),
                preenchido=str(result.filled_quantity),
                nao_preenchido=str(max(faltou, Decimal(0))),
                price=str(result.average_price),
                error=result.error,
            )
        else:
            self.log.warning(
                "execution.not_filled",
                symbol=request.symbol,
                status=str(result.status),
                error=result.error,
            )

