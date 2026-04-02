from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.core.security import hash_password
from app.models.turno import AppointmentStatus, EstadoTurno, TipoTurno
from app.services.appointment_service import AppointmentService
from app.tests.conftest import create_consultorio, create_paciente, create_tenant, create_user, login


async def _seed_turno(db_session, tenant_id: int, consultorio_id: int, paciente_id: int, start_at: datetime) -> int:
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
                start_at=start_at,
                timezone_name="America/Buenos_Aires",
                provider="manual",
                status=AppointmentStatus.CONFIRMED,
                estado=EstadoTurno.CONFIRMADO,
            )
            return turno.id


def test_dashboard_tenant_only_shows_own_data(client, db_session):
    tenant_1 = asyncio.run(create_tenant(db_session, "Tenant Uno", "whatsapp:+711"))
    tenant_2 = asyncio.run(create_tenant(db_session, "Tenant Dos", "whatsapp:+722"))
    consultorio_1 = asyncio.run(create_consultorio(db_session, tenant_1, "Consultorio Uno"))
    consultorio_2 = asyncio.run(create_consultorio(db_session, tenant_2, "Consultorio Dos"))
    paciente_1 = asyncio.run(create_paciente(db_session, tenant_1, "whatsapp:+7111"))
    paciente_2 = asyncio.run(create_paciente(db_session, tenant_2, "whatsapp:+7222"))

    password_hash = hash_password("secret-123")
    asyncio.run(create_user(db_session, "tenant1@test.com", password_hash, "TENANT_ADMIN", tenant_1))
    asyncio.run(create_user(db_session, "tenant2@test.com", password_hash, "TENANT_ADMIN", tenant_2))

    now = datetime.now(timezone.utc)
    asyncio.run(_seed_turno(db_session, tenant_1, consultorio_1, paciente_1, now))
    asyncio.run(_seed_turno(db_session, tenant_2, consultorio_2, paciente_2, now))

    login(client, "tenant1@test.com", "secret-123")
    response = client.get("/t/dashboard")
    assert response.status_code == 200
    assert "Juan Perez" in response.text
    assert "Consultorio Uno" in response.text
    assert "Consultorio Dos" not in response.text


def test_agenda_by_date_filters_turnos(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Agenda", "whatsapp:+733"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Consultorio Agenda"))
    paciente_a = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+7331"))
    paciente_b = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+7332"))

    password_hash = hash_password("secret-123")
    asyncio.run(create_user(db_session, "agenda@test.com", password_hash, "TENANT_ADMIN", tenant_id))

    async def _rename_patients():
        from app.models.paciente import Paciente

        async with db_session() as session:
            async with session.begin():
                first = await session.get(Paciente, paciente_a)
                second = await session.get(Paciente, paciente_b)
                first.nombre = "PacienteDiaDos"
                second.nombre = "PacienteDiaTres"

    asyncio.run(_rename_patients())
    asyncio.run(_seed_turno(db_session, tenant_id, consultorio_id, paciente_a, datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc)))
    asyncio.run(_seed_turno(db_session, tenant_id, consultorio_id, paciente_b, datetime(2026, 4, 3, 10, 0, tzinfo=timezone.utc)))

    login(client, "agenda@test.com", "secret-123")
    response = client.get("/t/appointments?date=2026-04-02")
    assert response.status_code == 200
    assert "PacienteDiaDos" in response.text
    assert "PacienteDiaTres" not in response.text
