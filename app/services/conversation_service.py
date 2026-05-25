from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import get_ba_tz, now_ba
from app.models.paciente import Paciente
from app.models.tenant import Tenant
from app.models.consultorio import Consultorio
from app.models.turno import AppointmentStatus, Turno
from app.repositories.conversacion_repository import ConversacionRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.paciente_repository import PacienteRepository
from app.services.conversation_intents import (
    ConversationCategory,
    PrescriptionSubtype,
    classify_main_intent,
    detect_for_whom,
    detect_prescription_subtype,
    detect_yes_no,
)
from app.services.ai_extraction_service import (
    get_missing_fields_for_intent,
    merge_extracted_into_context,
    should_handoff_by_extraction,
)
from app.services.ai_intent_classifier import (
    AIIntent,
    AIIntentClassifier,
    AIIntentResult,
    normalize_message,
)
from app.services.ai_tools import get_available_appointment_slots
from app.services.appointment_service import AppointmentService
from app.services.tenant_ai_settings_service import get_effective_ai_settings
from sqlalchemy import select

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
    ASK_AI_SLOT_SELECTION = "ask_ai_slot_selection"
    ASK_AI_BOOKING_CONFIRMATION = "ask_ai_booking_confirmation"
    ASK_CANCEL_APPOINTMENT_SELECTION = "ask_cancel_appointment_selection"
    ASK_CANCEL_APPOINTMENT_CONFIRMATION = "ask_cancel_appointment_confirmation"

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
                context = self._context_with_ai_result(context, ai_result)
                if should_handoff_by_extraction(ai_result.extracted):
                    await self._conversacion_repo.upsert_state(
                        tenant.id,
                        normalized_phone,
                        current_state,
                        context,
                        conversation_category=ConversationCategory.HUMAN_HANDOFF,
                    )
                    await self._set_pending(
                        tenant=tenant,
                        phone=normalized_phone,
                        reason="humano",
                        message=text or "Posible urgencia detectada por agente IA.",
                        title="Derivacion prioritaria a humano",
                        category=ConversationCategory.HUMAN_HANDOFF,
                        subtype=None,
                        requires_human_review=True,
                        has_media=has_media,
                        last_patient_message=text,
                        media_items=media_items,
                    )
                    return (
                        "Por lo que me comentas, es mejor que lo revise una persona del consultorio. "
                        "Ya dejo tu mensaje como prioridad para que te contacten. "
                        "Si es una urgencia medica, acudi a una guardia o llama a emergencias."
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
                return await self._start_appointment_flow_from_context(
                    tenant=tenant,
                    phone=normalized_phone,
                    context=context,
                    patient_id=getattr(paciente, "id", None),
                    reason=reason,
                    category=category,
                    appointment_type="presential",
                    original_text=text,
                    has_media=has_media,
                    media_items=media_items,
                )
            if reason == "turno_virtual":
                return await self._start_appointment_flow_from_context(
                    tenant=tenant,
                    phone=normalized_phone,
                    context=context,
                    patient_id=getattr(paciente, "id", None),
                    reason=reason,
                    category=category,
                    appointment_type="virtual",
                    original_text=text,
                    has_media=has_media,
                    media_items=media_items,
                )
            if reason == "receta_orden":
                return await self._start_recipe_flow_from_context(
                    tenant=tenant,
                    phone=normalized_phone,
                    context=context,
                    patient_id=getattr(paciente, "id", None),
                    original_text=text,
                    has_media=has_media,
                    media_items=media_items,
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
            if reason == "cancelar_turno":
                return await self._start_cancel_appointment_flow(
                    tenant=tenant,
                    phone=normalized_phone,
                    paciente=paciente,
                    context=context,
                )
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_OTHER_QUERY.value,
                {"reason": reason, "patient_id": getattr(paciente, "id", None)},
                conversation_category=category,
            )
            return "Escriba la consulta en un solo mensaje que sera respondida a la brevedad."

        if current_state == ConversationState.ASK_AI_SLOT_SELECTION.value:
            selected = self._select_offered_slot(context, text)
            if selected is None:
                return self._invalid_slot_selection_message(context)
            appointment = context.setdefault("appointment", {})
            appointment["selected_slot"] = selected
            appointment["awaiting_slot_selection"] = False
            appointment["awaiting_booking_confirmation"] = True
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_AI_BOOKING_CONFIRMATION.value,
                context,
                conversation_category=self._appointment_category_from_context(context),
            )
            return (
                f"Perfecto. Seleccionaste el turno {self._appointment_label_from_context(context)} "
                f"del {selected.get('label')}.\n"
                "Confirmas que queres reservarlo?\n"
                "1) Si, confirmar\n"
                "2) No, ver otras opciones"
            )

        if current_state == ConversationState.ASK_AI_BOOKING_CONFIRMATION.value:
            confirmation = detect_yes_no(text)
            if confirmation is None:
                return "Debe seleccionar una opcion valida.\n1) Si, confirmar\n2) No, ver otras opciones"
            if confirmation is False:
                context.setdefault("appointment", {})["selected_slot"] = None
                return await self._offer_appointment_slots_if_available(
                    tenant=tenant,
                    phone=normalized_phone,
                    context=context,
                    original_text=text,
                    has_media=has_media,
                    media_items=media_items,
                    force_manual_on_unavailable=False,
                )
            appointment = context.setdefault("appointment", {})
            appointment["awaiting_booking_confirmation"] = False
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                current_state,
                context,
                conversation_category=self._appointment_category_from_context(context),
            )
            await self._set_pending(
                tenant=tenant,
                phone=normalized_phone,
                reason=context.get("reason") or "turno_virtual",
                message=self._build_ai_selected_slot_summary(context),
                title="Seleccion de turno pendiente de confirmacion manual",
                category=self._appointment_category_from_context(context),
                subtype=None,
                requires_human_review=False,
                has_media=has_media,
                last_patient_message=text,
                media_items=media_items,
            )
            return "Perfecto, dejo registrada tu seleccion para que el consultorio la confirme."

        if current_state == ConversationState.ASK_CANCEL_APPOINTMENT_SELECTION.value:
            selected = self._select_cancellable_turno(context, text)
            if selected is None:
                return self._invalid_cancel_selection_message(context)
            context["cancel_appointment"]["selected"] = selected
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.ASK_CANCEL_APPOINTMENT_CONFIRMATION.value,
                context,
            )
            return (
                f"Seleccionaste cancelar el turno {selected.get('label')}.\n"
                "Confirmas la cancelacion?\n"
                "1) Si, cancelar\n"
                "2) No"
            )

        if current_state == ConversationState.ASK_CANCEL_APPOINTMENT_CONFIRMATION.value:
            confirmation = detect_yes_no(text)
            if confirmation is None:
                return "Debe seleccionar una opcion valida.\n1) Si, cancelar\n2) No"
            if confirmation is False:
                await self._conversacion_repo.upsert_state(
                    tenant.id,
                    normalized_phone,
                    ConversationState.MAIN_REASON_MENU.value,
                    {"patient_id": getattr(paciente, "id", None)},
                )
                return "Cancelacion descartada. Volves al menu principal.\n" + self._main_reason_menu_message()
            selected = (context.get("cancel_appointment") or {}).get("selected") or {}
            turno_id = selected.get("turno_id")
            cancelled = await self._cancel_turno_from_chat(
                tenant=tenant,
                phone=normalized_phone,
                turno_id=turno_id,
            )
            if not cancelled:
                return "No pude cancelar ese turno. Lo derivo para que el consultorio lo revise."
            await self._conversacion_repo.upsert_state(
                tenant.id,
                normalized_phone,
                ConversationState.MAIN_REASON_MENU.value,
                {"patient_id": getattr(paciente, "id", None)},
            )
            return "Listo, el turno fue cancelado y liberado."

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
            context.setdefault("appointment", {})["is_first_time"] = first_time
            if self._ai_tools_enabled(ai_settings):
                return await self._offer_appointment_slots_if_available(
                    tenant=tenant,
                    phone=normalized_phone,
                    context=context,
                    original_text=text,
                    has_media=has_media,
                    media_items=media_items,
                )
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
            context.setdefault("appointment", {})["is_first_time"] = first_time
            if self._ai_tools_enabled(ai_settings):
                return await self._offer_appointment_slots_if_available(
                    tenant=tenant,
                    phone=normalized_phone,
                    context=context,
                    original_text=text,
                    has_media=has_media,
                    media_items=media_items,
                )
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

    async def _start_appointment_flow_from_context(
        self,
        *,
        tenant: Tenant,
        phone: str,
        context: dict,
        patient_id: int | None,
        reason: str,
        category: str,
        appointment_type: str,
        original_text: str,
        has_media: bool,
        media_items: list[dict],
    ) -> str:
        context = dict(context or {})
        context["reason"] = reason
        context["patient_id"] = patient_id
        appointment = context.get("appointment") if isinstance(context.get("appointment"), dict) else {}
        appointment["type"] = appointment_type
        context["appointment"] = appointment

        prefix = f"Perfecto, entiendo que queres un turno {'virtual' if appointment_type == 'virtual' else 'presencial'}."
        state_prefix = "VIRTUAL" if appointment_type == "virtual" else "PRESENTIAL"
        for_whom_state = getattr(ConversationState, f"ASK_{state_prefix}_FOR_WHOM").value
        other_first_name_state = getattr(ConversationState, f"ASK_{state_prefix}_OTHER_FIRST_NAME").value
        other_last_name_state = getattr(ConversationState, f"ASK_{state_prefix}_OTHER_LAST_NAME").value
        other_dni_state = getattr(ConversationState, f"ASK_{state_prefix}_OTHER_DNI").value
        first_time_state = getattr(ConversationState, f"ASK_{state_prefix}_FIRST_TIME").value

        who = context.get("for_whom") or appointment.get("for")
        if not who:
            await self._conversacion_repo.upsert_state(
                tenant.id,
                phone,
                for_whom_state,
                context,
                conversation_category=category,
            )
            label = "virtual" if appointment_type == "virtual" else "presencial"
            return f"{prefix}\nEl turno {label} es:\n{self._for_whom_options()}"

        context["for_whom"] = who
        if who == "other":
            if not context.get("other_first_name"):
                await self._conversacion_repo.upsert_state(
                    tenant.id, phone, other_first_name_state, context, conversation_category=category
                )
                return f"{prefix} Indica nombre de la otra persona."
            if not context.get("other_last_name"):
                await self._conversacion_repo.upsert_state(
                    tenant.id, phone, other_last_name_state, context, conversation_category=category
                )
                return f"{prefix} Ya tengo el nombre. Ahora el apellido."
            if not self._is_valid_dni(str(context.get("other_dni") or "")):
                await self._conversacion_repo.upsert_state(
                    tenant.id, phone, other_dni_state, context, conversation_category=category
                )
                return "Ya tengo para quien es el turno. Indica DNI de la otra persona."

        if context.get("first_time") is None:
            await self._conversacion_repo.upsert_state(
                tenant.id,
                phone,
                first_time_state,
                context,
                conversation_category=category,
            )
            if who == "other":
                name = " ".join(
                    part for part in (context.get("other_first_name"), context.get("other_last_name")) if part
                )
                if name:
                    return f"Perfecto, entiendo que el turno es para {name}. {self._first_time_options()}"
            return f"{prefix} {self._first_time_options()}"

        if self._ai_tools_enabled(get_effective_ai_settings(tenant)):
            return await self._offer_appointment_slots_if_available(
                tenant=tenant,
                phone=phone,
                context=context,
                original_text=original_text,
                has_media=has_media,
                media_items=media_items,
            )

        summary = self._build_virtual_summary(context) if appointment_type == "virtual" else self._build_presential_summary(context)
        await self._conversacion_repo.upsert_state(
            tenant.id,
            phone,
            first_time_state,
            context,
            conversation_category=category,
        )
        await self._set_pending(
            tenant=tenant,
            phone=phone,
            reason=reason,
            message=summary,
            title="Solicitud de turno virtual" if appointment_type == "virtual" else "Solicitud de turno presencial",
            category=category,
            subtype=None,
            requires_human_review=False,
            has_media=has_media,
            last_patient_message=original_text,
            media_items=media_items,
        )
        if appointment_type == "virtual":
            return "Gracias. Aguarde que a la brevedad se le estaran informando los turnos virtuales disponibles."
        return (
            "Gracias por la informacion. Aguarde que a la brevedad se le respondera "
            "con los turnos presenciales disponibles."
        )

    async def _start_recipe_flow_from_context(
        self,
        *,
        tenant: Tenant,
        phone: str,
        context: dict,
        patient_id: int | None,
        original_text: str,
        has_media: bool,
        media_items: list[dict],
    ) -> str:
        context = dict(context or {})
        context["reason"] = "receta_orden"
        context["patient_id"] = patient_id
        recipe_order = context.get("recipe_order") if isinstance(context.get("recipe_order"), dict) else {}
        recipe_type = recipe_order.get("type")
        detail = recipe_order.get("detail")
        subtype = self._recipe_type_to_subtype(recipe_type)
        if subtype:
            context["recipe_kind"] = subtype

        if not subtype:
            await self._conversacion_repo.upsert_state(
                tenant.id,
                phone,
                ConversationState.ASK_RECIPE_KIND.value,
                context,
                conversation_category=ConversationCategory.PRESCRIPTION_OR_ORDER,
            )
            return f"Perfecto, entiendo que necesitas una receta u orden.\n{self._recipe_kind_options()}"

        if not detail:
            await self._conversacion_repo.upsert_state(
                tenant.id,
                phone,
                ConversationState.ASK_RECIPE_DETAIL.value,
                context,
                conversation_category=ConversationCategory.PRESCRIPTION_OR_ORDER,
                conversation_subtype=subtype,
            )
            return "Perfecto. Escribi el detalle del medicamento u orden, o envia foto/documento."

        summary = f"subtipo={subtype}; detalle={detail}; adjuntos={len(media_items)}"
        await self._conversacion_repo.upsert_state(
            tenant.id,
            phone,
            ConversationState.ASK_RECIPE_DETAIL.value,
            context,
            conversation_category=ConversationCategory.PRESCRIPTION_OR_ORDER,
            conversation_subtype=subtype,
        )
        await self._set_pending(
            tenant=tenant,
            phone=phone,
            reason="receta_orden",
            message=summary,
            title="Solicitud de receta u orden",
            category=ConversationCategory.PRESCRIPTION_OR_ORDER,
            subtype=subtype,
            requires_human_review=has_media,
            has_media=has_media,
            last_patient_message=original_text,
            media_items=media_items,
        )
        return "Gracias. Ya tengo el detalle de la solicitud y se le respondera a la brevedad."

    async def _offer_appointment_slots_if_available(
        self,
        *,
        tenant: Tenant,
        phone: str,
        context: dict,
        original_text: str,
        has_media: bool,
        media_items: list[dict],
        force_manual_on_unavailable: bool = True,
    ) -> str:
        del media_items
        appointment = context.setdefault("appointment", {})
        appointment_type = appointment.get("type") or (
            "virtual" if context.get("reason") == "turno_virtual" else "presential"
        )
        appointment["type"] = appointment_type
        reason = "turno_virtual" if appointment_type == "virtual" else "turno_presencial"
        context["reason"] = context.get("reason") or reason
        if not context.get("for_whom"):
            context["for_whom"] = appointment.get("for")
        if context.get("first_time") is None and appointment.get("is_first_time") is not None:
            context["first_time"] = appointment.get("is_first_time")

        missing = self._missing_before_availability(context)
        if missing:
            return await self._ask_missing_before_availability(
                tenant=tenant,
                phone=phone,
                context=context,
                missing=missing[0],
            )

        settings = get_effective_ai_settings(tenant)
        limit = int(settings.get("max_offered_slots") or 5)
        preferences = {
            "preferred_day": appointment.get("preferred_day"),
            "preferred_date": appointment.get("preferred_date"),
            "preferred_time": appointment.get("preferred_time"),
            "preferred_time_range": appointment.get("preferred_time_range"),
        }
        result = await get_available_appointment_slots(
            self._session,
            tenant_id=tenant.id,
            consultorio_type=appointment_type,
            patient_context=context,
            preferences=preferences,
            limit=limit,
        )
        slots = list((result.get("slots") or [])[:limit])
        self._log_ai_tool(
            tenant_id=tenant.id,
            phone=phone,
            intent="book_virtual_appointment" if appointment_type == "virtual" else "book_presential_appointment",
            tool_name="get_available_appointment_slots",
            slots_count=len(slots),
            provider=result.get("source"),
            error=result.get("error"),
        )
        if not result.get("ok") or not slots:
            appointment["offered_slots"] = []
            appointment["awaiting_slot_selection"] = False
            await self._conversacion_repo.upsert_state(
                tenant.id,
                phone,
                ConversationState.MAIN_REASON_MENU.value,
                context,
                conversation_category=self._appointment_category_from_context(context),
                has_media=has_media,
                last_patient_message=original_text,
            )
            if force_manual_on_unavailable:
                await self._set_pending(
                    tenant=tenant,
                    phone=phone,
                    reason=context["reason"],
                    message=result.get("message") or "No se pudo consultar disponibilidad automatica.",
                    title="Solicitud de turno para revision manual",
                    category=self._appointment_category_from_context(context),
                    subtype=None,
                    requires_human_review=True,
                    has_media=has_media,
                    last_patient_message=original_text,
                    media_items=[],
                )
                return "No pude consultar la agenda en este momento. Dejo tu solicitud para que el consultorio te contacte."
            return "No encontre turnos disponibles con esa preferencia. Queres que te muestre otros horarios?"

        offered = []
        for index, slot in enumerate(slots, start=1):
            offered.append(
                {
                    "option": index,
                    "slot_id": slot.get("slot_id"),
                    "label": slot.get("label"),
                    "start_at": slot.get("start_at"),
                    "end_at": slot.get("end_at"),
                    "provider": slot.get("provider"),
                    "metadata": slot.get("metadata") or {},
                }
            )
        appointment["preferences"] = {key: value for key, value in preferences.items() if value}
        appointment["offered_slots"] = offered
        appointment["selected_slot"] = None
        appointment["awaiting_slot_selection"] = True
        appointment["awaiting_booking_confirmation"] = False
        await self._conversacion_repo.upsert_state(
            tenant.id,
            phone,
            ConversationState.ASK_AI_SLOT_SELECTION.value,
            context,
            conversation_category=self._appointment_category_from_context(context),
        )
        label = "presenciales" if appointment_type == "presential" else "virtuales"
        lines = [f"Encontre estos turnos {label} disponibles:"]
        lines.extend(f"{slot['option']}) {slot['label']}" for slot in offered)
        lines.append("Responde con el numero de opcion que preferis.")
        return "\n".join(lines)

    async def _ask_missing_before_availability(
        self,
        *,
        tenant: Tenant,
        phone: str,
        context: dict,
        missing: str,
    ) -> str:
        appointment_type = context.get("appointment", {}).get("type") or "virtual"
        state_prefix = "VIRTUAL" if appointment_type == "virtual" else "PRESENTIAL"
        if missing == "appointment_for":
            state_value = getattr(ConversationState, f"ASK_{state_prefix}_FOR_WHOM").value
            message = f"El turno {self._appointment_label_from_context(context)} es:\n{self._for_whom_options()}"
        elif missing == "other_patient_dni":
            state_value = getattr(ConversationState, f"ASK_{state_prefix}_OTHER_DNI").value
            message = "Para consultar disponibilidad, indicame el DNI de la otra persona."
        else:
            state_value = getattr(ConversationState, f"ASK_{state_prefix}_FIRST_TIME").value
            message = self._first_time_options()
        await self._conversacion_repo.upsert_state(
            tenant.id,
            phone,
            state_value,
            context,
            conversation_category=self._appointment_category_from_context(context),
        )
        return message

    async def _start_cancel_appointment_flow(
        self,
        *,
        tenant: Tenant,
        phone: str,
        paciente: Paciente | None,
        context: dict,
    ) -> str:
        if paciente is None:
            await self._set_pending(
                tenant=tenant,
                phone=phone,
                reason="cancelar_turno",
                message="Paciente no identificado solicita cancelar turno.",
                title="Cancelacion de turno para revisar",
                category=ConversationCategory.HUMAN_HANDOFF,
                subtype=None,
                requires_human_review=True,
                has_media=False,
                last_patient_message=None,
                media_items=[],
            )
            return "No pude identificar tus turnos. Derivo tu solicitud para que el consultorio te contacte."
        options = await self._list_cancellable_turnos(tenant.id, paciente.id)
        if not options:
            return "No encontre turnos activos futuros para cancelar. Si necesitas ayuda, elegi hablar con una persona."
        context = dict(context or {})
        context["cancel_appointment"] = {"options": options, "selected": None}
        await self._conversacion_repo.upsert_state(
            tenant.id,
            phone,
            ConversationState.ASK_CANCEL_APPOINTMENT_SELECTION.value,
            context,
        )
        lines = ["Estos son tus turnos activos:"]
        lines.extend(f"{item['option']}) {item['label']}" for item in options)
        lines.append("Responde con el numero del turno que queres cancelar.")
        return "\n".join(lines)

    async def _list_cancellable_turnos(self, tenant_id: int, paciente_id: int) -> list[dict]:
        result = await self._session.execute(
            select(Turno, Consultorio)
            .join(Consultorio, Turno.consultorio_id == Consultorio.id)
            .where(
                Turno.tenant_id == tenant_id,
                Turno.paciente_id == paciente_id,
                Turno.deleted_at.is_(None),
                Turno.status.notin_([AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED]),
                Turno.fecha_hora >= now_ba().replace(tzinfo=None),
            )
            .order_by(Turno.fecha_hora.asc())
            .limit(10)
        )
        options = []
        for index, (turno, consultorio) in enumerate(result.all(), start=1):
            start = turno.start_at or turno.fecha_hora
            options.append(
                {
                    "option": index,
                    "turno_id": turno.id,
                    "label": f"{consultorio.nombre} - {start.strftime('%d/%m %H:%M')}",
                }
            )
        return options

    @staticmethod
    def _select_cancellable_turno(context: dict, text: str) -> dict | None:
        try:
            selected = int(normalize_message(text))
        except ValueError:
            return None
        options = (context.get("cancel_appointment") or {}).get("options") or []
        for item in options:
            if int(item.get("option") or 0) == selected:
                return item
        return None

    @staticmethod
    def _invalid_cancel_selection_message(context: dict) -> str:
        options = (context.get("cancel_appointment") or {}).get("options") or []
        lines = ["Debe seleccionar una opcion valida:"]
        lines.extend(f"{item.get('option')}) {item.get('label')}" for item in options)
        return "\n".join(lines)

    async def _cancel_turno_from_chat(
        self,
        *,
        tenant: Tenant,
        phone: str,
        turno_id: int | None,
    ) -> bool:
        if not turno_id:
            return False
        result = await self._session.execute(
            select(Turno, Consultorio, Tenant)
            .join(Consultorio, Turno.consultorio_id == Consultorio.id)
            .join(Tenant, Turno.tenant_id == Tenant.id)
            .where(Turno.id == int(turno_id), Turno.tenant_id == tenant.id)
        )
        row = result.first()
        if row is None:
            return False
        turno, consultorio, tenant_obj = row
        try:
            await AppointmentService(self._session).cancel_turno(
                request=None,
                tenant=tenant_obj,
                consultorio=consultorio,
                turno=turno,
            )
            return True
        except Exception:
            logger.exception(
                "chat_cancel_turno_failed tenant_id=%s telefono=%s turno_id=%s",
                tenant.id,
                phone,
                turno_id,
            )
            return False

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
    def _context_with_ai_result(context: dict, result: AIIntentResult) -> dict:
        merged = merge_extracted_into_context(context, result.extracted)
        ai_context = merged.setdefault("ai", {})
        ai_context["last_intent"] = result.intent
        ai_context["last_confidence"] = result.confidence
        ai_context["last_source"] = result.source
        missing_fields = result.missing_fields or get_missing_fields_for_intent(result.intent, merged)
        ai_context["missing_fields"] = missing_fields
        return merged

    @staticmethod
    def _recipe_type_to_subtype(recipe_type: str | None) -> str | None:
        normalized = normalize_message(recipe_type)
        if not normalized:
            return None
        if "orden" in normalized or "pedido" in normalized:
            return PrescriptionSubtype.MEDICAL_ORDER
        if "renov" in normalized:
            return PrescriptionSubtype.RENEW_PRESCRIPTION
        if "venc" in normalized:
            return PrescriptionSubtype.EXPIRED_PRESCRIPTION
        if "nueva" in normalized:
            return PrescriptionSubtype.NEW_PRESCRIPTION
        if "receta" in normalized or "medic" in normalized:
            return PrescriptionSubtype.OTHER_PRESCRIPTION_RELATED
        return PrescriptionSubtype.OTHER_PRESCRIPTION_RELATED

    @staticmethod
    def _ai_tools_enabled(ai_settings: dict) -> bool:
        return bool(
            ai_settings.get("enabled")
            and ai_settings.get("tools_enabled")
            and ai_settings.get("availability_lookup_enabled")
        )

    @staticmethod
    def _missing_before_availability(context: dict) -> list[str]:
        appointment = context.get("appointment") if isinstance(context.get("appointment"), dict) else {}
        missing = []
        appointment_for = context.get("for_whom") or appointment.get("for")
        if not appointment_for:
            missing.append("appointment_for")
        if appointment_for == "other" and not context.get("other_dni"):
            missing.append("other_patient_dni")
        if context.get("first_time") is None and appointment.get("is_first_time") is None:
            missing.append("is_first_time")
        return missing

    @staticmethod
    def _appointment_category_from_context(context: dict) -> str:
        appointment_type = (context.get("appointment") or {}).get("type")
        if appointment_type == "presential" or context.get("reason") == "turno_presencial":
            return ConversationCategory.PRESENTIAL_APPOINTMENT
        return ConversationCategory.VIRTUAL_APPOINTMENT

    @staticmethod
    def _appointment_label_from_context(context: dict) -> str:
        appointment_type = (context.get("appointment") or {}).get("type")
        return "presencial" if appointment_type == "presential" else "virtual"

    @staticmethod
    def _select_offered_slot(context: dict, text: str) -> dict | None:
        try:
            selected = int(normalize_message(text))
        except ValueError:
            return None
        offered = (context.get("appointment") or {}).get("offered_slots") or []
        for slot in offered:
            if int(slot.get("option") or 0) == selected:
                return slot
        return None

    @staticmethod
    def _invalid_slot_selection_message(context: dict) -> str:
        offered = (context.get("appointment") or {}).get("offered_slots") or []
        if not offered:
            return "Debe seleccionar una opcion valida."
        lines = ["Debe seleccionar una opcion valida:"]
        lines.extend(f"{slot.get('option')}) {slot.get('label')}" for slot in offered)
        return "\n".join(lines)

    @staticmethod
    def _build_ai_selected_slot_summary(context: dict) -> str:
        appointment = context.get("appointment") or {}
        selected = appointment.get("selected_slot") or {}
        return (
            f"tipo=turno_{appointment.get('type')}; "
            f"para={context.get('for_whom')}; "
            f"primera_vez={'si' if context.get('first_time') else 'no'}; "
            f"slot={selected.get('label')}; "
            f"start_at={selected.get('start_at')}; "
            f"provider={selected.get('provider')}; "
            "reserva_real=no"
        )

    @staticmethod
    def _log_ai_tool(
        *,
        tenant_id: int,
        phone: str,
        intent: str,
        tool_name: str,
        slots_count: int,
        provider: str | None,
        error: str | None,
    ) -> None:
        logger.info(
            "ai_tool_executed tenant_id=%s telefono=%s intent=%s tool=%s slots_count=%s provider=%s error=%s",
            tenant_id,
            phone,
            intent,
            tool_name,
            slots_count,
            provider or "",
            error or "",
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
        if result.intent == AIIntent.CANCEL_APPOINTMENT:
            return IntentResult("cancelar_turno", ConversationCategory.OTHER_QUERY)
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

