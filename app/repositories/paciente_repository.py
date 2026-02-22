from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paciente import Paciente
from app.repositories.conversacion_repository import normalize_phone, normalize_phone_expr


class PacienteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_phone(self, tenant_id: int, telefono: str) -> Paciente | None:
        normalized = normalize_phone(telefono)
        stmt = select(Paciente).where(
            Paciente.tenant_id == tenant_id,
            Paciente.deleted_at.is_(None),
            normalize_phone_expr(Paciente.telefono) == normalized,
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
        paciente.telefono = normalize_phone(paciente.telefono)
        self._session.add(paciente)
        await self._session.flush()
        return paciente
