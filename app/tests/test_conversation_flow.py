from __future__ import annotations

import asyncio
import re

from sqlalchemy import select

from app.core.timezone import now_ba
from app.core.security import hash_password
from app.models.conversation_history import ConversationHistory
from app.models.conversacion import EstadoConversacion
from app.models.tenant import Tenant
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
            assert "Debe seleccionar una opción válida." in reply

            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, "5491170000001")
            assert state is not None
            assert state.estado_actual == ConversationState.MAIN_REASON_MENU.value
            assert (state.status or "active") == "active"
            assert state.pending_reason is None

    asyncio.run(run())


def test_invalid_option_main_menu_keeps_state(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Invalid Main", "whatsapp:+710"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    asyncio.run(create_paciente(db_session, tenant_id, "5491171000001"))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, "whatsapp:+5491171000001", "hola")
            reply = await service.process_message(tenant, "whatsapp:+5491171000001", "cualquier cosa")
            assert "Debe seleccionar una opción válida." in reply
            assert "1) Turno presencial" in reply

            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, "5491171000001")
            assert state is not None
            assert state.estado_actual == ConversationState.MAIN_REASON_MENU.value

    asyncio.run(run())


def test_main_menu_numeric_options_keep_existing_flow(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Menu Options", "whatsapp:+713"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phones = [f"549117130000{index}" for index in range(1, 6)]
    for phone in phones:
        asyncio.run(create_paciente(db_session, tenant_id, phone))

    async def run():
        async with db_session() as session:
            service = _service(session)
            repo = ConversacionRepository(session)
            cases = [
                (phones[0], "1", ConversationState.ASK_PRESENTIAL_FOR_WHOM.value, None),
                (phones[1], "2", ConversationState.ASK_VIRTUAL_FOR_WHOM.value, None),
                (phones[2], "3", ConversationState.ASK_RECIPE_KIND.value, None),
                (phones[3], "4", ConversationState.ASK_OTHER_QUERY.value, None),
                (phones[4], "5", ConversationState.MAIN_REASON_MENU.value, "pending"),
            ]
            for phone, option, expected_state, expected_status in cases:
                await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
                await service.process_message(tenant, f"whatsapp:+{phone}", option)
                state = await repo.get_state(tenant.id, phone)
                assert state is not None
                assert state.estado_actual == expected_state
                if expected_status:
                    assert state.status == expected_status

    asyncio.run(run())


def test_free_text_main_menu_routes_with_ai_rules(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Free Text", "whatsapp:+714"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491171400001"
    asyncio.run(create_paciente(db_session, tenant_id, phone))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            reply = await service.process_message(
                tenant,
                f"whatsapp:+{phone}",
                "necesito turno en consultorio",
            )

            assert "El turno presencial es:" in reply
            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, phone)
            assert state is not None
            assert state.estado_actual == ConversationState.ASK_PRESENTIAL_FOR_WHOM.value
            assert state.status == "active"
            assert state.pending_reason is None

    asyncio.run(run())


def test_free_text_low_confidence_returns_menu(db_session):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant High Confidence",
            "whatsapp:+715",
            ai_settings={"enabled": False, "min_confidence": 0.95},
        )
    )
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491171500001"
    asyncio.run(create_paciente(db_session, tenant_id, phone))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            reply = await service.process_message(
                tenant,
                f"whatsapp:+{phone}",
                "necesito turno en consultorio",
            )

            assert "Debe seleccionar" in reply
            state = await ConversacionRepository(session).get_state(tenant.id, phone)
            assert state.estado_actual == ConversationState.MAIN_REASON_MENU.value

    asyncio.run(run())


def test_ai_settings_are_isolated_between_tenants(db_session):
    tenant_a_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant IA Alto",
            "whatsapp:+716",
            ai_settings={"enabled": False, "min_confidence": 0.95},
        )
    )
    tenant_b_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant IA Normal",
            "whatsapp:+717",
            ai_settings={"enabled": False, "min_confidence": 0.75},
        )
    )
    tenant_a = asyncio.run(get_tenant(db_session, tenant_a_id))
    tenant_b = asyncio.run(get_tenant(db_session, tenant_b_id))
    phone = "5491171600001"
    asyncio.run(create_paciente(db_session, tenant_a_id, phone))
    asyncio.run(create_paciente(db_session, tenant_b_id, phone))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant_a, f"whatsapp:+{phone}", "hola")
            await service.process_message(tenant_b, f"whatsapp:+{phone}", "hola")
            reply_a = await service.process_message(
                tenant_a, f"whatsapp:+{phone}", "necesito turno en consultorio"
            )
            reply_b = await service.process_message(
                tenant_b, f"whatsapp:+{phone}", "necesito turno en consultorio"
            )

            assert "Debe seleccionar" in reply_a
            assert "El turno presencial es:" in reply_b
            repo = ConversacionRepository(session)
            state_a = await repo.get_state(tenant_a.id, phone)
            state_b = await repo.get_state(tenant_b.id, phone)
            assert state_a.estado_actual == ConversationState.MAIN_REASON_MENU.value
            assert state_b.estado_actual == ConversationState.ASK_PRESENTIAL_FOR_WHOM.value

    asyncio.run(run())


def test_invalid_option_first_time_keeps_state(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Invalid First Time", "whatsapp:+711"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    asyncio.run(create_paciente(db_session, tenant_id, "5491171100001"))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, "whatsapp:+5491171100001", "hola")
            await service.process_message(tenant, "whatsapp:+5491171100001", "1")
            await service.process_message(tenant, "whatsapp:+5491171100001", "1")
            reply = await service.process_message(tenant, "whatsapp:+5491171100001", "tal vez")
            assert "Debe seleccionar una opción válida." in reply
            assert "Es primera vez?" in reply

            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, "5491171100001")
            assert state is not None
            assert state.estado_actual == ConversationState.ASK_PRESENTIAL_FIRST_TIME.value

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
            assert state.conversation_category == "PRESCRIPTION_OR_ORDER"
            assert state.conversation_subtype == "NEW_PRESCRIPTION"

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
            assert "consulta recibida" in reply.lower()

            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, "5491170300001")
            assert state is not None
            assert state.status == "pending"
            assert state.pending_reason == "otra_consulta"

    asyncio.run(run())


def test_salir_finishes_conversation_state(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Exit", "whatsapp:+712"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    asyncio.run(create_paciente(db_session, tenant_id, "5491171200001"))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, "whatsapp:+5491171200001", "hola")
            await service.process_message(tenant, "whatsapp:+5491171200001", "1")

            exit_reply = await service.process_message(tenant, "whatsapp:+5491171200001", "salir")
            assert "Conversacion finalizada" in exit_reply

            repo = ConversacionRepository(session)
            state_after_exit = await repo.get_state(tenant.id, "5491171200001")
            assert state_after_exit is not None
            assert state_after_exit.status == "finished"
            assert state_after_exit.resolved_at is not None
            history_result = await session.execute(
                select(ConversationHistory).where(
                    ConversationHistory.tenant_id == tenant.id,
                    ConversationHistory.telefono == "5491171200001",
                )
            )
            history_rows = list(history_result.scalars().all())
            assert len(history_rows) == 1
            assert history_rows[0].close_reason == "exit_command"

            next_reply = await service.process_message(tenant, "whatsapp:+5491171200001", "hola")
            assert "Cual es el motivo de tu consulta?" in next_reply
            state_after_new_message = await repo.get_state(tenant.id, "5491171200001")
            assert state_after_new_message is not None
            assert state_after_new_message.estado_actual == ConversationState.MAIN_REASON_MENU.value
            assert state_after_new_message.status == "active"
            history_result_after_new = await session.execute(
                select(ConversationHistory).where(
                    ConversationHistory.tenant_id == tenant.id,
                    ConversationHistory.telefono == "5491171200001",
                )
            )
            assert len(list(history_result_after_new.scalars().all())) == 1

    asyncio.run(run())


def test_salir_is_tenant_isolated(db_session):
    tenant_a_id = asyncio.run(create_tenant(db_session, "Tenant Exit A", "whatsapp:+726"))
    tenant_b_id = asyncio.run(create_tenant(db_session, "Tenant Exit B", "whatsapp:+727"))
    tenant_a = asyncio.run(get_tenant(db_session, tenant_a_id))
    tenant_b = asyncio.run(get_tenant(db_session, tenant_b_id))
    phone = "5491172600001"
    asyncio.run(create_paciente(db_session, tenant_a_id, phone))
    asyncio.run(create_paciente(db_session, tenant_b_id, phone))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant_a, f"whatsapp:+{phone}", "hola")
            await service.process_message(tenant_b, f"whatsapp:+{phone}", "hola")

            await service.process_message(tenant_a, f"whatsapp:+{phone}", "salir")

            repo = ConversacionRepository(session)
            state_a = await repo.get_state(tenant_a.id, phone)
            state_b = await repo.get_state(tenant_b.id, phone)
            assert state_a is not None
            assert state_b is not None
            assert state_a.status == "finished"
            assert state_b.status == "active"

    asyncio.run(run())


def test_delete_state_does_not_remove_finished_by_default(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Keep Finished", "whatsapp:+728"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491172800001"
    asyncio.run(create_paciente(db_session, tenant_id, phone))

    async def run():
        async with db_session() as session:
            service = _service(session)
            repo = ConversacionRepository(session)

            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            await service.process_message(tenant, f"whatsapp:+{phone}", "salir")

            finished = await repo.get_state(tenant.id, phone)
            assert finished is not None
            assert finished.status == "finished"

            await repo.delete_state(tenant.id, phone)
            still_there = await repo.get_state(tenant.id, phone)
            assert still_there is not None
            assert still_there.status == "finished"

    asyncio.run(run())


def test_first_message_registered_patient_gets_personalized_welcome(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Consultorio A", "whatsapp:+720"))

    async def prepare():
        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                tenant.fantasy_name = "Dra. Lopez"
                paciente = Paciente(
                    tenant_id=tenant_id,
                    telefono="5491172000001",
                    nombre="Maria",
                    apellido="Gomez",
                    dni="30111222",
                    email="maria@example.com",
                )
                session.add(paciente)

    asyncio.run(prepare())
    tenant = asyncio.run(get_tenant(db_session, tenant_id))

    async def run():
        async with db_session() as session:
            service = _service(session)
            reply = await service.process_message(tenant, "whatsapp:+5491172000001", "hola")
            assert "Hola Maria Gomez" in reply
            assert "1) Turno presencial" in reply

            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, "5491172000001")
            assert state is not None
            assert state.estado_actual == ConversationState.MAIN_REASON_MENU.value

    asyncio.run(run())


def test_first_message_unregistered_starts_signup_with_tenant_name(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Consultorio B", "whatsapp:+721"))

    async def prepare():
        async with db_session() as session:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                tenant.fantasy_name = "Consultorio Medico B"

    asyncio.run(prepare())
    tenant = asyncio.run(get_tenant(db_session, tenant_id))

    async def run():
        async with db_session() as session:
            service = _service(session)
            reply = await service.process_message(tenant, "whatsapp:+5491172100001", "hola")
            assert "asistente de Consultorio Medico B" in reply
            assert "Decime tu nombre" in reply
            assert "1) Turno presencial" not in reply

            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, "5491172100001")
            assert state is not None
            assert state.estado_actual == ConversationState.ASK_FIRST_NAME.value

    asyncio.run(run())


def test_new_patient_signup_completes_and_shows_main_menu(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Consultorio C", "whatsapp:+722"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, "whatsapp:+5491172200001", "hola")
            await service.process_message(tenant, "whatsapp:+5491172200001", "Lucia")
            await service.process_message(tenant, "whatsapp:+5491172200001", "Suarez")
            await service.process_message(tenant, "whatsapp:+5491172200001", "30123123")
            await service.process_message(tenant, "whatsapp:+5491172200001", "OSDE")
            await service.process_message(tenant, "whatsapp:+5491172200001", "A123")
            reply = await service.process_message(tenant, "whatsapp:+5491172200001", "lucia@test.com")

            assert "Hola Lucia Suarez" in reply
            assert "1) Turno presencial" in reply

            paciente = await PacienteRepository(session).get_by_phone(tenant.id, "5491172200001")
            assert paciente is not None
            assert paciente.nombre == "Lucia"
            assert paciente.apellido == "Suarez"

            state = await ConversacionRepository(session).get_state(tenant.id, "5491172200001")
            assert state is not None
            assert state.estado_actual == ConversationState.MAIN_REASON_MENU.value

    asyncio.run(run())


def test_active_conversation_does_not_re_evaluate_onboarding(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Consultorio D", "whatsapp:+723"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))

    async def run():
        async with db_session() as session:
            await ConversacionRepository(session).upsert_state(
                tenant.id,
                "5491172300001",
                ConversationState.ASK_LAST_NAME.value,
                {"first_name": "Carlos"},
            )
            service = _service(session)
            reply = await service.process_message(tenant, "whatsapp:+5491172300001", "Perez")
            assert "Indicame tu DNI" in reply

            state = await ConversacionRepository(session).get_state(tenant.id, "5491172300001")
            assert state is not None
            assert state.estado_actual == ConversationState.ASK_DNI.value

    asyncio.run(run())


def test_phone_isolation_between_tenants(db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Consultorio A", "whatsapp:+724"))
    tenant_b = asyncio.run(create_tenant(db_session, "Consultorio B", "whatsapp:+725"))

    async def prepare():
        async with db_session() as session:
            async with session.begin():
                session.add(
                    Paciente(
                        tenant_id=tenant_a,
                        telefono="5491172400001",
                        nombre="Paciente",
                        apellido="TenantA",
                        dni="22333444",
                        email="a@test.com",
                    )
                )

    asyncio.run(prepare())
    tenant_b_obj = asyncio.run(get_tenant(db_session, tenant_b))

    async def run():
        async with db_session() as session:
            service = _service(session)
            reply = await service.process_message(tenant_b_obj, "whatsapp:+5491172400001", "hola")
            assert "asistente de" in reply.lower()
            assert "Paciente TenantA" not in reply

            state = await ConversacionRepository(session).get_state(tenant_b_obj.id, "5491172400001")
            assert state is not None
            assert state.estado_actual == ConversationState.ASK_FIRST_NAME.value

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

    async def check_history():
        async with db_session() as session:
            result = await session.execute(
                select(ConversationHistory).where(
                    ConversationHistory.tenant_id == tenant_id,
                    ConversationHistory.telefono == "5491170400001",
                )
            )
            return list(result.scalars().all())

    history_rows = asyncio.run(check_history())
    assert len(history_rows) == 1
    assert history_rows[0].close_reason == "manual_resolve"


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
                            conversation_category="PRESCRIPTION_OR_ORDER",
                            conversation_subtype="EXPIRED_PRESCRIPTION",
                            has_media=True,
                            requires_human_review=True,
                        ),
                        EstadoConversacion(
                            tenant_id=tenant_id,
                            telefono="5491170500003",
                            estado_actual=ConversationState.MAIN_REASON_MENU.value,
                            status="finished",
                            pending_reason="otra_consulta",
                            pending_message="Finalizada",
                        ),
                        ConversationHistory(
                            tenant_id=tenant_id,
                            telefono="5491170500003",
                            patient_id=None,
                            estado_actual=ConversationState.MAIN_REASON_MENU.value,
                            contexto_json={},
                            previous_status="pending",
                            pending_reason="otra_consulta",
                            pending_message="Finalizada",
                            conversation_category="OTHER_QUERY",
                            conversation_subtype=None,
                            requires_human_review=False,
                            has_media=False,
                            last_patient_message="Finalizada",
                            media_metadata=[],
                            pending_at=None,
                            resolved_at=now_ba(),
                            resolved_by=None,
                            close_reason="manual_resolve",
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

    filtered = client.get(
        "/t/conversation-states?queue=all_pending&category=PRESCRIPTION_OR_ORDER&subtype=EXPIRED_PRESCRIPTION&media_only=1&human_only=1"
    )
    assert filtered.status_code == 200
    assert "5491170500002" in filtered.text
    assert "5491170500001" not in filtered.text


def test_recipe_media_sets_flags(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Media", "whatsapp:+707"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    asyncio.run(create_paciente(db_session, tenant_id, "5491170700001"))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, "whatsapp:+5491170700001", "hola")
            await service.process_message(tenant, "whatsapp:+5491170700001", "3")
            await service.process_message(tenant, "whatsapp:+5491170700001", "orden medica")
            reply = await service.process_message(
                tenant,
                "whatsapp:+5491170700001",
                "",
                media_items=[{"index": 0, "url": "https://example.test/foto.jpg", "content_type": "image/jpeg"}],
            )
            assert "se le respondera" in reply.lower()

            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, "5491170700001")
            assert state is not None
            assert state.status == "pending"
            assert state.has_media is True
            assert state.requires_human_review is True
            assert state.conversation_subtype == "MEDICAL_ORDER"
            assert state.media_metadata is not None

    asyncio.run(run())


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
