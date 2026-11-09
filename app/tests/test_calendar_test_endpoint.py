from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.core.security import hash_password
from app.integrations.interfaces import CalendarSlot
from app.models.user import UserRole
from app.tests.conftest import create_consultorio, create_tenant, create_user, login


def test_calendar_test_endpoint_returns_slots(client, db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Cal", "whatsapp:+777"))
    asyncio.run(create_consultorio(db_session, tenant_id, "Virtual"))
    password_hash = hash_password("secret-123")
    asyncio.run(
        create_user(db_session, "tenantcal@test.com", password_hash, UserRole.TENANT_ADMIN.value, tenant_id)
    )

    async def _set_calendar_settings():
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                tenant.calendar_settings = {
                    "google_calendar_id": "calendar-123",
                    "default_timezone": "America/Argentina/Buenos_Aires",
                }

    asyncio.run(_set_calendar_settings())

    async def _fake_list_available_slots(*args, **kwargs):
        now = datetime.now(timezone.utc)
        return [
            CalendarSlot(
                slot_id="slot-1",
                start_at=now,
                end_at=now,
                timezone="America/Argentina/Buenos_Aires",
                provider="google",
                calendar_id="calendar-123",
            )
        ]

    from app.services.calendar_service import CalendarService

    monkeypatch.setattr(CalendarService, "list_available_slots", _fake_list_available_slots)

    login(client, "tenantcal@test.com", "secret-123")
    response = client.get("/t/settings/calendar/test")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["items"][0]["slot_id"] == "slot-1"


def test_calendar_settings_manages_global_catalog_and_preserves_existing_fallback(
    client, db_session, monkeypatch
):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Cal Settings", "whatsapp:+778"))
    password_hash = hash_password("secret-123")
    asyncio.run(
        create_user(db_session, "tenantcalsettings@test.com", password_hash, UserRole.TENANT_ADMIN.value, tenant_id)
    )

    async def _set_calendar_settings():
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                tenant.calendar_settings = {
                    "google_calendar_id": "legacy-calendar-123",
                    "default_timezone": "America/Argentina/Buenos_Aires",
                    "calendar_tags": ["[TURNO DISPONIBLE]"],
                }

    asyncio.run(_set_calendar_settings())
    login(client, "tenantcalsettings@test.com", "secret-123")
    page = client.get("/t/settings/calendar")
    csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]

    assert "Google Calendar ID fallback opcional" not in page.text
    assert 'name="google_calendar_id"' not in page.text
    assert 'name="google_calendar_ids"' in page.text
    assert "Calendarios disponibles" in page.text

    from app.services.calendar_service import CalendarService

    monkeypatch.setattr(
        CalendarService,
        "register_google_calendars",
        lambda self, calendar_settings, calendar_ids: [
            {"id": calendar_id, "summary": f"Agenda {index}", "access_role": "writer"}
            for index, calendar_id in enumerate(calendar_ids, start=1)
        ],
    )

    response = client.post(
        "/t/settings/calendar",
        data={
            "csrf_token": csrf,
            "default_timezone": "America/Argentina/Buenos_Aires",
            "calendar_tags": "[TURNO DISPONIBLE]",
            "google_credentials_json": '{"client_email":"svc@example.com"}',
            "google_calendar_ids": "agenda-uno@example.com\nagenda-dos@example.com",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def _get_calendar_settings():
        from app.models.tenant import Tenant

        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            return tenant.calendar_settings

    settings = asyncio.run(_get_calendar_settings())
    assert settings["google_calendar_id"] == "legacy-calendar-123"
    assert settings["google_calendar_ids"] == [
        "agenda-uno@example.com",
        "agenda-dos@example.com",
    ]
    assert settings["google_calendars"] == [
        {"id": "agenda-uno@example.com", "summary": "Agenda 1", "access_role": "writer"},
        {"id": "agenda-dos@example.com", "summary": "Agenda 2", "access_role": "writer"},
    ]
