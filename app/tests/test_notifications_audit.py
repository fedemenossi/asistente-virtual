from __future__ import annotations

import asyncio
import re

from app.core.security import hash_password
from app.tests.conftest import (
    create_notification,
    create_tenant,
    create_user,
    get_notification,
    login,
)


def test_admin_audit_logs_page(client, db_session):
    asyncio.run(create_user(db_session, "admin@example.com", hash_password("change_me"), "SUPER_ADMIN", None))
    login(client, "admin@example.com", "change_me")
    resp = client.get("/admin/audit-logs")
    assert resp.status_code == 200


def test_tenant_notifications_flow(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant F", "whatsapp:+600"))
    asyncio.run(
        create_user(
            db_session,
            "tenantf@example.com",
            hash_password("secret"),
            "TENANT_ADMIN",
            tenant_id,
        )
    )
    notif_id = asyncio.run(create_notification(db_session, tenant_id, "Aviso"))

    login(client, "tenantf@example.com", "secret")
    resp = client.get("/t/notifications")
    assert resp.status_code == 200
    assert "Aviso" in resp.text

    csrf = re.search(r'name="csrf_token" value="([^"]+)"', resp.text).group(1)
    mark = client.post(
        f"/t/notifications/{notif_id}/read",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert mark.status_code in (302, 303)

    notification = asyncio.run(get_notification(db_session, notif_id))
    assert notification.read_at is not None
