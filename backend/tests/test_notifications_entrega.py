"""Entrega dos alertas nos casos que a auditoria de 09/11/2026 achou mudos.

- SMTP: `starttls()` sem contexto usa `ssl._create_stdlib_context()`, que NAO
  verifica certificado nem nome. Quem estiver no caminho apresenta certificado
  proprio, recebe `SMTP_PASSWORD` no `login` e pode engolir o alerta de stop.
- Canal ligado na tela sem os segredos no ambiente devolvia `DeliveryResult(False)`
  SEM uma linha de log: o "stop executado" virava so o `alert` generico.
- Postgres fora (OOM no limite de 256 MB): os destinatarios eram lidos do banco
  ANTES de qualquer canal, a leitura levantava, e justamente o alerta de "sem
  acesso ao banco" nao saia -- com os segredos e o canal no ambiente.
"""

from __future__ import annotations

import ssl
from contextlib import asynccontextmanager
from typing import ClassVar

import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from crypto_traders.config import Settings
from crypto_traders.db.repositories import NotificationConfigRepository
from crypto_traders.db.session import session_scope
from crypto_traders.notifications import MultiChannelNotifier
from crypto_traders.notifications import channels as modulo_canais
from crypto_traders.notifications.channels import DeliveryResult, EmailChannel


@pytest.fixture
def com_segredos(settings) -> Settings:
    return settings.model_copy(
        update={
            "smtp_host": "smtp.exemplo.com",
            "smtp_username": "bot@exemplo.com",
            # model_copy nao valida: o segredo ja vai como SecretStr.
            "smtp_password": SecretStr("senha-de-app"),
            "whatsapp_phone_number_id": "123456",
            "whatsapp_access_token": SecretStr("token"),
            "whatsapp_template_name": "alerta_sistema",
        }
    )


class _SmtpFalso:
    instancias: ClassVar[list[_SmtpFalso]] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.starttls_context: object = "nao chamado"
        self.logins: list[str] = []
        _SmtpFalso.instancias.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.starttls_context = context

    def login(self, usuario, senha):
        self.logins.append(usuario)

    def send_message(self, mensagem):
        pass


@pytest.fixture
def smtp_falso(monkeypatch):
    _SmtpFalso.instancias = []
    monkeypatch.setattr(modulo_canais.smtplib, "SMTP", _SmtpFalso)
    monkeypatch.setattr(modulo_canais.smtplib, "SMTP_SSL", _SmtpFalso)
    return _SmtpFalso.instancias


def _verifica_certificado(contexto: object) -> bool:
    return (
        isinstance(contexto, ssl.SSLContext)
        and contexto.verify_mode == ssl.CERT_REQUIRED
        and contexto.check_hostname
    )


class TestSmtpVerificaOCertificado:
    async def test_starttls_com_contexto_que_verifica(self, com_segredos, smtp_falso):
        resultado = await EmailChannel(com_segredos).send("t", "b", "paulo@exemplo.com")

        assert resultado.ok
        (servidor,) = smtp_falso
        assert _verifica_certificado(servidor.starttls_context), servidor.starttls_context

    async def test_porta_465_usa_tls_implicito_verificado(self, com_segredos, smtp_falso):
        """465 e TLS desde o primeiro byte: `SMTP` + `starttls` ficava 20 s esperando."""
        config = com_segredos.model_copy(update={"smtp_port": 465})
        resultado = await EmailChannel(config).send("t", "b", "paulo@exemplo.com")

        assert resultado.ok
        (servidor,) = smtp_falso
        assert _verifica_certificado(servidor.context)
        assert servidor.starttls_context == "nao chamado"


class TestFalhaDeEntregaDeixaRastro:
    @pytest.mark.parametrize("canal", ["email", "whatsapp"])
    async def test_canal_ligado_sem_segredo_loga_erro(self, settings, canal):
        async with session_scope(settings) as session:
            await NotificationConfigRepository(session).update(
                {
                    "email_enabled": canal == "email",
                    "email_to": "paulo@exemplo.com",
                    "whatsapp_enabled": canal == "whatsapp",
                    "whatsapp_to": "5511999998888",
                }
            )
        with capture_logs() as registros:
            resultados = await MultiChannelNotifier(settings).send("Stop executado", "BTC")

        assert [r.ok for r in resultados] == [False]
        erros = [r for r in registros if r["log_level"] == "error"]
        assert erros, registros
        assert any(r.get("channel") == canal for r in erros)

    async def test_o_email_do_destinatario_nao_vai_inteiro_para_o_log(
        self, com_segredos, smtp_falso
    ):
        with capture_logs() as registros:
            await EmailChannel(com_segredos).send("t", "b", "paulo.henrique@exemplo.com")
        assert "paulo.henrique@exemplo.com" not in repr(registros)


class TestAlertaSemPostgres:
    @pytest.fixture
    def banco_cai(self, monkeypatch):
        """Liga/desliga a leitura do banco do notificador."""
        estado = {"fora": False}
        import crypto_traders.notifications as modulo

        original = modulo.session_scope

        @asynccontextmanager
        async def talvez_fora(cfg=None):
            if estado["fora"]:
                raise ConnectionRefusedError("postgres fora do ar")
            async with original(cfg) as session:
                yield session

        monkeypatch.setattr(modulo, "session_scope", talvez_fora)
        return estado

    @staticmethod
    def _captura(notifier):
        enviados: list[tuple[str, str]] = []

        def fazer(nome):
            async def enviar(title, body, to):
                enviados.append((nome, to))
                return DeliveryResult(nome, True, "ok")

            return enviar

        for nome, canal in notifier.channels.items():
            canal.send = fazer(nome)  # type: ignore[method-assign]
        return enviados

    async def test_usa_a_ultima_configuracao_lida(self, com_segredos, banco_cai):
        async with session_scope(com_segredos) as session:
            await NotificationConfigRepository(session).update(
                {"email_enabled": True, "email_to": "paulo@exemplo.com"}
            )
        notifier = MultiChannelNotifier(com_segredos)
        enviados = self._captura(notifier)
        await notifier.send("primeiro", "banco no ar")

        banco_cai["fora"] = True
        with capture_logs() as registros:
            resultados = await notifier.send("Sem acesso ao banco", "postgres caiu")

        assert enviados == [("email", "paulo@exemplo.com")] * 2
        assert [r.ok for r in resultados] == [True]
        assert any(r["log_level"] == "error" for r in registros)

    async def test_sem_leitura_anterior_usa_os_destinatarios_de_reserva(
        self, com_segredos, banco_cai
    ):
        """O sistema subiu com o banco ja fora: nao ha ultima configuracao."""
        config = com_segredos.model_copy(
            update={
                "alerta_email_reserva": "paulo@exemplo.com",
                "alerta_whatsapp_reserva": "5511999998888",
            }
        )
        banco_cai["fora"] = True
        notifier = MultiChannelNotifier(config)
        enviados = self._captura(notifier)

        await notifier.send("Sem acesso ao banco", "postgres caiu")

        assert sorted(enviados) == [
            ("email", "paulo@exemplo.com"),
            ("whatsapp", "5511999998888"),
        ]

    async def test_sem_nada_o_alerta_fica_no_log_e_nao_levanta(self, com_segredos, banco_cai):
        banco_cai["fora"] = True
        notifier = MultiChannelNotifier(com_segredos)
        enviados = self._captura(notifier)
        with capture_logs() as registros:
            assert await notifier.send("Sem acesso ao banco", "postgres caiu") == []
        assert enviados == []
        assert any(r["event"] == "alert" for r in registros)
        assert any(r["log_level"] == "error" for r in registros)

    async def test_banco_que_nao_responde_nao_segura_o_alerta(
        self, com_segredos, monkeypatch
    ):
        """Postgres travado (conexao que nao volta) nao pode atrasar o alerta indefinidamente."""
        import asyncio

        import crypto_traders.notifications as modulo

        @asynccontextmanager
        async def travado(cfg=None):
            await asyncio.sleep(3600)
            yield None

        monkeypatch.setattr(modulo, "session_scope", travado)
        monkeypatch.setattr(modulo, "PRAZO_LEITURA_CONFIG_S", 0.05)
        config = com_segredos.model_copy(update={"alerta_email_reserva": "paulo@exemplo.com"})
        notifier = MultiChannelNotifier(config)
        enviados = self._captura(notifier)

        await asyncio.wait_for(notifier.send("t", "b"), timeout=2)
        assert enviados == [("email", "paulo@exemplo.com")]
