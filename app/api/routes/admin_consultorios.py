from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_basic_auth
from app.core.db import get_async_session
from app.models.consultorio import Consultorio
from app.models.tenant import Tenant
from app.schemas.consultorio import ConsultorioCreate, ConsultorioRead, ConsultorioUpdate

router = APIRouter(
    prefix="/api/admin/consultorios",
    tags=["admin"],
    dependencies=[Depends(admin_basic_auth)],
)


@router.get("", response_model=list[ConsultorioRead])
async def list_consultorios(
    session: AsyncSession = Depends(get_async_session),
) -> list[ConsultorioRead]:
    result = await session.execute(select(Consultorio))
    return list(result.scalars().all())


@router.post("", response_model=ConsultorioRead, status_code=status.HTTP_201_CREATED)
async def create_consultorio(
    payload: ConsultorioCreate,
    session: AsyncSession = Depends(get_async_session),
) -> ConsultorioRead:
    tenant = await session.get(Tenant, payload.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    consultorio = Consultorio(
        tenant_id=payload.tenant_id,
        nombre=payload.nombre,
        tipo=payload.tipo,
        proveedor_turnos=payload.proveedor_turnos,
        configuracion_externa=payload.configuracion_externa,
    )
    async with session.begin():
        session.add(consultorio)
        await session.flush()
    return consultorio


@router.get("/{consultorio_id}", response_model=ConsultorioRead)
async def get_consultorio(
    consultorio_id: int,
    session: AsyncSession = Depends(get_async_session),
) -> ConsultorioRead:
    consultorio = await session.get(Consultorio, consultorio_id)
    if consultorio is None:
        raise HTTPException(status_code=404, detail="Consultorio no encontrado")
    return consultorio


@router.put("/{consultorio_id}", response_model=ConsultorioRead)
async def update_consultorio(
    consultorio_id: int,
    payload: ConsultorioUpdate,
    session: AsyncSession = Depends(get_async_session),
) -> ConsultorioRead:
    async with session.begin():
        consultorio = await session.get(Consultorio, consultorio_id)
        if consultorio is None:
            raise HTTPException(status_code=404, detail="Consultorio no encontrado")
        if payload.nombre is not None:
            consultorio.nombre = payload.nombre
        if payload.tipo is not None:
            consultorio.tipo = payload.tipo
        if payload.proveedor_turnos is not None:
            consultorio.proveedor_turnos = payload.proveedor_turnos
        if payload.configuracion_externa is not None:
            consultorio.configuracion_externa = payload.configuracion_externa
        await session.flush()
        return consultorio


@router.delete(
    "/{consultorio_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_consultorio(
    consultorio_id: int,
    session: AsyncSession = Depends(get_async_session),
) -> None:
    async with session.begin():
        consultorio = await session.get(Consultorio, consultorio_id)
        if consultorio is None:
            raise HTTPException(status_code=404, detail="Consultorio no encontrado")
        await session.delete(consultorio)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
