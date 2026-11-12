from __future__ import annotations

import asyncio
import re

from sqlalchemy import select

from app.models.paciente import Paciente
from app.tests.conftest import (
    create_paciente,
    create_tenant,
    login_as_tenant_admin,
)


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_tenant_chat_simulator_uses_authenticated_tenant_and_accepts_new_patient(
    client, db_session
):
    tenant_id = asyncio.run(
        create_tenant(db_session, "Consultorio propio", "whatsapp:+5491100000101")
    )
    other_tenant_id = asyncio.run(
        create_tenant(db_session, "Consultorio ajeno", "whatsapp:+5491100000102")
    )
    patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491111111111",
            nombre="Ana",
            apellido="Propia",
        )
    )
    other_patient_id = asyncio.run(
        create_paciente(
            db_session,
            other_tenant_id,
            "5491122222222",
            nombre="Beto",
            apellido="Ajeno",
        )
    )
    asyncio.run(login_as_tenant_admin(client, db_session, tenant_id))

    page = client.get("/t/chat-simulator")

    assert page.status_code == 200
    assert "Ana Propia" in page.text
    assert "Beto Ajeno" not in page.text
    assert 'data-chat-tenant' not in page.text
    assert 'data-chat-phone' in page.text
    assert 'data-chat-patient' in page.text
    assert "Paciente nuevo" in page.text
    csrf_token = _csrf(page.text)

    missing_identity = client.post(
        "/t/chat-simulator/api",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "Hola"},
    )
    assert missing_identity.status_code == 400
    assert missing_identity.json()["error"] == "Ingresa el telefono del paciente nuevo."

    foreign_patient = client.post(
        "/t/chat-simulator/api",
        headers={"X-CSRF-Token": csrf_token},
        json={"patient_id": other_patient_id, "message": "Hola"},
    )
    assert foreign_patient.status_code == 404

    response = client.post(
        "/t/chat-simulator/api",
        headers={"X-CSRF-Token": csrf_token},
        json={"patient_id": patient_id, "message": "Hola"},
    )
    assert response.status_code == 200
    assert response.json()["reply"]


def test_tenant_chat_simulator_registers_new_patient_in_authenticated_tenant(
    client, db_session
):
    tenant_id = asyncio.run(
        create_tenant(db_session, "Consultorio altas", "whatsapp:+5491100000104")
    )
    other_tenant_id = asyncio.run(
        create_tenant(db_session, "Consultorio ajeno", "whatsapp:+5491100000105")
    )
    new_phone = "5491144444444"
    asyncio.run(
        create_paciente(
            db_session,
            other_tenant_id,
            new_phone,
            nombre="Paciente",
            apellido="De otro tenant",
        )
    )
    asyncio.run(login_as_tenant_admin(client, db_session, tenant_id))
    page = client.get("/t/chat-simulator")
    csrf_token = _csrf(page.text)

    messages = [
        "Hola",
        "1",
        "30123123",
        "Lucia",
        "Suarez",
        "lucia@test.com",
        "1",
        "OSDE",
        "A123",
    ]
    responses = []
    for message in messages:
        response = client.post(
            "/t/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={"from_number": new_phone, "message": message},
        )
        assert response.status_code == 200
        responses.append(response.json()["reply"])

    assert "Hola Lucia Suarez" in responses[-1]

    async def get_registered_patients():
        async with db_session() as session:
            result = await session.execute(
                select(Paciente).where(
                    Paciente.telefono == new_phone,
                    Paciente.deleted_at.is_(None),
                )
            )
            return list(result.scalars().all())

    registered = asyncio.run(get_registered_patients())
    assert {patient.tenant_id for patient in registered} == {
        tenant_id,
        other_tenant_id,
    }
    created = next(patient for patient in registered if patient.tenant_id == tenant_id)
    assert created.nombre == "Lucia"
    assert created.apellido == "Suarez"


def test_tenant_chat_simulator_reset_uses_selected_patient(client, db_session):
    tenant_id = asyncio.run(
        create_tenant(db_session, "Consultorio", "whatsapp:+5491100000103")
    )
    patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491133333333",
            nombre="Clara",
            apellido="Paciente",
        )
    )
    asyncio.run(login_as_tenant_admin(client, db_session, tenant_id))
    page = client.get("/t/chat-simulator")

    response = client.post(
        "/t/chat-simulator/reset",
        data={"csrf_token": _csrf(page.text), "patient_id": patient_id},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/t/chat-simulator"


def test_tenant_chat_simulator_reset_accepts_new_patient_phone(client, db_session):
    tenant_id = asyncio.run(
        create_tenant(db_session, "Consultorio", "whatsapp:+5491100000106")
    )
    asyncio.run(login_as_tenant_admin(client, db_session, tenant_id))
    page = client.get("/t/chat-simulator")
    csrf_token = _csrf(page.text)
    phone = "5491155555555"

    started = client.post(
        "/t/chat-simulator/api",
        headers={"X-CSRF-Token": csrf_token},
        json={"from_number": phone, "message": "Hola"},
    )
    assert started.status_code == 200

    reset = client.post(
        "/t/chat-simulator/reset",
        data={"csrf_token": csrf_token, "from_number": phone},
        follow_redirects=False,
    )
    assert reset.status_code == 303

    restarted = client.post(
        "/t/chat-simulator/api",
        headers={"X-CSRF-Token": csrf_token},
        json={"from_number": phone, "message": "Hola"},
    )
    assert restarted.status_code == 200
    assert "asistente de" in restarted.json()["reply"].lower()
