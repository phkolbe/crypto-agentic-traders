"""Ataques do CRITICO da rodada 3 contra a camada de persistencia.

Cada teste aqui existe para tentar QUEBRAR o que o implementador diz ter
fechado. Se algum falhar, e brecha aberta.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, insert, select, text, update

from crypto_traders.db import models as orm
from crypto_traders.db.models import AuditoriaImutavelError, normalizar_dinheiro
from crypto_traders.db.session import init_db, session_scope

ID_AGENTE = "a" * 32
ID_MANUAL = "m" * 32


def _arquivo(settings) -> str:
    return settings.database_url.split("///", 1)[1]


async def _semear_auditoria(settings) -> None:
    async with session_scope(settings) as s:
        s.add(orm.AuditLog(actor="paulo", action="risk_limits_updated", detail="original"))


async def _semear_trade(settings, tid: str, origin: str) -> None:
    async with session_scope(settings) as s:
        s.add(
            orm.Trade(
                id=tid,
                executed_at=datetime.now(UTC),
                exchange="binance",
                symbol="BTC/USDC",
                side="buy",
                quantity=Decimal("1"),
                price=Decimal("100"),
                notional=Decimal("100"),
                fee=Decimal("0.1"),
                origin=origin,
                mode="dry_run",
            )
        )


class TestAtaqueC1MergeNoOrm:
    """`session.merge()` nao passa por `session.dirty` do jeito obvio."""

    async def test_merge_de_auditoria_adulterada_e_recusado(self, settings):
        await _semear_auditoria(settings)
        with pytest.raises(Exception) as exc:
            async with session_scope(settings) as s:
                alvo = (await s.execute(select(orm.AuditLog))).scalars().one()
                s.expunge(alvo)
                alvo.action = "ADULTERADO"
                alvo.detail = "apagado"
                await s.merge(alvo)
        assert "append-only" in str(exc.value), f"merge passou: {exc.value!r}"
        async with session_scope(settings) as s:
            linha = (await s.execute(select(orm.AuditLog))).scalars().one()
            assert linha.action == "risk_limits_updated", f"merge adulterou: {linha.action}"


class TestAtaqueC2InsercaoEmMassa:
    """A recusa de INSERT nao-simples nao pode ter quebrado a gravacao normal."""

    async def test_insert_de_muitas_linhas_de_auditoria_continua_funcionando(self, settings):
        async with session_scope(settings) as s:
            for i in range(50):
                s.add(orm.AuditLog(actor="system", action=f"acao_{i}"))
        async with session_scope(settings) as s:
            total = len((await s.execute(select(orm.AuditLog))).scalars().all())
        assert total == 50, total

    async def test_executemany_do_core_em_risk_events_funciona(self, settings):
        linhas = [
            {
                "id": f"ev{i:030d}",
                "created_at": datetime.now(UTC),
                "event_type": "signal_assessed",
                "signal_id": None,
                "decision": "rejected",
                "reasons": ["teto"],
                "approved_quantity": None,
                "approved_notional": None,
                "stop_loss": None,
                "take_profit": None,
                "snapshot": {},
            }
            for i in range(20)
        ]
        async with session_scope(settings) as s:
            await s.execute(insert(orm.RiskEvent), linhas)
        async with session_scope(settings) as s:
            assert len((await s.execute(select(orm.RiskEvent))).scalars().all()) == 20


class TestAtaqueC3DeleteMisto:
    """DELETE em massa que pega manual E agente na mesma sentenca."""

    async def test_delete_em_massa_nao_apaga_nem_o_manual_quando_ha_agente(self, settings):
        await _semear_trade(settings, ID_AGENTE, "agent")
        await _semear_trade(settings, ID_MANUAL, "manual")
        with pytest.raises(Exception) as exc:
            async with session_scope(settings) as s:
                await s.execute(delete(orm.Trade))
        assert "append-only" in str(exc.value), exc.value
        async with session_scope(settings) as s:
            restantes = (await s.execute(select(orm.Trade))).scalars().all()
        assert len(restantes) == 2, f"apagou {2 - len(restantes)} linha(s)"


class TestAtaqueC4SqlCruDireto:
    """SQL cru pelo sqlite3, sem SQLAlchemy nenhum -- so o gatilho protege."""

    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE trades SET notional = '0' WHERE origin = 'agent'",
            "UPDATE trades SET notional = '0' WHERE origin = 'manual'",
            "DELETE FROM trades WHERE origin = 'agent'",
            "UPDATE OR REPLACE trades SET notional = '0' WHERE origin = 'agent'",
        ],
    )
    async def test_sql_cru_em_trades_e_recusado(self, settings, sql):
        await _semear_trade(settings, ID_AGENTE, "agent")
        await _semear_trade(settings, ID_MANUAL, "manual")
        conn = sqlite3.connect(_arquivo(settings))
        conn.execute("PRAGMA recursive_triggers=ON")
        try:
            with pytest.raises(sqlite3.IntegrityError) as exc:
                conn.execute(sql)
                conn.commit()
            assert "append-only" in str(exc.value), exc.value
        finally:
            conn.close()

    async def test_or_replace_cru_em_trade_de_agente_e_recusado(self, settings):
        await _semear_trade(settings, ID_AGENTE, "agent")
        conn = sqlite3.connect(_arquivo(settings))
        conn.execute("PRAGMA recursive_triggers=ON")
        sql = (
            "INSERT OR REPLACE INTO trades (id, created_at, executed_at, exchange, symbol,"
            " side, quantity, price, notional, fee, origin, mode) VALUES"
            " (?, '2026-01-01', '2026-01-01', 'x', 'y', 'sell', '0', '0', '0', '0',"
            " 'agent', 'live')"
        )
        try:
            with pytest.raises(sqlite3.IntegrityError) as exc:
                conn.execute(sql, (ID_AGENTE,))
                conn.commit()
            assert "append-only" in str(exc.value), exc.value
        finally:
            conn.close()

    async def test_truncate_por_delete_sem_where_em_audit_log_e_recusado(self, settings):
        await _semear_auditoria(settings)
        conn = sqlite3.connect(_arquivo(settings))
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM audit_log")
                conn.commit()
            assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1
        finally:
            conn.close()


class TestAtaqueC5GatilhoDerrubavel:
    """A aplicacao consegue derrubar o proprio gatilho e depois adulterar?"""

    async def test_drop_trigger_pela_sessao_da_aplicacao(self, settings):
        await _semear_auditoria(settings)
        # O ataque agora e RECUSADO antes de chegar ao banco (a barreira de SQL
        # cru em `_recusar_sql_cru_contra_a_protecao`), entao a excecao faz parte
        # do resultado esperado. O que importa continua sendo o depois: a linha
        # intacta e o gatilho ainda no ar.
        with pytest.raises(Exception) as exc:
            async with session_scope(settings) as s:
                await s.execute(text("DROP TRIGGER IF EXISTS audit_log_sem_update"))
                await s.execute(text("UPDATE audit_log SET action = 'ADULTERADO' WHERE id = 1"))
        assert "DROP TRIGGER recusado" in str(exc.value), exc.value
        async with session_scope(settings) as s:
            linha = (await s.execute(select(orm.AuditLog))).scalars().one()
            gatilhos = set(
                (
                    await s.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))
                ).scalars()
            )
        assert linha.action == "risk_limits_updated", (
            "BRECHA: a aplicacao derrubou o gatilho por SQL cru e reescreveu a auditoria"
        )
        assert "audit_log_sem_update" in gatilhos, "BRECHA: o gatilho saiu do ar"


class TestAtaqueC6LimiteExatoDoDinheiro:
    """Igualdade, nao 'proximo de': o maior e o menor valor gravaveis."""

    def test_maior_valor_exato_e_aceito_e_o_seguinte_recusado(self):
        maior = Decimal("9" * 20 + "." + "9" * 18)
        assert normalizar_dinheiro(maior) == maior
        with pytest.raises(ValueError):
            normalizar_dinheiro(Decimal("1" + "0" * 20))

    async def test_maior_valor_exato_sobrevive_a_ida_e_volta_no_banco(self, settings):
        maior = Decimal("9" * 20 + "." + "9" * 18)
        async with session_scope(settings) as s:
            s.add(
                orm.PortfolioSnapshot(
                    id="s" * 32,
                    timestamp=datetime.now(UTC),
                    total_value=maior,
                    cash_value=Decimal(0),
                    positions_value=Decimal(0),
                )
            )
        async with session_scope(settings) as s:
            lido = (await s.execute(select(orm.PortfolioSnapshot))).scalars().one()
        assert isinstance(lido.total_value, Decimal)
        assert lido.total_value == maior, f"{lido.total_value} != {maior}"

    async def test_um_wei_exato_sobrevive_a_ida_e_volta_no_banco(self, settings):
        wei = Decimal("0.000000000000000001")
        async with session_scope(settings) as s:
            s.add(
                orm.PortfolioSnapshot(
                    id="x" * 32,
                    timestamp=datetime.now(UTC),
                    total_value=wei,
                    cash_value=Decimal(0),
                    positions_value=Decimal(0),
                )
            )
        async with session_scope(settings) as s:
            lido = (await s.execute(select(orm.PortfolioSnapshot))).scalars().one()
        assert lido.total_value == wei, lido.total_value


class TestAtaqueC7IdempotenciaConcorrente:
    """Dois sinais simultaneos no mesmo ativo: o banco recusa a ordem repetida?"""

    async def test_duas_ordens_com_o_mesmo_client_order_id_nao_coexistem(self, settings):
        def nova(oid: str) -> orm.Order:
            return orm.Order(
                id=oid,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                client_order_id="mesmo-id-de-cliente",
                risk_event_id="r" * 32,
                exchange="binance",
                symbol="BTC/USDC",
                side="buy",
                order_type="market",
                quantity=Decimal("1"),
                notional=Decimal("100"),
                status="new",
                filled_quantity=Decimal(0),
                mode="dry_run",
            )

        async def grava(oid: str):
            try:
                async with session_scope(settings) as s:
                    s.add(nova(oid))
                return "ok"
            except Exception as exc:
                return type(exc).__name__

        r1, r2 = await asyncio.gather(grava("a" * 32), grava("b" * 32))
        async with session_scope(settings) as s:
            total = len((await s.execute(select(orm.Order))).scalars().all())
        assert total == 1, f"{total} ordens com o mesmo client_order_id ({r1}, {r2})"


class TestAtaqueC8MigracaoComGatilhoNoAr:
    """A migracao derruba gatilho. Se ela explodir no meio, o gatilho volta?"""

    async def test_falha_no_meio_da_migracao_nao_deixa_a_trilha_desprotegida(
        self, settings, monkeypatch
    ):
        arq = _arquivo(settings)
        conn = sqlite3.connect(arq)
        conn.execute(
            "INSERT INTO trades (id, created_at, executed_at, exchange, symbol, side,"
            " quantity, price, notional, fee, origin, mode) VALUES"
            " (?, '2026-01-01', '2026-01-01', 'binance', 'BTC/USDC', 'buy', '1', '100',"
            " '21.6178180996209239808000', '0.1', 'agent', 'dry_run')",
            ("z" * 32,),
        )
        conn.commit()
        conn.close()

        original = orm.aplicar_protecao_append_only

        def explode(connection):
            original(connection)
            raise RuntimeError("falha simulada logo apos recriar os gatilhos")

        monkeypatch.setattr("crypto_traders.db.session.aplicar_protecao_append_only", explode)
        with pytest.raises(RuntimeError):
            await init_db(settings)

        conn = sqlite3.connect(arq)
        conn.execute("PRAGMA recursive_triggers=ON")
        try:
            gatilhos = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            ]
            assert "trades_sem_update" in gatilhos, f"gatilho sumiu: {gatilhos}"
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE trades SET notional = '0'")
                conn.commit()
        finally:
            conn.close()


class TestAtaqueC9CredencialVazia:
    """Chave de API vazia: o que a persistencia faz com valor monetario vazio?"""

    @pytest.mark.parametrize("vazio", ["", "   ", b""])
    def test_valor_monetario_vazio_e_recusado(self, vazio):
        with pytest.raises(ValueError):
            normalizar_dinheiro(vazio)

    async def test_dinheiro_none_em_coluna_nao_nula_nao_grava_zero_silencioso(self, settings):
        with pytest.raises(Exception):  # noqa: B017 - qualquer recusa serve; o que nao pode e gravar
            async with session_scope(settings) as s:
                s.add(
                    orm.PortfolioSnapshot(
                        id="n" * 32,
                        timestamp=datetime.now(UTC),
                        total_value=None,
                        cash_value=Decimal(0),
                        positions_value=Decimal(0),
                    )
                )


class TestAtaqueC10UpdateComValorIgual:
    """UPDATE que nao muda nada tambem tem que ser recusado na trilha."""

    async def test_update_sem_mudanca_ainda_e_recusado(self, settings):
        await _semear_auditoria(settings)
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(settings) as s:
                await s.execute(update(orm.AuditLog).values(action="risk_limits_updated"))


class TestAtaqueC11HypertableRecusadaPelaChavePrimaria:
    """O TimescaleDB exige que TODO indice unico contenha a coluna de particao.

    `criar_hypertables` so foi medido contra um engine de mentira, que sempre
    aceita. Contra um TimescaleDB de verdade, `create_hypertable('candles',
    'open_time')` e recusado enquanto existir a PK `candles.id` sozinha:
    "cannot create a unique index without the column 'open_time' (used in
    partitioning)". O `except` engole o erro e o sistema sobe -- sem hypertable
    nenhuma, para sempre, e sem ninguem perceber.
    """

    @pytest.mark.parametrize(
        ("tabela", "coluna_de_tempo"),
        [("candles", "open_time"), ("portfolio_snapshots", "timestamp")],
    )
    def test_todo_indice_unico_contem_a_coluna_de_particao(self, tabela, coluna_de_tempo):
        t = orm.Base.metadata.tables[tabela]
        problemas = []
        pk = [c.name for c in t.primary_key.columns]
        if coluna_de_tempo not in pk:
            problemas.append(f"PRIMARY KEY {pk}")
        for uc in t.constraints:
            nomes = [c.name for c in getattr(uc, "columns", [])]
            if type(uc).__name__ == "UniqueConstraint" and coluna_de_tempo not in nomes:
                problemas.append(f"UNIQUE {nomes}")
        for idx in t.indexes:
            if idx.unique and coluna_de_tempo not in [c.name for c in idx.columns]:
                problemas.append(f"INDEX UNIQUE {[c.name for c in idx.columns]}")
        assert not problemas, (
            f"create_hypertable('{tabela}', '{coluna_de_tempo}') seria recusado pelo "
            f"TimescaleDB por causa de: {problemas}"
        )
