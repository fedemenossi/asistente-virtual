from __future__ import annotations

import asyncio
import traceback
import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import consultorio_movil
from app.models.audit_log import AuditLog
from app.models.paciente import Paciente
from app.models.consultorio import Consultorio, TipoConsultorio
from app.models.tenant import Tenant
from app.models.turno import AppointmentStatus, EstadoTurno, TipoTurno, Turno
from app.repositories.conversacion_repository import ConversacionRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.paciente_repository import PacienteRepository

logger = logging.getLogger(__name__)


class ConversationState(str, Enum):
    ASK_FIRST_NAME = "ask_first_name"
    ASK_LAST_NAME = "ask_last_name"
    ASK_DNI = "ask_dni"
    ASK_EMAIL = "ask_email"
    ASK_APPOINTMENT_FOR = "ask_appointment_for"
    ASK_OTHER_DNI = "ask_other_dni"
    ASK_OTHER_CONFIRM = "ask_other_confirm"
    ASK_PRESENTIAL_SLOT = "ask_presential_slot"
    ASK_PRESENTIAL_DNI = "ask_presential_dni"
    MAIN_MENU = "main_menu"
    FIRST_TIME_CHECK = "first_time_check"
    OTHER_DETAIL = "other_detail"
    HUMAN_REASON = "human_reason"


INACTIVITY_TIMEOUT_MINUTES = 30
EXIT_COMMANDS = {"salir", "cancelar", "exit", "reiniciar", "menu"}

MENU_TEXT = (
    "A) Pedir turno presencial\n"
    "B) Pedir turno virtual\n"
    "C) Otras consultas\n"
    "D) Hablar con un asistente humano"
)


class ConversationService:
    def __init__(
        self,
        session: AsyncSession,
        paciente_repo: PacienteRepository,
        conversacion_repo: ConversacionRepository,
        notification_repo: NotificationRepository | None = None,
        cabildo_client: Any | None = None,
    ) -> None:
        self._session = session
        self._paciente_repo = paciente_repo
        self._conversacion_repo = conversacion_repo
        self._notification_repo = notification_repo
        self._email_adapter = TypeAdapter(EmailStr)
        self._cabildo = cabildo_client or consultorio_movil

    async def process_message(
        self,
        tenant: Tenant,
        from_phone: str,
        body: str,
    ) -> str:
        normalized = body.strip()
        lowered = normalized.lower()

        if lowered in EXIT_COMMANDS:
            paciente = await self._paciente_repo.get_by_phone(tenant.id, from_phone)
            if paciente:
                await self._conversacion_repo.upsert_state(
                    tenant.id, from_phone, ConversationState.MAIN_MENU.value, {}
                )
                return f"Perfecto, reiniciamos la conversacion.\n{self._menu_message()}"
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.ASK_FIRST_NAME.value, {}
            )
            return "Perfecto, comencemos de nuevo. Necesito tu nombre."

        paciente = await self._paciente_repo.get_by_phone(tenant.id, from_phone)
        state = await self._conversacion_repo.get_state(tenant.id, from_phone)
        if state and self._state_expired(state.updated_at):
            await self._conversacion_repo.delete_state(tenant.id, from_phone)
            state = None

        if paciente is not None and state is None:
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.MAIN_MENU.value, {}
            )
            option = self._detect_option(normalized)
            if option:
                reply = await self._handle_menu(tenant, from_phone, normalized)
                return f"{self._greeting()}\n{reply}"
            return self._menu_message()

        if paciente is None and state is None:
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.ASK_FIRST_NAME.value, {}
            )
            return f"{self._greeting()}\nPara registrarte necesito tu nombre."

        if state is None:
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.MAIN_MENU.value, {}
            )
            return self._menu_message()

        contexto = state.contexto_json or {}

        if state.estado_actual == ConversationState.ASK_FIRST_NAME.value:
            if not normalized:
                return "Necesito tu nombre para continuar."
            contexto["nombre"] = normalized
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.ASK_LAST_NAME.value, contexto
            )
            return "Gracias. Ahora tu apellido."

        if state.estado_actual == ConversationState.ASK_LAST_NAME.value:
            if not normalized:
                return "Necesito tu apellido para continuar."
            contexto["apellido"] = normalized
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.ASK_DNI.value, contexto
            )
            return "Perfecto. Indicame tu DNI."

        if state.estado_actual == ConversationState.ASK_DNI.value:
            if not self._is_valid_dni(normalized):
                return "Dato incorrecto vuelva a ingresarlo."
            contexto["dni"] = normalized
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.ASK_EMAIL.value, contexto
            )
            return "Gracias. Ahora tu email."

        if state.estado_actual == ConversationState.ASK_EMAIL.value:
            try:
                email = self._email_adapter.validate_python(normalized)
            except ValidationError:
                return "Dato incorrecto vuelva a ingresarlo."

            paciente = Paciente(
                tenant_id=tenant.id,
                telefono=from_phone,
                nombre=contexto.get("nombre", ""),
                apellido=contexto.get("apellido", ""),
                dni=contexto.get("dni", ""),
                email=str(email),
            )
            await self._paciente_repo.create(paciente)
            intent = contexto.get("intent")
            if intent:
                await self._conversacion_repo.upsert_state(
                    tenant.id,
                    from_phone,
                    ConversationState.FIRST_TIME_CHECK.value,
                    {
                        "intent": intent,
                        "patient_id": paciente.id,
                        "patient_name": f"{paciente.nombre} {paciente.apellido}".strip(),
                    },
                )
                return (
                    "Registro completo. Ahora confirmemos el turno.\n"
                    "Es tu primera consulta? Responde SI o NO."
                )
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.MAIN_MENU.value, {}
            )
            return f"Registro completo.\n{MENU_TEXT}"

        if state.estado_actual == ConversationState.MAIN_MENU.value:
            return await self._handle_menu(tenant, from_phone, normalized)

        if state.estado_actual == ConversationState.ASK_APPOINTMENT_FOR.value:
            return await self._handle_appointment_for(tenant, from_phone, normalized, contexto)

        if state.estado_actual == ConversationState.ASK_OTHER_DNI.value:
            return await self._handle_other_dni(tenant, from_phone, normalized, contexto)

        if state.estado_actual == ConversationState.ASK_OTHER_CONFIRM.value:
            return await self._handle_other_confirm(tenant, from_phone, normalized, contexto)

        if state.estado_actual == ConversationState.ASK_PRESENTIAL_SLOT.value:
            return await self._handle_presential_slot(tenant, from_phone, normalized, contexto)

        if state.estado_actual == ConversationState.ASK_PRESENTIAL_DNI.value:
            return await self._handle_presential_dni(tenant, from_phone, normalized, contexto)

        if state.estado_actual == ConversationState.FIRST_TIME_CHECK.value:
            return await self._handle_first_time(tenant, from_phone, normalized, contexto)

        if state.estado_actual == ConversationState.OTHER_DETAIL.value:
            await self._notify(tenant, from_phone, "Nueva consulta de paciente")
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.MAIN_MENU.value, {}
            )
            return "Gracias por la informacion. En breve te contactamos."

        if state.estado_actual == ConversationState.HUMAN_REASON.value:
            await self._notify(tenant, from_phone, "Solicitud de asistente humano")
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.MAIN_MENU.value, {}
            )
            return "Gracias. Te derivamos a un asistente humano."

        await self._conversacion_repo.upsert_state(
            tenant.id, from_phone, ConversationState.MAIN_MENU.value, {}
        )
        return self._menu_message()

    async def _handle_menu(self, tenant: Tenant, telefono: str, selection: str) -> str:
        option = self._detect_option(selection)
        if option in {"turno_presencial", "turno_virtual"}:
            await self._conversacion_repo.upsert_state(
                tenant.id,
                telefono,
                ConversationState.ASK_APPOINTMENT_FOR.value,
                {"intent": option},
            )
            return (
                "El turno es para vos o para otra persona?\n"
                "Responde 1) Para mi  2) Para otra persona."
            )
        if option == "otra":
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.OTHER_DETAIL.value, {}
            )
            return "Contanos en detalle tu consulta en un solo mensaje."
        if option == "humano":
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.HUMAN_REASON.value, {}
            )
            return "Contanos el motivo para derivarte a un asistente."
        return MENU_TEXT

    async def _handle_first_time(
        self,
        tenant: Tenant,
        telefono: str,
        selection: str,
        contexto: dict,
    ) -> str:
        normalized = selection.strip().lower()
        if normalized in {"si", "s", "1"}:
            is_first = True
        elif normalized in {"no", "n", "2"}:
            is_first = False
        else:
            return "No entendi tu respuesta. Responde SI o NO."

        intent = contexto.get("intent")
        patient_name = contexto.get("patient_name")
        if intent == "turno_presencial":
            await self._notify(tenant, telefono, "Solicitud de turno presencial")
            contexto["is_first_time"] = is_first
            return await self._offer_presential_slots(tenant, telefono, contexto)
        if intent == "turno_virtual":
            await self._notify(tenant, telefono, "Solicitud de turno virtual")
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            if is_first:
                return f"Perfecto. Coordinaremos el primer turno virtual{self._who_suffix(patient_name)}."
            return f"Perfecto. Coordinaremos el turno virtual{self._who_suffix(patient_name)}."
        await self._conversacion_repo.upsert_state(
            tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
        )
        return MENU_TEXT

    @staticmethod
    def _detect_option(text: str) -> str | None:
        if not text:
            return None
        normalized = text.strip().lower()
        mapping = {
            "1": "turno_presencial",
            "a": "turno_presencial",
            "presencial": "turno_presencial",
            "turno presencial": "turno_presencial",
            "2": "turno_virtual",
            "b": "turno_virtual",
            "virtual": "turno_virtual",
            "turno virtual": "turno_virtual",
            "3": "otra",
            "c": "otra",
            "otra": "otra",
            "consulta": "otra",
            "otra consulta": "otra",
            "4": "humano",
            "d": "humano",
            "humano": "humano",
            "asistente": "humano",
            "hablar con un asistente": "humano",
        }
        return mapping.get(normalized)

    async def _handle_appointment_for(
        self,
        tenant: Tenant,
        telefono: str,
        selection: str,
        contexto: dict,
    ) -> str:
        normalized = selection.strip().lower()
        if normalized in {"1", "mi", "para mi", "yo"}:
            paciente = await self._paciente_repo.get_by_phone(tenant.id, telefono)
            await self._conversacion_repo.upsert_state(
                tenant.id,
                telefono,
                ConversationState.FIRST_TIME_CHECK.value,
                {
                    "intent": contexto.get("intent"),
                    "appointment_for": "self",
                    "patient_id": getattr(paciente, "id", None),
                    "patient_name": (
                        f"{paciente.nombre} {paciente.apellido}".strip() if paciente else None
                    ),
                },
            )
            return "Es tu primera consulta? Responde SI o NO."
        if normalized in {"2", "otro", "otra", "otra persona"}:
            await self._conversacion_repo.upsert_state(
                tenant.id,
                telefono,
                ConversationState.ASK_OTHER_DNI.value,
                {"intent": contexto.get("intent"), "appointment_for": "other"},
            )
            return "Indicame el DNI de la persona."
        return "Responde 1) Para mi o 2) Para otra persona."

    async def _handle_other_dni(
        self,
        tenant: Tenant,
        telefono: str,
        selection: str,
        contexto: dict,
    ) -> str:
        dni = selection.strip()
        if not self._is_valid_dni(dni):
            return "Dato incorrecto vuelva a ingresarlo."
        paciente = await self._paciente_repo.get_by_dni(tenant.id, dni)
        if paciente:
            await self._conversacion_repo.upsert_state(
                tenant.id,
                telefono,
                ConversationState.ASK_OTHER_CONFIRM.value,
                {
                    "intent": contexto.get("intent"),
                    "appointment_for": "other",
                    "patient_id": paciente.id,
                    "patient_name": f"{paciente.nombre} {paciente.apellido}".strip(),
                },
            )
            return (
                "Verifique en mis registros y encontre a la paciente "
                f"{paciente.nombre} {paciente.apellido}. "
                "Responder SI si es correcto y responder NO si no es correcto."
            )

        await self._conversacion_repo.upsert_state(
            tenant.id,
            telefono,
            ConversationState.ASK_FIRST_NAME.value,
            {
                "dni": dni,
                "intent": contexto.get("intent"),
                "appointment_for": "other",
            },
        )
        return "No encontramos ese DNI. Necesito el nombre de la persona."

    async def _handle_other_confirm(
        self,
        tenant: Tenant,
        telefono: str,
        selection: str,
        contexto: dict,
    ) -> str:
        normalized = selection.strip().lower()
        if normalized in {"si", "s", "1"}:
            await self._conversacion_repo.upsert_state(
                tenant.id,
                telefono,
                ConversationState.FIRST_TIME_CHECK.value,
                {
                    "intent": contexto.get("intent"),
                    "appointment_for": "other",
                    "patient_id": contexto.get("patient_id"),
                    "patient_name": contexto.get("patient_name"),
                },
            )
            return "Es su primera consulta? Responde SI o NO."
        if normalized in {"no", "n", "2"}:
            await self._notify(tenant, telefono, "Solicitud de asistente humano")
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            return "Perfecto. Te derivamos a un asistente humano."
        return "Responder SI si es correcto y responder NO si no es correcto."

    async def _offer_presential_slots(
        self,
        tenant: Tenant,
        telefono: str,
        contexto: dict,
    ) -> str:
        consultorio = await self._get_presential_consultorio(
            tenant.id, proveedor="consultorio_movil"
        )
        if consultorio is None:
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            return (
                "No tengo un consultorio presencial configurado con Consultorio Movil. "
                "Responde D para hablar con un asistente."
            )
        try:
            slots = await asyncio.to_thread(
                self._cabildo.list_next_presential_slots, tenant, consultorio, 5
            )
        except consultorio_movil.CabildoConfigError:
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            return (
                "Por ahora no puedo consultar la disponibilidad presencial automaticamente. "
                "Responde D si queres que un asistente te contacte."
            )
        except Exception:
            logger.exception("cabildo_availability_error")
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            return (
                "No pude consultar los turnos presenciales. Intenta mas tarde "
                "o responde D para hablar con un asistente."
            )

        if not slots:
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            return (
                "En este momento no hay turnos presenciales disponibles. "
                "Puedo derivarte a un asistente si lo deseas (responde D)."
            )

        context_slots = []
        lines = [
            "Estos son los proximos turnos presenciales disponibles en Cabildo:",
        ]
        for idx, slot in enumerate(slots, start=1):
            lines.append(f"{idx}) {slot.label}")
            context_slots.append(
                {
                    "number": idx,
                    "start_at": slot.start_at.isoformat(),
                    "end_at": slot.end_at.isoformat(),
                    "duration_minutes": slot.duration_minutes,
                    "timezone": slot.timezone,
                    "label": slot.label,
                }
            )

        lines.append("")
        lines.append(
            "Responde con el numero del turno que prefieras para reservarlo. "
            "Tambien podes escribir D para hablar con un asistente personal."
        )

        await self._conversacion_repo.upsert_state(
            tenant.id,
            telefono,
            ConversationState.ASK_PRESENTIAL_SLOT.value,
            {
                "slots": context_slots,
                "intent": contexto.get("intent"),
                "appointment_for": contexto.get("appointment_for"),
                "patient_id": contexto.get("patient_id"),
                "patient_name": contexto.get("patient_name"),
                "is_first_time": contexto.get("is_first_time"),
                "consultorio_id": consultorio.id,
            },
        )

        return "\n".join(lines)

    async def _handle_presential_slot(
        self,
        tenant: Tenant,
        telefono: str,
        selection: str,
        contexto: dict,
    ) -> str:
        normalized = selection.strip().lower()
        if normalized in {"d", "humano", "asistente"}:
            await self._notify(tenant, telefono, "Solicitud de asistente humano")
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            return "Gracias. Te derivamos a un asistente humano."

        try:
            selected_number = int(normalized)
        except ValueError:
            return "Responde con el numero del turno que prefieras."

        slots = contexto.get("slots") or []
        selected = next(
            (slot for slot in slots if slot.get("number") == selected_number), None
        )
        if not selected:
            return "Ese numero no es valido. Elegi una de las opciones listadas."

        appointment_for = contexto.get("appointment_for")
        paciente = None
        patient_id = contexto.get("patient_id")
        if patient_id:
            paciente = await self._session.get(Paciente, patient_id)
        if paciente is None and appointment_for != "other":
            paciente = await self._paciente_repo.get_by_phone(tenant.id, telefono)
        if paciente is None:
            if appointment_for == "other":
                await self._conversacion_repo.upsert_state(
                    tenant.id,
                    telefono,
                    ConversationState.ASK_OTHER_DNI.value,
                    {
                        "intent": contexto.get("intent"),
                        "appointment_for": "other",
                    },
                )
                return "Indicame el DNI de la persona."
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.ASK_FIRST_NAME.value, {}
            )
            return "Necesito registrar al paciente. Por favor indicame el nombre."

        if not paciente.dni:
            await self._conversacion_repo.upsert_state(
                tenant.id,
                telefono,
                ConversationState.ASK_PRESENTIAL_DNI.value,
                {
                    "slot": selected,
                    "patient_id": paciente.id,
                },
            )
            return "Para confirmar el turno presencial necesito el DNI. Enviamelo por favor."

        consultorio = await self._get_presential_consultorio(
            tenant.id, proveedor="consultorio_movil"
        )
        if consultorio is None:
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            return (
                "No tengo un consultorio presencial configurado con Consultorio Movil. "
                "Responde D para hablar con un asistente."
            )

        start_at = datetime.fromisoformat(selected["start_at"])
        end_at = datetime.fromisoformat(selected["end_at"])
        selection_info = consultorio_movil.SlotSelection(
            number=selected["number"],
            start_at=start_at,
            end_at=end_at,
            duration_minutes=int(selected.get("duration_minutes") or 0),
            timezone=selected.get("timezone") or "America/Argentina/Buenos_Aires",
            label=selected.get("label") or start_at.strftime("%d/%m %H:%M"),
        )

        return await self._reserve_cabildo_slot(
            tenant,
            telefono,
            consultorio,
            paciente,
            selection_info,
            contexto,
        )

    async def _handle_presential_dni(
        self,
        tenant: Tenant,
        telefono: str,
        selection: str,
        contexto: dict,
    ) -> str:
        if not self._is_valid_dni(selection):
            return "Dato incorrecto vuelva a ingresarlo."

        paciente_id = contexto.get("patient_id")
        slot = contexto.get("slot")
        if not paciente_id or not slot:
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            return "No pude retomar la reserva. Volvamos al menu principal."

        paciente = await self._session.get(Paciente, paciente_id)
        if paciente is None:
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            return "No pude encontrar al paciente. Volvamos al menu principal."

        paciente.dni = selection.strip()
        await self._session.flush()

        consultorio = await self._get_presential_consultorio(
            tenant.id, proveedor="consultorio_movil"
        )
        if consultorio is None:
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            return (
                "No tengo un consultorio presencial configurado con Consultorio Movil. "
                "Responde D para hablar con un asistente."
            )

        start_at = datetime.fromisoformat(slot["start_at"])
        end_at = datetime.fromisoformat(slot["end_at"])
        selection_info = consultorio_movil.SlotSelection(
            number=slot["number"],
            start_at=start_at,
            end_at=end_at,
            duration_minutes=int(slot.get("duration_minutes") or 0),
            timezone=slot.get("timezone") or "America/Argentina/Buenos_Aires",
            label=slot.get("label") or start_at.strftime("%d/%m %H:%M"),
        )

        return await self._reserve_cabildo_slot(
            tenant,
            telefono,
            consultorio,
            paciente,
            selection_info,
            contexto,
        )

    async def _reserve_cabildo_slot(
        self,
        tenant: Tenant,
        telefono: str,
        consultorio: Consultorio,
        paciente: Paciente,
        selection_info: consultorio_movil.SlotSelection,
        contexto: dict,
    ) -> str:
        try:
            reservation = await asyncio.to_thread(
                self._cabildo.reserve_presential_slot,
                tenant,
                consultorio,
                selection_info,
                paciente,
            )
        except RuntimeError as exc:
            logger.exception("cabildo_patient_error")
            await self._audit_cabildo_error(
                tenant=tenant,
                telefono=telefono,
                consultorio=consultorio,
                paciente=paciente,
                selection_info=selection_info,
                error_code="cabildo_patient_error",
                exc=exc,
            )
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            return (
                "No pude registrar al paciente en Consultorio Movil. "
                "Verifica que el DNI, email y telefono sean correctos. "
                "Responde D si queres que un asistente te contacte."
            )
        except consultorio_movil.CabildoSlotUnavailable as exc:
            await self._audit_cabildo_error(
                tenant=tenant,
                telefono=telefono,
                consultorio=consultorio,
                paciente=paciente,
                selection_info=selection_info,
                error_code="cabildo_slot_unavailable",
                exc=exc,
            )
            return await self._offer_presential_slots(tenant, telefono, contexto)
        except consultorio_movil.CabildoConfigError as exc:
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            await self._audit_cabildo_error(
                tenant=tenant,
                telefono=telefono,
                consultorio=consultorio,
                paciente=paciente,
                selection_info=selection_info,
                error_code="cabildo_config_error",
                exc=exc,
            )
            return (
                "No puedo confirmar turnos presenciales en este momento. "
                "Responde D si queres que un asistente te contacte."
            )
        except Exception as exc:
            logger.exception("cabildo_reservation_error")
            await self._audit_cabildo_error(
                tenant=tenant,
                telefono=telefono,
                consultorio=consultorio,
                paciente=paciente,
                selection_info=selection_info,
                error_code="cabildo_reservation_error",
                exc=exc,
            )
            await self._conversacion_repo.upsert_state(
                tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
            )
            return (
                "No pude reservar el turno automaticamente. "
                "Responde D si queres que un asistente te contacte."
            )

        await self._conversacion_repo.upsert_state(
            tenant.id, telefono, ConversationState.MAIN_MENU.value, {}
        )

        try:
            await self._create_cabildo_turno(
                tenant=tenant,
                consultorio=consultorio,
                paciente=paciente,
                reservation=reservation,
            )
        except Exception:
            logger.exception("cabildo_mirror_error")
            await self._notify(tenant, telefono, "Error al guardar turno presencial")

        start_label = reservation["start_at"].strftime("%d/%m a las %H:%M")
        return (
            "Turno presencial reservado para el "
            f"{start_label}. Recorda llegar unos minutos antes y llevar tu DNI."
        )

    async def _audit_cabildo_error(
        self,
        tenant: Tenant,
        telefono: str,
        consultorio: Consultorio,
        paciente: Paciente,
        selection_info: consultorio_movil.SlotSelection,
        error_code: str,
        exc: Exception,
    ) -> None:
        try:
            metadata = {
                "error_code": error_code,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "traceback": traceback.format_exc(),
                "consultorio_id": consultorio.id,
                "paciente_id": paciente.id,
                "telefono": telefono,
                "slot": {
                    "number": selection_info.number,
                    "start_at": selection_info.start_at.isoformat(),
                    "end_at": selection_info.end_at.isoformat(),
                    "duration_minutes": selection_info.duration_minutes,
                    "timezone": selection_info.timezone,
                },
            }
            self._session.add(
                AuditLog(
                    tenant_id=tenant.id,
                    user_id=None,
                    action="error",
                    entity="cabildo",
                    entity_id=consultorio.id,
                    metadata_json=metadata,
                )
            )
            await self._session.flush()
        except Exception:
            logger.exception("cabildo_audit_error")

    async def _create_cabildo_turno(
        self,
        tenant: Tenant,
        consultorio: Consultorio,
        paciente: Paciente,
        reservation: dict,
    ) -> None:
        turno = Turno(
            paciente_id=paciente.id,
            consultorio_id=consultorio.id,
            fecha_hora=reservation["start_at"],
            start_at=reservation["start_at"],
            end_at=reservation["end_at"],
            timezone=reservation.get("timezone"),
            tipo=TipoTurno.PRESENCIAL,
            estado=EstadoTurno.CONFIRMADO,
            status=AppointmentStatus.CONFIRMED,
            origen_externo="cabildo",
            referencia_externa=reservation.get("cabildo_id"),
            external_calendar_provider="cabildo",
        )
        async with self._session.begin_nested():
            self._session.add(turno)
            await self._session.flush()
            self._session.add(
                AuditLog(
                    tenant_id=tenant.id,
                    user_id=None,
                    action="create",
                    entity="turno",
                    entity_id=turno.id,
                    metadata_json={
                        "origen_externo": "cabildo",
                        "referencia_externa": reservation.get("cabildo_id"),
                    },
                )
            )

    async def _get_presential_consultorio(
        self,
        tenant_id: int,
        proveedor: str | None = None,
    ) -> Consultorio | None:
        stmt = select(Consultorio).where(
            Consultorio.tenant_id == tenant_id,
            Consultorio.tipo == TipoConsultorio.PRESENCIAL,
            Consultorio.deleted_at.is_(None),
        )
        if proveedor:
            stmt = stmt.where(Consultorio.proveedor_turnos == proveedor)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    @staticmethod
    def _who_suffix(patient_name: str | None) -> str:
        if not patient_name:
            return ""
        return f" de {patient_name}"

    @staticmethod
    def _greeting() -> str:
        now = datetime.now()
        if now.hour < 12:
            return (
                "Buenos dias, soy la asistente de la Dra Maria Laura Langdon "
                "y te voy a ayudar en lo que necesites."
                
            )
        return (
            "Buenas tardes, soy la asistente de la Dra Maria Laura Langdon "
            "y te voy a ayudar en lo que necesites."

        )

    def _menu_message(self) -> str:
        return f"{self._greeting()}\n{MENU_TEXT}"

    @staticmethod
    def _is_valid_dni(value: str) -> bool:
        return value.isdigit() and len(value) == 8

    @staticmethod
    def _state_expired(updated_at: datetime | None) -> bool:
        if not updated_at:
            return False
        now = datetime.now(timezone.utc)
        value = updated_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return now - value > timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)

    async def _notify(self, tenant: Tenant, telefono: str, title: str) -> None:
        if self._notification_repo is None:
            return
        await self._notification_repo.create(
            title=title,
            message=f"Paciente {telefono} en {tenant.nombre}",
            notif_type="info",
            tenant_id=tenant.id,
        )
