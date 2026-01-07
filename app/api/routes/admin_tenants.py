from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_basic_auth
from app.core.db import get_async_session
from app.models.tenant import Tenant
from app.schemas.tenant import TenantCreate, TenantRead, TenantUpdate

router = APIRouter(prefix="/api/admin/tenants", tags=["admin"], dependencies=[Depends(admin_basic_auth)])


@router.get("", response_model=list[TenantRead])
async def list_tenants(session: AsyncSession = Depends(get_async_session)) -> list[TenantRead]:
    result = await session.execute(select(Tenant))
    return list(result.scalars().all())


@router.post("", response_model=TenantRead, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    payload: TenantCreate,
    session: AsyncSession = Depends(get_async_session),
) -> TenantRead:
    tenant = Tenant(
        nombre=payload.nombre,
        whatsapp_number=payload.whatsapp_number,
        activo=payload.activo,
        fantasy_name=payload.fantasy_name,
        first_name=payload.first_name,
        last_name=payload.last_name,
        cuil=payload.cuil,
        address=payload.address,
        postal_code=payload.postal_code,
        phone=payload.phone,
    )
    async with session.begin():
        session.add(tenant)
        await session.flush()
    return tenant


@router.get("/{tenant_id}", response_model=TenantRead)
async def get_tenant(
    tenant_id: int,
    session: AsyncSession = Depends(get_async_session),
) -> TenantRead:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    return tenant


@router.put("/{tenant_id}", response_model=TenantRead)
async def update_tenant(
    tenant_id: int,
    payload: TenantUpdate,
    session: AsyncSession = Depends(get_async_session),
) -> TenantRead:
    async with session.begin():
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")
        if payload.nombre is not None:
            tenant.nombre = payload.nombre
        if payload.whatsapp_number is not None:
            tenant.whatsapp_number = payload.whatsapp_number
        if payload.activo is not None:
            tenant.activo = payload.activo
        if payload.fantasy_name is not None:
            tenant.fantasy_name = payload.fantasy_name
        if payload.first_name is not None:
            tenant.first_name = payload.first_name
        if payload.last_name is not None:
            tenant.last_name = payload.last_name
        if payload.cuil is not None:
            tenant.cuil = payload.cuil
        if payload.address is not None:
            tenant.address = payload.address
        if payload.postal_code is not None:
            tenant.postal_code = payload.postal_code
        if payload.phone is not None:
            tenant.phone = payload.phone
        await session.flush()
        return tenant


@router.delete(
    "/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_tenant(
    tenant_id: int,
    session: AsyncSession = Depends(get_async_session),
) -> None:
    async with session.begin():
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")
        await session.delete(tenant)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
