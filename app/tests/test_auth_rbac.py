from __future__ import annotations

import asyncio

from app.core.security import hash_password
from app.tests.conftest import create_tenant, create_user, login


def test_super_admin_login_and_access(client, db_session):
    asyncio.run(create_user(db_session, "admin@example.com", hash_password("change_me"), "SUPER_ADMIN", None))

    login(client, "admin@example.com", "change_me")
    resp = client.get("/admin/dashboard")
    assert resp.status_code == 200


def test_tenant_admin_cannot_access_admin(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant A", "whatsapp:+100"))
    asyncio.run(
        create_user(
            db_session,
            "tenant@example.com",
            hash_password("secret"),
            "TENANT_ADMIN",
            tenant_id,
        )
    )

    login(client, "tenant@example.com", "secret")
    resp = client.get("/admin/tenants")
    assert resp.status_code == 403
