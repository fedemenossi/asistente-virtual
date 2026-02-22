from __future__ import annotations

import asyncio
import re

from sqlalchemy import select

from app.core.security import hash_password
from app.models.conversacion import EstadoConversacion
from app.models.user import UserRole
from app.repositories.conversacion_repository import ConversacionRepository
from app.repositories.paciente_repository import PacienteRepository
from app.services.conversation_service import ConversationService, ConversationState
from app.tests.conftest import create_paciente, create_tenant, create_user, get_tenant, login
from app.models.paciente import Paciente


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


def test_text_outside_menu_does_not_break_state(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Text", "whatsapp:+700"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    asyncio.run(create_paciente(db_session, tenant_id, "5491170000001"))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, "whatsapp:+5491170000001", "hola")
            reply = await service.process_message(tenant, "whatsapp:+5491170000001", "quiero saber precios")
            assert "Para ayudarte" in reply

            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, "5491170000001")
            assert state is not None
            assert state.estado_actual == ConversationState.MAIN_REASON_MENU.value
            assert (state.status or "active") == "active"
            assert state.pending_reason is None

    asyncio.run(run())


def test_turno_presencial_sets_pending(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Pres", "whatsapp:+701"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    asyncio.run(create_paciente(db_session, tenant_id, "5491170100001"))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, "whatsapp:+5491170100001", "hola")
            await service.process_message(tenant, "whatsapp:+5491170100001", "1")
            await service.process_message(tenant, "whatsapp:+5491170100001", "para mi")
            reply = await service.process_message(tenant, "whatsapp:+5491170100001", "si")
            assert "turnos presenciales" in reply.lower()

            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, "5491170100001")
            assert state is not None
            assert state.status == "pending"
            assert state.pending_reason == "turno_presencial"

    asyncio.run(run())


def test_receta_sets_pending(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Rec", "whatsapp:+702"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    asyncio.run(create_paciente(db_session, tenant_id, "5491170200001"))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, "whatsapp:+5491170200001", "hola")
            await service.process_message(tenant, "whatsapp:+5491170200001", "3")
            await service.process_message(tenant, "whatsapp:+5491170200001", "nueva")
            reply = await service.process_message(tenant, "whatsapp:+5491170200001", "ibuprofeno 600")
            assert "se le respondera" in reply.lower()

            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, "5491170200001")
            assert state is not None
            assert state.status == "pending"
            assert state.pending_reason == "receta_orden"

    asyncio.run(run())


def test_otro_sets_pending(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Otro", "whatsapp:+703"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    asyncio.run(create_paciente(db_session, tenant_id, "5491170300001"))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, "whatsapp:+5491170300001", "hola")
            await service.process_message(tenant, "whatsapp:+5491170300001", "4")
            reply = await service.process_message(
                tenant,
                "whatsapp:+5491170300001",
                "Necesito una constancia de atencion para mi trabajo",
            )
            assert "medico le respondera" in reply.lower()

            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, "5491170300001")
            assert state is not None
            assert state.status == "pending"
            assert state.pending_reason == "otra_consulta"

    asyncio.run(run())


def test_resolve_archives_conversation(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Resolve", "whatsapp:+704"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-resolve@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def seed_state():
        async with db_session() as session:
            async with session.begin():
                session.add(
                    EstadoConversacion(
                        tenant_id=tenant_id,
                        telefono="5491170400001",
                        estado_actual=ConversationState.MAIN_REASON_MENU.value,
                        status="pending",
                        pending_reason="turno_virtual",
                        pending_message="Pendiente de respuesta",
                    )
                )

    asyncio.run(seed_state())
    login(client, "tenant-resolve@test.com", "secret-123")

    detail = client.get("/t/conversation-states/5491170400001")
    assert detail.status_code == 200
    csrf = _extract_csrf(detail.text)

    response = client.post(
        "/t/conversation-states/5491170400001/resolve",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    async def check_state():
        async with db_session() as session:
            result = await session.execute(
                select(EstadoConversacion).where(
                    EstadoConversacion.tenant_id == tenant_id,
                    EstadoConversacion.telefono == "5491170400001",
                )
            )
            return result.scalar_one()

    state = asyncio.run(check_state())
    assert state.status == "finished"
    assert state.resolved_at is not None
    assert state.resolved_by is not None


def test_queue_filtering(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Queues", "whatsapp:+705"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-queues@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )

    async def seed_states():
        async with db_session() as session:
            async with session.begin():
                session.add_all(
                    [
                        EstadoConversacion(
                            tenant_id=tenant_id,
                            telefono="5491170500001",
                            estado_actual=ConversationState.MAIN_REASON_MENU.value,
                            status="pending",
                            pending_reason="turno_presencial",
                            pending_message="Presencial",
                        ),
                        EstadoConversacion(
                            tenant_id=tenant_id,
                            telefono="5491170500002",
                            estado_actual=ConversationState.MAIN_REASON_MENU.value,
                            status="pending",
                            pending_reason="receta_orden",
                            pending_message="Receta",
                        ),
                        EstadoConversacion(
                            tenant_id=tenant_id,
                            telefono="5491170500003",
                            estado_actual=ConversationState.MAIN_REASON_MENU.value,
                            status="finished",
                            pending_reason="otra_consulta",
                            pending_message="Finalizada",
                        ),
                    ]
                )

    asyncio.run(seed_states())
    login(client, "tenant-queues@test.com", "secret-123")

    presencial = client.get("/t/conversation-states?queue=turno_presencial")
    assert presencial.status_code == 200
    assert "5491170500001" in presencial.text
    assert "5491170500002" not in presencial.text

    receta = client.get("/t/conversation-states?queue=receta_orden")
    assert receta.status_code == 200
    assert "5491170500002" in receta.text
    assert "5491170500001" not in receta.text

    finished = client.get("/t/conversation-states?queue=finished")
    assert finished.status_code == 200
    assert "5491170500003" in finished.text
    assert "5491170500001" not in finished.text


def test_web_patient_create_does_not_duplicate_by_phone(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Dedup", "whatsapp:+706"))
    asyncio.run(create_paciente(db_session, tenant_id, "5491170600001"))
    asyncio.run(
        create_user(
            db_session,
            "tenant-dedup@test.com",
            hash_password("secret-123"),
            UserRole.TENANT_ADMIN.value,
            tenant_id,
        )
    )
    login(client, "tenant-dedup@test.com", "secret-123")

    form_get = client.get("/t/pacientes/new")
    assert form_get.status_code == 200
    csrf = _extract_csrf(form_get.text)
    response = client.post(
        "/t/pacientes/new",
        data={
            "csrf_token": csrf,
            "nombre": "Nuevo",
            "apellido": "Paciente",
            "telefono": "whatsapp:+5491170600001",
            "dni": "44555666",
            "email": "nuevo@example.com",
            "obra_social": "OSDE",
            "insurance_number": "X1",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert "/t/pacientes/" in response.headers["location"]
    assert response.headers["location"].endswith("/edit")

    async def count_patients():
        async with db_session() as session:
            result = await session.execute(
                select(Paciente).where(Paciente.tenant_id == tenant_id, Paciente.deleted_at.is_(None))
            )
            return len(list(result.scalars().all()))

    assert asyncio.run(count_patients()) == 1
