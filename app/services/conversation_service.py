from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import get_ba_tz, now_ba
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.repositories.conversacion_repository import ConversacionRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.paciente_repository import PacienteRepository
from app.services.conversation_intents import (
    ConversationCategory,
    classify_main_intent,
    detect_for_whom,
    detect_prescription_subtype,
    detect_yes_no,
)
from app.services.ai_intent_classifier import (
    AIIntent,
    AIIntentClassifier,
    AIIntentResult,
    normalize_message,
)
from app.services.tenant_ai_settings_service import get_effective_ai_settings

logger = logging.getLogger(__name__)


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
        self._ai_intent_classifier = AIIntentClassifier()

    async def process_message(
        self,
        tenant: Tenant,
        from_phone: str,
        body: str,
        media_items: list[dict] | None = None,
    ) -> str:
        normalized_phone = self._normalize_phone(from_phone)
        text = (body or "").strip()
        lowered = text.lower()
        media_items = list(media_items or [])
        has_media = bool(media_items)

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
        if state is not None and (state.status or "").lower() == "finished":
            # Finished conversations must remain persisted for history.
            # Treat them as no active session so a new flow can start.
            state = None

        paciente = await self._paciente_repo.get_by_phone(tenant.id, normalized_phone)

        if state is not None and (state.status or "").lower() == "pending" and lowered not in EXIT_COMMANDS:
            return (
                "Tu consulta ya fue derivada y esta pendiente de respuesta humana. "
                "Si queres reiniciar, escribi SALIR."
            )

        if lowered in EXIT_COMMANDS:
            if state is not None and (state.status or "").lower() != "finished":
                await self._conversacion_repo.mark_resolved(
                    tenant.id, normalized_phone, close_reason="exit_command"
                )
            return "Conversacion finalizada. Escribi cualquier mensaje para comenzar una conversacion nueva."

        if state is None:
            if paciente is None:
                await self._conversacion_repo.upsert_state(
                    tenant.id, normalized_phone, ConversationState.ASK_FIRST_NAME.value, {}
                )
                return self._assistant_registration_greeting(tenant)
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.MAIN_REASON_MENU.value,
                {"patient_id": paciente.id},
            )
            return self._known_patient_greeting(tenant, paciente.nombre, paciente.apellido)

        context = state.contexto_json or {}
        current_state = state.estado_actual
        ai_settings = get_effective_ai_settings(tenant)

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
            return self._known_patient_greeting(tenant, paciente.nombre, paciente.apellido)

        if current_state in {ConversationState.MAIN_REASON_MENU.value, ConversationState.MAIN_MENU.value}:
            if self._is_direct_main_menu_option(text):
                intent = classify_main_intent(text)
            else:
                ai_result = await self._ai_intent_classifier.classify(
                    text,
                    tenant=tenant,
                    ai_settings=ai_settings,
                    conversation_state=current_state,
                    context=context,
                )
                self._log_ai_intent(
                    tenant_id=tenant.id,
                    phone=normalized_phone,
                    current_state=current_state,
                    message=text,
                    result=ai_result,
                )
                if (
                    ai_result.intent == AIIntent.EXIT
                    and ai_result.confidence >= ai_settings["min_confidence"]
                ):
                    await self._conversacion_repo.mark_resolved(
                        tenant.id, normalized_phone, close_reason="exit_command"
                    )
                    return "Conversacion finalizada. Escribi cualquier mensaje para comenzar una conversacion nueva."
                intent = self._intent_from_ai_result(ai_result, min_confidence=ai_settings["min_confidence"])
            reason = intent.pending_reason
            category = intent.category
            if intent.requires_clarification:
                return (
                    "Perfecto, te ayudo con tu turno. Indica por favor:\n"
                    "1) Turno presencial\n"
                    "2) Turno virtual"
                )
            if not reason or not category:
                return self._invalid_option_message(self._main_reason_menu_message())
            if reason == "turno_presencial":
                await self._conversacion_repo.upsert_state(
                    tenant.id,
                    normalized_phone,
                    ConversationState.ASK_PRESENTIAL_FOR_WHOM.value,
                    {"reason": reason, "patient_id": getattr(paciente, "id", None)},
                    conversation_category=category,
                )
                return (
                    "El turno presencial es:\n"
                    f"{self._for_whom_options()}"
                )
            if reason == "turno_virtual":
                await self._conversacion_repo.upsert_state(
                    tenant.id,
                    normalized_phone,
                    ConversationState.ASK_VIRTUAL_FOR_WHOM.value,
                    {"reason": reason, "patient_id": getattr(paciente, "id", None)},
                    conversation_category=category,
                )
                return (
                    "El turno virtual es:\n"
                    f"{self._for_whom_options()}"
                )
            if reason == "receta_orden":
                await self._conversacion_repo.upsert_state(
                    tenant.id,
                    normalized_phone,
                    ConversationState.ASK_RECIPE_KIND.value,
                    {"reason": reason, "patient_id": getattr(paciente, "id", None)},
                    conversation_category=category,
                )
                return (
                    "Tu solicitud de receta/orden es:\n"
                    f"{self._recipe_kind_options()}"
                )
            if reason == "humano":
                await self._set_pending(
                    tenant=tenant,
                    phone=normalized_phone,
                    reason=reason,
                    message=text or "Paciente solicita atencion humana.",
                    title="Derivacion a humano",
                    category=ConversationCategory.HUMAN_HANDOFF,
                    subtype=None,
                    requires_human_review=True,
                    has_media=has_media,
                    last_patient_message=text,
                    media_items=media_items,
                )
                return "Perfecto. Derivo tu consulta para atencion humana y te responderan a la brevedad."
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_OTHER_QUERY.value,
                {"reason": reason, "patient_id": getattr(paciente, "id", None)},
                conversation_category=category,
            )
            return "Escriba la consulta en un solo mensaje que sera respondida a la brevedad."

        if current_state == ConversationState.ASK_PRESENTIAL_FOR_WHOM.value:
            who = detect_for_whom(text)
            if not who:
                return self._invalid_option_message(self._for_whom_options())
            context["for_whom"] = who
            if who == "self":
                await self._conversacion_repo.upsert_state(
                    tenant.id,
                    normalized_phone,
                    ConversationState.ASK_PRESENTIAL_FIRST_TIME.value,
                    context,
                )
                return self._first_time_options()
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
            return self._first_time_options()

        if current_state == ConversationState.ASK_PRESENTIAL_FIRST_TIME.value:
            first_time = detect_yes_no(text)
            if first_time is None:
                return self._invalid_option_message(self._first_time_options())
            context["first_time"] = first_time
            summary = self._build_presential_summary(context)
            await self._set_pending(
                tenant=tenant,
                phone=normalized_phone,
                reason="turno_presencial",
                message=summary,
                title="Solicitud de turno presencial",
                category=ConversationCategory.PRESENTIAL_APPOINTMENT,
                subtype=None,
                requires_human_review=False,
                has_media=has_media,
                last_patient_message=text,
                media_items=media_items,
            )
            return (
                "Gracias por la informacion. Aguarde que a la brevedad se le respondera "
                "con los turnos presenciales disponibles."
            )

        if current_state == ConversationState.ASK_VIRTUAL_FOR_WHOM.value:
            who = detect_for_whom(text)
            if not who:
                return self._invalid_option_message(self._for_whom_options())
            context["for_whom"] = who
            if who == "self":
                await self._conversacion_repo.upsert_state(
                    tenant.id,
                    normalized_phone,
                    ConversationState.ASK_VIRTUAL_FIRST_TIME.value,
                    context,
                )
                return self._first_time_options()
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
            return self._first_time_options()

        if current_state == ConversationState.ASK_VIRTUAL_FIRST_TIME.value:
            first_time = detect_yes_no(text)
            if first_time is None:
                return self._invalid_option_message(self._first_time_options())
            context["first_time"] = first_time
            summary = self._build_virtual_summary(context)
            await self._set_pending(
                tenant=tenant,
                phone=normalized_phone,
                reason="turno_virtual",
                message=summary,
                title="Solicitud de turno virtual",
                category=ConversationCategory.VIRTUAL_APPOINTMENT,
                subtype=None,
                requires_human_review=False,
                has_media=has_media,
                last_patient_message=text,
                media_items=media_items,
            )
            return "Aguarde que a la brevedad se le estaran informando los turnos virtuales disponibles."

        if current_state == ConversationState.ASK_RECIPE_KIND.value:
            kind = detect_prescription_subtype(text)
            if not kind:
                return self._invalid_option_message(self._recipe_kind_options())
            context["recipe_kind"] = kind
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_RECIPE_DETAIL.value,
                context,
                conversation_category=ConversationCategory.PRESCRIPTION_OR_ORDER,
                conversation_subtype=kind,
            )
            return "Perfecto. Escribi el detalle del medicamento u orden, o envia foto/documento."

        if current_state == ConversationState.ASK_RECIPE_DETAIL.value:
            if not text and not has_media:
                return "Necesito un detalle escrito o un adjunto para continuar con receta/orden."
            detail = text or "Adjunto recibido"
            summary = f"subtipo={context.get('recipe_kind')}; detalle={detail}; adjuntos={len(media_items)}"
            await self._set_pending(
                tenant=tenant,
                phone=normalized_phone,
                reason="receta_orden",
                message=summary,
                title="Solicitud de receta u orden",
                category=ConversationCategory.PRESCRIPTION_OR_ORDER,
                subtype=context.get("recipe_kind"),
                requires_human_review=has_media,
                has_media=has_media,
                last_patient_message=text,
                media_items=media_items,
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
                category=ConversationCategory.OTHER_QUERY,
                subtype=None,
                requires_human_review=False,
                has_media=has_media,
                last_patient_message=text,
                media_items=media_items,
            )
            return "Consulta recibida, aguarde que nos pondremos en contacto a la brevedad."

        await self._conversacion_repo.upsert_state(
            tenant.id,
            normalized_phone,
            ConversationState.MAIN_REASON_MENU.value,
            {"patient_id": getattr(paciente, "id", None)},
            last_patient_message=text,
            has_media=has_media,
        )
        return self._main_reason_retry_message()

    async def _set_pending(
        self,
        tenant: Tenant,
        phone: str,
        reason: str,
        message: str,
        title: str,
        category: str | None,
        subtype: str | None,
        requires_human_review: bool,
        has_media: bool,
        last_patient_message: str | None,
        media_items: list[dict] | None,
    ) -> None:
        await self._conversacion_repo.mark_pending(
            tenant_id=tenant.id,
            telefono=phone,
            reason=reason,
            message=message,
            category=category,
            subtype=subtype,
            requires_human_review=requires_human_review,
            has_media=has_media,
            last_patient_message=last_patient_message,
            media_metadata=media_items or [],
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
    def _is_valid_email(value: str) -> bool:
        candidate = value.strip()
        return bool(candidate) and ("@" in candidate) and ("." in candidate.split("@")[-1])

    def _known_patient_greeting(
        self,
        tenant: Tenant,
        patient_name: str | None,
        patient_last_name: str | None = None,
    ) -> str:
        doctor_name = (tenant.fantasy_name or tenant.nombre or "profesional").strip()
        patient_label = " ".join(
            part.strip() for part in (patient_name or "", patient_last_name or "") if part and part.strip()
        ).strip()
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

    def _assistant_registration_greeting(self, tenant: Tenant) -> str:
        tenant_name = self._tenant_display_name(tenant)
        return (
            f"Hola, te contactaste con la asistente de {tenant_name}. "
            "Para continuar, primero necesito registrarte como paciente. "
            "Decime tu nombre."
        )

    @staticmethod
    def _tenant_display_name(tenant: Tenant) -> str:
        fantasy = (tenant.fantasy_name or "").strip()
        if fantasy:
            return fantasy
        first = (tenant.first_name or "").strip()
        last = (tenant.last_name or "").strip()
        full = " ".join(part for part in (first, last) if part)
        if full:
            return full
        return (tenant.nombre or "el consultorio").strip() or "el consultorio"

    @staticmethod
    def _main_reason_menu_message() -> str:
        return (
            "1) Turno presencial\n"
            "2) Turno virtual\n"
            "3) Solicitar receta u orden medica\n"
            "4) Otra consulta\n"
            "5) Hablar con una persona"
        )

    def _main_reason_retry_message(self) -> str:
        return self._invalid_option_message(self._main_reason_menu_message())

    @staticmethod
    def _invalid_option_message(options_text: str) -> str:
        return (
            "Debe seleccionar una opción válida.\n"
            f"{options_text}"
        )

    @staticmethod
    def _for_whom_options() -> str:
        return "1) Para mi\n2) Para otra persona"

    @staticmethod
    def _first_time_options() -> str:
        return "Es primera vez?\n1) Si\n2) No"

    @staticmethod
    def _recipe_kind_options() -> str:
        return (
            "1) Receta nueva\n"
            "2) Renovar receta\n"
            "3) Receta vencida\n"
            "4) Orden/pedido medico\n"
            "5) Otra solicitud de receta u orden"
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
        now = now_ba()
        value = updated_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=get_ba_tz())
        return now - value > timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)

    @staticmethod
    def _is_direct_main_menu_option(value: str | None) -> bool:
        return normalize_message(value) in {"1", "2", "3", "4", "5", "a", "b", "c", "d", "e"}

    def _intent_from_ai_result(self, result: AIIntentResult, *, min_confidence: float):
        from app.services.conversation_intents import IntentResult

        if result.confidence < min_confidence:
            return IntentResult(None, None)
        if result.intent == AIIntent.BOOK_PRESENTIAL_APPOINTMENT:
            return IntentResult("turno_presencial", ConversationCategory.PRESENTIAL_APPOINTMENT)
        if result.intent == AIIntent.BOOK_VIRTUAL_APPOINTMENT:
            return IntentResult("turno_virtual", ConversationCategory.VIRTUAL_APPOINTMENT)
        if result.intent == AIIntent.RECIPE_OR_ORDER:
            return IntentResult("receta_orden", ConversationCategory.PRESCRIPTION_OR_ORDER)
        if result.intent == AIIntent.OTHER_MEDICAL_QUERY:
            return IntentResult("otra_consulta", ConversationCategory.OTHER_QUERY)
        if result.intent == AIIntent.HUMAN_HANDOFF:
            return IntentResult("humano", ConversationCategory.HUMAN_HANDOFF)
        return IntentResult(None, None)

    @staticmethod
    def _log_ai_intent(
        *,
        tenant_id: int,
        phone: str,
        current_state: str,
        message: str,
        result: AIIntentResult,
    ) -> None:
        logger.info(
            "ai_intent_classified tenant_id=%s telefono=%s estado_actual=%s mensaje_normalizado=%s intent=%s confidence=%.3f source=%s error=%s",
            tenant_id,
            phone,
            current_state,
            normalize_message(message),
            result.intent,
            result.confidence,
            result.source,
            result.error or "",
        )

