from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.timezone import now_ba
from app.models.conversation_history import ConversationHistory
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
        conversation_category: str | None = None,
        conversation_subtype: str | None = None,
        requires_human_review: bool | None = None,
        has_media: bool | None = None,
        last_patient_message: str | None = None,
        media_metadata: list[dict] | None = None,
    ) -> EstadoConversacion:
        safe_context = dict(contexto_json or {})
        state = await self.get_state(tenant_id=tenant_id, telefono=telefono)
        if state is None:
            state = EstadoConversacion(
                tenant_id=tenant_id,
                telefono=telefono,
                estado_actual=estado_actual,
                contexto_json=safe_context,
                status="active",
            )
            self._session.add(state)
        else:
            state.estado_actual = estado_actual
            state.contexto_json = safe_context
            flag_modified(state, "contexto_json")
            previous_status = (state.status or "").lower()
            if previous_status != "pending":
                state.status = "active"
                if previous_status == "finished":
                    state.pending_reason = None
                    state.pending_message = None
                    state.pending_at = None
                    state.resolved_at = None
                    state.resolved_by = None
                    state.conversation_category = None
                    state.conversation_subtype = None
                    state.requires_human_review = False
                    state.has_media = False
                    state.last_patient_message = None
                    state.media_metadata = None
                    flag_modified(state, "media_metadata")
        if conversation_category is not None:
            state.conversation_category = conversation_category
        if conversation_subtype is not None:
            state.conversation_subtype = conversation_subtype
        if requires_human_review is not None:
            state.requires_human_review = requires_human_review
        if has_media is not None:
            state.has_media = has_media
        if last_patient_message is not None:
            state.last_patient_message = last_patient_message
        if media_metadata is not None:
            state.media_metadata = media_metadata
            flag_modified(state, "media_metadata")
        await self._session.flush()
        return state

    async def delete_state(self, tenant_id: int, telefono: str, *, allow_finished: bool = False) -> None:
        state = await self.get_state(tenant_id=tenant_id, telefono=telefono)
        if state is not None:
            if (state.status or "").lower() == "finished" and not allow_finished:
                return
            await self._session.delete(state)

    async def mark_pending(
        self,
        tenant_id: int,
        telefono: str,
        reason: str,
        message: str,
        category: str | None = None,
        subtype: str | None = None,
        requires_human_review: bool = False,
        has_media: bool = False,
        last_patient_message: str | None = None,
        media_metadata: list[dict] | None = None,
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
        state.conversation_category = category
        state.conversation_subtype = subtype
        state.requires_human_review = requires_human_review
        state.has_media = has_media
        if last_patient_message is not None:
            state.last_patient_message = last_patient_message
        if media_metadata is not None:
            state.media_metadata = media_metadata
            flag_modified(state, "media_metadata")
        state.pending_at = now_ba()
        state.resolved_at = None
        state.resolved_by = None
        await self._session.flush()
        return state

    async def mark_resolved(
        self,
        tenant_id: int,
        telefono: str,
        resolved_by: int | None = None,
        close_reason: str | None = None,
    ) -> EstadoConversacion | None:
        state = await self.get_state(tenant_id=tenant_id, telefono=telefono)
        if state is None:
            return None
        if (state.status or "").lower() == "finished":
            return state
        resolved_at = now_ba()
        self._session.add(
            ConversationHistory(
                tenant_id=state.tenant_id,
                telefono=state.telefono,
                patient_id=(state.contexto_json or {}).get("patient_id"),
                estado_actual=state.estado_actual,
                contexto_json=state.contexto_json,
                previous_status=state.status,
                pending_reason=state.pending_reason,
                pending_message=state.pending_message,
                conversation_category=state.conversation_category,
                conversation_subtype=state.conversation_subtype,
                requires_human_review=bool(state.requires_human_review),
                has_media=bool(state.has_media),
                last_patient_message=state.last_patient_message,
                media_metadata=state.media_metadata,
                pending_at=state.pending_at,
                resolved_at=resolved_at,
                resolved_by=resolved_by,
                close_reason=close_reason,
            )
        )
        state.status = "finished"
        state.resolved_at = resolved_at
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
