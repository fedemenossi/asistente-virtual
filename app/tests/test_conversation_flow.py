import asyncio
from datetime import datetime, timedelta, timezone

from app.repositories.conversacion_repository import ConversacionRepository
from app.repositories.paciente_repository import PacienteRepository
from app.services.conversation_service import ConversationService, ConversationState


def _service(session):
    return ConversationService(
        session=session,
        paciente_repo=PacienteRepository(session),
        conversacion_repo=ConversacionRepository(session),
    )


def test_new_patient_flow_creates_patient(db_session):
    from app.tests.conftest import create_tenant, get_tenant

    tenant_id = asyncio.run(create_tenant(db_session, "Tenant A", "whatsapp:+100"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))

    async def run():
        async with db_session() as session:
            service = _service(session)
            reply = await service.process_message(tenant, "whatsapp:+54911", "hola")
            assert "nombre" in reply.lower()
            reply = await service.process_message(tenant, "whatsapp:+54911", "Juan")
            assert "apellido" in reply.lower()
            reply = await service.process_message(tenant, "whatsapp:+54911", "Perez")
            assert "dni" in reply.lower()
            reply = await service.process_message(tenant, "whatsapp:+54911", "12345678")
            assert "email" in reply.lower()
            reply = await service.process_message(tenant, "whatsapp:+54911", "juan@example.com")
            assert "registro completo" in reply.lower()
            paciente_repo = PacienteRepository(session)
            paciente = await paciente_repo.get_by_phone(tenant.id, "whatsapp:+54911")
            assert paciente is not None
            assert paciente.nombre == "Juan"

    asyncio.run(run())


def test_exit_command_resets_to_menu(db_session):
    from app.tests.conftest import create_tenant, get_tenant, create_paciente

    tenant_id = asyncio.run(create_tenant(db_session, "Tenant B", "whatsapp:+200"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+54912"))

    async def run():
        async with db_session() as session:
            service = _service(session)
            reply = await service.process_message(tenant, "whatsapp:+54912", "salir")
            assert "reiniciamos" in reply.lower()
            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, "whatsapp:+54912")
            assert state is not None
            assert state.estado_actual == ConversationState.MAIN_MENU.value

    asyncio.run(run())


def test_state_expiration_resets_flow(db_session):
    from app.tests.conftest import create_tenant, get_tenant

    tenant_id = asyncio.run(create_tenant(db_session, "Tenant C", "whatsapp:+300"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))

    async def run():
        async with db_session() as session:
            repo = ConversacionRepository(session)
            await repo.upsert_state(
                tenant.id,
                "whatsapp:+54913",
                ConversationState.MAIN_MENU.value,
                {},
            )
            state = await repo.get_state(tenant.id, "whatsapp:+54913")
            state.updated_at = datetime.now(timezone.utc) - timedelta(minutes=31)
            await session.flush()

            service = _service(session)
            reply = await service.process_message(tenant, "whatsapp:+54913", "hola")
            assert "nombre" in reply.lower()
            state = await repo.get_state(tenant.id, "whatsapp:+54913")
            assert state is not None
            assert state.estado_actual == ConversationState.ASK_FIRST_NAME.value

    asyncio.run(run())


def test_known_patient_other_person_flow(db_session):
    from app.tests.conftest import create_tenant, get_tenant, create_paciente

    tenant_id = asyncio.run(create_tenant(db_session, "Tenant D", "whatsapp:+400"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    asyncio.run(create_paciente(db_session, tenant_id, "whatsapp:+54914"))

    async def run():
        async with db_session() as session:
            service = _service(session)

            reply = await service.process_message(tenant, "whatsapp:+54914", "A")
            assert "para vos" in reply.lower()
            reply = await service.process_message(tenant, "whatsapp:+54914", "2")
            assert "dni" in reply.lower()

            reply = await service.process_message(tenant, "whatsapp:+54914", "12345678")
            assert "encontre" in reply.lower()
            reply = await service.process_message(tenant, "whatsapp:+54914", "no")
            assert "asistente humano" in reply.lower()

            reply = await service.process_message(tenant, "whatsapp:+54914", "A")
            assert "para vos" in reply.lower()
            reply = await service.process_message(tenant, "whatsapp:+54914", "2")
            assert "dni" in reply.lower()
            reply = await service.process_message(tenant, "whatsapp:+54914", "11223344")
            assert "nombre" in reply.lower()
            reply = await service.process_message(tenant, "whatsapp:+54914", "Maria")
            assert "apellido" in reply.lower()
            reply = await service.process_message(tenant, "whatsapp:+54914", "Lopez")
            assert "dni" in reply.lower()
            reply = await service.process_message(tenant, "whatsapp:+54914", "11223344")
            assert "email" in reply.lower()
            reply = await service.process_message(tenant, "whatsapp:+54914", "maria@example.com")
            assert "primera consulta" in reply.lower()

    asyncio.run(run())
