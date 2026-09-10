"""A camada de persistencia: dinheiro exato, trilha imutavel, subida segura.

Cinco perguntas, medidas em vez de supostas:

1. Dinheiro sobrevive a ida e volta ao banco sem virar float, nos DOIS dialetos?
   Ler `db.models.Money` sugere que sim. Medir mostrou que o dialeto base do
   PostgreSQL convertia `Decimal` para float na saida, e que acima de 18 casas
   SQLite e Postgres gravavam valores DIFERENTES para o mesmo negocio.
2. `audit_log` e append-only de verdade? Era so combinado. Medindo,
   `row.action = "ADULTERADO"` gravava e `delete(AuditLog)` esvaziava a tabela.
   Depois de tres barreiras contra isso, `INSERT OR REPLACE` ainda reescrevia a
   linha id=1 sem erro nenhum, por SQL cru E pelo Core -- REPLACE e um DELETE
   com outro nome, e nenhuma das tres o via.
3. A trilha que guarda o MOTIVO da rejeicao de sinal (`risk_events`) e o
   historico do que aconteceu com dinheiro (`trades`) tambem sao append-only?
   Nao eram: `UPDATE risk_events SET decision = 'approved'` passava, e
   `DELETE FROM trades` pelo Core apagava a operacao de agente apesar do guarda
   na rota.
4. O dinheiro que JA esta gravado esta na escala 18? Nao estava: o banco do
   ensaio em dry_run tinha 4 valores com 22 a 25 casas, que continuariam
   divergindo numa migracao para o Postgres.
5. A ausencia do TimescaleDB derruba a subida? Pior: no Postgres ela desfazia o
   schema recem-criado, porque `create_hypertable` falhava DENTRO da mesma
   transacao do `create_all`.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    Numeric,
    Table,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.sqlite.base import SQLiteDialect
from sqlalchemy.schema import CreateTable
from structlog.testing import capture_logs

from crypto_traders.config import Settings
from crypto_traders.db import models as orm
from crypto_traders.db import session as session_mod
from crypto_traders.db.models import (
    MONEY,
    MONEY_DIGITS,
    MONEY_SCALE,
    AuditoriaImutavelError,
    normalizar_dinheiro,
)
from crypto_traders.db.repositories import (
    AuditLogRepository,
    CandleRepository,
    RiskEventRepository,
    TradeRepository,
)
from crypto_traders.db.session import (
    criar_hypertables,
    dispose_engine,
    get_engine,
    init_db,
    session_scope,
    timescale_instalado,
)
from crypto_traders.domain import models as dm
from crypto_traders.domain.enums import ExchangeName, RiskEventType, TradeOrigin

#: 18 casas decimais, o limite declarado pela coluna. Nenhum digito pode cair.
DEZOITO_CASAS = Decimal("1.234567890123456789")
#: O menor valor representavel: um wei. Vira `1E-18` em notacao cientifica, que
#: e justamente o caso que quebra quem grava `str(valor)` sem cuidado.
UM_WEI = Decimal("0.000000000000000001")

#: Os dialetos que interessam. `PGDialect` (base, sem driver) esta aqui de
#: proposito: e o que expos a conversao para float, e serve de sentinela para
#: que a garantia nao volte a depender de qual driver esta instalado.
DIALETOS = {
    "sqlite": SQLiteDialect(),
    "postgresql-base": PGDialect(),
    "postgresql-asyncpg": PGDialect_asyncpg(),
}


def _arquivo_do_banco(settings: Settings) -> str:
    """Caminho do arquivo SQLite por tras da `DATABASE_URL` do teste.

    Serve para abrir o MESMO banco por fora do SQLAlchemy, que e como se mede o
    comportamento do proprio SQLite (pragmas, gatilhos) sem a camada no meio.
    """
    return settings.database_url.split("///", 1)[1]


def _ida_e_volta(dialeto: Any, valor: Decimal) -> tuple[Any, Any]:
    """Aplica os processadores REAIS de ida e de volta do tipo `Money`.

    Passa por `dialect_impl`, e nao pelo tipo cru, porque e o que o SQLAlchemy
    faz ao executar (`TypeEngine._cached_bind_processor`). Chamar
    `MONEY.bind_processor` direto mede outra coisa -- foi o erro que quase
    escondeu o defeito do float atras de um probe enganoso.
    """
    impl = MONEY.dialect_impl(dialeto)
    ligado = impl.bind_processor(dialeto)(valor)
    devolvido = impl.result_processor(dialeto, None)(ligado)
    return ligado, devolvido


class TestDinheiroNuncaViraFloat:
    """D7: dinheiro trafega como Decimal/string, nunca float."""

    @pytest.mark.parametrize("nome", list(DIALETOS))
    @pytest.mark.parametrize("valor", [DEZOITO_CASAS, UM_WEI, Decimal("0"), Decimal("-0.5")])
    def test_dezoito_casas_sobrevivem_em_todo_dialeto(self, nome, valor):
        ligado, devolvido = _ida_e_volta(DIALETOS[nome], valor)
        assert not isinstance(ligado, float), f"{nome} entregou float ao banco: {ligado!r}"
        assert isinstance(ligado, str | Decimal)
        assert devolvido == valor, f"{nome} perdeu digito: {valor} -> {devolvido}"

    @pytest.mark.parametrize("nome", list(DIALETOS))
    def test_nenhum_processador_e_o_do_numeric(self, nome):
        """O `impl` do tipo e `Numeric`, e `Numeric` converte para float.

        `Numeric.bind_processor` devolve `processors.to_float` sempre que o
        dialeto nao declara `supports_native_decimal` -- e nenhum dialeto base
        do PostgreSQL declara. Este teste fixa que `Money` nao encadeia esse
        processador: o valor que sai daqui e string exata ou Decimal exato.
        """
        dialeto = DIALETOS[nome]
        assert dialeto.supports_native_decimal is False  # a armadilha continua la
        ligado, _ = _ida_e_volta(dialeto, DEZOITO_CASAS)
        # O que aconteceria se o `Numeric` tivesse processado:
        assert ligado != float(DEZOITO_CASAS)
        assert str(ligado).replace("-", "") == "1.234567890123456789"

    def test_a_coluna_declarada_e_texto_no_sqlite_e_numeric_no_postgres(self):
        tabela = orm.Trade.__table__
        ddl_sqlite = str(CreateTable(tabela).compile(dialect=DIALETOS["sqlite"]))
        ddl_pg = str(CreateTable(tabela).compile(dialect=DIALETOS["postgresql-asyncpg"]))
        assert "quantity VARCHAR(64)" in ddl_sqlite
        assert f"quantity NUMERIC({MONEY_DIGITS}, {MONEY_SCALE})" in ddl_pg
        assert "FLOAT" not in ddl_sqlite.upper().replace("FLOATING", "")
        assert "DOUBLE" not in ddl_pg.upper()

    def test_toda_coluna_de_dinheiro_do_schema_usa_money(self):
        """Guarda estrutural: modelo novo que esquecer `MONEY` quebra aqui.

        A garantia de D7 nao pode depender de quem escreve o proximo modelo
        lembrar do tipo certo. O nome da coluna denuncia a intencao, e o tipo
        tem de corresponder.
        """
        excecoes = {
            ("onchain_metrics", "value"),  # indicador normalizado, nao dinheiro
            ("trades", "fee_currency"),  # codigo da moeda da taxa, nao a taxa
        }
        pistas = (
            "price",
            "quantity",
            "notional",
            "volume",
            "fee",
            "pnl",
            "stop_loss",
            "take_profit",
        )
        faltando = []
        for tabela in orm.Base.metadata.tables.values():
            for coluna in tabela.columns:
                nome = coluna.name
                parece_dinheiro = any(pista in nome for pista in pistas) or (
                    nome == "value" or nome.endswith("_value")
                )
                if not parece_dinheiro or (tabela.name, nome) in excecoes:
                    continue
                if not isinstance(coluna.type, type(MONEY)):
                    faltando.append(f"{tabela.name}.{nome}: {coluna.type!r}")
        assert not faltando, f"colunas de dinheiro sem Money: {faltando}"

    def test_nenhuma_coluna_float_fora_da_excecao_documentada(self):
        floats = [
            f"{tabela.name}.{coluna.name}"
            for tabela in orm.Base.metadata.tables.values()
            for coluna in tabela.columns
            if isinstance(coluna.type, Float)
        ]
        # Unica coluna Float do schema, e ela e indicador, nao dinheiro.
        assert floats == ["onchain_metrics.value"]

    async def test_numeric_puro_no_mesmo_sqlite_perderia_digito(self, settings):
        """A prova de que a protecao AGE, e nao apenas existe.

        Mesma engine, mesmo valor: uma coluna `Numeric(38, 18)` comum (sem
        `Money`) volta com digito trocado, porque no SQLite ela e REAL. E o
        motivo de `Money` existir, medido lado a lado com ela.
        """
        metadata = MetaData()
        prova = Table(
            "prova_numeric_puro",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("valor", Numeric(MONEY_DIGITS, MONEY_SCALE)),
        )
        engine = get_engine(settings)
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.execute(insert(prova).values(id=1, valor=DEZOITO_CASAS))
            cru = (
                await conn.execute(text("SELECT typeof(valor), valor FROM prova_numeric_puro"))
            ).one()
            lido = (await conn.execute(select(prova.c.valor))).scalar_one()

        assert cru[0] == "real", "SQLite guardou como REAL -- e por isso que degrada"
        assert lido != DEZOITO_CASAS, "Numeric puro no SQLite deveria perder digito"

        async with session_scope(settings) as ses:
            await TradeRepository(ses).record(
                executed_at=datetime(2026, 1, 1, tzinfo=UTC),
                exchange="binance",
                symbol="BTC/USDC",
                side="buy",
                quantity=DEZOITO_CASAS,
                price=Decimal("1"),
            )
        async with session_scope(settings) as ses:
            guardado = (await TradeRepository(ses).list())[0]
        assert guardado.quantity == DEZOITO_CASAS, "Money nao pode perder o que Numeric perde"

    async def test_round_trip_pelo_repositorio_no_sqlite(self, settings):
        """O caminho real de producao: repositorio -> banco -> repositorio."""
        async with session_scope(settings) as ses:
            await TradeRepository(ses).record(
                executed_at=datetime(2026, 1, 2, tzinfo=UTC),
                exchange="binance",
                symbol="ETH/USDC",
                side="sell",
                quantity=DEZOITO_CASAS,
                price=DEZOITO_CASAS,
                fee=UM_WEI,
                realized_pnl=-UM_WEI,
            )
        async with session_scope(settings) as ses:
            trade = (await TradeRepository(ses).list())[0]
            crus = (
                await ses.execute(
                    text("SELECT typeof(quantity), quantity, typeof(fee), fee FROM trades")
                )
            ).one()

        assert isinstance(trade.quantity, Decimal)
        assert trade.quantity == DEZOITO_CASAS
        assert trade.fee == UM_WEI
        assert trade.realized_pnl == -UM_WEI
        assert crus[0] == "text" and crus[1] == "1.234567890123456789"
        # `1E-18` gravado como notacao cientifica voltaria como texto inutil.
        assert crus[2] == "text" and crus[3] == "0.000000000000000001"

    async def test_candle_ohlcv_mantem_precisao(self, settings):
        candles = [
            dm.Candle(
                exchange=ExchangeName.BINANCE,
                symbol="BTC/USDC",
                timeframe="1d",
                open_time=datetime(2026, 1, 4, tzinfo=UTC),
                open=DEZOITO_CASAS,
                high=DEZOITO_CASAS,
                low=DEZOITO_CASAS,
                close=DEZOITO_CASAS,
                volume=UM_WEI,
                closed=True,
            )
        ]
        async with session_scope(settings) as ses:
            assert await CandleRepository(ses).upsert_many(candles) == 1
        async with session_scope(settings) as ses:
            lidos = await CandleRepository(ses).recent("binance", "BTC/USDC", "1d")
        assert lidos[0].close == DEZOITO_CASAS
        assert lidos[0].volume == UM_WEI

    async def test_soma_de_pnl_nao_acumula_erro_de_float(self, settings):
        """`realized_pnl_total` soma em Decimal; a mesma soma em float erra.

        Os valores foram escolhidos para somar exatamente zero: um resultado
        "empatado" e o caso em que qualquer sujeira aparece de cara. Acumulando
        em float, o mesmo conjunto da 5,5e-17 em vez de 0 -- e um PnL que
        deveria ser zero passa a ser diferente de zero.
        """
        parcelas = [Decimal("0.1"), Decimal("0.2"), Decimal("-0.3"), UM_WEI, -UM_WEI]
        async with session_scope(settings) as ses:
            repo = TradeRepository(ses)
            for indice, parcela in enumerate(parcelas):
                await repo.record(
                    executed_at=datetime(2026, 1, 3, tzinfo=UTC),
                    exchange="binance",
                    symbol="BTC/USDC",
                    side="sell",
                    quantity=Decimal("1"),
                    price=Decimal("1"),
                    realized_pnl=parcela,
                    notes=str(indice),
                )
        async with session_scope(settings) as ses:
            total = await TradeRepository(ses).realized_pnl_total()

        assert isinstance(total, Decimal)
        assert total == Decimal("0")

        em_float = 0.0
        for parcela in parcelas:
            em_float += float(parcela)
        assert em_float != 0.0, "se float acertasse, este teste nao provaria nada"


class TestFaixaExataDeDinheiro:
    """Os dois dialetos precisam gravar o MESMO valor, ou nada."""

    def test_acima_de_18_casas_os_dialetos_concordam(self):
        """`cost / held` devolve 28 digitos; era ai que os bancos divergiam.

        Antes: SQLite guardava as 28 casas em texto, Postgres arredondava para
        18. Mesmo negocio, dois `realized_pnl` diferentes conforme o backend.
        """
        medio = Decimal("10") / Decimal("3")
        realizado = (Decimal("4.5") - medio) * Decimal("0.37")
        assert -realizado.as_tuple().exponent > MONEY_SCALE  # 28 casas, de fato

        gravados = {nome: _ida_e_volta(d, realizado)[1] for nome, d in DIALETOS.items()}
        assert len(set(gravados.values())) == 1, f"dialetos discordaram: {gravados}"
        assert -next(iter(gravados.values())).as_tuple().exponent == MONEY_SCALE

    @pytest.mark.parametrize(
        "valor",
        [
            Decimal("1E+21"),  # 21 casas inteiras: nao cabe em NUMERIC(38, 18)
            Decimal("-1E+25"),
            Decimal("NaN"),
            Decimal("Infinity"),
        ],
    )
    def test_valor_fora_da_faixa_e_recusado_em_vez_de_gravado_torto(self, valor):
        """Recusar e o estado seguro: o Postgres estouraria, o SQLite aceitaria."""
        with pytest.raises(ValueError):
            normalizar_dinheiro(valor)

    def test_valor_no_limite_da_faixa_e_aceito(self):
        limite = Decimal("9" * (MONEY_DIGITS - MONEY_SCALE))
        assert normalizar_dinheiro(limite) == limite

    def test_texto_e_float_de_entrada_viram_decimal_exato(self):
        assert normalizar_dinheiro("0.1") == Decimal("0.1")
        # `Decimal(0.1)` traria 0.1000000000000000055511151231257827; `str` nao.
        assert normalizar_dinheiro(0.1) == Decimal("0.1")

    @pytest.mark.parametrize(
        "entrada",
        [True, False, "nao e numero", "", None, object(), [Decimal("1")]],
    )
    def test_entrada_que_nao_e_numero_levanta_value_error(self, entrada):
        """A docstring promete `ValueError`; antes escapava `InvalidOperation`.

        `bool` esta na lista porque `True` e `int` para o Python: sem a recusa
        explicita, um campo booleano trocado por engano viraria 1 unidade de
        moeda -- silenciosamente, que e o pior jeito de errar dinheiro.
        """
        with pytest.raises(ValueError):
            normalizar_dinheiro(entrada)

    def test_abaixo_de_um_wei_arredonda_para_zero_e_avisa(self):
        """Decisao explicita, e o CONTRARIO da do estouro. Fixada aqui.

        Recusar 1E-25 nao protegeria dinheiro nenhum: impediria REGISTRAR uma
        negociacao que ja aconteceu por causa do vigesimo quinto decimal. Perder
        o registro e pior que perder esse digito. O que nao pode acontecer e o
        arredondamento ser silencioso -- e ele nao e.
        """
        with capture_logs() as eventos:
            assert normalizar_dinheiro(Decimal("1E-25")) == Decimal(0)
            assert normalizar_dinheiro(Decimal("-1E-25")) == Decimal(0)

        avisos = [e for e in eventos if e["event"] == "db.dinheiro_abaixo_de_um_wei"]
        assert len(avisos) == 2
        assert avisos[0]["log_level"] == "warning"
        assert avisos[0]["original"] == "1E-25"

    def test_um_wei_exato_nao_dispara_o_aviso(self):
        """A fronteira: 1E-18 cabe, entao nao ha nada a avisar."""
        with capture_logs() as eventos:
            assert normalizar_dinheiro(UM_WEI) == UM_WEI
        assert not [e for e in eventos if e["event"] == "db.dinheiro_abaixo_de_um_wei"]


class TestAuditLogAppendOnly:
    """Auditoria que o sistema consegue reescrever nao e auditoria."""

    @pytest.fixture
    async def com_uma_linha(self, settings):
        async with session_scope(settings) as ses:
            await AuditLogRepository(ses).append(
                action="risk_config_updated", actor="paulo", detail="original"
            )
        return settings

    async def _linhas(self, settings):
        async with session_scope(settings) as ses:
            return await AuditLogRepository(ses).list()

    async def test_insert_continua_funcionando(self, com_uma_linha):
        linhas = await self._linhas(com_uma_linha)
        assert len(linhas) == 1 and linhas[0].detail == "original"

    async def test_update_pelo_orm_e_recusado(self, com_uma_linha):
        """O furo medido: objeto vindo de `list()` era gravavel."""
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(com_uma_linha) as ses:
                linha = (await ses.execute(select(orm.AuditLog))).scalars().one()
                linha.action = "ADULTERADO"

        assert (await self._linhas(com_uma_linha))[0].action == "risk_config_updated"

    async def test_delete_pelo_orm_e_recusado(self, com_uma_linha):
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(com_uma_linha) as ses:
                linha = (await ses.execute(select(orm.AuditLog))).scalars().one()
                await ses.delete(linha)

        assert len(await self._linhas(com_uma_linha)) == 1

    @pytest.mark.parametrize(
        "comando",
        [
            lambda: delete(orm.AuditLog),
            lambda: delete(orm.AuditLog).where(orm.AuditLog.id == 1),
            lambda: update(orm.AuditLog).values(action="ADULTERADO"),
        ],
    )
    async def test_sql_em_massa_do_core_e_recusado(self, com_uma_linha, comando):
        """`delete(AuditLog)` nao acorda o flush -- foi o que esvaziou a tabela."""
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(com_uma_linha) as ses:
                await ses.execute(comando())

        assert len(await self._linhas(com_uma_linha)) == 1

    @pytest.mark.parametrize(
        "sql",
        [
            "DELETE FROM audit_log",
            "DELETE FROM audit_log WHERE id = 1",
            "UPDATE audit_log SET action = 'ADULTERADO'",
        ],
    )
    async def test_sql_cru_e_recusado_pelo_gatilho_do_banco(self, com_uma_linha, sql):
        """A barreira que sobrevive a aplicacao: gatilho dentro do banco.

        Inclui `DELETE FROM audit_log` sem WHERE, que no SQLite normalmente usa
        a otimizacao de truncate e nao dispararia gatilho nenhum -- ela e
        desativada justamente porque existe gatilho de DELETE.
        """
        with pytest.raises(Exception, match="append-only"):
            async with session_scope(com_uma_linha) as ses:
                await ses.execute(text(sql))

        linhas = await self._linhas(com_uma_linha)
        assert len(linhas) == 1 and linhas[0].action == "risk_config_updated"

    @pytest.mark.parametrize(
        "sql",
        [
            # REPLACE e um DELETE com outro nome: apaga a linha em conflito.
            "INSERT OR REPLACE INTO audit_log (id, timestamp, actor, action, before, after, "
            "detail) VALUES (1, '2026-01-01 00:00:00', 'invasor', 'ADULTERADO', '{}', '{}', 'x')",
            # A forma mais enxuta do mesmo ataque: apaga sem nem repor o conteudo.
            "REPLACE INTO audit_log (id, timestamp, actor, action, before, after) "
            "VALUES (1, '2026-01-01 00:00:00', 'x', 'x', '{}', '{}')",
            "INSERT INTO audit_log (id, timestamp, actor, action, before, after) "
            "VALUES (1, '2026-01-01 00:00:00', 'x', 'x', '{}', '{}') "
            "ON CONFLICT(id) DO UPDATE SET action = 'ADULTERADO'",
        ],
    )
    async def test_sql_cru_com_or_replace_e_recusado(self, com_uma_linha, sql):
        """A brecha da primeira rodada: nenhuma das tres barreiras a via.

        `INSERT OR REPLACE` e um `Insert`, entao a barreira do Core (que olhava
        `Update | Delete`) deixava passar; nao ha objeto ORM, entao o
        `before_flush` nao acordava; e o gatilho `audit_log_sem_delete` nao
        disparava porque no SQLite a resolucao REPLACE so aciona gatilho de
        DELETE com `PRAGMA recursive_triggers` ON, e o padrao e OFF.
        """
        with pytest.raises(Exception, match="append-only"):
            async with session_scope(com_uma_linha) as ses:
                await ses.execute(text(sql))

        linhas = await self._linhas(com_uma_linha)
        assert len(linhas) == 1
        assert linhas[0].action == "risk_config_updated" and linhas[0].actor == "paulo"

    @pytest.mark.parametrize(
        "construtor",
        [
            lambda: insert(orm.AuditLog).prefix_with("OR REPLACE"),
            lambda: sqlite_insert(orm.AuditLog).on_conflict_do_nothing(index_elements=["id"]),
            lambda: sqlite_insert(orm.AuditLog).on_conflict_do_update(
                index_elements=["id"], set_={"action": "ADULTERADO"}
            ),
        ],
    )
    async def test_or_replace_pelo_core_e_recusado(self, com_uma_linha, construtor):
        """Sem SQL cru nenhum: so o Core do SQLAlchemy, como qualquer rota faria.

        O `DO NOTHING` entra na lista de proposito: sozinho ele nao apaga nada,
        mas nenhuma linha do projeto precisa de resolucao de conflito numa trilha
        append-only, e recusar o que nao se reconhece e mais barato de manter
        correto que uma lista de prefixos permitidos.
        """
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(com_uma_linha) as ses:
                await ses.execute(
                    construtor().values(
                        id=1,
                        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                        actor="invasor",
                        action="ADULTERADO",
                        before={},
                        after={},
                        detail="reescrito pelo Core",
                    )
                )

        linhas = await self._linhas(com_uma_linha)
        assert len(linhas) == 1 and linhas[0].action == "risk_config_updated"

    async def test_insert_simples_de_auditoria_continua_passando_pelo_core(self, settings):
        """A recusa e cirurgica: `Insert` sem resolucao de conflito e o caminho normal."""
        async with session_scope(settings) as ses:
            await ses.execute(
                insert(orm.AuditLog).values(
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                    actor="system",
                    action="live_trading_enabled",
                    before={},
                    after={},
                )
            )
        assert len(await self._linhas(settings)) == 1

    async def test_o_pragma_recursive_triggers_esta_ligado_na_conexao_real(self, settings):
        """Nao no teste: na conexao que a aplicacao abre.

        Sem este pragma o gatilho de DELETE nao ve o REPLACE, e a barreira do
        banco -- a unica que sobrevive a aplicacao -- fica decorativa.
        """
        async with session_scope(settings) as ses:
            ligado = (await ses.execute(text("PRAGMA recursive_triggers"))).scalar_one()
        assert ligado == 1

    async def test_sem_o_pragma_o_mesmo_comando_passaria(self, com_uma_linha):
        """A prova de que o pragma e load-bearing, e nao enfeite.

        Mesma tabela, mesmos gatilhos, mesmo comando: numa conexao com
        `recursive_triggers` no padrao (OFF) o REPLACE apaga a linha de auditoria
        e ninguem reclama. E a medicao do mecanismo, lado a lado com a correcao.
        """
        arquivo = _arquivo_do_banco(com_uma_linha)
        replace = (
            "INSERT OR REPLACE INTO audit_log (id, timestamp, actor, action, before, after) "
            "VALUES (1, '2026-01-01 00:00:00', 'invasor', 'ADULTERADO', '{}', '{}')"
        )

        cru = sqlite3.connect(arquivo, timeout=10)
        try:
            assert cru.execute("PRAGMA recursive_triggers").fetchone()[0] == 0, "o padrao e OFF"
            cru.execute(replace)  # passa: e o defeito, reproduzido
            cru.commit()
            assert cru.execute("SELECT action FROM audit_log").fetchone()[0] == "ADULTERADO"

            cru.execute("PRAGMA recursive_triggers = ON")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                cru.execute(replace)
        finally:
            cru.close()

    async def test_os_gatilhos_existem_no_banco(self, settings):
        async with session_scope(settings) as ses:
            nomes = (
                await ses.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
            ).scalars().all()
        assert {
            "audit_log_sem_update",
            "audit_log_sem_delete",
            "risk_events_sem_update",
            "risk_events_sem_delete",
            "trades_sem_update",
            "trades_sem_delete_de_agente",
        } <= set(nomes)

    async def test_create_all_puro_ja_instala_todos_os_gatilhos(self, tmp_path):
        """Sem `init_db`: quem usa `create_all` direto tambem fica protegido.

        Nao e hipotese -- outros arquivos da suite montam o schema assim. E a
        ordem de criacao e a razao de os gatilhos serem registrados tabela por
        tabela: medida, ela e `audit_log` ANTES de `risk_events` e `trades`
        (alfabetica), entao um unico `after_create` em `audit_log` tentaria criar
        gatilho numa tabela que ainda nao existe.
        """
        await dispose_engine()
        configurado = Settings(
            database_url=f"sqlite+aiosqlite:///{(tmp_path / 'puro.db').as_posix()}"
        )
        engine = get_engine(configurado)
        async with engine.begin() as conn:
            await conn.run_sync(orm.Base.metadata.create_all)

        criadas = [tabela.name for tabela in orm.Base.metadata.sorted_tables]
        assert criadas.index("audit_log") < criadas.index("trades")

        async with session_scope(configurado) as ses:
            nomes = (
                await ses.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
            ).scalars().all()
        assert {
            "audit_log_sem_update",
            "audit_log_sem_delete",
            "risk_events_sem_update",
            "risk_events_sem_delete",
            "trades_sem_update",
            "trades_sem_delete_de_agente",
        } <= set(nomes)
        await dispose_engine()

    async def test_banco_antigo_sem_gatilho_e_protegido_na_subida(self, com_uma_linha):
        """Bancos criados antes desta protecao existem (o do ensaio em dry_run).

        `init_db` reaplica os gatilhos a cada subida, por isso: aqui eles sao
        derrubados de proposito, e a subida seguinte tem de recoloca-los.
        """
        # Por fora do SQLAlchemy: a aplicacao nao consegue mais fazer isso (ver
        # `TestSqlCruNaoDesligaAProtecao`), e um banco antigo justamente e um
        # banco que nasceu sem os gatilhos, nao um que os perdeu por dentro.
        conn = sqlite3.connect(_arquivo_do_banco(com_uma_linha))
        try:
            for nome in orm.GATILHOS_ESPERADOS["sqlite"]:
                conn.execute(f"DROP TRIGGER IF EXISTS {nome}")
            conn.commit()
        finally:
            conn.close()

        await init_db(com_uma_linha)

        async with session_scope(com_uma_linha) as ses:
            nomes = (
                await ses.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
            ).scalars().all()
        assert {
            "audit_log_sem_update",
            "audit_log_sem_delete",
            "risk_events_sem_update",
            "risk_events_sem_delete",
            "trades_sem_update",
            "trades_sem_delete_de_agente",
        } <= set(nomes)

    async def test_outras_tabelas_continuam_alteraveis(self, settings):
        """A protecao e cirurgica: nao pode congelar o resto do banco."""
        async with session_scope(settings) as ses:
            ses.add(orm.AgentRun(agent="strategy", state="running"))
        async with session_scope(settings) as ses:
            apagadas = (await ses.execute(delete(orm.AgentRun))).rowcount
        assert apagadas == 1

    def test_o_repositorio_nao_expoe_update_nem_delete(self):
        metodos = {nome for nome in vars(AuditLogRepository) if not nome.startswith("_")}
        assert metodos == {"append", "list"}


class TestTrilhaDeRejeicaoAppendOnly:
    """O MOTIVO da rejeicao de sinal mora em `risk_events`, e nao em `audit_log`.

    A checklist do projeto pede "todo sinal rejeitado logado com o motivo em
    trilha append-only". Rastreado: quem grava o motivo e
    `RiskEventRepository.save_assessment` (`decision` + `reasons`, em
    `db/repositories.py:153-169`), na tabela `risk_events`. Enquanto essa tabela
    aceitava UPDATE e DELETE, a linha da checklist era falsa mesmo com
    `audit_log` blindado: bastava reescrever `decision='rejected'` para
    `'approved'` com `reasons=[]`. Foi medido, funcionava, e e o que estes testes
    impedem.
    """

    @pytest.fixture
    async def com_uma_rejeicao(self, settings):
        async with session_scope(settings) as ses:
            ses.add(
                orm.RiskEvent(
                    id="ev1",
                    event_type="signal_evaluated",
                    signal_id="sig1",
                    decision="rejected",
                    reasons=["notional acima do teto"],
                    snapshot={},
                )
            )
        return settings

    async def _eventos(self, settings):
        async with session_scope(settings) as ses:
            return await RiskEventRepository(ses).list()

    async def test_o_motivo_gravado_e_o_que_volta(self, com_uma_rejeicao):
        eventos = await self._eventos(com_uma_rejeicao)
        assert len(eventos) == 1
        assert eventos[0].decision == "rejected"
        assert eventos[0].reasons == ["notional acima do teto"]

    async def test_reescrever_o_motivo_pelo_orm_e_recusado(self, com_uma_rejeicao):
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(com_uma_rejeicao) as ses:
                evento = (await ses.execute(select(orm.RiskEvent))).scalars().one()
                evento.decision = "approved"
                evento.reasons = []

        eventos = await self._eventos(com_uma_rejeicao)
        assert eventos[0].decision == "rejected" and eventos[0].reasons

    async def test_apagar_a_rejeicao_pelo_orm_e_recusado(self, com_uma_rejeicao):
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(com_uma_rejeicao) as ses:
                evento = (await ses.execute(select(orm.RiskEvent))).scalars().one()
                await ses.delete(evento)

        assert len(await self._eventos(com_uma_rejeicao)) == 1

    @pytest.mark.parametrize(
        "comando",
        [
            lambda: update(orm.RiskEvent).values(decision="approved", reasons=[]),
            lambda: delete(orm.RiskEvent),
            lambda: delete(orm.RiskEvent).where(orm.RiskEvent.id == "ev1"),
        ],
    )
    async def test_sql_em_massa_do_core_e_recusado(self, com_uma_rejeicao, comando):
        """Foi exatamente assim que a medicao reescreveu e depois apagou o motivo."""
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(com_uma_rejeicao) as ses:
                await ses.execute(comando())

        eventos = await self._eventos(com_uma_rejeicao)
        assert len(eventos) == 1 and eventos[0].decision == "rejected"

    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE risk_events SET decision = 'approved', reasons = '[]'",
            "DELETE FROM risk_events",
            "INSERT OR REPLACE INTO risk_events (id, created_at, event_type, decision, "
            "reasons, snapshot) VALUES ('ev1', '2026-01-01 00:00:00', 'signal_evaluated', "
            "'approved', '[]', '{}')",
        ],
    )
    async def test_sql_cru_e_recusado_pelo_gatilho(self, com_uma_rejeicao, sql):
        with pytest.raises(Exception, match="append-only"):
            async with session_scope(com_uma_rejeicao) as ses:
                await ses.execute(text(sql))

        eventos = await self._eventos(com_uma_rejeicao)
        assert len(eventos) == 1 and eventos[0].decision == "rejected"

    async def test_gravar_novo_evento_continua_funcionando(self, com_uma_rejeicao):
        """Append-only e append: o Risk Manager precisa seguir gravando."""
        async with session_scope(com_uma_rejeicao) as ses:
            await RiskEventRepository(ses).save_event(
                RiskEventType.CIRCUIT_BREAKER_TRIPPED, ["perda diaria acima do limite"]
            )
        assert len(await self._eventos(com_uma_rejeicao)) == 2


class TestHistoricoDeTradesAppendOnly:
    """O historico do que aconteceu com dinheiro, com UMA excecao explicita.

    UPDATE nunca: nenhum caminho do codigo altera trade gravado (conferido em
    `src/`, so `record`, `list`, `count`, `get`, `delete`). DELETE apenas de
    lancamento manual digitado errado -- a unica remocao que o produto preve.

    A regra ja existia, mas so na rota (`api/routes/trades.py:99-104`), ou seja
    "append-only por convencao": um `delete(orm.Trade)` pelo Core apagava a linha
    do agente sem passar por ela. Medido. Agora a regra esta no banco tambem.
    """

    @pytest.fixture
    async def com_dois_trades(self, settings):
        async with session_scope(settings) as ses:
            repo = TradeRepository(ses)
            do_agente = await repo.record(
                executed_at=datetime(2026, 1, 1, tzinfo=UTC),
                exchange="binance",
                symbol="BTC/USDC",
                side="buy",
                quantity=Decimal("1"),
                price=Decimal("100"),
                origin=TradeOrigin.AGENT,
            )
            manual = await repo.record(
                executed_at=datetime(2026, 1, 2, tzinfo=UTC),
                exchange="binance",
                symbol="ETH/USDC",
                side="buy",
                quantity=Decimal("2"),
                price=Decimal("50"),
                origin=TradeOrigin.MANUAL,
            )
            ids = (do_agente.id, manual.id)
        return settings, ids

    async def _quantos(self, settings):
        async with session_scope(settings) as ses:
            return await TradeRepository(ses).count()

    async def test_update_em_massa_do_core_e_recusado(self, com_dois_trades):
        settings, _ = com_dois_trades
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(settings) as ses:
                await ses.execute(update(orm.Trade).values(realized_pnl=Decimal("999")))

        assert await self._quantos(settings) == 2

    async def test_delete_em_massa_do_core_e_recusado(self, com_dois_trades):
        """`DELETE FROM trades` pelo Core apagou a linha na medicao. Nao mais.

        Quem recusa aqui e o gatilho do banco, e nao a barreira de aplicacao --
        de proposito: `origin` so da para olhar linha por linha, e o Core nao
        olha linha nenhuma. A barreira de aplicacao nao pode barrar DELETE em
        `trades` porque o proprio ORM emite um `Delete` comum ao apagar o
        lancamento manual permitido (medido), e ela nao consegue distinguir os
        dois.
        """
        settings, _ = com_dois_trades
        with pytest.raises(Exception, match="append-only"):
            async with session_scope(settings) as ses:
                await ses.execute(delete(orm.Trade))

        assert await self._quantos(settings) == 2, "nem a linha manual pode cair no meio"

    async def test_alterar_trade_pelo_orm_e_recusado(self, com_dois_trades):
        settings, _ = com_dois_trades
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(settings) as ses:
                trade = (await TradeRepository(ses).list())[0]
                trade.realized_pnl = Decimal("999")

        async with session_scope(settings) as ses:
            assert all(t.realized_pnl is None for t in await TradeRepository(ses).list())

    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE trades SET price = '1'",
            "DELETE FROM trades WHERE origin = 'agent'",
        ],
    )
    async def test_sql_cru_e_recusado_pelo_gatilho(self, com_dois_trades, sql):
        settings, _ = com_dois_trades
        with pytest.raises(Exception, match="append-only"):
            async with session_scope(settings) as ses:
                await ses.execute(text(sql))

        assert await self._quantos(settings) == 2

    async def test_apagar_operacao_de_agente_pelo_orm_e_recusado(self, com_dois_trades):
        settings, (id_agente, _) = com_dois_trades
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(settings) as ses:
                trade = await TradeRepository(ses).get(id_agente)
                await ses.delete(trade)

        assert await self._quantos(settings) == 2

    async def test_apagar_lancamento_manual_continua_permitido(self, com_dois_trades):
        """A excecao explicita: lancamento manual digitado errado e corrigivel.

        E o caminho que a rota `DELETE /trades/{id}` usa. Se a protecao do banco
        tivesse pegado tambem este caso, a funcionalidade teria morrido calada --
        por isso ele e testado junto, e nao depois.
        """
        settings, (_, id_manual) = com_dois_trades
        async with session_scope(settings) as ses:
            assert await TradeRepository(ses).delete(id_manual) is True

        assert await self._quantos(settings) == 1

    async def test_a_recusa_do_repositorio_e_a_do_banco_concordam(self, com_dois_trades):
        """`TradeRepository.delete` devolve False para operacao de agente.

        Duas barreiras dizendo a mesma coisa: o repositorio nem tenta, e se
        tentasse o gatilho abortaria. Testar as duas juntas e o que garante que
        elas nao divergem depois.
        """
        settings, (id_agente, _) = com_dois_trades
        async with session_scope(settings) as ses:
            assert await TradeRepository(ses).delete(id_agente) is False
        assert await self._quantos(settings) == 2

    async def test_or_replace_cru_em_operacao_de_agente_e_recusado(self, com_dois_trades):
        """O mesmo ataque que furou `audit_log`, apontado para o historico.

        REPLACE apaga a linha em conflito; o gatilho de DELETE ve `OLD.origin` e
        aborta -- porque `recursive_triggers` esta ON. Com ele OFF isto passaria.
        """
        settings, (id_agente, _) = com_dois_trades
        with pytest.raises(Exception, match="append-only"):
            async with session_scope(settings) as ses:
                await ses.execute(
                    text(
                        "INSERT OR REPLACE INTO trades (id, created_at, executed_at, exchange, "
                        "symbol, side, quantity, price, notional, fee, origin, mode) VALUES "
                        f"('{id_agente}', '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
                        "'binance', 'BTC/USDC', 'sell', '0', '0', '0', '0', 'agent', 'dry_run')"
                    )
                )

        async with session_scope(settings) as ses:
            original = await TradeRepository(ses).get(id_agente)
        assert original.side == "buy" and original.price == Decimal("100")

    async def test_orders_continuam_alteraveis(self, settings):
        """`orders` NAO entra na protecao, e isso e deliberado.

        Ordem tem ciclo de vida: nasce PENDING e vira FILLED/REJECTED por
        `OrderRepository.apply_result`. Congelar `orders` mataria a reconciliacao
        com a exchange. O registro imutavel do que aconteceu e `trades`.
        """
        async with session_scope(settings) as ses:
            ses.add(
                orm.Order(
                    id="o1",
                    client_order_id="c1",
                    risk_event_id="ev1",
                    exchange="binance",
                    symbol="BTC/USDC",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("1"),
                    notional=Decimal("100"),
                    status="pending",
                    mode="dry_run",
                )
            )
        async with session_scope(settings) as ses:
            ordem = await ses.get(orm.Order, "o1")
            ordem.status = "filled"
        async with session_scope(settings) as ses:
            assert (await ses.get(orm.Order, "o1")).status == "filled"


class TestEscalaDoDinheiroJaGravado:
    """As linhas que JA estao no banco tambem precisam devolver a escala 18.

    O banco do ensaio em dry_run foi medido e tem 4 valores fora da escala 18
    (`trades.notional` com 22 casas, `trades.fee` com 25,
    `portfolio_snapshots.total_value` e `cash_value` com 25). Enquanto eles
    existirem, a promessa "os dois dialetos dao o mesmo valor" precisa valer
    tambem para o passado.

    Isto ja foi resolvido por uma MIGRACAO, que reescrevia as linhas na subida,
    e a migracao era uma brecha grave: para reescrever `trades` ela derrubava o
    gatilho `trades_sem_update`, e no pysqlite o DDL nao entra na transacao (ver
    `TestDdlNaoEhTransacional`, que mede isso). Uma falha no meio da subida
    deixava o historico de dinheiro mutavel para sempre.

    Agora a escala e imposta na LEITURA. Os testes desta classe provam as duas
    metades: que o valor lido e o mesmo nos dois dialetos, e que o historico
    gravado NAO e tocado -- nem um UPDATE, nem um DROP TRIGGER.
    """

    ESCALA_28 = "0.4316666666666666666666666668"
    #: Mesmo numero, na escala que `NUMERIC(38, 18)` guarda.
    ESCALA_18 = "0.431666666666666667"

    def _semear_linha_legada_por_fora(self, settings) -> None:
        """Grava por sqlite3 cru, que e como as linhas antigas entraram.

        Por fora do SQLAlchemy de proposito: e assim que um valor fora da escala
        chega ao banco hoje, ja que `Money` normaliza toda gravacao da
        aplicacao.
        """
        conn = sqlite3.connect(_arquivo_do_banco(settings))
        try:
            conn.execute(
                "INSERT INTO trades (id, created_at, executed_at, exchange, symbol, side, "
                "quantity, price, notional, fee, origin, mode, realized_pnl) VALUES "
                "('legado', '2026-01-01 00:00:00', '2026-01-01 00:00:00', 'binance', "
                f"'BTC/USDC', 'sell', '{self.ESCALA_28}', '1', '1', '0', 'agent', "
                f"'dry_run', '{self.ESCALA_28}')"
            )
            conn.commit()
        finally:
            conn.close()

    async def test_linha_legada_de_28_casas_volta_com_18_na_leitura(self, settings):
        """A prova de que a normalizacao AGE: gravado com 28, lido com 18."""
        self._semear_linha_legada_por_fora(settings)

        async with session_scope(settings) as ses:
            cru = (await ses.execute(text("SELECT quantity FROM trades"))).scalar_one()
            lido = (await TradeRepository(ses).list())[0]

        assert cru == self.ESCALA_28, "o teste precisa de uma linha torta de verdade"
        assert -lido.quantity.as_tuple().exponent == MONEY_SCALE
        assert str(lido.quantity) == self.ESCALA_18
        # E o mesmo numero que os dois dialetos gravariam hoje, nao um numero novo.
        assert lido.quantity == normalizar_dinheiro(Decimal(self.ESCALA_28))

    def test_os_dois_dialetos_leem_a_mesma_linha_torta_do_mesmo_jeito(self):
        """O ponto todo: SQLite e PostgreSQL devolvem o MESMO numero.

        No PostgreSQL a coluna e `NUMERIC(38, 18)` e o servidor ja entrega o
        valor arredondado; no SQLite a coluna e texto e entrega os 28 digitos
        crus. Sem normalizar na leitura, o mesmo historico valeria dois numeros
        diferentes conforme o banco. Aqui os processadores REAIS de cada dialeto
        recebem exatamente o que cada servidor entregaria.
        """
        lidos = {}
        for nome, dialeto in DIALETOS.items():
            impl = MONEY.dialect_impl(dialeto)
            # O que o servidor entrega: texto cru no SQLite, Decimal ja na
            # escala da coluna no Postgres.
            do_servidor = (
                self.ESCALA_28
                if nome == "sqlite"
                else Decimal(self.ESCALA_28).quantize(Decimal(1).scaleb(-MONEY_SCALE))
            )
            lidos[nome] = impl.result_processor(dialeto, None)(do_servidor)
        assert len(set(lidos.values())) == 1, f"dialetos discordam: {lidos}"
        assert set(lidos.values()) == {Decimal(self.ESCALA_18)}

    async def test_a_subida_nao_reescreve_o_historico_nem_derruba_gatilho(self, settings):
        """A correcao da brecha, medida: a subida nao toca em `trades`.

        Antes, `init_db` executava `DROP TRIGGER` + `UPDATE trades`. Aqui se
        confere que a linha torta continua LETRA POR LETRA como estava depois da
        subida, e que os seis gatilhos seguem no ar -- ou seja, que nao existe
        mais janela nenhuma em que o historico de dinheiro fique mutavel.
        """
        self._semear_linha_legada_por_fora(settings)

        await init_db(settings)  # a proxima subida do sistema

        conn = sqlite3.connect(_arquivo_do_banco(settings))
        conn.execute("PRAGMA recursive_triggers=ON")
        try:
            depois = conn.execute("SELECT quantity, realized_pnl FROM trades").fetchone()
            gatilhos = {
                linha[0]
                for linha in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
        finally:
            conn.close()

        assert depois == (self.ESCALA_28, self.ESCALA_28), "a subida reescreveu o historico"
        assert set(orm.GATILHOS_ESPERADOS["sqlite"]) <= gatilhos, gatilhos

    async def test_a_subida_relata_o_que_esta_fora_de_escala(self, settings):
        """O fato continua visivel: nao reescrever nao e fingir que nao existe."""
        self._semear_linha_legada_por_fora(settings)
        engine = get_engine(settings)
        with capture_logs() as eventos:
            async with engine.begin() as conn:
                achados = await conn.run_sync(orm.relatar_dinheiro_fora_de_escala)

        assert len(achados) == 2, achados  # quantity e realized_pnl
        assert any("trades.quantity" in a for a in achados)
        avisos = [e for e in eventos if e["event"] == "db.dinheiro_gravado_fora_da_escala"]
        assert len(avisos) == 1 and avisos[0]["log_level"] == "warning"
        assert avisos[0]["total"] == 2

    async def test_banco_limpo_nao_relata_nada(self, settings):
        """Custo zero no caso normal: nada fora da escala, nada a dizer."""
        async with session_scope(settings) as ses:
            await TradeRepository(ses).record(
                executed_at=datetime(2026, 1, 1, tzinfo=UTC),
                exchange="binance",
                symbol="BTC/USDC",
                side="buy",
                quantity=DEZOITO_CASAS,
                price=Decimal("1"),
            )
        engine = get_engine(settings)
        async with engine.begin() as conn:
            assert await conn.run_sync(orm.relatar_dinheiro_fora_de_escala) == []

    async def test_valor_gravado_como_real_tambem_volta_normalizado(self, settings):
        """Linha anterior ao tipo `Money`: a coluna era `NUMERIC` e guardava REAL.

        Nao da para simular isso com o schema de hoje: a coluna atual e
        `VARCHAR(64)` e a afinidade TEXT converte qualquer numero em texto na
        entrada. Entao a tabela e recriada aqui com a FORMA ANTIGA (`NUMERIC`),
        que e o que existe em disco num banco criado antes do tipo `Money` --
        `create_all` nao mexe em tabela que ja existe, entao a forma antiga
        sobrevive a subida, exatamente como no banco de verdade.

        A recriacao vai por sqlite3 cru, e nao pela sessao da aplicacao, porque
        a aplicacao nao consegue mais fazer DDL sobre trilha protegida (ver
        `TestSqlCruNaoDesligaAProtecao`) -- e nao poder e o ponto.
        """
        conn = sqlite3.connect(_arquivo_do_banco(settings))
        try:
            conn.execute("DROP TABLE trades")
            conn.execute(
                "CREATE TABLE trades (id VARCHAR(32) PRIMARY KEY, "
                "created_at DATETIME, executed_at DATETIME, exchange VARCHAR(32), "
                "symbol VARCHAR(32), side VARCHAR(8), quantity NUMERIC(38, 18), "
                "price NUMERIC(38, 18), notional NUMERIC(38, 18), fee NUMERIC(38, 18), "
                "fee_currency VARCHAR(16), origin VARCHAR(16), order_id VARCHAR(32), "
                "signal_id VARCHAR(32), strategy VARCHAR(64), mode VARCHAR(16), "
                "realized_pnl NUMERIC(38, 18), notes TEXT)"
            )
            conn.execute(
                "INSERT INTO trades (id, created_at, executed_at, exchange, symbol, side, "
                "quantity, price, notional, fee, origin, mode) VALUES "
                "('real', '2026-01-01 00:00:00', '2026-01-01 00:00:00', 'binance', "
                "'BTC/USDC', 'buy', 0.4000000000000000222, 1, 1, 0, 'agent', 'dry_run')"
            )
            conn.commit()
            tipo_antes = conn.execute("SELECT typeof(quantity) FROM trades").fetchone()[0]
        finally:
            conn.close()
        assert tipo_antes == "real", "sem um REAL de verdade o teste nao prova nada"

        await init_db(settings)

        async with session_scope(settings) as ses:
            lido = (await TradeRepository(ses).list())[0].quantity

        # O REAL antigo volta como Decimal exato, e nao como float: o valor
        # gravado era o double mais proximo de 0,4, e e isso que se le.
        assert isinstance(lido, Decimal)
        assert lido == Decimal("0.4")
        assert -lido.as_tuple().exponent <= MONEY_SCALE

    async def test_a_forma_atual_da_coluna_e_texto_e_nao_numeric(self, settings):
        """A garantia que de fato protege o dado: afinidade TEXT na coluna.

        Enquanto a coluna for `VARCHAR`, o SQLite guarda exatamente o texto que
        `Money` escreve. Se um dia alguem trocar o tipo por `Numeric`, o dado
        volta a ser REAL -- por isso a forma e verificada, e nao so o conteudo.
        """
        async with session_scope(settings) as ses:
            ddl = (
                await ses.execute(
                    text("SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'")
                )
            ).scalar_one()
        assert "quantity VARCHAR(64)" in ddl
        assert "NUMERIC" not in ddl.upper()

    async def test_valor_impossivel_de_arredondar_volta_como_esta_e_grita(self, settings):
        """Estouro de 20 casas inteiras nao tem arredondamento honesto.

        Inventar um numero para caber na coluna seria pior que manter o valor
        errado visivel -- e RECUSAR a leitura seria pior ainda, porque o UPDATE
        que consertaria a linha e recusado pelo gatilho: o valor ficaria
        ilegivel para sempre e derrubaria junto toda consulta que passasse por
        ele. Volta como esta, com ERROR no log.
        """
        conn = sqlite3.connect(_arquivo_do_banco(settings))
        try:
            conn.execute(
                "INSERT INTO trades (id, created_at, executed_at, exchange, symbol, side, "
                "quantity, price, notional, fee, origin, mode) VALUES "
                "('estouro', '2026-01-01 00:00:00', '2026-01-01 00:00:00', 'binance', "
                "'BTC/USDC', 'buy', '123456789012345678901.1234567890123456789', "
                "'1', '1', '0', 'agent', 'dry_run')"
            )
            conn.commit()
        finally:
            conn.close()

        with capture_logs() as eventos:
            async with session_scope(settings) as ses:
                lido = (await TradeRepository(ses).list())[0].quantity

        assert lido == Decimal("123456789012345678901.1234567890123456789")
        erros = [e for e in eventos if e["event"] == "db.dinheiro_gravado_fora_da_faixa"]
        assert erros and erros[0]["log_level"] == "error"

    async def test_no_postgres_o_relatorio_nao_executa_sql(self):
        """No Postgres a coluna e `NUMERIC(38, 18)` e o banco ja impoe a escala."""

        class _ConexaoPostgres:
            dialect = PGDialect()

            def execute(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
                raise AssertionError("o relatorio nao deveria executar SQL no Postgres")

        assert orm.relatar_dinheiro_fora_de_escala(_ConexaoPostgres()) == []

    def test_o_relatorio_cobre_toda_coluna_de_dinheiro_do_schema(self):
        """Modelo novo com coluna `Money` entra no relatorio sem ninguem lembrar."""
        descobertas = orm._colunas_de_dinheiro()
        esperadas = {
            tabela.name: tuple(
                coluna.name for coluna in tabela.columns if isinstance(coluna.type, type(MONEY))
            )
            for tabela in orm.Base.metadata.tables.values()
        }
        esperadas = {nome: cols for nome, cols in esperadas.items() if cols}
        assert descobertas == esperadas
        assert "trades" in descobertas and "portfolio_snapshots" in descobertas


class TestDdlNaoEhTransacional:
    """A medicao que condena qualquer `DROP TRIGGER` no caminho de subida.

    Esta classe nao testa codigo do projeto: mede o SQLite por baixo dele. Ela
    esta aqui porque a brecha grave da rodada 2 nasceu de acreditar no
    contrario -- havia um comentario afirmando "tudo dentro DESTA transacao", e
    a afirmacao era falsa. Se um dia alguem propuser derrubar um gatilho na
    subida "porque o rollback devolve", este teste e a resposta.
    """

    async def test_drop_trigger_sobrevive_ao_rollback_e_o_update_nao(self, settings):
        arquivo = _arquivo_do_banco(settings)
        engine = get_engine(settings)
        async with session_scope(settings) as ses:
            ses.add(orm.AgentRun(agent="strategy", state="running"))

        with pytest.raises(RuntimeError):
            async with engine.begin() as conn:
                # DDL primeiro, como fazia a migracao: nenhuma transacao aberta
                # ainda, entao o pysqlite nao emite BEGIN e o comando e
                # autocommitado na hora.
                await conn.execute(
                    text("DROP TRIGGER trades_sem_update").execution_options(
                        crypto_traders_protecao=True
                    )
                )
                await conn.execute(text("UPDATE agent_runs SET state = 'stopped'"))
                raise RuntimeError("falha no meio da subida")

        conn = sqlite3.connect(arquivo)
        try:
            gatilhos = [
                linha[0]
                for linha in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            ]
            estado = conn.execute("SELECT state FROM agent_runs").fetchone()[0]
        finally:
            conn.close()

        assert "trades_sem_update" not in gatilhos, (
            "se o DDL voltasse atras, a migracao de escala poderia ter continuado existindo"
        )
        assert estado == "running", "o UPDATE do MESMO bloco voltou atras -- o DDL nao"

    def test_nenhum_caminho_de_subida_emite_drop_trigger(self):
        """Guarda estrutural: o codigo de producao nao tem mais esse comando.

        Vale mais que um teste de comportamento aqui, porque o perigo nao e o
        `DROP` executar errado -- e ele existir no caminho de subida.
        """
        raiz = Path(orm.__file__).resolve().parent
        for arquivo in raiz.glob("*.py"):
            # `"DROP TRIGGER` entre aspas: pega comando de verdade, e nao a
            # palavra escrita em comentario ou docstring.
            for linha in arquivo.read_text(encoding="utf-8").splitlines():
                if '"DROP TRIGGER ' not in linha:
                    continue
                # Os comandos do Postgres recriam funcao e gatilho no mesmo
                # bloco, onde o DDL E transacional; o que nao pode existir e
                # DROP solto, do jeito que o SQLite executaria.
                assert " ON " in linha, (
                    f"{arquivo.name}: DROP TRIGGER fora do bloco atomico do Postgres: {linha}"
                )


class TestSubidaRecusaTrilhaDesprotegida:
    """A conferencia de que os gatilhos existem tem de AGIR, nao so existir."""

    def _derrubar_por_fora(self, settings, *nomes: str) -> None:
        conn = sqlite3.connect(_arquivo_do_banco(settings))
        try:
            for nome in nomes:
                conn.execute(f"DROP TRIGGER IF EXISTS {nome}")
            conn.commit()
        finally:
            conn.close()

    async def test_gatilho_faltando_impede_a_subida(self, settings, monkeypatch):
        """Se aplicar a protecao falhar em silencio, a subida para aqui.

        `aplicar_protecao_append_only` e neutralizado de proposito: e o unico
        jeito de chegar em `init_db` com gatilho faltando, e e exatamente o
        estado em que o banco do ensaio em dry_run estava rodando -- zero
        gatilhos, sem nada reclamando.
        """
        self._derrubar_por_fora(settings, "trades_sem_update")
        monkeypatch.setattr(
            "crypto_traders.db.session.aplicar_protecao_append_only", lambda conn: ()
        )
        with pytest.raises(orm.ProtecaoAusenteError, match="trades_sem_update"):
            await init_db(settings)

    async def test_a_subida_normal_recoloca_o_gatilho_derrubado_por_fora(self, settings):
        """Banco antigo (o do ensaio) sobe protegido, sem intervencao."""
        self._derrubar_por_fora(settings, *orm.GATILHOS_ESPERADOS["sqlite"])
        await init_db(settings)
        async with session_scope(settings) as ses:
            nomes = set(
                (
                    await ses.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))
                ).scalars()
            )
        assert set(orm.GATILHOS_ESPERADOS["sqlite"]) <= nomes
        # E a protecao recolocada AGE:
        with pytest.raises(Exception, match="append-only"):
            async with session_scope(settings) as ses:
                await ses.execute(update(orm.AuditLog).values(action="x"))

    async def test_falha_no_meio_da_subida_deixa_a_trilha_protegida(self, settings):
        """O ataque C8 do critico: morrer EM VOO, com o dinheiro torto no banco.

        Uma linha fora de escala e semeada (era o que fazia a migracao antiga
        derrubar os gatilhos), a subida explode logo depois da protecao, e o que
        se mede depois e se o historico continua imutavel.
        """
        conn = sqlite3.connect(_arquivo_do_banco(settings))
        try:
            conn.execute(
                "INSERT INTO trades (id, created_at, executed_at, exchange, symbol, side,"
                " quantity, price, notional, fee, origin, mode) VALUES"
                " ('torto', '2026-01-01', '2026-01-01', 'binance', 'BTC/USDC', 'buy', '1',"
                " '100', '21.6178180996209239808000', '0.1', 'agent', 'dry_run')"
            )
            conn.commit()
        finally:
            conn.close()

        original = orm.relatar_dinheiro_fora_de_escala

        def explode(connection):
            original(connection)
            raise RuntimeError("falha simulada no meio da subida")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("crypto_traders.db.session.relatar_dinheiro_fora_de_escala", explode)
            with pytest.raises(RuntimeError):
                await init_db(settings)

        conn = sqlite3.connect(_arquivo_do_banco(settings))
        conn.execute("PRAGMA recursive_triggers=ON")
        try:
            gatilhos = {
                linha[0]
                for linha in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            }
            assert set(orm.GATILHOS_ESPERADOS["sqlite"]) <= gatilhos, gatilhos
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("UPDATE trades SET notional = '0'")
                conn.commit()
        finally:
            conn.close()

    def test_a_lista_de_gatilhos_esperados_cobre_as_tres_trilhas(self):
        """A conferencia so vale se a lista dela for a lista de verdade."""
        for dialeto in ("sqlite", "postgresql"):
            esperados = orm.GATILHOS_ESPERADOS[dialeto]
            for tabela in orm.TABELAS_APPEND_ONLY:
                assert any(nome.startswith(tabela) for nome in esperados), (
                    f"{dialeto} nao confere gatilho de {tabela}"
                )
            criados = " ".join(
                comando
                for gatilhos in orm._GATILHOS_POR_TABELA.values()
                for comando in gatilhos[dialeto]
                if comando.upper().startswith("CREATE TRIGGER")
            )
            for nome in esperados:
                assert nome in criados, f"{dialeto}: {nome} e esperado mas nunca criado"


class TestSqlCruNaoDesligaAProtecao:
    """A ultima barreira nao pode ser removivel por quem ela deveria conter.

    SQL cru por `text()` nao e visto pela barreira do Core (nao ha `.table`) nem
    pelo `before_flush` (nao ha objeto ORM). Ate a rodada 2, a unica protecao
    contra ele era o gatilho -- e a sessao normal da aplicacao conseguia
    derrubar o gatilho e adulterar em seguida. Medido: a linha de `audit_log`
    voltava como 'ADULTERADO'.
    """

    async def _uma_linha(self, settings) -> None:
        async with session_scope(settings) as ses:
            ses.add(orm.AuditLog(actor="paulo", action="risk_limits_updated"))

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TRIGGER IF EXISTS audit_log_sem_update",
            "drop trigger audit_log_sem_delete",
            "DROP TRIGGER trades_sem_update",
            "PRAGMA writable_schema = ON",
            "PRAGMA recursive_triggers = OFF",
            "DROP TABLE audit_log",
            "ALTER TABLE trades RENAME TO trades_velho",
            "DROP TABLE IF EXISTS risk_events",
        ],
    )
    async def test_sabotagem_por_sql_cru_e_recusada(self, settings, sql):
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(settings) as ses:
                await ses.execute(text(sql))

    async def test_a_adulteracao_completa_da_rodada_2_nao_acontece_mais(self, settings):
        """O ataque inteiro, do jeito que passou: derrubar e reescrever."""
        await self._uma_linha(settings)
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(settings) as ses:
                await ses.execute(text("DROP TRIGGER IF EXISTS audit_log_sem_update"))
                await ses.execute(text("UPDATE audit_log SET action = 'ADULTERADO' WHERE id = 1"))

        async with session_scope(settings) as ses:
            linha = (await ses.execute(select(orm.AuditLog))).scalars().one()
            gatilhos = set(
                (
                    await ses.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))
                ).scalars()
            )
        assert linha.action == "risk_limits_updated"
        assert "audit_log_sem_update" in gatilhos, "o gatilho saiu do ar"

    async def test_exec_driver_sql_tambem_e_recusado(self, settings):
        """O desvio que `before_execute` nao ve.

        `exec_driver_sql` manda a string direto ao driver sem passar por
        `before_execute` -- seria uma volta inteira em torno da barreira se ela
        vivesse la. Por isso a barreira vive em `before_cursor_execute`, que e o
        ultimo ponto antes do cursor.
        """
        await self._uma_linha(settings)
        engine = get_engine(settings)
        with pytest.raises(AuditoriaImutavelError):
            async with engine.begin() as conn:
                await conn.exec_driver_sql("DROP TRIGGER IF EXISTS audit_log_sem_update")

        async with session_scope(settings) as ses:
            gatilhos = set(
                (
                    await ses.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))
                ).scalars()
            )
        assert "audit_log_sem_update" in gatilhos

    async def test_sql_cru_legitimo_continua_passando(self, settings):
        """A barreira e cirurgica: nao pode congelar o SQL normal do projeto."""
        async with session_scope(settings) as ses:
            ses.add(orm.AgentRun(agent="strategy", state="running"))
        async with session_scope(settings) as ses:
            assert (await ses.execute(text("SELECT count(*) FROM trades"))).scalar_one() == 0
            await ses.execute(text("UPDATE agent_runs SET state = 'stopped'"))
            await ses.execute(text("DELETE FROM agent_runs"))
            # Tabela que nao e trilha protegida pode ate ser recriada.
            await ses.execute(text("CREATE TABLE rascunho (a INTEGER)"))
            await ses.execute(text("DROP TABLE rascunho"))

    async def test_a_propria_subida_continua_conseguindo_criar_os_gatilhos(self, settings):
        """A barreira deixa passar o unico caminho autorizado -- e so ele."""
        engine = get_engine(settings)
        async with engine.begin() as conn:
            aplicados = await conn.run_sync(orm.aplicar_protecao_append_only)
            existentes = await conn.run_sync(orm.verificar_protecao_append_only)
        assert aplicados, "a subida nao aplicou gatilho nenhum"
        assert set(orm.GATILHOS_ESPERADOS["sqlite"]) <= set(existentes)


class TestBarreiraDoCoreCobreAsTresTrilhas:
    """Uma guarda estrutural: a barreira nao pode valer so para `audit_log`.

    Foi assim que a brecha do `INSERT OR REPLACE` sobreviveu a primeira rodada --
    a checagem existia, mas so olhava um tipo de comando numa tabela. Aqui a
    cobertura e verificada a partir da propria lista de tabelas protegidas, entao
    trilha nova entra sem ninguem lembrar de escrever o teste.
    """

    def test_a_lista_de_tabelas_protegidas_e_a_esperada(self):
        assert set(orm.TABELAS_APPEND_ONLY) == {"audit_log", "risk_events", "trades"}

    @pytest.mark.parametrize("tabela", sorted(orm.TABELAS_APPEND_ONLY))
    async def test_update_em_massa_e_recusado_em_toda_trilha(self, settings, tabela):
        alvo = orm.Base.metadata.tables[tabela]
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(settings) as ses:
                await ses.execute(update(alvo).values(id="x"))

    @pytest.mark.parametrize("tabela", sorted(orm.TABELAS_APPEND_ONLY))
    async def test_insert_com_resolucao_de_conflito_e_recusado_em_toda_trilha(
        self, settings, tabela
    ):
        alvo = orm.Base.metadata.tables[tabela]
        with pytest.raises(AuditoriaImutavelError):
            async with session_scope(settings) as ses:
                await ses.execute(insert(alvo).prefix_with("OR REPLACE").values(id="x"))

    @pytest.mark.parametrize("tabela", sorted(orm.TABELAS_APPEND_ONLY))
    async def test_toda_trilha_tem_gatilho_no_banco(self, settings, tabela):
        """A barreira de aplicacao nao vale nada sozinha: precisa do banco atras."""
        async with session_scope(settings) as ses:
            nomes = (
                await ses.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :t"),
                    {"t": tabela},
                )
            ).scalars().all()
        assert len(nomes) >= 2, f"{tabela} sem gatilho de UPDATE e de DELETE: {nomes}"


class _ResultadoFalso:
    def __init__(self, linhas: list[Any]) -> None:
        self._linhas = linhas

    def first(self) -> Any:
        return self._linhas[0] if self._linhas else None


class _ConexaoFalsa:
    """Conexao de mentira que anota o que foi executado e pode falhar de proposito."""

    def __init__(self, engine: _EngineFalso) -> None:
        self._engine = engine

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _ResultadoFalso:
        sql = str(statement)
        self._engine.registro.append(sql)
        if "pg_extension" in sql:
            return _ResultadoFalso([(1,)] if self._engine.timescale else [])
        if self._engine.falha_hypertable and "create_hypertable" in sql:
            raise RuntimeError('function create_hypertable(unknown, unknown) does not exist')
        return _ResultadoFalso([])

    async def run_sync(self, fn: Any, *args: Any) -> None:
        self._engine.registro.append(f"run_sync:{getattr(fn, '__name__', fn)}")


class _Escopo:
    def __init__(self, engine: _EngineFalso, abertura: str, fechamento: str) -> None:
        self._engine = engine
        self._abertura = abertura
        self._fechamento = fechamento

    async def __aenter__(self) -> _ConexaoFalsa:
        self._engine.registro.append(self._abertura)
        return _ConexaoFalsa(self._engine)

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._engine.registro.append("rollback" if exc_type else self._fechamento)
        return False


class _EngineFalso:
    """Engine de mentira: registra transacoes e comandos, sem banco nenhum.

    Existe porque o defeito e de CONTROLE DE TRANSACAO, e nao de SQL: no SQLite
    (o unico banco disponivel na suite) um comando que falha nao contamina a
    transacao, entao o defeito era invisivel la. O que precisa ser provado e a
    fronteira das transacoes, e ela da para observar.
    """

    def __init__(self, *, timescale: bool = True, falha_hypertable: bool = False) -> None:
        self.registro: list[str] = []
        self.timescale = timescale
        self.falha_hypertable = falha_hypertable

    def begin(self) -> _Escopo:
        return _Escopo(self, "begin", "commit")

    def connect(self) -> _Escopo:
        return _Escopo(self, "connect", "close")


class TestHypertablesDoTimescale:
    """A otimizacao nunca pode custar a subida -- e ja custou o schema."""

    async def test_criadas_quando_a_extensao_existe(self):
        engine = _EngineFalso(timescale=True)
        criadas = await criar_hypertables(engine)

        assert criadas == ["candles", "portfolio_snapshots"]
        chamadas = [sql for sql in engine.registro if "create_hypertable" in sql]
        assert len(chamadas) == 2
        assert "candles" in chamadas[0] and "open_time" in chamadas[0]
        assert "portfolio_snapshots" in chamadas[1] and "timestamp" in chamadas[1]

    async def test_cada_hypertable_vai_na_propria_transacao(self):
        """Uma transacao por tabela: falha de uma nao arrasta a outra."""
        engine = _EngineFalso(timescale=True)
        await criar_hypertables(engine)

        transacoes = [passo for passo in engine.registro if passo in {"begin", "commit"}]
        assert transacoes == ["begin", "commit", "begin", "commit"]

    async def test_ausencia_da_extensao_nao_derruba_nem_tenta_criar(self):
        engine = _EngineFalso(timescale=False)
        criadas = await criar_hypertables(engine)

        assert criadas == []
        assert not [sql for sql in engine.registro if "create_hypertable" in sql]
        assert "begin" not in engine.registro  # nada de DDL numa transacao a esmo

    async def test_falha_do_create_hypertable_nao_propaga(self):
        engine = _EngineFalso(timescale=True, falha_hypertable=True)
        criadas = await criar_hypertables(engine)

        assert criadas == []
        # Cada tentativa fracassada e desfeita sozinha, e nao ha commit nenhum.
        assert engine.registro.count("rollback") == 2
        assert "commit" not in engine.registro

    async def test_schema_e_commitado_antes_de_tocar_no_timescale(self, tmp_path, monkeypatch):
        """O defeito, na sua forma exata: DDL e hypertable na mesma transacao.

        No PostgreSQL, `create_hypertable` sem a extensao aborta a transacao; o
        COMMIT seguinte vira ROLLBACK e leva o `create_all` embora. Este teste
        fixa a ordem que impede isso: schema commitado ANTES, hypertable depois,
        em transacao propria.
        """
        engine = _EngineFalso(timescale=True, falha_hypertable=True)
        monkeypatch.setattr(session_mod, "get_engine", lambda _=None: engine)
        configurado = Settings(
            database_url="postgresql+asyncpg://u:p@localhost:5432/crypto_traders"
        )

        await init_db(configurado)

        passos = engine.registro
        fim_do_schema = passos.index("commit")
        primeira_hypertable = next(
            indice for indice, sql in enumerate(passos) if "create_hypertable" in sql
        )
        assert "run_sync:create_all" in passos
        assert passos.index("run_sync:create_all") < fim_do_schema
        assert fim_do_schema < primeira_hypertable, f"schema nao foi commitado antes: {passos}"

    async def test_sqlite_nao_tenta_hypertable(self, tmp_path, monkeypatch):
        engine = _EngineFalso(timescale=True)
        monkeypatch.setattr(session_mod, "get_engine", lambda _=None: engine)
        configurado = Settings(
            database_url=f"sqlite+aiosqlite:///{(tmp_path / 'x.db').as_posix()}"
        )

        await init_db(configurado)

        assert not [sql for sql in engine.registro if "create_hypertable" in sql]
        assert "run_sync:aplicar_protecao_append_only" in engine.registro


class TestOSchemaPermiteHypertable:
    """A pergunta que nunca tinha sido feita: o TimescaleDB ACEITARIA estas tabelas?

    Os testes acima medem o CONTROLE DE TRANSACAO contra um engine de mentira
    que aceita qualquer SQL -- provam o fluxo, nao o fato. E o fato era outro: o
    TimescaleDB exige que todo indice unico contenha a coluna de particao, e
    `candles` e `portfolio_snapshots` tinham PK que nao continha. As duas
    chamadas de `create_hypertable` eram RECUSADAS pelo servidor, o `except`
    engolia o erro num WARNING, e o sistema subia em tabela comum para sempre,
    achando que tinha hypertable.

    Aqui a regra e verificada contra o schema de verdade, sem servidor nenhum.
    """

    @pytest.mark.parametrize(("tabela", "coluna"), list(session_mod.HYPERTABLES))
    def test_todo_indice_unico_contem_a_coluna_de_particao(self, tabela, coluna):
        assert orm.hypertable_possivel(tabela, coluna) == []

    def test_a_chave_primaria_de_candles_carrega_a_coluna_de_tempo_no_ddl(self):
        """Nao basta a metadata concordar: o DDL emitido e o que o servidor le."""
        ddl = str(CreateTable(orm.Candle.__table__).compile(dialect=DIALETOS["postgresql-asyncpg"]))
        assert "PRIMARY KEY (exchange, symbol, timeframe, open_time)" in ddl
        ddl_snapshots = str(
            CreateTable(orm.PortfolioSnapshot.__table__).compile(
                dialect=DIALETOS["postgresql-asyncpg"]
            )
        )
        assert "PRIMARY KEY (id, timestamp)" in ddl_snapshots

    def test_a_regra_reprova_quem_nao_cumpre(self):
        """A guarda so vale se souber dizer nao: `trades` nao serve de hypertable."""
        problemas = orm.hypertable_possivel("trades", "executed_at")
        assert problemas == ["PRIMARY KEY ['id']"], problemas

    async def test_tabela_que_nao_cumpre_a_regra_nao_e_sequer_tentada(self, monkeypatch):
        """E o WARNING generico vira ERROR nomeando o indice que impede."""
        engine = _EngineFalso(timescale=True)
        monkeypatch.setattr(session_mod, "HYPERTABLES", (("trades", "executed_at"),))
        with capture_logs() as eventos:
            criadas = await criar_hypertables(engine)

        assert criadas == []
        assert not [sql for sql in engine.registro if "create_hypertable" in sql]
        erros = [e for e in eventos if e["event"] == "db.hypertable_impossivel"]
        assert erros and erros[0]["log_level"] == "error"
        assert "PRIMARY KEY ['id']" in erros[0]["detail"]

    async def test_hypertable_que_o_servidor_recusa_sai_como_erro(self):
        """"Mandei criar" nao e "foi criada": a ausencia tem de gritar."""
        engine = _EngineFalso(timescale=True, falha_hypertable=True)
        with capture_logs() as eventos:
            criadas = await criar_hypertables(engine)

        assert criadas == []
        ausentes = [e for e in eventos if e["event"] == "db.hypertables_ausentes"]
        assert ausentes and ausentes[0]["log_level"] == "error"
        assert ausentes[0]["tables"] == ["candles", "portfolio_snapshots"]


#: Postgres de verdade, quando houver um. Sem servidor a suite nao tem como
#: exercitar o dialeto real -- os testes acima medem os processadores e o DDL,
#: que e o que da para medir sem banco. Com
#: `POSTGRES_TEST_URL=postgresql+asyncpg://...` apontando para um banco
#: DESCARTAVEL (o `docker compose up` do projeto serve), esta classe fecha a
#: prova de ponta a ponta: ida e volta de 18 casas, hypertables criadas e
#: auditoria imutavel no dialeto de producao.
POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="POSTGRES_TEST_URL nao definida")
class TestPostgresDeVerdade:
    """Opcional por falta de servidor na maquina, nao por ser menos importante."""

    @pytest.fixture
    async def pg(self):
        await dispose_engine()
        configurado = Settings(database_url=POSTGRES_TEST_URL)
        await init_db(configurado)
        yield configurado
        await dispose_engine()

    async def test_round_trip_de_18_casas(self, pg):
        async with session_scope(pg) as ses:
            await TradeRepository(ses).record(
                executed_at=datetime(2026, 1, 5, tzinfo=UTC),
                exchange="binance",
                symbol="BTC/USDC",
                side="buy",
                quantity=DEZOITO_CASAS,
                price=Decimal("1"),
                fee=UM_WEI,
                realized_pnl=-UM_WEI,
            )
        async with session_scope(pg) as ses:
            trade = (await TradeRepository(ses).list(limit=1))[0]

        assert isinstance(trade.quantity, Decimal)
        assert trade.quantity == DEZOITO_CASAS
        assert trade.fee == UM_WEI
        assert trade.realized_pnl == -UM_WEI

    async def test_hypertables_existem_quando_a_extensao_existe(self, pg):
        engine = get_engine(pg)
        assert await timescale_instalado(engine), "instale a extensao para valer este teste"
        async with session_scope(pg) as ses:
            nomes = (
                await ses.execute(
                    text("SELECT hypertable_name FROM timescaledb_information.hypertables")
                )
            ).scalars().all()
        assert {"candles", "portfolio_snapshots"} <= set(nomes)

    async def test_auditoria_e_imutavel_tambem_no_postgres(self, pg):
        async with session_scope(pg) as ses:
            await AuditLogRepository(ses).append(action="prova_postgres", actor="teste")
        with pytest.raises(Exception, match="append-only"):
            async with session_scope(pg) as ses:
                await ses.execute(text("UPDATE audit_log SET action = 'ADULTERADO'"))

    async def test_os_gatilhos_esperados_existem_de_fato_no_postgres(self, pg):
        """A lista de `GATILHOS_ESPERADOS` conferida contra o catalogo real.

        No SQLite a suite mede isso o tempo todo; no Postgres o dialeto inteiro
        (funcoes plpgsql, gatilhos de TRUNCATE) esta escrito e so roda aqui.
        """
        engine = get_engine(pg)
        async with engine.begin() as conn:
            existentes = await conn.run_sync(orm.verificar_protecao_append_only)
        assert set(orm.GATILHOS_ESPERADOS["postgresql"]) <= set(existentes)

    async def test_truncate_da_trilha_e_recusado_no_postgres(self, pg):
        """TRUNCATE nao existe no SQLite: este caminho so da para provar aqui."""
        async with session_scope(pg) as ses:
            await AuditLogRepository(ses).append(action="prova_truncate", actor="teste")
        with pytest.raises(Exception, match="append-only"):
            async with session_scope(pg) as ses:
                await ses.execute(
                    text("TRUNCATE audit_log").execution_options(crypto_traders_protecao=True)
                )

    async def test_linha_fora_de_escala_le_igual_ao_sqlite(self, pg):
        """A promessa dos dois dialetos, medida contra um servidor de verdade."""
        async with session_scope(pg) as ses:
            await TradeRepository(ses).record(
                executed_at=datetime(2026, 1, 6, tzinfo=UTC),
                exchange="binance",
                symbol="ETH/USDC",
                side="sell",
                quantity=Decimal("0.4316666666666666666666666668"),
                price=Decimal("1"),
            )
        async with session_scope(pg) as ses:
            trade = (await TradeRepository(ses).list(limit=1))[0]
        assert trade.quantity == Decimal("0.431666666666666667")
