from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import asyncio

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.consultorio import Consultorio, TipoConsultorio
from app.models.tenant import Tenant


DEFAULT_TENANT_NAME = "Consultorio Demo"
DEFAULT_WHATSAPP_NUMBER = "whatsapp:+14155238886"
DEFAULT_CONSULTORIO_NAME = "Sede Principal"


async def seed() -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            stmt = select(Tenant).where(Tenant.whatsapp_number == DEFAULT_WHATSAPP_NUMBER)
            result = await session.execute(stmt)
            tenant = result.scalar_one_or_none()

            if tenant is None:
                tenant = Tenant(
                    nombre=DEFAULT_TENANT_NAME,
                    whatsapp_number=DEFAULT_WHATSAPP_NUMBER,
                    activo=True,
                )
                session.add(tenant)
                await session.flush()

            consultorio_stmt = select(Consultorio).where(
                Consultorio.tenant_id == tenant.id,
                Consultorio.nombre == DEFAULT_CONSULTORIO_NAME,
            )
            consultorio_result = await session.execute(consultorio_stmt)
            consultorio = consultorio_result.scalar_one_or_none()

            if consultorio is None:
                consultorio = Consultorio(
                    tenant_id=tenant.id,
                    nombre=DEFAULT_CONSULTORIO_NAME,
                    tipo=TipoConsultorio.PRESENCIAL,
                )
                session.add(consultorio)


if __name__ == "__main__":
    asyncio.run(seed())
