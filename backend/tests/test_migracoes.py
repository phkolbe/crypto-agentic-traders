"""Migracoes versionadas e a conferencia do banco contra o modelo (auditoria de
09/11/2026, banco F2/F3/F4).

O defeito de origem: o `migrar` era so `create_all`, que nao toca em tabela que
ja existe, e a subida conferia so os gatilhos. Uma coluna nova em `trades`
passava pelo `migrar` ("schema e gatilhos prontos"), a aplicacao subia, e o
primeiro fill falhava no INSERT -- ordem executada na exchange, PENDING e sem
trade no banco.

A metade SQLite roda sempre. A metade Postgres precisa de `POSTGRES_TEST_URL`
apontando para um servidor DESCARTAVEL com permissao de criar database e role:
cada teste cria o proprio database e o apaga no fim. O banco "antigo" (versao 1)
e criado a partir de `tests/dados/schema_v1_postgres.sql`, o `pg_dump` do schema
que o `migrar` da main deixava.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from structlog.testing import capture_logs

from crypto_traders.config import Settings
from crypto_traders.db import models as orm
from crypto_traders.db.migracoes import (
    VERSAO_ATUAL,
    SchemaDivergenteError,
    divergencias_do_schema,
)
from crypto_traders.db.models import AplicacaoPodeDerrubarGatilhoError, ProtecaoAusenteError
from crypto_traders.db.repositories import TradeRepository
from crypto_traders.db.session import dispose_engine, get_engine, init_db, session_scope
from crypto_traders.domain.enums import TradeOrigin

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
SCHEMA_V1 = Path(__file__).parent / "dados" / "schema_v1_postgres.sql"


def _arquivo(settings: Settings) -> str:
    return make_url(settings.database_url).database


def _sqlite(settings: Settings, *comandos: str) -> None:
    """DDL por fora da aplicacao, como o dono (ou um descuido) faria."""
    conn = sqlite3.connect(_arquivo(settings))
    try:
        for comando in comandos:
            conn.execute(comando)
        conn.commit()
    finally:
        conn.close()


def _versoes_sqlite(settings: Settings) -> list[int]:
    conn = sqlite3.connect(_arquivo(settings))
    try:
        return [v for (v,) in conn.execute("SELECT versao FROM schema_versao ORDER BY versao")]
    finally:
        conn.close()


async def _divergencias(settings: Settings) -> list[str]:
    async with get_engine(settings).connect() as conn:
        return await conn.run_sync(divergencias_do_schema)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
class TestVersaoESchemaNoSqlite:
    async def test_banco_novo_nasce_na_versao_atual_e_igual_ao_modelo(self, settings):
        assert _versoes_sqlite(settings) == list(range(1, VERSAO_ATUAL + 1))
        assert await _divergencias(settings) == []

    async def test_banco_sem_versao_recusa_subir_e_o_migrar_o_leva_a_versao_atual(
        self, settings
    ):
        _sqlite(settings, "DROP TABLE schema_versao")
        await dispose_engine()
        with pytest.raises(SchemaDivergenteError, match=r"crypto-traders migrar"):
            await init_db(settings, migrar=False)
        await dispose_engine()

        with capture_logs() as logs:
            await init_db(settings, migrar=True)
        eventos = [e["event"] for e in logs]
        assert "db.schema_sem_versao" in eventos
        assert "db.migracao_aplicada" in eventos
        assert _versoes_sqlite(settings) == list(range(1, VERSAO_ATUAL + 1))
        await dispose_engine()
        await init_db(settings, migrar=False)

    async def test_coluna_que_falta_recusa_a_subida_e_o_migrar(self, settings):
        """O cenario do relatorio, do lado que da para medir sem mudar o modelo:
        o banco sem uma coluna que o modelo grava."""
        await dispose_engine()
        _sqlite(settings, "ALTER TABLE agent_runs DROP COLUMN detail")
        with pytest.raises(SchemaDivergenteError, match=r"agent_runs\.detail: coluna ausente"):
            await init_db(settings, migrar=False)
        await dispose_engine()
        # O `migrar` tambem confere no fim, e falha com o dono olhando.
        with pytest.raises(SchemaDivergenteError, match=r"agent_runs\.detail"):
            await init_db(settings, migrar=True)

    async def test_indice_unico_que_falta_recusa_a_subida(self, settings):
        await dispose_engine()
        _sqlite(settings, "DROP INDEX uq_trades_order_agent")
        with pytest.raises(SchemaDivergenteError, match="uq_trades_order_agent"):
            await init_db(settings, migrar=False)

    async def test_indice_comum_que_falta_so_avisa(self, settings):
        await dispose_engine()
        _sqlite(settings, "DROP INDEX ix_trades_symbol")
        with capture_logs() as logs:
            await init_db(settings, migrar=False)
        avisos = [e for e in logs if e["event"] == "db.schema_diferente_sem_risco"]
        assert avisos and "ix_trades_symbol" in str(avisos[0]["diferencas"])

    async def test_banco_numa_versao_mais_nova_que_o_codigo_recusa(self, settings):
        await dispose_engine()
        _sqlite(
            settings,
            "INSERT INTO schema_versao (versao, descricao, aplicada_em) "
            "VALUES (99, 'do futuro', '2027-01-01 00:00:00')",
        )
        with pytest.raises(SchemaDivergenteError, match="codigo ANTIGO"):
            await init_db(settings, migrar=False)
        await dispose_engine()
        with pytest.raises(SchemaDivergenteError, match="codigo ANTIGO"):
            await init_db(settings, migrar=True)

    async def test_gatilho_de_delete_fecha_origem_nula(self, settings):
        """`IS NOT` no lugar de `<>`: com `origin` NULL, `<>` deixava apagar."""
        ddl = [
            linha
            for linha in orm._GATILHOS_TRADES["sqlite"] + orm._GATILHOS_TRADES["postgresql"]
            if "delete" in linha.lower() and "origin" in linha
        ]
        assert ddl and all("<>" not in linha for linha in ddl), ddl
        assert any("IS DISTINCT FROM" in linha for linha in ddl)


# ---------------------------------------------------------------------------
# Postgres de verdade (descartavel)
# ---------------------------------------------------------------------------
def _dsn_cru(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


async def _executar_cru(url: str, *comandos: str) -> None:
    """Pelo driver, sem SQLAlchemy: e o dono (ou o operador) no psql."""
    import asyncpg

    conn = await asyncpg.connect(_dsn_cru(url))
    try:
        for comando in comandos:
            await conn.execute(comando)
    finally:
        await conn.close()


async def _consultar_cru(url: str, consulta: str) -> list:
    import asyncpg

    conn = await asyncpg.connect(_dsn_cru(url))
    try:
        return await conn.fetch(consulta)
    finally:
        await conn.close()


@pytest.fixture
async def pg():
    """Um database NOVO, so deste teste, e um papel de aplicacao sem privilegio."""
    base = make_url(POSTGRES_TEST_URL)
    sufixo = uuid.uuid4().hex[:8]
    banco, app = f"crypto_mig_{sufixo}", f"crypto_app_{sufixo}"
    admin = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{banco}"'))
        await conn.execute(text(f"CREATE ROLE \"{app}\" LOGIN PASSWORD 'app-teste'"))
        await conn.execute(text(f'GRANT CONNECT ON DATABASE "{banco}" TO "{app}"'))
    dono = base.set(database=banco)
    yield {
        "dono": dono.render_as_string(hide_password=False),
        "app": dono.set(username=app, password="app-teste").render_as_string(
            hide_password=False
        ),
        "usuario_app": app,
    }
    await dispose_engine()
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{banco}" WITH (FORCE)'))
        await conn.execute(text(f'DROP ROLE IF EXISTS "{app}"'))
    await admin.dispose()


async def _banco_v1(url: str, *linhas: str) -> None:
    """O banco como o `migrar` da main o deixava, com as linhas dadas."""
    await _executar_cru(url, SCHEMA_V1.read_text(encoding="utf-8"), *linhas)


def _trade_v1(id_: str, order_id: str | None, origin: str = "agent", mode: str = "live") -> str:
    ordem = f"'{order_id}'" if order_id else "NULL"
    return (
        "INSERT INTO trades (id, created_at, executed_at, exchange, symbol, side, quantity, "
        "price, notional, fee, origin, order_id, mode) VALUES "
        f"('{id_}', now(), now(), 'binance', 'BTC/USDC', 'buy', 0.001, 60000, 60, 0, "
        f"'{origin}', {ordem}, '{mode}')"
    )


async def _migrar(url: str, usuario: str | None = None, **extra) -> int:
    from crypto_traders.cli import _migrar as migrar

    return await migrar(Settings(database_url=url, **extra), usuario)


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="POSTGRES_TEST_URL nao definida")
class TestMigracaoNoPostgres:
    async def test_banco_antigo_recusa_subir_ate_o_migrar_e_os_dados_ficam(self, pg):
        url = pg["dono"]
        await _banco_v1(
            url,
            _trade_v1("t1", "ordem-1"),
            _trade_v1("t2", "ordem-2"),
            _trade_v1("m1", None, origin="manual", mode="manual"),
            "INSERT INTO audit_log (\"timestamp\", actor, action, before, after) "
            "VALUES (now(), 'teste', 'limits_updated', '{}', '{}')",
        )

        # A subida da aplicacao (sem DDL) recusa o banco antigo, nomeando o remedio.
        with pytest.raises(SchemaDivergenteError, match=r"schema_versao.*crypto-traders migrar"):
            await init_db(Settings(database_url=url), migrar=False)
        await dispose_engine()

        assert await _migrar(url) == 0

        versoes = [r["versao"] for r in await _consultar_cru(
            url, "SELECT versao FROM schema_versao ORDER BY versao"
        )]
        assert versoes == [1, 2]
        (indice,) = await _consultar_cru(
            url, "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_trades_order_agent'"
        )
        assert "UNIQUE" in indice["indexdef"] and "origin" in indice["indexdef"]
        checks = {r["conname"] for r in await _consultar_cru(
            url, "SELECT conname FROM pg_constraint WHERE conrelid = 'trades'::regclass "
            "AND contype = 'c'"
        )}
        assert checks == set(orm.CHECKS_DE_TRADES)
        (funcao,) = await _consultar_cru(
            url, "SELECT prosrc FROM pg_proc WHERE proname = 'trades_delete_apenas_manual'"
        )
        assert "IS DISTINCT FROM" in funcao["prosrc"]
        assert await _consultar_cru(
            url, "SELECT 1 FROM pg_indexes WHERE indexname = 'ix_audit_action_ts'"
        )

        # Os dados antigos continuam la, e a aplicacao sobe.
        cfg = Settings(database_url=url)
        await init_db(cfg, migrar=False)
        assert await _divergencias(cfg) == []
        async with session_scope(cfg) as ses:
            assert await TradeRepository(ses).count() == 3
        # E o banco agora recusa o trade de agente repetido.
        with pytest.raises(IntegrityError):
            async with session_scope(cfg) as ses:
                await TradeRepository(ses).record(
                    executed_at=datetime(2026, 11, 9, tzinfo=UTC), exchange="binance",
                    symbol="BTC/USDC", side="buy", quantity=Decimal("1"),
                    price=Decimal("1"), origin=TradeOrigin.AGENT, order_id="ordem-1",
                    mode="live",
                )
        await dispose_engine()

        # Segunda rodada do `migrar`: nada a fazer, nada muda.
        assert await _migrar(url) == 0
        assert len(await _consultar_cru(url, "SELECT 1 FROM schema_versao")) == 2

    async def test_trade_repetido_no_banco_antigo_recusa_o_migrar_sem_mudar_nada(
        self, pg, capsys
    ):
        url = pg["dono"]
        await _banco_v1(url, _trade_v1("t1", "ordem-1"), _trade_v1("t2", "ordem-1"))

        assert await _migrar(url) == 1
        erro = capsys.readouterr().err
        assert "repetidos" in erro and "ordem-1" in erro
        # Uma transacao so: nem a tabela de versao ficou.
        assert not await _consultar_cru(
            url, "SELECT 1 FROM pg_class WHERE relname = 'schema_versao'"
        )

    async def test_valor_fora_do_dominio_no_banco_antigo_recusa_o_migrar(self, pg, capsys):
        url = pg["dono"]
        await _banco_v1(url, _trade_v1("t1", "ordem-1", mode="paper"))

        assert await _migrar(url) == 1
        assert "paper" in capsys.readouterr().err

    async def test_coluna_tipo_e_coluna_extra_divergentes_recusam_a_subida(self, pg):
        url = pg["dono"]
        assert await _migrar(url) == 0
        await _executar_cru(
            url,
            "ALTER TABLE agent_runs DROP COLUMN detail",
            "ALTER TABLE orders ALTER COLUMN symbol TYPE varchar(16)",
            "ALTER TABLE signals ADD COLUMN obrigatoria integer NOT NULL DEFAULT 0",
            "ALTER TABLE signals ALTER COLUMN obrigatoria DROP DEFAULT",
        )
        with pytest.raises(SchemaDivergenteError) as exc:
            await init_db(Settings(database_url=url), migrar=False)
        mensagem = str(exc.value)
        assert "agent_runs.detail: coluna ausente" in mensagem
        assert "orders.symbol: tipo VARCHAR(16) no banco, VARCHAR(32) no modelo" in mensagem
        assert "signals.obrigatoria: coluna NOT NULL sem default" in mensagem

    async def test_gatilho_desligado_recusa_a_subida_e_o_migrar_religa(self, pg):
        """F3: o gatilho existe no catalogo e a tabela esta mutavel."""
        url = pg["dono"]
        assert await _migrar(url) == 0
        await _executar_cru(url, "ALTER TABLE trades DISABLE TRIGGER trades_sem_update")

        with pytest.raises(ProtecaoAusenteError, match=r"trades_sem_update.*DESLIGADOS"):
            await init_db(Settings(database_url=url), migrar=False)
        await dispose_engine()

        assert await _migrar(url) == 0
        await init_db(Settings(database_url=url), migrar=False)

    async def test_live_como_dono_recusa_subir_e_dry_run_so_avisa(self, pg):
        """F4: em live, a aplicacao que pode derrubar os proprios gatilhos nao sobe."""
        url, url_app, usuario = pg["dono"], pg["app"], pg["usuario_app"]
        live = {"trading_mode": "live", "live_trading_confirmed": True}
        # O `migrar` e do dono por definicao: em live ele roda.
        assert await _migrar(url, usuario, **live) == 0

        with pytest.raises(AplicacaoPodeDerrubarGatilhoError, match="Em live"):
            await init_db(Settings(database_url=url, **live), migrar=False)
        await dispose_engine()
        # Subida com migrar na subida, em live, e a aplicacao como dono: recusa tambem.
        with pytest.raises(AplicacaoPodeDerrubarGatilhoError):
            await init_db(Settings(database_url=url, **live), migrar=True)
        await dispose_engine()

        with capture_logs() as logs:
            await init_db(Settings(database_url=url), migrar=False)
        assert any(e["event"] == "db.aplicacao_pode_derrubar_gatilho" for e in logs)
        await dispose_engine()

        # Com o usuario da aplicacao (sem dono, sem TRIGGER), live sobe.
        with capture_logs() as logs:
            await init_db(Settings(database_url=url_app, **live), migrar=False)
        assert not any(e["event"] == "db.aplicacao_pode_derrubar_gatilho" for e in logs)

    async def test_a_aplicacao_nao_escreve_a_versao_do_schema(self, pg):
        url, url_app, usuario = pg["dono"], pg["app"], pg["usuario_app"]
        assert await _migrar(url, usuario) == 0
        with pytest.raises(Exception, match=r"(?i)permission"):
            await _executar_cru(
                url_app,
                "INSERT INTO schema_versao (versao, descricao, aplicada_em) "
                "VALUES (3, 'falsa', now())",
            )

    async def test_nan_e_decimal_em_json_gravam_no_postgres(self, pg):
        """O tipo `json` do Postgres recusa `NaN`; no SQLite passava."""
        url = pg["dono"]
        assert await _migrar(url) == 0
        cfg = Settings(database_url=url)
        async with session_scope(cfg) as ses:
            ses.add(orm.Signal(
                id="sinal-nan", exchange="binance", symbol="BTC/USDC", timeframe="1d",
                strategy="ma_crossover", direction="long", confidence=Decimal("0.5"),
                reason="aquecimento", reference_price=Decimal("1"),
                indicators={"sma_200": float("nan"), "preco": Decimal("60000.1")},
            ))
        async with session_scope(cfg) as ses:
            sinal = await ses.get(orm.Signal, "sinal-nan")
        assert sinal.indicators == {"sma_200": "NaN", "preco": "60000.1"}
