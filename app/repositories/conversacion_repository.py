from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import func, select
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
                status="active",
            )
            self._session.add(state)
        else:
            state.estado_actual = estado_actual
            state.contexto_json = contexto_json
            if not state.status:
                state.status = "active"
        await self._session.flush()
        return state

    async def delete_state(self, tenant_id: int, telefono: str) -> None:
        state = await self.get_state(tenant_id=tenant_id, telefono=telefono)
        if state is not None:
            await self._session.delete(state)

    async def mark_pending(
        self,
        tenant_id: int,
        telefono: str,
        reason: str,
        message: str,
    ) -> EstadoConversacion:
        state = await self.get_state(tenant_id=tenant_id, telefono=telefono)
        if state is None:
            state = EstadoConversacion(
                tenant_id=tenant_id,
                telefono=telefono,
                estado_actual="main_reason_menu",
                contexto_json={},
            )
            self._session.add(state)
        state.status = "pending"
        state.pending_reason = reason
        state.pending_message = message
        state.pending_at = datetime.now(timezone.utc)
        state.resolved_at = None
        state.resolved_by = None
        await self._session.flush()
        return state

    async def mark_resolved(self, tenant_id: int, telefono: str, resolved_by: int | None = None) -> EstadoConversacion | None:
        state = await self.get_state(tenant_id=tenant_id, telefono=telefono)
        if state is None:
            return None
        state.status = "finished"
        state.resolved_at = datetime.now(timezone.utc)
        state.resolved_by = resolved_by
        await self._session.flush()
        return state


def normalize_phone(value: str | None) -> str:
    return re.sub(r"\D+", "", value or "")


def normalize_phone_expr(column):
    expr = func.replace(column, "whatsapp:", "")
    expr = func.replace(expr, "+", "")
    expr = func.replace(expr, "-", "")
    expr = func.replace(expr, " ", "")
    expr = func.replace(expr, "(", "")
    expr = func.replace(expr, ")", "")
    return expr
