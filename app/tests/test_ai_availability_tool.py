from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.integrations.interfaces import CalendarSlot
from app.models.consultorio import Consultorio, TipoConsultorio
from app.models.tenant import Tenant
from app.services.ai_tools import get_available_appointment_slots
from app.tests.conftest import create_tenant


def test_virtual_availability_tool_returns_normalized_slots(monkeypatch, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Tool Virtual", "whatsapp:+901"))

    async def seed():
        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                tenant.calendar_settings = {
                    "google_calendar_id": "secret-calendar-id",
                    "google_credentials_json": "{}",
                    "default_timezone": "America/Argentina/Buenos_Aires",
                }
                session.add(
                    Consultorio(
                        tenant_id=tenant_id,
                        nombre="Virtual",
                        tipo=TipoConsultorio.VIRTUAL,
                        proveedor_turnos="google",
                    )
                )

    async def fake_slots(self, tenant, consultorio, start, end):
        base = datetime(2026, 5, 28, 18, 30, tzinfo=timezone(timedelta(hours=-3)))
        return [
            CalendarSlot(
                slot_id=f"external-{index}",
                start_at=base + timedelta(days=index),
                end_at=base + timedelta(days=index, minutes=30),
                timezone="America/Argentina/Buenos_Aires",
                provider="google_calendar",
                calendar_id="secret-calendar-id",
            )
            for index in range(3)
        ]

    asyncio.run(seed())
    monkeypatch.setattr("app.services.calendar_service.CalendarService.list_available_slots", fake_slots)

    async def run():
        async with db_session() as session:
            return await get_available_appointment_slots(
                session,
                tenant_id=tenant_id,
                consultorio_type="virtual",
                patient_context={},
                preferences={},
                limit=2,
            )

    result = asyncio.run(run())

    assert result["ok"] is True
    assert result["source"] == "calendar"
    assert len(result["slots"]) == 2
    assert result["slots"][0]["label"].startswith("Jueves")
    assert result["slots"][0]["slot_id"] != "external-0"
    assert "secret-calendar-id" not in str(result)


def test_presential_availability_tool_returns_controlled_error_when_not_configured(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Tool Pres", "whatsapp:+902"))

    async def seed():
        async with db_session() as session:
            async with session.begin():
                session.add(
                    Consultorio(
                        tenant_id=tenant_id,
                        nombre="Presencial",
                        tipo=TipoConsultorio.PRESENCIAL,
                        proveedor_turnos="consultorio_movil",
                        configuracion_externa={},
                    )
                )

    asyncio.run(seed())

    async def run():
        async with db_session() as session:
            return await get_available_appointment_slots(
                session,
                tenant_id=tenant_id,
                consultorio_type="presential",
                patient_context={},
                preferences={},
                limit=5,
            )

    result = asyncio.run(run())

    assert result["ok"] is False
    assert result["source"] == "consultorio_movil"
    assert result["slots"] == []
    assert result["error"] == "consultorio_movil_not_configured"


def test_availability_tool_handles_provider_failure(monkeypatch, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Tool Fail", "whatsapp:+903"))

    async def seed():
        async with db_session() as session:
            async with session.begin():
                session.add(
                    Consultorio(
                        tenant_id=tenant_id,
                        nombre="Virtual",
                        tipo=TipoConsultorio.VIRTUAL,
                        proveedor_turnos="google",
                    )
                )

    async def fail_slots(self, tenant, consultorio, start, end):
        raise RuntimeError("boom secret-token")

    asyncio.run(seed())
    monkeypatch.setattr("app.services.calendar_service.CalendarService.list_available_slots", fail_slots)

    async def run():
        async with db_session() as session:
            return await get_available_appointment_slots(
                session,
                tenant_id=tenant_id,
                consultorio_type="virtual",
                patient_context={},
                preferences={},
                limit=5,
            )

    result = asyncio.run(run())

    assert result["ok"] is False
    assert result["error"] == "RuntimeError"
    assert "secret-token" not in str(result)


def test_availability_tool_respects_tenant_id(db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant Tool A", "whatsapp:+904"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant Tool B", "whatsapp:+905"))

    async def seed():
        async with db_session() as session:
            async with session.begin():
                session.add(
                    Consultorio(
                        tenant_id=tenant_b,
                        nombre="Virtual B",
                        tipo=TipoConsultorio.VIRTUAL,
                        proveedor_turnos="google",
                    )
                )

    asyncio.run(seed())

    async def run():
        async with db_session() as session:
            return await get_available_appointment_slots(
                session,
                tenant_id=tenant_a,
                consultorio_type="virtual",
                patient_context={},
                preferences={},
                limit=5,
            )

    result = asyncio.run(run())

    assert result["ok"] is False
    assert result["error"] == "consultorio_not_configured"
