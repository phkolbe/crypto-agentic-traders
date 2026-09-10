"""Ataques do dono do projeto contra o item 4 (Execution Agent).

Os quatro exigidos por escrito, no que alcancam este agente:

1. ordem no limite EXATO do teto (igualdade, nao "proximo de");
2. dois sinais simultaneos no mesmo ativo;
3. circuit breaker disparando com uma ordem EM VOO;
4. chave de API vazia.

Mais o que o implementador nao pensou: a recusa por filtro nao tem
deduplicacao de alerta, e a ordem de abertura sem stop-loss nao e recusada no
ultimo portao.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from crypto_traders.agents.execution import ExecutionAgent
from crypto_traders.bus import InMemoryEventBus, Topics
from crypto_traders.db.repositories import (
    AuditLogRepository,
    OrderRepository,
    TradeRepository,
)
from crypto_traders.db.session import session_scope
from crypto_traders.domain.enums import ExchangeName, OrderStatus, OrderType, Side
from crypto_traders.domain.models import OrderRequest, OrderResult
from crypto_traders.exchanges.base import Broker
from crypto_traders.exchanges.filters import MarketFilter
from crypto_traders.exchanges.paper import PaperBroker


class BusEspiao(InMemoryEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.publicados: list[tuple[str, object]] = []

    async def publish(self, topic: str, payload: object) -> None:
        self.publicados.append((topic, payload))
        await super().publish(topic, payload)

    def alertas(self, tipo: str | None = None) -> list[dict]:
        alertas = [p for t, p in self.publicados if t == Topics.ALERTS]
        if tipo is None:
            return alertas
        return [a for a in alertas if a.get("type") == tipo]


def pedido(
    *,
    client_order_id: str = "atk-1",
    side: Side = Side.BUY,
    quantity: str = "0.001",
    symbol: str = "BTC/USDT",
    reference: str = "50000",
    risk_event_id: str | None = "risk-1",
    stop_loss: str | None = "48500",
) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id,
        signal_id="sig-1",
        risk_event_id=risk_event_id,
        exchange=ExchangeName.PAPER,
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
        price=None,
        notional=Decimal(quantity) * Decimal(reference),
        stop_loss=Decimal(stop_loss) if stop_loss else None,
        take_profit=Decimal("53000") if stop_loss else None,
        strategy="ma_crossover",
    )


async def audit_actions(settings) -> list[str]:
    async with session_scope(settings) as session:
        return [e.action for e in await AuditLogRepository(session).list()]


@pytest.fixture
def paper() -> PaperBroker:
    broker = PaperBroker(initial_balance=Decimal("1000"))
    broker.set_price("BTC", Decimal("50000"))
    return broker


# ----------------------------------------------------------------------
# ATAQUE 1 -- igualdade exata no minimo da exchange
# ----------------------------------------------------------------------
class TestAtaque1LimiteExato:
    """MIN_NOTIONAL na Binance e `>=`. O sistema tem que concordar na igualdade.

    Errar o lado do comparador aqui recusa em silencio toda ordem dimensionada
    exatamente no minimo -- que e justamente como o Risk Manager dimensiona
    quando o capital autorizado e pequeno (29,29 USDC hoje).
    """

    def _broker(self) -> PaperBroker:
        broker = PaperBroker(quote_currency="USDC", initial_balance=Decimal("1000"))
        broker.set_price("BNB", Decimal("1000"))
        broker.set_market_filter(
            MarketFilter("BNB/USDC", Decimal("0.001"), Decimal("5"), Decimal("0"))
        )
        return broker

    async def test_valor_exatamente_igual_ao_minimo_passa(self, settings):
        """0,005 BNB a 1000 = 5,000 USDC == minimo de 5. Tem que passar."""
        broker = self._broker()
        agent = ExecutionAgent(BusEspiao(), broker, settings)
        result = await agent._execute(
            pedido(
                client_order_id="atk-igual-1",
                symbol="BNB/USDC",
                quantity="0.005",
                reference="1000",
            )
        )
        assert result is not None, "igualdade recusada: o comparador esta errado"
        assert result.status is OrderStatus.FILLED

    async def test_um_passo_abaixo_do_minimo_e_recusado(self, settings):
        """0,004 BNB = 4,000 USDC. O lado de fora da igualdade tem que recusar."""
        broker = self._broker()
        agent = ExecutionAgent(BusEspiao(), broker, settings)
        result = await agent._execute(
            pedido(
                client_order_id="atk-igual-2",
                symbol="BNB/USDC",
                quantity="0.004",
                reference="1000",
            )
        )
        assert result is None

    async def test_quantidade_exatamente_no_passo_do_lote_nao_perde_nada(
        self, settings
    ):
        """Quantidade que ja e multiplo exato do passo nao pode ser truncada."""
        broker = self._broker()
        agent = ExecutionAgent(BusEspiao(), broker, settings)
        result = await agent._execute(
            pedido(
                client_order_id="atk-igual-3",
                symbol="BNB/USDC",
                quantity="0.006",
                reference="1000",
            )
        )
        assert result is not None
        assert result.filled_quantity == Decimal("0.006")


# ----------------------------------------------------------------------
# ATAQUE 2 -- dois sinais simultaneos no mesmo ativo
# ----------------------------------------------------------------------
class TestAtaque2SinaisSimultaneos:
    async def test_dois_pedidos_distintos_no_mesmo_ativo_gastam_duas_vezes(
        self, settings, paper
    ):
        """Documenta a fronteira: o Execution Agent NAO deduplica por ativo.

        Dois `client_order_id` distintos no mesmo par sao duas ordens legitimas
        aqui -- quem limita posicao por ativo e o Risk Manager. O que este
        teste trava e que as duas realmente aconteceram e as duas ficaram
        registradas: o pior desfecho seria gastar duas vezes e registrar uma.
        """
        agent = ExecutionAgent(BusEspiao(), paper, settings)
        a = pedido(client_order_id="atk-sim-a")
        b = pedido(client_order_id="atk-sim-b")

        await asyncio.gather(agent._execute(a), agent._execute(b))

        async with session_scope(settings) as session:
            ordens = await OrderRepository(session).list()
            trades = await TradeRepository(session).list()
        assert len(ordens) == 2
        assert len(trades) == 2, "gastou duas vezes e registrou menos de duas"
        assert (await paper.fetch_balances())["BTC"] == Decimal("0.002")


# ----------------------------------------------------------------------
# ATAQUE 3 -- circuit breaker com a ordem EM VOO
# ----------------------------------------------------------------------
class BrokerLento(Broker):
    """Broker que avisa quando chegou no envio e espera ser liberado.

    E o unico jeito de colocar a ordem EM VOO de verdade: o teste ganha o
    controle exatamente no instante entre "o agente decidiu enviar" e "a
    exchange recebeu".
    """

    name = "lento"

    def __init__(self, real: PaperBroker) -> None:
        self._real = real
        self.chegou = asyncio.Event()
        self.liberado = asyncio.Event()
        self.enviados: list[str] = []

    async def place_order(self, request):
        self.chegou.set()
        await self.liberado.wait()
        self.enviados.append(request.client_order_id)
        return await self._real.place_order(request)

    async def fetch_balances(self):
        return await self._real.fetch_balances()

    async def fetch_positions(self, prices):
        return await self._real.fetch_positions(prices)

    async def close(self):
        return None


class TestAtaque3OrdemEmVoo:
    async def test_trava_durante_a_janela_de_gravacao_nao_para_a_compra(
        self, settings, paper, monkeypatch
    ):
        """A trava do D4 e checada UMA vez, antes de dois `await`.

        Sequencia real, toda ela dentro de `_execute`:

            1. `if self._openings_blocked ... and side is BUY`  -> solta, passa
            2. `async with session_scope(...)` -> grava PENDING  (AWAIT: cede o laco)
            3. `await self._broker.place_order(request)`         -> GASTA

        O circuit breaker que dispara entre o passo 1 e o passo 3 nao e visto
        por ninguem: a compra e executada DEPOIS da trava, que e exatamente o
        defeito que a trava foi criada para corrigir.
        """
        agent = ExecutionAgent(BusEspiao(), paper, settings)

        # A trava e armada como ultimo ato da gravacao do PENDING. Isso poe o
        # disparo do circuit breaker PROVADAMENTE dentro do passo 2 -- depois da
        # checagem, antes do envio -- sem depender de tempo nem de quantos
        # `sleep(0)` o laco precisa. Nao ha ordem na exchange neste instante:
        # nada aqui e irrevogavel.
        ordem_dos_fatos: list[str] = []
        create_pending_real = OrderRepository.create_pending
        place_order_real = PaperBroker.place_order

        async def create_pending_espiao(self, request, mode):
            registro = await create_pending_real(self, request, mode)
            agent.block_openings("perda diaria de 12% -- circuit breaker")
            ordem_dos_fatos.append("trava armada")
            return registro

        async def place_order_espiao(self, request):
            ordem_dos_fatos.append("ordem enviada")
            return await place_order_real(self, request)

        monkeypatch.setattr(OrderRepository, "create_pending", create_pending_espiao)
        monkeypatch.setattr(PaperBroker, "place_order", place_order_espiao)

        await agent._execute(pedido(client_order_id="atk-voo-1"))

        saldos = await paper.fetch_balances()
        assert agent.openings_blocked is not None, "a trava nao armou; teste invalido"
        assert ordem_dos_fatos[0] == "trava armada", (
            f"teste invalido: a ordem dos fatos foi {ordem_dos_fatos}"
        )
        assert "BTC" not in saldos, (
            "COMPRA EXECUTADA COM A TRAVA ARMADA. Sequencia medida: "
            f"{ordem_dos_fatos} -- o circuit breaker disparou antes de a ordem "
            f"sair e ela saiu de qualquer jeito. saldos={saldos}"
        )

    async def test_trava_com_a_ordem_na_exchange_nao_e_recuperavel(
        self, settings, paper
    ):
        """Contraste honesto: depois de a exchange receber, nao ha volta.

        Este teste existe para separar os dois casos. Ordem JA na exchange e
        irrevogavel e ninguem pede outra coisa. O caso do teste anterior e
        diferente: lá a ordem ainda nao havia saido.
        """
        lento = BrokerLento(paper)
        agent = ExecutionAgent(BusEspiao(), lento, settings)

        tarefa = asyncio.create_task(agent._execute(pedido(client_order_id="atk-voo-2")))
        await asyncio.wait_for(lento.chegou.wait(), timeout=2)
        agent.block_openings("perda diaria -- circuit breaker")
        lento.liberado.set()
        await asyncio.wait_for(tarefa, timeout=2)

        # A ordem estava na exchange: preencheu, e o sistema registrou.
        assert lento.enviados == ["atk-voo-2"]
        async with session_scope(settings) as session:
            assert len(await TradeRepository(session).list()) == 1

    async def test_trava_barra_a_compra_seguinte(self, settings, paper):
        """O que funciona hoje, e nao pode regredir."""
        agent = ExecutionAgent(BusEspiao(), paper, settings)
        agent.block_openings("perda diaria -- circuit breaker")
        assert await agent._execute(pedido(client_order_id="atk-voo-3")) is None
        assert "BTC" not in await paper.fetch_balances()

    async def test_trava_nunca_barra_o_fechamento(self, settings, paper):
        """D4: a trava e sobre ABRIR. Fechar sempre passa."""
        paper.credit("BTC", Decimal("0.001"))
        agent = ExecutionAgent(BusEspiao(), paper, settings)
        agent.block_openings("perda diaria -- circuit breaker")
        result = await agent._execute(
            pedido(client_order_id="atk-voo-4", side=Side.SELL)
        )
        assert result is not None and result.status is OrderStatus.FILLED


# ----------------------------------------------------------------------
# ATAQUE 4 -- chave de API vazia
# ----------------------------------------------------------------------
class BrokerSemCredencial(Broker):
    """Exchange respondendo o que responde a uma chave vazia.

    A Binance devolve `-2014 API-key format invalid` como erro de autenticacao,
    que o adaptador traduz para `ApiAccessDenied`.
    """

    name = "sem-credencial"

    async def place_order(self, request):
        from crypto_traders.exchanges.base import ApiAccessDenied

        raise ApiAccessDenied(
            "binance.create_order: API-key format invalid. (-2014)",
            exchange="binance",
            operation="create_order",
        )

    async def fetch_balances(self):
        return {}

    async def fetch_positions(self, prices):
        return []

    async def close(self):
        return None


class TestAtaque4ChaveVazia:
    async def test_chave_vazia_alerta_e_nao_finge_que_nada_aconteceu(self, settings):
        agent = ExecutionAgent(BusEspiao(), BrokerSemCredencial(), settings)
        bus = agent.bus
        result = await agent._execute(pedido(client_order_id="atk-chave-1"))

        assert result is not None and result.status is OrderStatus.FAILED
        alertas = bus.alertas("api_access_denied")
        assert alertas, "chave recusada tem que alertar"
        assert agent.access_denied
        async with session_scope(settings) as session:
            assert len(await TradeRepository(session).list()) == 0

    async def test_chave_vazia_nao_vira_desfecho_desconhecido(self, settings):
        """A exchange RESPONDEU 'nao': nao ha o que reconciliar."""
        agent = ExecutionAgent(BusEspiao(), BrokerSemCredencial(), settings)
        await agent._execute(pedido(client_order_id="atk-chave-2"))
        assert not agent.bus.alertas("order_outcome_unknown")

    async def test_chave_vazia_em_toda_ordem_alerta_uma_vez_por_incidente(
        self, settings
    ):
        agent = ExecutionAgent(BusEspiao(), BrokerSemCredencial(), settings)
        for i in range(5):
            await agent._execute(pedido(client_order_id=f"atk-chave-spam-{i}"))
        assert len(agent.bus.alertas("api_access_denied")) == 1


# ----------------------------------------------------------------------
# O que o implementador nao pensou
# ----------------------------------------------------------------------
class TestRecusaPorFiltroNaoTemFreio:
    """`_refuse_by_filter` alerta a CADA ordem recusada, sem deduplicacao.

    `_refuse_opening` tem freio (`_blocked_alerted`); a recusa por filtro nao.
    Isso importa porque a recusa por filtro nao e um evento unico: uma posicao
    presa (poeira abaixo do MIN_NOTIONAL) faz o Risk Manager reemitir a venda
    de protecao a cada ciclo, e cada ciclo gera um alerta `position_trapped`.

    E a mesma patologia do achado 1 do ensaio (1.440 alertas/dia), no alerta
    que menos pode ser ignorado.
    """

    def _broker(self) -> PaperBroker:
        broker = PaperBroker(quote_currency="USDC", initial_balance=Decimal("1000"))
        broker.set_price("BNB", Decimal("1200"))
        broker.credit("BNB", Decimal("0.004"))
        broker.set_market_filter(
            MarketFilter("BNB/USDC", Decimal("0.001"), Decimal("5"), Decimal("0"))
        )
        return broker

    async def test_posicao_presa_nao_repete_o_alerta_a_cada_ciclo(self, settings):
        broker = self._broker()
        bus = BusEspiao()
        agent = ExecutionAgent(bus, broker, settings)

        # 20 ciclos do Risk Manager reemitindo o mesmo stop-loss impossivel.
        for i in range(20):
            await agent._execute(
                pedido(
                    client_order_id=f"atk-presa-{i}",
                    symbol="BNB/USDC",
                    side=Side.SELL,
                    quantity="0.004",
                    reference="1200",
                    risk_event_id="protecao-stop_loss-BNB",
                )
            )

        alertas = bus.alertas("position_trapped")
        assert len(alertas) == 1, (
            f"{len(alertas)} alertas para a MESMA posicao presa. Com o ciclo de "
            "producao isso e alerta continuo no aviso que menos pode ser "
            "ignorado -- e o operador para de ler."
        )
        # O registro por ciclo continua no audit_log: o freio e do alerta.
        assert (await audit_actions(settings)).count("position_trapped") == 20


class TestAberturaSemStopLoss:
    """Item 2 da checklist: toda ordem carrega stop-loss antes de ser enviada.

    O Execution Agent e o ULTIMO portao. Hoje ele nao olha o campo: uma
    abertura com `stop_loss=None` e enviada e preenchida.
    """

    async def test_abertura_sem_stop_loss_nao_e_enviada(self, settings, paper):
        bus = BusEspiao()
        agent = ExecutionAgent(bus, paper, settings)
        result = await agent._execute(
            pedido(client_order_id="atk-sem-stop", stop_loss=None)
        )
        assert result is None, (
            "abertura SEM stop-loss foi enviada e preenchida: o ultimo portao "
            "nao verifica o campo que a checklist exige"
        )
        assert "BTC" not in await paper.fetch_balances()

    async def test_fechamento_sem_stop_loss_continua_passando(self, settings, paper):
        """Fechamento nao tem stop-loss por construcao, e nao pode ser barrado."""
        paper.credit("BTC", Decimal("0.001"))
        agent = ExecutionAgent(BusEspiao(), paper, settings)
        result = await agent._execute(
            pedido(client_order_id="atk-fecha-sem-stop", side=Side.SELL, stop_loss=None)
        )
        assert result is not None and result.status is OrderStatus.FILLED


class TestDesfechoDesconhecidoNaoViraTradeFantasma:
    async def test_pending_desconhecido_nao_grava_trade(self, settings):
        """PENDING sem preenchimento conhecido nao pode inventar trade."""

        class SemResposta(Broker):
            name = "sem-resposta"

            async def place_order(self, request):
                raise TimeoutError("a resposta nunca chegou")

            async def fetch_balances(self):
                return {}

            async def fetch_positions(self, prices):
                return []

            async def close(self):
                return None

        bus = BusEspiao()
        agent = ExecutionAgent(bus, SemResposta(), settings)
        result = await agent._execute(pedido(client_order_id="atk-pend-1"))

        assert result is not None and result.status is OrderStatus.PENDING
        async with session_scope(settings) as session:
            assert len(await TradeRepository(session).list()) == 0
            assert (await OrderRepository(session).list())[0].status == str(
                OrderStatus.PENDING
            )
        assert bus.alertas("order_outcome_unknown")


class TestDuplicataComStatusIncoerente:
    async def test_duplicata_preenchida_nao_fica_registrada_como_falha(self, settings):
        """`-2010` com preenchimento conhecido: `_classificar` devolve FAILED.

        `_classificar` sai cedo quando `filled_quantity > 0`, entao o resultado
        segue para `_record` com status FAILED e o trade e gravado. A ordem fica
        `failed` no banco com um trade preenchido pendurado nela -- estados que
        se contradizem no registro que o dono usa para conferir dinheiro.
        """
        class Duplicata(Broker):
            name = "duplicata"

            async def place_order(self, request):
                return OrderResult(
                    order_request_id=request.id,
                    client_order_id=request.client_order_id,
                    exchange_order_id=None,
                    status=OrderStatus.FAILED,
                    error="binance.create_order: Duplicate order sent. (-2010)",
                    filled_quantity=request.quantity,
                    average_price=Decimal("50000"),
                )

            async def fetch_balances(self):
                return {}

            async def fetch_positions(self, prices):
                return []

            async def close(self):
                return None

        agent = ExecutionAgent(BusEspiao(), Duplicata(), settings)
        await agent._execute(pedido(client_order_id="atk-dup-incoerente"))

        async with session_scope(settings) as session:
            ordem = (await OrderRepository(session).list())[0]
            trades = await TradeRepository(session).list()

        assert len(trades) == 1, "a cripto mudou de mao: o trade tem que existir"
        assert ordem.status != str(OrderStatus.FAILED), (
            f"ordem gravada como '{ordem.status}' com um trade preenchido "
            "pendurado nela: o historico se contradiz"
        )


class TestChaveVaziaRecusaSubir:
    """ATAQUE 4, o lado da configuracao: chave em branco nao e "autenticado".

    `BINANCE__API_KEY=` no `.env` produziria `SecretStr('')`, que e diferente
    de `None` -- e o sistema se consideraria credenciado e tentaria enviar
    ordem com credencial vazia. O guarda existe e AGE: este teste ve
    `build_broker` recusar.
    """

    async def test_dry_run_e_o_padrao_de_fabrica(self):
        from crypto_traders.config import Settings
        from crypto_traders.domain.enums import TradingMode

        s = Settings(_env_file=None)
        assert s.trading_mode is TradingMode.DRY_RUN
        assert s.live_trading_confirmed is False

    async def test_live_sem_a_segunda_confirmacao_nao_instancia(self):
        import pydantic

        from crypto_traders.config import Settings

        with pytest.raises(pydantic.ValidationError, match="LIVE_TRADING_CONFIRMED"):
            Settings(_env_file=None, trading_mode="live")

    async def test_chave_em_branco_nao_conta_como_configurada(self):
        from crypto_traders.config import ExchangeCredentials

        vazia = ExchangeCredentials(api_key="", api_secret="   ")
        assert vazia.api_key is None
        assert vazia.configured is False

    async def test_testnet_com_chave_vazia_recusa_construir_o_broker(self):
        from crypto_traders.config import Settings
        from crypto_traders.exchanges import build_broker
        from crypto_traders.exchanges.base import ExchangeError

        s = Settings(_env_file=None, trading_mode="testnet")
        with pytest.raises(ExchangeError, match="exige credenciais"):
            build_broker(s)

    async def test_dry_run_nunca_constroi_broker_que_envia_ordem(self):
        from crypto_traders.config import Settings
        from crypto_traders.exchanges import build_broker

        assert isinstance(build_broker(Settings(_env_file=None)), PaperBroker)


class TestPreflightNoSistemaReal:
    """A checagem de filtro existe, e testada, e NUNCA RODA no sistema real.

    `set_market_filter`/`set_market_filters` nao tem UM chamador em `src/` --
    so os testes chamam. Consequencia medida:

    - dry_run (o modo que esta rodando): `PaperBroker._market_filters` fica
      vazio, `market_filter()` devolve `None`, `_preflight` devolve `None`;
    - LIVE/TESTNET: `CcxtExchange` nao implementa `MarketFilterSource`, entao
      `_preflight` devolve `None` antes de qualquer coisa.

    Em toda configuracao real o comportamento e o de ANTES da correcao: a ordem
    e enviada e recusada pela exchange -- exatamente o que o mandato proibiu.
    """

    async def test_o_broker_do_sistema_real_conhece_os_filtros_dos_pares(self):
        from crypto_traders.config import Settings
        from crypto_traders.exchanges import build_broker

        broker = build_broker(Settings(_env_file=None))
        assert broker.market_filter("BNB/USDC") is not None, (
            "o broker que o sistema sobe nao conhece filtro de par nenhum: a "
            "checagem de MIN_NOTIONAL nao age em dry_run"
        )

    async def test_o_broker_de_producao_sabe_informar_filtros(self):
        from crypto_traders.exchanges.ccxt_adapter import CcxtExchange
        from crypto_traders.exchanges.filters import MarketFilterSource

        assert issubclass(CcxtExchange, MarketFilterSource), (
            "CcxtExchange nao implementa market_filter: em LIVE o _preflight "
            "devolve None sempre e a ordem abaixo do MIN_NOTIONAL e enviada"
        )

    async def test_o_caso_do_mandato_e_recusado_no_sistema_como_ele_sobe(
        self, settings
    ):
        """5,86 USDC com BNB a 1.200 -> 0,004 BNB = 4,80: abaixo do minimo de 5."""
        from crypto_traders.config import Settings
        from crypto_traders.exchanges import build_broker

        broker = build_broker(Settings(_env_file=None))
        broker.set_price("BNB", Decimal("1200"))
        # Saldo folgado de proposito: se a ordem for recusada, tem que ser pelo
        # filtro de MIN_NOTIONAL, nunca por falta de saldo.
        broker.credit("USDC", Decimal("1000"))
        agent = ExecutionAgent(BusEspiao(), broker, settings)

        result = await agent._execute(
            pedido(
                client_order_id="atk-mandato-1",
                symbol="BNB/USDC",
                quantity="0.004883",
                reference="1200",
            )
        )
        assert result is None, (
            "a ordem de 4,80 USDC foi ENVIADA e voltou "
            f"'{result.status if result else None}': no sistema como ele sobe "
            "hoje a checagem de filtro nao age, e a recusa continua vindo da "
            "exchange -- exatamente o que o mandato proibiu"
        )
