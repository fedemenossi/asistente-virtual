from __future__ import annotations

import asyncio
import re

from app.core.security import hash_password
from app.models.user import UserRole
from app.tests.conftest import create_paciente, create_tenant, create_user, login


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _create_pending_link(client, db_session, *, suffix: str = "list") -> tuple[int, str, str]:
    to_number = f"whatsapp:+761{len(suffix)}"
    from_number = f"whatsapp:+54911761{len(suffix):04d}"
    tenant_id = asyncio.run(create_tenant(db_session, f"Tenant {suffix}", to_number))
    asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491176199999",
            nombre="Dalia",
            apellido="Reservada",
            dni="39.444.555",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            f"super-{suffix}@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    asyncio.run(
        create_user(
            db_session,
            f"operator-{suffix}@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, f"super-{suffix}@test.com", "secret-123")
    csrf_token = _csrf(client.get("/admin/chat-simulator").text)

    def send(message: str) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={"to_number": to_number, "from_number": from_number, "message": message},
        )
        assert response.status_code == 200
        return response.json()["reply"]

    send("hola")
    send("2")
    reply = send("39.444.555")
    assert "pendiente" in reply.lower()
    assert "Dalia" not in reply
    login(client, f"operator-{suffix}@test.com", "secret-123")
    return tenant_id, to_number, from_number


def _detail_url(client) -> str:
    listing = client.get("/t/contact-links")
    match = re.search(r'href="(/t/contact-links/\d+)"', listing.text)
    assert match is not None
    return match.group(1)


def _send_as_super(client, *, suffix: str, to_number: str, from_number: str, message: str) -> str:
    login(client, f"super-{suffix}@test.com", "secret-123")
    csrf_token = _csrf(client.get("/admin/chat-simulator").text)
    response = client.post(
        "/admin/chat-simulator/api",
        headers={"X-CSRF-Token": csrf_token},
        json={"to_number": to_number, "from_number": from_number, "message": message},
    )
    assert response.status_code == 200
    return response.json()["reply"]


def test_operator_lists_and_reviews_pending_contact_links(client, db_session):
    _create_pending_link(client, db_session)

    listing = client.get("/t/contact-links")

    assert listing.status_code == 200
    assert "Vinculaciones de contactos" in listing.text
    assert "Dalia Reservada" in listing.text
    assert "39.444.555" in listing.text
    assert "Pendiente" in listing.text
    detail_url = re.search(r'href="(/t/contact-links/\d+)"', listing.text)
    assert detail_url is not None

    detail = client.get(detail_url.group(1))
    assert detail.status_code == 200
    assert "Revisar vinculacion" in detail.text
    assert "Dalia Reservada" in detail.text
    assert "Aprobar" in detail.text
    assert "Rechazar" in detail.text


def test_operator_approval_is_idempotent_audited_and_enables_contact(client, db_session):
    _, to_number, from_number = _create_pending_link(client, db_session, suffix="approve")
    detail_url = _detail_url(client)
    detail = client.get(detail_url)
    csrf_token = _csrf(detail.text)

    first = client.post(f"{detail_url}/approve", data={"csrf_token": csrf_token}, follow_redirects=False)
    second = client.post(f"{detail_url}/approve", data={"csrf_token": csrf_token}, follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    approved = client.get(detail_url)
    assert "Aprobada" in approved.text
    assert "Rechazar" not in approved.text
    audit = client.get("/t/audit-logs?action=approve&entity=patient_contact_link")
    assert audit.text.count("patient_contact_link") >= 2
    assert '"idempotent": true' in audit.text.lower()

    conflicting = client.post(
        f"{detail_url}/reject",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert conflicting.status_code == 303
    assert "Aprobada" in client.get(detail_url).text
    conflict_audit = client.get("/t/audit-logs?action=reject&entity=patient_contact_link")
    assert '"conflicting": true' in conflict_audit.text.lower()

    reply = _send_as_super(
        client,
        suffix="approve",
        to_number=to_number,
        from_number=from_number,
        message="continuar",
    )
    assert "Dalia Reservada" in reply
    assert "Turno presencial" in reply


def test_operator_rejection_is_idempotent_audited_and_keeps_contact_blocked(client, db_session):
    _, to_number, from_number = _create_pending_link(client, db_session, suffix="reject")
    detail_url = _detail_url(client)
    csrf_token = _csrf(client.get(detail_url).text)

    first = client.post(f"{detail_url}/reject", data={"csrf_token": csrf_token}, follow_redirects=False)
    second = client.post(f"{detail_url}/reject", data={"csrf_token": csrf_token}, follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    rejected = client.get(detail_url)
    assert "Rechazada" in rejected.text
    assert "Aprobar" not in rejected.text
    audit = client.get("/t/audit-logs?action=reject&entity=patient_contact_link")
    assert audit.text.count("patient_contact_link") >= 2
    assert '"idempotent": true' in audit.text.lower()

    reply = _send_as_super(
        client,
        suffix="reject",
        to_number=to_number,
        from_number=from_number,
        message="continuar",
    )
    assert "rechazada" in reply.lower()
    assert "Dalia" not in reply
    assert "Turno presencial" not in reply

    for message in ("salir", "hola", "2"):
        _send_as_super(
            client,
            suffix="reject",
            to_number=to_number,
            from_number=from_number,
            message=message,
        )
    retried = _send_as_super(
        client,
        suffix="reject",
        to_number=to_number,
        from_number=from_number,
        message="39.444.555",
    )
    assert "rechazada" in retried.lower()
    assert "Dalia" not in retried


def test_operator_cannot_review_or_resolve_another_tenants_link(client, db_session):
    tenant_id, _, _ = _create_pending_link(client, db_session, suffix="scope")
    detail_url = _detail_url(client)
    other_tenant_id = asyncio.run(create_tenant(db_session, "Tenant ajeno", "whatsapp:+799"))
    asyncio.run(
        create_user(
            db_session,
            "operator-other@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            other_tenant_id,
        )
    )
    login(client, "operator-other@test.com", "secret-123")
    own_page = client.get("/t/contact-links")
    csrf_token = _csrf(own_page.text)

    assert client.get(detail_url).status_code == 404
    assert client.post(
        f"{detail_url}/approve",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    ).status_code == 404

    login(client, "operator-scope@test.com", "secret-123")
    assert "Pendiente" in client.get(detail_url).text
    assert tenant_id != other_tenant_id
