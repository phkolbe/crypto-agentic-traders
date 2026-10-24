"""O que a ida para o Docker acrescentou: schema so pelo `migrar`, telas servidas
pela API, telemetria (metricas, logs) e a copia do historico SQLite -> Postgres.

Mesma regra do resto da suite: o teste tem de ver a coisa AGIR. A subida sem
`migrar` tem de recusar banco vazio (e nao cria-lo em silencio); o usuario da
aplicacao no Postgres tem de ser RECUSADO pelo servidor ao tentar derrubar
gatilho; a metrica tem de mudar quando o estado muda.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from crypto_traders import observabilidade
from crypto_traders.api.app import PortaoTailscale, _servir_telas
from crypto_traders.bus.base import Topics
from crypto_traders.bus.memory import InMemoryEventBus
from crypto_traders.config import Settings
from crypto_traders.db.models import ProtecaoAusenteError
from crypto_traders.db.repositories import TradeRepository
from crypto_traders.db.session import dispose_engine, init_db, session_scope

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")


# ---------------------------------------------------------------------------
# Barramento: observador ve tudo e nao derruba nada
# ---------------------------------------------------------------------------
class TestObservadorDoBarramento:
    async def test_observador_ve_cada_publicacao(self):
        bus = InMemoryEventBus()
        await bus.start()
        vistos: list[tuple[str, object]] = []
        bus.observar(lambda topico, payload: vistos.append((topico, payload)))

        await bus.publish(Topics.ALERTS, {"x": 1})
        await bus.publish(Topics.HEARTBEATS, {"y": 2})

        assert [t for t, _ in vistos] == [Topics.ALERTS, Topics.HEARTBEATS]

    async def test_observador_que_quebra_nao_impede_a_entrega(self):
        bus = InMemoryEventBus()
        await bus.start()

        def quebra(_topico, _payload):
            raise RuntimeError("metrica com defeito")

        bus.observar(quebra)
        fluxo = bus.subscribe(Topics.ORDER_RESULTS)
        proximo = fluxo.__anext__()
        # A assinatura so existe depois do primeiro passo do gerador.
        import asyncio

        tarefa = asyncio.ensure_future(proximo)
        await asyncio.sleep(0)
        await bus.publish(Topics.ORDER_RESULTS, "ordem")

        assert await asyncio.wait_for(tarefa, 1) == "ordem"
        await bus.stop()


# ---------------------------------------------------------------------------
# Subida sem DDL (DB_MIGRAR_NA_SUBIDA=false)
# ---------------------------------------------------------------------------
class TestSubidaSemMigrar:
    async def test_banco_vazio_recusa_subir_e_nao_cria_nada(self, tmp_path):
        arquivo = tmp_path / "vazio.db"
        await dispose_engine()
        cfg = Settings(database_url=f"sqlite+aiosqlite:///{arquivo.as_posix()}")
        try:
            with pytest.raises(ProtecaoAusenteError, match=r"crypto-traders migrar"):
                await init_db(cfg, migrar=False)
        finally:
            await dispose_engine()

        conn = sqlite3.connect(arquivo)
        try:
            tabelas = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        finally:
            conn.close()
        assert tabelas == 0, "a subida sem migrar criou schema"

    async def test_banco_migrado_sobe_sem_migrar(self, tmp_path):
        arquivo = tmp_path / "migrado.db"
        await dispose_engine()
        cfg = Settings(database_url=f"sqlite+aiosqlite:///{arquivo.as_posix()}")
        try:
            await init_db(cfg, migrar=True)
            await dispose_engine()
            await init_db(cfg, migrar=False)
        finally:
            await dispose_engine()

    async def test_gatilho_derrubado_por_fora_impede_a_subida(self, tmp_path):
        arquivo = tmp_path / "sem_gatilho.db"
        await dispose_engine()
        cfg = Settings(database_url=f"sqlite+aiosqlite:///{arquivo.as_posix()}")
        try:
            await init_db(cfg, migrar=True)
        finally:
            await dispose_engine()
        conn = sqlite3.connect(arquivo)
        try:
            conn.execute("DROP TRIGGER audit_log_sem_update")
            conn.commit()
        finally:
            conn.close()

        try:
            with pytest.raises(ProtecaoAusenteError, match="audit_log_sem_update"):
                await init_db(cfg, migrar=False)
        finally:
            await dispose_engine()


# ---------------------------------------------------------------------------
# Telas servidas pela API (imagem Docker, sem Vite)
# ---------------------------------------------------------------------------
class TestTelas:
    @pytest.fixture
    def cliente(self, tmp_path):
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html>indice</html>", encoding="utf-8")
        (dist / "assets" / "app-123.js").write_text("console.log(1)", encoding="utf-8")
        (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
        (tmp_path / "segredo.txt").write_text("nao pode sair", encoding="utf-8")

        app = FastAPI()

        @app.get("/api/health")
        async def health():
            return {"ok": True}

        _servir_telas(app, dist)
        return TestClient(app)

    def test_raiz_e_rota_do_react_devolvem_o_indice(self, cliente):
        for caminho in ("/", "/risco", "/configuracoes/notificacoes"):
            resposta = cliente.get(caminho)
            assert resposta.status_code == 200
            assert "indice" in resposta.text
            assert resposta.headers["cache-control"] == "no-cache"

    def test_arquivos_do_build_saem_como_arquivo(self, cliente):
        assert cliente.get("/assets/app-123.js").text == "console.log(1)"
        assert cliente.get("/favicon.svg").text == "<svg/>"

    def test_api_desconhecida_continua_404(self, cliente):
        assert cliente.get("/api/health").json() == {"ok": True}
        assert cliente.get("/api/nao-existe").status_code == 404
        assert cliente.get("/api").status_code == 404

    def test_nao_sai_arquivo_de_fora_do_build(self, cliente):
        resposta = cliente.get("/..%2fsegredo.txt")
        assert "nao pode sair" not in resposta.text

    def test_sem_build_nada_e_registrado(self, tmp_path):
        app = FastAPI()
        _servir_telas(app, tmp_path / "nao-existe")
        assert TestClient(app).get("/").status_code == 404


# ---------------------------------------------------------------------------
# Trava de producao: so o login Tailscale autorizado passa (HTTP e WebSocket)
# ---------------------------------------------------------------------------
class TestPortaoTailscale:
    LOGIN = "paulo@exemplo.com"

    @pytest.fixture
    def app(self):
        app = FastAPI()

        @app.get("/api/health")
        async def health():
            return {"ok": True}

        @app.post("/api/risk/circuit-breaker/reset")
        async def rearmar():
            return {"rearmado": True}

        @app.websocket("/ws")
        async def tempo_real(ws: WebSocket):
            await ws.accept()
            await ws.send_text("oi")
            await ws.close()

        app.add_middleware(PortaoTailscale, login=self.LOGIN)
        return app

    def test_sem_login_e_recusado_e_a_rota_nao_roda(self, app):
        resposta = TestClient(app).post("/api/risk/circuit-breaker/reset")
        assert resposta.status_code == 403
        assert "rearmado" not in resposta.text

    def test_login_errado_e_recusado(self, app):
        resposta = TestClient(app).post(
            "/api/risk/circuit-breaker/reset", headers={"Tailscale-User-Login": "sarah@exemplo.com"}
        )
        assert resposta.status_code == 403

    def test_o_login_autorizado_passa_sem_diferenciar_maiusculas(self, app):
        resposta = TestClient(app).post(
            "/api/risk/circuit-breaker/reset", headers={"Tailscale-User-Login": "Paulo@Exemplo.com"}
        )
        assert resposta.json() == {"rearmado": True}

    def test_websocket_sem_login_nao_abre(self, app):
        with pytest.raises(WebSocketDisconnect), TestClient(app).websocket_connect("/ws"):
            pass

    def test_websocket_com_login_abre(self, app):
        with TestClient(app).websocket_connect(
            "/ws", headers={"Tailscale-User-Login": self.LOGIN}
        ) as ws:
            assert ws.receive_text() == "oi"

    def test_sonda_local_de_saude_passa_e_so_ela(self, app):
        local = TestClient(app, client=("127.0.0.1", 50000))
        assert local.get("/api/health").status_code == 200
        # Do proprio conteiner, mas fora da sonda: continua recusado.
        assert local.post("/api/risk/circuit-breaker/reset").status_code == 403
        # A sonda vinda de fora (pelo serve) nao tem excecao.
        assert TestClient(app).get("/api/health").status_code == 403

    def test_sem_configuracao_a_trava_nao_existe(self):
        from crypto_traders.api.app import create_app

        app = create_app()
        assert not any(m.cls is PortaoTailscale for m in app.user_middleware)


# ---------------------------------------------------------------------------
# Metricas: mudam quando o estado muda
# ---------------------------------------------------------------------------
def _ler(leitor: InMemoryMetricReader) -> dict[str, dict[frozenset, float]]:
    valores: dict[str, dict[frozenset, float]] = {}
    dados = leitor.get_metrics_data()
    for rm in dados.resource_metrics if dados else []:
        for sm in rm.scope_metrics:
            for metrica in sm.metrics:
                pontos = valores.setdefault(metrica.name, {})
                for ponto in metrica.data.data_points:
                    pontos[frozenset(ponto.attributes.items())] = ponto.value
    return valores


@pytest.fixture
def metricas(monkeypatch):
    monkeypatch.setattr(observabilidade, "_estado", {})
    monkeypatch.setattr(observabilidade, "_contadores", {})
    leitor = InMemoryMetricReader()
    provedor = MeterProvider(metric_readers=[leitor])
    observabilidade._registrar_metricas(provedor.get_meter("teste"))
    yield leitor
    provedor.shutdown()


def _saude(**sobrescreve):
    agora = datetime.now(UTC)
    saude = {
        "mode": "dry_run",
        "exchange": "binance",
        "symbols": ["BTC/USDC", "ETH/USDC"],
        "circuit_breaker_active": False,
        "sizing_feasible": True,
        "tasks": {"watchdog": True, "alertas": False},
        "agents": {
            "market_data": {
                "state": "running",
                "running": True,
                "paused": False,
                "stale": False,
                "given_up": False,
                "last_heartbeat": (agora - timedelta(seconds=30)).isoformat(),
                "pending_events": 2,
                "deaf_topics": [],
                "restarts": 0,
                "lost_events": 0,
            },
            "execution": {
                "state": "running",
                "running": True,
                "paused": False,
                "stale": True,
                "given_up": False,
                "last_heartbeat": None,
                "pending_events": 0,
                "deaf_topics": ["risk.order_requests"],
                "restarts": 3,
                "lost_events": 1,
                "openings_blocked": True,
            },
        },
    }
    saude.update(sobrescreve)
    return saude


class TestMetricas:
    def test_sem_estado_lido_nao_ha_serie_inventada(self, metricas):
        valores = _ler(metricas)
        assert not valores.get("crypto_circuit_breaker_ativo")
        assert not valores.get("crypto_agente_saudavel")

    def test_estado_do_orquestrador_vira_metrica(self, metricas):
        observabilidade._estado["saude"] = _saude(circuit_breaker_active=True)
        observabilidade._estado["lido_em"] = datetime.now(UTC)
        valores = _ler(metricas)

        assert valores["crypto_info"] == {
            frozenset({("modo", "dry_run"), ("exchange", "binance")}): 1
        }
        assert valores["crypto_circuit_breaker_ativo"] == {frozenset(): 1}
        assert valores["crypto_aberturas_bloqueadas"] == {frozenset(): 1}
        assert valores["crypto_pares_ativos"] == {frozenset(): 2}
        saudavel = valores["crypto_agente_saudavel"]
        assert saudavel[frozenset({("agente", "market_data")})] == 1
        # Atrasado ("stale") nao e saudavel, mesmo rodando e sem erro.
        assert saudavel[frozenset({("agente", "execution")})] == 0
        idade = valores["crypto_agente_heartbeat_idade"]
        assert 25 <= idade[frozenset({("agente", "market_data")})] <= 60
        # Sem batida nenhuma: sem ponto, e nao idade zero.
        assert frozenset({("agente", "execution")}) not in idade
        assert valores["crypto_tarefa_viva"][frozenset({("tarefa", "alertas")})] == 0
        assert valores["crypto_agente_reinicios"][frozenset({("agente", "execution")})] == 3
        assert valores["crypto_agente_topicos_surdos"][frozenset({("agente", "execution")})] == 1

        observabilidade._estado["saude"] = _saude(circuit_breaker_active=False)
        assert _ler(metricas)["crypto_circuit_breaker_ativo"] == {frozenset(): 0}

    def test_execucao_liberada_e_zero_e_nao_sem_dado(self, metricas):
        # `openings_blocked` e o MOTIVO (texto) ou None; None e "liberado", que tem
        # de aparecer como 0 -- sem ponto, o painel dizia "No data" (visto no Grafana).
        saude = _saude()
        saude["agents"]["execution"]["openings_blocked"] = None
        observabilidade._estado["saude"] = saude
        assert _ler(metricas)["crypto_aberturas_bloqueadas"] == {frozenset(): 0}

    def test_eventos_do_barramento_viram_contadores_e_snapshot(self, metricas):
        observabilidade._estado["moeda"] = "USDC"
        observabilidade.contar_evento(
            Topics.SIGNALS, SimpleNamespace(strategy="ma_crossover", direction="long")
        )
        observabilidade.contar_evento(Topics.RISK_ASSESSMENTS, SimpleNamespace(decision="rejected"))
        observabilidade.contar_evento(Topics.RISK_ASSESSMENTS, SimpleNamespace(decision="rejected"))
        observabilidade.contar_evento(Topics.ORDER_RESULTS, SimpleNamespace(status="filled"))
        observabilidade.contar_evento(
            Topics.PORTFOLIO_SNAPSHOTS,
            SimpleNamespace(
                timestamp=datetime.now(UTC),
                total_value=Decimal("1000.5"),
                cash_value=Decimal("400"),
                positions_value=Decimal("600.5"),
                realized_pnl=Decimal("-2"),
                unrealized_pnl=Decimal("3.25"),
                positions=[
                    SimpleNamespace(asset="BTC", quantity=Decimal("0.1")),
                    SimpleNamespace(asset="ETH", quantity=Decimal("0")),
                    # O caixa vem na lista do snapshot e nao e posicao aberta.
                    SimpleNamespace(asset="USDC", quantity=Decimal("400")),
                ],
            ),
        )
        valores = _ler(metricas)

        assert valores["crypto_risco_decisoes"] == {frozenset({("decisao", "rejected")}): 2}
        assert valores["crypto_ordens"] == {frozenset({("status", "filled")}): 1}
        assert valores["crypto_sinais"] == {
            frozenset({("estrategia", "ma_crossover"), ("direcao", "long")}): 1
        }
        assert valores["crypto_eventos"][frozenset({("topico", Topics.RISK_ASSESSMENTS)})] == 2
        assert valores["crypto_patrimonio"][frozenset({("parte", "total")})] == 1000.5
        assert valores["crypto_pnl"][frozenset({("tipo", "nao_realizado")})] == 3.25
        # Posicao zerada nao conta como aberta.
        assert valores["crypto_posicoes_abertas"] == {frozenset(): 1}


# ---------------------------------------------------------------------------
# Logs: a linha que vai ao coletor e a mesma, ja redigida
# ---------------------------------------------------------------------------
class _Coleta(logging.Handler):
    def __init__(self):
        super().__init__()
        self.registros: list[logging.LogRecord] = []

    def emit(self, record):
        self.registros.append(record)


class TestEspelhoDeLog:
    def test_campos_viram_atributos_sem_colidir_com_o_registro(self, monkeypatch):
        espelho = logging.getLogger("teste.espelho")
        espelho.propagate = False
        espelho.setLevel(logging.DEBUG)
        coleta = _Coleta()
        espelho.addHandler(coleta)
        monkeypatch.setattr(observabilidade, "_logger_espelho", espelho)

        observabilidade._espelhar_log(
            "warning",
            {
                "event": "risk.rejeitado",
                "level": "warning",
                "timestamp": "x",
                "name": "colide",
                "motivos": ["cooldown"],
                "api_key": "***",
            },
        )

        (registro,) = coleta.registros
        assert registro.levelno == logging.WARNING
        assert registro.getMessage() == "risk.rejeitado"
        assert registro.name_ == "colide"
        assert registro.motivos == "['cooldown']"
        assert registro.api_key == "***"

    def test_redacao_acontece_antes_do_espelho(self, monkeypatch):
        from crypto_traders import logging_setup

        vistos: list[dict] = []
        monkeypatch.setattr(logging_setup, "_espelho", lambda _m, ev: vistos.append(dict(ev)))
        evento = logging_setup._redact_secrets(None, "info", {"event": "x", "api_secret": "abc"})
        logging_setup._espelhar(None, "info", evento)
        assert vistos == [{"event": "x", "api_secret": "***"}]


# ---------------------------------------------------------------------------
# Postgres de verdade: privilegio separado e copia do historico
# ---------------------------------------------------------------------------
@pytest.fixture
async def pg_novo():
    """Um database e um usuario de aplicacao NOVOS, so deste teste.

    Precisa de um POSTGRES_TEST_URL com permissao de criar database e role (o
    superusuario do compose de teste); o banco e o usuario somem no fim.
    """
    base = make_url(POSTGRES_TEST_URL)
    sufixo = uuid.uuid4().hex[:8]
    banco, app, senha = f"crypto_t_{sufixo}", f"crypto_app_{sufixo}", "app-teste"
    admin = create_async_engine(base, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{banco}"'))
            await conn.execute(text(f"CREATE ROLE \"{app}\" LOGIN PASSWORD '{senha}'"))
            await conn.execute(text(f'GRANT CONNECT ON DATABASE "{banco}" TO "{app}"'))
    except Exception as exc:  # pragma: no cover - servidor sem permissao
        await admin.dispose()
        pytest.skip(f"sem permissao para criar database/role de teste: {exc}")
    dono = base.set(database=banco)
    aplicacao = dono.set(username=app, password=senha)
    yield (
        dono.render_as_string(hide_password=False),
        aplicacao.render_as_string(hide_password=False),
        app,
    )
    await dispose_engine()
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{banco}" WITH (FORCE)'))
        await conn.execute(text(f'DROP ROLE IF EXISTS "{app}"'))
    await admin.dispose()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="POSTGRES_TEST_URL nao definida")
class TestPostgresComUsuarioDaAplicacao:
    async def test_aplicacao_nao_derruba_gatilho_e_continua_gravando(self, pg_novo):
        from crypto_traders.cli import _migrar

        url_dono, url_app, usuario = pg_novo
        assert await _migrar(Settings(database_url=url_dono), usuario) == 0

        app_cfg = Settings(database_url=url_app, db_migrar_na_subida=False)
        await init_db(app_cfg)  # so confere: o app nao tem como criar nada

        async with session_scope(app_cfg) as ses:
            await TradeRepository(ses).record(
                executed_at=datetime(2026, 10, 1, tzinfo=UTC),
                exchange="binance",
                symbol="BTC/USDC",
                side="buy",
                quantity=Decimal("0.001"),
                price=Decimal("60000"),
                fee=Decimal("0.01"),
                realized_pnl=Decimal("0"),
            )

        engine = create_async_engine(url_app)
        try:
            # O SERVIDOR recusa (privilegio), nao a lista de palavras do processo:
            # por isso o comando vai pelo driver cru, sem passar pelo SQLAlchemy.
            async with engine.connect() as conn:
                bruto = await conn.get_raw_connection()
                with pytest.raises(Exception, match=r"(?i)owner|permission|privilege"):
                    await bruto.driver_connection.execute(
                        "DROP TRIGGER trades_sem_update ON trades"
                    )
                with pytest.raises(Exception, match=r"(?i)permission|privilege"):
                    await bruto.driver_connection.execute("CREATE TABLE intrusa (x int)")
                with pytest.raises(Exception, match=r"append-only"):
                    await bruto.driver_connection.execute("UPDATE trades SET price = 1")
        finally:
            await engine.dispose()
        await dispose_engine()

    async def test_copia_do_sqlite_leva_tudo_e_recusa_destino_com_dados(
        self, pg_novo, settings
    ):
        from crypto_traders.cli import _copiar_sqlite, _migrar
        from crypto_traders.db.models import AuditLog

        # Origem: o SQLite do fixture `settings`, com uma trade e uma auditoria.
        async with session_scope(settings) as ses:
            await TradeRepository(ses).record(
                executed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
                exchange="binance",
                symbol="ETH/USDC",
                side="sell",
                quantity=Decimal("0.123456789012345678"),
                price=Decimal("2500"),
                fee=Decimal("0.000000000000000001"),
                realized_pnl=Decimal("-1.5"),
            )
            ses.add(AuditLog(actor="teste", action="limits_updated", detail="copia"))
        arquivo = make_url(settings.database_url).database
        await dispose_engine()

        url_dono, _url_app, usuario = pg_novo
        assert await _migrar(Settings(database_url=url_dono), usuario) == 0
        destino = Settings(database_url=url_dono)
        assert await _copiar_sqlite(destino, Path(arquivo)) == 0

        async with session_scope(destino) as ses:
            (trade,) = await TradeRepository(ses).list(limit=5)
            linhas_audit = (await ses.execute(text("SELECT count(*) FROM audit_log"))).scalar()
        assert trade.quantity == Decimal("0.123456789012345678")
        assert trade.fee == Decimal("0.000000000000000001")
        assert trade.executed_at == datetime(2026, 9, 1, 12, tzinfo=UTC)
        assert linhas_audit >= 1
        await dispose_engine()

        # A sequencia andou: um INSERT novo nao colide com o id copiado.
        async with session_scope(destino) as ses:
            await TradeRepository(ses).record(
                executed_at=datetime(2026, 10, 2, tzinfo=UTC),
                exchange="binance",
                symbol="ETH/USDC",
                side="buy",
                quantity=Decimal("1"),
                price=Decimal("2400"),
                fee=Decimal("0"),
                realized_pnl=Decimal("0"),
            )
        await dispose_engine()

        # Segunda copia: destino com dados, recusada sem gravar nada.
        assert await _copiar_sqlite(destino, Path(arquivo)) == 1
