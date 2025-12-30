from __future__ import annotations

import asyncio
import re

from app.core.security import hash_password
from app.tests.conftest import (
    create_consultorio,
    create_tenant,
    create_user,
    get_consultorio,
    get_tenant,
    get_user,
    login,
)


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_admin_soft_delete_tenant_and_user(client, db_session):
    asyncio.run(create_user(db_session, "admin@example.com", hash_password("change_me"), "SUPER_ADMIN", None))
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant D", "whatsapp:+400"))
    user_id = asyncio.run(
        create_user(
            db_session,
            "tenantd@example.com",
            hash_password("secret"),
            "TENANT_ADMIN",
            tenant_id,
        )
    )

    login(client, "admin@example.com", "change_me")

    resp = client.get("/admin/tenants")
    csrf = _csrf(resp.text)
    delete_tenant = client.post(
        f"/admin/tenants/{tenant_id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert delete_tenant.status_code in (302, 303)

    resp_users = client.get("/admin/users")
    csrf_users = _csrf(resp_users.text)
    delete_user = client.post(
        f"/admin/users/{user_id}/delete",
        data={"csrf_token": csrf_users},
        follow_redirects=False,
    )
    assert delete_user.status_code in (302, 303)

    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    user = asyncio.run(get_user(db_session, user_id))
    assert tenant.deleted_at is not None
    assert user.deleted_at is not None


def test_tenant_soft_delete_consultorio(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant E", "whatsapp:+500"))
    asyncio.run(
        create_user(
            db_session,
            "tenante@example.com",
            hash_password("secret"),
            "TENANT_ADMIN",
            tenant_id,
        )
    )
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Sede"))

    login(client, "tenante@example.com", "secret")
    resp = client.get("/t/consultorios")
    csrf = _csrf(resp.text)
    delete_resp = client.post(
        f"/t/consultorios/{consultorio_id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert delete_resp.status_code in (302, 303)

    consultorio = asyncio.run(get_consultorio(db_session, consultorio_id))
    assert consultorio.deleted_at is not None
