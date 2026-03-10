from __future__ import annotations

import asyncio

from app.tests.conftest import create_tenant, get_tenant


def test_webhook_by_tenant_uses_secret_and_processes_message(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Webhook", "whatsapp:+100"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))

    async def _configure():
        async with db_session() as session:
            async with session.begin():
                current = await session.get(type(tenant), tenant_id)
                current.whatsapp_settings = {
                    "twilio_account_sid": "AC_TEST",
                    "twilio_auth_token": "auth_test",
                    "twilio_whatsapp_number": "whatsapp:+100",
                    "twilio_webhook_secret": "tenant-secret-123",
                }

    asyncio.run(_configure())

    monkeypatch.setattr(
        "app.api.routes.webhook.RequestValidator.validate",
        lambda self, url, form, signature: True,
    )

    async def _fake_process(self, tenant, from_phone, body, media_items=None):
        return "ok-tenant-webhook"

    monkeypatch.setattr(
        "app.services.conversation_service.ConversationService.process_message",
        _fake_process,
    )

    response = client.post(
        f"/webhook/whatsapp/{tenant_id}/tenant-secret-123",
        data={
            "From": "whatsapp:+5491111111111",
            "To": "whatsapp:+14155238886",
            "Body": "hola",
            "MessageSid": "SM123",
        },
        headers={"X-Twilio-Signature": "test-signature"},
    )
    assert response.status_code == 200
    assert "ok-tenant-webhook" in response.text


def test_webhook_by_tenant_rejects_invalid_secret(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Webhook 2", "whatsapp:+200"))

    async def _configure():
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                current = await session.get(Tenant, tenant_id)
                current.whatsapp_settings = {
                    "twilio_auth_token": "auth_test",
                    "twilio_webhook_secret": "correct-secret",
                }

    asyncio.run(_configure())

    response = client.post(
        f"/webhook/whatsapp/{tenant_id}/wrong-secret",
        data={
            "From": "whatsapp:+5491111111111",
            "To": "whatsapp:+14155238886",
            "Body": "hola",
            "MessageSid": "SM456",
        },
        headers={"X-Twilio-Signature": "test-signature"},
    )
    assert response.status_code == 403
