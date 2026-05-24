from __future__ import annotations

import copy
import re
from typing import Any

EXTRACTED_DEFAULTS: dict[str, Any] = {
    "patient_first_name": None,
    "patient_last_name": None,
    "dni": None,
    "email": None,
    "insurance": None,
    "insurance_number": None,
    "appointment_type": None,
    "appointment_for": None,
    "other_patient_name": None,
    "other_patient_dni": None,
    "is_first_time": None,
    "preferred_day": None,
    "preferred_date": None,
    "preferred_time": None,
    "preferred_time_range": None,
    "medical_reason": None,
    "recipe_or_order_type": None,
    "urgency_level": "unknown",
    "needs_human": False,
}


def normalize_extracted_data(data: dict[str, Any] | None) -> dict[str, Any]:
    raw = data or {}
    normalized = dict(EXTRACTED_DEFAULTS)
    for key in EXTRACTED_DEFAULTS:
        value = raw.get(key)
        normalized[key] = _normalize_value(key, value)
    return normalized


def merge_extracted_into_context(context: dict[str, Any] | None, extracted: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(context or {})
    data = normalize_extracted_data(extracted)

    ai_context = _ensure_dict(merged, "ai")
    ai_extracted = _ensure_dict(ai_context, "extracted")
    for key, value in data.items():
        _set_if_present(ai_extracted, key, value)

    patient = _ensure_dict(merged, "patient")
    _set_if_present(patient, "first_name", data["patient_first_name"])
    _set_if_present(patient, "last_name", data["patient_last_name"])
    _set_if_present(patient, "dni", data["dni"])
    _set_if_present(patient, "email", data["email"])
    _set_if_present(patient, "insurance", data["insurance"])
    _set_if_present(patient, "insurance_number", data["insurance_number"])

    appointment = _ensure_dict(merged, "appointment")
    _set_if_present(appointment, "type", data["appointment_type"])
    _set_if_present(appointment, "for", data["appointment_for"])
    _set_if_present(appointment, "is_first_time", data["is_first_time"])
    _set_if_present(appointment, "preferred_day", data["preferred_day"])
    _set_if_present(appointment, "preferred_date", data["preferred_date"])
    _set_if_present(appointment, "preferred_time", data["preferred_time"])
    _set_if_present(appointment, "preferred_time_range", data["preferred_time_range"])

    other_patient = _ensure_dict(merged, "other_patient")
    _set_if_present(other_patient, "name", data["other_patient_name"])
    _set_if_present(other_patient, "dni", data["other_patient_dni"])

    recipe_order = _ensure_dict(merged, "recipe_order")
    _set_if_present(recipe_order, "type", data["recipe_or_order_type"])
    _set_if_present(recipe_order, "detail", data["medical_reason"])

    # Flat aliases keep compatibility with the existing state machine.
    _set_if_present(merged, "dni", data["dni"])
    _set_if_present(merged, "email", data["email"])
    _set_if_present(merged, "insurance", data["insurance"])
    _set_if_present(merged, "insurance_number", data["insurance_number"])
    _set_if_present(merged, "for_whom", data["appointment_for"])
    if data["is_first_time"] is not None:
        merged.setdefault("first_time", data["is_first_time"])
    _set_if_present(merged, "other_dni", data["other_patient_dni"])
    if data["other_patient_name"]:
        first_name, last_name = split_patient_name(data["other_patient_name"])
        _set_if_present(merged, "other_first_name", first_name)
        _set_if_present(merged, "other_last_name", last_name)
    return merged


def get_missing_fields_for_intent(intent: str, context: dict[str, Any] | None) -> list[str]:
    current = context or {}
    appointment = current.get("appointment") if isinstance(current.get("appointment"), dict) else {}
    other_patient = current.get("other_patient") if isinstance(current.get("other_patient"), dict) else {}
    recipe_order = current.get("recipe_order") if isinstance(current.get("recipe_order"), dict) else {}

    if intent in {"book_presential_appointment", "book_virtual_appointment"}:
        missing: list[str] = []
        appointment_for = current.get("for_whom") or appointment.get("for")
        if not appointment_for:
            missing.append("appointment_for")
        if appointment_for == "other":
            if not (other_patient.get("name") or current.get("other_first_name")):
                missing.append("other_patient_name")
            if not (other_patient.get("dni") or current.get("other_dni")):
                missing.append("other_patient_dni")
        if current.get("first_time") is None and appointment.get("is_first_time") is None:
            missing.append("is_first_time")
        return missing

    if intent == "recipe_or_order":
        missing = []
        if not (recipe_order.get("type") or current.get("recipe_kind")):
            missing.append("recipe_or_order_type")
        if not recipe_order.get("detail"):
            missing.append("medical_reason")
        return missing

    return []


def should_handoff_by_extraction(extracted: dict[str, Any] | None) -> bool:
    data = normalize_extracted_data(extracted)
    return bool(data["needs_human"]) or data["urgency_level"] == "high"


def split_patient_name(value: str | None) -> tuple[str | None, str | None]:
    parts = [part for part in (value or "").strip().split() if part]
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def _ensure_dict(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        value = {}
        container[key] = value
    return value


def _set_if_present(container: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str) and not value.strip():
        return
    if key in container and container[key] not in (None, ""):
        return
    container[key] = value


def _normalize_value(key: str, value: Any) -> Any:
    if value is None:
        return EXTRACTED_DEFAULTS[key]
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return EXTRACTED_DEFAULTS[key]

    if key in {"dni", "other_patient_dni"}:
        digits = re.sub(r"\D+", "", str(value))
        return digits or None
    if key == "email":
        email = str(value).strip().lower()
        return email if "@" in email and "." in email.split("@")[-1] else None
    if key == "appointment_type":
        text = str(value).strip().lower()
        if text in {"presential", "presencial", "consultorio"}:
            return "presential"
        if text in {"virtual", "online", "videollamada", "video llamada"}:
            return "virtual"
        return None
    if key == "appointment_for":
        text = str(value).strip().lower()
        if text in {"self", "yo", "para mi", "mi"}:
            return "self"
        if text in {"other", "otro", "otra", "otra persona", "hijo", "hija", "familiar"}:
            return "other"
        return None
    if key == "is_first_time":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "si", "s", "primera vez", "1"}:
            return True
        if text in {"false", "no", "n", "ya se atiende", "2"}:
            return False
        return None
    if key == "urgency_level":
        text = str(value).strip().lower()
        return text if text in {"low", "medium", "high", "unknown"} else "unknown"
    if key == "needs_human":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "si", "1", "yes"}
    return str(value).strip() if isinstance(value, str) else value
