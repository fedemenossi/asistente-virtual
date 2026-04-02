from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.notifications import create_notification
from app.core.timezone import now_ba
from app.models.consultorio import Consultorio, TipoConsultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.models.turno import AppointmentStatus, EstadoTurno, TipoTurno, Turno
from app.services.calendar_service import CalendarService


class AppointmentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._calendar = CalendarService()

    async def create_local_turno(
        self,
        *,
        tenant: Tenant,
        consultorio: Consultorio,
        paciente: Paciente,
        tipo: TipoTurno,
        start_at: datetime,
        end_at: datetime | None = None,
        timezone_name: str | None = None,
        provider: str | None = None,
        external_id: str | None = None,
        external_status: str | None = None,
        status: AppointmentStatus = AppointmentStatus.DRAFT,
        estado: EstadoTurno = EstadoTurno.DRAFT,
        notes: str | None = None,
    ) -> Turno:
        # La DB local es la fuente operativa. La sincronizacion externa se refleja
        # despues sobre el mismo registro, sin crear una entidad paralela.
        resolved_provider = provider or self._calendar.resolve_provider_name(consultorio)
        turno = Turno(
            tenant_id=tenant.id,
            paciente_id=paciente.id,
            consultorio_id=consultorio.id,
            fecha_hora=start_at,
            start_at=start_at,
            end_at=end_at,
            timezone=timezone_name,
            tipo=tipo,
            provider=resolved_provider,
            external_id=external_id,
            external_status=external_status,
            notes=notes,
            reminder_24h_sent=False,
            reminder_2h_sent=False,
            estado=estado,
            status=status,
            external_calendar_provider=resolved_provider,
            external_calendar_id=self._calendar.resolve_external_source_id(tenant, consultorio),
            external_event_id=external_id,
        )
        self._session.add(turno)
        await self._session.flush()
        return turno

    async def update_local_turno(self, turno: Turno, **changes) -> Turno:
        for key, value in changes.items():
            if hasattr(turno, key):
                setattr(turno, key, value)
        await self._session.flush()
        return turno

    async def list_turnos_by_tenant_and_date(
        self,
        tenant_id: int,
        target_date: date,
    ) -> list[Turno]:
        start = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=now_ba().tzinfo)
        end = start + timedelta(days=1)
        result = await self._session.execute(
            select(Turno)
            .where(
                Turno.tenant_id == tenant_id,
                Turno.deleted_at.is_(None),
                Turno.fecha_hora >= start,
                Turno.fecha_hora < end,
            )
            .order_by(Turno.fecha_hora.asc())
        )
        return list(result.scalars().all())

    async def get_daily_agenda(self, tenant_id: int, target_date: date) -> list[Turno]:
        # Alias semantico para las vistas operativas del dashboard/agenda.
        return await self.list_turnos_by_tenant_and_date(tenant_id, target_date)

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
        provider_name = self._calendar.resolve_provider_name(consultorio)
        turno = await self.create_local_turno(
            tenant=tenant,
            consultorio=consultorio,
            paciente=paciente,
            tipo=tipo_turno,
            start_at=start_at,
            end_at=end_at,
            timezone_name=timezone_name,
            provider=provider_name,
            external_id=slot_id,
            external_status="draft",
            status=AppointmentStatus.DRAFT,
            estado=EstadoTurno.DRAFT,
            notes="Turno creado localmente pendiente de sincronizacion/reserva final.",
        )
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
        # La reserva externa confirma un turno local ya existente.
        if turno.external_calendar_provider and turno.external_event_id:
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
                turno.external_status = "sync_failed"
                turno.cancelled_at = now_ba()
                turno.cancellation_reason = "slot_unavailable"
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
            turno.external_id = result.get("event_id")
            turno.external_status = "reserved"
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
        turno.cancelled_at = None
        turno.cancellation_reason = None
        turno.external_status = turno.external_status or "confirmed"
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
        if turno.external_calendar_provider and turno.external_event_id:
            await self._calendar.cancel_slot(
                tenant,
                consultorio,
                turno.external_calendar_provider,
                turno.external_event_id,
            )
        turno.status = AppointmentStatus.CANCELLED
        turno.estado = EstadoTurno.CANCELADO
        turno.cancelled_at = now_ba()
        turno.cancellation_reason = "cancelled_by_user"
        turno.external_status = "cancelled"
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
