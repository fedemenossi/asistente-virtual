from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paciente import Paciente


class PacienteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_phone(self, tenant_id: int, telefono: str) -> Paciente | None:
        stmt = select(Paciente).where(
            Paciente.tenant_id == tenant_id,
            Paciente.telefono == telefono,
            Paciente.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_dni(self, tenant_id: int, dni: str) -> Paciente | None:
        stmt = select(Paciente).where(
            Paciente.tenant_id == tenant_id,
            Paciente.dni == dni,
            Paciente.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, paciente: Paciente) -> Paciente:
        self._session.add(paciente)
        await self._session.flush()
        return paciente
