"""Ataques do critico do item 8 (API FastAPI + WebSocket).

Cada teste aqui existe para QUEBRAR o codigo atual. Nao sao testes de contrato:
sao tentativas de fazer a API afrouxar um limite, perder um pedido aceito com
200, ou calar o dashboard para sempre.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import ClassVar

import pytest
from httpx import ASGITransport, AsyncClient

from crypto_traders.api.app import create_app
from crypto_traders.api.routes import ws as ws_module
from crypto_traders.domain.models import PortfolioSnapshot


@pytest.fixture
async def controlled(settings):
    from crypto_traders.agents.orchestrator import Orchestrator
    from crypto_traders.agents.risk_manager import RiskManagerAgent
    from crypto_traders.bus import InMemoryEventBus

    orchestrator = Orchestrator(settings)
    orchestrator.bus = InMemoryEventBus()
    await orchestrator.bus.start()
    orchestrator.risk_manager = RiskManagerAgent(orchestrator.bus, settings)

    app = create_app()
    app.state.settings = settings
    app.state.orchestrator = orchestrator
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, orchestrator


def _snapshot(valor: str) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        total_value=Decimal(valor), cash_value=Decimal(valor), positions_value=Decimal(0)
    )


# ---------------------------------------------------------------------------
class TestAtaqueAutorizarCapitalPorUmClique:
    """D8: afrouxar limite de risco nao pode ser efeito colateral de um clique.

    `authorized_capital` E um limite de risco -- e o unico que decide quanto do
    patrimonio o sistema pode por para trabalhar. `PUT /api/risk/config` recusa
    sem `confirm=true`. `POST /api/risk/capital/authorize` sobe o MESMO limite
    sem corpo, sem confirmacao e sem nenhum parametro.
    """

    async def test_um_post_sem_corpo_sobe_o_limite_de_capital(self, controlled):
        http, orchestrator = controlled
        # Portao estreito, como o do ensaio real: 29,29 autorizados.
        await http.put(
            "/api/risk/config", json={"authorized_capital": "29.29", "confirm": True}
        )
        assert orchestrator.risk_manager.configured_limits.authorized_capital == Decimal(
            "29.29"
        )
        # Chegou um aporte: o patrimonio agora e 5.000.
        orchestrator.risk_manager.observe_snapshot(_snapshot("5000"))

        resposta = await http.post("/api/risk/capital/authorize")

        # AJUSTE DO IMPLEMENTADOR (rodada 2): o assert original era
        # `status_code == 200` junto com "o limite nao mudou", e os dois nao
        # podem valer ao mesmo tempo depois da correcao que o proprio critico
        # pediu -- uma rota que exige `confirm=true` recusa com 400 um POST sem
        # corpo. O que o teste mede continua sendo exatamente o mesmo: o portao
        # de 29,29 continua de pe depois do ataque.
        assert resposta.status_code == 400, resposta.text
        depois = orchestrator.risk_manager.configured_limits.authorized_capital
        assert depois == Decimal("29.29"), (
            "um POST sem corpo e sem confirm=true multiplicou por 170 o capital "
            f"que o sistema pode comprometer: {depois}"
        )

    async def test_a_rota_de_autorizar_exige_confirmacao_como_a_de_limites(
        self, controlled
    ):
        """Mesma trava, mesma consequencia: dinheiro real comprometido."""
        http, orchestrator = controlled
        orchestrator.risk_manager.observe_snapshot(_snapshot("5000"))

        resposta = await http.post("/api/risk/capital/authorize", json={})

        assert resposta.status_code == 400, (
            "autorizar todo o patrimonio foi aceito sem confirmacao explicita; "
            f"o corpo respondeu {resposta.status_code}"
        )


class TestAtaqueCsrfNasRotasDeControle:
    """A API nao tem autenticacao nenhuma e as rotas de controle sao POST sem
    corpo -- ou seja, "requisicao simples" para o navegador.

    CORS restringe LER a resposta; ele nao impede o pedido de ser enviado. Uma
    aba qualquer aberta na maquina do dono dispara estes efeitos.
    """

    ROTAS_SEM_CORPO: ClassVar[list[str]] = [
        "/api/risk/capital/authorize",
        "/api/risk/circuit-breaker/reset",
        "/api/agents/pause-all",
        "/api/agents/resume-all",
    ]

    @pytest.mark.parametrize("rota", ROTAS_SEM_CORPO)
    async def test_rota_de_controle_aceita_pedido_de_origem_estranha(
        self, controlled, rota
    ):
        http, orchestrator = controlled
        orchestrator.risk_manager.observe_snapshot(_snapshot("5000"))

        resposta = await http.post(rota, headers={"Origin": "https://site-qualquer.example"})

        assert resposta.status_code in (401, 403), (
            f"{rota} atendeu um pedido vindo de outra origem sem nenhuma "
            f"credencial: {resposta.status_code}"
        )

    async def test_put_de_limites_com_content_type_simples(self, controlled):
        """`text/plain` nao dispara preflight: se o PUT aceitar, o CORS nao
        protege nem a rota que exige confirm."""
        http, _ = controlled

        resposta = await http.put(
            "/api/risk/config",
            content=json.dumps({"clear_authorized_capital": True, "confirm": True}),
            headers={"Content-Type": "text/plain"},
        )

        assert resposta.status_code >= 400, (
            "a rota interpretou como JSON um corpo enviado como text/plain -- "
            "o pedido nao passa por preflight e o CORS nao protege nada"
        )


class TestPedidosSimultaneosNaRotaDeRisco:
    """Ataque 2 do dono ("dois sinais simultaneos") traduzido para a API.

    `update_limits` faz ler-alterar-gravar com `await` no meio e sem trava, o
    que deveria perder alteracao. MEDIDO, nao perde: as duas requisicoes se
    serializam (a rota resolve `Depends(db_session)` antes do handler, e a
    segunda so entra depois que a primeira soltou o banco). O rastro de
    auditoria confirma -- o `before` do segundo pedido ja e o resultado do
    primeiro.

    Fica como teste porque a seguranca aqui e ACIDENTAL: se a dependencia de
    sessao sair da rota, os oito pedidos abaixo passam a se atropelar.
    """

    PEDIDOS: ClassVar[list[tuple[str, object]]] = [
        ("max_order_notional", "10"),
        ("min_signal_confidence", 0.99),
        ("cooldown_seconds", 1234),
        ("max_open_positions", 2),
        ("stop_loss_pct", 0.02),
        ("take_profit_pct", 0.04),
        ("daily_loss_limit_pct", 0.01),
        ("max_asset_exposure_pct", 0.2),
    ]

    async def test_nenhum_limite_aceito_com_200_desaparece(self, controlled):
        http, orchestrator = controlled

        respostas = await asyncio.gather(
            *(
                http.put("/api/risk/config", json={campo: valor, "confirm": True})
                for campo, valor in self.PEDIDOS
            )
        )

        assert [r.status_code for r in respostas] == [200] * len(self.PEDIDOS), [
            r.status_code for r in respostas
        ]
        limites = orchestrator.risk_manager.configured_limits
        perdidos = [
            f"{campo}: pedi {valor}, vigora {getattr(limites, campo)}"
            for campo, valor in self.PEDIDOS
            if str(getattr(limites, campo)) != str(valor)
        ]
        assert perdidos == [], (
            f"{len(perdidos)} de {len(self.PEDIDOS)} pedidos receberam 200 e nao "
            "estao em vigor: " + "; ".join(perdidos)
        )


class TestAtaqueRelayDoWebsocketMorreDeVez:
    """Um unico evento que nao serializa mata o topico ate a API reiniciar.

    `relay` captura `Exception` e faz `return`. Se o topico for ALERTS, o dono
    para de receber pela tela o aviso de circuit breaker acionado -- e a tela
    fica identica a "nada aconteceu".
    """

    async def test_um_alerta_impossivel_de_serializar_cala_o_topico_para_sempre(self):
        from crypto_traders.bus import InMemoryEventBus, Topics

        class _Orquestrador:
            def __init__(self, bus):
                self.bus = bus

        bus = InMemoryEventBus()
        await bus.start()
        gerente = ws_module.ConnectionManager()
        cliente = _SocketDeMentira()
        await gerente.connect(cliente)

        original = ws_module.manager
        ws_module.manager = gerente
        app = type("App", (), {"state": type("S", (), {})()})()
        try:
            ws_module.attach_broadcaster(app, _Orquestrador(bus))
            await asyncio.sleep(0.05)

            # Alerta com chave nao-string: `json.dumps` levanta TypeError.
            await bus.publish(Topics.ALERTS, {("tupla",): "chave impossivel"})
            await asyncio.sleep(0.1)

            # Agora o alerta que importa de verdade.
            await bus.publish(
                Topics.ALERTS,
                {"type": "circuit_breaker", "title": "trava", "reason": "perda diaria"},
            )
            await asyncio.sleep(0.1)

            recebidos = [json.loads(t) for t in cliente.recebidas]
            tipos = [m["data"].get("type") for m in recebidos if isinstance(m["data"], dict)]
            assert "circuit_breaker" in tipos, (
                "o alerta de circuit breaker nunca chegou ao navegador: um evento "
                "anterior matou o relay do topico ALERTS de vez"
            )
        finally:
            await ws_module.detach_broadcaster(app)
            ws_module.manager = original
            await bus.stop()


class TestAtaqueTarefasDeEnvioVazamNoDesligamento:
    """`detach_broadcaster` desliga os relays e deixa as tarefas de envio vivas."""

    async def test_desligar_a_api_nao_deixa_tarefa_de_envio_pendurada(self):
        gerente = ws_module.ConnectionManager()
        cliente = _SocketDeMentira(travado=True)
        await gerente.connect(cliente)
        app = type("App", (), {"state": type("S", (), {})()})()
        # AJUSTE DO IMPLEMENTADOR (rodada 2): o teste original conectava o
        # navegador a um `ConnectionManager` recem-criado e nunca o punha no
        # lugar do `manager` do modulo -- ou seja, media a fuga de tarefas de um
        # fan-out que a API nao usa, e nenhuma correcao no desligamento poderia
        # alcanca-lo. Trocar o singleton (como o teste vizinho do relay ja faz,
        # linha 224) e o que faz o ataque bater no caminho real. O defeito
        # apontado era real e continua medido aqui.
        original = ws_module.manager
        ws_module.manager = gerente
        try:
            ws_module.attach_broadcaster(
                app, type("O", (), {"bus": _BusVazio()})()
            )
            await asyncio.sleep(0.01)

            await ws_module.detach_broadcaster(app)
            await asyncio.sleep(0.01)

            pendentes = [
                t
                for t in asyncio.all_tasks()
                if t.get_name() == "ws-writer" and not t.done()
            ]
            assert pendentes == [], (
                f"{len(pendentes)} tarefa(s) de envio continuam vivas depois do "
                "desligamento da API"
            )
            for t in pendentes:
                t.cancel()
        finally:
            ws_module.manager = original


class TestVarreduraDeDinheiroComDadoDeVerdade:
    """D7 nas rotas que o implementador declarou limpas.

    A varredura dele mediu as 14 rotas, mas o unico teste permanente de
    varredura cobre `/api/backtest` e o WebSocket. Rota de historico com o banco
    VAZIO devolve `[]`, e varrer `[]` nao prova nada -- e o mesmo vicio que
    deixou `equity_curve[].value` passar batido. Aqui cada rota e conferida com
    linha de verdade dentro, e o teste falha se o corpo vier vazio.
    """

    CAMPOS_DE_DINHEIRO = frozenset(
        {
            "price", "reference_price", "quantity", "notional", "stop_loss",
            "take_profit", "filled_quantity", "average_price", "fee",
            "approved_quantity", "approved_notional", "total_value",
            "cash_value", "positions_value", "realized_pnl", "unrealized_pnl",
            "market_value", "current_price", "max_order_notional",
            "min_order_notional", "authorized_capital", "unauthorized_value",
            "cash", "asset_exposure", "asset_quantity", "nivel",
        }
    )

    def _numeros(self, corpo, caminho: str = "") -> list[str]:
        if isinstance(corpo, dict):
            return [
                erro
                for chave, valor in corpo.items()
                for erro in self._numeros(valor, f"{caminho}.{chave}")
            ]
        if isinstance(corpo, list):
            return [
                erro
                for i, valor in enumerate(corpo)
                for erro in self._numeros(valor, f"{caminho}[{i}]")
            ]
        campo = caminho.split(".")[-1].split("[")[0]
        if campo in self.CAMPOS_DE_DINHEIRO and type(corpo) in (int, float):
            return [f"{caminho} = {corpo!r} ({type(corpo).__name__})"]
        return []

    @pytest.fixture
    async def povoado(self, settings):
        """Semeia sinal, ordem, evento de risco e auditoria de verdade.

        O evento de risco vem do RiskEngine REAL, e nao de um dict montado a
        mao: o `snapshot` de `RiskEventOut` e `dict[str, Any]`, exatamente o
        tipo sem contrato em que o defeito do backtest morava.
        """
        from crypto_traders.db.repositories import (
            AuditLogRepository,
            OrderRepository,
            RiskEventRepository,
            SignalRepository,
        )
        from crypto_traders.db.session import session_scope
        from crypto_traders.domain.enums import (
            ExchangeName,
            OrderStatus,
            OrderType,
            Side,
            SignalDirection,
        )
        from crypto_traders.domain.models import (
            IndicatorSnapshot,
            OrderRequest,
            OrderResult,
            Signal,
        )
        from crypto_traders.risk.rules import PortfolioState, RiskEngine

        sinal = Signal(
            exchange=ExchangeName.BINANCE,
            symbol="BTC/USDT",
            timeframe="1d",
            strategy="ma_crossover",
            direction=SignalDirection.LONG,
            confidence=0.9,
            reason="cruzamento",
            reference_price=Decimal("50000.123456789"),
            indicators=IndicatorSnapshot(values={"ma_fast": 1.0}),
        )
        engine = RiskEngine(settings.risk, quote_currency="USDT")
        estado = PortfolioState(
            total_value=Decimal("1000.123456789"),
            cash=Decimal("1000.123456789"),
        )
        avaliacao = engine.evaluate(sinal, estado)

        async with session_scope(settings) as session:
            await SignalRepository(session).save(sinal)
            await RiskEventRepository(session).save_assessment(avaliacao)
            pedido = OrderRequest(
                signal_id=sinal.id,
                risk_event_id=avaliacao.id,
                client_order_id="cat-critico-8",
                exchange=ExchangeName.BINANCE,
                symbol="BTC/USDT",
                side=Side.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("0.000123456789"),
                price=Decimal("50000.123456789"),
                notional=Decimal("6.172975"),
                stop_loss=Decimal("48500.11"),
                take_profit=Decimal("53000.22"),
                strategy="ma_crossover",
            )
            ordem = await OrderRepository(session).create_pending(pedido, "dry_run")
            await OrderRepository(session).apply_result(
                OrderResult(
                    order_request_id=ordem.id,
                    client_order_id=pedido.client_order_id,
                    exchange_order_id="x-critico-8",
                    status=OrderStatus.FILLED,
                    filled_quantity=Decimal("0.000123456789"),
                    average_price=Decimal("50000.123456789"),
                    fee=Decimal("0.0061729"),
                )
            )
            await AuditLogRepository(session).append(
                action="teste_critico",
                actor="critico",
                target="risk_config",
                after={"max_order_notional": "100"},
            )
        return settings

    @pytest.mark.parametrize(
        "rota",
        ["/api/signals", "/api/orders", "/api/risk/events", "/api/audit"],
    )
    async def test_nenhuma_rota_de_historico_manda_dinheiro_em_number(
        self, povoado, rota
    ):
        app = create_app()
        app.state.settings = povoado
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            resposta = await http.get(rota)

        assert resposta.status_code == 200, resposta.text
        corpo = resposta.json()
        assert corpo, f"{rota} devolveu vazio; varrer vazio nao prova nada"
        erros = self._numeros(corpo, rota)
        assert erros == [], f"dinheiro em number em {rota}: " + "; ".join(erros)


class TestAtaqueDoisPedidosSimultaneosNaConfigDeNegocio:
    """A mesma corrida de `PUT /api/risk/config`, na rota de negocio."""

    async def test_dois_puts_de_negocio_ao_mesmo_tempo_perdem_um_campo(
        self, controlled
    ):
        http, orchestrator = controlled

        primeiro, segundo = await asyncio.gather(
            http.put("/api/trading/config", json={"timeframe": "4h", "confirm": True}),
            http.put(
                "/api/trading/config",
                json={"candle_history_limit": 321, "confirm": True},
            ),
        )

        assert primeiro.status_code == 200, primeiro.text
        assert segundo.status_code == 200, segundo.text
        trading = orchestrator.settings.trading
        assert trading.timeframe == "4h" and trading.candle_history_limit == 321, (
            "dois PUT aceitos com 200 e o estado final tem so um: "
            f"timeframe={trading.timeframe} "
            f"candle_history_limit={trading.candle_history_limit}"
        )


class _BusVazio:
    async def subscribe(self, topic):
        while True:
            await asyncio.sleep(3600)
            yield None


class _SocketDeMentira:
    def __init__(self, *, travado: bool = False) -> None:
        self.travado = travado
        self.recebidas: list[str] = []
        self.fechado = False

    async def accept(self) -> None:
        return None

    async def close(self, code: int = 1000) -> None:
        self.fechado = True

    async def send_text(self, texto: str) -> None:
        if self.travado:
            await asyncio.Event().wait()
        self.recebidas.append(texto)


class TestChaveDeApiPelaBordaDaApi:
    """Ataque 4 do dono, do lado da API: chave de exchange vazia e chave real.

    `TestNoRouteEverReturnsASecret` planta senha de SMTP e token do WhatsApp --
    e nao planta a credencial da exchange, que e a unica capaz de mover o
    dinheiro do dono. A varredura precisa incluir o segredo que importa mais.
    """

    SEGREDOS: ClassVar[tuple[str, str]] = (
        "binance-api-key-em-claro-zzz",
        "binance-api-secret-em-claro-zzz",
    )

    #  fica fora: exige o orquestrador com todos os agentes
    # construidos.  foi conferido campo a campo (schemas.py:330-344)
    # e nao tem campo de credencial.
    ROTAS: ClassVar[list[str]] = [
        "/api/notifications/config",
        "/api/trading/config",
        "/api/risk/config",
        "/api/risk/capital",
        "/api/strategies",
        "/api/portfolio",
        "/api/trades",
        "/api/signals",
        "/api/orders",
        "/api/risk/events",
        "/api/audit",
        "/openapi.json",
    ]

    @pytest.fixture
    async def com_credencial(self, settings):
        from pydantic import SecretStr

        from crypto_traders.agents.orchestrator import Orchestrator
        from crypto_traders.agents.risk_manager import RiskManagerAgent
        from crypto_traders.bus import InMemoryEventBus
        from crypto_traders.config import ExchangeCredentials

        configurado = settings.model_copy(
            update={
                "binance": ExchangeCredentials(
                    api_key=SecretStr(self.SEGREDOS[0]),
                    api_secret=SecretStr(self.SEGREDOS[1]),
                )
            }
        )
        orchestrator = Orchestrator(configurado)
        orchestrator.bus = InMemoryEventBus()
        await orchestrator.bus.start()
        orchestrator.risk_manager = RiskManagerAgent(orchestrator.bus, configurado)

        app = create_app()
        app.state.settings = configurado
        app.state.orchestrator = orchestrator
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http

    @pytest.mark.parametrize("rota", ROTAS)
    async def test_nenhuma_rota_devolve_a_credencial_da_exchange(
        self, com_credencial, rota
    ):
        resposta = await com_credencial.get(rota)
        assert resposta.status_code == 200, f"{rota}: {resposta.status_code}"
        for segredo in self.SEGREDOS:
            assert segredo not in resposta.text, f"{rota} devolveu a credencial"

    async def test_chave_vazia_nao_faz_a_api_se_dizer_autenticada(self, settings):
        """`BINANCE__API_KEY=` e ausencia, nao chave vazia (D6)."""
        from crypto_traders.config import ExchangeCredentials

        vazio = ExchangeCredentials(api_key="  ", api_secret="")
        assert vazio.configured is False, (
            "credencial em branco se considerou configurada; com isso o sistema "
            "tentaria enviar ordem em vez de recusar a subida"
        )


class TestIgualdadeExataNosTetos:
    """Ataque 1 do dono, na borda da API: igualdade, nao "proximo de"."""

    async def test_teto_igual_ao_piso_e_aceito_e_nao_arredondado(self, controlled):
        http, orchestrator = controlled

        resposta = await http.put(
            "/api/risk/config",
            json={
                "min_order_notional": "25.00",
                "max_order_notional": "25.00",
                "confirm": True,
            },
        )

        assert resposta.status_code == 200, resposta.text
        corpo = resposta.json()
        assert corpo["min_order_notional"] == corpo["max_order_notional"] == "25.00", corpo
        limites = orchestrator.risk_manager.configured_limits
        assert limites.min_order_notional == limites.max_order_notional == Decimal("25.00")

    async def test_capital_exatamente_autorizado_nao_deixa_sobra(self, controlled):
        """Patrimonio IGUAL ao autorizado: nao pode sobrar centavo pendente."""
        http, orchestrator = controlled
        orchestrator.risk_manager.observe_snapshot(_snapshot("29.29"))
        await http.put(
            "/api/risk/config", json={"authorized_capital": "29.29", "confirm": True}
        )

        corpo = (await http.get("/api/risk/capital")).json()

        assert corpo["total_value"] == corpo["authorized_capital"] == "29.29", corpo
        assert corpo["unauthorized_value"] == "0", (
            "com patrimonio igual ao autorizado sobrou valor pendente: "
            f"{corpo['unauthorized_value']}"
        )
        assert corpo["gate_active"] is True


class TestOEndpointRealDeWebsocket:
    """A rota `/ws` de verdade, pela pilha ASGI.

    Os sete testes de WebSocket da rodada 1 conduzem o `ConnectionManager`
    direto, com um socket de mentira. Nenhum passa pelo `websocket_endpoint`
    real -- e o refactor mudou justamente o que `connect`/`disconnect` fazem.
    Um `accept()` que nao acontecesse, ou um `disconnect` que deixasse a conexao
    na lista, nao apareceria em nenhum teste.
    """

    def test_o_navegador_conecta_recebe_e_sai_da_lista(self, settings):
        from starlette.testclient import TestClient

        from crypto_traders.domain.models import PortfolioSnapshot as _Snap

        app = create_app()
        app.state.settings = settings
        gerente = ws_module.manager
        antes = gerente.count

        with TestClient(app) as cliente, cliente.websocket_connect("/ws") as socket:
            assert gerente.count == antes + 1, "a conexao real nao entrou na lista"
            portal = socket.portal  # loop do TestClient, onde o gerente vive
            portal.call(
                gerente.broadcast,
                "portfolio",
                _Snap(
                    total_value=Decimal("29.29"),
                    cash_value=Decimal("29.29"),
                    positions_value=Decimal(0),
                ),
            )
            mensagem = socket.receive_json()

        assert mensagem["event"] == "portfolio"
        assert mensagem["data"]["total_value"] == "29.29", (
            "dinheiro chegou ao navegador real como number: "
            f"{mensagem['data']['total_value']!r}"
        )
        assert gerente.count == antes, (
            "a conexao fechada continuou na lista do fan-out: o proximo "
            "broadcast enfileira para um socket morto"
        )
