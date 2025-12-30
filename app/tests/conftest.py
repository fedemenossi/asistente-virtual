from __future__ import annotations

import asyncio
import importlib
import os
import re
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


@pytest.fixture(scope="session")
def app_and_db(tmp_path_factory: pytest.TempPathFactory):
    db_path = tmp_path_factory.mktemp("data") / "test.db"
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
    return main.app, db


@pytest.fixture()
def client(app_and_db: tuple[object, object]):
    app, _ = app_and_db
    return TestClient(app)


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


async def create_tenant(db_session, nombre: str, whatsapp: str) -> int:
    from app.models.tenant import Tenant

    async with db_session() as session:
        async with session.begin():
            tenant = Tenant(nombre=nombre, whatsapp_number=whatsapp, activo=True)
            session.add(tenant)
            await session.flush()
            return tenant.id


async def create_user(db_session, email: str, password_hash: str, role: str, tenant_id: int | None) -> int:
    from app.models.user import User

    async with db_session() as session:
        async with session.begin():
            user = User(
                email=email,
                password_hash=password_hash,
                role=role,
                tenant_id=tenant_id,
                active=True,
            )
            session.add(user)
            await session.flush()
            return user.id


async def create_consultorio(db_session, tenant_id: int, nombre: str) -> int:
    from app.models.consultorio import Consultorio, TipoConsultorio

    async with db_session() as session:
        async with session.begin():
            consultorio = Consultorio(
                tenant_id=tenant_id,
                nombre=nombre,
                tipo=TipoConsultorio.PRESENCIAL,
            )
            session.add(consultorio)
            await session.flush()
            return consultorio.id


async def create_paciente(db_session, tenant_id: int, telefono: str) -> int:
    from app.models.paciente import Paciente

    async with db_session() as session:
        async with session.begin():
            paciente = Paciente(
                tenant_id=tenant_id,
                telefono=telefono,
                nombre="Juan",
                apellido="Perez",
                dni="123",
                email="juan@example.com",
            )
            session.add(paciente)
            await session.flush()
            return paciente.id


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
