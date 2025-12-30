from __future__ import annotations

from contextvars import ContextVar

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


_tenant_id: ContextVar[int | None] = ContextVar("tenant_id", default=None)


def set_current_tenant_id(tenant_id: int | None) -> None:
    _tenant_id.set(tenant_id)


def get_current_tenant_id() -> int | None:
    return _tenant_id.get()


async def get_tenant_entity_or_404(
    session: AsyncSession,
    model,
    entity_id: int,
    tenant_id: int,
):
    stmt = select(model).where(
        model.id == entity_id,
        model.tenant_id == tenant_id,
        model.deleted_at.is_(None),
    )
    result = await session.execute(stmt)
    entity = result.scalar_one_or_none()
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recurso no encontrado")
    return entity


async def get_entity_or_404(
    session: AsyncSession,
    model,
    entity_id: int,
):
    stmt = select(model).where(model.id == entity_id, model.deleted_at.is_(None))
    result = await session.execute(stmt)
    entity = result.scalar_one_or_none()
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recurso no encontrado")
    return entity
