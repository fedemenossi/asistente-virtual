from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.repositories.conversacion_repository import ConversacionRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.paciente_repository import PacienteRepository


class ConversationState(str, Enum):
    ASK_FIRST_NAME = "ask_first_name"
    ASK_LAST_NAME = "ask_last_name"
    ASK_DNI = "ask_dni"
    ASK_INSURANCE = "ask_insurance"
    ASK_INSURANCE_NUMBER = "ask_insurance_number"
    MAIN_REASON_MENU = "main_reason_menu"

    ASK_PRESENTIAL_FOR_WHOM = "ask_presential_for_whom"
    ASK_PRESENTIAL_OTHER_FIRST_NAME = "ask_presential_other_first_name"
    ASK_PRESENTIAL_OTHER_LAST_NAME = "ask_presential_other_last_name"
    ASK_PRESENTIAL_OTHER_DNI = "ask_presential_other_dni"
    ASK_PRESENTIAL_OTHER_INSURANCE = "ask_presential_other_insurance"
    ASK_PRESENTIAL_OTHER_INSURANCE_NUMBER = "ask_presential_other_insurance_number"
    ASK_PRESENTIAL_FIRST_TIME = "ask_presential_first_time"

    ASK_VIRTUAL_FOR_WHOM = "ask_virtual_for_whom"
    ASK_VIRTUAL_OTHER_FIRST_NAME = "ask_virtual_other_first_name"
    ASK_VIRTUAL_OTHER_LAST_NAME = "ask_virtual_other_last_name"
    ASK_VIRTUAL_OTHER_DNI = "ask_virtual_other_dni"
    ASK_VIRTUAL_OTHER_INSURANCE = "ask_virtual_other_insurance"
    ASK_VIRTUAL_OTHER_INSURANCE_NUMBER = "ask_virtual_other_insurance_number"
    ASK_VIRTUAL_FIRST_TIME = "ask_virtual_first_time"

    ASK_RECIPE_KIND = "ask_recipe_kind"
    ASK_RECIPE_DETAIL = "ask_recipe_detail"
    ASK_OTHER_QUERY = "ask_other_query"

    # Legacy states kept for backwards compatibility
    MAIN_MENU = "main_menu"
    ASK_EMAIL = "ask_email"
    ASK_APPOINTMENT_FOR = "ask_appointment_for"
    ASK_OTHER_DNI = "ask_other_dni"
    ASK_OTHER_CONFIRM = "ask_other_confirm"
    ASK_PRESENTIAL_SLOT = "ask_presential_slot"
    ASK_PRESENTIAL_DNI = "ask_presential_dni"
    FIRST_TIME_CHECK = "first_time_check"
    OTHER_DETAIL = "other_detail"
    HUMAN_REASON = "human_reason"


INACTIVITY_TIMEOUT_MINUTES = 30
EXIT_COMMANDS = {"salir", "cancelar", "exit", "reiniciar", "menu"}


class ConversationService:
    def __init__(
        self,
        session: AsyncSession,
        paciente_repo: PacienteRepository,
        conversacion_repo: ConversacionRepository,
        notification_repo: NotificationRepository | None = None,
        cabildo_client: object | None = None,  # compatibility
    ) -> None:
        self._session = session
        self._paciente_repo = paciente_repo
        self._conversacion_repo = conversacion_repo
        self._notification_repo = notification_repo

    async def process_message(self, tenant: Tenant, from_phone: str, body: str) -> str:
        normalized_phone = self._normalize_phone(from_phone)
        text = (body or "").strip()
        lowered = text.lower()

        state = await self._conversacion_repo.get_state(tenant.id, normalized_phone)
        if state is None and normalized_phone != from_phone:
            legacy_state = await self._conversacion_repo.get_state(tenant.id, from_phone)
            if legacy_state is not None:
                legacy_state.telefono = normalized_phone
                await self._session.flush()
                state = legacy_state

        if state and self._state_expired(state.updated_at):
            if (state.status or "active").lower() == "active":
                await self._conversacion_repo.delete_state(tenant.id, normalized_phone)
                state = None

        paciente = await self._paciente_repo.get_by_phone(tenant.id, normalized_phone)

        if state is not None and (state.status or "").lower() == "pending" and lowered not in EXIT_COMMANDS:
            return (
                "Tu consulta ya fue derivada y esta pendiente de respuesta humana. "
                "Si queres reiniciar, escribi MENU."
            )

        if lowered in EXIT_COMMANDS:
            if paciente is None:
                await self._conversacion_repo.upsert_state(
                    tenant.id, normalized_phone, ConversationState.ASK_FIRST_NAME.value, {}
                )
                return "Perfecto, reiniciamos. Decime tu nombre."
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.MAIN_REASON_MENU.value,
                {"patient_id": paciente.id},
            )
            return self._known_patient_greeting(tenant, paciente.nombre)

        if state is None:
            if paciente is None:
                await self._conversacion_repo.upsert_state(
                    tenant.id, normalized_phone, ConversationState.ASK_FIRST_NAME.value, {}
                )
                return "Hola, para comenzar necesito tu nombre."
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.MAIN_REASON_MENU.value,
                {"patient_id": paciente.id},
            )
            return self._known_patient_greeting(tenant, paciente.nombre)

        context = state.contexto_json or {}
        current_state = state.estado_actual

        if current_state == ConversationState.ASK_FIRST_NAME.value:
            if not text:
                return "Necesito tu nombre para continuar."
            context["first_name"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id, normalized_phone, ConversationState.ASK_LAST_NAME.value, context
            )
            return "Gracias. Ahora tu apellido."

        if current_state == ConversationState.ASK_LAST_NAME.value:
            if not text:
                return "Necesito tu apellido para continuar."
            context["last_name"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id, normalized_phone, ConversationState.ASK_DNI.value, context
            )
            return "Perfecto. Indicame tu DNI."

        if current_state == ConversationState.ASK_DNI.value:
            if not self._is_valid_dni(text):
                return "DNI invalido. Debe contener solo numeros (7 a 9 digitos)."
            context["dni"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id, normalized_phone, ConversationState.ASK_INSURANCE.value, context
            )
            return "Indicanos tu obra social (o particular)."

        if current_state == ConversationState.ASK_INSURANCE.value:
            if not text:
                return "La obra social no puede estar vacia."
            context["insurance"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_INSURANCE_NUMBER.value,
                context,
            )
            return "Ahora tu numero de afiliado."

        if current_state == ConversationState.ASK_INSURANCE_NUMBER.value:
            if not text:
                return "El numero de afiliado no puede estar vacio."
            context["insurance_number"] = text
            first_name = (context.get("first_name") or context.get("nombre") or "").strip()
            last_name = (context.get("last_name") or context.get("apellido") or "").strip()
            dni = (context.get("dni") or "").strip()
            insurance = (context.get("insurance") or context.get("obra_social") or "").strip()

            if not first_name:
                await self._conversacion_repo.upsert_state(
                    tenant.id, normalized_phone, ConversationState.ASK_FIRST_NAME.value, context
                )
                return "Falta tu nombre para completar el registro. Indicalo por favor."
            if not last_name:
                await self._conversacion_repo.upsert_state(
                    tenant.id, normalized_phone, ConversationState.ASK_LAST_NAME.value, context
                )
                return "Falta tu apellido para completar el registro."
            if not self._is_valid_dni(dni):
                await self._conversacion_repo.upsert_state(
                    tenant.id, normalized_phone, ConversationState.ASK_DNI.value, context
                )
                return "Falta un DNI valido para completar el registro (7 a 9 digitos)."
            if not insurance:
                await self._conversacion_repo.upsert_state(
                    tenant.id, normalized_phone, ConversationState.ASK_INSURANCE.value, context
                )
                return "Falta la obra social para completar el registro."
            await self._conversacion_repo.upsert_state(
                tenant.id, normalized_phone, ConversationState.ASK_EMAIL.value, context
            )
            return "Ahora indicanos tu email."

        if current_state == ConversationState.ASK_EMAIL.value:
            email = text.strip()
            if not self._is_valid_email(email):
                return "Email invalido. Ingresalo nuevamente."
            context["email"] = email
            first_name = (context.get("first_name") or context.get("nombre") or "").strip()
            last_name = (context.get("last_name") or context.get("apellido") or "").strip()
            dni = (context.get("dni") or "").strip()
            insurance = (context.get("insurance") or context.get("obra_social") or "").strip()

            existing = await self._paciente_repo.get_by_phone(tenant.id, normalized_phone)
            if existing is not None:
                existing.nombre = first_name
                existing.apellido = last_name
                existing.dni = dni
                existing.obra_social = insurance
                existing.email = email
                existing.insurance_number = context.get("insurance_number")
                paciente = existing
                await self._session.flush()
            else:
                paciente = Paciente(
                    tenant_id=tenant.id,
                    telefono=normalized_phone,
                    nombre=first_name,
                    apellido=last_name,
                    dni=dni,
                    email=email,
                    obra_social=insurance,
                    insurance_number=context.get("insurance_number"),
                )
                await self._paciente_repo.create(paciente)
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.MAIN_REASON_MENU.value,
                {"patient_id": paciente.id, "insurance_number": context.get("insurance_number")},
            )
            return self._known_patient_greeting(tenant, paciente.nombre)

        if current_state in {ConversationState.MAIN_REASON_MENU.value, ConversationState.MAIN_MENU.value}:
            reason = self._detect_reason(text)
            if not reason:
                return self._main_reason_retry_message()
            if reason == "turno_presencial":
                await self._conversacion_repo.upsert_state(
                    tenant.id,
                    normalized_phone,
                    ConversationState.ASK_PRESENTIAL_FOR_WHOM.value,
                    {"reason": reason, "patient_id": getattr(paciente, "id", None)},
                )
                return "El turno presencial es para vos o para otra persona?"
            if reason == "turno_virtual":
                await self._conversacion_repo.upsert_state(
                    tenant.id,
                    normalized_phone,
                    ConversationState.ASK_VIRTUAL_FOR_WHOM.value,
                    {"reason": reason, "patient_id": getattr(paciente, "id", None)},
                )
                return "El turno virtual es para vos o para otra persona?"
            if reason == "receta_orden":
                await self._conversacion_repo.upsert_state(
                    tenant.id,
                    normalized_phone,
                    ConversationState.ASK_RECIPE_KIND.value,
                    {"reason": reason, "patient_id": getattr(paciente, "id", None)},
                )
                return "Tu solicitud es para una receta/orden vencida o nueva?"
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_OTHER_QUERY.value,
                {"reason": reason, "patient_id": getattr(paciente, "id", None)},
            )
            return "Escribi en un solo mensaje tu consulta."

        if current_state == ConversationState.ASK_PRESENTIAL_FOR_WHOM.value:
            who = self._detect_for_whom(text)
            if not who:
                return "Responde: para vos o para otra persona."
            context["for_whom"] = who
            if who == "self":
                await self._conversacion_repo.upsert_state(
                    tenant.id,
                    normalized_phone,
                    ConversationState.ASK_PRESENTIAL_FIRST_TIME.value,
                    context,
                )
                return "Es primera vez? (si/no)"
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_PRESENTIAL_OTHER_FIRST_NAME.value,
                context,
            )
            return "Indica nombre de la otra persona."

        if current_state == ConversationState.ASK_PRESENTIAL_OTHER_FIRST_NAME.value:
            if not text:
                return "El nombre no puede estar vacio."
            context["other_first_name"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_PRESENTIAL_OTHER_LAST_NAME.value,
                context,
            )
            return "Ahora el apellido."

        if current_state == ConversationState.ASK_PRESENTIAL_OTHER_LAST_NAME.value:
            if not text:
                return "El apellido no puede estar vacio."
            context["other_last_name"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_PRESENTIAL_OTHER_DNI.value,
                context,
            )
            return "Indica DNI de la otra persona."

        if current_state == ConversationState.ASK_PRESENTIAL_OTHER_DNI.value:
            if not self._is_valid_dni(text):
                return "DNI invalido. Debe contener solo numeros (7 a 9 digitos)."
            context["other_dni"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_PRESENTIAL_OTHER_INSURANCE.value,
                context,
            )
            return "Indica obra social de la otra persona."

        if current_state == ConversationState.ASK_PRESENTIAL_OTHER_INSURANCE.value:
            if not text:
                return "La obra social no puede estar vacia."
            context["other_insurance"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_PRESENTIAL_OTHER_INSURANCE_NUMBER.value,
                context,
            )
            return "Indica numero de afiliado de la otra persona."

        if current_state == ConversationState.ASK_PRESENTIAL_OTHER_INSURANCE_NUMBER.value:
            if not text:
                return "El numero de afiliado no puede estar vacio."
            context["other_insurance_number"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_PRESENTIAL_FIRST_TIME.value,
                context,
            )
            return "Es primera vez? (si/no)"

        if current_state == ConversationState.ASK_PRESENTIAL_FIRST_TIME.value:
            first_time = self._detect_yes_no(text)
            if first_time is None:
                return "Responde si o no."
            context["first_time"] = first_time
            summary = self._build_presential_summary(context)
            await self._set_pending(
                tenant=tenant,
                phone=normalized_phone,
                reason="turno_presencial",
                message=summary,
                title="Solicitud de turno presencial",
            )
            return (
                "Gracias por la informacion. Aguarde que a la brevedad se le respondera "
                "con los turnos presenciales disponibles."
            )

        if current_state == ConversationState.ASK_VIRTUAL_FOR_WHOM.value:
            who = self._detect_for_whom(text)
            if not who:
                return "Responde: para vos o para otra persona."
            context["for_whom"] = who
            if who == "self":
                await self._conversacion_repo.upsert_state(
                    tenant.id,
                    normalized_phone,
                    ConversationState.ASK_VIRTUAL_FIRST_TIME.value,
                    context,
                )
                return "Es primera vez? (si/no)"
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_VIRTUAL_OTHER_FIRST_NAME.value,
                context,
            )
            return "Indica nombre de la otra persona."

        if current_state == ConversationState.ASK_VIRTUAL_OTHER_FIRST_NAME.value:
            if not text:
                return "El nombre no puede estar vacio."
            context["other_first_name"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_VIRTUAL_OTHER_LAST_NAME.value,
                context,
            )
            return "Ahora el apellido."

        if current_state == ConversationState.ASK_VIRTUAL_OTHER_LAST_NAME.value:
            if not text:
                return "El apellido no puede estar vacio."
            context["other_last_name"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_VIRTUAL_OTHER_DNI.value,
                context,
            )
            return "Indica DNI de la otra persona."

        if current_state == ConversationState.ASK_VIRTUAL_OTHER_DNI.value:
            if not self._is_valid_dni(text):
                return "DNI invalido. Debe contener solo numeros (7 a 9 digitos)."
            context["other_dni"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_VIRTUAL_OTHER_INSURANCE.value,
                context,
            )
            return "Indica obra social de la otra persona."

        if current_state == ConversationState.ASK_VIRTUAL_OTHER_INSURANCE.value:
            if not text:
                return "La obra social no puede estar vacia."
            context["other_insurance"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_VIRTUAL_OTHER_INSURANCE_NUMBER.value,
                context,
            )
            return "Indica numero de afiliado de la otra persona."

        if current_state == ConversationState.ASK_VIRTUAL_OTHER_INSURANCE_NUMBER.value:
            if not text:
                return "El numero de afiliado no puede estar vacio."
            context["other_insurance_number"] = text
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_VIRTUAL_FIRST_TIME.value,
                context,
            )
            return "Es primera vez? (si/no)"

        if current_state == ConversationState.ASK_VIRTUAL_FIRST_TIME.value:
            first_time = self._detect_yes_no(text)
            if first_time is None:
                return "Responde si o no."
            context["first_time"] = first_time
            summary = self._build_virtual_summary(context)
            await self._set_pending(
                tenant=tenant,
                phone=normalized_phone,
                reason="turno_virtual",
                message=summary,
                title="Solicitud de turno virtual",
            )
            return "Aguarde que a la brevedad se le estaran informando los turnos virtuales disponibles."

        if current_state == ConversationState.ASK_RECIPE_KIND.value:
            kind = self._detect_recipe_kind(text)
            if not kind:
                return "Responde si la solicitud es vencida o nueva."
            context["recipe_kind"] = kind
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_RECIPE_DETAIL.value,
                context,
            )
            return "Perfecto. Escribi el detalle del medicamento u orden."

        if current_state == ConversationState.ASK_RECIPE_DETAIL.value:
            detail = text or "adjunto_multimedia"
            summary = f"tipo={context.get('recipe_kind')}; detalle={detail}"
            await self._set_pending(
                tenant=tenant,
                phone=normalized_phone,
                reason="receta_orden",
                message=summary,
                title="Solicitud de receta u orden",
            )
            return "Gracias. Aguarde que se le respondera a la brevedad."

        if current_state in {
            ConversationState.ASK_OTHER_QUERY.value,
            ConversationState.OTHER_DETAIL.value,
            ConversationState.HUMAN_REASON.value,
        }:
            if not text:
                return "Necesito que escribas tu consulta en un solo mensaje."
            await self._set_pending(
                tenant=tenant,
                phone=normalized_phone,
                reason="otra_consulta",
                message=text,
                title="Otra consulta pendiente",
            )
            return "Gracias. Aguarde que el medico le respondera a la brevedad."

        await self._conversacion_repo.upsert_state(
            tenant.id,
            normalized_phone,
            ConversationState.MAIN_REASON_MENU.value,
            {"patient_id": getattr(paciente, "id", None)},
        )
        return self._main_reason_retry_message()

    async def _set_pending(
        self,
        tenant: Tenant,
        phone: str,
        reason: str,
        message: str,
        title: str,
    ) -> None:
        await self._conversacion_repo.mark_pending(
            tenant_id=tenant.id,
            telefono=phone,
            reason=reason,
            message=message,
        )
        if self._notification_repo is not None:
            await self._notification_repo.create(
                title=title,
                message=f"Paciente {phone} en {tenant.nombre}",
                notif_type="info",
                tenant_id=tenant.id,
            )

    @staticmethod
    def _normalize_phone(value: str | None) -> str:
        digits = re.sub(r"\D+", "", value or "")
        return digits

    @staticmethod
    def _is_valid_dni(value: str) -> bool:
        return bool(re.fullmatch(r"\d{7,9}", value.strip()))

    @staticmethod
    def _detect_for_whom(value: str) -> str | None:
        normalized = value.strip().lower()
        if normalized in {"1", "yo", "para mi", "para mí", "mi", "mio", "mía", "mio/a", "self"}:
            return "self"
        if normalized in {"2", "otra persona", "otro", "otra", "tercero", "familiar"}:
            return "other"
        return None

    @staticmethod
    def _detect_yes_no(value: str) -> bool | None:
        normalized = value.strip().lower()
        if normalized in {"si", "sí", "s", "1", "yes"}:
            return True
        if normalized in {"no", "n", "2"}:
            return False
        return None

    @staticmethod
    def _detect_recipe_kind(value: str) -> str | None:
        normalized = value.strip().lower()
        if normalized in {"vencida", "vencido", "1"}:
            return "vencida"
        if normalized in {"nueva", "nuevo", "2"}:
            return "nueva"
        return None

    @staticmethod
    def _is_valid_email(value: str) -> bool:
        candidate = value.strip()
        return bool(candidate) and ("@" in candidate) and ("." in candidate.split("@")[-1])

    @staticmethod
    def _detect_reason(value: str) -> str | None:
        normalized = value.strip().lower()
        mapping = {
            "1": "turno_presencial",
            "a": "turno_presencial",
            "turno presencial": "turno_presencial",
            "presencial": "turno_presencial",
            "turno": "turno_presencial",
            "2": "turno_virtual",
            "b": "turno_virtual",
            "turno virtual": "turno_virtual",
            "virtual": "turno_virtual",
            "3": "receta_orden",
            "c": "receta_orden",
            "receta": "receta_orden",
            "orden": "receta_orden",
            "receta u orden": "receta_orden",
            "4": "otra_consulta",
            "d": "otra_consulta",
            "otra": "otra_consulta",
            "otra consulta": "otra_consulta",
            "consulta": "otra_consulta",
        }
        return mapping.get(normalized)

    def _known_patient_greeting(self, tenant: Tenant, patient_name: str | None) -> str:
        doctor_name = (tenant.fantasy_name or tenant.nombre or "profesional").strip()
        patient_label = (patient_name or "").strip() or ""
        if patient_label:
            return (
                f"Hola {patient_label}, te contactaste con el consultorio del Dr. {doctor_name}. "
                "Cual es el motivo de tu consulta?\n"
                f"{self._main_reason_menu_message()}"
            )
        return (
            f"Hola, te contactaste con el consultorio del Dr. {doctor_name}. "
            "Cual es el motivo de tu consulta?\n"
            f"{self._main_reason_menu_message()}"
        )

    @staticmethod
    def _main_reason_menu_message() -> str:
        return (
            "1) Turno presencial\n"
            "2) Turno virtual\n"
            "3) Solicitar receta u orden medica\n"
            "4) Otra consulta"
        )

    def _main_reason_retry_message(self) -> str:
        return (
            "Para ayudarte, elegi una opcion:\n"
            "1?? Turno presencial\n"
            "2?? Turno virtual\n"
            "3?? Receta u orden medica\n"
            "4?? Otra consulta"
        )

    @staticmethod
    def _build_presential_summary(context: dict) -> str:
        who = context.get("for_whom")
        if who == "other":
            return (
                "tipo=turno_presencial; "
                f"paciente={context.get('other_first_name', '')} {context.get('other_last_name', '')}; "
                f"dni={context.get('other_dni')}; "
                f"obra_social={context.get('other_insurance')}; "
                f"afiliado={context.get('other_insurance_number')}; "
                f"primera_vez={'si' if context.get('first_time') else 'no'}"
            )
        return f"tipo=turno_presencial; para=self; primera_vez={'si' if context.get('first_time') else 'no'}"

    @staticmethod
    def _build_virtual_summary(context: dict) -> str:
        who = context.get("for_whom")
        if who == "other":
            return (
                "tipo=turno_virtual; "
                f"paciente={context.get('other_first_name', '')} {context.get('other_last_name', '')}; "
                f"dni={context.get('other_dni')}; "
                f"obra_social={context.get('other_insurance')}; "
                f"afiliado={context.get('other_insurance_number')}; "
                f"primera_vez={'si' if context.get('first_time') else 'no'}"
            )
        return f"tipo=turno_virtual; para=self; primera_vez={'si' if context.get('first_time') else 'no'}"

    @staticmethod
    def _state_expired(updated_at: datetime | None) -> bool:
        if not updated_at:
            return False
        now = datetime.now(timezone.utc)
        value = updated_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return now - value > timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)
