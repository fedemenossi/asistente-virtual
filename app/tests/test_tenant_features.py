from __future__ import annotations

import asyncio
import re

from sqlalchemy import select

from app.core.features import FEATURE_REGISTRY
from app.core.security import hash_password
from app.models.audit_log import AuditLog
from app.models.tenant_feature import TenantFeature
from app.models.user import UserRole
from app.services.tenant_feature_service import TenantFeatureService
from app.tests.conftest import create_tenant, create_user, login


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token no encontrado"
    return match.group(1)


def test_super_admin_can_edit_flags(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Flags", "whatsapp:+510"))
    asyncio.run(
        create_user(
            db_session,
            "admin-flags@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-flags@test.com", "secret-123")

    response = client.get(f"/admin/tenant-features/{tenant_id}")
    assert response.status_code == 200
    csrf_token = _extract_csrf(response.text)

    result = client.post(
        f"/admin/tenant-features/{tenant_id}",
        data={"csrf_token": csrf_token, "action": "disable_all"},
        follow_redirects=False,
    )
    assert result.status_code in (302, 303)

    async def _fetch():
        async with db_session() as session:
            row = await session.execute(
                select(TenantFeature).where(
                    TenantFeature.tenant_id == tenant_id,
                    TenantFeature.feature_key == "pacientes",
                )
            )
            audit_rows = await session.execute(
                select(AuditLog).where(
                    AuditLog.entity == "tenant_features",
                    AuditLog.action == "tenant_features_updated",
                    AuditLog.entity_id == tenant_id,
                )
            )
            return row.scalar_one_or_none(), list(audit_rows.scalars().all())

    pacientes_flag, audits = asyncio.run(_fetch())
    assert pacientes_flag is not None
    assert pacientes_flag.enabled is False
    assert audits


def test_tenant_admin_cannot_access_feature_admin(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant No Admin", "whatsapp:+511"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-no-admin@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-no-admin@test.com", "secret-123")

    response = client.get("/admin/tenant-features")
    assert response.status_code == 403


def test_feature_disabled_blocks_route(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Block", "whatsapp:+512"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-block@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def _disable():
        async with db_session() as session:
            async with session.begin():
                service = TenantFeatureService(session)
                await service.sync_tenant_with_registry(tenant_id)
                await service.set_flags(tenant_id, {"pacientes": False}, updated_by=None)

    asyncio.run(_disable())
    login(client, "tenant-block@test.com", "secret-123")

    response = client.get("/t/pacientes")
    assert response.status_code == 403


def test_feature_disabled_hides_sidebar_item(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Menu", "whatsapp:+513"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-menu@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def _disable():
        async with db_session() as session:
            async with session.begin():
                service = TenantFeatureService(session)
                await service.sync_tenant_with_registry(tenant_id)
                await service.set_flags(tenant_id, {"pacientes": False}, updated_by=None)

    asyncio.run(_disable())
    login(client, "tenant-menu@test.com", "secret-123")

    response = client.get("/t/dashboard")
    assert response.status_code == 200
    assert 'href="/t/pacientes"' not in response.text


def test_sync_creates_missing_feature(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Sync", "whatsapp:+514"))

    async def _sync():
        async with db_session() as session:
            async with session.begin():
                inserted = await TenantFeatureService(session).sync_all_tenants_with_registry()
                rows = await session.execute(
                    select(TenantFeature).where(TenantFeature.tenant_id == tenant_id)
                )
                return inserted, list(rows.scalars().all())

    inserted, rows = asyncio.run(_sync())
    assert inserted >= len(FEATURE_REGISTRY)
    assert len(rows) == len(FEATURE_REGISTRY)


def test_new_registry_feature_auto_created(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant New Feature", "whatsapp:+515"))
    FEATURE_REGISTRY["nuevo_feature_test"] = {
        "label": "Nuevo feature test",
        "routes": ["/t/new-feature-test"],
        "default_enabled": True,
    }

    try:
        async def _sync():
            async with db_session() as session:
                async with session.begin():
                    await TenantFeatureService(session).sync_all_tenants_with_registry()
                    row = await session.execute(
                        select(TenantFeature).where(
                            TenantFeature.tenant_id == tenant_id,
                            TenantFeature.feature_key == "nuevo_feature_test",
                        )
                    )
                    return row.scalar_one_or_none()

        created = asyncio.run(_sync())
        assert created is not None
        assert created.enabled is True
    finally:
        FEATURE_REGISTRY.pop("nuevo_feature_test", None)
