"""Alerta que nao chega ao operador nao pode falhar em silencio.

O `Notifier.send` devolve um `DeliveryResult` por canal, e o canal que RECUSA
(SMTP 535, token do WhatsApp vencido) nao levanta: devolve `ok=False`. O
orquestrador descartava a lista -- so o `except` era logado. Um canal quebrado
deixava o stop-loss e o circuit breaker "avisados" sem ninguem receber, e nada
no log em ERROR nem no estado do sistema dizia isso (auditoria de 09/11/2026,
orquestracao).
"""

from __future__ import annotations

import asyncio

from helpers import esperar_ate
from structlog.testing import capture_logs

from crypto_traders.agents.orchestrator import Orchestrator
from crypto_traders.bus import InMemoryEventBus, Topics
from crypto_traders.notifications import DeliveryResult


class _Bancada(Orchestrator):
    def __init__(self, settings, bus) -> None:
        super().__init__(settings)
        self.bus = bus

    @property
    def agents(self):
        return {}


class _Notificador:
    def __init__(self, resultado) -> None:
        self._resultado = resultado
        self.enviados: list[str] = []

    async def send(self, titulo, mensagem):
        self.enviados.append(titulo)
        if isinstance(self._resultado, Exception):
            raise self._resultado
        return self._resultado


async def _publicar_e_drenar(orquestrador, bus, notificador, quantos: int = 1):
    tarefa = asyncio.create_task(orquestrador._drain_alerts())
    await orquestrador._alertas_assinados.wait()
    try:
        for i in range(quantos):
            await bus.publish(
                Topics.ALERTS,
                {"type": "stop_loss", "title": f"Stop-loss acionado {i}", "message": "BTC"},
            )
        await esperar_ate(lambda: len(notificador.enviados) == quantos)
        # Uma volta a mais: o resultado e conferido depois do `send`.
        await asyncio.sleep(0)
    finally:
        tarefa.cancel()


class TestEntregaQueFalha:
    async def test_canal_que_recusa_sai_em_error_e_fica_no_estado(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        orquestrador = _Bancada(settings, bus)
        notificador = _Notificador(
            [
                DeliveryResult("email", False, "SMTP 535: autenticacao recusada"),
                DeliveryResult("whatsapp", True),
            ]
        )
        orquestrador.notifier = notificador

        with capture_logs() as eventos:
            await _publicar_e_drenar(orquestrador, bus, notificador, quantos=2)

        falhas = [e for e in eventos if e["event"] == "orchestrator.alert_delivery_failed"]
        assert len(falhas) == 2
        assert all(f["log_level"] == "error" for f in falhas)
        assert falhas[0]["channel"] == "email"
        assert "535" in falhas[0]["detail"]

        estado = orquestrador.entrega_de_alertas
        assert estado["falhas"] == 2
        assert estado["entregues"] == 2
        assert estado["ultima_falha"]["canal"] == "email"
        assert estado["ultima_falha"]["titulo"] == "Stop-loss acionado 1"
        assert estado["falhas_por_canal"] == {"email": 2}

    async def test_notificador_que_levanta_tambem_conta(self, settings):
        bus = InMemoryEventBus()
        await bus.start()
        orquestrador = _Bancada(settings, bus)
        notificador = _Notificador(RuntimeError("banco fora: sem configuracao de canais"))
        orquestrador.notifier = notificador

        await _publicar_e_drenar(orquestrador, bus, notificador)

        assert orquestrador.entrega_de_alertas["falhas"] == 1
        assert orquestrador.entrega_de_alertas["ultima_falha"]["canal"] == "notificador"

    async def test_sem_canal_ligado_nao_e_falha(self, settings):
        """Nenhum canal configurado e escolha do operador: o log segue valendo."""
        bus = InMemoryEventBus()
        await bus.start()
        orquestrador = _Bancada(settings, bus)
        notificador = _Notificador([])
        orquestrador.notifier = notificador

        with capture_logs() as eventos:
            await _publicar_e_drenar(orquestrador, bus, notificador)

        assert orquestrador.entrega_de_alertas["falhas"] == 0
        assert not [e for e in eventos if e["event"] == "orchestrator.alert_delivery_failed"]

    async def test_o_aviso_direto_tambem_confere_a_entrega(self, settings):
        orquestrador = _Bancada(settings, InMemoryEventBus())
        orquestrador.notifier = _Notificador([DeliveryResult("whatsapp", False, "token vencido")])

        await orquestrador._notify_directly("Caminho de alertas caiu", "...")

        assert orquestrador.entrega_de_alertas["falhas"] == 1
        assert orquestrador.entrega_de_alertas["ultima_falha"]["canal"] == "whatsapp"
