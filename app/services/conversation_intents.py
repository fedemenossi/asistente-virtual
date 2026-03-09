from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


class ConversationCategory:
    PRESENTIAL_APPOINTMENT = "PRESENTIAL_APPOINTMENT"
    VIRTUAL_APPOINTMENT = "VIRTUAL_APPOINTMENT"
    PRESCRIPTION_OR_ORDER = "PRESCRIPTION_OR_ORDER"
    OTHER_QUERY = "OTHER_QUERY"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"


class PrescriptionSubtype:
    NEW_PRESCRIPTION = "NEW_PRESCRIPTION"
    RENEW_PRESCRIPTION = "RENEW_PRESCRIPTION"
    EXPIRED_PRESCRIPTION = "EXPIRED_PRESCRIPTION"
    MEDICAL_ORDER = "MEDICAL_ORDER"
    OTHER_PRESCRIPTION_RELATED = "OTHER_PRESCRIPTION_RELATED"


PENDING_REASON_BY_CATEGORY = {
    ConversationCategory.PRESENTIAL_APPOINTMENT: "turno_presencial",
    ConversationCategory.VIRTUAL_APPOINTMENT: "turno_virtual",
    ConversationCategory.PRESCRIPTION_OR_ORDER: "receta_orden",
    ConversationCategory.OTHER_QUERY: "otra_consulta",
    ConversationCategory.HUMAN_HANDOFF: "humano",
}


CATEGORY_LABELS = {
    ConversationCategory.PRESENTIAL_APPOINTMENT: "Turno presencial",
    ConversationCategory.VIRTUAL_APPOINTMENT: "Turno virtual",
    ConversationCategory.PRESCRIPTION_OR_ORDER: "Receta / orden",
    ConversationCategory.OTHER_QUERY: "Otra consulta",
    ConversationCategory.HUMAN_HANDOFF: "Humano",
}


SUBTYPE_LABELS = {
    PrescriptionSubtype.NEW_PRESCRIPTION: "Receta nueva",
    PrescriptionSubtype.RENEW_PRESCRIPTION: "Renovar receta",
    PrescriptionSubtype.EXPIRED_PRESCRIPTION: "Receta vencida",
    PrescriptionSubtype.MEDICAL_ORDER: "Orden medica",
    PrescriptionSubtype.OTHER_PRESCRIPTION_RELATED: "Otro receta/orden",
}


AFFIRMATIVE_TOKENS = {
    "si",
    "s",
    "dale",
    "ok",
    "oka",
    "bueno",
    "correcto",
    "confirmo",
    "perfecto",
    "de acuerdo",
}

NEGATIVE_TOKENS = {
    "no",
    "nop",
    "cancelar",
    "mejor no",
    "deja",
    "dejalo",
    "olvidalo",
}


@dataclass
class IntentResult:
    pending_reason: str | None
    category: str | None
    requires_clarification: bool = False


def normalize_text(value: str | None) -> str:
    raw = (value or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", raw)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def is_affirmative(value: str | None) -> bool:
    text = normalize_text(value)
    return text in AFFIRMATIVE_TOKENS or text in {"1", "yes", "y"}


def is_negative(value: str | None) -> bool:
    text = normalize_text(value)
    return text in NEGATIVE_TOKENS or text in {"2", "n"}


def detect_yes_no(value: str | None) -> bool | None:
    if is_affirmative(value):
        return True
    if is_negative(value):
        return False
    return None


def detect_for_whom(value: str | None) -> str | None:
    text = normalize_text(value)
    if text in {"1", "yo", "para mi", "mi", "self"}:
        return "self"
    if text in {"2", "otra persona", "otro", "otra", "tercero", "familiar"}:
        return "other"
    return None


def classify_main_intent(value: str | None) -> IntentResult:
    text = normalize_text(value)
    if not text:
        return IntentResult(None, None)

    if text in {"1", "a"}:
        return IntentResult(
            pending_reason="turno_presencial",
            category=ConversationCategory.PRESENTIAL_APPOINTMENT,
        )
    if text in {"2", "b"}:
        return IntentResult(
            pending_reason="turno_virtual",
            category=ConversationCategory.VIRTUAL_APPOINTMENT,
        )
    if text in {"3", "c"}:
        return IntentResult(
            pending_reason="receta_orden",
            category=ConversationCategory.PRESCRIPTION_OR_ORDER,
        )
    if text in {"4", "d"}:
        return IntentResult(
            pending_reason="otra_consulta",
            category=ConversationCategory.OTHER_QUERY,
        )
    if text in {"5", "e"}:
        return IntentResult(
            pending_reason="humano",
            category=ConversationCategory.HUMAN_HANDOFF,
        )

    human_keywords = ("humano", "persona", "secretaria", "doctora", "doctor", "asesor")
    if any(token in text for token in human_keywords):
        return IntentResult(
            pending_reason="humano",
            category=ConversationCategory.HUMAN_HANDOFF,
        )

    prescription_keywords = (
        "receta",
        "medicacion",
        "medicamento",
        "orden",
        "pedido medico",
        "estudio",
        "analisis",
        "practica",
    )
    if any(token in text for token in prescription_keywords):
        return IntentResult(
            pending_reason="receta_orden",
            category=ConversationCategory.PRESCRIPTION_OR_ORDER,
        )

    virtual_keywords = ("virtual", "online", "videollamada", "video llamada")
    if any(token in text for token in virtual_keywords):
        return IntentResult(
            pending_reason="turno_virtual",
            category=ConversationCategory.VIRTUAL_APPOINTMENT,
        )

    presencial_keywords = ("presencial", "consultorio", "en persona")
    if any(token in text for token in presencial_keywords):
        return IntentResult(
            pending_reason="turno_presencial",
            category=ConversationCategory.PRESENTIAL_APPOINTMENT,
        )

    if "turno" in text or "atenderme" in text:
        return IntentResult(None, None, requires_clarification=True)

    if "consulta" in text or "duda" in text or "ayuda" in text:
        return IntentResult(
            pending_reason="otra_consulta",
            category=ConversationCategory.OTHER_QUERY,
        )

    return IntentResult(None, None)


def detect_prescription_subtype(value: str | None) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    if text == "1":
        return PrescriptionSubtype.NEW_PRESCRIPTION
    if text == "2":
        return PrescriptionSubtype.RENEW_PRESCRIPTION
    if text == "3":
        return PrescriptionSubtype.EXPIRED_PRESCRIPTION
    if text == "4":
        return PrescriptionSubtype.MEDICAL_ORDER
    if text == "5":
        return PrescriptionSubtype.OTHER_PRESCRIPTION_RELATED

    if "vencida" in text or "vencio" in text:
        return PrescriptionSubtype.EXPIRED_PRESCRIPTION
    if "renov" in text:
        return PrescriptionSubtype.RENEW_PRESCRIPTION
    if "nueva" in text:
        return PrescriptionSubtype.NEW_PRESCRIPTION
    if "orden" in text or "pedido medico" in text or "estudio" in text or "analisis" in text:
        return PrescriptionSubtype.MEDICAL_ORDER
    if "receta" in text or "medic" in text:
        return PrescriptionSubtype.OTHER_PRESCRIPTION_RELATED
    return None
