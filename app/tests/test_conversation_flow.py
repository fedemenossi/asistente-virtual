from __future__ import annotations

import asyncio
import re
from datetime import timedelta

import pytest
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
from app.tests.conftest import create_consultorio, create_paciente, create_tenant, create_turno, create_user, get_tenant, login
from app.integrations.interfaces import CalendarSlot
from app.models.consultorio import TipoConsultorio
from app.models.paciente import Paciente
from app.models.patient_contact_link import PatientContactLink
from app.models.turno import AppointmentStatus, TipoTurno, Turno


def _tools_ai_settings(**overrides):
    data = {
        "enabled": True,
        "tools_enabled": True,
        "availability_lookup_enabled": True,
        "max_offered_slots": 3,
        "require_confirmation_before_booking": True,
    }
    data.update(overrides)
    return data


async def _fake_availability_slots(session, *, tenant_id, consultorio_type, patient_context, preferences, limit=5):
    return {
        "ok": True,
        "source": "calendar" if consultorio_type == "virtual" else "consultorio_movil",
        "consultorio_type": consultorio_type,
        "slots": [
            {
                "slot_id": f"slot-{tenant_id}-{index}",
                "label": label,
                "start_at": f"2026-05-2{index}T18:30:00-03:00",
                "end_at": f"2026-05-2{index}T19:00:00-03:00",
                "timezone": "America/Argentina/Buenos_Aires",
                "provider": "google_calendar",
                "metadata": {},
            }
            for index, label in enumerate(
                ["Martes 28/05 a las 18:30", "Miercoles 29/05 a las 10:00", "Jueves 30/05 a las 16:30"],
                start=1,
            )
        ][:limit],
        "message": None,
        "error": None,
    }


class _IdentifiedPatientTestService(ConversationService):
    """Test driver that selects the legacy patient for downstream flow tests."""

    async def process_message(self, tenant, from_phone, body, media_items=None):
        reply = await super().process_message(tenant, from_phone, body, media_items)
        state = await self._conversacion_repo.get_state(tenant.id, self._normalize_phone(from_phone))
        if state is not None and state.estado_actual == ConversationState.ASK_MANAGED_FOR.value:
            patient = await self._paciente_repo.get_by_phone(tenant.id, from_phone)
            if patient is not None:
                reply = await super().process_message(tenant, from_phone, "1", media_items)
                state = await self._conversacion_repo.get_state(tenant.id, self._normalize_phone(from_phone))
        if state is not None and state.estado_actual == ConversationState.ASK_PATIENT_DNI.value:
            patient = await self._paciente_repo.get_by_phone(tenant.id, from_phone)
            if patient is not None:
                return await super().process_message(tenant, from_phone, patient.dni, media_items)
        return reply


def _service(session):
    return _IdentifiedPatientTestService(
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


def test_free_text_main_menu_saves_extracted_context_and_skips_known_child_data(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant IA Extract", "whatsapp:+718"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491171800001"
    asyncio.run(create_paciente(db_session, tenant_id, phone))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            reply = await service.process_message(
                tenant,
                f"whatsapp:+{phone}",
                "quiero un turno virtual para mi hijo Juan Perez DNI 40111222, el martes a la tarde",
            )

            assert "turno es para Juan Perez" in reply
            assert "Es primera vez?" in reply
            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, phone)
            assert state is not None
            assert state.estado_actual == ConversationState.ASK_VIRTUAL_FIRST_TIME.value
            assert state.contexto_json["for_whom"] == "other"
            assert state.contexto_json["other_first_name"] == "Juan"
            assert state.contexto_json["other_last_name"] == "Perez"
            assert state.contexto_json["other_dni"] == "40111222"
            assert state.contexto_json["appointment"]["preferred_day"] == "martes"
            assert state.contexto_json["appointment"]["preferred_time_range"] == "tarde"
            assert state.contexto_json["ai"]["last_intent"] == "book_virtual_appointment"

    asyncio.run(run())


def test_free_text_recipe_uses_extracted_detail(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Recipe Extract", "whatsapp:+719"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491171900001"
    asyncio.run(create_paciente(db_session, tenant_id, phone))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            reply = await service.process_message(
                tenant,
                f"whatsapp:+{phone}",
                "Necesito receta para mi medicacion habitual",
            )

            assert "Ya tengo el detalle" in reply
            state = await ConversacionRepository(session).get_state(tenant.id, phone)
            assert state is not None
            assert state.status == "pending"
            assert state.pending_reason == "receta_orden"
            assert "mi medicacion habitual" in state.pending_message
            assert state.contexto_json["recipe_order"]["type"] == "receta"

    asyncio.run(run())


def test_free_text_high_urgency_derives_to_human(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Urgency Extract", "whatsapp:+729"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491172900001"
    asyncio.run(create_paciente(db_session, tenant_id, phone))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            reply = await service.process_message(
                tenant,
                f"whatsapp:+{phone}",
                "tengo dolor fuerte en el pecho, es urgente",
            )

            assert "guardia" in reply.lower()
            assert "emergencias" in reply.lower()
            state = await ConversacionRepository(session).get_state(tenant.id, phone)
            assert state is not None
            assert state.status == "pending"
            assert state.pending_reason == "humano"
            assert state.requires_human_review is True

    asyncio.run(run())


def test_tools_disabled_keeps_previous_free_text_flow(db_session):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Tools Disabled",
            "whatsapp:+730",
            ai_settings=_tools_ai_settings(tools_enabled=False),
        )
    )
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491173000001"
    asyncio.run(create_paciente(db_session, tenant_id, phone))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            reply = await service.process_message(
                tenant,
                f"whatsapp:+{phone}",
                "Necesito turno virtual para mi, es primera vez",
            )

            assert "turnos virtuales disponibles" in reply.lower()
            state = await ConversacionRepository(session).get_state(tenant.id, phone)
            assert state is not None
            assert state.status == "pending"
            assert state.estado_actual != ConversationState.ASK_AI_SLOT_SELECTION.value

    asyncio.run(run())


def test_tools_enabled_free_text_offers_virtual_slots(monkeypatch, db_session):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Tools Virtual",
            "whatsapp:+731",
            ai_settings=_tools_ai_settings(),
        )
    )
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491173100001"
    asyncio.run(create_paciente(db_session, tenant_id, phone))
    monkeypatch.setattr("app.services.conversation_service.get_available_appointment_slots", _fake_availability_slots)

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            reply = await service.process_message(
                tenant,
                f"whatsapp:+{phone}",
                "Quiero turno virtual para mi, es primera vez el martes a la tarde",
            )

            assert "Encontre estos turnos virtuales disponibles" in reply
            assert "1) Martes 28/05 a las 18:30" in reply
            state = await ConversacionRepository(session).get_state(tenant.id, phone)
            assert state is not None
            assert state.estado_actual == ConversationState.ASK_AI_SLOT_SELECTION.value
            offered = state.contexto_json["appointment"]["offered_slots"]
            assert len(offered) == 3
            assert offered[0]["slot_id"] == f"slot-{tenant_id}-1"
            assert state.contexto_json["appointment"]["awaiting_slot_selection"] is True

    asyncio.run(run())


def test_tools_enabled_missing_data_asks_before_lookup(monkeypatch, db_session):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Tools Missing",
            "whatsapp:+732",
            ai_settings=_tools_ai_settings(),
        )
    )
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491173200001"
    asyncio.run(create_paciente(db_session, tenant_id, phone))

    async def fail_lookup(*args, **kwargs):
        raise AssertionError("No debe consultar disponibilidad con datos incompletos")

    monkeypatch.setattr("app.services.conversation_service.get_available_appointment_slots", fail_lookup)

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            reply = await service.process_message(
                tenant,
                f"whatsapp:+{phone}",
                "Quiero turno virtual",
            )

            assert "El turno virtual es:" in reply
            state = await ConversacionRepository(session).get_state(tenant.id, phone)
            assert state.estado_actual == ConversationState.ASK_VIRTUAL_FOR_WHOM.value

    asyncio.run(run())


def test_slot_selection_and_confirmation_do_not_book(monkeypatch, db_session):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Tools Select",
            "whatsapp:+733",
            ai_settings=_tools_ai_settings(),
        )
    )
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491173300001"
    asyncio.run(create_paciente(db_session, tenant_id, phone))
    monkeypatch.setattr("app.services.conversation_service.get_available_appointment_slots", _fake_availability_slots)

    async def run():
        async with db_session() as session:
            service = _service(session)
            repo = ConversacionRepository(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            await service.process_message(tenant, f"whatsapp:+{phone}", "Quiero turno virtual para mi, es primera vez")

            invalid = await service.process_message(tenant, f"whatsapp:+{phone}", "9")
            assert "Debe seleccionar una opcion valida" in invalid

            selected_reply = await service.process_message(tenant, f"whatsapp:+{phone}", "1")
            assert "Confirmas que queres reservarlo" in selected_reply
            state = await repo.get_state(tenant.id, phone)
            assert state.estado_actual == ConversationState.ASK_AI_BOOKING_CONFIRMATION.value
            assert state.contexto_json["appointment"]["selected_slot"]["option"] == 1

            final_reply = await service.process_message(tenant, f"whatsapp:+{phone}", "1")
            assert "dejo registrada tu seleccion" in final_reply
            state = await repo.get_state(tenant.id, phone)
            assert state.status == "pending"
            assert "reserva_real=no" in state.pending_message

    asyncio.run(run())


def test_simulator_api_identifies_existing_associated_patient_before_menu(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant DNI Existing", "whatsapp:+739"))
    phone = "5491173900001"
    paciente_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            phone,
            nombre="Ana",
            apellido="Identificada",
            dni="30111222",
            email="ana.identificada@example.com",
            obra_social="OSDE",
            insurance_number="A-123",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "admin-dni-existing@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-dni-existing@test.com", "secret-123")
    csrf_token = _extract_csrf(client.get("/admin/chat-simulator").text)

    def send(message: str) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": "whatsapp:+739",
                "from_number": f"whatsapp:+{phone}",
                "message": message,
            },
        )
        assert response.status_code == 200
        return response.json()["reply"]

    first_reply = send("hola")
    assert "para vos" in first_reply.lower()
    assert "Ana Identificada" not in first_reply
    assert "Turno presencial" not in first_reply

    identified_reply = send("1")
    assert "Hola Ana Identificada" in identified_reply
    assert "1) Turno presencial" in identified_reply

    async def load_result():
        async with db_session() as session:
            patients = list(
                (
                    await session.execute(
                        select(Paciente).where(Paciente.tenant_id == tenant_id, Paciente.deleted_at.is_(None))
                    )
                ).scalars().all()
            )
            state = await ConversacionRepository(session).get_state(tenant_id, phone)
            return patients, state

    patients, state = asyncio.run(load_result())
    assert len(patients) == 1
    assert patients[0].email == "ana.identificada@example.com"
    assert patients[0].obra_social == "OSDE"
    assert state.estado_actual == ConversationState.MAIN_REASON_MENU.value
    assert state.contexto_json["patient_id"] == paciente_id


def test_simulator_api_lets_contact_choose_an_associated_patient_without_cross_tenant_exposure(
    client,
    db_session,
):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Shared Contact", "whatsapp:+744"))
    other_tenant_id = asyncio.run(create_tenant(db_session, "Tenant Hidden Contact", "whatsapp:+745"))
    phone = "5491174400001"
    first_patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            phone,
            nombre="Ana",
            apellido="Asociada",
            dni="34111222",
        )
    )
    second_patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            phone,
            nombre="Beto",
            apellido="Asociado",
            dni="35222333",
        )
    )
    asyncio.run(
        create_paciente(
            db_session,
            other_tenant_id,
            phone,
            nombre="Paciente",
            apellido="No Expuesto",
            dni="36333444",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "admin-shared-contact@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-shared-contact@test.com", "secret-123")
    csrf_token = _extract_csrf(client.get("/admin/chat-simulator").text)

    def send(message: str, *, from_phone: str = phone) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": "whatsapp:+744",
                "from_number": f"whatsapp:+{from_phone}",
                "message": message,
            },
        )
        assert response.status_code == 200
        return response.json()["reply"]

    first_reply = send("hola")
    assert "para vos" in first_reply.lower()
    assert "otra persona" in first_reply.lower()

    patient_options = send("2")
    assert "Ana Asociada" in patient_options
    assert "Beto Asociado" in patient_options
    assert "No Expuesto" not in patient_options

    selected_reply = send("2")
    assert "Hola Beto Asociado" in selected_reply

    async def load_state():
        async with db_session() as session:
            return await ConversacionRepository(session).get_state(tenant_id, phone)

    state = asyncio.run(load_state())
    assert state.contexto_json["patient_id"] == second_patient_id
    assert state.contexto_json["patient_id"] != first_patient_id


def test_simulator_api_links_a_new_patient_to_the_contact_for_later_conversations(client, db_session):
    asyncio.run(create_tenant(db_session, "Tenant New Association", "whatsapp:+746"))
    phone = "5491174600001"
    asyncio.run(
        create_user(
            db_session,
            "admin-new-association@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-new-association@test.com", "secret-123")
    csrf_token = _extract_csrf(client.get("/admin/chat-simulator").text)

    def send(message: str) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": "whatsapp:+746",
                "from_number": f"whatsapp:+{phone}",
                "message": message,
            },
        )
        assert response.status_code == 200
        return response.json()["reply"]

    assert "otra persona" in send("hola").lower()
    assert "DNI" in send("2")
    assert "nombre" in send("37.444.555").lower()
    assert "apellido" in send("Cecilia").lower()
    assert "email" in send("Representada").lower()
    assert "Particular" in send("cecilia.representada@example.com")
    completed_reply = send("3")
    assert "Hola Cecilia Representada" in completed_reply

    send("salir")
    assert "otra persona" in send("hola").lower()
    associated_options = send("2")
    assert "Cecilia Representada" in associated_options


def test_simulator_api_keeps_an_existing_unassociated_patient_private_and_does_not_duplicate_it(
    client,
    db_session,
):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Pending Association", "whatsapp:+747"))
    patient_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            "5491174700099",
            nombre="Dario",
            apellido="Protegido",
            dni="38.555.666",
        )
    )
    phone = "5491174700001"
    asyncio.run(
        create_user(
            db_session,
            "admin-pending-association@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-pending-association@test.com", "secret-123")
    csrf_token = _extract_csrf(client.get("/admin/chat-simulator").text)

    def send(message: str, *, from_phone: str = phone) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": "whatsapp:+747",
                "from_number": f"whatsapp:+{from_phone}",
                "message": message,
            },
        )
        assert response.status_code == 200
        return response.json()["reply"]

    send("hola", from_phone="5491174700099")
    approved_reply = send("1", from_phone="5491174700099")
    assert "Turno presencial" in approved_reply

    send("hola")
    send("2")
    pending_reply = send("38.555.666")
    assert "pendiente" in pending_reply.lower()
    assert "Dario" not in pending_reply
    assert "Protegido" not in pending_reply
    assert "Turno presencial" not in pending_reply

    repeated_reply = send("1")
    assert "pendiente" in repeated_reply.lower()
    assert "Dario" not in repeated_reply

    async def load_result():
        async with db_session() as session:
            patients = list(
                (
                    await session.execute(select(Paciente).where(Paciente.tenant_id == tenant_id))
                ).scalars().all()
            )
            state = await ConversacionRepository(session).get_state(tenant_id, phone)
            links = list(
                (
                    await session.execute(
                        select(PatientContactLink).where(
                            PatientContactLink.tenant_id == tenant_id,
                            PatientContactLink.patient_id == patient_id,
                        )
                    )
                ).scalars().all()
            )
            return patients, state, links

    patients, state, links = asyncio.run(load_result())
    assert len(patients) == 1
    assert "patient_id" not in state.contexto_json
    assert sorted(link.status for link in links) == ["approved", "pending"]


def test_simulator_api_registers_new_private_patient_without_insurance_number(client, db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant DNI Particular", "whatsapp:+740"))
    phone = "5491174000001"
    asyncio.run(
        create_user(
            db_session,
            "admin-dni-private@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-dni-private@test.com", "secret-123")
    csrf_token = _extract_csrf(client.get("/admin/chat-simulator").text)

    def send(message: str) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": "whatsapp:+740",
                "from_number": f"whatsapp:+{phone}",
                "message": message,
            },
        )
        assert response.status_code == 200
        return response.json()["reply"]

    assert "otra persona" in send("hola").lower()
    assert "DNI" in send("1")
    assert "nombre" in send("32.222.333").lower()
    assert "apellido" in send("Berta").lower()
    assert "email" in send("Nueva").lower()
    coverage_reply = send("berta.nueva@example.com")
    assert "1) Obra social" in coverage_reply
    assert "2) Prepaga" in coverage_reply
    assert "3) Particular" in coverage_reply

    async def count_before_coverage():
        async with db_session() as session:
            return len(
                list(
                    (
                        await session.execute(
                            select(Paciente).where(Paciente.tenant_id == tenant_id, Paciente.deleted_at.is_(None))
                        )
                    ).scalars().all()
                )
            )

    assert asyncio.run(count_before_coverage()) == 0
    completed_reply = send("3")
    assert "Hola Berta Nueva" in completed_reply
    assert "1) Turno presencial" in completed_reply

    async def load_patient():
        async with db_session() as session:
            result = await session.execute(select(Paciente).where(Paciente.tenant_id == tenant_id))
            return result.scalar_one()

    patient = asyncio.run(load_patient())
    assert patient.dni == "32222333"
    assert patient.email == "berta.nueva@example.com"
    assert patient.obra_social == "Particular"
    assert patient.insurance_number is None


@pytest.mark.parametrize(
    ("coverage_choice", "coverage_type"),
    [("1", "obra_social"), ("2", "prepaga")],
)
def test_simulator_api_requires_entity_and_member_number_for_health_coverage(
    client,
    db_session,
    coverage_choice,
    coverage_type,
):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant DNI Coverage", "whatsapp:+741"))
    phone = "5491174100001"
    asyncio.run(
        create_user(
            db_session,
            "admin-dni-coverage@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-dni-coverage@test.com", "secret-123")
    csrf_token = _extract_csrf(client.get("/admin/chat-simulator").text)

    def send(message: str) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": "whatsapp:+741",
                "from_number": f"whatsapp:+{phone}",
                "message": message,
            },
        )
        assert response.status_code == 200
        return response.json()["reply"]

    send("hola")
    send("1")
    send("33.333.444")
    send("Carla")
    send("Cobertura")
    send("carla.cobertura@example.com")
    entity_reply = send(coverage_choice)
    assert "entidad" in entity_reply.lower()
    number_reply = send("OSDE")
    assert "numero de afiliado" in number_reply.lower()

    async def count_before_number():
        async with db_session() as session:
            result = await session.execute(select(Paciente).where(Paciente.tenant_id == tenant_id))
            return len(list(result.scalars().all()))

    assert asyncio.run(count_before_number()) == 0
    completed_reply = send("SOC-9988")
    assert "Hola Carla Cobertura" in completed_reply

    async def load_patient():
        async with db_session() as session:
            result = await session.execute(select(Paciente).where(Paciente.tenant_id == tenant_id))
            return result.scalar_one()

    patient = asyncio.run(load_patient())
    assert patient.health_coverage_type == coverage_type
    assert patient.obra_social == "OSDE"
    assert patient.insurance_number == "SOC-9988"


def test_simulator_api_normalizes_dni_and_keeps_patient_identity_isolated_by_tenant(client, db_session):
    tenant_a = asyncio.run(create_tenant(db_session, "Tenant DNI A", "whatsapp:+742"))
    tenant_b = asyncio.run(create_tenant(db_session, "Tenant DNI B", "whatsapp:+743"))
    patient_a = asyncio.run(
        create_paciente(
            db_session,
            tenant_a,
            "5491174200001",
            nombre="Paciente",
            apellido="Tenant A",
            dni="30.555.666",
        )
    )
    patient_b = asyncio.run(
        create_paciente(
            db_session,
            tenant_b,
            "5491174300001",
            nombre="Paciente",
            apellido="Tenant B",
            dni="30555666",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "admin-dni-isolation@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-dni-isolation@test.com", "secret-123")
    csrf_token = _extract_csrf(client.get("/admin/chat-simulator").text)

    def send(to_number: str, from_number: str, message: str) -> str:
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": to_number,
                "from_number": from_number,
                "message": message,
            },
        )
        assert response.status_code == 200
        return response.json()["reply"]

    send("whatsapp:+742", "whatsapp:+5491174200001", "hola")
    reply_a = send("whatsapp:+742", "whatsapp:+5491174200001", "1")
    send("whatsapp:+743", "whatsapp:+5491174300001", "hola")
    reply_b = send("whatsapp:+743", "whatsapp:+5491174300001", "1")

    assert "Hola Paciente Tenant A" in reply_a
    assert "Tenant B" not in reply_a
    assert "Hola Paciente Tenant B" in reply_b
    assert "Tenant A" not in reply_b

    async def load_states():
        async with db_session() as session:
            state_a = await ConversacionRepository(session).get_state(tenant_a, "5491174200001")
            state_b = await ConversacionRepository(session).get_state(tenant_b, "5491174300001")
            return state_a, state_b

    state_a, state_b = asyncio.run(load_states())
    assert state_a.contexto_json["patient_id"] == patient_a
    assert state_b.contexto_json["patient_id"] == patient_b


def test_simulator_api_confirms_google_slot_once_and_returns_calendar_links(client, db_session, monkeypatch):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Google Booking",
            "whatsapp:+737",
            ai_settings=_tools_ai_settings(),
            calendar_settings={"default_timezone": "America/Argentina/Buenos_Aires"},
        )
    )
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Google",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": {"calendar_id": "calendar-1"}},
        )
    )
    phone = "5491173700001"
    paciente_id = asyncio.run(
        create_paciente(
            db_session,
            tenant_id,
            phone,
            nombre="Ana",
            apellido="Gomez",
            dni="30111999",
            obra_social="OSDE",
        )
    )
    asyncio.run(
        create_user(
            db_session,
            "admin-google-booking@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-google-booking@test.com", "secret-123")

    start = now_ba() + timedelta(days=2)
    end = start + timedelta(minutes=30)
    reservation_calls = []

    async def controlled_slots(self, tenant, consultorio, window_start, window_end):
        return [
            CalendarSlot(
                slot_id="google-event-1",
                start_at=start,
                end_at=end,
                timezone="America/Argentina/Buenos_Aires",
                provider="google_calendar",
                calendar_id="calendar-1",
            )
        ]

    async def controlled_reservation(self, tenant, consultorio, slot_id, patient, metadata):
        reservation_calls.append((tenant.id, consultorio.id, slot_id, patient.id, metadata))
        return {
            "event_id": slot_id,
            "calendar_id": "calendar-1",
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "timezone": "America/Argentina/Buenos_Aires",
            "html_link": "https://calendar.google.com/event?eid=google-event-1",
            "meet_link": "https://meet.google.com/abc-defg-hij",
        }

    monkeypatch.setattr("app.services.calendar_service.CalendarService.list_available_slots", controlled_slots)
    monkeypatch.setattr("app.services.calendar_service.CalendarService.reserve_slot", controlled_reservation)

    simulator_page = client.get("/admin/chat-simulator")
    csrf_token = _extract_csrf(simulator_page.text)
    assert "puede modificar el Google Calendar configurado" in simulator_page.text

    def send(message: str):
        response = client.post(
            "/admin/chat-simulator/api",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "to_number": "whatsapp:+737",
                "from_number": f"whatsapp:+{phone}",
                "patient_id": paciente_id,
                "message": message,
            },
        )
        assert response.status_code == 200
        return response.json()["reply"]

    send("hola")
    send("1")
    send("2")
    send("1")
    offered_reply = send("2")
    assert "turnos virtuales disponibles" in offered_reply

    selection_reply = send("1")
    assert "Confirmas que queres reservarlo" in selection_reply
    assert reservation_calls == []

    confirmation_reply = send("1")
    assert "Turno confirmado" in confirmation_reply
    assert "https://calendar.google.com/event?eid=google-event-1" in confirmation_reply
    assert "https://meet.google.com/abc-defg-hij" in confirmation_reply
    assert reservation_calls == [
        (tenant_id, consultorio_id, "google-event-1", paciente_id, {"tenant_id": tenant_id})
    ]

    send("1")
    assert len(reservation_calls) == 1

    async def load_turnos():
        async with db_session() as session:
            result = await session.execute(select(Turno).where(Turno.tenant_id == tenant_id))
            return list(result.scalars().all())

    turnos = asyncio.run(load_turnos())
    assert len(turnos) == 1
    assert turnos[0].status == AppointmentStatus.CONFIRMED
    assert turnos[0].external_event_id == "google-event-1"
    assert turnos[0].external_calendar_id == "calendar-1"
    assert turnos[0].referencia_externa == "https://meet.google.com/abc-defg-hij"


def test_simulator_api_does_not_confirm_when_google_rejects_the_slot(client, db_session, monkeypatch):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Google Conflict",
            "whatsapp:+738",
            ai_settings=_tools_ai_settings(),
        )
    )
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Virtual Conflict",
            tipo=TipoConsultorio.VIRTUAL,
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": {"calendar_id": "calendar-conflict"}},
        )
    )
    phone = "5491173800001"
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, phone, obra_social="OSDE"))
    asyncio.run(
        create_user(
            db_session,
            "admin-google-conflict@test.com",
            hash_password("secret-123"),
            UserRole.SUPER_ADMIN.value,
            None,
        )
    )
    login(client, "admin-google-conflict@test.com", "secret-123")

    start = now_ba() + timedelta(days=3)
    end = start + timedelta(minutes=30)

    async def seed_confirmation_state():
        async with db_session() as session:
            repo = ConversacionRepository(session)
            await repo.upsert_state(
                tenant_id,
                phone,
                ConversationState.ASK_AI_BOOKING_CONFIRMATION.value,
                {
                    "patient_id": paciente_id,
                    "reason": "turno_virtual",
                    "for_whom": "self",
                    "first_time": False,
                    "appointment": {
                        "type": "virtual",
                        "awaiting_booking_confirmation": True,
                        "selected_slot": {
                            "option": 1,
                            "label": "Viernes 30/05 a las 10:00",
                            "start_at": start.isoformat(),
                            "end_at": end.isoformat(),
                            "timezone": "America/Argentina/Buenos_Aires",
                            "provider": "google_calendar",
                            "metadata": {
                                "consultorio_id": consultorio_id,
                                "external_event_id": "google-event-conflict",
                            },
                        },
                    },
                },
            )
            await session.commit()

    asyncio.run(seed_confirmation_state())

    async def rejected_reservation(self, tenant, consultorio, slot_id, patient, metadata):
        raise RuntimeError("Slot no disponible")

    async def no_more_slots(self, tenant, consultorio, window_start, window_end):
        return []

    monkeypatch.setattr("app.services.calendar_service.CalendarService.reserve_slot", rejected_reservation)
    monkeypatch.setattr("app.services.calendar_service.CalendarService.list_available_slots", no_more_slots)

    simulator_page = client.get("/admin/chat-simulator")
    response = client.post(
        "/admin/chat-simulator/api",
        headers={"X-CSRF-Token": _extract_csrf(simulator_page.text)},
        json={
            "to_number": "whatsapp:+738",
            "from_number": f"whatsapp:+{phone}",
            "patient_id": paciente_id,
            "message": "1",
        },
    )

    assert response.status_code == 200
    assert "No pude reservar ese horario" in response.json()["reply"]
    assert "No confirme ningun turno" in response.json()["reply"]
    assert "Turno confirmado" not in response.json()["reply"]

    async def load_result():
        async with db_session() as session:
            turnos_result = await session.execute(select(Turno).where(Turno.tenant_id == tenant_id))
            state = await ConversacionRepository(session).get_state(tenant_id, phone)
            return list(turnos_result.scalars().all()), state

    turnos, state = asyncio.run(load_result())
    assert len(turnos) == 1
    assert turnos[0].status == AppointmentStatus.CANCELLED
    assert not any(turno.status == AppointmentStatus.CONFIRMED for turno in turnos)
    assert state.status == "active"


def test_tools_high_urgency_does_not_lookup(monkeypatch, db_session):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Tools Urgency",
            "whatsapp:+734",
            ai_settings=_tools_ai_settings(),
        )
    )
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491173400001"
    asyncio.run(create_paciente(db_session, tenant_id, phone))

    async def fail_lookup(*args, **kwargs):
        raise AssertionError("No debe consultar disponibilidad ante urgencia")

    monkeypatch.setattr("app.services.conversation_service.get_available_appointment_slots", fail_lookup)

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            reply = await service.process_message(tenant, f"whatsapp:+{phone}", "dolor fuerte en el pecho urgente")

            assert "guardia" in reply.lower()
            state = await ConversacionRepository(session).get_state(tenant.id, phone)
            assert state.status == "pending"
            assert state.pending_reason == "humano"

    asyncio.run(run())


def test_tools_slots_are_tenant_isolated(monkeypatch, db_session):
    tenant_a_id = asyncio.run(
        create_tenant(db_session, "Tenant Tools A", "whatsapp:+735", ai_settings=_tools_ai_settings())
    )
    tenant_b_id = asyncio.run(
        create_tenant(db_session, "Tenant Tools B", "whatsapp:+736", ai_settings=_tools_ai_settings(max_offered_slots=1))
    )
    tenant_a = asyncio.run(get_tenant(db_session, tenant_a_id))
    tenant_b = asyncio.run(get_tenant(db_session, tenant_b_id))
    phone = "5491173500001"
    asyncio.run(create_paciente(db_session, tenant_a_id, phone))
    asyncio.run(create_paciente(db_session, tenant_b_id, phone))
    calls = []

    async def fake_slots(session, *, tenant_id, consultorio_type, patient_context, preferences, limit=5):
        calls.append((tenant_id, limit))
        return await _fake_availability_slots(
            session,
            tenant_id=tenant_id,
            consultorio_type=consultorio_type,
            patient_context=patient_context,
            preferences=preferences,
            limit=limit,
        )

    monkeypatch.setattr("app.services.conversation_service.get_available_appointment_slots", fake_slots)

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant_a, f"whatsapp:+{phone}", "hola")
            await service.process_message(tenant_b, f"whatsapp:+{phone}", "hola")
            await service.process_message(tenant_a, f"whatsapp:+{phone}", "turno virtual para mi, es primera vez")
            await service.process_message(tenant_b, f"whatsapp:+{phone}", "turno virtual para mi, es primera vez")
            repo = ConversacionRepository(session)
            state_a = await repo.get_state(tenant_a.id, phone)
            state_b = await repo.get_state(tenant_b.id, phone)
            assert len(state_a.contexto_json["appointment"]["offered_slots"]) == 3
            assert len(state_b.contexto_json["appointment"]["offered_slots"]) == 1

    asyncio.run(run())
    assert (tenant_a_id, 3) in calls
    assert (tenant_b_id, 1) in calls


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


def test_chat_can_cancel_future_appointment(db_session, monkeypatch):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Cancel Chat", "whatsapp:+737"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491173700001"
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, phone))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Monroe"))
    turno_id = asyncio.run(
        create_turno(
            db_session,
            paciente_id,
            consultorio_id,
            fecha_hora=now_ba().replace(tzinfo=None) + timedelta(days=1),
            status=AppointmentStatus.CONFIRMED,
        )
    )
    cancelled = {}

    async def fake_cancel(self, request, tenant, consultorio, turno):
        cancelled["turno_id"] = turno.id
        turno.status = AppointmentStatus.CANCELLED
        turno.estado = "cancelado"
        await self._session.flush()

    monkeypatch.setattr("app.services.appointment_service.AppointmentService.cancel_turno", fake_cancel)

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            reply = await service.process_message(tenant, f"whatsapp:+{phone}", "quiero cancelar turno")
            assert "Estos son tus turnos activos" in reply
            assert "1)" in reply

            confirm_prompt = await service.process_message(tenant, f"whatsapp:+{phone}", "1")
            assert "Confirmas la cancelacion" in confirm_prompt

            final = await service.process_message(tenant, f"whatsapp:+{phone}", "1")
            assert "fue cancelado y liberado" in final

    asyncio.run(run())
    assert cancelled["turno_id"] == turno_id


def test_identified_patient_menu_offers_cancellation_for_future_confirmed_turno(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Cancel Menu", "whatsapp:+738"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491173800001"
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, phone))
    consultorio_id = asyncio.run(create_consultorio(db_session, tenant_id, "Palermo"))
    asyncio.run(
        create_turno(
            db_session,
            paciente_id,
            consultorio_id,
            fecha_hora=now_ba().replace(tzinfo=None) + timedelta(days=2),
            status=AppointmentStatus.CONFIRMED,
        )
    )

    async def run():
        async with db_session() as session:
            service = _service(session)
            greeting = await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            assert "6) Cancelar un turno" in greeting

            selection = await service.process_message(tenant, f"whatsapp:+{phone}", "6")
            assert "Estos son tus turnos activos" in selection

    asyncio.run(run())


def test_cancellation_lists_only_patient_turnos_with_modality_and_consultorio(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Tenant Cancel List", "whatsapp:+739"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491173900001"
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, phone))
    other_patient_id = asyncio.run(create_paciente(db_session, tenant_id, "5491173900002"))
    presencial_id = asyncio.run(
        create_consultorio(db_session, tenant_id, "Palermo", tipo=TipoConsultorio.PRESENCIAL)
    )
    virtual_id = asyncio.run(
        create_consultorio(db_session, tenant_id, "Online", tipo=TipoConsultorio.VIRTUAL)
    )
    future = now_ba().replace(tzinfo=None) + timedelta(days=3)
    for patient_id, consultorio_id in (
        (paciente_id, presencial_id),
        (paciente_id, virtual_id),
        (other_patient_id, presencial_id),
    ):
        asyncio.run(
            create_turno(
                db_session,
                patient_id,
                consultorio_id,
                fecha_hora=future,
                tipo=(
                    TipoTurno.VIRTUAL
                    if consultorio_id == virtual_id
                    else TipoTurno.PRESENCIAL
                ),
                status=AppointmentStatus.CONFIRMED,
            )
        )

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            listing = await service.process_message(tenant, f"whatsapp:+{phone}", "6")

            assert listing.count("Palermo") == 1
            assert listing.count("Online") == 1
            assert "Presencial" in listing
            assert "Virtual" in listing
            assert future.strftime("%d/%m %H:%M") in listing

    asyncio.run(run())


def test_external_cancellation_failure_stays_confirmed_and_is_handed_off(db_session, monkeypatch):
    tenant_id = asyncio.run(
        create_tenant(
            db_session,
            "Tenant Cancel Failure",
            "whatsapp:+741",
            calendar_settings={"google_credentials_json": "{}"},
        )
    )
    tenant = asyncio.run(get_tenant(db_session, tenant_id))
    phone = "5491174100001"
    paciente_id = asyncio.run(create_paciente(db_session, tenant_id, phone))
    consultorio_id = asyncio.run(
        create_consultorio(
            db_session,
            tenant_id,
            "Google Belgrano",
            proveedor_turnos="google",
            configuracion_externa={"google_calendar": {"calendar_id": "calendar@example.com"}},
        )
    )
    turno_when = now_ba().replace(tzinfo=None) + timedelta(days=2)
    turno_id = asyncio.run(
        create_turno(
            db_session,
            paciente_id,
            consultorio_id,
            fecha_hora=turno_when,
            status=AppointmentStatus.CONFIRMED,
            external_calendar_provider="google",
            external_calendar_id="calendar@example.com",
            external_event_id="google-failure-22",
            external_status="reserved",
        )
    )

    async def failing_cancel(self, external_event_id):
        raise RuntimeError("google unavailable")

    monkeypatch.setattr(
        "app.integrations.google_calendar_provider.GoogleCalendarProvider.cancel_slot",
        failing_cancel,
    )

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, f"whatsapp:+{phone}", "hola")
            await service.process_message(tenant, f"whatsapp:+{phone}", "6")
            confirmation = await service.process_message(tenant, f"whatsapp:+{phone}", "1")
            assert "Google Belgrano" in confirmation
            assert "Presencial" in confirmation
            assert turno_when.strftime("%d/%m %H:%M") in confirmation

            result = await service.process_message(tenant, f"whatsapp:+{phone}", "1")
            assert "No pude cancelar" in result
            assert "fue cancelado y liberado" not in result

            state = await ConversacionRepository(session).get_state(tenant.id, phone)
            turno = await session.get(Turno, turno_id)
            return state, turno

    state, turno = asyncio.run(run())

    assert state.status == "pending"
    assert state.requires_human_review is True
    assert state.pending_reason == "cancelar_turno"
    assert turno.status == AppointmentStatus.CONFIRMED
    assert turno.cancelled_at is None
    assert turno.external_status == "cancellation_failed"
    assert turno.cancellation_reason == "external_cancellation_failed"


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
            assert "para vos o para otra persona" in reply
            assert "1) Turno presencial" not in reply

            repo = ConversacionRepository(session)
            state = await repo.get_state(tenant.id, "5491172100001")
            assert state is not None
            assert state.estado_actual == ConversationState.ASK_MANAGED_FOR.value

    asyncio.run(run())


def test_new_patient_signup_completes_and_shows_main_menu(db_session):
    tenant_id = asyncio.run(create_tenant(db_session, "Consultorio C", "whatsapp:+722"))
    tenant = asyncio.run(get_tenant(db_session, tenant_id))

    async def run():
        async with db_session() as session:
            service = _service(session)
            await service.process_message(tenant, "whatsapp:+5491172200001", "hola")
            await service.process_message(tenant, "whatsapp:+5491172200001", "1")
            await service.process_message(tenant, "whatsapp:+5491172200001", "30123123")
            await service.process_message(tenant, "whatsapp:+5491172200001", "Lucia")
            await service.process_message(tenant, "whatsapp:+5491172200001", "Suarez")
            await service.process_message(tenant, "whatsapp:+5491172200001", "lucia@test.com")
            await service.process_message(tenant, "whatsapp:+5491172200001", "1")
            await service.process_message(tenant, "whatsapp:+5491172200001", "OSDE")
            reply = await service.process_message(tenant, "whatsapp:+5491172200001", "A123")

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
            assert state.estado_actual == ConversationState.ASK_MANAGED_FOR.value

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


def test_web_patient_create_allows_multiple_patients_for_the_same_contact_phone(client, db_session):
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
    assert response.headers["location"] == "/t/pacientes"

    async def count_patients():
        async with db_session() as session:
            result = await session.execute(
                select(Paciente).where(Paciente.tenant_id == tenant_id, Paciente.deleted_at.is_(None))
            )
            return len(list(result.scalars().all()))

    assert asyncio.run(count_patients()) == 2
