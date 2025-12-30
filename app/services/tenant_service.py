from __future__ import annotations

from app.models.tenant import Tenant
from app.repositories.tenant_repository import TenantRepository


class TenantService:
    def __init__(self, repo: TenantRepository) -> None:
        self._repo = repo

    async def resolve_by_whatsapp(self, whatsapp_number: str) -> Tenant | None:
        return await self._repo.get_by_whatsapp_number(whatsapp_number)
