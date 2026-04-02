from __future__ import annotations

import asyncio
import importlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not match:
        raise AssertionError("CSRF token no encontrado")
    return match.group(1)


def _reload_app() -> tuple[object, object]:
    import app.core.config as config

    config.get_settings.cache_clear()
    config.get_database_settings.cache_clear()

    import app.core.db as db
    importlib.reload(db)

    import app.main as main
    importlib.reload(main)

    return main, db


async def _init_db(db_module: object) -> None:
    from app.models.base import Base

    async with db_module.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await db_module.engine.dispose()


@pytest.fixture()
def app_and_db(tmp_path: Path):
    db_path = tmp_path / "test.db"
    os.environ["APP_ENV"] = "test"
    os.environ["APP_NAME"] = "asistente-virtual"
    os.environ["SECRET_KEY"] = "test-secret"
    os.environ["TWILIO_ACCOUNT_SID"] = "test"
    os.environ["TWILIO_AUTH_TOKEN"] = "test"
    os.environ["TWILIO_WHATSAPP_NUMBER"] = "whatsapp:+100000000"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["ADMIN_EMAIL"] = "admin@example.com"
    os.environ["ADMIN_PASSWORD_SEED"] = "change_me"

    main, db = _reload_app()
    asyncio.run(_init_db(db))
    try:
        yield main.app, db
    finally:
        asyncio.run(db.engine.dispose())


@pytest.fixture()
def client(app_and_db: tuple[object, object]):
    app, _ = app_and_db
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def db_session(app_and_db: tuple[object, object]):
    _, db = app_and_db
    return db.AsyncSessionLocal


def login(client: TestClient, email: str, password: str) -> None:
    response = client.get("/login")
    csrf_token = _extract_csrf(response.text)
    result = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert result.status_code in (302, 303)


def build_whatsapp_phone(seed: int) -> str:
    return f"whatsapp:+54911{seed:07d}"


async def login_as_super_admin(
    client: TestClient,
    db_session,
    *,
    email: str = "admin@example.com",
    password: str = "change_me",
) -> str:
    from app.core.security import hash_password
    from app.models.user import UserRole

    await create_user(
        db_session,
        email,
        hash_password(password),
        UserRole.SUPER_ADMIN.value,
        None,
    )
    login(client, email, password)
    return email


async def login_as_tenant_admin(
    client: TestClient,
    db_session,
    tenant_id: int,
    *,
    email: str = "tenant@example.com",
    password: str = "secret-123",
) -> str:
    from app.core.security import hash_password
    from app.models.user import UserRole

    await create_user(
        db_session,
        email,
        hash_password(password),
        UserRole.TENANT_ADMIN.value,
        tenant_id,
    )
    login(client, email, password)
    return email


async def create_tenant(
    db_session,
    nombre: str,
    whatsapp: str,
    *,
    activo: bool = True,
    fantasy_name: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    **extra,
) -> int:
    from app.models.tenant import Tenant

    async with db_session() as session:
        async with session.begin():
            tenant = Tenant(
                nombre=nombre,
                whatsapp_number=whatsapp,
                activo=activo,
                fantasy_name=fantasy_name,
                first_name=first_name,
                last_name=last_name,
                **extra,
            )
            session.add(tenant)
            await session.flush()
            return tenant.id


async def create_user(
    db_session,
    email: str,
    password_hash: str,
    role: str,
    tenant_id: int | None,
    *,
    active: bool = True,
    **extra,
) -> int:
    from app.models.user import User

    async with db_session() as session:
        async with session.begin():
            user = User(
                email=email,
                password_hash=password_hash,
                role=role,
                tenant_id=tenant_id,
                active=active,
                **extra,
            )
            session.add(user)
            await session.flush()
            return user.id


async def create_consultorio(
    db_session,
    tenant_id: int,
    nombre: str,
    *,
    tipo=None,
    proveedor_turnos: str | None = None,
    configuracion_externa: dict | None = None,
    **extra,
) -> int:
    from app.models.consultorio import Consultorio, TipoConsultorio

    async with db_session() as session:
        async with session.begin():
            consultorio = Consultorio(
                tenant_id=tenant_id,
                nombre=nombre,
                tipo=tipo or TipoConsultorio.PRESENCIAL,
                proveedor_turnos=proveedor_turnos,
                configuracion_externa=configuracion_externa,
                **extra,
            )
            session.add(consultorio)
            await session.flush()
            return consultorio.id


async def create_paciente(
    db_session,
    tenant_id: int,
    telefono: str,
    *,
    nombre: str = "Juan",
    apellido: str = "Perez",
    dni: str = "12345678",
    email: str = "juan@example.com",
    obra_social: str | None = None,
    insurance_number: str | None = None,
    **extra,
) -> int:
    from app.models.paciente import Paciente

    async with db_session() as session:
        async with session.begin():
            paciente = Paciente(
                tenant_id=tenant_id,
                telefono=telefono,
                nombre=nombre,
                apellido=apellido,
                dni=dni,
                email=email,
                obra_social=obra_social,
                insurance_number=insurance_number,
                **extra,
            )
            session.add(paciente)
            await session.flush()
            return paciente.id


async def create_turno(
    db_session,
    paciente_id: int,
    consultorio_id: int,
    *,
    fecha_hora: datetime | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    tipo=None,
    estado=None,
    status=None,
    provider: str | None = None,
    external_id: str | None = None,
    external_status: str | None = None,
    notes: str | None = None,
    **extra,
) -> int:
    from app.models.consultorio import Consultorio
    from app.models.turno import AppointmentStatus, EstadoTurno, TipoTurno, Turno

    async with db_session() as session:
        async with session.begin():
            consultorio = await session.get(Consultorio, consultorio_id)
            assert consultorio is not None
            when = fecha_hora or start_at or datetime.now(timezone.utc)
            turno = Turno(
                tenant_id=consultorio.tenant_id,
                paciente_id=paciente_id,
                consultorio_id=consultorio_id,
                fecha_hora=when,
                start_at=start_at or when,
                end_at=end_at,
                tipo=tipo or TipoTurno.PRESENCIAL,
                estado=estado or EstadoTurno.PENDIENTE,
                status=status or AppointmentStatus.DRAFT,
                provider=provider,
                external_id=external_id,
                external_status=external_status,
                notes=notes,
                **extra,
            )
            session.add(turno)
            await session.flush()
            return turno.id


async def create_conversation_state(
    db_session,
    *,
    tenant_id: int,
    telefono: str,
    estado_actual: str,
    status: str = "active",
    contexto_json: dict | None = None,
    pending_reason: str | None = None,
    pending_message: str | None = None,
    conversation_category: str | None = None,
    conversation_subtype: str | None = None,
    operational_category: str | None = None,
    manual_note: str | None = None,
    requires_human_review: bool = False,
    has_media: bool = False,
    last_patient_message: str | None = None,
    media_metadata=None,
    pending_at=None,
    resolved_at=None,
    resolved_by=None,
    updated_at=None,
) -> str:
    from app.models.conversacion import EstadoConversacion

    async with db_session() as session:
        async with session.begin():
            row = EstadoConversacion(
                tenant_id=tenant_id,
                telefono=telefono,
                estado_actual=estado_actual,
                status=status,
                contexto_json=contexto_json or {},
                pending_reason=pending_reason,
                pending_message=pending_message,
                conversation_category=conversation_category,
                conversation_subtype=conversation_subtype,
                operational_category=operational_category,
                manual_note=manual_note,
                requires_human_review=requires_human_review,
                has_media=has_media,
                last_patient_message=last_patient_message,
                media_metadata=media_metadata,
                pending_at=pending_at,
                resolved_at=resolved_at,
                resolved_by=resolved_by,
                updated_at=updated_at or datetime.now(timezone.utc),
            )
            session.add(row)
            return telefono


async def create_conversation_history(
    db_session,
    *,
    tenant_id: int,
    telefono: str,
    resolved_at,
    patient_id: int | None = None,
    estado_actual: str | None = None,
    contexto_json: dict | None = None,
    previous_status: str | None = None,
    pending_reason: str | None = None,
    pending_message: str | None = None,
    conversation_category: str | None = None,
    conversation_subtype: str | None = None,
    operational_category: str | None = None,
    manual_note: str | None = None,
    requires_human_review: bool = False,
    has_media: bool = False,
    last_patient_message: str | None = None,
    media_metadata=None,
    pending_at=None,
    resolved_by=None,
    close_reason: str | None = None,
) -> int:
    from app.models.conversation_history import ConversationHistory

    async with db_session() as session:
        async with session.begin():
            row = ConversationHistory(
                tenant_id=tenant_id,
                telefono=telefono,
                patient_id=patient_id,
                estado_actual=estado_actual,
                contexto_json=contexto_json or {},
                previous_status=previous_status,
                pending_reason=pending_reason,
                pending_message=pending_message,
                conversation_category=conversation_category,
                conversation_subtype=conversation_subtype,
                operational_category=operational_category,
                manual_note=manual_note,
                requires_human_review=requires_human_review,
                has_media=has_media,
                last_patient_message=last_patient_message,
                media_metadata=media_metadata,
                pending_at=pending_at,
                resolved_at=resolved_at,
                resolved_by=resolved_by,
                close_reason=close_reason,
            )
            session.add(row)
            await session.flush()
            return row.id


async def create_payment(db_session, tenant_id: int, paciente_id: int, turno_id: int | None) -> int:
    from app.models.payment import Payment, PaymentStatus

    async with db_session() as session:
        async with session.begin():
            payment = Payment(
                tenant_id=tenant_id,
                patient_id=paciente_id,
                appointment_id=turno_id,
                provider="mercadopago",
                status=PaymentStatus.PENDING,
                amount=100,
                currency="ARS",
                description="Pago test",
            )
            session.add(payment)
            await session.flush()
            return payment.id


async def create_notification(db_session, tenant_id: int | None, title: str) -> int:
    from app.models.notification import Notification

    async with db_session() as session:
        async with session.begin():
            notification = Notification(
                tenant_id=tenant_id,
                title=title,
                message="Mensaje",
                type="info",
            )
            session.add(notification)
            await session.flush()
            return notification.id


async def get_paciente(db_session, paciente_id: int):
    from app.models.paciente import Paciente

    async with db_session() as session:
        return await session.get(Paciente, paciente_id)


async def get_consultorio(db_session, consultorio_id: int):
    from app.models.consultorio import Consultorio

    async with db_session() as session:
        return await session.get(Consultorio, consultorio_id)


async def get_tenant(db_session, tenant_id: int):
    from app.models.tenant import Tenant

    async with db_session() as session:
        return await session.get(Tenant, tenant_id)


async def get_user(db_session, user_id: int):
    from app.models.user import User

    async with db_session() as session:
        return await session.get(User, user_id)


async def get_audit_logs(db_session, entity: str):
    from app.models.audit_log import AuditLog
    from sqlalchemy import select

    async with db_session() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.entity == entity))
        return list(result.scalars().all())


async def get_notifications(db_session):
    from app.models.notification import Notification
    from sqlalchemy import select

    async with db_session() as session:
        result = await session.execute(select(Notification))
        return list(result.scalars().all())


async def get_notification(db_session, notification_id: int):
    from app.models.notification import Notification

    async with db_session() as session:
        return await session.get(Notification, notification_id)
