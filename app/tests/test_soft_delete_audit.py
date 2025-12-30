from __future__ import annotations

import asyncio

from app.core.security import hash_password
from app.tests.conftest import (
    create_paciente,
    create_tenant,
    create_user,
    get_audit_logs,
    get_notifications,
    get_paciente,
    login,
)


def _csrf(client):
    response = client.get("/t/pacientes")
    match = __import__("re").search(r'name="csrf_token" value="([^"]+)"', response.text)
    return match.group(1)


def test_soft_delete_paciente_creates_audit(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant B", "whatsapp:+200"))
    asyncio.run(
        create_user(
            db_session,
            "tenantb@example.com",
            hash_password("secret"),
            "TENANT_ADMIN",
            tenant_id,
        )
    )
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+1"))

    login(client, "tenantb@example.com", "secret")
    csrf_token = _csrf(client)
    resp = client.post(
        f"/t/pacientes/{paciente_id}/delete",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    paciente = asyncio.run(get_paciente(db_session, paciente_id))
    assert paciente.deleted_at is not None

    logs = asyncio.run(get_audit_logs(db_session, "paciente"))
    assert any(log.action == "delete" for log in logs)


def test_tenant_toggle_creates_notification(client, db_session):
    asyncio.run(create_user(db_session, "admin2@example.com", hash_password("change_me"), "SUPER_ADMIN", None))
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant C", "whatsapp:+300"))

    login(client, "admin2@example.com", "change_me")
    response = client.get("/admin/tenants")
    csrf = __import__("re").search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
    resp = client.post(
        f"/admin/tenants/{tenant_id}/toggle",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    notifications = asyncio.run(get_notifications(db_session))
    assert any(n.title == "Tenant desactivado" for n in notifications)
