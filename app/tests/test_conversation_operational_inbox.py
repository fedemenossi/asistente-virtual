from __future__ import annotations

import asyncio
import re

from sqlalchemy import select

from app.core.security import hash_password
from app.core.timezone import now_ba
from app.models.conversation_history import ConversationHistory
from app.models.conversacion import EstadoConversacion
from app.models.user import UserRole
from app.tests.conftest import create_conversation_history, create_tenant, create_user, login


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token no encontrado"
    return match.group(1)


def _ai_context() -> dict:
    return {
        "ai": {
            "last_intent": "book_virtual_appointment",
            "last_confidence": 0.88,
            "last_source": "ai",
            "raw_response": {"must_not_render": "secret"},
            "missing_fields": ["is_first_time"],
            "extracted": {
                "appointment_type": "virtual",
                "appointment_for": "other",
                "other_patient_name": "Juan Perez",
                "other_patient_dni": "40111222",
                "preferred_day": "martes",
                "preferred_time_range": "tarde",
                "urgency_level": "high",
                "needs_human": True,
            },
        }
    }


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


def test_tenant_conversation_list_shows_ai_summary_and_masks_sensitive_data(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant IA Inbox", "whatsapp:+803"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-ia-inbox@test.com",
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
                        telefono="5491180300001",
                        estado_actual="main_reason_menu",
                        status="pending",
                        pending_reason="turno_virtual",
                        pending_message="Turno para hijo",
                        contexto_json=_ai_context(),
                    )
                )

    asyncio.run(seed())
    login(client, "tenant-ia-inbox@test.com", "secret-123")

    response = client.get("/t/conversation-states")

    assert response.status_code == 200
    assert "IA: Turno virtual" in response.text
    assert "88%" in response.text
    assert "Urgencia posible" in response.text
    assert "***222" in response.text
    assert "40111222" not in response.text
    assert "raw_response" not in response.text


def test_tenant_conversation_list_does_not_break_with_empty_context(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Empty IA Inbox", "whatsapp:+804"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-empty-ia-inbox@test.com",
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
                        telefono="5491180400001",
                        estado_actual="main_reason_menu",
                        status="pending",
                        pending_message="Sin contexto IA",
                        contexto_json={},
                    )
                )

    asyncio.run(seed())
    login(client, "tenant-empty-ia-inbox@test.com", "secret-123")

    response = client.get("/t/conversation-states")

    assert response.status_code == 200
    assert "Sin contexto IA" in response.text


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


def test_tenant_conversation_detail_shows_ai_interpretation_and_review_fields(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant IA Detail", "whatsapp:+813"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-ia-detail@test.com",
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
                        telefono="5491181300001",
                        estado_actual="main_reason_menu",
                        status="pending",
                        pending_message="Turno para hijo",
                        contexto_json=_ai_context(),
                    )
                )

    asyncio.run(seed())
    login(client, "tenant-ia-detail@test.com", "secret-123")

    response = client.get("/t/conversation-states/5491181300001")

    assert response.status_code == 200
    assert "Interpretacion de IA" in response.text
    assert "Turno virtual" in response.text
    assert "Nombre paciente" in response.text
    assert "40111222" in response.text
    assert "Correccion humana de intencion" in response.text
    assert "raw_response" not in response.text


def test_human_ai_review_is_saved_in_context(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant IA Review", "whatsapp:+814"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-ia-review@test.com",
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
                        telefono="5491181400001",
                        estado_actual="main_reason_menu",
                        status="pending",
                        contexto_json=_ai_context(),
                    )
                )

    asyncio.run(seed())
    login(client, "tenant-ia-review@test.com", "secret-123")
    detail = client.get("/t/conversation-states/5491181400001")
    csrf = _extract_csrf(detail.text)

    response = client.post(
        "/t/conversation-states/5491181400001/review",
        data={
            "csrf_token": csrf,
            "operational_category": "receta_orden",
            "manual_note": "Revisar",
            "ai_corrected_intent": "recipe_or_order",
            "ai_review_note": "Era receta, no turno.",
            "status_action": "",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def read_state():
        async with db_session() as session:
            return (
                await session.execute(
                    select(EstadoConversacion).where(
                        EstadoConversacion.tenant_id == tenant_id,
                        EstadoConversacion.telefono == "5491181400001",
                    )
                )
            ).scalar_one()

    state = asyncio.run(read_state())
    assert state.contexto_json["ai_review"]["human_corrected_intent"] == "recipe_or_order"
    assert state.contexto_json["ai_review"]["review_note"] == "Era receta, no turno."


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


def test_super_admin_conversation_inbox_redirects_to_tenants(client, db_session):
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

    response = client.get("/admin/conversation-states", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/tenants"


def test_admin_conversation_routes_do_not_expose_tenant_conversation_detail(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Admin IA", "whatsapp:+833"))
    asyncio.run(
        create_user(
            db_session,
            "admin-ia-conversations@test.com",
            hash_password("change_me"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )

    async def seed():
        async with db_session() as session:
            async with session.begin():
                session.add(
                    EstadoConversacion(
                        tenant_id=tenant_id,
                        telefono="5491183300001",
                        estado_actual="main_reason_menu",
                        status="pending",
                        pending_message="Admin IA",
                        contexto_json=_ai_context(),
                    )
                )

    asyncio.run(seed())
    login(client, "admin-ia-conversations@test.com", "change_me")

    listing = client.get("/admin/conversation-states", follow_redirects=False)
    detail = client.get(f"/admin/conversation-states/{tenant_id}/5491183300001", follow_redirects=False)

    assert listing.status_code == 303
    assert listing.headers["location"] == "/admin/tenants"
    assert detail.status_code == 303
    assert detail.headers["location"] == "/admin/tenants"


def test_conversation_history_detail_shows_ai_summary_when_present(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant History IA", "whatsapp:+834"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-history-ia@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    history_id = asyncio.run(
        create_conversation_history(
            db_session,
            tenant_id=tenant_id,
            telefono="5491183400001",
            resolved_at=now_ba(),
            contexto_json=_ai_context(),
            pending_message="Historico IA",
        )
    )
    login(client, "tenant-history-ia@test.com", "secret-123")

    response = client.get(f"/t/conversation-states/history/{history_id}")

    assert response.status_code == 200
    assert "Interpretacion de IA" in response.text
    assert "Turno virtual" in response.text


def test_conversation_history_detail_without_ai_context_does_not_break(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant History No IA", "whatsapp:+835"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-history-no-ia@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    history_id = asyncio.run(
        create_conversation_history(
            db_session,
            tenant_id=tenant_id,
            telefono="5491183500001",
            resolved_at=now_ba(),
            contexto_json={},
            pending_message="Historico sin IA",
        )
    )
    login(client, "tenant-history-no-ia@test.com", "secret-123")

    response = client.get(f"/t/conversation-states/history/{history_id}")

    assert response.status_code == 200
    assert "Historico sin IA" in response.text
    assert "Interpretacion de IA" not in response.text


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


def test_super_admin_sidebar_does_not_show_conversations_link(client, db_session):
    asyncio.run(
        create_user(
            db_session,
            "admin-no-conversations-link@test.com",
            hash_password("change_me"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-no-conversations-link@test.com", "change_me")

    response = client.get("/admin/dashboard")

    assert response.status_code == 200
    assert 'href="/admin/conversation-states"' not in response.text
