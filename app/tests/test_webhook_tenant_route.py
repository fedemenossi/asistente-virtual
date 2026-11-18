from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from twilio.request_validator import RequestValidator

from app.core.security import hash_password
from app.core.timezone import now_ba
from app.models.consultorio import TipoConsultorio
from app.models.user import UserRole
from app.tests.conftest import (
    create_consultorio,
    create_paciente,
    create_tenant,
    create_user,
    get_tenant,
    login,
)


class _GoogleRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _GoogleEvents:
    def __init__(self, available_event: dict):
        self.available_event = available_event
        self.reserved_event = None
        self.patch_count = 0

    def list(self, **kwargs):
        return _GoogleRequest({"items": [self.available_event]})

    def get(self, **kwargs):
        return _GoogleRequest(self.available_event)

    def patch(self, **kwargs):
        self.patch_count += 1
        body = kwargs["body"]
        self.reserved_event = {
            **self.available_event,
            **body,
            "htmlLink": "https://calendar.google.com/event?eid=webhook-event-1",
            "hangoutLink": "https://meet.google.com/web-hook-test",
        }
        return _GoogleRequest(self.reserved_event)


class _GoogleService:
    def __init__(self, available_event: dict):
        self.google_events = _GoogleEvents(available_event)

    def events(self):
        return self.google_events


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

    captured = {}

    async def _fake_process(self, tenant, from_phone, body, media_items=None):
        captured.update(
            {
                "tenant_id": tenant.id,
                "from_phone": from_phone,
                "body": body,
                "media_items": media_items,
            }
        )
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
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.test/media/1",
            "MediaContentType0": "image/jpeg",
        },
        headers={"X-Twilio-Signature": "test-signature"},
    )
    assert response.status_code == 200
    assert "ok-tenant-webhook" in response.text
    assert captured == {
        "tenant_id": tenant_id,
        "from_phone": "whatsapp:+5491111111111",
        "body": "hola",
        "media_items": [
            {
                "index": 0,
                "url": "https://api.twilio.test/media/1",
                "content_type": "image/jpeg",
            }
        ],
    }


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


def test_whatsapp_webhook_falls_back_to_global_twilio_token(client, db_session, monkeypatch):
    tenant_id = asyncio.run(
        create_tenant(db_session, "Tenant Global Token", "whatsapp:+14155238886")
    )

    async def _configure():
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                current = await session.get(Tenant, tenant_id)
                current.whatsapp_settings = {
                    "twilio_account_sid": "AC_TENANT",
                    "twilio_whatsapp_number": "whatsapp:+14155238886",
                }

    asyncio.run(_configure())

    captured = {}

    class _FakeValidator:
        def __init__(self, token: str) -> None:
            captured["token"] = token

        def validate(self, url, form, signature):
            return True

    monkeypatch.setattr("app.api.routes.webhook.RequestValidator", _FakeValidator)

    async def _fake_process(self, tenant, from_phone, body, media_items=None):
        return "ok-global-token"

    monkeypatch.setattr(
        "app.services.conversation_service.ConversationService.process_message",
        _fake_process,
    )

    response = client.post(
        "/webhook/whatsapp",
        data={
            "From": "whatsapp:+5491111111111",
            "To": "whatsapp:+14155238886",
            "Body": "hola",
            "MessageSid": "SM789",
        },
        headers={"X-Twilio-Signature": "test-signature"},
    )

    assert response.status_code == 200
    assert "ok-global-token" in response.text
    assert captured["token"] == "test"


def test_real_whatsapp_webhook_confirms_google_slot_and_exposes_local_appointment(
    client,
    db_session,
    monkeypatch,
):
    auth_token = "real-webhook-test-token"
    whatsapp_number = "whatsapp:+14155239991"
    patient_phone = "5491199910001"
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Real Booking",
            whatsapp_number,
            calendar_settings={
                "google_credentials_json": "{}",
                "default_timezone": "America/Argentina/Buenos_Aires",
            },
            whatsapp_settings={
                "twilio_auth_token": auth_token,
                "twilio_whatsapp_number": whatsapp_number,
            },
        )
    )
    asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Consultorio virtual",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={
                "google_calendar": {"calendar_id": "calendar-real-booking"}
            },
        )
    )
    asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            patient_phone,
            nombre="Ana",
            apellido="Webhook",
            dni="30111222",
            obra_social="Particular",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "tenant-real-booking@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    start = (now_ba() + timedelta(days=2)).replace(
        hour=10,
        minute=30,
        second=0,
        microsecond=0,
    )
    google_service = _GoogleService(
        {
            "id": "webhook-event-1",
            "summary": "[DISPONIBLE] Consulta",
            "start": {
                "dateTime": start.isoformat(),
                "timeZone": "America/Argentina/Buenos_Aires",
            },
            "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()},
            "extendedProperties": {"private": {"slot_status": "available"}},
        }
    )
    monkeypatch.setattr(
        "app.integrations.google_calendar_provider.GoogleCalendarProvider._build_service",
        lambda self: google_service,
    )

    webhook_url = "http://testserver/webhook/whatsapp"
    validator = RequestValidator(auth_token)
    message_number = 0

    def send(message: str, *, message_sid: str | None = None):
        nonlocal message_number
        message_number += 1
        form = {
            "From": f"whatsapp:+{patient_phone}",
            "To": whatsapp_number,
            "Body": message,
            "MessageSid": message_sid or f"SM-REAL-{message_number}",
        }
        signature = validator.compute_signature(webhook_url, form)
        response = client.post(
            "/webhook/whatsapp",
            data=form,
            headers={"X-Twilio-Signature": signature},
        )
        assert response.status_code == 200
        return response.text

    assert "para vos" in send("quiero un turno virtual").lower()
    assert "Es primera vez" in send("1")
    assert "turnos virtuales disponibles" in send("2")
    assert "Confirmas que queres reservarlo" in send("1")
    confirmation_sid = "SM-REAL-CONFIRMATION"
    confirmation = send("1", message_sid=confirmation_sid)
    repeated_confirmation = send("1", message_sid=confirmation_sid)

    assert "Turno confirmado" in confirmation
    assert repeated_confirmation == confirmation
    assert "https://calendar.google.com/event?eid=webhook-event-1" in confirmation
    assert "https://meet.google.com/web-hook-test" in confirmation
    assert google_service.google_events.reserved_event is not None
    assert google_service.google_events.patch_count == 1

    login(client, "tenant-real-booking@test.com", "secret-123")
    agenda = client.get(f"/t/appointments?date={start.date().isoformat()}")

    assert agenda.status_code == 200
    assert "Ana Webhook" in agenda.text
    assert "Consultorio virtual" in agenda.text
    assert "Confirmado" in agenda.text
    assert "Reservado" in agenda.text
    detail_path = re.search(r'href="(/t/appointments/\d+)"', agenda.text)
    assert detail_path is not None

    detail = client.get(detail_path.group(1))
    assert detail.status_code == 200
    assert "webhook-event-1" in detail.text
    assert 'href="https://meet.google.com/web-hook-test"' in detail.text
