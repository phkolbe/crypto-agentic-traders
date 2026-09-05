"""Execution Agent.

Unico componente autorizado a enviar ordens. Nao decide nada: recebe
`OrderRequest` ja aprovados e cuida de enviar, reconciliar e registrar.

Ordem das operacoes, que importa:

1. Verifica idempotencia (`client_order_id` ja usado?).
2. Grava a ordem como PENDING **antes** de chamar a exchange.
3. Envia.
4. Grava o resultado e, se preenchida, o trade no historico consolidado.

O passo 2 antes do 3 e deliberado: se o processo morrer entre o envio e a
confirmacao, existe registro local de que a ordem foi tentada. O inverso
deixaria uma ordem viva na exchange e invisivel aqui.
"""

from __future__ import annotations

from ..bus import EventBus, Topics
from ..config import Settings
from ..db.repositories import OrderRepository, TradeRepository
from ..db.session import session_scope
from ..domain.enums import OrderStatus, TradeOrigin
from ..domain.models import OrderRequest, OrderResult
from ..exchanges.base import Broker
from .base import BaseAgent


class ExecutionAgent(BaseAgent):
    name = "execution"

    def __init__(self, bus: EventBus, broker: Broker, settings: Settings) -> None:
        super().__init__(bus)
        self._broker = broker
        self._settings = settings
        self._mode = str(settings.trading_mode)

    @property
    def broker(self) -> Broker:
        return self._broker

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

        async with session_scope(self._settings) as session:
            orders = OrderRepository(session)
            if await orders.find_by_client_id(request.client_order_id) is not None:
                self.log.warning(
                    "execution.duplicate_ignored", client_order_id=request.client_order_id
                )
                return None
            await orders.create_pending(request, self._mode)

        self.log.info(
            "execution.sending",
            symbol=request.symbol,
            side=str(request.side),
            quantity=str(request.quantity),
            mode=self._mode,
        )

        try:
            result = await self._broker.place_order(request)
        except Exception as exc:
            # Falha na chamada tambem e um desfecho: precisa ficar registrada,
            # senao a ordem fica PENDING para sempre sem explicacao.
            result = OrderResult(
                order_request_id=request.id,
                client_order_id=request.client_order_id,
                exchange_order_id=None,
                status=OrderStatus.FAILED,
                error=str(exc),
            )

        await self._record(request, result)
        await self.bus.publish(Topics.ORDER_RESULTS, result)
        return result

    async def _record(self, request: OrderRequest, result: OrderResult) -> None:
        filled = result.status is OrderStatus.FILLED and result.filled_quantity > 0
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
        else:
            self.log.warning(
                "execution.not_filled",
                symbol=request.symbol,
                status=str(result.status),
                error=result.error,
            )

