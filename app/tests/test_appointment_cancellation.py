from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.core.timezone import now_ba
from app.integrations.google_calendar_provider import GoogleCalendarProvider
from app.models.turno import AppointmentStatus
from app.services.appointment_service import AppointmentService
from app.tests.conftest import create_consultorio, create_paciente, create_tenant, create_turno


def test_google_cancellation_releases_slot_once_and_preserves_trace(db_session, monkeypatch):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Google Cancel",
            "whatsapp:+740",
            calendar_settings={"google_credentials_json": "{}"},
        )
    )
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Google Palermo",
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": {"calendar_id": "calendar@example.com"}},
        )
    )
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "5491174000001"))
    turno_id = asyncio.run(
        create_turno(
            db_session,
            paciente_id,
            consultorio_id,
            fecha_hora=now_ba().replace(tzinfo=None) + timedelta(days=2),
            status=AppointmentStatus.CONFIRMED,
            external_calendar_provider="google",
            external_calendar_id="calendar@example.com",
            external_event_id="google-event-22",
            external_status="reserved",
        )
    )
    patches: list[dict] = []

    class _Executable:
        def __init__(self, value):
            self._value = value

        def execute(self):
            return self._value

    class _Events:
        def get(self, **kwargs):
            return _Executable(
                {"extendedProperties": {"private": {"slot_status": "reserved"}}}
            )

        def patch(self, **kwargs):
            patches.append(kwargs)
            return _Executable({})

    class _GoogleService:
        def events(self):
            return _Events()

    google_service = _GoogleService()
    monkeypatch.setattr(GoogleCalendarProvider, "_build_service", lambda self: google_service)

    async def run():
        from app.models.consultorio import Consultorio
        from app.models.tenant import Tenant
        from app.models.turno import Turno

        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            consultorio = await session.get(Consultorio, consultorio_id)
            turno = await session.get(Turno, turno_id)
            service = AppointmentService(session)

            await service.cancel_turno(None, tenant, consultorio, turno)
            await service.cancel_turno(None, tenant, consultorio, turno)
            await session.refresh(turno)
            return turno

    turno = asyncio.run(run())

    assert len(patches) == 1
    assert patches[0]["eventId"] == "google-event-22"
    assert patches[0]["body"]["extendedProperties"]["private"]["slot_status"] == "available"
    assert turno.status == AppointmentStatus.CANCELLED
    assert turno.external_calendar_provider == "google"
    assert turno.external_calendar_id == "calendar@example.com"
    assert turno.external_event_id == "google-event-22"
    assert turno.cancelled_at is not None
    assert turno.cancellation_reason == "cancelled_by_user"


def test_drapp_failed_cancellation_can_be_retried_without_repeating_success(db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant DrApp Cancel", "whatsapp:+742"))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "DrApp Centro",
            proveedor_turnos="drapp",
            configuracion_externa={"drapp": {"staff_id": "22"}},
        )
    )
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "5491174200001"))
    turno_id = asyncio.run(
        create_turno(
            db_session,
            paciente_id,
            consultorio_id,
            fecha_hora=now_ba().replace(tzinfo=None) + timedelta(days=2),
            status=AppointmentStatus.CONFIRMED,
            external_calendar_provider="drapp",
            external_calendar_id="22",
            external_event_id="drapp-event-22",
            external_status="reserved",
        )
    )
    calls: list[str] = []

    def cancel_drapp(tenant, consultorio, external_event_id):
        calls.append(external_event_id)
        if len(calls) == 1:
            raise RuntimeError("drapp unavailable")
        return {"status": "cancelled"}

    monkeypatch.setattr(
        "app.integrations.drapp_provider.consultorio_movil.cancel_presential_slot",
        cancel_drapp,
    )

    async def run():
        from app.models.consultorio import Consultorio
        from app.models.tenant import Tenant
        from app.models.turno import Turno

        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            consultorio = await session.get(Consultorio, consultorio_id)
            turno = await session.get(Turno, turno_id)
            service = AppointmentService(session)

            with pytest.raises(RuntimeError, match="drapp unavailable"):
                await service.cancel_turno(None, tenant, consultorio, turno)
            assert turno.status == AppointmentStatus.CONFIRMED
            assert turno.external_status == "cancellation_failed"

            await service.cancel_turno(None, tenant, consultorio, turno)
            await service.cancel_turno(None, tenant, consultorio, turno)
            await session.refresh(turno)
            return turno

    turno = asyncio.run(run())

    assert calls == ["drapp-event-22", "drapp-event-22"]
    assert turno.status == AppointmentStatus.CANCELLED
    assert turno.external_status == "cancelled"
    assert turno.external_calendar_provider == "drapp"
    assert turno.external_event_id == "drapp-event-22"
