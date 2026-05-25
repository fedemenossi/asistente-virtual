from __future__ import annotations

from typing import Any


INTENT_LABELS = {
    "book_presential_appointment": "Turno presencial",
    "book_virtual_appointment": "Turno virtual",
    "recipe_or_order": "Receta u orden",
    "other_medical_query": "Otra consulta",
    "human_handoff": "Derivacion humana",
    "cancel_appointment": "Cancelacion de turno",
    "reschedule_appointment": "Reprogramacion de turno",
    "greeting": "Saludo",
    "exit": "Salida",
    "unknown": "Sin identificar",
}

FIELD_LABELS = {
    "patient_first_name": "Nombre",
    "patient_last_name": "Apellido",
    "dni": "DNI",
    "email": "Email",
    "insurance": "Obra social",
    "insurance_number": "Afiliado",
    "appointment_type": "Tipo de turno",
    "appointment_for": "Para quien",
    "other_patient_name": "Nombre paciente",
    "other_patient_dni": "DNI",
    "is_first_time": "Primera vez",
    "preferred_day": "Dia preferido",
    "preferred_date": "Fecha preferida",
    "preferred_time": "Hora preferida",
    "preferred_time_range": "Franja preferida",
    "medical_reason": "Detalle",
    "recipe_or_order_type": "Tipo de solicitud",
    "urgency_level": "Urgencia",
    "needs_human": "Requiere humano",
}

MISSING_FIELD_LABELS = {
    "appointment_for": "para quien",
    "other_patient_name": "nombre del paciente",
    "other_patient_dni": "DNI del paciente",
    "is_first_time": "primera vez",
    "recipe_or_order_type": "tipo de receta u orden",
    "medical_reason": "detalle",
}

VALUE_LABELS = {
    "presential": "Presencial",
    "virtual": "Virtual",
    "self": "Paciente",
    "other": "Otra persona",
    "low": "Baja",
    "medium": "Media",
    "high": "Alta",
    "unknown": "Sin determinar",
    True: "Si",
    False: "No",
}


def get_ai_summary_from_context(context: dict | None, *, mask_sensitive: bool = False) -> dict[str, Any]:
    if not isinstance(context, dict):
        return _empty_summary()
    ai = context.get("ai")
    if not isinstance(ai, dict):
        return _empty_summary()

    intent = ai.get("last_intent")
    confidence = _safe_float(ai.get("last_confidence"))
    extracted = ai.get("extracted") if isinstance(ai.get("extracted"), dict) else {}
    missing_fields = ai.get("missing_fields") if isinstance(ai.get("missing_fields"), list) else []
    if not should_show_ai_summary(context):
        return _empty_summary()

    urgency_level = _string(extracted.get("urgency_level")) or "unknown"
    needs_human = bool(extracted.get("needs_human"))
    return {
        "show": True,
        "intent": intent or "unknown",
        "intent_label": format_intent_label(intent),
        "confidence": confidence,
        "confidence_label": format_confidence(confidence),
        "confidence_level": get_confidence_badge_level(confidence),
        "confidence_badge_label": _confidence_badge_label(confidence),
        "source": _string(ai.get("last_source")) or "-",
        "needs_human": needs_human,
        "urgency_level": urgency_level,
        "urgency_label": VALUE_LABELS.get(urgency_level, urgency_level),
        "missing_fields": format_missing_fields(missing_fields),
        "extracted_fields": format_extracted_fields(extracted, mask_sensitive=mask_sensitive),
        "updated_at": ai.get("updated_at"),
    }


def format_intent_label(intent: str | None) -> str:
    return INTENT_LABELS.get(_string(intent) or "", _string(intent) or "Sin identificar")


def format_confidence(confidence: float | None) -> str:
    value = _safe_float(confidence)
    if value is None:
        return "-"
    return f"{round(value * 100):.0f}%"


def get_confidence_badge_level(confidence: float | None) -> str:
    value = _safe_float(confidence)
    if value is None:
        return "unknown"
    if value >= 0.75:
        return "high"
    if value >= 0.5:
        return "medium"
    return "low"


def format_extracted_fields(extracted: dict | None, *, mask_sensitive: bool = False) -> list[dict[str, str]]:
    if not isinstance(extracted, dict):
        return []
    fields: list[dict[str, str]] = []
    preference = _format_preference(extracted)
    for key, label in FIELD_LABELS.items():
        if key in {"preferred_day", "preferred_date", "preferred_time", "preferred_time_range"}:
            continue
        value = extracted.get(key)
        if value is None or value == "":
            continue
        if key == "urgency_level" and value == "unknown":
            continue
        display = _format_value(key, value, mask_sensitive=mask_sensitive)
        if display:
            fields.append({"label": label, "value": display})
    if preference:
        fields.append({"label": "Preferencia", "value": preference})
    return fields


def format_missing_fields(missing_fields: list[str] | None) -> list[str]:
    if not isinstance(missing_fields, list):
        return []
    formatted = []
    for item in missing_fields:
        label = MISSING_FIELD_LABELS.get(_string(item) or "", _string(item) or "")
        if label:
            formatted.append(label)
    return formatted


def should_show_ai_summary(context: dict | None) -> bool:
    if not isinstance(context, dict):
        return False
    ai = context.get("ai")
    if not isinstance(ai, dict):
        return False
    return bool(ai.get("last_intent") or ai.get("last_confidence") is not None or ai.get("extracted"))


def sanitize_context_for_display(context: dict | None) -> dict:
    if not isinstance(context, dict):
        return {}
    sanitized = _sanitize_value(context)
    return sanitized if isinstance(sanitized, dict) else {}


def _empty_summary() -> dict[str, Any]:
    return {"show": False}


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, child in value.items():
            normalized_key = str(key).lower()
            if normalized_key in {"raw_response", "api_key", "system_prompt", "prompt"}:
                continue
            cleaned[key] = _sanitize_value(child)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def _format_preference(extracted: dict) -> str | None:
    parts = [
        extracted.get("preferred_date") or extracted.get("preferred_day"),
        extracted.get("preferred_time") or extracted.get("preferred_time_range"),
    ]
    label = " a la ".join(str(part).strip() for part in parts if part)
    return label or None


def _format_value(key: str, value: Any, *, mask_sensitive: bool) -> str | None:
    if isinstance(value, bool):
        return VALUE_LABELS[value]
    text = str(VALUE_LABELS.get(value, value)).strip()
    if not text:
        return None
    if mask_sensitive and key in {"dni", "other_patient_dni"}:
        return _mask_dni(text)
    return text


def _mask_dni(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) <= 3:
        return "***"
    return f"***{digits[-3:]}"


def _confidence_badge_label(confidence: float | None) -> str:
    level = get_confidence_badge_level(confidence)
    return {"high": "Alta", "medium": "Media", "low": "Baja"}.get(level, "Sin dato")


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return None


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
