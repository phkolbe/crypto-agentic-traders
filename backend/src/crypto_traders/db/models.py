"""Modelo de dados (SQLAlchemy 2.0).

Portabilidade SQLite <-> PostgreSQL/TimescaleDB e requisito: os tipos usados aqui
existem nos dois. Valores monetarios usam `Numeric` (nunca `Float`), preservando
precisao decimal exata no Postgres, e TEXTO no SQLite. A escala 18 e imposta na
gravacao E na leitura (`normalizar_dinheiro` e `normalizar_dinheiro_lido`), que e
o que faz os dois dialetos devolverem o mesmo numero para a mesma linha --
inclusive para linha antiga, sem reescrever historico.

Tres trilhas deste schema sao append-only, e a imutabilidade delas e imposta pelo
banco, nao combinada entre chamadores: `audit_log` (quem mexeu em risco, quem
ligou LIVE), `risk_events` (o motivo de cada sinal aprovado ou rejeitado) e
`trades` (o que de fato aconteceu com dinheiro, com a unica excecao do
lancamento manual digitado errado). Ver `TABELAS_APPEND_ONLY`.

Sobre o alcance dessa imutabilidade, dito onde nao da para nao ler: ela vale
contra os caminhos da aplicacao -- ORM, Core e SQL cru, em qualquer grafia --, e
NAO vale contra codigo hostil rodando dentro deste processo, que desliga todos
os gatilhos com uma chamada de `setconfig` sem emitir SQL (medido). Os limites
estao escritos no bloco acima de `motivo_para_negar_no_sqlite`, e estao escritos
de proposito: neste projeto, protecao que a documentacao promete e o codigo nao
entrega ja custou tres defeitos silenciosos.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Context, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Connection,
    Date,
    DateTime,
    Delete,
    Dialect,
    Engine,
    Float,
    Index,
    Insert,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    Update,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from ..logging_setup import get_logger

log = get_logger(__name__)

#: Casas decimais das colunas de dinheiro: 18 cobre wei (ETH) e satoshi (BTC).
MONEY_SCALE = 18
#: Digitos totais. 38 - 18 = 20 casas inteiras, ~10^20 unidades da moeda.
MONEY_DIGITS = 38
#: Precisao ampla o suficiente para satoshis (8 casas) e tokens com 18 casas.
MONEY_PRECISION = Numeric(MONEY_DIGITS, MONEY_SCALE)

_MONEY_QUANTUM = Decimal(1).scaleb(-MONEY_SCALE)
#: Contexto proprio: o padrao do `decimal` tem 28 digitos e recusaria 38.
_MONEY_CONTEXT = Context(prec=MONEY_DIGITS)


def normalizar_dinheiro(value: Any) -> Decimal:
    """Traz o valor para exatamente o que `NUMERIC(38, 18)` consegue guardar.

    Existe porque os dois dialetos discordavam do lado de fora dessa faixa, e a
    discordancia era silenciosa. `cost / held` (custo medio, em
    `agents/portfolio.py`) devolve 28 digitos significativos, e esse resultado
    entra em `Trade.realized_pnl`: o SQLite guardava as 28 casas como texto e o
    PostgreSQL arredondava para 18. Mesmo negocio, dois valores gravados,
    dependendo do banco -- e uma migracao de SQLite para Postgres mudaria o
    historico sem aviso.

    Arredondar aqui, para os dois, faz o valor gravado ser o mesmo em qualquer
    backend. Nada de real se perde: 18 casas e a maior precisao que existe em
    cripto (wei).

    Tres bordas, decididas de proposito e nao por omissao:

    - **Acima de 20 casas inteiras** nao ha arredondamento possivel, e ai o valor
      e RECUSADO em vez de gravado torto -- o Postgres levantaria `numeric field
      overflow` e o SQLite aceitaria calado, que e o pior dos dois.
    - **Entrada que nao e numero** (`bool`, string qualquer, objeto) e recusada
      com `ValueError`, e nao com o `decimal.InvalidOperation` que escapava antes:
      quem chama trata "valor monetario invalido", nao a biblioteca decimal. `bool`
      e recusado explicitamente porque `True` e `int` para o Python e viraria
      1 unidade de moeda por acidente de tipagem.
    - **Abaixo de um wei** (`1E-25`, por exemplo) o valor ARREDONDA para zero, e
      nao e recusado. Aqui a decisao e o contrario da do estouro, e a razao e que
      o estado seguro muda de lado: recusar um estouro impede gravar um numero
      errado; recusar 1E-25 impediria REGISTRAR UMA NEGOCIACAO QUE JA ACONTECEU
      por causa de 0,0000000000000000000000001 USDC. Perder o registro e pior que
      perder o vigesimo quinto decimal. O arredondamento nao e silencioso: sempre
      que um valor diferente de zero colapsa para zero, sai um `WARNING`
      `db.dinheiro_abaixo_de_um_wei` com o valor original.
    """
    if isinstance(value, bool):
        raise ValueError(f"valor monetario invalido (bool nao e dinheiro): {value!r}")
    if isinstance(value, Decimal):
        number = value
    else:
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"valor monetario invalido: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"valor monetario nao finito: {number}")

    exponent = number.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > MONEY_SCALE:
        try:
            arredondado = number.quantize(_MONEY_QUANTUM, context=_MONEY_CONTEXT)
        except InvalidOperation as exc:  # inteiro grande demais para 38 digitos
            raise ValueError(
                f"valor monetario fora do alcance de NUMERIC({MONEY_DIGITS}, {MONEY_SCALE}): "
                f"{number}"
            ) from exc
        if arredondado == 0 and number != 0:
            log.warning(
                "db.dinheiro_abaixo_de_um_wei",
                original=str(number),
                gravado="0",
                detail=f"menor que 1E-{MONEY_SCALE}; arredondado para zero de proposito",
            )
        number = arredondado

    casas_inteiras = 0 if number == 0 else number.adjusted() + 1
    if casas_inteiras > MONEY_DIGITS - MONEY_SCALE:
        raise ValueError(
            f"valor monetario fora do alcance de NUMERIC({MONEY_DIGITS}, {MONEY_SCALE}): {number}"
        )
    return number


def normalizar_dinheiro_lido(bruto: Any) -> Decimal:
    """A MESMA escala, aplicada na LEITURA. E aqui que os dialetos se igualam.

    O PostgreSQL guarda `NUMERIC(38, 18)`: qualquer valor que chegue com mais de
    18 casas ja volta arredondado, porque o proprio banco impoe a escala. O
    SQLite guarda TEXTO, entao devolve exatamente o que foi escrito -- inclusive
    linha antiga com 22, 25 ou 28 casas, escrita antes de `normalizar_dinheiro`
    existir (medido no banco do ensaio em dry_run: 4 valores fora da escala).

    Sem esta funcao, o MESMO historico lido em SQLite e em PostgreSQL daria dois
    numeros diferentes. A tentacao obvia era consertar isso reescrevendo as
    linhas antigas na subida -- e essa tentacao custou caro: reescrever `trades`
    exige derrubar o gatilho `trades_sem_update`, e no pysqlite o DDL nao entra
    na transacao (medido: um `DROP TRIGGER` emitido antes do primeiro DML da
    conexao e autocommitado, e sobrevive ao rollback). Uma falha na subida --
    ou a maquina suspendendo, que ja aconteceu neste projeto -- deixava o
    historico de dinheiro mutavel, sem nada dizendo isso.

    Normalizar na leitura chega ao mesmo resultado sem escrever uma linha
    sequer: o passado nao muda de valor, a trilha nunca fica desprotegida, e os
    dois dialetos devolvem o mesmo numero. Vale tambem para linha escrita por
    fora da aplicacao, que uma migracao de uma vez so nao alcancaria.

    Fora da faixa de `NUMERIC(38, 18)` (mais de 20 casas inteiras) o valor volta
    COMO ESTA, com `ERROR` no log. Recusar a leitura seria pior de um jeito sem
    saida: o UPDATE que consertaria a linha e recusado pelo gatilho, entao o
    valor ficaria ilegivel para sempre e derrubaria junto toda consulta que
    passasse por ele. Devolver o numero exato que esta gravado, gritando, deixa
    o problema visivel sem apagar o historico.
    """
    if bruto is None:  # pragma: no cover - chamador ja trata None
        raise ValueError("valor monetario nulo")
    # `Decimal(str(...))` e nao `Decimal(...)`: se o driver devolvesse float,
    # `Decimal(0.1)` traria o lixo binario inteiro, `Decimal("0.1")` nao.
    numero = bruto if isinstance(bruto, Decimal) else Decimal(str(bruto))
    try:
        return normalizar_dinheiro(numero)
    except ValueError as exc:
        log.error(
            "db.dinheiro_gravado_fora_da_faixa",
            value=str(numero),
            error=str(exc),
            detail=(
                f"valor gravado nao cabe em NUMERIC({MONEY_DIGITS}, {MONEY_SCALE}); "
                "devolvido como esta, sem arredondar"
            ),
        )
        return numero


class Money(TypeDecorator):
    """Valor monetario exato, em SQLite e em PostgreSQL.

    O SQLite nao tem tipo decimal nativo: um `Numeric` la vira `REAL`, ou seja,
    float64. Isso e suficiente para estragar dinheiro de verdade -- gravar
    `0.4` e ler `0.400000000000000022` foi exatamente o que aconteceu antes
    deste tipo existir, e o erro se propaga por PnL, custo medio e exportacao
    fiscal.

    A solucao e guardar o decimal como TEXTO no SQLite (representacao exata) e
    como `NUMERIC` no PostgreSQL, que tem decimal de verdade. A conversao para
    `Decimal` acontece na leitura, nos dois casos.

    Consequencia a lembrar: no SQLite estas colunas sao texto, entao `ORDER BY`
    e comparacoes numericas em SQL sobre elas nao sao confiaveis. Nenhuma query
    do projeto faz isso -- agregacoes de dinheiro sao somadas em Python.
    """

    impl = Numeric
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(64))
        return dialect.type_descriptor(MONEY_PRECISION)

    def bind_processor(self, dialect: Dialect) -> Callable[[Any], Any]:
        """Conversao de saida SEM o processador do `impl`. Nao e detalhe.

        `TypeDecorator` normalmente encadeia `process_bind_param` com o
        processador do tipo interno, e `Numeric.bind_processor` devolve
        `processors.to_float` quando o dialeto nao declara
        `supports_native_decimal` -- o que nenhum dialeto base do PostgreSQL
        declara (o padrao em `engine/default.py` e False). Medido com o dialeto
        base: `Decimal('1.234567890123456789')` chegava ao banco como
        `1.2345678901234567` e voltava como `...456690`, dois digitos a menos.

        Hoje `asyncpg` e `psycopg2` sobrepoem `Numeric` por colspecs que nao
        convertem, entao a producao estava salva por sorte da escolha de driver.
        Dinheiro nao depende de sorte: a conversao passa a ser nossa, sempre.
        """

        def processo(value: Any) -> Any:
            return self.process_bind_param(value, dialect)

        return processo

    def result_processor(self, dialect: Dialect, coltype: Any) -> Callable[[Any], Any]:
        """Leitura tambem sem o processador do `impl`, pelo mesmo motivo."""

        def processo(value: Any) -> Any:
            return self.process_result_value(value, dialect)

        return processo

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        """Grava o valor. `float` e RECUSADO aqui, e nao convertido em silencio.

        D7 diz Decimal ou string em todas as camadas, e esta e a ultima camada
        antes do banco -- o lugar mais barato de fazer a regra valer, porque
        depois daqui o numero errado esta gravado.

        `float` nao e recusado por purismo: ele JA PERDEU valor antes de chegar
        aqui, e a conversao `Decimal(str(...))` grava a perda com cara de numero
        exato. Medido: o literal `21.617818099620924` e o float cuja repr mais
        curta e `'21.617818099620923'`, entao o codigo escreve um numero e o
        banco guarda outro, sem erro nenhum e sem aviso nenhum. Um digito de
        dinheiro trocado em silencio e exatamente a familia de defeito que este
        projeto ja pagou tres vezes.

        `normalizar_dinheiro` continua aceitando `float` de proposito: ela e o
        conversor de entrada crua (JSON de exchange, texto de CSV), e ali `float`
        e o que chega. Quem escolhe o que vai para o banco e este metodo.
        """
        if value is None:
            return None
        if isinstance(value, float):
            raise ValueError(
                "valor monetario nao pode ser float (D7: Decimal ou string em todas as "
                f"camadas): {value!r}. Use Decimal(str(...)) na origem -- este float ja "
                "chegou aqui com o arredondamento binario embutido, e gravar assim "
                "esconde o digito perdido."
            )
        number = normalizar_dinheiro(value)
        if dialect.name == "sqlite":
            # `format(..., "f")` evita notacao cientifica: `1E-8` como texto
            # voltaria como string nao comparavel e confundiria a leitura.
            return format(number, "f")
        return number

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        # Escala imposta tambem na LEITURA, e nao so na gravacao: e isto que faz
        # SQLite e PostgreSQL devolverem o mesmo numero para a MESMA linha,
        # inclusive linha antiga gravada antes de `normalizar_dinheiro` existir.
        # Ver `normalizar_dinheiro_lido` para o porque de nao ser uma migracao.
        return normalizar_dinheiro_lido(value)


MONEY = Money()


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Candle(Base):
    """Serie temporal de candles OHLCV.

    No Postgres esta tabela vira uma hypertable do TimescaleDB (ver
    `docker/timescale_init.sql`); no SQLite continua uma tabela comum.

    A chave primaria e a chave NATURAL, e isso nao e estetica: o TimescaleDB
    exige que todo indice unico da tabela contenha a coluna de particao. Havia
    aqui um `id` sequencial como PK sozinha -- e enquanto ele existiu,
    `create_hypertable('candles', 'open_time')` era RECUSADO ("cannot create a
    unique index without the column open_time"), o `except` de
    `criar_hypertables` engolia o erro, e o sistema subia em tabela comum
    achando que tinha hypertable. O `id` nao era usado por nenhuma consulta
    (conferido) e a chave natural ja existia como `UniqueConstraint`; promove-la
    a PK remove o indice redundante e torna a hypertable possivel. A guarda
    contra a volta do defeito esta em `hypertable_possivel`, exercitada por
    teste sobre este schema.
    """

    __tablename__ = "candles"

    exchange: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(MONEY)
    high: Mapped[Decimal] = mapped_column(MONEY)
    low: Mapped[Decimal] = mapped_column(MONEY)
    close: Mapped[Decimal] = mapped_column(MONEY)
    volume: Mapped[Decimal] = mapped_column(MONEY)


class Signal(Base):
    """Sinal gerado por uma estrategia, aprovado ou nao."""

    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    strategy: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[float] = mapped_column(Numeric(5, 4))
    reason: Mapped[str] = mapped_column(Text)
    reference_price: Mapped[Decimal] = mapped_column(MONEY)
    indicators: Mapped[dict] = mapped_column(JSON, default=dict)


class RiskEvent(Base):
    """Toda decisao do Risk Manager, incluindo rejeicoes e circuit breaker.

    Esta tabela e a resposta a pergunta "por que o agente (nao) operou?".

    Append-only imposto pelo banco (`TABELAS_APPEND_ONLY`). E aqui que mora o
    MOTIVO da rejeicao de um sinal -- `decision` e `reasons`, gravados por
    `RiskEventRepository.save_assessment` --, e nao em `audit_log`. Enquanto esta
    tabela aceitava UPDATE, "todo sinal rejeitado logado com o motivo em trilha
    append-only" era falso mesmo com `audit_log` blindado: bastava reescrever
    `decision='rejected'` para `'approved'` com `reasons=[]`. Medido, funcionava.
    """

    __tablename__ = "risk_events"
    __table_args__ = (Index("ix_risk_events_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    event_type: Mapped[str] = mapped_column(String(32))
    signal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    approved_quantity: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    approved_notional: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)


class Order(Base):
    """Ordem enviada (ou simulada) e seu ciclo de vida na exchange."""

    __tablename__ = "orders"
    __table_args__ = (
        # Idempotencia: a exchange e o banco concordam que este ID e unico.
        UniqueConstraint("client_order_id", name="uq_order_client_id"),
        Index("ix_orders_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    client_order_id: Mapped[str] = mapped_column(String(64))
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_event_id: Mapped[str] = mapped_column(String(32))
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    notional: Mapped[Decimal] = mapped_column(MONEY)
    stop_loss: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    status: Mapped[str] = mapped_column(String(24))
    filled_quantity: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    average_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(16))
    """dry_run | testnet | live -- registrado por ordem, para o historico nunca ser ambiguo."""

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class Trade(Base):
    """Historico consolidado: negociacoes dos agentes E lancamentos manuais.

    Uma unica tabela para os dois casos, distinguidos por `origin`. Isso mantem o
    dashboard, os filtros e a exportacao fiscal simples, sem UNION entre tabelas.

    Append-only imposto pelo banco, com UMA excecao: UPDATE nunca (nenhum caminho
    do codigo altera trade gravado), DELETE apenas de lancamento manual
    (`origin='manual'`). Essa excecao ja existia, mas so na rota
    `DELETE /trades/{id}` -- append-only por convencao, que e o arranjo que se
    provou nao valer nada em `audit_log`: `delete(orm.Trade)` pelo Core apagava a
    operacao de agente sem passar pela rota. Medido. Agora quem decide e o
    gatilho `trades_sem_delete_de_agente`, que olha `OLD.origin` linha por linha.
    """

    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_executed", "executed_at"),
        Index("ix_trades_symbol", "symbol"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    price: Mapped[Decimal] = mapped_column(MONEY)
    notional: Mapped[Decimal] = mapped_column(MONEY)
    fee: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    fee_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    origin: Mapped[str] = mapped_column(String(16))
    """agent | manual"""

    order_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    signal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="dry_run")
    realized_pnl: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class PortfolioSnapshot(Base):
    """Serie temporal do patrimonio, que alimenta o grafico do dashboard.

    `timestamp` entra na chave primaria pelo mesmo motivo de `Candle`: sem ele,
    o TimescaleDB recusa `create_hypertable('portfolio_snapshots', 'timestamp')`
    e a otimizacao nunca acontece, em silencio. O `id` continua sendo o
    identificador do retrato (nenhuma consulta busca por ele; ver
    `PortfolioSnapshotRepository`), e a PK composta nao afrouxa nada: dois
    retratos com o mesmo `id` teriam de ter tambem o mesmo instante.
    """

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (Index("ix_snapshots_ts", "timestamp"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, primary_key=True
    )
    total_value: Mapped[Decimal] = mapped_column(MONEY)
    cash_value: Mapped[Decimal] = mapped_column(MONEY)
    positions_value: Mapped[Decimal] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    allocations: Mapped[dict] = mapped_column(JSON, default=dict)
    positions: Mapped[list] = mapped_column(JSON, default=list)
    mode: Mapped[str] = mapped_column(String(16), default="dry_run")


class AgentRun(Base):
    """Heartbeat e estado de cada agente ao longo do tempo."""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_agent_ts", "agent", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    """Log append-only de acoes sensiveis.

    Mudanca de limite de risco, pausa de agente e ativacao de LIVE passam
    obrigatoriamente por esta tabela.

    "Append-only" aqui e imposto, nao combinado. Antes era combinado -- o
    repositorio nao expunha update nem delete -- e foi medido que a combinacao
    nao valia nada: `row.action = "ADULTERADO"` num objeto vindo de
    `AuditLogRepository.list()` gravava, e `delete(AuditLog)` esvaziava a tabela
    inteira. Auditoria que o proprio sistema consegue reescrever nao e
    auditoria. Agora ha tres barreiras, cada uma cobrindo o que a outra nao
    alcanca: `_recusar_alteracao_de_auditoria` (ORM), `_recusar_sql_de_auditoria`
    (SQL do Core) e gatilhos no banco (SQL cru, e bancos antigos que ja
    existiam antes deste codigo pegam os gatilhos em `init_db`).

    A promessa acima ja foi furada uma vez, e por um caminho que nenhuma das
    tres via: `INSERT OR REPLACE INTO audit_log (id, ...) VALUES (1, ...)`.
    REPLACE e um DELETE com outro nome -- e um `Insert`, entao a barreira do
    Core (que olhava `Update | Delete`) deixava passar; nao ha objeto ORM, entao
    o `before_flush` nao acordava; e no SQLite a resolucao de conflito REPLACE
    so aciona gatilho de DELETE quando `PRAGMA recursive_triggers` esta ON, e o
    padrao e OFF. As tres foram fechadas para esse caminho: o pragma agora e
    ligado na conexao (`db.session._tune_sqlite`) e a barreira do Core recusa
    tambem `Insert` nao-simples nesta tabela.
    """

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_ts", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class OnChainMetric(Base):
    """Serie diaria de metrica on-chain (hoje: MVRV Z-Score).

    Cacheada porque o dado vem de provedor externo, e nao da exchange: guardar
    permite backtest sem rede e evita depender de um site de terceiros estar no
    ar para o sistema decidir. O passado da serie e imutavel, entao gravar uma
    vez e suficiente.
    """

    __tablename__ = "onchain_metrics"
    __table_args__ = (
        UniqueConstraint("metric", "day", name="uq_onchain_metric_day"),
        Index("ix_onchain_metric_day", "metric", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric: Mapped[str] = mapped_column(String(32))
    day: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    """Indicador normalizado, nao dinheiro -- `Float` serve e mantem a query simples."""

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationConfig(Base):
    """Canais de alerta: liga/desliga e destinatarios, editaveis pela interface.

    Linha unica (`id=1`). Guarda apenas o que NAO e segredo -- toggles, endereco
    de e-mail e numero de WhatsApp. Senha de SMTP e token da Meta continuam
    exclusivamente no `.env`, porque credencial em banco contraria a premissa de
    seguranca do projeto e um backup do banco passaria a vazar acesso.
    """

    __tablename__ = "notification_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    email_to: Mapped[str | None] = mapped_column(String(320), nullable=True)
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    whatsapp_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """Numero no formato internacional, so digitos: 5511999999999."""


class TradingConfig(Base):
    """O que negociar e com que cadencia: configuracao de negocio, nunca do `.env`.

    Linha unica (`id=1`), no mesmo formato de `RiskConfig`: um JSON com os campos
    de `TradingSettings`. Guardar como JSON em vez de uma coluna por campo e
    deliberado -- estes campos mudam junto com a estrategia do produto, e uma
    migracao de schema a cada ajuste de negocio seria atrito sem retorno. A
    validacao mora no modelo Pydantic, que e onde o erro precisa aparecer.
    """

    __tablename__ = "trading_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    values: Mapped[dict] = mapped_column(JSON, default=dict)


class RiskConfig(Base):
    """Limites de risco vigentes, editaveis pela interface sem mexer em codigo.

    Linha unica (`id=1`). O `.env` fornece os valores iniciais; a partir dai esta
    tabela e a fonte da verdade, e toda alteracao gera um `AuditLog`.
    """

    __tablename__ = "risk_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    circuit_breaker_active: Mapped[bool] = mapped_column(Boolean, default=False)
    circuit_breaker_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    circuit_breaker_tripped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AuditoriaImutavelError(RuntimeError):
    """Tentativa de alterar ou apagar linha de uma trilha append-only."""


_AUDITORIA_IMUTAVEL = "audit_log e append-only: UPDATE/DELETE recusado"
_HISTORICO_IMUTAVEL = "risk_events e append-only: UPDATE/DELETE recusado"
#: Sem apostrofo no texto de proposito: a mensagem e interpolada dentro de
#: `RAISE(ABORT, '...')` e de `RAISE EXCEPTION '...'`, e um apostrofo aqui vira
#: erro de sintaxe no CREATE TRIGGER (aconteceu).
_TRADES_IMUTAVEL = (
    "trades e append-only: UPDATE recusado, e DELETE so de lancamento manual (origin manual)"
)
_ORIGEM_MANUAL = "manual"
"""Igual a `domain.enums.TradeOrigin.MANUAL`. Repetido como literal de proposito:
o gatilho vive dentro do banco e nao pode importar Python."""

#: Tabelas que a aplicacao nao pode reescrever, e o motivo de cada uma:
#:
#: - `audit_log`: quem mexeu em limite de risco, quem armou o circuit breaker,
#:   quem ligou LIVE. Nem UPDATE nem DELETE, nunca.
#: - `risk_events`: o MOTIVO de cada sinal aprovado ou rejeitado
#:   (`decision` + `reasons`, gravados por `RiskEventRepository`). A checklist do
#:   projeto pede a trilha de rejeicao append-only, e o motivo mora AQUI, nao em
#:   `audit_log` -- protege-la e o que faz aquela linha da checklist ser verdade.
#: - `trades`: o historico do que de fato aconteceu com dinheiro. UPDATE nunca
#:   (nenhum caminho do codigo altera trade gravado -- conferido); DELETE apenas
#:   de lancamento manual digitado errado, que e a unica remocao que o produto
#:   preve (`TradeRepository.delete` e a rota `DELETE /trades/{id}`). A regra
#:   estava so na rota, ou seja, "append-only por convencao" -- que e exatamente
#:   o arranjo que se provou nao valer nada em `audit_log`. Agora esta no banco.
TABELAS_APPEND_ONLY: dict[str, str] = {
    "audit_log": _AUDITORIA_IMUTAVEL,
    "risk_events": _HISTORICO_IMUTAVEL,
    "trades": _TRADES_IMUTAVEL,
}

#: Gatilhos por dialeto. Sao a ultima barreira, a que pega SQL cru -- e a unica
#: que continua valendo se alguem abrir o banco fora da aplicacao.
#:
#: Estes gatilhos NUNCA sao removidos por codigo nosso -- nem na subida, nem por
#: migracao. Ja foram: a migracao de escala derrubava os gatilhos de `trades` e
#: `risk_events` para reescrever linhas antigas, e isso era uma brecha e nao um
#: detalhe, porque no pysqlite o DDL nao entra na transacao (medido) e uma falha
#: no meio deixava o historico mutavel para sempre. Hoje a escala e imposta na
#: LEITURA (`normalizar_dinheiro_lido`) e nenhum caminho de subida emite
#: `DROP TRIGGER`. `verificar_protecao_append_only` confere na subida que os seis
#: gatilhos existem, e o sistema RECUSA subir se faltar um.
_GATILHOS_APPEND_ONLY: dict[str, tuple[str, ...]] = {
    "sqlite": (
        "CREATE TRIGGER IF NOT EXISTS audit_log_sem_update BEFORE UPDATE ON audit_log "
        f"BEGIN SELECT RAISE(ABORT, '{_AUDITORIA_IMUTAVEL}'); END",
        "CREATE TRIGGER IF NOT EXISTS audit_log_sem_delete BEFORE DELETE ON audit_log "
        f"BEGIN SELECT RAISE(ABORT, '{_AUDITORIA_IMUTAVEL}'); END",
    ),
    "postgresql": (
        "CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        f"RAISE EXCEPTION '{_AUDITORIA_IMUTAVEL}'; "
        "END $$",
        "DROP TRIGGER IF EXISTS audit_log_sem_update_delete ON audit_log",
        "CREATE TRIGGER audit_log_sem_update_delete BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()",
        "DROP TRIGGER IF EXISTS audit_log_sem_truncate ON audit_log",
        "CREATE TRIGGER audit_log_sem_truncate BEFORE TRUNCATE ON audit_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only()",
    ),
}

#: Gatilhos da trilha de decisao (`risk_events`).
_GATILHOS_RISK_EVENTS: dict[str, tuple[str, ...]] = {
    "sqlite": (
        "CREATE TRIGGER IF NOT EXISTS risk_events_sem_update BEFORE UPDATE ON risk_events "
        f"BEGIN SELECT RAISE(ABORT, '{_HISTORICO_IMUTAVEL}'); END",
        "CREATE TRIGGER IF NOT EXISTS risk_events_sem_delete BEFORE DELETE ON risk_events "
        f"BEGIN SELECT RAISE(ABORT, '{_HISTORICO_IMUTAVEL}'); END",
    ),
    "postgresql": (
        "CREATE OR REPLACE FUNCTION historico_append_only() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        f"RAISE EXCEPTION '{_HISTORICO_IMUTAVEL}'; "
        "END $$",
        "DROP TRIGGER IF EXISTS risk_events_sem_update_delete ON risk_events",
        "CREATE TRIGGER risk_events_sem_update_delete BEFORE UPDATE OR DELETE ON risk_events "
        "FOR EACH ROW EXECUTE FUNCTION historico_append_only()",
        "DROP TRIGGER IF EXISTS risk_events_sem_truncate ON risk_events",
        "CREATE TRIGGER risk_events_sem_truncate BEFORE TRUNCATE ON risk_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION historico_append_only()",
    ),
}

#: Gatilhos do historico (`trades`): UPDATE nunca, DELETE so de lancamento manual.
_GATILHOS_TRADES: dict[str, tuple[str, ...]] = {
    "sqlite": (
        "CREATE TRIGGER IF NOT EXISTS trades_sem_update BEFORE UPDATE ON trades "
        f"BEGIN SELECT RAISE(ABORT, '{_TRADES_IMUTAVEL}'); END",
        "CREATE TRIGGER IF NOT EXISTS trades_sem_delete_de_agente BEFORE DELETE ON trades "
        f"WHEN OLD.origin <> '{_ORIGEM_MANUAL}' "
        f"BEGIN SELECT RAISE(ABORT, '{_TRADES_IMUTAVEL}'); END",
    ),
    "postgresql": (
        "CREATE OR REPLACE FUNCTION trades_append_only() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        f"RAISE EXCEPTION '{_TRADES_IMUTAVEL}'; "
        "END $$",
        "CREATE OR REPLACE FUNCTION trades_delete_apenas_manual() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        f"IF OLD.origin <> '{_ORIGEM_MANUAL}' THEN RAISE EXCEPTION '{_TRADES_IMUTAVEL}'; END IF; "
        "RETURN OLD; END $$",
        "DROP TRIGGER IF EXISTS trades_sem_update ON trades",
        "CREATE TRIGGER trades_sem_update BEFORE UPDATE ON trades "
        "FOR EACH ROW EXECUTE FUNCTION trades_append_only()",
        "DROP TRIGGER IF EXISTS trades_sem_delete_de_agente ON trades",
        "CREATE TRIGGER trades_sem_delete_de_agente BEFORE DELETE ON trades "
        "FOR EACH ROW EXECUTE FUNCTION trades_delete_apenas_manual()",
        "DROP TRIGGER IF EXISTS trades_sem_truncate ON trades",
        "CREATE TRIGGER trades_sem_truncate BEFORE TRUNCATE ON trades "
        "FOR EACH STATEMENT EXECUTE FUNCTION trades_append_only()",
    ),
}

#: Por tabela, e nao numa lista so, porque `create_all` cria uma tabela de cada
#: vez: o `after_create` de `audit_log` nao pode tentar criar gatilho em `trades`,
#: que talvez ainda nao exista.
_GATILHOS_POR_TABELA: dict[str, dict[str, tuple[str, ...]]] = {
    "audit_log": _GATILHOS_APPEND_ONLY,
    "risk_events": _GATILHOS_RISK_EVENTS,
    "trades": _GATILHOS_TRADES,
}

#: Os gatilhos que TEM de existir depois da subida, por dialeto. E contra esta
#: lista que `verificar_protecao_append_only` mede o banco de verdade.
GATILHOS_ESPERADOS: dict[str, tuple[str, ...]] = {
    "sqlite": (
        "audit_log_sem_update",
        "audit_log_sem_delete",
        "risk_events_sem_update",
        "risk_events_sem_delete",
        "trades_sem_update",
        "trades_sem_delete_de_agente",
    ),
    "postgresql": (
        "audit_log_sem_update_delete",
        "audit_log_sem_truncate",
        "risk_events_sem_update_delete",
        "risk_events_sem_truncate",
        "trades_sem_update",
        "trades_sem_delete_de_agente",
        "trades_sem_truncate",
    ),
}

#: Como listar os gatilhos existentes, por dialeto.
_SQL_LISTAR_GATILHOS: dict[str, str] = {
    "sqlite": "SELECT name FROM sqlite_master WHERE type = 'trigger'",
    "postgresql": (
        "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE NOT t.tgisinternal AND c.relname IN ('audit_log', 'risk_events', 'trades')"
    ),
}

#: Marcador que autoriza um comando a mexer na propria protecao. So os caminhos
#: deste modulo o usam; ver `_recusar_sql_de_auditoria`.
_OPCAO_INTERNA = {"crypto_traders_protecao": True}

#: A separacao que so o PostgreSQL consegue impor de verdade: no SQLite qualquer
#: conexao com o arquivo e dona do schema, entao a barreira do processo e o
#: maximo possivel. No Postgres o dono do banco deve rodar isto uma vez, e ai
#: nem um `DROP TRIGGER` vindo da aplicacao funciona -- o servidor recusa.
SQL_REVOGACAO_POSTGRES: tuple[str, ...] = (
    "REVOKE TRIGGER ON audit_log, risk_events, trades FROM CURRENT_USER",
    "REVOKE CREATE ON SCHEMA public FROM CURRENT_USER",
)


class ProtecaoAusenteError(RuntimeError):
    """Falta gatilho de trilha append-only no banco. O sistema nao sobe assim."""


def _aplicar_gatilhos_de(connection: Connection, tabela: str) -> tuple[str, ...]:
    comandos = _GATILHOS_POR_TABELA[tabela].get(connection.dialect.name, ())
    for comando in comandos:
        connection.execute(text(comando).execution_options(**_OPCAO_INTERNA))
    return comandos


def aplicar_protecao_append_only(connection: Connection) -> tuple[str, ...]:
    """Cria os gatilhos que recusam UPDATE/DELETE nas trilhas append-only.

    Idempotente de proposito: `init_db` chama isto a cada subida, para que um
    banco criado antes destes gatilhos existirem tambem passe a ser protegido.
    Devolve os comandos aplicados, para o teste poder olhar.
    """
    aplicados: tuple[str, ...] = ()
    for tabela in _GATILHOS_POR_TABELA:
        aplicados += _aplicar_gatilhos_de(connection, tabela)
    return aplicados


def verificar_protecao_append_only(connection: Connection) -> tuple[str, ...]:
    """Confere no BANCO que os gatilhos existem. Falta um, o sistema nao sobe.

    Aplicar a protecao e uma coisa; ter certeza de que ela esta la e outra --
    e a diferenca entre as duas foi medida deste projeto: o banco do ensaio em
    dry_run rodou com ZERO gatilhos sem que nada reclamasse, porque ninguem
    nunca perguntou ao banco. `CREATE TRIGGER` pode ter sido revogado, o gatilho
    pode ter sido derrubado por fora, ou uma subida anterior pode ter morrido no
    meio (a maquina suspendendo ja matou processo deste projeto sem traceback).

    Nao ha `try/except` e nao ha WARNING: WARNING e o que ninguem le. Trilha de
    auditoria mutavel nao e degradacao aceitavel, e a regra do projeto e que na
    duvida o sistema NAO opera. Devolve os gatilhos encontrados, para o teste
    poder olhar.
    """
    dialeto = connection.dialect.name
    esperados = GATILHOS_ESPERADOS.get(dialeto)
    consulta = _SQL_LISTAR_GATILHOS.get(dialeto)
    if esperados is None or consulta is None:  # pragma: no cover - dialeto sem suporte
        raise ProtecaoAusenteError(
            f"dialeto '{dialeto}' nao sabe impor trilha append-only; o sistema nao sobe nele"
        )

    existentes = {
        linha[0]
        for linha in connection.execute(
            text(consulta).execution_options(**_OPCAO_INTERNA)
        ).all()
    }
    faltando = [nome for nome in esperados if nome not in existentes]
    if faltando:
        raise ProtecaoAusenteError(
            "trilha append-only desprotegida: faltam os gatilhos "
            f"{faltando} em {dialeto}. O sistema nao sobe com auditoria mutavel."
        )
    return tuple(sorted(existentes))


def avisar_se_a_aplicacao_pode_derrubar_gatilho(connection: Connection) -> None:
    """No Postgres da para perguntar ao servidor quem pode mexer nos gatilhos.

    Enquanto o usuario da aplicacao tiver o privilegio TRIGGER, ele consegue
    derrubar a propria protecao -- e ai a ultima barreira e removivel por quem
    ela deveria conter. Nao derruba a subida (revogar exige ser dono do banco, e
    quem sobe o processo em geral nao e), mas sai no log com o SQL exato a
    rodar.

    Roda em conexao PROPRIA, fora da transacao que cria o schema, e nao por
    zelo: no PostgreSQL um comando que falha aborta a transacao inteira, e o
    COMMIT seguinte vira ROLLBACK. Esse defeito exato ja levou o schema
    recem-criado embora aqui uma vez, pelo `create_hypertable`. Uma sonda de
    diagnostico nao pode ter poder de apagar o banco.
    """
    if connection.dialect.name != "postgresql":
        return
    try:
        pode = connection.execute(
            text(
                "SELECT bool_or(has_table_privilege(current_user, t, 'TRIGGER')) "
                "FROM unnest(ARRAY['audit_log', 'risk_events', 'trades']) AS t"
            ).execution_options(**_OPCAO_INTERNA)
        ).scalar()
    except Exception as exc:  # pragma: no cover - servidor sem has_table_privilege
        log.warning("db.privilegio_indeterminado", error=str(exc))
        return
    if pode:
        log.error(
            "db.aplicacao_pode_derrubar_gatilho",
            detail=(
                "o usuario da aplicacao tem privilegio TRIGGER nas trilhas append-only; "
                "rode como dono do banco: " + "; ".join(SQL_REVOGACAO_POSTGRES)
            ),
        )


def hypertable_possivel(tabela: str, coluna_de_tempo: str) -> list[str]:
    """Os indices unicos que impediriam `create_hypertable`. Vazio = pode.

    O TimescaleDB exige que TODO indice unico da tabela contenha a coluna de
    particao; senao ele recusa com "cannot create a unique index without the
    column ... (used in partitioning)". Isso ja aconteceu aqui sem ninguem ver:
    `candles` e `portfolio_snapshots` tinham PK que nao continha a coluna de
    tempo, `create_hypertable` era recusado nas duas, o `except` engolia o erro
    e o sistema subia em tabela comum -- para sempre, com um WARNING que ninguem
    le. As chaves primarias foram corrigidas; esta funcao existe para que a
    proxima pessoa que adicionar um indice unico descubra pelo teste, e nao pelo
    banco de producao.
    """
    tabela_orm = Base.metadata.tables[tabela]
    problemas: list[str] = []
    pk = [coluna.name for coluna in tabela_orm.primary_key.columns]
    if coluna_de_tempo not in pk:
        problemas.append(f"PRIMARY KEY {pk}")
    for restricao in tabela_orm.constraints:
        if isinstance(restricao, UniqueConstraint):
            nomes = [coluna.name for coluna in restricao.columns]
            if coluna_de_tempo not in nomes:
                problemas.append(f"UNIQUE {nomes}")
    for indice in tabela_orm.indexes:
        nomes = [coluna.name for coluna in indice.columns]
        if indice.unique and coluna_de_tempo not in nomes:
            problemas.append(f"INDEX UNIQUE {nomes}")
    return problemas


@event.listens_for(AuditLog.__table__, "after_create")
def _proteger_audit_log_ao_criar(target: Any, connection: Connection, **kw: Any) -> None:
    _aplicar_gatilhos_de(connection, "audit_log")


@event.listens_for(RiskEvent.__table__, "after_create")
def _proteger_risk_events_ao_criar(target: Any, connection: Connection, **kw: Any) -> None:
    _aplicar_gatilhos_de(connection, "risk_events")


@event.listens_for(Trade.__table__, "after_create")
def _proteger_trades_ao_criar(target: Any, connection: Connection, **kw: Any) -> None:
    _aplicar_gatilhos_de(connection, "trades")


def _nome_da_tabela(clauseelement: Any) -> str | None:
    alvo = getattr(clauseelement, "table", None)
    return getattr(alvo, "name", None)


# ---------------------------------------------------------------------------
# Barreira do BANCO: o autorizador do SQLite.
#
# Aqui esta a resposta a uma medicao que derrubou o desenho anterior. A unica
# barreira contra a aplicacao desligar a propria auditoria era uma lista de
# palavras proibidas casada por expressao regular sobre o TEXTO do comando, e
# foi medido que ela cai de tres jeitos diferentes sem esforco: comentario no
# meio (`DROP/**/TRIGGER x`, `DROP--x\nTRIGGER x`, que sao espaco em branco para
# o parser e nao para a regex) e pragma qualificado por schema
# (`PRAGMA main.writable_schema=ON`, que apaga o gatilho sem escrever a palavra
# TRIGGER em lugar nenhum).
#
# Perseguir grafia com regex e uma corrida que a regex perde sempre: a proxima
# grafia sempre existe. A saida nao e mais regex, e sim parar de ler o texto.
# O SQLite tem um autorizador (`sqlite3_set_authorizer`): ele e consultado ao
# PREPARAR cada comando, depois do parser, e recebe o que o comando FAZ -- acao,
# objeto, valor. Comentario, espaco e qualificacao de schema desapareceram no
# parser, entao nao ha grafia a adivinhar.
#
# O QUE ESTA CAMADA NAO RESOLVE, e que precisa ficar escrito porque documentacao
# errada sobre protecao e pior que ausencia de protecao -- foi lendo "stop-loss
# configurado" que o stop inerte deste projeto sobreviveu tanto tempo:
#
# contra codigo hostil dentro DESTE processo, nao ha barreira do lado do SQLite.
# Medido, em uma linha e sem SQL nenhum:
#
#     conexao.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
#     -> o `UPDATE` em tabela protegida passa e a linha muda
#
# Nao ha comando para casar, nao ha acao para o autorizador ver: `setconfig` nao
# e SQL. O mesmo vale para `set_authorizer(None)` e para abrir uma conexao
# propria com o arquivo. Ou seja: trilha append-only a prova de codigo hostil no
# proprio processo NAO EXISTE aqui, e nao existe por escolha de arquitetura --
# ela exigiria separacao de privilegio (usuario de banco sem DDL, que so o
# Postgres oferece via `SQL_REVOGACAO_POSTGRES`, ou append remoto fora deste
# processo). Enquanto o banco for um arquivo SQLite aberto por este processo,
# quem roda dentro dele e dono do schema.
#
# O que o autorizador fecha, e fecha de verdade, e o que ate aqui estava aberto:
# SQL cru vindo da aplicacao por acidente ou por caminho esquecido, em qualquer
# grafia -- `DROP TRIGGER` e os pragmas de sabotagem.
# ---------------------------------------------------------------------------

#: Onde o autorizador deixa o motivo da recusa para
#: `_traduzir_negacao_do_autorizador` achar. Vive no `info` do registro da
#: conexao, e nao num thread-local, porque o callback roda na thread do worker
#: do aiosqlite e a excecao sobe na thread do chamador: thread-local nao ligaria
#: as duas pontas.
_CHAVE_NEGACAO = "crypto_traders_negacao_do_autorizador"

#: Valores que DESLIGAM um pragma. Ligar de novo e inofensivo.
_VALORES_QUE_DESLIGAM = frozenset({"off", "0", "false", "no"})

#: Pragmas que a aplicacao nao tem motivo nenhum para tocar, e o que cada um
#: abre se for tocado.
_PRAGMAS_PROIBIDOS: dict[str, str] = {
    "writable_schema": "permite apagar o gatilho editando sqlite_master direto",
    "ignore_check_constraints": "desliga as restricoes CHECK das tabelas",
}

#: Onde o `before_cursor_execute` avisa o autorizador que o comando em curso e
#: do caminho autorizado (`_OPCAO_INTERNA`). O autorizador roda dentro do parser
#: e nao ve `execution_options`, entao quem as ve deixa o recado aqui.
#:
#: Isto e uma concessao consciente, e nao um descuido: o marcador e opt-in e
#: qualquer codigo do processo pode escrever `execution_options(
#: crypto_traders_protecao=True)`. Ou seja, o autorizador nao e mais forte que o
#: marcador contra quem conhece o marcador -- e nem precisa ser: contra codigo
#: hostil no processo a barreira ja cai por `setconfig` (ver o bloco acima). Ele
#: e mais forte no que importa: nao depende do TEXTO do comando, e nenhum dos
#: ataques medidos usa o marcador.
_CHAVE_INTERNA = "crypto_traders_comando_interno"


def motivo_para_negar_no_sqlite(acao: int, arg1: Any, arg2: Any) -> str | None:
    """Politica do autorizador: o motivo da recusa, ou `None` para deixar passar.

    A diferenca em relacao a `_recusar_sql_cru_contra_a_protecao` e a unica que
    importa: aqui nao ha texto de comando nenhum. Quem chama e o SQLite, depois
    de parsear, dizendo o que o comando faz.

    Medido: `DROP TRIGGER audit_log_sem_update`, `DROP/**/TRIGGER ...` e
    `DROP--x\\nTRIGGER ...` chegam os TRES como
    `(SQLITE_DROP_TRIGGER, 'audit_log_sem_update', 'audit_log')`; e
    `PRAGMA main.writable_schema=ON` chega como
    `(SQLITE_PRAGMA, 'writable_schema', 'ON')`, identico a `PRAGMA
    writable_schema=ON`. E isso que faz a defesa nao depender de grafia.
    """
    if acao in (sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_TRIGGER):
        # Pelo NOME do gatilho, e nao "todo DROP TRIGGER": um `DROP TABLE` de
        # tabela sem protecao derruba em cascata os gatilhos dela e chega aqui
        # com esta mesma acao (medido). Negar tudo quebraria isso sem proteger
        # nada -- os unicos gatilhos deste schema sao os da protecao, e o
        # `DROP TABLE` das tabelas protegidas ja e negado logo abaixo.
        if arg1 in GATILHOS_ESPERADOS["sqlite"]:
            # Mensagem que NAO comeca pelo nome do comando, de proposito: existe
            # uma guarda estrutural que varre este pacote procurando o comando
            # de derrubar gatilho escrito como literal de string, para garantir
            # que nenhum caminho de subida o emita. A guarda esta certa, e uma
            # mensagem de erro nao pode disfarcar-se de comando para ela.
            return (
                f"recusado o DROP do gatilho {arg1}: a aplicacao nao desliga a propria "
                "trilha append-only"
            )
        return None
    if acao == sqlite3.SQLITE_PRAGMA:
        nome = str(arg1 or "").lower()
        risco = _PRAGMAS_PROIBIDOS.get(nome)
        if risco is not None:
            return f"PRAGMA {nome} recusado: {risco}"
        if nome == "recursive_triggers" and str(arg2 or "").lower() in _VALORES_QUE_DESLIGAM:
            return (
                "PRAGMA recursive_triggers=OFF recusado: com ele desligado, "
                "INSERT OR REPLACE reescreve linha de auditoria por baixo do gatilho "
                "audit_log_sem_delete"
            )
        return None
    # Nao existe regra sobre escrita em `sqlite_master`, e isso foi decidido
    # depois de medir, nao por esquecimento. O passo do meio do ataque
    # (`DELETE FROM sqlite_master WHERE name = 'audit_log_sem_update'`) chega
    # aqui como `SQLITE_DELETE` em `sqlite_master` -- mas o MESMO par chega
    # quando o SQLite executa um `DROP TABLE` legitimo de tabela qualquer, e
    # `SQLITE_INSERT`/`SQLITE_UPDATE` em `sqlite_master` sao emitidos por
    # `CREATE TABLE`, `CREATE INDEX` e `CREATE TRIGGER` (medido nos quatro).
    # Uma regra ali nao distingue ataque de DDL comum: ela quebraria a propria
    # subida. E nao e preciso, porque o ataque so funciona com
    # `writable_schema` ligado -- que e negado acima, em qualquer grafia, porque
    # o parser resolve o nome antes de nos consultar -- e sem ele o proprio
    # SQLite recusa: `table sqlite_master may not be modified` (medido).
    if acao in (sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_TEMP_TABLE):
        return TABELAS_APPEND_ONLY.get(str(arg1 or "").lower())
    if acao == sqlite3.SQLITE_ALTER_TABLE:
        # Nesta acao (e so nela) `arg1` e o schema e `arg2` e a tabela.
        return TABELAS_APPEND_ONLY.get(str(arg2 or "").lower())
    return None


def instalar_autorizador_sqlite(dbapi_connection: Any, record: Any) -> None:
    """Liga a politica acima na conexao. Chamado no evento `connect` do engine.

    Cada conexao precisa do seu: o autorizador e por conexao, nao por banco.
    Por isso ele mora no `connect`, do lado de `_tune_sqlite`, e nao numa rotina
    de subida -- conexao nova do pool tambem tem de nascer autorizada.
    """

    def autorizador(acao: int, arg1: Any, arg2: Any, _dbname: Any, _gatilho: Any) -> int:
        if record.info.get(_CHAVE_INTERNA):
            return sqlite3.SQLITE_OK
        motivo = motivo_para_negar_no_sqlite(acao, arg1, arg2)
        if motivo is None:
            return sqlite3.SQLITE_OK
        record.info[_CHAVE_NEGACAO] = motivo
        return sqlite3.SQLITE_DENY

    driver = getattr(dbapi_connection, "driver_connection", None)
    if driver is not None and hasattr(dbapi_connection, "await_"):
        # aiosqlite: a conexao crua do sqlite3 vive na thread do worker, e mexer
        # nela desta thread levanta `ProgrammingError: SQLite objects created in
        # a thread can only be used in that same thread` (medido). `await_`
        # entrega a chamada na thread certa, que e a mesma ponte que o
        # `cursor.execute` dos pragmas usa.
        dbapi_connection.await_(driver.set_authorizer(autorizador))
    else:  # pragma: no cover - pysqlite sincrono; o projeto usa aiosqlite
        dbapi_connection.set_authorizer(autorizador)


@event.listens_for(Engine, "handle_error")
def _traduzir_negacao_do_autorizador(context: Any) -> None:
    """`not authorized` do driver vira `AuditoriaImutavelError` com o motivo.

    Sem esta traducao quem chama receberia `DatabaseError: not authorized`, que
    nao diz nada sobre trilha append-only. Mensagem que nao explica e o comeco de
    todo defeito silencioso deste projeto: o `.env` sobreposto e o watchdog
    reiniciando agente ocioso sobreviveram meses atras de mensagens assim.
    """
    conexao = context.connection
    if conexao is None:  # pragma: no cover - erro antes de haver conexao
        return
    try:
        motivo = conexao.info.pop(_CHAVE_NEGACAO, None)
    except Exception:  # pragma: no cover - conexao invalidada nao tem info
        return
    if motivo is None:
        return
    raise AuditoriaImutavelError(motivo) from context.original_exception


# ---------------------------------------------------------------------------
# Barreira do TEXTO: lista de palavras proibidas. Segunda linha, nao a primeira.
# ---------------------------------------------------------------------------

#: SQL cru que mexe na propria protecao, e nao no dado. Nenhuma linha do produto
#: precisa de qualquer um destes; quem precisa e quem quer apagar rastro.
#:
#: O qualificador de schema (`main.`, `temp.`) esta nos pragmas porque ele foi
#: medido passando por aqui; e o comentario e removido antes da busca, por
#: `_COMENTARIO_SQL`. As duas coisas sao a GRAMATICA do SQL, fechada e conhecida,
#: e nao mais uma grafia adivinhada -- mas fechar a gramatica nao transforma
#: lista de palavras em barreira: quem decide no SQLite e o autorizador acima.
_PADROES_DE_SABOTAGEM: tuple[tuple[str, str], ...] = (
    (r"\bdrop\s+trigger\b", "DROP TRIGGER"),
    (r"\balter\s+trigger\b", "ALTER TRIGGER"),
    (r"\bpragma\s+(?:\w+\s*\.\s*)?writable_schema\b", "PRAGMA writable_schema"),
    (
        r"\bpragma\s+(?:\w+\s*\.\s*)?recursive_triggers\s*=\s*(?:off|0|false|no)\b",
        "PRAGMA recursive_triggers=OFF",
    ),
    (
        r"\bpragma\s+(?:\w+\s*\.\s*)?ignore_check_constraints\b",
        "PRAGMA ignore_check_constraints",
    ),
)

#: Comentario SQL: `-- ate o fim da linha` e `/* bloco */`. Os dois sao espaco em
#: branco para o parser e nao para a regex, e era por eles que a lista de
#: palavras passava (medido: `DROP/**/TRIGGER x` nao casa com
#: `\bdrop\s+trigger\b`). Removido antes da busca.
_COMENTARIO_SQL = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)

_SABOTAGEM = re.compile(
    "|".join(f"(?P<g{i}>{padrao})" for i, (padrao, _) in enumerate(_PADROES_DE_SABOTAGEM))
)

#: `DROP TABLE audit_log` e `ALTER TABLE trades` levam a protecao junto com a
#: tabela; sao tratados a parte porque so importam quando citam uma trilha.
_DDL_SOBRE_TABELA = re.compile(
    r"\b(drop|alter|truncate)\s+(table\s+)?(if\s+exists\s+)?(?P<alvo>[\"'`\[\w.]+)"
)


#: Pre-filtro barato: so vale a pena olhar de perto o SQL que tem uma destas
#: palavras. Roda em todo comando do processo, entao nao pode custar caro.
_PALAVRAS_SUSPEITAS = ("trigger", "pragma", "drop", "alter", "truncate")


def _recusar_sql_cru_contra_a_protecao(sql: str, execution_options: Any) -> None:
    """Lista de palavras proibidas sobre o texto do comando. Nao e a barreira.

    O que ela e, dito sem enfeite: um casamento de expressao regular contra o
    texto do SQL. Isso para as grafias que quem escreveu imaginou, e a rodada 4
    do critico mediu tres que ela nao imaginava -- comentario de bloco colado,
    comentario de linha e pragma qualificado por schema. Os dois primeiros estao
    fechados agora (comentario e removido antes da busca) e o terceiro tambem
    (`main.`/`temp.` entrou nos padroes), mas isso NAO promove esta funcao a
    barreira: a proxima grafia sempre existe, e prometer o contrario aqui seria
    a mesma mentira do "stop-loss configurado" que manteve o stop inerte vivo
    neste projeto.

    Quem decide, no SQLite, e `motivo_para_negar_no_sqlite`, consultado pelo
    autorizador do proprio banco com a acao JA PARSEADA -- sem texto, sem grafia.
    Esta funcao continua por dois motivos honestos:

    1. **PostgreSQL nao tem autorizador.** La esta lista e tudo o que existe no
       processo, e ela e explicitamente best-effort: a defesa de verdade no
       Postgres e privilegio, `SQL_REVOGACAO_POSTGRES`, conferido na subida por
       `avisar_se_a_aplicacao_pode_derrubar_gatilho`.
    2. **Mensagem.** Ela recusa antes de o comando sair do processo e nomeia o
       comando; o autorizador recusa depois, e o driver so diz "not authorized"
       (traduzido por `_traduzir_negacao_do_autorizador`).

    E o que nenhuma das duas camadas e: barreira contra quem abre o arquivo do
    SQLite por fora, ou contra codigo hostil no proprio processo, que pode
    limpar o autorizador e refazer a conexao. Para isso e preciso separacao de
    privilegio, que e arquitetura e nao listener.
    """
    if (execution_options or {}).get("crypto_traders_protecao"):
        return
    minusculo = sql.lower()
    if not any(palavra in minusculo for palavra in _PALAVRAS_SUSPEITAS):
        return
    # Comentario fora ANTES de procurar. O pre-filtro acima roda no texto cru de
    # proposito: comentario nao esconde as LETRAS da palavra (`DROP/**/TRIGGER`
    # contem "drop" e "trigger"), so o espaco entre elas -- entao o filtro barato
    # continua valendo e a limpeza mais cara so roda no comando ja suspeito.
    limpo = _COMENTARIO_SQL.sub(" ", minusculo)
    encontrado = _SABOTAGEM.search(limpo)
    if encontrado is not None:
        indice = int(encontrado.lastgroup[1:]) if encontrado.lastgroup else 0
        raise AuditoriaImutavelError(
            f"{_PADROES_DE_SABOTAGEM[indice][1]} recusado: a aplicacao nao desliga a "
            "propria trilha append-only"
        )
    for ocorrencia in _DDL_SOBRE_TABELA.finditer(limpo):
        alvo = ocorrencia.group("alvo").strip("\"'`[]").split(".")[-1]
        motivo = TABELAS_APPEND_ONLY.get(alvo)
        if motivo is not None:
            raise AuditoriaImutavelError(motivo)


def _insert_pode_sobrescrever(clauseelement: Insert) -> bool:
    """O INSERT carrega resolucao de conflito, ou seja, pode apagar linha?

    `INSERT OR REPLACE` (prefixo, SQLite) e `ON CONFLICT DO UPDATE`
    (`_post_values_clause`, SQLite e Postgres) reescrevem linha existente. Um
    deles reescreveu `audit_log` id=1 na medicao, sem erro nenhum, pelo Core:
    `insert(AuditLog).prefix_with("OR REPLACE").values(id=1, ...)`.

    Qualquer INSERT nao-simples nestas tabelas e recusado, inclusive
    `OR IGNORE`/`DO NOTHING`, que sozinhos nao apagam nada: nenhuma linha do
    projeto precisa disso numa trilha append-only, e "recusa o que nao reconhece"
    e mais barato de manter correto que uma lista de prefixos permitidos.
    """
    if getattr(clauseelement, "_prefixes", ()):
        return True
    return getattr(clauseelement, "_post_values_clause", None) is not None


@event.listens_for(Session, "before_flush")
def _recusar_alteracao_de_auditoria(session: Session, flush_context: Any, instances: Any) -> None:
    """Barreira do ORM: objeto de trilha append-only sujo ou marcado para exclusao.

    Registrada na classe `Session`, portanto vale para toda sessao do processo,
    inclusive a que a `AsyncSession` embrulha -- nao ha como criar uma sessao
    "sem essa protecao" por engano.
    """
    for instancia in session.deleted:
        if isinstance(instancia, AuditLog):
            raise AuditoriaImutavelError(_AUDITORIA_IMUTAVEL)
        if isinstance(instancia, RiskEvent):
            raise AuditoriaImutavelError(_HISTORICO_IMUTAVEL)
        # A unica remocao prevista no produto: lancamento manual digitado errado.
        if isinstance(instancia, Trade) and instancia.origin != _ORIGEM_MANUAL:
            raise AuditoriaImutavelError(_TRADES_IMUTAVEL)
    for instancia in session.dirty:
        if not session.is_modified(instancia):
            continue
        if isinstance(instancia, AuditLog):
            raise AuditoriaImutavelError(_AUDITORIA_IMUTAVEL)
        if isinstance(instancia, RiskEvent):
            raise AuditoriaImutavelError(_HISTORICO_IMUTAVEL)
        if isinstance(instancia, Trade):
            raise AuditoriaImutavelError(_TRADES_IMUTAVEL)


@event.listens_for(Engine, "before_execute")
def _recusar_sql_de_auditoria(
    conn: Connection,
    clauseelement: Any,
    multiparams: Any,
    params: Any,
    execution_options: Any,
) -> None:
    """Barreira do Core: SQL em massa, que nao passa pelo flush.

    `await session.execute(delete(AuditLog))` apagou a tabela inteira na medicao
    e nao acorda o `before_flush`, porque nao ha objeto carregado nenhum. O mesmo
    valia para `insert(AuditLog).prefix_with("OR REPLACE")`, que e um `Insert` e
    passava por baixo de uma checagem que so olhava `Update | Delete`.
    """
    tabela = _nome_da_tabela(clauseelement) or ""
    motivo = TABELAS_APPEND_ONLY.get(tabela)
    if motivo is None:
        return

    if isinstance(clauseelement, Delete):
        # `trades` fica de FORA desta checagem, e nao por esquecimento: o DELETE
        # de lancamento manual e permitido, e quem sabe se a linha e manual e o
        # `origin` dela -- que so o banco ve, linha por linha. Barrar aqui
        # pegaria tambem o DELETE que o proprio ORM emite ao apagar o lancamento
        # manual (medido: o unit of work chega neste listener como um `Delete`
        # comum, indistinguivel de um `delete()` de aplicacao), matando a
        # funcionalidade. Quem barra DELETE em `trades` e o gatilho
        # `trades_sem_delete_de_agente`, que testa `OLD.origin`.
        if tabela != Trade.__tablename__:
            raise AuditoriaImutavelError(motivo)
    elif isinstance(clauseelement, Update) or (
        isinstance(clauseelement, Insert) and _insert_pode_sobrescrever(clauseelement)
    ):
        raise AuditoriaImutavelError(motivo)


#: Colunas de dinheiro por tabela, descobertas do proprio schema. Modelo novo com
#: coluna `Money` entra aqui sem ninguem precisar lembrar.
def _colunas_de_dinheiro() -> dict[str, tuple[str, ...]]:
    return {
        tabela.name: tuple(
            coluna.name for coluna in tabela.columns if isinstance(coluna.type, Money)
        )
        for tabela in Base.metadata.tables.values()
        if any(isinstance(coluna.type, Money) for coluna in tabela.columns)
    }


def relatar_dinheiro_fora_de_escala(connection: Connection) -> list[str]:
    """Diagnostico: aponta o dinheiro JA gravado fora da escala 18. Nao escreve.

    Esta funcao ja foi uma migracao -- ela reescrevia as linhas antigas na
    subida -- e o que ela virou e a resposta a uma brecha grave. Para reescrever
    `trades` era preciso derrubar `trades_sem_update`, e no pysqlite/aiosqlite o
    DDL nao entra na transacao: medido, um `DROP TRIGGER` emitido antes do
    primeiro DML da conexao e autocommitado e SOBREVIVE ao rollback, enquanto o
    `UPDATE` do mesmo bloco volta atras. Ou seja, uma falha no meio da subida
    (ou a maquina suspendendo, que ja matou processo deste projeto sem
    traceback) deixava `trades` e `risk_events` mutaveis -- e como o `UPDATE`
    voltava atras, o valor torto continuava la e CADA subida seguinte derrubava
    os gatilhos de novo.

    A escala e imposta hoje na leitura, por `normalizar_dinheiro_lido`, que
    resolve o problema de verdade (os dois dialetos devolvem o mesmo numero) sem
    reescrever historico e sem desligar protecao nenhuma. Aqui sobra o
    diagnostico: contar e nomear o que esta gravado fora da escala, para o fato
    continuar visivel em vez de virar folclore.

    So faz sentido no SQLite: no PostgreSQL a coluna e `NUMERIC(38, 18)` e o
    proprio banco impoe a escala em toda gravacao.

    Devolve `tabela.coluna[rowid]: gravado -> lido`, vazio no caso normal.
    """
    if connection.dialect.name != "sqlite":
        return []

    achados: list[str] = []
    for tabela, colunas in _colunas_de_dinheiro().items():
        for coluna in colunas:
            # Filtra no SQL para nao arrastar 17 mil candles para o Python a cada
            # subida. `Money` grava sem notacao cientifica, entao contar os
            # digitos depois do ponto e exato; `typeof <> 'text'` pega linha
            # anterior ao tipo `Money`, que estava gravada como REAL.
            suspeitas = connection.execute(
                text(
                    f"SELECT rowid, {coluna} FROM {tabela} WHERE {coluna} IS NOT NULL AND ("
                    f"typeof({coluna}) <> 'text' OR ("
                    f"instr({coluna}, '.') > 0 "
                    f"AND length({coluna}) - instr({coluna}, '.') > {MONEY_SCALE}))"
                )
            ).all()
            for rowid, bruto in suspeitas:
                lido = format(normalizar_dinheiro_lido(bruto), "f")
                if lido == str(bruto):
                    continue
                achados.append(f"{tabela}.{coluna}[{rowid}]: {bruto} -> {lido}")

    if achados:
        log.warning(
            "db.dinheiro_gravado_fora_da_escala",
            total=len(achados),
            exemplos=achados[:10],
            detail=(
                f"valores gravados com mais de {MONEY_SCALE} casas; a leitura os "
                "normaliza, e o historico gravado NAO e reescrito de proposito"
            ),
        )
    return achados


@event.listens_for(Engine, "before_cursor_execute")
def _recusar_sabotagem_da_protecao(
    conn: Connection,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """Barreira do SQL cru, no ultimo ponto antes do driver.

    Fica em `before_cursor_execute`, e nao em `before_execute`, de proposito:
    `before_execute` nao ve `exec_driver_sql`, que e API publica do SQLAlchemy e
    seria um desvio inteiro em volta da barreira. Aqui passa TUDO -- `text()`,
    `exec_driver_sql`, SQL compilado do Core -- e o comando ja esta em texto.

    O que ela fecha e o caminho de DENTRO: ate a rodada 2 a propria sessao da
    aplicacao derrubava o gatilho de `audit_log` por SQL cru e reescrevia a
    linha em seguida (medido: a linha voltava como 'ADULTERADO'), porque
    `_nome_da_tabela` devolve `None` para `TextClause` e o `before_flush` nao
    acorda sem objeto ORM. O unico caminho autorizado a mexer nos gatilhos e o
    deste modulo, que marca os proprios comandos com `_OPCAO_INTERNA`.

    Aqui tambem e onde o marcador e traduzido para o autorizador do SQLite, que
    roda dentro do parser e nao ve `execution_options`. O recado e escrito a
    CADA comando, inclusive quando o comando nao e interno: assim a marca nunca
    fica pendurada na conexao por um caminho de excecao que esqueceu de limpar
    -- que seria uma conexao permanentemente sem barreira, o pior defeito
    possivel nesta camada.
    """
    opcoes: Any = {}
    try:
        opcoes = context.execution_options if context is not None else {}
    except AttributeError:  # pragma: no cover - contexto sem opcoes
        opcoes = {}
    # Sem try/except: aqui existe conexao viva por definicao -- o cursor dela
    # esta no argumento. `conn.info` e o MESMO dicionario que o autorizador
    # recebeu como `record.info` no `connect`.
    conn.info[_CHAVE_INTERNA] = bool((opcoes or {}).get("crypto_traders_protecao"))
    _recusar_sql_cru_contra_a_protecao(statement, opcoes)
