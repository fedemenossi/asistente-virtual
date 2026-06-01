from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.security import hash_password
from app.models.audit_log import AuditLog
from app.models.consultorio import TipoConsultorio
from app.models.turno import AppointmentStatus, EstadoTurno, TipoTurno, Turno
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


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


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


def test_agenda_filters_turnos_by_date_range(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Agenda Range", "whatsapp:+741"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Consultorio Range"))
    paciente_a = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+7411", nombre="PacienteDos"))
    paciente_b = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+7412", nombre="PacienteTres"))
    paciente_c = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+7413", nombre="PacienteCinco"))

    password_hash = hash_password("secret-123")
    asyncio.run(create_user(db_session, "agenda-range@test.com", password_hash, "TENANT_ADMIN", tenant_id))

    asyncio.run(_seed_turno(db_session, tenant_id, consultorio_id, paciente_a, datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc)))
    asyncio.run(_seed_turno(db_session, tenant_id, consultorio_id, paciente_b, datetime(2026, 4, 3, 10, 0, tzinfo=timezone.utc)))
    asyncio.run(_seed_turno(db_session, tenant_id, consultorio_id, paciente_c, datetime(2026, 4, 5, 10, 0, tzinfo=timezone.utc)))

    login(client, "agenda-range@test.com", "secret-123")
    response = client.get("/t/appointments?date_from=2026-04-02&date_to=2026-04-03")

    assert response.status_code == 200
    assert "Agenda por rango" in response.text
    assert "Turnos asignados" in response.text
    assert "PacienteDos" in response.text
    assert "PacienteTres" in response.text
    assert "PacienteCinco" not in response.text


def test_agenda_searches_assigned_turnos_by_patient_dni(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Agenda Search", "whatsapp:+742"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Consultorio Search"))
    paciente_a = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "whatsapp:+7421",
            nombre="Buscado",
            apellido="Correcto",
            dni="30111222",
        )
    )
    paciente_b = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "whatsapp:+7422",
            nombre="NoBuscado",
            apellido="Incorrecto",
            dni="40999888",
        )
    )

    password_hash = hash_password("secret-123")
    asyncio.run(create_user(db_session, "agenda-search@test.com", password_hash, "TENANT_ADMIN", tenant_id))

    asyncio.run(_seed_turno(db_session, tenant_id, consultorio_id, paciente_a, datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc)))
    asyncio.run(_seed_turno(db_session, tenant_id, consultorio_id, paciente_b, datetime(2026, 4, 2, 11, 0, tzinfo=timezone.utc)))

    login(client, "agenda-search@test.com", "secret-123")
    response = client.get("/t/appointments?date_from=2026-04-02&date_to=2026-04-02&q=30111222")

    assert response.status_code == 200
    assert "Buscado" in response.text
    assert "Correcto" in response.text
    assert "NoBuscado" not in response.text


def test_appointment_patient_search_filters_by_tenant_and_dni(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Patient Search", "whatsapp:+743"))
    other_tenant_id = asyncio.run(create_tenant(db_session, "Tenant Patient Search Other", "whatsapp:+744"))
    asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "whatsapp:+7431",
            nombre="Maria",
            apellido="Lopez",
            dni="28077008",
        )
    )
    asyncio.run(
        create_paciente(
            db_session,
            other_tenant_id,
            "whatsapp:+7441",
            nombre="Otro",
            apellido="Paciente",
            dni="28077008",
        )
    )
    password_hash = hash_password("secret-123")
    asyncio.run(create_user(db_session, "patient-search@test.com", password_hash, "TENANT_ADMIN", tenant_id))

    login(client, "patient-search@test.com", "secret-123")
    response = client.get("/t/appointments/patients/search?q=28077008")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["label"] == "Lopez, Maria"
    assert payload["items"][0]["dni"] == "28077008"


def test_appointments_list_shows_live_google_calendar_events(client, db_session, monkeypatch):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Agenda Google",
            "whatsapp:+737",
            calendar_settings={"google_credentials_json": "{}"},
        )
    )
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Google",
            proveedor_turnos="google",
            configuracion_externa={
                "google_calendar": {
                    "calendar_id": "calendar-google",
                    "timezone": "America/Argentina/Buenos_Aires",
                }
            },
        )
    )
    password_hash = hash_password("secret-123")
    asyncio.run(create_user(db_session, "agenda-google@test.com", password_hash, "TENANT_ADMIN", tenant_id))

    async def _fake_events(self, tenant, consultorio, start, end):
        assert tenant.id == tenant_id
        assert consultorio.id == consultorio_id
        assert start.date().isoformat() == "2026-04-02"
        assert end.date().isoformat() == "2026-04-04"
        start_at = datetime(2026, 4, 2, 9, 0, tzinfo=timezone(timedelta(hours=-3)))
        return [
            {
                "event_id": "evt-1",
                "summary": "[TURNO DISPONIBLE]",
                "start_at": start_at,
                "end_at": start_at + timedelta(minutes=30),
                "timezone": "America/Argentina/Buenos_Aires",
                "status": "available",
                "provider": "google",
                "calendar_id": "calendar-google",
                "html_link": None,
                "generated_by_app": True,
            }
        ]

    monkeypatch.setattr("app.services.calendar_service.CalendarService.list_calendar_events", _fake_events)

    login(client, "agenda-google@test.com", "secret-123")
    response = client.get(f"/t/appointments?date_from=2026-04-02&date_to=2026-04-03&consultorio_id={consultorio_id}")

    assert response.status_code == 200
    assert "Agenda Google en vivo" in response.text
    assert "[TURNO DISPONIBLE]" in response.text
    assert "Disponible" in response.text
    assert "data-assign-open" in response.text
    assert "data-assign-modal" in response.text


def test_assign_google_live_event_creates_local_turno(client, db_session, monkeypatch):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Assign Google",
            "whatsapp:+738",
            calendar_settings={"google_credentials_json": "{}"},
        )
    )
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Assign",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={
                "google_calendar": {
                    "calendar_id": "calendar-assign",
                    "timezone": "America/Argentina/Buenos_Aires",
                }
            },
        )
    )
    paciente_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "whatsapp:+7381",
            nombre="Ana",
            apellido="Gomez",
            dni="28077008",
        )
    )
    password_hash = hash_password("secret-123")
    user_id = asyncio.run(create_user(db_session, "assign-google@test.com", password_hash, "TENANT_ADMIN", tenant_id))

    async def _fake_reserve(self, tenant, consultorio, slot_id, patient, metadata):
        assert tenant.id == tenant_id
        assert consultorio.id == consultorio_id
        assert slot_id == "evt-assign"
        assert patient.id == paciente_id
        assert metadata["source"] == "tenant_panel"
        return {
            "event_id": "evt-assign",
            "calendar_id": "calendar-assign",
            "start_at": "2026-04-02T09:00:00-03:00",
            "end_at": "2026-04-02T09:30:00-03:00",
            "timezone": "America/Argentina/Buenos_Aires",
            "html_link": None,
            "meet_link": "https://meet.google.com/demo",
        }

    monkeypatch.setattr("app.services.calendar_service.CalendarService.reserve_slot", _fake_reserve)

    login(client, "assign-google@test.com", "secret-123")
    form_page = client.get(f"/t/appointments?date=2026-04-02&consultorio_id={consultorio_id}")
    csrf_token = _csrf_from(form_page.text)

    response = client.post(
        "/t/appointments/google/assign",
        data={
            "csrf_token": csrf_token,
            "consultorio_id": str(consultorio_id),
            "event_id": "evt-assign",
            "patient_id": str(paciente_id),
            "date": "2026-04-02",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/t/appointments/")

    async def _load_turno():
        async with db_session() as session:
            return await session.scalar(
                select(Turno).where(
                    Turno.tenant_id == tenant_id,
                    Turno.paciente_id == paciente_id,
                    Turno.external_event_id == "evt-assign",
                )
            )

    async def _load_audit_log():
        async with db_session() as session:
            return await session.scalar(
                select(AuditLog).where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.action == "assign_google_slot",
                )
            )

    turno = asyncio.run(_load_turno())
    assert turno is not None
    assert turno.consultorio_id == consultorio_id
    assert turno.status == AppointmentStatus.CONFIRMED
    assert turno.estado == EstadoTurno.CONFIRMADO
    assert turno.tipo == TipoTurno.VIRTUAL
    assert turno.external_calendar_provider == "google"
    assert turno.external_calendar_id == "calendar-assign"
    assert turno.referencia_externa == "https://meet.google.com/demo"
    audit = asyncio.run(_load_audit_log())
    assert audit is not None
    assert audit.user_id == user_id


def test_assign_google_live_event_rejects_other_tenant_patient(client, db_session, monkeypatch):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Assign Isolation A",
            "whatsapp:+739",
            calendar_settings={"google_credentials_json": "{}"},
        )
    )
    other_tenant_id = asyncio.run(create_tenant(db_session, "Tenant Assign Isolation B", "whatsapp:+740"))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Isolation",
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": {"calendar_id": "calendar-isolation"}},
        )
    )
    other_patient_id = asyncio.run(create_paciente(db_session, other_tenant_id, "whatsapp:+7401"))
    password_hash = hash_password("secret-123")
    asyncio.run(create_user(db_session, "assign-isolation@test.com", password_hash, "TENANT_ADMIN", tenant_id))
    called = {"reserve": False}

    async def _fake_reserve(self, tenant, consultorio, slot_id, patient, metadata):
        called["reserve"] = True
        return {}

    monkeypatch.setattr("app.services.calendar_service.CalendarService.reserve_slot", _fake_reserve)

    login(client, "assign-isolation@test.com", "secret-123")
    form_page = client.get(f"/t/appointments?date=2026-04-02&consultorio_id={consultorio_id}")
    csrf_token = _csrf_from(form_page.text)

    response = client.post(
        "/t/appointments/google/assign",
        data={
            "csrf_token": csrf_token,
            "consultorio_id": str(consultorio_id),
            "event_id": "evt-isolation",
            "patient_id": str(other_patient_id),
            "date": "2026-04-02",
        },
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert called["reserve"] is False


def test_legacy_turnos_redirects_to_canonical_appointments(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Legacy Turnos", "whatsapp:+734"))
    password_hash = hash_password("secret-123")
    asyncio.run(create_user(db_session, "legacy-turnos@test.com", password_hash, "TENANT_ADMIN", tenant_id))

    login(client, "legacy-turnos@test.com", "secret-123")

    response = client.get("/t/turnos?date=2026-04-02", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/t/appointments?date=2026-04-02"


def test_legacy_turnos_detail_redirects_to_canonical_appointment_detail(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Legacy Detail", "whatsapp:+735"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Consultorio Legacy"))
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+7351"))
    turno_id = asyncio.run(
        _seed_turno(
            db_session,
            tenant_id,
            consultorio_id,
            paciente_id,
            datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
        )
    )
    password_hash = hash_password("secret-123")
    asyncio.run(create_user(db_session, "legacy-detail@test.com", password_hash, "TENANT_ADMIN", tenant_id))

    login(client, "legacy-detail@test.com", "secret-123")

    response = client.get(f"/t/turnos/{turno_id}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/t/appointments/{turno_id}"


def test_sidebar_points_turnos_to_canonical_appointments_only(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sidebar Turnos", "whatsapp:+736"))
    password_hash = hash_password("secret-123")
    asyncio.run(create_user(db_session, "sidebar-turnos@test.com", password_hash, "TENANT_ADMIN", tenant_id))

    login(client, "sidebar-turnos@test.com", "secret-123")
    response = client.get("/t/dashboard")

    assert response.status_code == 200
    assert 'href="/t/appointments"' in response.text
    assert 'href="/t/turnos"' not in response.text
    assert "Turnos reales" not in response.text
