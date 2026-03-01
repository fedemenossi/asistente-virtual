from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.features import FEATURE_REGISTRY, feature_defaults
from app.models.tenant import Tenant
from app.models.tenant_feature import TenantFeature


class TenantFeatureService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_flags(self, tenant_id: int) -> dict[str, bool]:
        defaults = feature_defaults()
        result = await self._session.execute(
            select(TenantFeature).where(TenantFeature.tenant_id == tenant_id)
        )
        rows = list(result.scalars().all())
        for row in rows:
            if row.feature_key in defaults:
                defaults[row.feature_key] = bool(row.enabled)
        return defaults

    async def set_flags(self, tenant_id: int, flags: dict[str, bool], updated_by: int | None) -> dict[str, bool]:
        valid_flags = {key: bool(value) for key, value in flags.items() if key in FEATURE_REGISTRY}
        if not valid_flags:
            return await self.get_flags(tenant_id)

        result = await self._session.execute(
            select(TenantFeature).where(
                TenantFeature.tenant_id == tenant_id,
                TenantFeature.feature_key.in_(list(valid_flags.keys())),
            )
        )
        existing = {row.feature_key: row for row in result.scalars().all()}

        for key, enabled in valid_flags.items():
            current = existing.get(key)
            if current is None:
                self._session.add(
                    TenantFeature(
                        tenant_id=tenant_id,
                        feature_key=key,
                        enabled=enabled,
                        updated_by=updated_by,
                    )
                )
                continue
            current.enabled = enabled
            current.updated_by = updated_by

        await self._session.flush()
        return await self.get_flags(tenant_id)

    async def sync_all_tenants_with_registry(self) -> int:
        keys = list(FEATURE_REGISTRY.keys())
        tenant_rows = await self._session.execute(
            select(Tenant.id).where(Tenant.activo.is_(True), Tenant.deleted_at.is_(None))
        )
        tenant_ids = [row[0] for row in tenant_rows.all()]
        if not tenant_ids or not keys:
            return 0

        existing_rows = await self._session.execute(
            select(TenantFeature.tenant_id, TenantFeature.feature_key).where(
                TenantFeature.tenant_id.in_(tenant_ids),
                TenantFeature.feature_key.in_(keys),
            )
        )
        existing = {(tenant_id, feature_key) for tenant_id, feature_key in existing_rows.all()}

        to_insert: list[TenantFeature] = []
        for tenant_id in tenant_ids:
            for key in keys:
                pair = (tenant_id, key)
                if pair in existing:
                    continue
                default_enabled = bool(FEATURE_REGISTRY[key].get("default_enabled", True))
                to_insert.append(
                    TenantFeature(
                        tenant_id=tenant_id,
                        feature_key=key,
                        enabled=default_enabled,
                        updated_by=None,
                    )
                )

        if to_insert:
            self._session.add_all(to_insert)
            await self._session.flush()
        return len(to_insert)

    async def sync_tenant_with_registry(self, tenant_id: int) -> int:
        keys = list(FEATURE_REGISTRY.keys())
        existing_rows = await self._session.execute(
            select(TenantFeature.feature_key).where(
                TenantFeature.tenant_id == tenant_id,
                TenantFeature.feature_key.in_(keys),
            )
        )
        existing = {row[0] for row in existing_rows.all()}
        missing = [key for key in keys if key not in existing]
        if not missing:
            return 0
        for key in missing:
            self._session.add(
                TenantFeature(
                    tenant_id=tenant_id,
                    feature_key=key,
                    enabled=bool(FEATURE_REGISTRY[key].get("default_enabled", True)),
                    updated_by=None,
                )
            )
        await self._session.flush()
        return len(missing)
