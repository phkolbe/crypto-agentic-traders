"""Testes dos canais de alerta (e-mail e WhatsApp).

O que precisa ser garantido: um canal quebrado não silencia o outro, o log
sempre registra mesmo sem canal nenhum, e os segredos nunca saem pela API.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from crypto_traders.api.app import create_app
from crypto_traders.config import Settings
from crypto_traders.notifications import MultiChannelNotifier, NullNotifier
from crypto_traders.notifications.channels import (
    DeliveryResult,
    EmailChannel,
    WhatsAppChannel,
    _describe_meta_error,
    _mask_phone,
    sanitize_template_param,
)


@pytest.fixture
def configured_settings(settings) -> Settings:
    return settings.model_copy(
        update={
            "smtp_host": "smtp.exemplo.com",
            "smtp_username": "bot@exemplo.com",
            "smtp_password": "senha-de-app",
            "whatsapp_phone_number_id": "123456",
            "whatsapp_access_token": "token",
            "whatsapp_template_name": "alerta_sistema",
        }
    )


class TestChannelConfiguration:
    def test_reports_exactly_what_is_missing(self, settings):
        """A interface precisa dizer o que fazer, não só que não funciona."""
        assert EmailChannel(settings).missing_settings() == [
            "SMTP_HOST",
            "SMTP_USERNAME",
            "SMTP_PASSWORD",
        ]
        assert WhatsAppChannel(settings).missing_settings() == [
            "WHATSAPP_PHONE_NUMBER_ID",
            "WHATSAPP_ACCESS_TOKEN",
            "WHATSAPP_TEMPLATE_NAME",
        ]

    def test_configured_when_secrets_are_present(self, configured_settings):
        assert EmailChannel(configured_settings).configured
        assert WhatsAppChannel(configured_settings).configured

    def test_blank_secret_counts_as_absent(self):
        """`SMTP_PASSWORD=` é ausência, não senha vazia."""
        blank = Settings(smtp_host="h", smtp_username="u", smtp_password="")
        assert "SMTP_PASSWORD" in EmailChannel(blank).missing_settings()

    async def test_send_without_configuration_fails_with_guidance(self, settings):
        result = await EmailChannel(settings).send("t", "b", "a@b.com")
        assert not result.ok
        assert "SMTP_HOST" in result.detail

    async def test_send_without_recipient_fails(self, configured_settings):
        result = await WhatsAppChannel(configured_settings).send("t", "b", "")
        assert not result.ok
        assert "numero" in result.detail


class TestWhatsAppTemplateSanitizing:
    def test_collapses_newlines(self):
        """A Meta rejeita parâmetro de template com quebra de linha.

        Um alerta bem escrito tem exatamente isso — sem a limpeza, justamente as
        mensagens mais importantes falhariam no envio.
        """
        assert "\n" not in sanitize_template_param("linha um\nlinha dois")
        assert sanitize_template_param("a\n\nb") == "a b"

    def test_collapses_tabs_and_repeated_spaces(self):
        assert sanitize_template_param("a\t\tb   c") == "a b c"

    def test_truncates_long_messages(self):
        result = sanitize_template_param("x" * 5000)
        assert len(result) <= 900
        assert result.endswith("…")

    def test_keeps_short_messages_intact(self):
        assert sanitize_template_param("circuit breaker acionado") == "circuit breaker acionado"


class TestMetaErrorTranslation:
    def test_explains_missing_template(self):
        detail = _describe_meta_error('{"error":{"message":"template does not exist"}}')
        assert "template" in detail and "aprovado" in detail

    def test_explains_invalid_token(self):
        assert "token" in _describe_meta_error('{"error":{"message":"Invalid access token"}}')

    def test_explains_unauthorized_recipient(self):
        detail = _describe_meta_error('{"error":{"message":"Recipient not in allowed list"}}')
        assert "cadastrado" in detail

    def test_masks_the_phone_number_in_logs(self):
        """Número completo em log operacional é dado pessoal desnecessário."""
        assert _mask_phone("5511999998888") == "…8888"


class TestMultiChannelDelivery:
    async def test_disabled_channels_are_skipped(self, configured_settings):
        notifier = MultiChannelNotifier(configured_settings)
        assert await notifier.send("t", "b") == []

    async def test_a_crashing_channel_does_not_block_the_other(
        self, configured_settings, monkeypatch
    ):
        """Um canal quebrado não pode impedir o outro de avisar."""
        from crypto_traders.db.repositories import NotificationConfigRepository
        from crypto_traders.db.session import session_scope

        async with session_scope(configured_settings) as session:
            await NotificationConfigRepository(session).update(
                {
                    "email_enabled": True,
                    "email_to": "a@b.com",
                    "whatsapp_enabled": True,
                    "whatsapp_to": "5511999998888",
                }
            )

        notifier = MultiChannelNotifier(configured_settings)

        async def explode(title, body, to):
            raise RuntimeError("SMTP fora do ar")

        async def works(title, body, to):
            return DeliveryResult("whatsapp", True, "enviado")

        monkeypatch.setattr(notifier.channels["email"], "send", explode)
        monkeypatch.setattr(notifier.channels["whatsapp"], "send", works)

        results = await notifier.send("Circuit breaker", "detalhe")
        assert {r.channel: r.ok for r in results} == {"email": False, "whatsapp": True}

    async def test_null_notifier_still_logs(self):
        """Sem canal configurado não é erro: o log é a fonte de verdade."""
        assert await NullNotifier().send("t", "b") == []


class TestNotificationApi:
    @pytest.fixture
    async def client(self, settings):
        app = create_app()
        app.state.settings = settings
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http

    async def test_starts_with_both_channels_off(self, client):
        """Um canal só passa a existir quando o operador liga conscientemente."""
        body = (await client.get("/api/notifications/config")).json()
        assert body["email_enabled"] is False
        assert body["whatsapp_enabled"] is False

    async def test_never_returns_secrets(self, client):
        """A resposta diz se o segredo existe, nunca qual é."""
        raw = (await client.get("/api/notifications/config")).text
        assert "smtp_password" not in raw
        assert "access_token" not in raw
        assert "missing_settings" in raw

    async def test_reports_what_is_missing_per_channel(self, client):
        body = (await client.get("/api/notifications/config")).json()
        assert "SMTP_HOST" in body["email"]["missing_settings"]
        assert body["whatsapp"]["configured"] is False

    async def test_saves_recipients(self, client):
        response = await client.put(
            "/api/notifications/config",
            json={"email_to": "paulo@exemplo.com", "whatsapp_to": "5511999998888"},
        )
        assert response.status_code == 200
        assert response.json()["email_to"] == "paulo@exemplo.com"

    async def test_enabling_a_channel_without_recipient_is_refused(self, client):
        """Canal "ligado" que nunca entrega é pior que desligado: cria falsa cobertura."""
        response = await client.put("/api/notifications/config", json={"email_enabled": True})
        assert response.status_code == 422
        assert "destino" in response.json()["detail"]

    async def test_enabling_whatsapp_without_number_is_refused(self, client):
        response = await client.put("/api/notifications/config", json={"whatsapp_enabled": True})
        assert response.status_code == 422

    async def test_enabling_with_recipient_in_the_same_request_works(self, client):
        response = await client.put(
            "/api/notifications/config",
            json={"email_enabled": True, "email_to": "paulo@exemplo.com"},
        )
        assert response.status_code == 200
        assert response.json()["email_enabled"] is True

    async def test_unknown_field_is_refused(self, client):
        response = await client.put("/api/notifications/config", json={"smtp_password": "x"})
        assert response.status_code == 422

    async def test_changes_are_audited(self, client):
        await client.put(
            "/api/notifications/config",
            json={"email_enabled": True, "email_to": "paulo@exemplo.com"},
        )
        entries = (await client.get("/api/audit")).json()
        assert any(e["action"] == "notification_config_updated" for e in entries)

    async def test_test_route_needs_the_agents_running(self, client):
        assert (await client.post("/api/notifications/test")).status_code == 503
