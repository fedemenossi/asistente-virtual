from __future__ import annotations

from enum import Enum

from pydantic import EmailStr, TypeAdapter, ValidationError

from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.repositories.conversacion_repository import ConversacionRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.paciente_repository import PacienteRepository


class ConversationState(str, Enum):
    ASK_FIRST_NAME = "ask_first_name"
    ASK_LAST_NAME = "ask_last_name"
    ASK_DNI = "ask_dni"
    ASK_EMAIL = "ask_email"
    MENU = "menu"


MENU_TEXT = (
    "1) Pedir turno presencial\n"
    "2) Pedir turno virtual\n"
    "3) Otras consultas\n"
    "4) Hablar con un asistente humano"
)


class ConversationService:
    def __init__(
        self,
        paciente_repo: PacienteRepository,
        conversacion_repo: ConversacionRepository,
        notification_repo: NotificationRepository | None = None,
    ) -> None:
        self._paciente_repo = paciente_repo
        self._conversacion_repo = conversacion_repo
        self._notification_repo = notification_repo
        self._email_adapter = TypeAdapter(EmailStr)

    async def process_message(
        self,
        tenant: Tenant,
        from_phone: str,
        body: str,
    ) -> str:
        normalized = body.strip()
        lowered = normalized.lower()

        if lowered == "salir":
            await self._conversacion_repo.delete_state(tenant.id, from_phone)
            return "Conversacion reiniciada. Escribi cualquier mensaje para comenzar."

        paciente = await self._paciente_repo.get_by_phone(tenant.id, from_phone)
        state = await self._conversacion_repo.get_state(tenant.id, from_phone)

        if paciente is not None and state is None:
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.MENU.value, {}
            )
            return f"Hola {paciente.nombre} {paciente.apellido}.\n{MENU_TEXT}"

        if paciente is None and state is None:
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.ASK_FIRST_NAME.value, {}
            )
            return "Hola, para registrarte necesito tu nombre."

        if state is None:
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.MENU.value, {}
            )
            return MENU_TEXT

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
            if not normalized:
                return "Necesito tu DNI para continuar."
            contexto["dni"] = normalized
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.ASK_EMAIL.value, contexto
            )
            return "Gracias. Ahora tu email."

        if state.estado_actual == ConversationState.ASK_EMAIL.value:
            try:
                email = self._email_adapter.validate_python(normalized)
            except ValidationError:
                return "El email no parece valido. Volve a escribirlo."

            paciente = Paciente(
                tenant_id=tenant.id,
                telefono=from_phone,
                nombre=contexto.get("nombre", ""),
                apellido=contexto.get("apellido", ""),
                dni=contexto.get("dni", ""),
                email=str(email),
            )
            await self._paciente_repo.create(paciente)
            await self._conversacion_repo.upsert_state(
                tenant.id, from_phone, ConversationState.MENU.value, {}
            )
            return f"Registro completo. Hola {paciente.nombre} {paciente.apellido}.\n{MENU_TEXT}"

        if state.estado_actual == ConversationState.MENU.value:
            return await self._handle_menu(tenant, from_phone, normalized)

        await self._conversacion_repo.upsert_state(
            tenant.id, from_phone, ConversationState.MENU.value, {}
        )
        return MENU_TEXT

    async def _handle_menu(self, tenant: Tenant, telefono: str, selection: str) -> str:
        match selection:
            case "1":
                await self._notify(tenant, telefono, "Solicitud de turno presencial")
                return "Perfecto. En breve coordinamos un turno presencial."
            case "2":
                await self._notify(tenant, telefono, "Solicitud de turno virtual")
                return "Perfecto. En breve coordinamos un turno virtual."
            case "3":
                await self._notify(tenant, telefono, "Nueva consulta de paciente")
                return "Contanos tu consulta y un asistente la respondra."
            case "4":
                await self._notify(tenant, telefono, "Solicitud de asistente humano")
                return "Te derivamos a un asistente humano."
            case _:
                return MENU_TEXT

    async def _notify(self, tenant: Tenant, telefono: str, title: str) -> None:
        if self._notification_repo is None:
            return
        await self._notification_repo.create(
            title=title,
            message=f"Paciente {telefono} en {tenant.nombre}",
            notif_type="info",
            tenant_id=tenant.id,
        )
