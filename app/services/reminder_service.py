from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.notifications import create_notification
from app.models.consultorio import Consultorio
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.models.turno import AppointmentStatus, EstadoTurno, Turno
from app.services.messaging_service import MessagingService


class ReminderService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._messaging = MessagingService()

    async def run(self, request, hours_before: int = 24, window_minutes: int = 10) -> int:
        now = datetime.now(timezone.utc)
        target_start = now + timedelta(hours=hours_before)
        window_end = target_start + timedelta(minutes=window_minutes)
        stmt = select(Turno).where(
            Turno.reminder_sent_at.is_(None),
            or_(
                Turno.status == AppointmentStatus.CONFIRMED,
                Turno.estado == EstadoTurno.CONFIRMADO,
            ),
            or_(Turno.start_at.is_not(None), Turno.fecha_hora.is_not(None)),
        )
        result = await self._session.execute(stmt)
        turnos = list(result.scalars().all())
        sent = 0
        for turno in turnos:
            start_at = turno.start_at or turno.fecha_hora
            if not start_at:
                continue
            if not (target_start <= start_at <= window_end):
                continue
            paciente = await self._session.get(Paciente, turno.paciente_id)
            consultorio = await self._session.get(Consultorio, turno.consultorio_id)
            if not paciente or not consultorio:
                continue
            tenant = await self._session.get(Tenant, consultorio.tenant_id)
            message = (
                f"Recordatorio: turno {consultorio.nombre} el {start_at.strftime('%Y-%m-%d %H:%M')}."
            )
            self._messaging.send_whatsapp(paciente.telefono, message, tenant=tenant)
            if paciente.email:
                self._messaging.send_email(
                    paciente.email, "Recordatorio de turno", message
                )
            turno.reminder_sent_at = now
            await create_notification(
                self._session,
                title="Recordatorio enviado",
                message=f"Recordatorio enviado para turno #{turno.id}.",
                notif_type="info",
                tenant_id=consultorio.tenant_id,
            )
            await audit_log(
                self._session,
                request,
                None,
                action="reminder_sent",
                entity="turno",
                entity_id=turno.id,
                tenant_id=consultorio.tenant_id,
            )
            sent += 1
        await self._session.commit()
        return sent
