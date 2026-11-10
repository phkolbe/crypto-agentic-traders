"""Historico de negociacoes: consulta, lancamento manual e exportacao."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.repositories import (
    AuditLogRepository,
    OrderRepository,
    SignalRepository,
    TradeRepository,
)
from ...domain.enums import TradeOrigin
from ..deps import db_session
from ..schemas import ManualTradeIn, OrderOut, SignalOut, TradeOut, TradePage

router = APIRouter()

#: Teto de linhas do CSV. Acima disso a rota recusa e pede um periodo, em vez de
#: cortar: a exportacao e para apuracao fiscal, e um arquivo sem as negociacoes
#: mais antigas, sem nada dizendo que faltam, e pior do que nenhum arquivo.
LIMITE_DA_EXPORTACAO = 100_000

#: Primeiros caracteres que fazem o Excel/LibreOffice tratar a celula como
#: formula (OWASP, "CSV injection"); tab e CR entram pelo mesmo motivo.
_INICIO_DE_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _em_utc(valor: datetime | None) -> datetime | None:
    """`start`/`end` em UTC: sem fuso vale UTC, com fuso e convertido.

    O banco guarda UTC. No SQLite o parametro e comparado como hora de parede, e
    `10:00-03:00` (13:00 UTC) passava por 10:00 -- o trade das 12:00 UTC entrava
    num filtro que comeca depois dele. No Postgres o caso sem fuso dependia do
    fuso da sessao.
    """
    if valor is None:
        return None
    return valor.replace(tzinfo=UTC) if valor.tzinfo is None else valor.astimezone(UTC)


def _texto_seguro(valor: str | None) -> str:
    """Texto livre do CSV sem virar formula na planilha.

    A observacao do lancamento manual e digitada (ou colada) pelo operador:
    `=HYPERLINK(...)` nela virava formula ao abrir o CSV fiscal. O apostrofo na
    frente e a convencao das planilhas para "isto e texto".
    """
    texto = (valor or "").replace("\n", " ")
    return "'" + texto if texto.startswith(_INICIO_DE_FORMULA) else texto


@router.get("/trades", response_model=TradePage)
async def list_trades(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    origin: str | None = Query(default=None, pattern="^(agent|manual)$"),
    symbol: str | None = None,
    side: str | None = Query(default=None, pattern="^(buy|sell)$"),
    start: datetime | None = None,
    end: datetime | None = None,
    session: AsyncSession = Depends(db_session),
) -> TradePage:
    """Historico consolidado: operacoes dos agentes E lancamentos manuais."""
    repository = TradeRepository(session)
    filters = {
        "origin": origin,
        "symbol": symbol,
        "side": side,
        "start": _em_utc(start),
        "end": _em_utc(end),
    }
    rows = await repository.list(limit=limit, offset=offset, **filters)
    total = await repository.count(**filters)
    return TradePage(
        items=[TradeOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


#: Distancia maxima entre o preco lancado e o ultimo preco conhecido do par
#: antes de a rota pedir confirmacao propria. Larga o bastante para a variacao
#: de um dia num ativo volatil; estreita o bastante para pegar o zero a mais ou
#: a menos (que e um fator 10, e nao 20%).
FAIXA_DE_PLAUSIBILIDADE = Decimal("0.20")


def _preco_de_referencia(request: Request, symbol: str) -> Decimal | None:
    """Ultimo preco conhecido do par, ou `None` quando o sistema nao o observa.

    Le do `MarketDataAgent` do processo (ticker corrente; sem ele, o fechamento
    do ultimo candle) pelo mesmo metodo que valoriza a carteira -- sem consultar
    a exchange, para que lancar um trade nao dependa de rede nem gaste limite
    de requisicao. A chave segue `MarketDataAgent._chave_de_preco`: o ativo base
    para o par na moeda de cotacao, o nome completo do par para as demais.
    """
    orchestrator = getattr(request.app.state, "orchestrator", None)
    market_data = getattr(orchestrator, "market_data", None)
    if market_data is None:
        return None
    base, _, quote = symbol.partition("/")
    cotacao = orchestrator.settings.trading.quote_currency.upper()
    chave = base if quote == cotacao else symbol
    return market_data.precos_para_avaliacao().get(chave)


def _trade_completo(trade) -> dict:
    """O lancamento inteiro, serializavel no `audit_log` (dinheiro como string)."""
    return TradeOut.model_validate(trade).model_dump(mode="json")


@router.post("/trades/manual", response_model=TradeOut, status_code=status.HTTP_201_CREATED)
async def create_manual_trade(
    payload: ManualTradeIn, request: Request, session: AsyncSession = Depends(db_session)
) -> TradeOut:
    """Registra uma operacao feita fora do sistema (ex.: pelo app da exchange).

    Entra na mesma tabela `trades`, o que mantem o dashboard, os filtros e a
    exportacao fiscal com uma unica fonte -- e faz o lancamento contar no
    calculo de preco medio junto com as operacoes dos agentes.

    E esse preco medio que define o stop e o alvo. Por isso a rota exige
    `confirm=true` e confere o preco contra o ultimo preco conhecido do par:
    fora de +-20% devolve 409 e so grava com `aceitar_preco_fora_do_mercado`.
    Cenario da auditoria de 09/11/2026 (P0 #12): preco medio 95.000, compra
    lancada a 9.500 por um zero a menos -> stop de 3% de ~92.150 para ~50.680,
    sem aviso. Sem preco conhecido (agentes parados, par fora do universo) nao
    ha com o que comparar: grava, e o `audit_log` registra que nao conferiu.
    """
    if not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "o lancamento manual entra no preco medio e move o stop e o alvo "
                "da posicao; envie confirm=true"
            ),
        )

    referencia = _preco_de_referencia(request, payload.symbol)
    if referencia is not None and referencia > 0:
        desvio = abs(payload.price - referencia) / referencia
        if desvio > FAIXA_DE_PLAUSIBILIDADE and not payload.aceitar_preco_fora_do_mercado:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"o preco {payload.price} esta {desvio:.0%} longe do ultimo preco "
                    f"conhecido de {payload.symbol} ({referencia}). Confira se nao "
                    "falta ou sobra um zero: este preco entra no preco medio e move "
                    "o stop. Se a operacao e antiga e o preco esta certo, envie "
                    "aceitar_preco_fora_do_mercado=true"
                ),
            )

    trade = await TradeRepository(session).record(
        executed_at=payload.executed_at,
        exchange=payload.exchange,
        symbol=payload.symbol,
        side=payload.side,
        quantity=payload.quantity,
        price=payload.price,
        fee=payload.fee,
        fee_currency=payload.fee_currency,
        origin=TradeOrigin.MANUAL,
        mode="manual",
        notes=payload.notes,
    )
    await AuditLogRepository(session).append(
        action="manual_trade_recorded",
        actor="user",
        target=trade.id,
        after={
            **_trade_completo(trade),
            "preco_de_referencia": str(referencia) if referencia is not None else None,
            "aceitar_preco_fora_do_mercado": payload.aceitar_preco_fora_do_mercado,
        },
    )
    return TradeOut.model_validate(trade)


@router.delete("/trades/{trade_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_manual_trade(
    trade_id: str,
    confirm: bool = Query(
        default=False,
        description="Confirmacao explicita: remover o lancamento reapura o preco medio e o stop.",
    ),
    session: AsyncSession = Depends(db_session),
) -> Response:
    """Remove um lancamento manual digitado errado.

    Operacoes dos agentes nunca sao removiveis: elas sao o registro do que o
    sistema de fato fez, e apagar isso destruiria a auditoria.

    Remover tambem mexe no preco medio, e portanto no stop: exige
    `?confirm=true` (na query, porque corpo em DELETE nao e confiavel entre
    clientes e proxies) e grava o lancamento INTEIRO no `before` do
    `audit_log`. Antes gravava so o id, e nao dava para saber o que foi apagado
    nem reconstruir o efeito no stop.
    """
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "remover o lancamento reapura o preco medio e move o stop e o alvo "
                "da posicao; envie confirm=true"
            ),
        )

    repository = TradeRepository(session)
    trade = await repository.get(trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="negociacao nao encontrada")
    if trade.origin != str(TradeOrigin.MANUAL):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="apenas lancamentos manuais podem ser removidos; "
            "operacoes dos agentes fazem parte da auditoria",
        )

    antes = _trade_completo(trade)
    await repository.delete(trade_id)
    await AuditLogRepository(session).append(
        action="manual_trade_deleted", actor="user", target=trade_id, before=antes
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/trades/export")
async def export_trades(
    start: datetime | None = None,
    end: datetime | None = None,
    session: AsyncSession = Depends(db_session),
) -> Response:
    """Exporta o historico em CSV.

    Pensado para apuracao fiscal: inclui taxa, moeda da taxa, origem e o modo de
    operacao, para que simulacao nunca seja confundida com dinheiro real.
    """
    repository = TradeRepository(session)
    inicio, fim = _em_utc(start), _em_utc(end)
    total = await repository.count(start=inicio, end=fim)
    if total > LIMITE_DA_EXPORTACAO:
        raise HTTPException(
            status_code=422,
            detail=(
                f"o periodo tem {total} negociacoes, acima do teto de "
                f"{LIMITE_DA_EXPORTACAO} por arquivo; exporte por partes com start/end"
            ),
        )
    rows = await repository.list(limit=LIMITE_DA_EXPORTACAO, start=inicio, end=fim)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        [
            "data_hora_utc", "exchange", "par", "lado", "quantidade", "preco",
            "valor_total", "taxa", "moeda_taxa", "origem", "estrategia",
            "modo", "pnl_realizado", "observacoes",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.executed_at.isoformat(), row.exchange, row.symbol, row.side,
                row.quantity, row.price, row.notional, row.fee, row.fee_currency or "",
                row.origin, _texto_seguro(row.strategy), row.mode,
                row.realized_pnl if row.realized_pnl is not None else "",
                _texto_seguro(row.notes),
            ]
        )

    return Response(
        content=buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="negociacoes.csv"'},
    )


@router.get("/signals", response_model=list[SignalOut])
async def list_signals(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(db_session),
) -> list[SignalOut]:
    rows = await SignalRepository(session).list(limit=limit, offset=offset)
    return [SignalOut.model_validate(row) for row in rows]


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(db_session),
) -> list[OrderOut]:
    rows = await OrderRepository(session).list(limit=limit, offset=offset)
    return [OrderOut.model_validate(row) for row in rows]
