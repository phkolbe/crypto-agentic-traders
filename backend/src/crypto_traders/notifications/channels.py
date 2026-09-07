"""Canais de alerta: e-mail (SMTP) e WhatsApp (Meta Cloud API).

Divisao deliberada entre segredo e preferencia:

- **Segredos ficam no `.env`**: senha de SMTP e token da Meta. Credencial em
  banco contraria a premissa do projeto, e um backup do banco passaria a vazar
  acesso a coisas que nao tem nada a ver com trading.
- **Preferencias ficam no banco** e sao editaveis pela interface: ligar/desligar
  cada canal, endereco de e-mail e numero de WhatsApp.

Falha de entrega nunca derruba nada: um canal quebrado nao pode impedir o outro
de avisar, e nenhum dos dois pode parar o sistema de negociacao.
"""

from __future__ import annotations

import abc
import asyncio
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

from ..config import Settings
from ..logging_setup import get_logger

log = get_logger(__name__)

#: Versao da Graph API da Meta usada para enviar mensagens.
GRAPH_API_VERSION = "v21.0"

#: A Meta rejeita parametro de template com quebra de linha, tab ou espacos
#: consecutivos, e o limite pratico do corpo e ~1000 caracteres.
WHATSAPP_PARAM_MAX_LENGTH = 900


@dataclass(frozen=True)
class DeliveryResult:
    channel: str
    ok: bool
    detail: str = ""


class NotificationChannel(abc.ABC):
    """Um meio de avisar o operador."""

    name: str

    @abc.abstractmethod
    async def send(self, title: str, body: str, to: str) -> DeliveryResult: ...

    @property
    @abc.abstractmethod
    def configured(self) -> bool:
        """True quando os segredos do `.env` estao presentes."""

    @abc.abstractmethod
    def missing_settings(self) -> list[str]:
        """Variaveis de ambiente que faltam, para a interface poder dizer o que fazer."""


class EmailChannel(NotificationChannel):
    name = "email"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return not self.missing_settings()

    def missing_settings(self) -> list[str]:
        s = self._settings
        missing = []
        if not s.smtp_host:
            missing.append("SMTP_HOST")
        if not s.smtp_username:
            missing.append("SMTP_USERNAME")
        if s.smtp_password is None:
            missing.append("SMTP_PASSWORD")
        return missing

    async def send(self, title: str, body: str, to: str) -> DeliveryResult:
        if missing := self.missing_settings():
            return DeliveryResult(self.name, False, f"faltam no .env: {', '.join(missing)}")
        if not to:
            return DeliveryResult(self.name, False, "destinatario nao configurado")

        message = EmailMessage()
        message["Subject"] = f"[crypto-traders] {title}"
        message["From"] = self._settings.smtp_from or self._settings.smtp_username
        message["To"] = to
        message.set_content(body)

        try:
            # `smtplib` e sincrono; rodar em thread evita bloquear o loop dos
            # agentes durante o handshake TLS, que pode levar segundos.
            await asyncio.to_thread(self._send_sync, message)
        except Exception as exc:
            log.error("notification.email_failed", error=str(exc))
            return DeliveryResult(self.name, False, str(exc))

        log.info("notification.email_sent", to=to)
        return DeliveryResult(self.name, True, f"enviado para {to}")

    def _send_sync(self, message: EmailMessage) -> None:
        s = self._settings
        password = s.smtp_password.get_secret_value() if s.smtp_password else ""
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as server:
            if s.smtp_use_tls:
                server.starttls()
            server.login(s.smtp_username, password)
            server.send_message(message)


class WhatsAppChannel(NotificationChannel):
    """WhatsApp via Meta Cloud API.

    Alertas sao mensagens **proativas** e raras, entao nunca existe uma sessao de
    24h aberta -- e sem sessao o WhatsApp so aceita **template** aprovado, nunca
    texto livre. Por isso o envio usa `type: template` e injeta o conteudo como
    parametro do corpo.

    O template precisa ser criado no Meta for Developers (categoria "utility")
    com exatamente dois parametros no corpo, por exemplo:

        Alerta {{1}}. Detalhe: {{2}}
    """

    name = "whatsapp"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return not self.missing_settings()

    def missing_settings(self) -> list[str]:
        s = self._settings
        missing = []
        if not s.whatsapp_phone_number_id:
            missing.append("WHATSAPP_PHONE_NUMBER_ID")
        if s.whatsapp_access_token is None:
            missing.append("WHATSAPP_ACCESS_TOKEN")
        if not s.whatsapp_template_name:
            missing.append("WHATSAPP_TEMPLATE_NAME")
        return missing

    async def send(self, title: str, body: str, to: str) -> DeliveryResult:
        if missing := self.missing_settings():
            return DeliveryResult(self.name, False, f"faltam no .env: {', '.join(missing)}")
        if not to:
            return DeliveryResult(self.name, False, "numero nao configurado")

        s = self._settings
        url = (
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
            f"{s.whatsapp_phone_number_id}/messages"
        )
        payload = {
            "messaging_product": "whatsapp",
            "to": _only_digits(to),
            "type": "template",
            "template": {
                "name": s.whatsapp_template_name,
                "language": {"code": s.whatsapp_template_language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": sanitize_template_param(title)},
                            {"type": "text", "text": sanitize_template_param(body)},
                        ],
                    }
                ],
            },
        }

        try:
            async with httpx.AsyncClient(timeout=15) as http:
                response = await http.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {s.whatsapp_access_token.get_secret_value()}"
                    },
                )
            if response.status_code >= 400:
                detail = _describe_meta_error(response.text)
                log.error(
                    "notification.whatsapp_failed",
                    status=response.status_code,
                    detail=detail,
                )
                return DeliveryResult(self.name, False, detail)
        except Exception as exc:
            log.error("notification.whatsapp_failed", error=str(exc))
            return DeliveryResult(self.name, False, str(exc))

        log.info("notification.whatsapp_sent", to=_mask_phone(to))
        return DeliveryResult(self.name, True, f"enviado para {_mask_phone(to)}")


def sanitize_template_param(text: str) -> str:
    """Deixa o texto aceitavel como parametro de template da Meta.

    A API rejeita parametro com quebra de linha, tab ou espacos consecutivos --
    e um alerta bem escrito tem exatamente isso. Sem esta limpeza, justamente as
    mensagens mais importantes falhariam no envio.
    """
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= WHATSAPP_PARAM_MAX_LENGTH:
        return collapsed
    return collapsed[: WHATSAPP_PARAM_MAX_LENGTH - 1].rstrip() + "…"


def _only_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _mask_phone(value: str) -> str:
    """Numero completo em log de operacao e dado pessoal desnecessario."""
    digits = _only_digits(value)
    return f"…{digits[-4:]}" if len(digits) > 4 else "…"


def _describe_meta_error(raw: str) -> str:
    """Traduz o erro da Meta para algo acionavel."""
    lowered = raw.lower()
    if "template" in lowered and ("not exist" in lowered or "not found" in lowered):
        return (
            "template nao encontrado ou ainda nao aprovado pela Meta. "
            "Confira WHATSAPP_TEMPLATE_NAME e o idioma. " + raw[:200]
        )
    if "access token" in lowered or "oauth" in lowered:
        return "token de acesso invalido ou expirado. " + raw[:200]
    if "recipient" in lowered or "not in allowed list" in lowered:
        return (
            "numero destinatario nao autorizado. Em contas de teste da Meta, "
            "o numero precisa ser cadastrado previamente. " + raw[:200]
        )
    return raw[:300]
