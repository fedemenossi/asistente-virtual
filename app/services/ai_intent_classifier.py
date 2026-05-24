from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
import unicodedata
from typing import Any

import httpx

from app.core.config import get_settings

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
    raw_response: dict[str, Any] | None = None
    source: str = "fallback"
    error: str | None = None


EXTRACTED_DEFAULTS = {
    "appointment_type": None,
    "is_first_time": None,
    "patient_name": None,
    "dni": None,
    "preferred_day": None,
    "preferred_time": None,
    "reason": None,
}


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
    raw_response: dict[str, Any] | None = None,
    error: str | None = None,
) -> AIIntentResult:
    safe_extracted = {**EXTRACTED_DEFAULTS, **(extracted or {})}
    if intent not in SUPPORTED_INTENTS:
        intent = AIIntent.UNKNOWN
        confidence = min(confidence, 0.25)
    return AIIntentResult(
        intent=intent,
        confidence=max(0.0, min(float(confidence), 1.0)),
        extracted=safe_extracted,
        raw_response=raw_response,
        source=source,
        error=error,
    )


def classify_by_rules(message: str) -> AIIntentResult:
    text = normalize_message(message)
    if not text:
        return _result(AIIntent.UNKNOWN, 0.0, source="fallback")

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
        return _result(AIIntent.RECIPE_OR_ORDER, 0.9)

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
            extracted={"appointment_type": "presential"},
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
            extracted={"appointment_type": "virtual"},
        )

    if text in {"hola", "buen dia", "buenas", "buenas tardes", "buenas noches"}:
        return _result(AIIntent.GREETING, 0.85)

    if "turno" in text:
        return _result(AIIntent.UNKNOWN, 0.45)

    if "consulta" in text or "dolor" in text or "sintoma" in text or "síntoma" in text:
        return _result(AIIntent.OTHER_MEDICAL_QUERY, 0.78)

    return _result(AIIntent.UNKNOWN, 0.2, source="fallback")


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
            "Analiza el mensaje libre del paciente y devolve SOLO JSON valido. "
            "No respondas al paciente, no inventes acciones, no ejecutes herramientas, "
            "no indiques reservas, pagos ni cancelaciones realizadas. "
            "No guardes ni repitas datos clinicos sensibles innecesarios. "
            "Usa null cuando no haya un dato claro. is_first_time debe ser true, false o null. "
            f"Instruccion adicional del tenant: {tenant_prompt}. "
            "Intents validos: "
            + ", ".join(sorted(allowed_intents))
            + ". Formato exacto: "
            '{"intent":"...","confidence":0.0,"extracted":{"appointment_type":"presential|virtual|null",'
            '"is_first_time":null,"patient_name":null,"dni":null,"preferred_day":null,'
            '"preferred_time":null,"reason":null}}'
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
                        "required": ["intent", "confidence", "extracted"],
                        "properties": {
                            "intent": {"type": "string", "enum": sorted(allowed_intents)},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "extracted": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": list(EXTRACTED_DEFAULTS.keys()),
                                "properties": {
                                    "appointment_type": {
                                        "type": ["string", "null"],
                                        "enum": ["presential", "virtual", None],
                                    },
                                    "is_first_time": {"type": ["boolean", "null"]},
                                    "patient_name": {"type": ["string", "null"]},
                                    "dni": {"type": ["string", "null"]},
                                    "preferred_day": {"type": ["string", "null"]},
                                    "preferred_time": {"type": ["string", "null"]},
                                    "reason": {"type": ["string", "null"]},
                                },
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
                extracted=parsed.get("extracted") or {},
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
