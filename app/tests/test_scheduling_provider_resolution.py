from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app.integrations.interfaces import CalendarSlot
from app.services.appointment_service import AppointmentService
from app.services.calendar_service import CalendarService
from app.tests.conftest import create_consultorio, create_paciente, create_tenant


def test_calendar_service_uses_google_by_default(db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Google", "whatsapp:+991"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Consultorio Google"))

    async def _prepare():
        from app.models.consultorio import Consultorio
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                consultorio = await session.get(Consultorio, consultorio_id)
                tenant.calendar_settings = {"google_calendar_id": "calendar-123"}
                return tenant, consultorio

    tenant, consultorio = asyncio.run(_prepare())

    async def _fake_list(*args, **kwargs):
        now = datetime.now(timezone.utc)
        return [CalendarSlot("slot-1", now, now, "America/Argentina/Buenos_Aires", "google", "calendar-123")]

    monkeypatch.setattr("app.services.calendar_service.resolve_google_credentials", lambda settings: ("{}", None))
    monkeypatch.setattr("app.integrations.google_calendar_provider.GoogleCalendarProvider.list_available_slots", _fake_list)
    slots = asyncio.run(CalendarService().list_available_slots(tenant, consultorio, datetime.now(timezone.utc), datetime.now(timezone.utc)))
    assert slots[0].provider == "google"


def test_calendar_service_uses_cabildo_provider(db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Cabildo", "whatsapp:+992"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Consultorio Cabildo"))

    async def _prepare():
        from app.models.consultorio import Consultorio
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                consultorio = await session.get(Consultorio, consultorio_id)
                consultorio.proveedor_turnos = "consultorio_movil"
                consultorio.configuracion_externa = {
                    "cabildo": {"user": "u", "password": "p", "staff_id": "77", "days": 21}
                }
                return tenant, consultorio

    tenant, consultorio = asyncio.run(_prepare())

    from app.integrations.consultorio_movil import SlotSelection

    def _fake_list(*args, **kwargs):
        now = datetime.now(timezone.utc)
        return [
            SlotSelection(
                number=1,
                start_at=now,
                end_at=now,
                duration_minutes=30,
                timezone="America/Argentina/Buenos_Aires",
                label="Lunes 10:00",
            )
        ]

    monkeypatch.setattr("app.integrations.cabildo_provider.list_next_presential_slots", _fake_list)
    slots = asyncio.run(CalendarService().list_available_slots(tenant, consultorio, datetime.now(timezone.utc), datetime.now(timezone.utc)))
    assert slots[0].provider == "consultorio_movil"
    assert slots[0].calendar_id == "77"


def test_appointment_service_create_draft_uses_consultorio_provider(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Draft", "whatsapp:+993"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Consultorio Draft"))
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+9931"))

    async def _run():
        from app.models.consultorio import Consultorio
        from app.models.paciente import Paciente
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                consultorio = await session.get(Consultorio, consultorio_id)
                paciente = await session.get(Paciente, paciente_id)
                consultorio.proveedor_turnos = "consultorio_movil"
                consultorio.configuracion_externa = {
                    "cabildo": {"user": "u", "password": "p", "staff_id": "88", "days": 21}
                }
            request = SimpleNamespace(client=None, headers={})
            turno = await AppointmentService(session).create_draft(
                request=request,
                tenant=tenant,
                consultorio=consultorio,
                paciente=paciente,
                slot_id="slot-cab",
                start_at=datetime.now(timezone.utc),
                end_at=datetime.now(timezone.utc),
                timezone_name="America/Argentina/Buenos_Aires",
            )
            return turno

    turno = asyncio.run(_run())
    assert turno.external_calendar_provider == "consultorio_movil"
    assert turno.external_calendar_id == "88"
