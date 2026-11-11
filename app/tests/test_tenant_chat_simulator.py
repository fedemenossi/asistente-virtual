from __future__ import annotations

import asyncio
import re

from app.tests.conftest import (
    create_paciente,
    create_tenant,
    login_as_tenant_admin,
)


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_tenant_chat_simulator_uses_authenticated_tenant_and_requires_patient(
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
    assert 'data-chat-phone' not in page.text
    assert 'data-chat-patient' in page.text
    csrf_token = _csrf(page.text)

    missing_patient = client.post(
        "/t/chat-simulator/api",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "Hola"},
    )
    assert missing_patient.status_code == 400
    assert missing_patient.json()["error"] == "Selecciona un paciente."

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
