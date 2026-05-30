from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from app.core.security import hash_password
from app.core.timezone import now_ba
from app.models.user import UserRole
from app.repositories.conversacion_repository import ConversacionRepository
from app.repositories.paciente_repository import PacienteRepository
from app.services.conversation_service import ConversationService, ConversationState
from app.tests.conftest import (
    create_conversation_history,
    create_conversation_state,
    create_consultorio,
    create_paciente,
    create_tenant,
    create_turno,
    create_user,
    get_tenant,
    login,
)


def _service(session):
    return ConversationService(
        session=session,
        paciente_repo=PacienteRepository(session),
        conversacion_repo=ConversacionRepository(session),
    )


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token no encontrado"
    return match.group(1)


def test_timeout_restarts_registered_patient_flow(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Timeout", "whatsapp:+851"))
    phone = "5491185100001"
    asyncio.run(create_paciente(db_session, tenant_id, phone, nombre="Maria", apellido="Lopez"))

    async def seed_and_run():
        async with db_session() as session:
            await create_conversation_state(
                db_session,
                tenant_id=tenant_id,
                telefono=phone,
                estado_actual=ConversationState.ASK_LAST_NAME.value,
                status="active",
                contexto_json={"first_name": "Maria"},
                updated_at=now_ba() - timedelta(minutes=31),
            )
            tenant = await session.get(__import__("app.models.tenant", fromlist=["Tenant"]).Tenant, tenant_id)
            service = _service(session)
            reply = await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            state = await ConversacionRepository(session).get_state(tenant_id, phone)
            return reply, state

    reply, state = asyncio.run(seed_and_run())
    assert "Hola Maria Lopez" in reply
    assert "1) Turno presencial" in reply
    assert state is not None
    assert state.estado_actual == ConversationState.MAIN_REASON_MENU.value


def test_cancelar_finishes_conversation_like_salir(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Cancel", "whatsapp:+852"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491185200001"
    asyncio.run(create_paciente(db_session, tenant_id, phone))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            await service.process_message(tenant, f"whatsapp:+{phone}", "1")
            reply = await service.process_message(tenant, f"whatsapp:+{phone}", "cancelar")
            state = await ConversacionRepository(session).get_state(tenant_id, phone)
            return reply, state

    reply, state = asyncio.run(run())
    assert "Conversacion finalizada" in reply
    assert state is not None
    assert state.status == "finished"


def test_reiniciar_finishes_conversation_like_salir(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Reiniciar", "whatsapp:+853"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491185300001"
    asyncio.run(create_paciente(db_session, tenant_id, phone))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            await service.process_message(tenant, f"whatsapp:+{phone}", "3")
            reply = await service.process_message(tenant, f"whatsapp:+{phone}", "reiniciar")
            state = await ConversacionRepository(session).get_state(tenant_id, phone)
            return reply, state

    reply, state = asyncio.run(run())
    assert "Conversacion finalizada" in reply
    assert state is not None
    assert state.status == "finished"


def test_resolved_conversation_moves_between_pending_and_resolved_lists(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Resolve List", "whatsapp:+854"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-resolve-list@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    phone = "5491185400001"
    asyncio.run(
        create_conversation_state(
            db_session,
            tenant_id=tenant_id,
            telefono=phone,
            estado_actual=ConversationState.MAIN_REASON_MENU.value,
            status="pending",
            pending_reason="otra_consulta",
            pending_message="Consulta pendiente",
        )
    )
    login(client, "tenant-resolve-list@test.com", "secret-123")

    detail = client.get(f"/t/conversation-states/{phone}")
    csrf = _extract_csrf(detail.text)
    result = client.post(
        f"/t/conversation-states/{phone}/resolve",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert result.status_code in (302, 303)

    pending = client.get("/t/conversation-states?status=pending")
    resolved = client.get("/t/conversation-states?status=resolved")
    assert phone not in pending.text
    assert phone in resolved.text


def test_unauthenticated_sensitive_routes_redirect_to_login(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Redirect", "whatsapp:+855"))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Consultorio Redirect"))
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, "5491185500001"))
    turno_id = asyncio.run(create_turno(db_session, paciente_id, consultorio_id))

    response_conversations = client.get("/t/conversation-states", follow_redirects=False)
    response_turnos = client.get("/t/turnos", follow_redirects=False)
    response_appointments = client.get(f"/t/appointments/{turno_id}", follow_redirects=False)
    assert response_conversations.status_code == 303
    assert response_turnos.status_code == 303
    assert response_appointments.status_code == 303


def test_tenant_cannot_access_other_tenant_conversation_history_detail(client, db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant History A", "whatsapp:+856"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant History B", "whatsapp:+857"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-history@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_a,
        )
    )
    history_id = asyncio.run(
        create_conversation_history(
            db_session,
            tenant_id=tenant_b,
            telefono="5491185700001",
            estado_actual=ConversationState.MAIN_REASON_MENU.value,
            previous_status="pending",
            pending_reason="receta_orden",
            pending_message="Historial privado",
            resolved_at=now_ba(),
            close_reason="manual_resolve",
        )
    )
    login(client, "tenant-history@test.com", "secret-123")

    response = client.get(f"/t/conversation-states/history/{history_id}")
    assert response.status_code == 404


def test_super_admin_global_conversation_detail_redirects_to_tenants(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Admin Detail", "whatsapp:+858"))
    asyncio.run(
        create_user(
            db_session,
            "admin-global-detail@test.com",
            hash_password("change_me"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    phone = "5491185800001"
    asyncio.run(
        create_conversation_state(
            db_session,
            tenant_id=tenant_id,
            telefono=phone,
            estado_actual=ConversationState.MAIN_REASON_MENU.value,
            status="pending",
            pending_reason="turno_virtual",
            pending_message="Detalle global",
        )
    )
    login(client, "admin-global-detail@test.com", "change_me")

    response = client.get(f"/admin/conversation-states/{tenant_id}/{phone}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/tenants"
