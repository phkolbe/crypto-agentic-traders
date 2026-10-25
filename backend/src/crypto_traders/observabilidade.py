"""Observabilidade: o Sistema Cripto fala OpenTelemetry com um coletor.

A observabilidade e um servico que o Crypto usa, nao um sistema do qual ele faz
parte: um coletor OpenTelemetry recebe, e Loki, Tempo, Prometheus e Grafana
guardam e mostram. Tudo sai por OTLP/HTTP para `OTEL_EXPORTER_OTLP_ENDPOINT`, com
`service.name=crypto` e `deployment.environment` vindo de `ENVIRONMENT`. O painel e
os alertas sao deste repositorio (`observabilidade/publicar_grafana.py`):

* **Logs** -- cada linha do structlog tambem vai ao coletor (Loki), ja com os
  segredos redigidos e com `trace_id` quando ha span ativo.
* **Traces** -- os pedidos HTTP da API (FastAPI) e o SQL feito dentro deles.
  SQL dos agentes, fora de um pedido, nao vira trace: seriam milhares de raizes
  por hora sem ninguem para olhar. O amostrador so aceita raiz de servidor.
* **Metricas** -- por OTLP, e nao por `/metrics` raspado: a API nao tem login, e
  uma rota a mais aberta e o que este projeto evita. O coletor converte para o
  Prometheus com `job="crypto"`. Vem de duas fontes:
  o estado do orquestrador (o mesmo `/api/health` do dashboard, lido a cada
  `INTERVALO_ESTADO`) e a contagem de eventos do barramento (`EventBus.observar`).

Sem a variavel nada e instalado, e o sistema roda exatamente como antes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace import SpanKind

from . import logging_setup
from .bus.base import Topics
from .config import Settings
from .logging_setup import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .agents.orchestrator import Orchestrator

SERVICE_NAME = "crypto"

#: Pedidos que nao viram span: a sonda de saude (a cada 30 s, do Docker) e o
#: WebSocket do dashboard (uma conexao de horas seria um span de horas).
ROTAS_SEM_TRACE = "/api/health,/ws"

#: De quanto em quanto tempo o estado do orquestrador e relido para as metricas.
INTERVALO_ESTADO = 15.0

#: Intervalo de envio das metricas ao coletor (ms), o mesmo da raspagem da plataforma.
INTERVALO_METRICAS_MS = 15_000

#: Atributos do `logging.LogRecord`: um campo do structlog com um destes nomes
#: colidiria com o registro e o `logging` recusaria a linha.
_ATRIBUTOS_DO_REGISTRO = frozenset(logging.LogRecord("x", 0, "x", 0, "x", None, None).__dict__) | {
    "message",
    "asctime",
}

log = get_logger(__name__)

_tracer_provider: Any = None
_logger_provider: Any = None
_meter_provider: Any = None
_logger_espelho: logging.Logger | None = None

#: O que as metricas de estado mostram; preenchido por `acompanhar`.
_estado: dict[str, Any] = {}


def ligada(settings: Settings) -> bool:
    return bool(settings.otel_exporter_otlp_endpoint.strip())


# ---------------------------------------------------------------------------
# Instalacao
# ---------------------------------------------------------------------------
def instalar(settings: Settings) -> bool:
    """Liga traces, logs e metricas. Devolve False quando a observabilidade esta desligada.

    Idempotente: os testes sobem varios sistemas no mesmo processo.
    """
    global _tracer_provider, _logger_provider, _meter_provider, _logger_espelho
    if not ligada(settings) or _tracer_provider is not None:
        return _tracer_provider is not None

    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased

    endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
    recurso = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": _versao(),
            "deployment.environment": settings.environment,
            # Fixo por ambiente: ha um processo so, e um id aleatorio por subida
            # viraria serie nova no Prometheus a cada reinicio (rotulo `instance`).
            "service.instance.id": f"{SERVICE_NAME}-{settings.environment}",
        }
    )

    _tracer_provider = TracerProvider(resource=recurso, sampler=ParentBased(_SoRaizDeServidor()))
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(_tracer_provider)

    _logger_provider = LoggerProvider(resource=recurso)
    _logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"))
    )
    _logger_espelho = logging.getLogger("crypto_traders.otel")
    _logger_espelho.handlers.clear()
    _logger_espelho.addHandler(
        LoggingHandler(level=logging.DEBUG, logger_provider=_logger_provider)
    )
    _logger_espelho.setLevel(logging.DEBUG)
    _logger_espelho.propagate = False
    logging_setup.definir_espelho(_espelhar_log)

    _meter_provider = MeterProvider(
        resource=recurso,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
                export_interval_millis=INTERVALO_METRICAS_MS,
            )
        ],
    )
    metrics.set_meter_provider(_meter_provider)
    _registrar_metricas(_meter_provider.get_meter("crypto_traders"))

    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    from .db.session import get_engine

    # skip_dep_check: a instrumentacao declara uma faixa de SQLAlchemy mais
    # estreita que a usada; os eventos do engine que ela escuta sao os mesmos.
    SQLAlchemyInstrumentor().instrument(
        engine=get_engine(settings).sync_engine,
        tracer_provider=_tracer_provider,
        skip_dep_check=True,
    )
    # Coletor fora do ar nao pode virar uma linha de erro a cada lote ("Failed
    # to export ... batch" sai em ERROR): o sintoma de verdade e o painel vazio,
    # e o alerta "Crypto sem telemetria" no Grafana.
    logging.getLogger("opentelemetry").setLevel(logging.CRITICAL)
    log.info("observabilidade.ligada", endpoint=endpoint, ambiente=settings.environment)
    return True


def instrumentar_api(app: FastAPI) -> None:
    """Um span por pedido HTTP, continuando o `traceparent` que vier do Caddy."""
    if _tracer_provider is None:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=_tracer_provider, excluded_urls=ROTAS_SEM_TRACE
    )


def desligar() -> None:
    """Entrega ao coletor o que esta pendente antes de o processo sair."""
    for provedor in (_tracer_provider, _logger_provider, _meter_provider):
        if provedor is not None:
            with contextlib.suppress(Exception):
                provedor.force_flush()


def _versao() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("crypto-agentic-traders")
    except PackageNotFoundError:  # pragma: no cover - rodando sem instalar
        return "0.0.0"


class _SoRaizDeServidor(Sampler):
    """Aceita raiz de span SERVER (pedido HTTP); descarta raiz de qualquer outro tipo.

    Filho segue o pai (`ParentBased`), entao o SQL de dentro de um pedido entra
    no trace dele, e o SQL que os agentes fazem sozinhos nao gera trace nenhum.
    """

    def should_sample(  # type: ignore[no-untyped-def]
        self,
        parent_context,
        trace_id,
        name,
        kind=None,
        attributes=None,
        links=None,
        trace_state=None,
    ):
        decisao = Decision.RECORD_AND_SAMPLE if kind == SpanKind.SERVER else Decision.DROP
        return SamplingResult(decisao, attributes if decisao.is_sampled() else None)

    def get_description(self) -> str:
        return "SoRaizDeServidor"


# ---------------------------------------------------------------------------
# Logs: o structlog continua sendo a unica forma de logar
# ---------------------------------------------------------------------------
_NIVEIS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "exception": logging.ERROR,
}


def _espelhar_log(metodo: str, evento: dict[str, Any]) -> None:
    """Manda ao coletor a mesma linha que vai ao console (ja redigida)."""
    if _logger_espelho is None:
        return
    nivel = _NIVEIS.get(str(evento.get("level", metodo)).lower(), logging.INFO)
    extra: dict[str, Any] = {}
    for chave, valor in evento.items():
        if chave in ("event", "level", "timestamp"):
            continue
        nome = f"{chave}_" if chave in _ATRIBUTOS_DO_REGISTRO else chave
        extra[nome] = valor if isinstance(valor, str | int | float | bool) else str(valor)
    _logger_espelho.log(nivel, str(evento.get("event", "")), extra=extra)


# ---------------------------------------------------------------------------
# Metricas
# ---------------------------------------------------------------------------
_contadores: dict[str, Any] = {}


def _registrar_metricas(meter: Any) -> None:
    from opentelemetry.metrics import Observation

    def gauge(nome: str, descricao: str, unidade: str, leitor) -> None:  # type: ignore[no-untyped-def]
        def callback(_options):  # type: ignore[no-untyped-def]
            try:
                return [Observation(valor, atributos) for valor, atributos in leitor()]
            except Exception:  # pragma: no cover - metrica nunca derruba o exportador
                return []

        meter.create_observable_gauge(
            nome, callbacks=[callback], unit=unidade, description=descricao
        )

    def acumulado(nome: str, descricao: str, unidade: str, leitor) -> None:  # type: ignore[no-untyped-def]
        def callback(_options):  # type: ignore[no-untyped-def]
            try:
                return [Observation(valor, atributos) for valor, atributos in leitor()]
            except Exception:  # pragma: no cover
                return []

        meter.create_observable_counter(
            nome, callbacks=[callback], unit=unidade, description=descricao
        )

    # Unidades entre chaves sao so anotacao: o Prometheus nao acrescenta sufixo.
    gauge(
        "crypto_info",
        "Modo de operacao e exchange desta instalacao (sempre 1)",
        "{info}",
        _ler_info,
    )
    gauge(
        "crypto_agente_saudavel",
        "1 se o agente esta rodando, sem atraso e sem desistencia",
        "{bool}",
        lambda: _por_agente(_agente_saudavel),
    )
    gauge(
        "crypto_agente_pausado",
        "1 se o agente esta pausado",
        "{bool}",
        lambda: _por_agente(lambda a: int(bool(a.get("paused")))),
    )
    gauge(
        "crypto_agente_heartbeat_idade",
        "Tempo desde a ultima batida do agente",
        "s",
        lambda: _por_agente(_idade_heartbeat, pular_nulo=True),
    )
    gauge(
        "crypto_agente_eventos_pendentes",
        "Eventos na caixa de entrada do agente",
        "{eventos}",
        lambda: _por_agente(lambda a: int(a.get("pending_events") or 0)),
    )
    gauge(
        "crypto_agente_topicos_surdos",
        "Topicos em que o agente esta inscrito e nao recebe nada",
        "{topicos}",
        lambda: _por_agente(lambda a: len(a.get("deaf_topics") or [])),
    )
    acumulado(
        "crypto_agente_reinicios",
        "Reinicios do agente pelo watchdog desde a subida",
        "{reinicios}",
        lambda: _por_agente(lambda a: int(a.get("restarts") or 0)),
    )
    acumulado(
        "crypto_agente_eventos_perdidos",
        "Eventos que o agente perdeu desde a subida",
        "{eventos}",
        lambda: _por_agente(lambda a: int(a.get("lost_events") or 0)),
    )
    gauge("crypto_tarefa_viva", "1 se a tarefa do orquestrador esta de pe", "{bool}", _ler_tarefas)
    gauge(
        "crypto_circuit_breaker_ativo",
        "1 com o circuit breaker armado (abertura bloqueada)",
        "{bool}",
        lambda: _escalar("circuit_breaker_active"),
    )
    gauge(
        "crypto_aberturas_bloqueadas",
        "1 quando a execucao esta barrando novas aberturas",
        "{bool}",
        _ler_aberturas_bloqueadas,
    )
    gauge(
        "crypto_dimensionamento_viavel",
        "0 quando patrimonio e limites impedem qualquer ordem",
        "{bool}",
        lambda: _escalar("sizing_feasible"),
    )
    gauge(
        "crypto_pares_ativos",
        "Pares que o sistema esta negociando",
        "{pares}",
        lambda: (
            [(len(_estado.get("saude", {}).get("symbols") or []), {})] if "saude" in _estado else []
        ),
    )
    gauge(
        "crypto_estado_idade",
        "Tempo desde a ultima leitura do estado do orquestrador",
        "s",
        _ler_idade_estado,
    )
    gauge(
        "crypto_patrimonio", "Patrimonio na moeda de cotacao, por parte", "{moeda}", _ler_patrimonio
    )
    gauge(
        "crypto_pnl",
        "Resultado na moeda de cotacao (realizado e nao realizado)",
        "{moeda}",
        _ler_pnl,
    )
    gauge(
        "crypto_posicoes_abertas",
        "Posicoes com quantidade no ultimo snapshot",
        "{posicoes}",
        _ler_posicoes,
    )
    gauge(
        "crypto_snapshot_idade",
        "Tempo desde o ultimo snapshot da carteira",
        "s",
        _ler_idade_snapshot,
    )

    _contadores["eventos"] = meter.create_counter(
        "crypto_eventos",
        unit="{eventos}",
        description="Eventos publicados no barramento, por topico",
    )
    _contadores["sinais"] = meter.create_counter(
        "crypto_sinais",
        unit="{sinais}",
        description="Sinais das estrategias, por estrategia e direcao",
    )
    _contadores["risco"] = meter.create_counter(
        "crypto_risco_decisoes",
        unit="{decisoes}",
        description="Decisoes do Risk Manager (aprovado ou rejeitado)",
    )
    _contadores["ordens"] = meter.create_counter(
        "crypto_ordens", unit="{ordens}", description="Resultados de ordem da execucao, por status"
    )


def _ler_info() -> Iterable[tuple[int, dict[str, str]]]:
    saude = _estado.get("saude")
    if not saude:
        return []
    return [(1, {"modo": str(saude.get("mode")), "exchange": str(saude.get("exchange"))})]


def _por_agente(leitor, pular_nulo: bool = False):  # type: ignore[no-untyped-def]
    resultado = []
    for nome, agente in (_estado.get("saude", {}).get("agents") or {}).items():
        valor = leitor(agente)
        if valor is None and pular_nulo:
            continue
        resultado.append((valor, {"agente": nome}))
    return resultado


def _agente_saudavel(agente: dict[str, Any]) -> int:
    return int(
        bool(agente.get("running"))
        and not agente.get("stale")
        and not agente.get("given_up")
        and str(agente.get("state")) != "error"
    )


def _idade_heartbeat(agente: dict[str, Any]) -> float | None:
    batida = agente.get("last_heartbeat")
    if not batida:
        return None
    return max(0.0, (datetime.now(UTC) - datetime.fromisoformat(batida)).total_seconds())


def _ler_tarefas():  # type: ignore[no-untyped-def]
    tarefas = _estado.get("saude", {}).get("tasks") or {}
    return [(int(bool(viva)), {"tarefa": nome}) for nome, viva in tarefas.items()]


def _escalar(chave: str):  # type: ignore[no-untyped-def]
    saude = _estado.get("saude")
    if not saude or chave not in saude:
        return []
    return [(int(bool(saude[chave])), {})]


def _ler_aberturas_bloqueadas():  # type: ignore[no-untyped-def]
    """`openings_blocked` e o MOTIVO do bloqueio (texto), ou None quando liberado."""
    execucao = (_estado.get("saude", {}).get("agents") or {}).get("execution")
    if not execucao or "openings_blocked" not in execucao:
        return []
    return [(int(bool(execucao["openings_blocked"])), {})]


def _ler_idade_estado():  # type: ignore[no-untyped-def]
    lido = _estado.get("lido_em")
    if lido is None:
        return []
    return [(max(0.0, (datetime.now(UTC) - lido).total_seconds()), {})]


def _ler_patrimonio():  # type: ignore[no-untyped-def]
    snap = _estado.get("snapshot")
    if snap is None:
        return []
    return [
        (float(snap.total_value), {"parte": "total"}),
        (float(snap.cash_value), {"parte": "caixa"}),
        (float(snap.positions_value), {"parte": "posicoes"}),
    ]


def _ler_pnl():  # type: ignore[no-untyped-def]
    snap = _estado.get("snapshot")
    if snap is None:
        return []
    return [
        (float(snap.realized_pnl), {"tipo": "realizado"}),
        (float(snap.unrealized_pnl), {"tipo": "nao_realizado"}),
    ]


def _ler_posicoes():  # type: ignore[no-untyped-def]
    """Ativos com quantidade, fora a moeda de cotacao (o caixa tambem vem na lista)."""
    snap = _estado.get("snapshot")
    if snap is None:
        return []
    moeda = _estado.get("moeda")
    return [(sum(1 for p in snap.positions if p.quantity > 0 and p.asset != moeda), {})]


def _ler_idade_snapshot():  # type: ignore[no-untyped-def]
    snap = _estado.get("snapshot")
    if snap is None:
        return []
    return [(max(0.0, (datetime.now(UTC) - snap.timestamp).total_seconds()), {})]


def contar_evento(topico: str, payload: Any) -> None:
    """Observador do barramento: conta o evento e guarda o ultimo snapshot.

    Rotulos so de conjuntos fechados (topico, estrategia, direcao, decisao,
    status): o par negociado fica de fora, porque no modo de descoberta o
    universo muda sozinho e cada par novo seria uma serie nova para sempre.
    """
    if not _contadores:
        return
    _contadores["eventos"].add(1, {"topico": topico})
    if topico == Topics.SIGNALS:
        _contadores["sinais"].add(
            1, {"estrategia": str(payload.strategy), "direcao": str(payload.direction)}
        )
    elif topico == Topics.RISK_ASSESSMENTS:
        _contadores["risco"].add(1, {"decisao": str(payload.decision)})
    elif topico == Topics.ORDER_RESULTS:
        _contadores["ordens"].add(1, {"status": str(payload.status)})
    elif topico == Topics.PORTFOLIO_SNAPSHOTS:
        _estado["snapshot"] = payload


async def acompanhar(orchestrator: Orchestrator) -> None:
    """Le o estado do orquestrador a cada `INTERVALO_ESTADO` e conta os eventos.

    Roda como tarefa ate ser cancelada. Uma leitura que falha nao derruba a
    tarefa: `crypto_estado_idade` crescendo e o sinal de que ela parou de ler.
    """
    if _meter_provider is None:
        return
    orchestrator.bus.observar(contar_evento)
    while True:
        try:
            _estado["moeda"] = orchestrator.settings.trading.quote_currency
            _estado["saude"] = await orchestrator.health()
            _estado["lido_em"] = datetime.now(UTC)
        except Exception as exc:  # pragma: no cover - defesa
            log.warning("observabilidade.estado_ilegivel", error=str(exc))
        await asyncio.sleep(INTERVALO_ESTADO)
