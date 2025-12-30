from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversacion import EstadoConversacion


class ConversacionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_state(self, tenant_id: int, telefono: str) -> EstadoConversacion | None:
        stmt = select(EstadoConversacion).where(
            EstadoConversacion.tenant_id == tenant_id,
            EstadoConversacion.telefono == telefono,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_state(
        self,
        tenant_id: int,
        telefono: str,
        estado_actual: str,
        contexto_json: dict | None,
    ) -> EstadoConversacion:
        state = await self.get_state(tenant_id=tenant_id, telefono=telefono)
        if state is None:
            state = EstadoConversacion(
                tenant_id=tenant_id,
                telefono=telefono,
                estado_actual=estado_actual,
                contexto_json=contexto_json,
            )
            self._session.add(state)
        else:
            state.estado_actual = estado_actual
            state.contexto_json = contexto_json
        await self._session.flush()
        return state

    async def delete_state(self, tenant_id: int, telefono: str) -> None:
        state = await self.get_state(tenant_id=tenant_id, telefono=telefono)
        if state is not None:
            await self._session.delete(state)
