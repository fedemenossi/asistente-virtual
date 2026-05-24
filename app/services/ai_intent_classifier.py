from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
import unicodedata
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.ai_extraction_service import EXTRACTED_DEFAULTS, normalize_extracted_data

logger = logging.getLogger(__name__)


class AIIntent:
    BOOK_PRESENTIAL_APPOINTMENT = "book_presential_appointment"
    BOOK_VIRTUAL_APPOINTMENT = "book_virtual_appointment"
    RECIPE_OR_ORDER = "recipe_or_order"
    OTHER_MEDICAL_QUERY = "other_medical_query"
    HUMAN_HANDOFF = "human_handoff"
    CANCEL_APPOINTMENT = "cancel_appointment"
    RESCHEDULE_APPOINTMENT = "reschedule_appointment"
    GREETING = "greeting"
    EXIT = "exit"
    UNKNOWN = "unknown"


SUPPORTED_INTENTS = {
    AIIntent.BOOK_PRESENTIAL_APPOINTMENT,
    AIIntent.BOOK_VIRTUAL_APPOINTMENT,
    AIIntent.RECIPE_OR_ORDER,
    AIIntent.OTHER_MEDICAL_QUERY,
    AIIntent.HUMAN_HANDOFF,
    AIIntent.CANCEL_APPOINTMENT,
    AIIntent.RESCHEDULE_APPOINTMENT,
    AIIntent.GREETING,
    AIIntent.EXIT,
    AIIntent.UNKNOWN,
}


@dataclass
class AIIntentResult:
    intent: str
    confidence: float
    extracted: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    raw_response: dict[str, Any] | None = None
    source: str = "fallback"
    error: str | None = None


def normalize_message(message: str | None) -> str:
    raw = (message or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", raw)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _result(
    intent: str,
    confidence: float,
    *,
    source: str = "rules",
    extracted: dict[str, Any] | None = None,
    missing_fields: list[str] | None = None,
    raw_response: dict[str, Any] | None = None,
    error: str | None = None,
) -> AIIntentResult:
    safe_extracted = normalize_extracted_data(extracted or {})
    if intent not in SUPPORTED_INTENTS:
        intent = AIIntent.UNKNOWN
        confidence = min(confidence, 0.25)
    return AIIntentResult(
        intent=intent,
        confidence=max(0.0, min(float(confidence), 1.0)),
        extracted=safe_extracted,
        missing_fields=list(missing_fields or []),
        raw_response=raw_response,
        source=source,
        error=error,
    )


def classify_by_rules(message: str) -> AIIntentResult:
    text = normalize_message(message)
    extracted = _extract_by_rules(text)
    if not text:
        return _result(AIIntent.UNKNOWN, 0.0, source="fallback")

    if extracted.get("needs_human") or extracted.get("urgency_level") == "high":
        return _result(AIIntent.HUMAN_HANDOFF, 0.92, extracted=extracted)

    if text in {"salir", "cancelar", "exit", "reiniciar", "menu"}:
        return _result(AIIntent.EXIT, 0.98)

    if any(token in text for token in ("reprogramar", "cambiar turno", "mover turno")):
        return _result(AIIntent.RESCHEDULE_APPOINTMENT, 0.9)

    if "cancelar turno" in text or "anular turno" in text or "dar de baja turno" in text:
        return _result(AIIntent.CANCEL_APPOINTMENT, 0.9)

    if any(
        token in text
        for token in (
            "hablar con una persona",
            "hablar con alguien",
            "me atiende alguien",
            "secretaria",
            "humano",
            "asistente humano",
        )
    ):
        return _result(AIIntent.HUMAN_HANDOFF, 0.92)

    if any(
        token in text
        for token in (
            "receta",
            "orden medica",
            "orden médica",
            "pedido medico",
            "pedido médico",
            "medicamento",
            "medicacion",
            "medicación",
        )
    ):
        return _result(AIIntent.RECIPE_OR_ORDER, 0.9, extracted=extracted)

    if any(
        token in text
        for token in (
            "turno presencial",
            "consulta presencial",
            "turno en consultorio",
            "en consultorio",
            "atenderme en consultorio",
        )
    ):
        return _result(
            AIIntent.BOOK_PRESENTIAL_APPOINTMENT,
            0.91,
            extracted={**extracted, "appointment_type": "presential"},
        )

    if any(
        token in text
        for token in (
            "consulta virtual",
            "turno virtual",
            "videollamada",
            "video llamada",
            "atencion virtual",
            "atención virtual",
            "online",
        )
    ):
        return _result(
            AIIntent.BOOK_VIRTUAL_APPOINTMENT,
            0.91,
            extracted={**extracted, "appointment_type": "virtual"},
        )

    if text in {"hola", "buen dia", "buenas", "buenas tardes", "buenas noches"}:
        return _result(AIIntent.GREETING, 0.85)

    if "turno" in text:
        return _result(AIIntent.UNKNOWN, 0.45)

    if "consulta" in text or "dolor" in text or "sintoma" in text or "síntoma" in text:
        return _result(AIIntent.OTHER_MEDICAL_QUERY, 0.78, extracted=extracted)

    return _result(AIIntent.UNKNOWN, 0.2, source="fallback", extracted=extracted)


def _extract_by_rules(text: str) -> dict[str, Any]:
    extracted: dict[str, Any] = {}
    if not text:
        return extracted

    dni_match = re.search(r"\b(?:dni|documento)?\s*(\d[\d\.\-\s]{5,12}\d)\b", text)
    if dni_match:
        dni = re.sub(r"\D+", "", dni_match.group(1))
        if dni:
            extracted["dni"] = dni

    email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
    if email_match:
        extracted["email"] = email_match.group(0)

    if any(token in text for token in ("para mi hijo", "para mi hija", "mi hijo", "mi hija", "para otra persona")):
        extracted["appointment_for"] = "other"
    elif any(token in text for token in ("para mi", "para mí", "para yo", "es para mi")):
        extracted["appointment_for"] = "self"

    other_name = _extract_other_patient_name(text)
    if other_name:
        extracted["other_patient_name"] = other_name
        if extracted.get("dni"):
            extracted["other_patient_dni"] = extracted["dni"]
            extracted.pop("dni", None)

    if "primera vez" in text or "nunca se atendio" in text or "nunca me atendio" in text:
        extracted["is_first_time"] = True
    if "no es primera vez" in text or "ya se atiende" in text or "ya me atiendo" in text:
        extracted["is_first_time"] = False

    for day in ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"):
        if day in text:
            extracted["preferred_day"] = day
            break

    time_match = re.search(r"\b(?:a las|tipo|sobre las)\s*(\d{1,2})(?::?(\d{2}))?\b", text)
    if time_match:
        hour = time_match.group(1)
        minutes = time_match.group(2) or "00"
        extracted["preferred_time"] = f"{hour}:{minutes}"
    elif "manana" in text or "mañana" in text:
        extracted["preferred_time_range"] = "manana"
    elif "tarde" in text:
        extracted["preferred_time_range"] = "tarde"
    elif "noche" in text:
        extracted["preferred_time_range"] = "noche"

    if any(token in text for token in ("orden medica", "orden médica", "pedido medico", "pedido médico")):
        extracted["recipe_or_order_type"] = "orden"
    elif "receta" in text:
        extracted["recipe_or_order_type"] = "receta"

    reason = _extract_medical_reason(text)
    if reason:
        extracted["medical_reason"] = reason

    if any(
        token in text
        for token in (
            "urgente",
            "emergencia",
            "dolor fuerte",
            "dolor en el pecho",
            "falta de aire",
            "dificultad para respirar",
            "sangrado",
            "desmayo",
        )
    ):
        extracted["urgency_level"] = "high"
        extracted["needs_human"] = True
    return extracted


def _extract_other_patient_name(text: str) -> str | None:
    match = re.search(
        r"(?:para mi hijo|para mi hija|mi hijo|mi hija|para)\s+([a-záéíóúñ]+(?:\s+[a-záéíóúñ]+){0,3})(?:\s+dni|\s+documento|,|$)",
        text,
    )
    if not match:
        return None
    name = match.group(1).strip()
    stop_words = {"otra persona", "mi", "hijo", "hija"}
    if name in stop_words:
        return None
    return " ".join(part.capitalize() for part in name.split())


def _extract_medical_reason(text: str) -> str | None:
    match = re.search(r"(?:receta|orden medica|orden médica|pedido medico|pedido médico)\s+(?:para|por|de)?\s*(.+)", text)
    if not match:
        if "medicacion habitual" in text:
            return "medicacion habitual"
        return None
    reason = match.group(1).strip(" .")
    if not reason:
        return None
    reason = re.sub(r"\b(?:dni|documento)\b.*$", "", reason).strip(" .")
    return reason or None


class AIIntentClassifier:
    def __init__(self) -> None:
        self._settings = get_settings()

    async def classify(
        self,
        message: str,
        *,
        tenant=None,
        ai_settings: dict[str, Any] | None = None,
        conversation_state: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AIIntentResult:
        del conversation_state, context
        effective = self._effective_settings(tenant, ai_settings)
        rules_result = classify_by_rules(message)
        if not effective["enabled"]:
            return rules_result
        if not self._intent_allowed(rules_result.intent, effective):
            rules_result = _result(AIIntent.UNKNOWN, 0.2, source="fallback")
        api_key = (effective.get("api_key") or "").strip() or (self._settings.openai_api_key or "").strip()
        if not api_key:
            return _result(
                rules_result.intent,
                rules_result.confidence,
                source=rules_result.source,
                extracted=rules_result.extracted,
                error="API key IA no configurada",
            )
        return await self.classify_with_ai(
            message,
            ai_settings=effective,
            api_key=api_key,
            fallback=rules_result,
        )

    async def classify_with_ai(
        self,
        message: str,
        *,
        ai_settings: dict[str, Any],
        api_key: str,
        fallback: AIIntentResult | None = None,
    ) -> AIIntentResult:
        allowed_intents = [
            intent for intent in ai_settings.get("allowed_intents", []) if intent in SUPPORTED_INTENTS
        ] or sorted(SUPPORTED_INTENTS)
        agent_name = ai_settings.get("agent_name") or "Asistente virtual"
        personality = ai_settings.get("personality") or "cordial, clara, profesional y breve"
        tenant_prompt = ai_settings.get("system_prompt") or ""
        prompt = (
            f"Sos {agent_name}, un clasificador de intencion para un consultorio medico. "
            f"Personalidad operativa: {personality}. "
            "Analiza el mensaje libre del paciente, clasifica la intencion y extrae datos utiles. "
            "Devolve SOLO JSON valido. "
            "No respondas al paciente, no inventes acciones, no ejecutes herramientas, "
            "no indiques reservas, pagos ni cancelaciones realizadas. "
            "No diagnostiques, no sugieras tratamientos y no des consejo medico. "
            "Si detectas una posible urgencia medica, usa urgency_level=high y needs_human=true. "
            "No guardes ni repitas datos clinicos sensibles innecesarios. "
            "Usa null cuando no haya un dato claro. Si no estas seguro, baja la confidence. "
            f"Instruccion adicional del tenant: {tenant_prompt}. "
            "Intents validos: "
            + ", ".join(sorted(allowed_intents))
            + ". Formato exacto: "
            '{"intent":"...","confidence":0.0,"extracted":{...},"missing_fields":[],"needs_human":false}'
        )
        payload = {
            "model": ai_settings["model"],
            "input": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": normalize_message(message)},
            ],
            "temperature": float(ai_settings["temperature"]),
            "max_output_tokens": int(ai_settings["max_tokens"]),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "medical_intent_classifier",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["intent", "confidence", "extracted", "missing_fields", "needs_human"],
                        "properties": {
                            "intent": {"type": "string", "enum": sorted(allowed_intents)},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "missing_fields": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "needs_human": {"type": "boolean"},
                            "extracted": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": list(EXTRACTED_DEFAULTS.keys()),
                                "properties": _extracted_json_schema_properties(),
                            },
                        },
                    },
                }
            },
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=float(ai_settings["timeout_seconds"])
            ) as client:
                response = await client.post(
                    "https://api.openai.com/v1/responses",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                raw = response.json()
            parsed = _parse_openai_response(raw)
            if parsed.get("intent") not in allowed_intents:
                return _result(
                    AIIntent.UNKNOWN,
                    0.0,
                    source="fallback",
                    raw_response=raw,
                    error="intent_not_allowed",
                )
            return _result(
                parsed.get("intent") or AIIntent.UNKNOWN,
                float(parsed.get("confidence") or 0.0),
                source="ai",
                extracted={
                    **(parsed.get("extracted") or {}),
                    "needs_human": bool(parsed.get("needs_human") or (parsed.get("extracted") or {}).get("needs_human")),
                },
                missing_fields=parsed.get("missing_fields") or [],
                raw_response=raw,
            )
        except Exception as exc:
            logger.warning("ai_intent_classifier_failed error=%s", type(exc).__name__)
            if fallback is not None:
                fallback.error = type(exc).__name__
                return fallback
            return _result(AIIntent.UNKNOWN, 0.0, source="fallback", error=type(exc).__name__)

    @staticmethod
    def _effective_settings(tenant, ai_settings: dict[str, Any] | None) -> dict[str, Any]:
        from app.services.tenant_ai_settings_service import get_effective_ai_settings, validate_ai_settings

        if ai_settings is not None:
            return validate_ai_settings(ai_settings, allow_global_fallback=True)
        return get_effective_ai_settings(tenant)

    @staticmethod
    def _intent_allowed(intent: str, ai_settings: dict[str, Any]) -> bool:
        allowed = ai_settings.get("allowed_intents") or []
        return intent in allowed or intent == AIIntent.UNKNOWN


def _parse_openai_response(raw: dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw.get("output_text"), str):
        return json.loads(raw["output_text"])
    for item in raw.get("output", []) or []:
        for content in item.get("content", []) or []:
            text = content.get("text")
            if text:
                return json.loads(text)
    raise ValueError("Respuesta IA sin JSON parseable")


def _extracted_json_schema_properties() -> dict[str, Any]:
    string_or_null = {"type": ["string", "null"]}
    return {
        "patient_first_name": string_or_null,
        "patient_last_name": string_or_null,
        "dni": string_or_null,
        "email": string_or_null,
        "insurance": string_or_null,
        "insurance_number": string_or_null,
        "appointment_type": {"type": ["string", "null"], "enum": ["presential", "virtual", None]},
        "appointment_for": {"type": ["string", "null"], "enum": ["self", "other", None]},
        "other_patient_name": string_or_null,
        "other_patient_dni": string_or_null,
        "is_first_time": {"type": ["boolean", "null"]},
        "preferred_day": string_or_null,
        "preferred_date": string_or_null,
        "preferred_time": string_or_null,
        "preferred_time_range": string_or_null,
        "medical_reason": string_or_null,
        "recipe_or_order_type": string_or_null,
        "urgency_level": {"type": "string", "enum": ["low", "medium", "high", "unknown"]},
        "needs_human": {"type": "boolean"},
    }
