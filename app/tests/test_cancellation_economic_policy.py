from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.timezone import get_ba_tz
from app.models.audit_log import AuditLog
from app.models.patient_credit import PatientCredit, PatientCreditStatus
from app.models.payment import Payment, PaymentStatus
from app.models.turno import AppointmentStatus
from app.services.appointment_service import AppointmentService
from app.tests.conftest import create_consultorio, create_paciente, create_tenant, create_turno


def test_cancellation_at_24_hours_creates_one_seven_day_credit_and_separate_audit(
    db_session, monkeypatch
):
    fixed_now = datetime(2026, 11, 7, 10, 0, tzinfo=get_ba_tz())
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Early Cancel", "whatsapp:+743"))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "DrApp Norte",
            proveedor_turnos="drapp",
            configuracion_externa={"drapp": {"staff_id": "23"}},
        )
    )
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "5491174300001"))
    turno_id = asyncio.run(
        create_turno(
            db_session,
            paciente_id,
            consultorio_id,
            fecha_hora=(fixed_now + timedelta(hours=24)).replace(tzinfo=None),
            timezone="America/Argentina/Buenos_Aires",
            status=AppointmentStatus.CONFIRMED,
            external_calendar_provider="drapp",
            external_calendar_id="23",
            external_event_id="drapp-economic-23",
            external_status="reserved",
        )
    )

    async def seed_payment() -> int:
        async with db_session() as session:
            async with session.begin():
                payment = Payment(
                    tenant_id=tenant_id,
                    patient_id=paciente_id,
                    appointment_id=turno_id,
                    provider="mercadopago",
                    status=PaymentStatus.APPROVED,
                    amount=42000,
                    currency="ARS",
                )
                session.add(payment)
                await session.flush()
                return payment.id

    payment_id = asyncio.run(seed_payment())
    external_calls: list[str] = []

    def cancel_drapp(tenant, consultorio, external_event_id):
        external_calls.append(external_event_id)
        return {"status": "cancelled"}

    monkeypatch.setattr(
        "app.integrations.drapp_provider.consultorio_movil.cancel_presential_slot",
        cancel_drapp,
    )
    monkeypatch.setattr("app.services.appointment_service.now_ba", lambda: fixed_now)

    async def run():
        from app.models.consultorio import Consultorio
        from app.models.tenant import Tenant
        from app.models.turno import Turno

        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            consultorio = await session.get(Consultorio, consultorio_id)
            turno = await session.get(Turno, turno_id)
            service = AppointmentService(session)

            await service.cancel_turno(None, tenant, consultorio, turno)
            await service.cancel_turno(None, tenant, consultorio, turno)

            payment = await session.get(Payment, payment_id)
            credits = list(
                (
                    await session.execute(
                        select(PatientCredit).where(
                            PatientCredit.source_payment_id == payment_id
                        )
                    )
                ).scalars()
            )
            audits = list(
                (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.tenant_id == tenant_id,
                            AuditLog.entity_id.in_([turno_id, payment_id]),
                        )
                    )
                ).scalars()
            )
            return payment, credits, audits

    payment, credits, audits = asyncio.run(run())

    assert external_calls == ["drapp-economic-23"]
    assert payment.status == PaymentStatus.APPROVED
    assert payment.reuse_status == "credit_issued"
    assert len(credits) == 1
    assert credits[0].patient_id == paciente_id
    assert credits[0].status == PatientCreditStatus.ACTIVE
    assert credits[0].expires_at == (fixed_now + timedelta(days=7)).replace(tzinfo=None)
    assert {audit.action for audit in audits} >= {"cancel", "cancel_economic_decision"}
    assert sum(audit.action == "cancel_economic_decision" for audit in audits) == 1


def test_late_cancellation_releases_slot_but_marks_payment_non_reusable(
    db_session, monkeypatch
):
    fixed_now = datetime(2026, 11, 7, 10, 0, tzinfo=get_ba_tz())
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Late Cancel", "whatsapp:+744"))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "DrApp Sur",
            proveedor_turnos="drapp",
            configuracion_externa={"drapp": {"staff_id": "24"}},
        )
    )
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "5491174400001"))
    turno_id = asyncio.run(
        create_turno(
            db_session,
            paciente_id,
            consultorio_id,
            fecha_hora=(
                fixed_now.astimezone(timezone.utc) + timedelta(hours=23, minutes=59)
            ).replace(tzinfo=None),
            timezone="UTC",
            status=AppointmentStatus.CONFIRMED,
            external_calendar_provider="drapp",
            external_calendar_id="24",
            external_event_id="drapp-late-23",
            external_status="reserved",
        )
    )

    async def seed_payment() -> int:
        async with db_session() as session:
            async with session.begin():
                payment = Payment(
                    tenant_id=tenant_id,
                    patient_id=paciente_id,
                    appointment_id=turno_id,
                    provider="mercadopago",
                    status=PaymentStatus.APPROVED,
                    amount=42000,
                    currency="ARS",
                )
                session.add(payment)
                await session.flush()
                return payment.id

    payment_id = asyncio.run(seed_payment())
    external_calls: list[str] = []

    def cancel_drapp(tenant, consultorio, external_event_id):
        external_calls.append(external_event_id)
        return {"status": "cancelled"}

    monkeypatch.setattr(
        "app.integrations.drapp_provider.consultorio_movil.cancel_presential_slot",
        cancel_drapp,
    )
    monkeypatch.setattr("app.services.appointment_service.now_ba", lambda: fixed_now)

    async def run():
        from app.models.consultorio import Consultorio
        from app.models.tenant import Tenant
        from app.models.turno import Turno

        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            consultorio = await session.get(Consultorio, consultorio_id)
            turno = await session.get(Turno, turno_id)
            await AppointmentService(session).cancel_turno(None, tenant, consultorio, turno)

            payment = await session.get(Payment, payment_id)
            credits = list(
                (
                    await session.execute(
                        select(PatientCredit).where(
                            PatientCredit.source_payment_id == payment_id
                        )
                    )
                ).scalars()
            )
            economic_audit = (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "cancel_economic_decision",
                        AuditLog.entity == "payment",
                        AuditLog.entity_id == payment_id,
                    )
                )
            ).scalar_one()
            return turno, payment, credits, economic_audit

    turno, payment, credits, economic_audit = asyncio.run(run())

    assert external_calls == ["drapp-late-23"]
    assert turno.status == AppointmentStatus.CANCELLED
    assert payment.status == PaymentStatus.APPROVED
    assert payment.reuse_status == "non_reusable"
    assert credits == []
    assert economic_audit.metadata_json["outcome"] == "non_reusable"


def test_cancellation_without_approved_payment_audits_economic_decision(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Unpaid Cancel", "whatsapp:+746"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Consultorio Oeste"))
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "5491174600001"))
    turno_id = asyncio.run(
        create_turno(
            db_session,
            paciente_id,
            consultorio_id,
            fecha_hora=datetime(2026, 11, 10, 10, 0),
            status=AppointmentStatus.CONFIRMED,
        )
    )

    async def run():
        from app.models.consultorio import Consultorio
        from app.models.tenant import Tenant
        from app.models.turno import Turno

        async with db_session() as session:
            tenant = await session.get(Tenant, tenant_id)
            consultorio = await session.get(Consultorio, consultorio_id)
            turno = await session.get(Turno, turno_id)
            await AppointmentService(session).cancel_turno(None, tenant, consultorio, turno)
            return list(
                (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.tenant_id == tenant_id,
                            AuditLog.entity_id == turno_id,
                        )
                    )
                ).scalars()
            )

    audits = asyncio.run(run())

    assert {audit.action for audit in audits} >= {"cancel", "cancel_economic_decision"}
    economic_audit = next(
        audit for audit in audits if audit.action == "cancel_economic_decision"
    )
    assert economic_audit.metadata_json["outcome"] == "no_approved_payment"
