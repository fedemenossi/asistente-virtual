from __future__ import annotations

import asyncio
import re

from sqlalchemy import select

from app.core.security import hash_password
from app.models.conversation_history import ConversationHistory
from app.models.conversacion import EstadoConversacion
from app.models.user import UserRole
from app.tests.conftest import create_tenant, create_user, login


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token no encontrado"
    return match.group(1)


def test_tenant_conversation_list_is_isolated(client, db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant Inbox A", "whatsapp:+801"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant Inbox B", "whatsapp:+802"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-inbox-a@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_a,
        )
    )

    async def seed():
        async with db_session() as session:
            async with session.begin():
                session.add_all(
                    [
                        EstadoConversacion(
                            tenant_id=tenant_a,
                            telefono="5491180100001",
                            estado_actual="main_reason_menu",
                            status="pending",
                            pending_reason="turno_presencial",
                            pending_message="Consulta tenant A",
                            operational_category="turno_presencial",
                        ),
                        EstadoConversacion(
                            tenant_id=tenant_b,
                            telefono="5491180200001",
                            estado_actual="main_reason_menu",
                            status="pending",
                            pending_reason="receta_orden",
                            pending_message="Consulta tenant B",
                            operational_category="receta_orden",
                        ),
                    ]
                )

    asyncio.run(seed())
    login(client, "tenant-inbox-a@test.com", "secret-123")

    response = client.get("/t/conversation-states")
    assert response.status_code == 200
    assert "5491180100001" in response.text
    assert "Consulta tenant A" in response.text
    assert "5491180200001" not in response.text
    assert "Consulta tenant B" not in response.text


def test_tenant_conversation_detail_respects_permissions(client, db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant Detail A", "whatsapp:+811"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant Detail B", "whatsapp:+812"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-detail@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_a,
        )
    )

    async def seed():
        async with db_session() as session:
            async with session.begin():
                session.add(
                    EstadoConversacion(
                        tenant_id=tenant_b,
                        telefono="5491181200001",
                        estado_actual="main_reason_menu",
                        status="pending",
                        pending_reason="otra_consulta",
                        pending_message="Privada tenant B",
                    )
                )

    asyncio.run(seed())
    login(client, "tenant-detail@test.com", "secret-123")

    response = client.get("/t/conversation-states/5491181200001")
    assert response.status_code == 404


def test_conversation_review_update_can_resolve_and_reopen(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Review", "whatsapp:+821"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-review@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def seed():
        async with db_session() as session:
            async with session.begin():
                session.add(
                    EstadoConversacion(
                        tenant_id=tenant_id,
                        telefono="5491182100001",
                        estado_actual="main_reason_menu",
                        status="pending",
                        pending_reason="sin_clasificar",
                        pending_message="Necesita seguimiento",
                    )
                )

    asyncio.run(seed())
    login(client, "tenant-review@test.com", "secret-123")

    detail = client.get("/t/conversation-states/5491182100001")
    assert detail.status_code == 200
    csrf = _extract_csrf(detail.text)

    resolve = client.post(
        "/t/conversation-states/5491182100001/review",
        data={
            "csrf_token": csrf,
            "operational_category": "derivacion_humana",
            "manual_note": "Llamar al finalizar consultorio",
            "status_action": "resolved",
        },
        follow_redirects=False,
    )
    assert resolve.status_code in (302, 303)

    async def read_after_resolve():
        async with db_session() as session:
            state = (
                await session.execute(
                    select(EstadoConversacion).where(
                        EstadoConversacion.tenant_id == tenant_id,
                        EstadoConversacion.telefono == "5491182100001",
                    )
                )
            ).scalar_one()
            history = list(
                (
                    await session.execute(
                        select(ConversationHistory).where(
                            ConversationHistory.tenant_id == tenant_id,
                            ConversationHistory.telefono == "5491182100001",
                        )
                    )
                ).scalars()
            )
            return state, history

    state, history = asyncio.run(read_after_resolve())
    assert state.status == "finished"
    assert state.operational_category == "derivacion_humana"
    assert state.manual_note == "Llamar al finalizar consultorio"
    assert len(history) == 1
    assert history[0].operational_category == "derivacion_humana"
    assert history[0].manual_note == "Llamar al finalizar consultorio"

    detail_again = client.get("/t/conversation-states/5491182100001")
    assert detail_again.status_code == 200
    csrf_again = _extract_csrf(detail_again.text)

    reopen = client.post(
        "/t/conversation-states/5491182100001/review",
        data={
            "csrf_token": csrf_again,
            "operational_category": "derivacion_humana",
            "manual_note": "Vuelve a seguimiento",
            "status_action": "pending",
        },
        follow_redirects=False,
    )
    assert reopen.status_code in (302, 303)

    async def read_after_reopen():
        async with db_session() as session:
            return (
                await session.execute(
                    select(EstadoConversacion).where(
                        EstadoConversacion.tenant_id == tenant_id,
                        EstadoConversacion.telefono == "5491182100001",
                    )
                )
            ).scalar_one()

    reopened = asyncio.run(read_after_reopen())
    assert reopened.status == "pending"
    assert reopened.resolved_at is None


def test_super_admin_conversation_inbox_is_global(client, db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant Admin View A", "whatsapp:+831"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant Admin View B", "whatsapp:+832"))
    asyncio.run(
        create_user(
            db_session,
            "admin-conversations@test.com",
            hash_password("change_me"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )

    async def seed():
        async with db_session() as session:
            async with session.begin():
                session.add_all(
                    [
                        EstadoConversacion(
                            tenant_id=tenant_a,
                            telefono="5491183100001",
                            estado_actual="main_reason_menu",
                            status="pending",
                            pending_reason="turno_virtual",
                            pending_message="Tenant A",
                            operational_category="turno_virtual",
                        ),
                        EstadoConversacion(
                            tenant_id=tenant_b,
                            telefono="5491183200001",
                            estado_actual="main_reason_menu",
                            status="pending",
                            pending_reason="receta_orden",
                            pending_message="Tenant B",
                            operational_category="receta_orden",
                        ),
                    ]
                )

    asyncio.run(seed())
    login(client, "admin-conversations@test.com", "change_me")

    response = client.get("/admin/conversation-states")
    assert response.status_code == 200
    assert "Tenant Admin View A" in response.text
    assert "Tenant Admin View B" in response.text
    assert "5491183100001" in response.text
    assert "5491183200001" in response.text


def test_tenant_admin_cannot_access_admin_conversation_inbox(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant No Admin Inbox", "whatsapp:+841"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-no-admin-inbox@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-no-admin-inbox@test.com", "secret-123")

    response = client.get("/admin/conversation-states")
    assert response.status_code == 403
