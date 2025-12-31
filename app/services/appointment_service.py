from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.notifications import create_notification
from app.models.consultorio import Consultorio, TipoConsultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.models.turno import AppointmentStatus, EstadoTurno, TipoTurno, Turno
from app.services.calendar_service import CalendarService


class AppointmentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._calendar = CalendarService()

    async def create_draft(
        self,
        request: Request,
        tenant: Tenant,
        consultorio: Consultorio,
        paciente: Paciente,
        slot_id: str,
        start_at: datetime,
        end_at: datetime,
        timezone_name: str,
    ) -> Turno:
        tipo_turno = (
            TipoTurno.VIRTUAL if consultorio.tipo == TipoConsultorio.VIRTUAL else TipoTurno.PRESENCIAL
        )
        turno = Turno(
            paciente_id=paciente.id,
            consultorio_id=consultorio.id,
            fecha_hora=start_at,
            start_at=start_at,
            end_at=end_at,
            timezone=timezone_name,
            tipo=tipo_turno,
            estado=EstadoTurno.DRAFT,
            status=AppointmentStatus.DRAFT,
            external_calendar_provider="google",
            external_calendar_id=(tenant.calendar_settings or {}).get("google_calendar_id"),
            external_event_id=slot_id,
        )
        self._session.add(turno)
        await self._session.flush()
        await audit_log(
            self._session,
            request,
            None,
            action="create",
            entity="turno",
            entity_id=turno.id,
            tenant_id=tenant.id,
        )
        await self._session.commit()
        return turno

    async def confirm_after_payment(
        self,
        request: Request,
        tenant: Tenant,
        consultorio: Consultorio,
        paciente: Paciente,
        turno: Turno,
    ) -> None:
        if turno.external_calendar_provider == "google" and turno.external_event_id:
            try:
                result = await self._calendar.reserve_slot(
                    tenant,
                    consultorio,
                    turno.external_event_id,
                    paciente,
                    {"turno_id": turno.id, "tenant_id": tenant.id},
                )
            except Exception:
                turno.status = AppointmentStatus.CANCELLED
                turno.estado = EstadoTurno.CANCELADO
                await audit_log(
                    self._session,
                    request,
                    None,
                    action="slot_unavailable",
                    entity="turno",
                    entity_id=turno.id,
                    tenant_id=tenant.id,
                )
                await create_notification(
                    self._session,
                    title="Turno no disponible",
                    message=f"El slot del turno #{turno.id} ya no esta disponible.",
                    notif_type="warning",
                    tenant_id=tenant.id,
                )
                await self._session.commit()
                return
            turno.external_calendar_id = result.get("calendar_id")
            turno.external_event_id = result.get("event_id")
            if result.get("start_at"):
                turno.start_at = datetime.fromisoformat(result["start_at"].replace("Z", "+00:00"))
                turno.fecha_hora = turno.start_at
            if result.get("end_at"):
                turno.end_at = datetime.fromisoformat(result["end_at"].replace("Z", "+00:00"))
            turno.timezone = result.get("timezone") or turno.timezone
            if result.get("meet_link"):
                turno.referencia_externa = result.get("meet_link")

        turno.status = AppointmentStatus.CONFIRMED
        turno.estado = EstadoTurno.CONFIRMADO
        await audit_log(
            self._session,
            request,
            None,
            action="confirm",
            entity="turno",
            entity_id=turno.id,
            tenant_id=tenant.id,
        )
        await create_notification(
            self._session,
            title="Turno confirmado",
            message=f"Turno #{turno.id} confirmado.",
            notif_type="success",
            tenant_id=tenant.id,
        )
        await self._session.commit()

    async def cancel_turno(
        self,
        request: Request,
        tenant: Tenant,
        consultorio: Consultorio,
        turno: Turno,
    ) -> None:
        if turno.external_calendar_provider == "google" and turno.external_event_id:
            await self._calendar.cancel_slot(tenant, turno.external_event_id)
        turno.status = AppointmentStatus.CANCELLED
        turno.estado = EstadoTurno.CANCELADO
        await audit_log(
            self._session,
            request,
            None,
            action="cancel",
            entity="turno",
            entity_id=turno.id,
            tenant_id=tenant.id,
        )
        await create_notification(
            self._session,
            title="Turno cancelado",
            message=f"Turno #{turno.id} cancelado.",
            notif_type="warning",
            tenant_id=tenant.id,
        )
        await self._session.commit()
