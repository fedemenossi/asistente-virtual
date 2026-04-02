from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from app.models.turno import AppointmentStatus, TipoTurno
from app.services.appointment_service import AppointmentService
from app.tests.conftest import create_consultorio, create_paciente, create_tenant


def test_create_local_turno(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Local", "whatsapp:+881"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Sede Local"))
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+8811"))

    async def _run():
        from app.models.consultorio import Consultorio
        from app.models.paciente import Paciente
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                consultorio = await session.get(Consultorio, consultorio_id)
                paciente = await session.get(Paciente, paciente_id)
                turno = await AppointmentService(session).create_local_turno(
                    tenant=tenant,
                    consultorio=consultorio,
                    paciente=paciente,
                    tipo=TipoTurno.PRESENCIAL,
                    start_at=datetime.now(timezone.utc),
                    end_at=datetime.now(timezone.utc) + timedelta(minutes=30),
                    timezone_name="America/Argentina/Buenos_Aires",
                    provider="manual",
                    external_status="draft",
                    status=AppointmentStatus.DRAFT,
                    notes="Turno creado desde test",
                )
                return turno.id

    turno_id = asyncio.run(_run())

    async def _fetch():
        from app.models.turno import Turno

        async with db_session() as session:
            return await session.get(Turno, turno_id)

    turno = asyncio.run(_fetch())
    assert turno is not None
    assert turno.tenant_id == tenant_id
    assert turno.provider == "manual"
    assert turno.notes == "Turno creado desde test"


def test_list_turnos_by_tenant_without_mixing(db_session):
    tenant_1 = asyncio.run(create_tenant(db_session, "Tenant Uno", "whatsapp:+882"))
    tenant_2 = asyncio.run(create_tenant(db_session, "Tenant Dos", "whatsapp:+883"))
    consultorio_1 = asyncio.run(create_consultorio(db_session, tenant_1, "Sede Uno"))
    consultorio_2 = asyncio.run(create_consultorio(db_session, tenant_2, "Sede Dos"))
    paciente_1 = asyncio.run(create_paciente(db_session, tenant_1, "whatsapp:+8821"))
    paciente_2 = asyncio.run(create_paciente(db_session, tenant_2, "whatsapp:+8831"))

    target_day = date(2026, 4, 2)

    async def _run():
        from app.models.consultorio import Consultorio
        from app.models.paciente import Paciente
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                tenant_one = await session.get(Tenant, tenant_1)
                consultorio_one = await session.get(Consultorio, consultorio_1)
                paciente_one = await session.get(Paciente, paciente_1)
                tenant_two = await session.get(Tenant, tenant_2)
                consultorio_two = await session.get(Consultorio, consultorio_2)
                paciente_two = await session.get(Paciente, paciente_2)
                await AppointmentService(session).create_local_turno(
                    tenant=tenant_one,
                    consultorio=consultorio_one,
                    paciente=paciente_one,
                    tipo=TipoTurno.PRESENCIAL,
                    start_at=datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
                    timezone_name="America/Buenos_Aires",
                    provider="manual",
                )
                await AppointmentService(session).create_local_turno(
                    tenant=tenant_two,
                    consultorio=consultorio_two,
                    paciente=paciente_two,
                    tipo=TipoTurno.PRESENCIAL,
                    start_at=datetime(2026, 4, 2, 11, 0, tzinfo=timezone.utc),
                    timezone_name="America/Buenos_Aires",
                    provider="manual",
                )

        async with db_session() as session:
            service = AppointmentService(session)
            rows = await service.list_turnos_by_tenant_and_date(tenant_1, target_day)
            return rows

    rows = asyncio.run(_run())
    assert len(rows) == 1
    assert rows[0].tenant_id == tenant_1


def test_get_daily_agenda_filters_by_date(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Agenda", "whatsapp:+884"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Sede Agenda"))
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+8841"))

    async def _run():
        from app.models.consultorio import Consultorio
        from app.models.paciente import Paciente
        from app.models.tenant import Tenant

        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                consultorio = await session.get(Consultorio, consultorio_id)
                paciente = await session.get(Paciente, paciente_id)
                await AppointmentService(session).create_local_turno(
                    tenant=tenant,
                    consultorio=consultorio,
                    paciente=paciente,
                    tipo=TipoTurno.VIRTUAL,
                    start_at=datetime(2026, 4, 2, 9, 0, tzinfo=timezone.utc),
                    timezone_name="America/Buenos_Aires",
                    provider="manual",
                )
                await AppointmentService(session).create_local_turno(
                    tenant=tenant,
                    consultorio=consultorio,
                    paciente=paciente,
                    tipo=TipoTurno.VIRTUAL,
                    start_at=datetime(2026, 4, 3, 9, 0, tzinfo=timezone.utc),
                    timezone_name="America/Buenos_Aires",
                    provider="manual",
                )

        async with db_session() as session:
            service = AppointmentService(session)
            return await service.get_daily_agenda(tenant_id, date(2026, 4, 2))

    rows = asyncio.run(_run())
    assert len(rows) == 1
    assert rows[0].fecha_hora.date().isoformat() == "2026-04-02"
