from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.notifications import create_notification
from app.core.timezone import get_ba_tz, now_ba
from app.models.consultorio import Consultorio, TipoConsultorio
from app.models.paciente import Paciente
from app.models.patient_credit import PatientCredit, PatientCreditStatus
from app.models.payment import Payment, PaymentReuseStatus, PaymentStatus
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
        eligibility_context: dict | None = None,
        rescheduled_from_id: int | None = None,
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
            eligibility_context=eligibility_context,
            rescheduled_from_id=rescheduled_from_id,
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

    async def reserve_without_payment(
        self,
        *,
        tenant: Tenant,
        consultorio: Consultorio,
        paciente: Paciente,
        slot_id: str,
        start_at: datetime,
        end_at: datetime,
        timezone_name: str,
        eligibility_context: dict | None = None,
        rescheduled_from_id: int | None = None,
    ) -> tuple[Turno, dict]:
        existing_result = await self._session.execute(
            select(Turno).where(
                Turno.tenant_id == tenant.id,
                Turno.consultorio_id == consultorio.id,
                Turno.paciente_id == paciente.id,
                Turno.external_event_id == slot_id,
                Turno.status == AppointmentStatus.CONFIRMED,
                Turno.deleted_at.is_(None),
            )
        )
        existing = existing_result.scalar_one_or_none()
        if existing is not None:
            return existing, {
                "event_id": existing.external_event_id,
                "calendar_id": existing.external_calendar_id,
                "meet_link": existing.referencia_externa,
                "idempotent": True,
            }

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
            external_status="reserving",
            status=AppointmentStatus.DRAFT,
            estado=EstadoTurno.DRAFT,
            notes="Reserva solicitada desde el Asistente.",
            eligibility_context=eligibility_context,
            rescheduled_from_id=rescheduled_from_id,
        )
        try:
            result = await self._calendar.reserve_slot(
                tenant,
                consultorio,
                slot_id,
                paciente,
                {"tenant_id": tenant.id},
            )
        except Exception:
            turno.status = AppointmentStatus.CANCELLED
            turno.estado = EstadoTurno.CANCELADO
            turno.external_status = "sync_failed"
            turno.cancelled_at = now_ba()
            turno.cancellation_reason = "slot_unavailable"
            turno.rescheduled_from_id = None
            await audit_log(
                self._session,
                None,
                None,
                action="slot_unavailable",
                entity="turno",
                entity_id=turno.id,
                tenant_id=tenant.id,
            )
            await self._session.flush()
            raise

        turno.external_calendar_id = result.get("calendar_id") or turno.external_calendar_id
        turno.external_event_id = result.get("event_id") or slot_id
        turno.external_id = turno.external_event_id
        turno.external_status = "reserved"
        turno.status = AppointmentStatus.CONFIRMED
        turno.estado = EstadoTurno.CONFIRMADO
        turno.referencia_externa = result.get("meet_link")
        if result.get("start_at"):
            turno.start_at = datetime.fromisoformat(result["start_at"].replace("Z", "+00:00"))
            turno.fecha_hora = turno.start_at
        if result.get("end_at"):
            turno.end_at = datetime.fromisoformat(result["end_at"].replace("Z", "+00:00"))
        turno.timezone = result.get("timezone") or turno.timezone
        await audit_log(
            self._session,
            None,
            None,
            action="confirm",
            entity="turno",
            entity_id=turno.id,
            tenant_id=tenant.id,
            metadata={"source": "assistant", "payment_required": False},
        )
        await create_notification(
            self._session,
            title="Turno confirmado",
            message=f"Turno #{turno.id} confirmado desde el Asistente.",
            notif_type="success",
            tenant_id=tenant.id,
        )
        await self._session.flush()
        return turno, result

    async def reschedule_turno(
        self,
        request: Request | None,
        tenant: Tenant,
        paciente: Paciente,
        origin: Turno,
        consultorio: Consultorio,
        *,
        slot_id: str,
        start_at: datetime,
        end_at: datetime,
        timezone_name: str,
    ) -> tuple[Turno, dict]:
        if (
            tenant.id != paciente.tenant_id
            or tenant.id != consultorio.tenant_id
            or origin.tenant_id != tenant.id
            or origin.paciente_id != paciente.id
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Turno no encontrado")
        existing = (
            await self._session.execute(
                select(Turno).where(
                    Turno.tenant_id == tenant.id,
                    Turno.paciente_id == paciente.id,
                    Turno.rescheduled_from_id == origin.id,
                    Turno.status == AppointmentStatus.CONFIRMED,
                    Turno.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, {
                "event_id": existing.external_event_id,
                "calendar_id": existing.external_calendar_id,
                "meet_link": existing.referencia_externa,
                "idempotent": True,
            }

        locked_origin = (
            await self._session.execute(
                select(Turno)
                .where(
                    Turno.id == origin.id,
                    Turno.tenant_id == tenant.id,
                    Turno.paciente_id == paciente.id,
                    Turno.status == AppointmentStatus.CONFIRMED,
                    Turno.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked_origin is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Turno no reprogramable")
        origin_consultorio = await self._session.get(Consultorio, locked_origin.consultorio_id)
        if origin_consultorio is None or origin_consultorio.tenant_id != tenant.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agenda de origen no disponible")

        replacement, reservation = await self.reserve_without_payment(
            tenant=tenant,
            consultorio=consultorio,
            paciente=paciente,
            slot_id=slot_id,
            start_at=start_at,
            end_at=end_at,
            timezone_name=timezone_name,
            eligibility_context=dict(locked_origin.eligibility_context or {}),
            rescheduled_from_id=locked_origin.id,
        )
        if replacement.id == locked_origin.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="La nueva disponibilidad coincide con el turno de origen",
            )
        replacement.rescheduled_from_id = locked_origin.id
        replacement.eligibility_context = dict(locked_origin.eligibility_context or {})

        try:
            if locked_origin.external_calendar_provider and locked_origin.external_event_id:
                await self._calendar.cancel_slot(
                    tenant,
                    origin_consultorio,
                    locked_origin.external_calendar_provider,
                    locked_origin.external_event_id,
                )
        except Exception:
            if replacement.external_calendar_provider and replacement.external_event_id:
                try:
                    await self._calendar.cancel_slot(
                        tenant,
                        consultorio,
                        replacement.external_calendar_provider,
                        replacement.external_event_id,
                    )
                except Exception:
                    pass
            replacement.status = AppointmentStatus.CANCELLED
            replacement.estado = EstadoTurno.CANCELADO
            replacement.external_status = "reschedule_rollback"
            replacement.cancelled_at = now_ba()
            replacement.cancellation_reason = "origin_release_failed"
            replacement.rescheduled_from_id = None
            await self._session.flush()
            raise

        locked_origin.status = AppointmentStatus.CANCELLED
        locked_origin.estado = EstadoTurno.CANCELADO
        locked_origin.cancelled_at = now_ba()
        locked_origin.cancellation_reason = "rescheduled"
        locked_origin.external_status = "cancelled"

        payments = (
            await self._session.execute(
                select(Payment).where(
                    Payment.tenant_id == tenant.id,
                    Payment.patient_id == paciente.id,
                    Payment.appointment_id == locked_origin.id,
                    Payment.status == PaymentStatus.APPROVED,
                )
            )
        ).scalars().all()
        for payment in payments:
            payment.appointment_id = replacement.id

        await audit_log(
            self._session,
            request,
            None,
            action="reschedule",
            entity="turno",
            entity_id=locked_origin.id,
            tenant_id=tenant.id,
            metadata={
                "origin_turno_id": locked_origin.id,
                "replacement_turno_id": replacement.id,
            },
        )
        await self._session.commit()
        return replacement, reservation

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
        request: Request | None,
        tenant: Tenant,
        consultorio: Consultorio,
        paciente: Paciente,
        turno: Turno,
        *,
        commit: bool = True,
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
                if commit:
                    await self._session.commit()
                else:
                    await self._session.flush()
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
        if commit:
            await self._session.commit()
        else:
            await self._session.flush()

    async def cancel_turno(
        self,
        request: Request | None,
        tenant: Tenant,
        consultorio: Consultorio,
        turno: Turno,
    ) -> None:
        locked_turno = (
            await self._session.execute(
                select(Turno)
                .where(
                    Turno.id == turno.id,
                    Turno.tenant_id == tenant.id,
                    Turno.consultorio_id == consultorio.id,
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked_turno is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Turno no encontrado")
        turno = locked_turno
        if turno.status == AppointmentStatus.CANCELLED:
            return
        should_cancel_external = bool(turno.external_calendar_provider and turno.external_event_id)
        if turno.external_calendar_provider in {"drapp", "consultorio_movil"}:
            should_cancel_external = (
                should_cancel_external
                and str(turno.external_status or "").lower()
                in {"reserved", "confirmed", "cancellation_failed"}
                and turno.status != AppointmentStatus.DRAFT
            )
        if should_cancel_external:
            try:
                await self._calendar.cancel_slot(
                    tenant,
                    consultorio,
                    turno.external_calendar_provider,
                    turno.external_event_id,
                )
            except Exception:
                turno.external_status = "cancellation_failed"
                turno.cancellation_reason = "external_cancellation_failed"
                await audit_log(
                    self._session,
                    request,
                    None,
                    action="cancel_failed",
                    entity="turno",
                    entity_id=turno.id,
                    tenant_id=tenant.id,
                    metadata={"provider": turno.external_calendar_provider},
                )
                await create_notification(
                    self._session,
                    title="Cancelacion de turno pendiente",
                    message=f"No se pudo liberar externamente el turno #{turno.id}.",
                    notif_type="warning",
                    tenant_id=tenant.id,
                )
                await self._session.flush()
                raise
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
        await self._apply_cancellation_economic_policy(request, turno)
        await create_notification(
            self._session,
            title="Turno cancelado",
            message=f"Turno #{turno.id} cancelado.",
            notif_type="warning",
            tenant_id=tenant.id,
        )
        await self._session.commit()

    async def cancellation_economic_outcome(
        self,
        turno: Turno,
    ) -> PaymentReuseStatus | None:
        has_approved_payment = (
            await self._session.execute(
                select(Payment.id)
                .where(
                    Payment.tenant_id == turno.tenant_id,
                    Payment.patient_id == turno.paciente_id,
                    Payment.appointment_id == turno.id,
                    Payment.status == PaymentStatus.APPROVED,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if has_approved_payment is None:
            return None
        if self._cancellation_has_minimum_notice(turno):
            return PaymentReuseStatus.CREDIT_ISSUED
        return PaymentReuseStatus.NON_REUSABLE

    @staticmethod
    def _cancellation_has_minimum_notice(turno: Turno) -> bool:
        timezone_name = turno.timezone or "America/Argentina/Buenos_Aires"
        if timezone_name == "America/Argentina/Buenos_Aires":
            operational_timezone = get_ba_tz()
        else:
            try:
                operational_timezone = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                operational_timezone = get_ba_tz()
        starts_at = turno.start_at or turno.fecha_hora
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=operational_timezone)
        else:
            starts_at = starts_at.astimezone(operational_timezone)
        current = now_ba().astimezone(operational_timezone)
        return starts_at - current >= timedelta(hours=24)

    async def _apply_cancellation_economic_policy(
        self,
        request: Request | None,
        turno: Turno,
    ) -> None:
        payment = (
            await self._session.execute(
                select(Payment)
                .where(
                    Payment.tenant_id == turno.tenant_id,
                    Payment.patient_id == turno.paciente_id,
                    Payment.appointment_id == turno.id,
                    Payment.status == PaymentStatus.APPROVED,
                )
                .order_by(Payment.id.desc())
                .with_for_update()
                .limit(1)
            )
        ).scalar_one_or_none()
        if payment is None:
            await audit_log(
                self._session,
                request,
                None,
                action="cancel_economic_decision",
                entity="turno",
                entity_id=turno.id,
                tenant_id=turno.tenant_id,
                metadata={
                    "outcome": "no_approved_payment",
                    "turno_id": turno.id,
                    "threshold_hours": 24,
                },
            )
            return

        if not self._cancellation_has_minimum_notice(turno):
            payment.reuse_status = PaymentReuseStatus.NON_REUSABLE
            await audit_log(
                self._session,
                request,
                None,
                action="cancel_economic_decision",
                entity="payment",
                entity_id=payment.id,
                tenant_id=payment.tenant_id,
                metadata={
                    "outcome": PaymentReuseStatus.NON_REUSABLE.value,
                    "turno_id": turno.id,
                    "threshold_hours": 24,
                },
            )
            return

        credit = (
            await self._session.execute(
                select(PatientCredit)
                .where(PatientCredit.source_payment_id == payment.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if credit is None:
            credit = PatientCredit(
                tenant_id=payment.tenant_id,
                patient_id=payment.patient_id,
                source_payment_id=payment.id,
                amount=payment.amount,
                currency=payment.currency,
                status=PatientCreditStatus.ACTIVE,
                expires_at=now_ba() + timedelta(days=7),
            )
            self._session.add(credit)
            await self._session.flush()
        payment.reuse_status = PaymentReuseStatus.CREDIT_ISSUED
        await audit_log(
            self._session,
            request,
            None,
            action="cancel_economic_decision",
            entity="payment",
            entity_id=payment.id,
            tenant_id=payment.tenant_id,
            metadata={
                "outcome": PaymentReuseStatus.CREDIT_ISSUED.value,
                "turno_id": turno.id,
                "threshold_hours": 24,
            },
        )
