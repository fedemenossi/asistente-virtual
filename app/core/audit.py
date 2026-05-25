from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import CurrentUser
from app.models.audit_log import AuditLog


def _get_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    if request.client:
        return request.client.host
    return None


def _get_user_agent(request: Request | None) -> str | None:
    if request is None:
        return None
    return request.headers.get("User-Agent")


async def audit_log(
    session: AsyncSession,
    request: Request | None,
    user: CurrentUser | None,
    action: str,
    entity: str,
    entity_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    tenant_id: int | None = None,
) -> None:
    resolved_tenant = tenant_id if tenant_id is not None else getattr(user, "tenant_id", None)
    entry = AuditLog(
        tenant_id=resolved_tenant,
        user_id=getattr(user, "id", None),
        action=action,
        entity=entity,
        entity_id=entity_id,
        metadata_json=metadata,
        ip_address=_get_ip(request),
        user_agent=_get_user_agent(request),
    )
    session.add(entry)
