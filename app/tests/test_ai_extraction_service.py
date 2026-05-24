from __future__ import annotations

from app.services.ai_extraction_service import (
    get_missing_fields_for_intent,
    merge_extracted_into_context,
    normalize_extracted_data,
    should_handoff_by_extraction,
)
from app.services.ai_intent_classifier import AIIntent, classify_by_rules


def test_rules_extract_virtual_for_child_with_dni_and_time_preference() -> None:
    result = classify_by_rules(
        "Hola, quiero un turno virtual para mi hijo Juan Perez DNI 40.111.222, si puede ser el martes a la tarde"
    )

    assert result.intent == AIIntent.BOOK_VIRTUAL_APPOINTMENT
    assert result.extracted["appointment_type"] == "virtual"
    assert result.extracted["appointment_for"] == "other"
    assert result.extracted["other_patient_name"] == "Juan Perez"
    assert result.extracted["other_patient_dni"] == "40111222"
    assert result.extracted["preferred_day"] == "martes"
    assert result.extracted["preferred_time_range"] == "tarde"


def test_rules_extract_presential_first_time() -> None:
    result = classify_by_rules("Necesito turno presencial para mi, es primera vez")

    assert result.intent == AIIntent.BOOK_PRESENTIAL_APPOINTMENT
    assert result.extracted["appointment_type"] == "presential"
    assert result.extracted["appointment_for"] == "self"
    assert result.extracted["is_first_time"] is True


def test_rules_extract_recipe_detail() -> None:
    result = classify_by_rules("Necesito receta para mi medicacion habitual")

    assert result.intent == AIIntent.RECIPE_OR_ORDER
    assert result.extracted["recipe_or_order_type"] == "receta"
    assert result.extracted["medical_reason"] == "mi medicacion habitual"


def test_rules_extract_medical_order() -> None:
    result = classify_by_rules("Necesito una orden medica para analisis de sangre")

    assert result.intent == AIIntent.RECIPE_OR_ORDER
    assert result.extracted["recipe_or_order_type"] == "orden"
    assert result.extracted["medical_reason"] == "analisis de sangre"


def test_normalizes_email_and_dni() -> None:
    data = normalize_extracted_data({"email": " TEST@Example.COM ", "dni": "30.123.456"})

    assert data["email"] == "test@example.com"
    assert data["dni"] == "30123456"


def test_ambiguous_message_keeps_nulls_and_low_confidence() -> None:
    result = classify_by_rules("quiero un turno")

    assert result.intent == AIIntent.UNKNOWN
    assert result.confidence < 0.75
    assert result.extracted["appointment_for"] is None
    assert result.extracted["preferred_day"] is None


def test_merge_does_not_overwrite_existing_values_with_nulls() -> None:
    context = {"appointment": {"for": "self"}, "dni": "30111222"}
    merged = merge_extracted_into_context(context, {"appointment_for": None, "dni": None})

    assert merged["appointment"]["for"] == "self"
    assert merged["dni"] == "30111222"


def test_missing_fields_for_other_appointment() -> None:
    context = merge_extracted_into_context(
        {},
        {
            "appointment_for": "other",
            "other_patient_name": "Juan Perez",
            "other_patient_dni": "40111222",
        },
    )

    assert get_missing_fields_for_intent(AIIntent.BOOK_VIRTUAL_APPOINTMENT, context) == ["is_first_time"]


def test_high_urgency_requests_handoff() -> None:
    assert should_handoff_by_extraction({"urgency_level": "high"}) is True
