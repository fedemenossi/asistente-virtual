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
